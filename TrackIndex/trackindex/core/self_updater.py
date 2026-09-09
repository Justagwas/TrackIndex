from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http.client import IncompleteRead
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import (
    APP_NAME,
    APP_VERSION,
    INNO_SETUP_APP_ID,
    OFFICIAL_PAGE_URL,
    UPDATE_CHECK_TIMEOUT_SECONDS,
    UPDATE_MANIFEST_URL,
    UPDATE_SETUP_FALLBACK_URL,
)
from .filesystem import is_link_like
from .paths import app_dir, updates_dir

_ALLOWED_HOSTS = {
    "justagwas.com",
    "www.justagwas.com",
    "downloads.justagwas.com",
}
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_INSTALLER_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UpdateCheckData:
    update_available: bool
    current_version: str
    latest_version: str
    page_url: str
    setup_url: str = ""
    setup_sha256: str = ""
    setup_size: int = 0
    released: str = ""
    notes: tuple[str, ...] = ()
    channel: str = "stable"
    minimum_supported_version: str = "1.0.0"
    requires_manual_update: bool = False
    setup_managed_install: bool = False

    @property
    def install_supported(self) -> bool:
        return bool(
            self.update_available
            and self.setup_managed_install
            and not self.requires_manual_update
            and self.setup_url
            and self.setup_sha256
            and self.setup_size > 0
        )


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    setup_path: Path
    latest_version: str
    setup_sha256: str = ""
    setup_size: int = 0
    requires_elevation: bool = False


