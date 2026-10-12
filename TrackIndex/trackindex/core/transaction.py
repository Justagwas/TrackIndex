from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_json, write_bytes_durable
from .config import (
    SESSION_RECOVERY_FOLDER,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_PLAYLIST_EXTENSIONS,
)
from .filesystem import is_link_like
from .integrity import inspect_content, verify_content
from .models import (
    ApplyResult,
    ChangePlan,
    CommitReceipt,
    CommittedPlaylistState,
    CommittedTrackState,
)
from .paths import _is_direct_child_of_resolved_folder, is_direct_child, recovery_dir
from .presence import (
    ensure_recovery_parent,
    is_managed_entry,
    validate_recovery_ancestors,
)
from .windows_names import is_valid_windows_name


class StalePlanError(RuntimeError):
    pass


_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_RENAME_TEMP_RE = re.compile(
    r"^\.trackindex-[0-9a-f]{32}\.tmp(?P<suffix>\.[^.]+)$",
    re.IGNORECASE,
)
_IMPORT_TEMP_RE = re.compile(
    r"^\.trackindex-[0-9a-f]{32}\.import(?P<suffix>\.[^.]+)$",
    re.IGNORECASE,
)
_PLAYLIST_TEMP_RE = re.compile(
    r"^\.trackindex-[0-9a-f]{32}\.m3u8\.(?:tmp|backup)$",
    re.IGNORECASE,
)
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_plan(plan: ChangePlan) -> None:
    if plan.blockers:
        raise StalePlanError("The change plan contains blocking issues.")
    if _PLAN_ID_RE.fullmatch(plan.plan_id) is None:
        raise StalePlanError("The change plan identifier is unsafe.")
    folder = _validated_plan_folder(plan.folder)
    try:
        initial_stat = folder.stat()
    except OSError as exc:
        raise StalePlanError("The selected folder is no longer available.") from exc
    source_names, target_names, track_ids = _validate_renames(plan, folder)
    _validate_imports(plan, folder, source_names, target_names, track_ids)
    _validate_presence(plan, folder, source_names, target_names, track_ids)
    _validate_playlist_operations(plan, folder)
    _validate_inventory_and_snapshots(plan, folder)
    _validate_occupied_rename_targets(plan, source_names)
    _validate_playlist_state(plan, folder)
    current_folder = _validated_plan_folder(plan.folder)
    try:
        current_stat = current_folder.stat()
    except OSError as exc:
        raise StalePlanError("The selected folder is no longer available.") from exc
    if (
        current_folder != folder
        or (current_stat.st_dev, current_stat.st_ino) != (initial_stat.st_dev, initial_stat.st_ino)
    ):
        raise StalePlanError("The selected folder changed during validation. Rescan before applying.")


def _validated_plan_folder(path: Path) -> Path:
    if is_link_like(path):
        raise StalePlanError("The selected folder is no longer available.")
    try:
        folder = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StalePlanError("The selected folder is no longer available.") from exc
    if not folder.is_dir():
        raise StalePlanError("The selected folder is no longer available.")
    return folder


def _validation_child(path: Path, folder: Path, *, missing: bool = False) -> bool:
    if path.is_absolute() and path.parent == folder and path.name not in {".", ".."}:
        try:
            value = path.lstat()
        except FileNotFoundError:
            return missing
        except OSError:
            return False
        return stat.S_ISREG(value.st_mode) and not (getattr(value, "st_file_attributes", 0) & 0x400)
    if not _is_direct_child_of_resolved_folder(path, folder) or is_link_like(path):
        return False
    return path.is_file() or (missing and not path.exists())


def _validate_renames(
    plan: ChangePlan,
    folder: Path,
) -> tuple[set[str], set[str], set[str]]:
    source_names: set[str] = set()
    target_names: set[str] = set()
    track_ids: set[str] = set()
    for rename in plan.renames:
        source_key = rename.source.name.casefold()
        target_key = rename.target.name.casefold()
        if (
            not rename.track_id
            or rename.track_id in track_ids
            or source_key in source_names
            or target_key in target_names
        ):
            raise StalePlanError("The change plan contains duplicate rename operations.")
        if (
            not _validation_child(rename.source, folder)
            or not _validation_child(rename.target, folder, missing=True)
        ):
            raise StalePlanError("A rename operation points outside the selected folder.")
        if rename.source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise StalePlanError("A rename source is not a supported audio file.")
        if rename.target.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise StalePlanError("A rename target is not a supported audio file.")
        if not is_valid_windows_name(rename.target.name):
            raise StalePlanError("A rename target is not a valid Windows filename.")
        track_ids.add(rename.track_id)
        source_names.add(source_key)
        target_names.add(target_key)
    return source_names, target_names, track_ids


def _validate_imports(
    plan: ChangePlan,
    folder: Path,
    source_names: set[str],
    target_names: set[str],
    track_ids: set[str],
) -> None:
    import_sources: set[str] = set()
    for operation in plan.imports:
        try:
            source_key = str(operation.source.resolve(strict=False)).casefold()
        except (OSError, RuntimeError) as exc:
            raise StalePlanError("An imported file path is unavailable.") from exc
        target_key = operation.target.name.casefold()
        if (
            not operation.track_id
            or operation.track_id in track_ids
            or source_key in import_sources
            or target_key in target_names
        ):
            raise StalePlanError("The change plan contains duplicate import operations.")
        if (
            is_link_like(operation.source)
            or not operation.source.is_file()
            or operation.source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
        ):
            raise StalePlanError(f"An imported file is no longer available: {operation.source.name}")
        if not _validation_child(operation.target, folder, missing=True):
            raise StalePlanError("An import target points outside the selected folder.")
        if (
            operation.target.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
            or not is_valid_windows_name(operation.target.name)
        ):
            raise StalePlanError("An import target is not a safe audio filename.")
        try:
            stat = operation.source.stat()
        except OSError as exc:
            raise StalePlanError(
                f"An imported file is no longer available: {operation.source.name}"
            ) from exc
        if stat.st_size != operation.size or stat.st_mtime_ns != operation.mtime_ns:
            raise StalePlanError(f"An imported file changed before it could be copied: {operation.source.name}")
        if operation.sha256 and _hash_file(operation.source) != operation.sha256:
            raise StalePlanError(f"An imported file changed before it could be copied: {operation.source.name}")
        if operation.target.exists() and target_key not in source_names:
            raise StalePlanError(f"An import target is now occupied: {operation.target.name}")
        track_ids.add(operation.track_id)
        import_sources.add(source_key)
        target_names.add(target_key)


