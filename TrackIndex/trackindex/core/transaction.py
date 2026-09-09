from __future__ import annotations

import hashlib
import json
import os
import re
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
from .models import (
    ApplyResult,
    ChangePlan,
    CommitReceipt,
    CommittedPlaylistState,
    CommittedTrackState,
)
from .paths import is_direct_child, recovery_dir
from .presence import (
    ensure_recovery_parent,
    is_managed_entry,
    is_recovery_path,
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
    source_names, target_names, track_ids = _validate_renames(plan, folder)
    _validate_imports(plan, folder, source_names, target_names, track_ids)
    _validate_presence(plan, folder, source_names, target_names, track_ids)
    _validate_playlist_operations(plan, folder)
    _validate_inventory_and_snapshots(plan, folder)
    _validate_occupied_rename_targets(plan, source_names)
    _validate_playlist_state(plan, folder)


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
        if not is_direct_child(rename.source, folder) or not is_direct_child(rename.target, folder):
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
        if not is_direct_child(operation.target, folder):
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
        source_direct = is_direct_child(presence_operation.source, folder)
        target_direct = is_direct_child(presence_operation.target, folder)
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
            not is_direct_child(playlist_op.path, folder)
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
        if not is_direct_child(snapshot.path, folder):
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
        if not is_direct_child(playlist_op.path, folder):
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
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plan_id": plan.plan_id,
        "folder": str(plan.folder),
        "phase": phase,
        "renames": [
            {
                "source": str(operation.source),
                "temporary": str(temp_paths[operation.track_id]),
                "target": str(operation.target),
            }
            for operation in plan.renames
        ],
        "imports": import_state,
        "presence": presence_state,
        "playlists": playlist_state,
    }


def _journal_items(payload: dict[str, object]) -> tuple[Path, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], str]:
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

    raw_folder = str(payload.get("folder", "")).strip()
    if raw_folder:
        folder = Path(raw_folder).resolve(strict=False)
    elif renames:
        folder = Path(str(renames[0].get("source", ""))).resolve(strict=False).parent
    elif imports:
        folder = Path(str(imports[0].get("target", ""))).resolve(strict=False).parent
    elif presence:
        source = Path(str(presence[0].get("source", ""))).resolve(strict=False)
        target = Path(str(presence[0].get("target", ""))).resolve(strict=False)
        folder = source.parent if presence[0].get("action") == "stash" else target.parent
    elif playlists:
        folder = Path(str(playlists[0].get("target", ""))).resolve(strict=False).parent
    else:
        raise ValueError("Recovery journal does not identify its folder.")

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
            or not (is_direct_child(source, folder) or is_recovery_path(source, folder))
            or not (is_direct_child(target, folder) or is_recovery_path(target, folder))
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
            raw_path = str(item.get(key, "")).strip()
            if raw_path:
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
                Path(raw_temporary).unlink(missing_ok=True)
            except OSError as exc:
                details.append(f"Could not remove completed import data: {exc}")
    for item in playlists:
        for key in ("temporary", "backup"):
            raw_cleanup_path = str(item.get(key, ""))
            if not raw_cleanup_path:
                continue
            cleanup_path = Path(raw_cleanup_path)
            try:
                cleanup_path.unlink(missing_ok=True)
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
) -> None:
    for item in reversed(playlists):
        target = Path(str(item.get("target", "")))
        backup = Path(str(item.get("backup", "")))
        created = str(item.get("created", "false")).lower() == "true"
        raw_temporary = str(item.get("temporary", "")).strip()
        temporary = Path(raw_temporary) if raw_temporary else None
        try:
            if backup.exists():
                if target.exists():
                    after_hash = str(item.get("after_sha256", ""))
                    if not after_hash or _hash_file(target).casefold() != after_hash.casefold():
                        details.append(
                            f"Could not restore {target.name} because it changed after the interruption."
                        )
                        continue
                    target.unlink()
                backup.rename(target)
            elif created and target.exists():
                after_hash = str(item.get("after_sha256", ""))
                if not after_hash or _hash_file(target).casefold() != after_hash.casefold():
                    details.append(
                        f"Could not remove {target.name} because it changed after the interruption."
                    )
                    continue
                target.unlink()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            details.append(f"Could not restore playlist {target.name}: {exc}")


def _remove_imports(
    imports: list[dict[str, object]],
    phase: str,
    details: list[str],
) -> None:
    for item in reversed(imports):
        target = Path(str(item.get("target", "")))
        temporary = Path(str(item.get("temporary", "")))
        digest = str(item.get("sha256", ""))
        for candidate in (target, temporary):
            if not candidate.exists():
                continue
            try:
                uncommitted = candidate == temporary and phase in {
                    "prepared",
                    "imports-prepared",
                    "temporary-renames",
                    "final-renames",
                }
                if not uncommitted and (
                    not digest
                    or _hash_file(candidate).casefold() != digest.casefold()
                ):
                    details.append(
                        f"Could not remove imported copy {candidate.name} because it changed."
                    )
                    continue
                candidate.unlink()
            except OSError as exc:
                details.append(
                    f"Could not remove imported copy {candidate.name}: {exc}"
                )


