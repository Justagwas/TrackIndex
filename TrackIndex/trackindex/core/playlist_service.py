from __future__ import annotations

import hashlib
import locale
import re
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from .config import SUPPORTED_AUDIO_EXTENSIONS

_MAX_PLAYLIST_BYTES = 16 * 1024 * 1024
_MAX_PLAYLIST_LINES = 100000


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    prelude: tuple[str, ...]
    raw_path: str
    resolved_name: str | None


@dataclass(frozen=True, slots=True)
class PlaylistDocument:
    path: Path
    before_bytes: bytes
    bom: bool
    newline: str
    header: tuple[str, ...]
    entries: tuple[PlaylistEntry, ...]
    trailing: tuple[str, ...]
    errors: tuple[str, ...]
    fully_inspected: bool = True
    encoding: str = "utf-8"

    @property
    def safe_to_rewrite(self) -> bool:
        return not self.errors

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.before_bytes).hexdigest()

    @property
    def track_names(self) -> tuple[str, ...]:
        return tuple(entry.resolved_name for entry in self.entries if entry.resolved_name is not None)


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _detect_newline(raw: bytes) -> str:
    if b"\r\n" in raw:
        return "\r\n"
    return "\r" if b"\r" in raw else "\n"


def _direct_entry_name(value: str, folder: Path) -> str | None:
    windows_path = PureWindowsPath(value.replace("/", "\\"))
    if _DRIVE_RE.match(value):
        folder_path = PureWindowsPath(str(folder.resolve(strict=False)))
        if str(windows_path.parent).casefold() != str(folder_path).casefold():
            return None
        return windows_path.name
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith(("\\", "/")):
        return None
    parts = windows_path.parts
    if len(parts) != 1 or any(part in {"..", "."} for part in parts):
        return None
    return windows_path.name


def parse_playlist(path: Path, folder: Path, available_names: dict[str, str]) -> PlaylistDocument:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_PLAYLIST_BYTES + 1)
    except OSError as exc:
        return PlaylistDocument(
            path, b"", False, "\r\n", ("#EXTM3U",), (), (),
            (f"Could not read {path.name}: {exc}",), False,
        )
    if len(raw) > _MAX_PLAYLIST_BYTES:
        return PlaylistDocument(
            path,
            b"",
            False,
            "\r\n",
            ("#EXTM3U",),
            (),
            (),
            (f"{path.name} is too large for safe in-place playlist management.",),
            False,
        )

    bom = raw.startswith(b"\xef\xbb\xbf")
    newline = _detect_newline(raw)
    errors: list[str] = []
    encoding = "utf-8"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if path.suffix.casefold() == ".m3u":
            encoding = locale.getpreferredencoding(False) or "cp1252"
            try:
                text = raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                encoding = "cp1252"
                text = raw.decode(encoding)
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            errors.append(f"{path.name} is not valid UTF-8.")

    lines = text.splitlines()
    if len(lines) > _MAX_PLAYLIST_LINES:
        return PlaylistDocument(
            path,
            raw,
            bom,
            newline,
            ("#EXTM3U",),
            (),
            (),
            (f"{path.name} contains too many lines for safe playlist management.",),
            False,
            encoding,
        )
    header: list[str] = []
    pending: list[str] = []
    entries: list[PlaylistEntry] = []
    seen: set[str] = set()

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not entries and stripped.upper() == "#EXTM3U":
                header.append(line)
            else:
                pending.append(line)
            continue

        raw_path = stripped.strip('"')
        resolved_name: str | None = None
        entry_name = _direct_entry_name(raw_path, folder)
        if entry_name is None:
            errors.append(f"Line {line_number} contains an external or nested entry: {raw_path}")
        elif Path(entry_name).suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
            errors.append(f"Line {line_number} is not a supported audio entry: {raw_path}")
        else:
            canonical = available_names.get(entry_name.casefold())
            if canonical is None:
                errors.append(f"Line {line_number} references a missing track: {raw_path}")
            elif canonical.casefold() in seen:
                errors.append(f"Line {line_number} duplicates track: {canonical}")
            else:
                resolved_name = canonical
                seen.add(canonical.casefold())
        entries.append(PlaylistEntry(tuple(pending), raw_path, resolved_name))
        pending.clear()

    if not header:
        header.append("#EXTM3U")
    return PlaylistDocument(
        path=path,
        before_bytes=raw,
        bom=bom,
        newline=newline,
        header=tuple(header),
        entries=tuple(entries),
        trailing=tuple(pending),
        errors=tuple(errors),
        encoding=encoding,
    )


def new_playlist_document(path: Path) -> PlaylistDocument:
    return PlaylistDocument(path, b"", False, "\r\n", ("#EXTM3U",), (), (), ())


def migrated_playlist_document(document: PlaylistDocument, path: Path) -> PlaylistDocument:
    return replace(
        document,
        path=path,
        before_bytes=b"",
        bom=False,
        newline="\r\n",
        encoding="utf-8",
    )


def render_playlist(
    document: PlaylistDocument,
    ordered_original_names: tuple[str, ...],
    final_names: dict[str, str],
) -> bytes:
    if not document.safe_to_rewrite:
        raise ValueError("Unsafe playlist cannot be rendered")

    existing = {
        entry.resolved_name.casefold(): entry
        for entry in document.entries
        if entry.resolved_name is not None
    }
    lines = list(document.header)
    for original_name in ordered_original_names:
        entry = existing.get(original_name.casefold())
        if entry is not None:
            lines.extend(entry.prelude)
        lines.append(final_names.get(original_name.casefold(), original_name))
    lines.extend(document.trailing)
    text = document.newline.join(lines) + document.newline
    try:
        encoded = text.encode(document.encoding)
    except (LookupError, UnicodeEncodeError) as exc:
        raise ValueError(
            f"{document.path.name} cannot represent the updated track names in its original encoding."
        ) from exc
    return (b"\xef\xbb\xbf" + encoded) if document.bom else encoded


def playlist_references_any(document: PlaylistDocument, names: set[str]) -> bool:
    folded = {name.casefold() for name in names}
    return any(name.casefold() in folded for name in document.track_names)