def _validate_presence(
    plan: ChangePlan,
    folder: Path,
    source_names: set[str],
    target_names: set[str],
    track_ids: set[str],
) -> None:
    presence_sources: set[str] = set()
    presence_targets: set[str] = set()
    for presence_operation in plan.presence:
        source_direct = _validation_child(presence_operation.source, folder)
        target_direct = _validation_child(presence_operation.target, folder, missing=True)
        if (
            not presence_operation.track_id
            or presence_operation.track_id in track_ids
            or source_direct == target_direct
            or presence_operation.action not in {"stash", "restore"}
            or (presence_operation.action == "stash" and not source_direct)
            or (presence_operation.action == "restore" and not target_direct)
        ):
            raise StalePlanError("The change plan contains an unsafe presence operation.")
        recovery = (
            presence_operation.target
            if presence_operation.action == "stash"
            else presence_operation.source
        )
        if not validate_recovery_ancestors(recovery, folder):
            raise StalePlanError("A recovery operation points outside safe session storage.")
        if (
            presence_operation.source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
            or presence_operation.target.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
        ):
            raise StalePlanError("A recovery operation does not reference an audio file.")
        if is_link_like(presence_operation.source) or not presence_operation.source.is_file():
            raise StalePlanError("Recovery source data is unavailable.")
        try:
            stat = presence_operation.source.stat()
        except OSError as exc:
            raise StalePlanError("Recovery source data is unavailable.") from exc
        if stat.st_size != presence_operation.size or stat.st_mtime_ns != presence_operation.mtime_ns:
            raise StalePlanError("A track changed before its presence could be updated.")
        if presence_operation.action == "restore" and not presence_operation.sha256:
            raise StalePlanError("Original content evidence is unavailable for this retained track; recovery data was preserved.")
        if presence_operation.sha256 and _hash_file(presence_operation.source) != presence_operation.sha256:
            raise StalePlanError("Recovery source data does not match the session command.")
        try:
            source_key = str(presence_operation.source.resolve(strict=False)).casefold()
            target_key = str(presence_operation.target.resolve(strict=False)).casefold()
        except (OSError, RuntimeError) as exc:
            raise StalePlanError("A recovery path is unavailable.") from exc
        if source_key in presence_sources or target_key in presence_targets:
            raise StalePlanError("The change plan contains duplicate recovery paths.")
        direct_source_key = presence_operation.source.name.casefold() if source_direct else ""
        direct_target_key = presence_operation.target.name.casefold() if target_direct else ""
        if direct_source_key and direct_source_key in source_names:
            raise StalePlanError("The change plan contains duplicate track sources.")
        if direct_target_key and direct_target_key in target_names:
            raise StalePlanError("The change plan contains duplicate track targets.")
        if presence_operation.target.exists() and not (
            target_direct and presence_operation.target.name.casefold() in source_names
        ):
            raise StalePlanError("A recovery destination is already occupied.")
        if source_direct:
            source_names.add(direct_source_key)
        if target_direct:
            target_names.add(direct_target_key)
        presence_sources.add(source_key)
        presence_targets.add(target_key)
        track_ids.add(presence_operation.track_id)


def _validate_playlist_operations(plan: ChangePlan, folder: Path) -> None:
    playlist_paths: set[str] = set()
    for playlist_op in plan.playlist_operations:
        path_key = playlist_op.path.name.casefold()
        if path_key in playlist_paths:
            raise StalePlanError("The change plan contains duplicate playlist operations.")
        if (
            not _validation_child(playlist_op.path, folder, missing=True)
            or playlist_op.path.suffix.casefold()
            not in SUPPORTED_PLAYLIST_EXTENSIONS
        ):
            raise StalePlanError("A playlist operation points outside the selected folder.")
        if playlist_op.action not in {"create", "update", "remove"}:
            raise StalePlanError("The change plan contains an unsupported playlist operation.")
        playlist_paths.add(path_key)


def _validate_inventory_and_snapshots(plan: ChangePlan, folder: Path) -> None:
    try:
        current_inventory = tuple(
            sorted(
                (path.name for path in folder.iterdir() if not is_managed_entry(path)),
                key=str.casefold,
            )
        )
    except OSError as exc:
        raise StalePlanError(f"The selected folder could not be re-read: {exc}") from exc
    if current_inventory != plan.folder_inventory:
        raise StalePlanError("The selected folder contents changed after preview. Rescan before applying.")
    for snapshot in plan.snapshots:
        if not _validation_child(snapshot.path, folder):
            raise StalePlanError("A file snapshot points outside the selected folder.")
        if is_link_like(snapshot.path) or not snapshot.path.is_file():
            raise StalePlanError(f"A planned file is no longer a regular file: {snapshot.path.name}")
        try:
            stat = snapshot.path.stat()
        except OSError as exc:
            raise StalePlanError(f"A planned file is no longer available: {snapshot.path.name}") from exc
        if stat.st_size != snapshot.size or stat.st_mtime_ns != snapshot.mtime_ns:
            raise StalePlanError(f"A planned file changed after preview: {snapshot.path.name}")