def parse_semver(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.fullmatch(str(value or "").strip().lstrip("vV"))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer_version(latest: str, current: str) -> bool:
    latest_value, current_value = parse_semver(latest), parse_semver(current)
    return bool(latest_value and current_value and latest_value > current_value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _allowed_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in _ALLOWED_HOSTS
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _registry_install_mode() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        install = app_dir().resolve()
        for hive, mode in ((winreg.HKEY_LOCAL_MACHINE, "/ALLUSERS"), (winreg.HKEY_CURRENT_USER, "/CURRENTUSER")):
            for flags in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                for name in (f"{INNO_SETUP_APP_ID}_is1", INNO_SETUP_APP_ID):
                    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{name}"
                    try:
                        with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | flags) as key:
                            for field in ("InstallLocation", "Inno Setup: App Path", "DisplayIcon", "UninstallString"):
                                try:
                                    value = str(winreg.QueryValueEx(key, field)[0] or "").strip()
                                except OSError:
                                    continue
                                if not value:
                                    continue
                                if field in {"DisplayIcon", "UninstallString"}:
                                    match = re.match(r'^"([^"]+)"|^(.+?\.exe)(?:\s|,|$)', value, re.IGNORECASE)
                                    if match is None:
                                        continue
                                    candidate = Path(os.path.expandvars(match.group(1) or match.group(2))).parent
                                else:
                                    candidate = Path(os.path.expandvars(value.strip('"')))
                                if candidate.is_absolute() and candidate.resolve() == install:
                                    return mode
                    except (OSError, ValueError, RuntimeError):
                        continue
    except (ImportError, OSError):
        return None
    return None


def _setup_managed_install() -> bool:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    if _registry_install_mode() is not None:
        return True
    try:
        for executable in app_dir().glob("unins*.exe"):
            data = executable.with_suffix(".dat")
            if all(path.is_file() and not is_link_like(path) for path in (executable, data)):
                return True
    except OSError:
        pass
    return False


def _installer_mode() -> str:
    mode = _registry_install_mode()
    if mode is not None:
        return mode
    install = os.path.normcase(os.path.abspath(str(app_dir())))
    for environment_name in ("ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(environment_name)
        if root:
            normalized = os.path.normcase(os.path.abspath(root))
            if install == normalized or install.startswith(normalized + os.sep):
                return "/ALLUSERS"
    return "/CURRENTUSER"


def _with_network_retries[T](operation: Callable[[], T], stop_event: Event | None) -> T:
    stopped = stop_event or Event()
    for attempt in range(3):
        if stopped.is_set():
            raise InterruptedError("Update operation canceled.")
        try:
            return operation()
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
            if isinstance(exc, HTTPError) and exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
            if attempt == 2:
                raise
            if stopped.wait(0.5 * (attempt + 1)):
                raise InterruptedError("Update operation canceled.") from exc
    raise RuntimeError("Update request failed.")


def _request_bytes(url: str, *, stop_event: Event | None = None, limit: int | None = None) -> bytes:
    if not _allowed_url(url):
        raise RuntimeError("Update URL is not on a trusted HTTPS host.")
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept": "application/json, */*"})

    def request_once() -> bytes:
        with urlopen(request, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
            if not _allowed_url(str(response.geturl())):
                raise RuntimeError("Update request redirected to an untrusted host.")
            data = response.read((limit + 1) if limit else -1)
        if stop_event and stop_event.is_set():
            raise InterruptedError("Update operation canceled.")
        if limit and len(data) > limit:
            raise RuntimeError("Update response exceeded the allowed size.")
        return data

    return _with_network_retries(request_once, stop_event)


def check_for_updates(current_version: str = APP_VERSION, *, stop_event: Event | None = None) -> UpdateCheckData:
    payload = json.loads(_request_bytes(UPDATE_MANIFEST_URL, stop_event=stop_event, limit=_MAX_MANIFEST_BYTES).decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError("Update manifest root is not an object.")
    channel = str(os.environ.get("JUSTAGWAS_UPDATE_CHANNEL", payload.get("channel", "stable"))).strip().casefold()
    if channel not in {"stable", "nightly"}:
        channel = "stable"
    channels = payload.get("channels")
    selected = payload
    if isinstance(channels, dict):
        normalized = {str(key).strip().casefold(): value for key, value in channels.items()}
        if not isinstance(normalized.get(channel), dict):
            channel = "stable"
        if not isinstance(normalized.get(channel), dict):
            raise RuntimeError("Update manifest does not contain the stable update channel.")
        selected = normalized[channel]
    latest = str(selected.get("version") or selected.get("latest") or selected.get("app_version") or "").strip().lstrip("vV")
    if parse_semver(latest) is None:
        raise RuntimeError("Update manifest does not contain a semantic version.")
    minimum = str(selected.get("minimum_supported_version", "1.0.0")).strip().lstrip("vV")
    if parse_semver(minimum) is None:
        minimum = "1.0.0"
    setup_url = str(selected.get("url-update") or selected.get("url_update") or selected.get("setup_url") or UPDATE_SETUP_FALLBACK_URL).strip()
    setup_hash = str(selected.get("setup-sha256") or selected.get("setup_sha256") or "").strip().lower()
    try:
        setup_size = int(selected.get("setup-size") or selected.get("setup_size") or 0)
    except (TypeError, ValueError):
        setup_size = 0
    if setup_url and not _allowed_url(setup_url):
        setup_url = ""
    page_url = str(selected.get("url") or OFFICIAL_PAGE_URL).strip()
    if not _allowed_url(page_url):
        page_url = OFFICIAL_PAGE_URL
    if not _SHA256_RE.fullmatch(setup_hash):
        setup_hash = ""
    current_tuple = parse_semver(current_version) or (0, 0, 0)
    minimum_tuple = parse_semver(minimum) or (1, 0, 0)
    notes_raw = selected.get("notes", ())
    notes = (
        tuple(
            str(item).strip()[:2000]
            for item in notes_raw[:100]
            if str(item).strip()
        )
        if isinstance(notes_raw, (list, tuple))
        else ()
    )
    return UpdateCheckData(
        update_available=is_newer_version(latest, current_version),
        current_version=current_version,
        latest_version=latest,
        page_url=page_url,
        setup_url=setup_url,
        setup_sha256=setup_hash,
        setup_size=setup_size if 0 < setup_size <= _MAX_INSTALLER_BYTES else 0,
        released=str(selected.get("released", "")),
        notes=notes,
        channel=channel,
        minimum_supported_version=minimum,
        requires_manual_update=current_tuple < minimum_tuple,
        setup_managed_install=_setup_managed_install(),
    )


def prepare_update(
    check: UpdateCheckData,
    *,
    stop_event: Event | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> PreparedUpdate:
    if not check.install_supported:
        raise RuntimeError("Automatic installation is not available for this build; use the download page.")
    if not _allowed_url(check.setup_url):
        raise RuntimeError("Update URL is not on a trusted HTTPS host.")
    if (
        not _SHA256_RE.fullmatch(check.setup_sha256)
        or not 0 < check.setup_size <= _MAX_INSTALLER_BYTES
    ):
        raise RuntimeError("Update verification metadata is invalid.")
    staging_root = updates_dir()
    staging_root.mkdir(parents=True, exist_ok=True)
    if is_link_like(staging_root):
        raise RuntimeError("Update staging cannot use a link or junction.")
    root = staging_root / f"trackindex-update-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    target = root / "TrackIndexSetup.exe"
    temporary = target.with_suffix(".exe.tmp")
    request = Request(check.setup_url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept": "*/*"})
    def download_once() -> None:
        downloaded = 0
        digest = hashlib.sha256()
        with urlopen(request, timeout=max(20.0, UPDATE_CHECK_TIMEOUT_SECONDS * 2)) as response, temporary.open("wb") as handle:
            if not _allowed_url(str(response.geturl())):
                raise RuntimeError("Update download redirected to an untrusted host.")
            while True:
                if stop_event and stop_event.is_set():
                    raise InterruptedError("Update download canceled.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > check.setup_size:
                    raise RuntimeError("Downloaded installer exceeded its declared size.")
                handle.write(chunk)
                digest.update(chunk)
                if progress_callback:
                    progress_callback(min(94.0, downloaded / check.setup_size * 94.0), f"Downloading update... {downloaded / check.setup_size:.0%}")
            handle.flush()
            os.fsync(handle.fileno())
        if downloaded != check.setup_size:
            raise RuntimeError(f"Installer size mismatch: expected {check.setup_size}, received {downloaded}.")
        if digest.hexdigest().casefold() != check.setup_sha256.casefold():
            raise RuntimeError("Installer SHA-256 verification failed.")

    try:
        _with_network_retries(download_once, stop_event)
        os.replace(temporary, target)
        if progress_callback:
            progress_callback(100.0, "Update is ready to install.")
        return PreparedUpdate(
            target,
            check.latest_version,
            check.setup_sha256,
            check.setup_size,
            _installer_mode() == "/ALLUSERS",
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        with suppress(OSError):
            root.rmdir()
        raise


def launch_prepared_update(prepared: PreparedUpdate, *, restart_after_update: bool = True) -> None:
    if (
        not _SHA256_RE.fullmatch(prepared.setup_sha256)
        or not 0 < prepared.setup_size <= _MAX_INSTALLER_BYTES
    ):
        raise RuntimeError("Prepared update verification metadata is invalid.")
    raw_staging_root = updates_dir()
    raw_update_root = prepared.setup_path.parent
    raw_setup_path = prepared.setup_path
    if (
        is_link_like(raw_staging_root)
        or is_link_like(raw_update_root)
        or is_link_like(raw_setup_path)
    ):
        raise RuntimeError("Prepared update is outside protected staging storage.")
    try:
        staging_root = raw_staging_root.resolve(strict=True)
        update_root = raw_update_root.resolve(strict=True)
        setup_path = raw_setup_path.resolve(strict=True)
        size = setup_path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Prepared update is unavailable: {exc}") from exc
    if (
        update_root.parent != staging_root
        or not update_root.name.startswith("trackindex-update-")
        or setup_path.parent != update_root
        or setup_path.name != "TrackIndexSetup.exe"
        or not setup_path.is_file()
    ):
        raise RuntimeError("Prepared update is outside protected staging storage.")
    try:
        digest = _sha256_file(setup_path)
    except OSError as exc:
        raise RuntimeError(f"Prepared update is unavailable: {exc}") from exc
    if size != prepared.setup_size or digest.casefold() != prepared.setup_sha256.casefold():
        raise RuntimeError("Prepared update changed after verification.")
    mode = _installer_mode()
    args = [
        "/VERYSILENT",
        "/SP-",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/FORCECLOSEAPPLICATIONS",
        mode,
        f"/DIR={app_dir()}",
    ]
    if not restart_after_update:
        args.append("/SKIPLAUNCH")
    if mode == "/ALLUSERS":
        import ctypes

        result = int(
            ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                str(setup_path),
                subprocess.list2cmdline(args),
                str(update_root),
                1,
            )
        )
        if result <= 32:
            raise RuntimeError("Administrator permission was denied or the update installer could not be started.")
        return
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(getattr(subprocess, "DETACHED_PROCESS", 0))
    subprocess.Popen([str(setup_path), *args], close_fds=True, creationflags=flags)


def discard_prepared_update(prepared: PreparedUpdate) -> None:
    raw_staging_root = updates_dir()
    raw_update_root = prepared.setup_path.parent
    if (
        prepared.setup_path.name != "TrackIndexSetup.exe"
        or is_link_like(raw_staging_root)
        or is_link_like(raw_update_root)
        or is_link_like(prepared.setup_path)
    ):
        return
    try:
        staging_root = raw_staging_root.resolve(strict=True)
        update_root = raw_update_root.resolve(strict=True)
    except OSError:
        return
    if (
        update_root.parent != staging_root
        or not update_root.name.startswith("trackindex-update-")
        or is_link_like(update_root)
    ):
        return
    try:
        for child in update_root.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
        update_root.rmdir()
    except OSError:
        return


def cleanup_old_update_staging(max_age_days: int = 7) -> None:
    root = updates_dir()
    if not root.exists() or is_link_like(root):
        return
    threshold = time.time() - max_age_days * 86400
    for child in root.glob("trackindex-update-*"):
        try:
            if is_link_like(child) or not child.is_dir():
                continue
            if child.stat().st_mtime >= threshold:
                continue
            for file_path in child.iterdir():
                if file_path.is_file() or file_path.is_symlink():
                    file_path.unlink(missing_ok=True)
            child.rmdir()
        except OSError:
            continue