def _return_restored_presence(
    folder: Path,
    presence: list[dict[str, object]],
    details: list[str],
) -> None:
    for item in reversed(presence):
        if str(item.get("action", "")) != "restore":
            continue
        source = Path(str(item.get("source", "")))
        target = Path(str(item.get("target", "")))
        try:
            if target.exists() and not source.exists():
                ensure_recovery_parent(source, folder)
                target.rename(source)
        except OSError as exc:
            details.append(
                f"Could not return restored track to recovery storage: {exc}"
            )


def _restore_renames(
    renames: list[dict[str, object]],
    phase: str,
    details: list[str],
) -> None:
    staged: list[tuple[Path, Path]] = []
    for item in renames:
        source = Path(str(item.get("source", "")))
        temporary = Path(str(item.get("temporary", "")))
        target = Path(str(item.get("target", "")))
        current: Path | None = None
        if temporary.exists():
            current = temporary
        elif phase not in {"prepared", "imports-prepared"} and target.exists():
            current = target
        elif source.exists():
            continue
        if current is None:
            details.append(f"Could not locate the original data for {source.name}.")
            continue
        recovery_temp = source.parent / (
            f".trackindex-recover-{uuid.uuid4().hex}.tmp{source.suffix}"
        )
        try:
            current.rename(recovery_temp)
            staged.append((recovery_temp, source))
        except OSError as exc:
            details.append(f"Could not stage {source.name} for recovery: {exc}")
    for recovery_temp, source in staged:
        try:
            if source.exists():
                details.append(
                    f"Could not restore {source.name} because the original path is occupied."
                )
                continue
            recovery_temp.rename(source)
        except OSError as exc:
            details.append(f"Could not restore {source.name}: {exc}")


def _restore_stashed_presence(
    presence: list[dict[str, object]],
    details: list[str],
) -> None:
    for item in reversed(presence):
        if str(item.get("action", "")) != "stash":
            continue
        source = Path(str(item.get("source", "")))
        target = Path(str(item.get("target", "")))
        try:
            if target.exists() and not source.exists():
                target.rename(source)
        except OSError as exc:
            details.append(f"Could not restore removed track {source.name}: {exc}")


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
    _restore_playlists(playlists, details)
    _remove_imports(imports, phase, details)
    _return_restored_presence(_folder, presence, details)
    _restore_renames(renames, phase, details)
    _restore_stashed_presence(presence, details)
    if details:
        return ApplyResult(False, "Recovery was incomplete.", recovery_journal=journal_path, details=tuple(details))
    with suppress(OSError):
        journal_path.unlink(missing_ok=True)
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


def _available_internal_path(folder: Path, pattern: str, suffix: str) -> Path:
    while True:
        candidate = folder / pattern.format(uuid.uuid4().hex, suffix)
        if not candidate.exists():
            return candidate


def _prepare_execution(plan: ChangePlan, journal: Path) -> _TransactionExecution:
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
            "sha256": "",
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
    )


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
    for rename_operation in plan.renames:
        rename_operation.source.rename(
            execution.temp_paths[rename_operation.track_id]
        )
    _checkpoint(plan, execution, "temporary-renames", failure_injector)
    for presence_operation in plan.presence:
        if presence_operation.action == "stash":
            ensure_recovery_parent(presence_operation.target, plan.folder)
            presence_operation.source.rename(presence_operation.target)
    _checkpoint(plan, execution, "presence-stashed", failure_injector)
    for rename_operation in plan.renames:
        execution.temp_paths[rename_operation.track_id].rename(
            rename_operation.target
        )
    _checkpoint(plan, execution, "final-renames", failure_injector)
    for presence_operation in plan.presence:
        if presence_operation.action == "restore":
            if presence_operation.target.exists():
                raise StalePlanError(
                    "A restored track target became occupied: "
                    f"{presence_operation.target.name}"
                )
            presence_operation.source.rename(presence_operation.target)
    _checkpoint(plan, execution, "presence-restored", failure_injector)
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
        ),
    )
    cleanup_details: list[str] = []
    for state in execution.playlist_state:
        try:
            Path(state["backup"]).unlink(missing_ok=True)
        except OSError as exc:
            cleanup_details.append(f"Could not remove playlist backup: {exc}")
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
    execution = _prepare_execution(plan, journal)
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
        for temporary in execution.import_temps.values():
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        for temporary in execution.playlist_temps.values():
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