def _validate_occupied_rename_targets(
    plan: ChangePlan,
    source_names: set[str],
) -> None:
    for rename in plan.renames:
        if rename.target.exists() and rename.target.name.casefold() not in source_names:
            raise StalePlanError(f"A rename target is now occupied: {rename.target.name}")


def _validate_playlist_state(plan: ChangePlan, folder: Path) -> None:
    for playlist_op in plan.playlist_operations:
        if not _validation_child(playlist_op.path, folder, missing=playlist_op.expected_sha256 is None):
            raise StalePlanError("A playlist operation points outside the selected folder.")
        if playlist_op.path.exists() and is_link_like(playlist_op.path):
            raise StalePlanError(f"The playlist target is no longer a regular file: {playlist_op.path.name}")
        if playlist_op.expected_sha256 is None:
            if playlist_op.path.exists():
                raise StalePlanError(f"The new playlist target now exists: {playlist_op.path.name}")
        else:
            try:
                matches = (
                    playlist_op.path.exists()
                    and _hash_file(playlist_op.path) == playlist_op.expected_sha256
                )
            except OSError as exc:
                raise StalePlanError(
                    f"The playlist is no longer available: {playlist_op.path.name}"
                ) from exc
            if not matches:
                raise StalePlanError(f"The playlist changed after preview: {playlist_op.path.name}")


def _journal_payload(
    plan: ChangePlan,
    temp_paths: dict[str, Path],
    import_state: list[dict[str, str]],
    presence_state: list[dict[str, str]],
    playlist_state: list[dict[str, str]],
    phase: str,
    rename_state: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "plan_id": plan.plan_id,
        "folder": str(plan.folder),
        "phase": phase,
        "renames": rename_state,
        "imports": import_state,
        "presence": presence_state,
        "playlists": playlist_state,
    }


def _journal_items(payload: dict[str, object]) -> tuple[Path, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], str]:
    schema = payload.get("schema_version", 1)
    if type(schema) is not int or schema not in {1, 2, 3}:
        raise ValueError("Recovery journal schema is unsupported.")
    phase = str(payload.get("phase", "prepared"))
    if phase not in {"prepared", "imports-prepared", "temporary-renames", "presence-stashed", "final-renames", "presence-restored", "imports-final", "playlist-written", "complete"}:
        raise ValueError("Recovery journal contains an unknown transaction phase.")
    raw_renames = payload.get("renames", [])
    raw_imports = payload.get("imports", [])
    raw_presence = payload.get("presence", [])
    raw_playlists = payload.get("playlists", [])
    if not isinstance(raw_renames, list) or not isinstance(raw_imports, list) or not isinstance(raw_presence, list) or not isinstance(raw_playlists, list):
        raise ValueError("Recovery journal operations are malformed.")
    renames = [item for item in raw_renames if isinstance(item, dict)]
    imports = [item for item in raw_imports if isinstance(item, dict)]
    presence = [item for item in raw_presence if isinstance(item, dict)]
    playlists = [item for item in raw_playlists if isinstance(item, dict)]
    if len(renames) != len(raw_renames) or len(imports) != len(raw_imports) or len(presence) != len(raw_presence) or len(playlists) != len(raw_playlists):
        raise ValueError("Recovery journal operations are malformed.")
    for item in (*renames, *presence):
        size, digest = item.get("size"), item.get("sha256", "")
        if (schema == 3 or size is not None or digest) and (
            type(size) is not int or size < 0
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise ValueError("Recovery journal original content evidence is malformed.")

    raw_folder = str(payload.get("folder", "")).strip()
    if raw_folder:
        folder = _validated_plan_folder(Path(raw_folder))
    elif renames:
        folder = Path(str(renames[0].get("source", ""))).absolute().parent
    elif imports:
        folder = Path(str(imports[0].get("target", ""))).absolute().parent
    elif presence:
        source = Path(str(presence[0].get("source", ""))).absolute()
        target = Path(str(presence[0].get("target", ""))).absolute()
        folder = source.parent if presence[0].get("action") == "stash" else target.parent
    elif playlists:
        folder = Path(str(playlists[0].get("target", ""))).absolute().parent
    else:
        raise ValueError("Recovery journal does not identify its folder.")
    folder = _validated_plan_folder(folder)

    for item in renames:
        for key in ("source", "temporary", "target"):
            path = Path(str(item.get(key, "")))
            if not str(item.get(key, "")).strip() or not is_direct_child(path, folder):
                raise ValueError("Recovery journal contains an out-of-folder rename path.")
        source = Path(str(item["source"]))
        temporary = Path(str(item["temporary"]))
        match = _RENAME_TEMP_RE.fullmatch(temporary.name)
        if (
            source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
            or match is None
            or match.group("suffix").casefold() != source.suffix.casefold()
        ):
            raise ValueError("Recovery journal contains a non-audio rename source.")
    for item in imports:
        target = Path(str(item.get("target", "")))
        temporary = Path(str(item.get("temporary", "")))
        if not is_direct_child(target, folder) or not is_direct_child(temporary, folder):
            raise ValueError("Recovery journal contains an out-of-folder import path.")
        match = _IMPORT_TEMP_RE.fullmatch(temporary.name)
        if (
            target.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
            or match is None
            or match.group("suffix").casefold() != target.suffix.casefold()
        ):
            raise ValueError("Recovery journal contains a non-audio import target.")
        digest = str(item.get("sha256", ""))
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.casefold())):
            raise ValueError("Recovery journal contains an invalid import hash.")
    for item in presence:
        source = Path(str(item.get("source", "")))
        target = Path(str(item.get("target", "")))
        action = str(item.get("action", ""))
        if (
            action not in {"stash", "restore"}
            or not (is_direct_child(source, folder) or validate_recovery_ancestors(source, folder))
            or not (is_direct_child(target, folder) or validate_recovery_ancestors(target, folder))
            or is_direct_child(source, folder) == is_direct_child(target, folder)
        ):
            raise ValueError("Recovery journal contains an unsafe presence path.")
    for item in playlists:
        target = Path(str(item.get("target", "")))
        if (
            not str(item.get("target", "")).strip()
            or not is_direct_child(target, folder)
            or target.suffix.casefold() not in SUPPORTED_PLAYLIST_EXTENSIONS
        ):
            raise ValueError("Recovery journal contains an out-of-folder playlist path.")
        for key in ("temporary", "backup"):
            raw_path = item.get(key)
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError(f"Recovery journal is missing a valid playlist {key} path.")
            internal_path = Path(raw_path)
            if (
                not is_direct_child(internal_path, folder)
                or _PLAYLIST_TEMP_RE.fullmatch(internal_path.name) is None
            ):
                raise ValueError("Recovery journal contains unsafe recovery data.")
        for key in ("before_sha256", "after_sha256"):
            value = str(item.get(key, ""))
            if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold())):
                raise ValueError("Recovery journal contains an invalid playlist hash.")
    return folder, renames, imports, presence, playlists, phase


def _verify_original(path: Path, item: dict[str, object]) -> None:
    verify_content(path, item.get("size"), item.get("sha256", ""))


def _rename_candidate(item: dict[str, object], phase: str) -> tuple[Path, bool]:
    source, temporary, target = (Path(str(item[key])) for key in ("source", "temporary", "target"))
    state = item.get("rollback_state", "")
    if state == "restored" or (state == "restoring" and not temporary.exists() and source.exists()):
        return source, True
    if temporary.exists():
        return temporary, False
    if state in {"staged", "restoring"}:
        raise OSError(f"Could not locate staged recovery data for {source.name}.")
    if phase not in {"prepared", "imports-prepared"} and target.exists():
        return target, False
    if source.exists():
        return source, True
    raise OSError(f"Could not locate original data for {source.name}.")


def _preflight_recovery(renames, imports, presence, playlists, phase: str) -> None:
    for item in renames:
        candidate, _restored = _rename_candidate(item, phase)
        _verify_original(candidate, item)
    for item in presence:
        source, target = Path(str(item["source"])), Path(str(item["target"]))
        candidate = (target if item.get("action") == "stash" and item.get("rollback_done") is not True
                     and target.exists() else source if source.exists() else target)
        _verify_original(candidate, item)
    for item in playlists:
        target, backup = Path(str(item["target"])), Path(str(item["backup"]))
        if backup.exists():
            verify_content(backup, None, item.get("before_sha256", ""))
            if target.exists():
                verify_content(target, None, item.get("after_sha256", ""))
        elif str(item.get("created", "false")).lower() != "true":
            verify_content(target, None, item.get("before_sha256", ""))
        elif target.exists():
            verify_content(target, None, item.get("after_sha256", ""))
        temporary = Path(str(item["temporary"]))
        if temporary.exists():
            verify_content(temporary, None, item.get("after_sha256", ""))
    for item in imports:
        if item.get("rollback_done") is True:
            continue
        temporary, target = Path(str(item["temporary"])), Path(str(item["target"]))
        if temporary.exists():
            verify_content(temporary, None, item.get("sha256", ""))
        elif phase in {"presence-restored", "imports-final", "playlist-written"} and target.exists():
            verify_content(target, None, item.get("sha256", ""))


def _verified_unlink(path: Path, digest: str) -> None:
    if path.exists() or is_link_like(path):
        verify_content(path, None, digest)
        path.unlink()


def _cleanup_completed_journal(
    journal_path: Path,
    imports: list[dict[str, object]],
    playlists: list[dict[str, object]],
) -> ApplyResult:
    details: list[str] = []
    for item in imports:
        raw_temporary = str(item.get("temporary", ""))
        if raw_temporary:
            try:
                _verified_unlink(Path(raw_temporary), str(item.get("sha256", "")))
            except OSError as exc:
                details.append(f"Could not remove completed import data: {exc}")
    for item in playlists:
        for key in ("temporary", "backup"):
            raw_cleanup_path = str(item.get(key, ""))
            if not raw_cleanup_path:
                continue
            cleanup_path = Path(raw_cleanup_path)
            try:
                _verified_unlink(cleanup_path, str(item.get("before_sha256" if key == "backup" else "after_sha256", "")))
            except OSError as exc:
                details.append(
                    "Could not remove completed transaction data "
                    f"{cleanup_path.name}: {exc}"
                )
    if not details:
        try:
            journal_path.unlink(missing_ok=True)
        except OSError as exc:
            details.append(f"Could not remove the completed recovery journal: {exc}")
    if details:
        return ApplyResult(
            False,
            "Committed changes are intact, but recovery cleanup was incomplete.",
            recovery_journal=journal_path,
            details=tuple(details),
        )
    return ApplyResult(
        True,
        "Committed changes were intact; leftover recovery data was cleaned up.",
    )


def _restore_playlists(
    playlists: list[dict[str, object]],
    details: list[str],
    save: Callable[[], None],
) -> None:
    for item in reversed(playlists):
        if item.get("rollback_done") is True:
            continue
        target = Path(str(item.get("target", "")))
        backup = Path(str(item.get("backup", "")))
        created = str(item.get("created", "false")).lower() == "true"
        raw_temporary = str(item.get("temporary", "")).strip()
        temporary = Path(raw_temporary) if raw_temporary else None
        try:
            if backup.exists():
                verify_content(backup, None, str(item.get("before_sha256", "")))
                if target.exists():
                    after_hash = str(item.get("after_sha256", ""))
                    verify_content(target, None, after_hash)
                    target.unlink()
                verify_content(backup, None, str(item.get("before_sha256", "")))
                backup.rename(target)
            elif created and target.exists():
                after_hash = str(item.get("after_sha256", ""))
                verify_content(target, None, after_hash)
                target.unlink()
            if temporary is not None:
                _verified_unlink(temporary, str(item.get("after_sha256", "")))
            item["rollback_done"] = True
        except OSError as exc:
            details.append(f"Could not restore playlist {target.name}: {exc}")
    if playlists:
        save()


def _remove_imports(
    imports: list[dict[str, object]],
    phase: str,
    details: list[str],
    save: Callable[[], None],
) -> None:
    for item in reversed(imports):
        if item.get("rollback_done") is True:
            continue
        target = Path(str(item.get("target", "")))
        temporary = Path(str(item.get("temporary", "")))
        digest = str(item.get("sha256", ""))
        installed = (
            phase in {"presence-restored", "imports-final", "playlist-written"}
            and not temporary.exists()
        )
        candidates = (target, temporary) if installed else (temporary,)
        complete = True
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                verify_content(candidate, None, digest)
                candidate.unlink()
            except OSError as exc:
                details.append(
                    f"Could not remove imported copy {candidate.name}: {exc}"
                )
                complete = False
        if complete:
            item["rollback_done"] = True
    if imports:
        save()


def _return_restored_presence(
    folder: Path,
    presence: list[dict[str, object]],
    details: list[str],
    save: Callable[[], None],
) -> None:
    for item in reversed(presence):
        if str(item.get("action", "")) != "restore":
            continue
        if item.get("rollback_done") is True:
            continue
        source = Path(str(item.get("source", "")))
        target = Path(str(item.get("target", "")))
        try:
            _validate_presence_move(target, source, folder)
            if target.exists() and not source.exists():
                ensure_recovery_parent(source, folder)
                _validate_presence_move(target, source, folder)
                _verify_original(target, item)
                target.rename(source)
            if not source.exists():
                details.append(f"Recovery data is unavailable for {target.name}.")
                continue
            _verify_original(source, item)
            item["rollback_done"] = True
        except OSError as exc:
            details.append(
                f"Could not return restored track to recovery storage: {exc}"
            )
    if presence:
        save()


def _restore_renames(
    renames: list[dict[str, object]],
    phase: str,
    details: list[str],
    save: Callable[[], None],
) -> None:
    staged: list[tuple[dict[str, object], Path, Path]] = []
    staging_failed = False
    for item in renames:
        source = Path(str(item.get("source", "")))
        temporary = Path(str(item.get("temporary", "")))
        try:
            current, restored = _rename_candidate(item, phase)
            _verify_original(current, item)
            if restored:
                item["rollback_state"] = "restored"
                continue
            if current != temporary:
                current.rename(temporary)
            item["rollback_state"] = "staged"
            staged.append((item, temporary, source))
        except OSError as exc:
            staging_failed = True
            details.append(f"Could not stage {source.name} for recovery: {exc}")
    if staging_failed:
        save()
        return
    for item, _temporary, _source in staged:
        item["rollback_state"] = "restoring"
    if renames:
        save()
    for item, recovery_temp, source in staged:
        try:
            if source.exists():
                details.append(
                    f"Could not restore {source.name} because the original path is occupied."
                )
                continue
            _verify_original(recovery_temp, item)
            recovery_temp.rename(source)
            item["rollback_state"] = "restored"
        except OSError as exc:
            details.append(f"Could not restore {source.name}: {exc}")
    if renames:
        save()


def _validate_presence_move(source: Path, target: Path, folder: Path) -> None:
    try:
        _validated_plan_folder(folder)
    except StalePlanError as exc:
        raise OSError(str(exc)) from exc
    source_direct = is_direct_child(source, folder)
    target_direct = is_direct_child(target, folder)
    if source_direct == target_direct:
        raise OSError("Presence recovery paths are unsafe.")
    recovery = target if source_direct else source
    direct = source if source_direct else target
    if (
        not validate_recovery_ancestors(recovery, folder)
        or is_link_like(direct)
        or (direct.exists() and not direct.is_file())
    ):
        raise OSError("Presence recovery storage became unsafe.")


def _restore_stashed_presence(
    folder: Path,
    presence: list[dict[str, object]],
    details: list[str],
    save: Callable[[], None],
) -> None:
    for item in reversed(presence):
        if str(item.get("action", "")) != "stash":
            continue
        if item.get("rollback_done") is True:
            continue
        source = Path(str(item.get("source", "")))
        target = Path(str(item.get("target", "")))
        try:
            _validate_presence_move(target, source, folder)
            if target.exists() and source.exists():
                details.append(f"Could not restore removed track {source.name} because its path is occupied.")
                continue
            if target.exists() and not source.exists():
                _verify_original(target, item)
                target.rename(source)
            if not source.exists():
                details.append(f"Could not locate removed track {source.name}.")
                continue
            _verify_original(source, item)
            item["rollback_done"] = True
        except OSError as exc:
            details.append(f"Could not restore removed track {source.name}: {exc}")
    if presence:
        save()


def recover_journal(journal_path: Path) -> ApplyResult:
    try:
        with journal_path.open("rb") as handle:
            raw = handle.read(_MAX_JOURNAL_BYTES + 1)
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise ValueError("recovery journal is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Recovery journal root is not an object.")
        _folder, renames, imports, presence, playlists, phase = _journal_items(payload)
    except (OSError, RuntimeError, ValueError, RecursionError) as exc:
        return ApplyResult(False, f"Could not read recovery journal: {exc}", recovery_journal=journal_path)
    if phase == "complete":
        return _cleanup_completed_journal(journal_path, imports, playlists)
    details: list[str] = []

    def save() -> None:
        atomic_write_json(journal_path, payload)

    try:
        _preflight_recovery(renames, imports, presence, playlists, phase)
        _restore_playlists(playlists, details, save)
        _remove_imports(imports, phase, details, save)
        _return_restored_presence(_folder, presence, details, save)
        _restore_renames(renames, phase, details, save)
        _restore_stashed_presence(_folder, presence, details, save)
    except (OSError, ValueError) as exc:
        details.append(f"Could not record recovery progress: {exc}")
    if details:
        return ApplyResult(False, "Recovery was incomplete.", recovery_journal=journal_path, details=tuple(details))
    try:
        journal_path.unlink(missing_ok=True)
    except OSError as exc:
        return ApplyResult(False, "Original files were restored, but the recovery journal could not be removed.",
                           recovery_journal=journal_path, details=(str(exc),))
    return ApplyResult(True, "The interrupted operation was rolled back safely.")


def pending_journals() -> tuple[Path, ...]:
    root = recovery_dir()
    if not root.exists():
        return ()
    return tuple(sorted(root.glob("*.json")))


def _commit_receipt(plan: ChangePlan, internal_paths: set[Path]) -> CommitReceipt:


    if len(plan.filename_preview) != len(plan.ordered_track_ids):
        raise StalePlanError("The change plan cannot identify every committed track.")

    tracks: list[CommittedTrackState] = []
    for track_id, (_before_name, final_name) in zip(
        plan.ordered_track_ids, plan.filename_preview, strict=True
    ):
        final_path = plan.folder / final_name
        if is_link_like(final_path) or not final_path.is_file():
            raise StalePlanError(f"A committed track is no longer available: {final_name}")
        stat = final_path.stat()
        tracks.append(
            CommittedTrackState(track_id, final_path, stat.st_size, stat.st_mtime_ns)
        )

    playlists: list[CommittedPlaylistState] = []
    for operation in plan.playlist_operations:
        if operation.after_bytes is None:
            if operation.path.exists():
                raise StalePlanError(
                    f"The playlist could not be removed safely: {operation.path.name}"
                )
            playlists.append(CommittedPlaylistState(operation.path, False))
            continue
        if is_link_like(operation.path) or not operation.path.is_file():
            raise StalePlanError(
                f"A committed playlist is no longer available: {operation.path.name}"
            )
        stat = operation.path.stat()
        digest = _hash_file(operation.path)
        expected = hashlib.sha256(operation.after_bytes).hexdigest()
        if digest != expected:
            raise StalePlanError(
                f"The committed playlist could not be verified: {operation.path.name}"
            )
        playlists.append(
            CommittedPlaylistState(
                operation.path,
                True,
                stat.st_size,
                stat.st_mtime_ns,
                digest,
            )
        )

    ignored = {str(path.resolve(strict=False)).casefold() for path in internal_paths}
    inventory: list[str] = []
    try:
        for path in plan.folder.iterdir():
            if str(path.resolve(strict=False)).casefold() not in ignored:
                inventory.append(path.name)
    except OSError as exc:
        raise StalePlanError(f"The committed folder could not be verified: {exc}") from exc
    return CommitReceipt(
        tuple(tracks),
        tuple(playlists),
        tuple(sorted(inventory, key=str.casefold)),
    )


def _copy_import_durable(operation, temporary: Path) -> str:
    before = operation.source.stat()
    if before.st_size != operation.size or before.st_mtime_ns != operation.mtime_ns:
        raise StalePlanError(f"An imported file changed before copying: {operation.source.name}")
    digest = hashlib.sha256()
    with operation.source.open("rb") as source, temporary.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    after = operation.source.stat()
    if after.st_size != operation.size or after.st_mtime_ns != operation.mtime_ns:
        raise StalePlanError(f"An imported file changed while copying: {operation.source.name}")
    if temporary.stat().st_size != operation.size:
        raise OSError(f"The imported copy is incomplete: {operation.source.name}")
    result = digest.hexdigest()
    if operation.sha256 and result != operation.sha256:
        raise StalePlanError(f"An imported file changed while copying: {operation.source.name}")
    return result


@dataclass(slots=True)
class _TransactionExecution:
    journal: Path
    temp_paths: dict[str, Path]
    import_temps: dict[str, Path]
    import_state: list[dict[str, str]]
    presence_state: list[dict[str, str]]
    playlist_state: list[dict[str, str]]
    playlist_temps: dict[Path, Path]
    rename_state: list[dict[str, object]]
    root_identity: tuple[int, int]
    canonical_root: Path


def _available_internal_path(folder: Path, pattern: str, suffix: str) -> Path:
    while True:
        candidate = folder / pattern.format(uuid.uuid4().hex, suffix)
        if not candidate.exists():
            return candidate


def _prepare_execution(plan: ChangePlan, journal: Path) -> _TransactionExecution:
    canonical_root = _validated_plan_folder(plan.folder)
    root_stat = canonical_root.stat()
    temp_paths = {
        operation.track_id: _available_internal_path(
            plan.folder,
            ".trackindex-{}.tmp{}",
            operation.source.suffix,
        )
        for operation in plan.renames
    }
    import_temps = {
        operation.track_id: _available_internal_path(
            plan.folder,
            ".trackindex-{}.import{}",
            operation.source.suffix,
        )
        for operation in plan.imports
    }
    import_state = [
        {
            "source": str(operation.source),
            "temporary": str(import_temps[operation.track_id]),
            "target": str(operation.target),
            "sha256": inspect_content(operation.source).sha256,
        }
        for operation in plan.imports
    ]
    presence_state = [
        {
            "source": str(operation.source),
            "target": str(operation.target),
            "action": operation.action,
        }
        for operation in plan.presence
    ]
    for operation, state in zip(plan.presence, presence_state, strict=True):
        evidence = inspect_content(operation.source)
        if evidence.size != operation.size or (operation.sha256 and evidence.sha256 != operation.sha256):
            raise StalePlanError("Recovery content changed before journal preparation.")
        state.update(size=evidence.size, sha256=evidence.sha256)
    rename_state = []
    for operation in plan.renames:
        evidence = inspect_content(operation.source)
        rename_state.append(dict(source=str(operation.source), temporary=str(temp_paths[operation.track_id]),
                                 target=str(operation.target), size=evidence.size, sha256=evidence.sha256))
    playlist_state: list[dict[str, str]] = []
    playlist_temps: dict[Path, Path] = {}
    for operation in plan.playlist_operations:
        temporary = _available_internal_path(
            plan.folder,
            ".trackindex-{}.m3u8{}",
            ".tmp",
        )
        backup = _available_internal_path(
            plan.folder,
            ".trackindex-{}.m3u8{}",
            ".backup",
        )
        if operation.after_bytes is not None:
            playlist_temps[operation.path] = temporary
        playlist_state.append(
            {
                "target": str(operation.path),
                "temporary": str(temporary),
                "backup": str(backup),
                "created": str(operation.before_bytes is None).lower(),
                "removed": str(operation.after_bytes is None).lower(),
                "before_sha256": (
                    hashlib.sha256(operation.before_bytes).hexdigest()
                    if operation.before_bytes is not None
                    else ""
                ),
                "after_sha256": (
                    hashlib.sha256(operation.after_bytes).hexdigest()
                    if operation.after_bytes is not None
                    else ""
                ),
            }
        )
    return _TransactionExecution(
        journal,
        temp_paths,
        import_temps,
        import_state,
        presence_state,
        playlist_state,
        playlist_temps,
        rename_state,
        (root_stat.st_dev, root_stat.st_ino),
        canonical_root,
    )


def _check_execution_root(plan: ChangePlan, execution: _TransactionExecution) -> None:
    folder = _validated_plan_folder(plan.folder)
    value = folder.stat()
    if folder != execution.canonical_root or (value.st_dev, value.st_ino) != execution.root_identity:
        raise StalePlanError("The selected folder changed during the transaction.")


def _checkpoint(
    plan: ChangePlan,
    execution: _TransactionExecution,
    phase: str,
    failure_injector: Callable[[str], None] | None,
) -> None:
    atomic_write_json(
        execution.journal,
        _journal_payload(
            plan,
            execution.temp_paths,
            execution.import_state,
            execution.presence_state,
            execution.playlist_state,
            phase,
            execution.rename_state,
        ),
    )
    if failure_injector is not None:
        failure_injector(phase)


def _prepare_transaction_inputs(
    plan: ChangePlan,
    execution: _TransactionExecution,
    failure_injector: Callable[[str], None] | None,
) -> None:
    _checkpoint(plan, execution, "prepared", failure_injector)
    _check_execution_root(plan, execution)
    for import_operation, state in zip(
        plan.imports,
        execution.import_state,
        strict=True,
    ):
        state["sha256"] = _copy_import_durable(
            import_operation,
            execution.import_temps[import_operation.track_id],
        )
    _checkpoint(plan, execution, "imports-prepared", failure_injector)
    for playlist_operation in plan.playlist_operations:
        if playlist_operation.after_bytes is not None:
            write_bytes_durable(
                execution.playlist_temps[playlist_operation.path],
                playlist_operation.after_bytes,
                exclusive=True,
            )
    if failure_injector is not None:
        failure_injector("playlist-temporaries")


def _commit_track_paths(
    plan: ChangePlan,
    execution: _TransactionExecution,
    failure_injector: Callable[[str], None] | None,
) -> None:
    _check_execution_root(plan, execution)
    for rename_operation, state in zip(plan.renames, execution.rename_state, strict=True):
        _verify_original(rename_operation.source, state)
        rename_operation.source.rename(
            execution.temp_paths[rename_operation.track_id]
        )
    _checkpoint(plan, execution, "temporary-renames", failure_injector)
    _check_execution_root(plan, execution)
    for presence_operation, state in zip(plan.presence, execution.presence_state, strict=True):
        if presence_operation.action == "stash":
            ensure_recovery_parent(presence_operation.target, plan.folder)
            _validate_presence_move(presence_operation.source, presence_operation.target, plan.folder)
            _verify_original(presence_operation.source, state)
            presence_operation.source.rename(presence_operation.target)
    _checkpoint(plan, execution, "presence-stashed", failure_injector)
    _check_execution_root(plan, execution)
    for rename_operation, state in zip(plan.renames, execution.rename_state, strict=True):
        _verify_original(execution.temp_paths[rename_operation.track_id], state)
        execution.temp_paths[rename_operation.track_id].rename(
            rename_operation.target
        )
    _checkpoint(plan, execution, "final-renames", failure_injector)
    _check_execution_root(plan, execution)
    for presence_operation, state in zip(plan.presence, execution.presence_state, strict=True):
        if presence_operation.action == "restore":
            _validate_presence_move(presence_operation.source, presence_operation.target, plan.folder)
            _verify_original(presence_operation.source, state)
            if presence_operation.target.exists():
                raise StalePlanError(
                    "A restored track target became occupied: "
                    f"{presence_operation.target.name}"
                )
            presence_operation.source.rename(presence_operation.target)
    _checkpoint(plan, execution, "presence-restored", failure_injector)
    _check_execution_root(plan, execution)
    for import_operation in plan.imports:
        if import_operation.target.exists():
            raise StalePlanError(
                f"An import target became occupied: {import_operation.target.name}"
            )
        execution.import_temps[import_operation.track_id].rename(
            import_operation.target
        )
    _checkpoint(plan, execution, "imports-final", failure_injector)


def _commit_playlists(
    plan: ChangePlan,
    execution: _TransactionExecution,
    failure_injector: Callable[[str], None] | None,
) -> None:
    _check_execution_root(plan, execution)
    for operation, state in zip(
        plan.playlist_operations,
        execution.playlist_state,
        strict=True,
    ):
        backup = Path(state["backup"])
        if operation.expected_sha256 is None and operation.path.exists():
            raise StalePlanError(
                f"The new playlist target now exists: {operation.path.name}"
            )
        if operation.expected_sha256 is not None and (
            not operation.path.exists()
            or _hash_file(operation.path) != operation.expected_sha256
        ):
            raise StalePlanError(
                "The playlist changed while changes were being prepared: "
                f"{operation.path.name}"
            )
        if operation.path.exists():
            operation.path.rename(backup)
        if operation.after_bytes is not None:
            execution.playlist_temps[operation.path].rename(operation.path)
    _checkpoint(plan, execution, "playlist-written", failure_injector)


def _complete_transaction(
    plan: ChangePlan,
    execution: _TransactionExecution,
) -> ApplyResult:
    _check_execution_root(plan, execution)
    internal_paths = {
        *execution.temp_paths.values(),
        *execution.import_temps.values(),
        *execution.playlist_temps.values(),
        *(Path(state["backup"]) for state in execution.playlist_state),
        plan.folder / SESSION_RECOVERY_FOLDER,
    }
    receipt = _commit_receipt(plan, internal_paths)
    atomic_write_json(
        execution.journal,
        _journal_payload(
            plan,
            execution.temp_paths,
            execution.import_state,
            execution.presence_state,
            execution.playlist_state,
            "complete",
            execution.rename_state,
        ),
    )
    cleanup_details: list[str] = []
    for state in execution.playlist_state:
        try:
            _verified_unlink(Path(state["backup"]), state["before_sha256"])
        except OSError as exc:
            cleanup_details.append(f"Could not remove playlist backup: {exc}")
    if not cleanup_details:
        try:
            execution.journal.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_details.append(f"Could not remove recovery journal: {exc}")
    return ApplyResult(
        True,
        "Changes were applied successfully."
        if not cleanup_details
        else "Changes were applied; recovery cleanup will be retried at startup.",
        renamed_count=len(plan.renames),
        playlists_written=len(plan.playlist_operations),
        recovery_journal=execution.journal if cleanup_details else None,
        details=tuple(cleanup_details),
        receipt=receipt,
    )


def apply_change_plan(
    plan: ChangePlan,
    journal_root: Path | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> ApplyResult:
    try:
        validate_plan(plan)
    except (OSError, RuntimeError, StalePlanError, ValueError) as exc:
        return ApplyResult(False, str(exc))

    journal_parent = journal_root or recovery_dir()
    try:
        journal_parent.mkdir(parents=True, exist_ok=True)
        if is_link_like(journal_parent):
            raise OSError("Recovery storage cannot use a link or junction.")
    except OSError as exc:
        return ApplyResult(False, f"Could not prepare recovery storage: {exc}")
    journal = journal_parent / f"{plan.plan_id}.json"
    try:
        execution = _prepare_execution(plan, journal)
        prepared = _journal_payload(plan, execution.temp_paths, execution.import_state,
                                    execution.presence_state, execution.playlist_state,
                                    "prepared", execution.rename_state)
        largest = json.loads(json.dumps(prepared))
        for item in largest["renames"]:
            item["rollback_state"] = "restoring"
        for key in ("presence", "imports", "playlists"):
            for item in largest[key]:
                item["rollback_done"] = False
        largest["phase"] = "temporary-renames"
        if len((json.dumps(largest, indent=2) + "\n").encode("utf-8")) > _MAX_JOURNAL_BYTES:
            raise ValueError("The recovery journal would exceed its supported size.")
    except (OSError, RuntimeError, ValueError) as exc:
        return ApplyResult(False, f"Could not prepare verified recovery evidence: {exc}")
    try:
        _prepare_transaction_inputs(plan, execution, failure_injector)
        _commit_track_paths(plan, execution, failure_injector)
        _commit_playlists(plan, execution, failure_injector)
        return _complete_transaction(plan, execution)
    except Exception as exc:
        recovery = (
            recover_journal(journal)
            if journal.exists()
            else ApplyResult(False, "No recovery journal was created.")
        )
        details = (str(exc), *recovery.details)
        return ApplyResult(
            False,
            "Applying changes failed; TrackIndex attempted to restore the original state.",
            recovery_journal=journal if not recovery.success else None,
            details=tuple(details),
        )
    finally:
        if not journal.exists():
            for temporary in (*execution.import_temps.values(), *execution.playlist_temps.values()):
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
