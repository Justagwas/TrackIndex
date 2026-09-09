from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path

from .config import (
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_PLAYLIST_EXTENSIONS,
    default_playlist_name,
)
from .filesystem import is_link_like
from .integrity import file_sha256
from .models import (
    AuditResult,
    ChangePlan,
    FileSnapshot,
    ImportOperation,
    OrderAuthority,
    PlaylistOperation,
    PresenceOperation,
    RenameOperation,
    SessionCommand,
    StorageMode,
    TrackRecord,
)
from .paths import is_direct_child
from .playlist_service import (
    migrated_playlist_document,
    new_playlist_document,
    parse_playlist,
    playlist_references_any,
    render_playlist,
)
from .presence import is_managed_entry, recovery_path
from .scanner import parse_filename_index
from .windows_names import is_valid_windows_name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_playlist_target(audit: AuditResult, playlist_target: Path | None) -> Path:
    target = playlist_target or audit.selected_playlist or (audit.folder / default_playlist_name(audit.folder))
    if (
        not is_direct_child(target, audit.folder)
        or target.suffix.casefold() not in SUPPORTED_PLAYLIST_EXTENSIONS
    ):
        raise ValueError(
            "Playlist target must be an .m3u or .m3u8 file directly inside the selected folder"
        )
    target = target.resolve(strict=False)
    if not is_valid_windows_name(target.name):
        raise ValueError("Playlist target is not a valid Windows filename")
    if len(str(target)) >= 260:
        raise ValueError("Playlist target path is too long for conservative Windows compatibility")
    return target


def _ordered_tracks(
    audit: AuditResult,
    ordered_track_ids: tuple[str, ...] | list[str],
    departing_track_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], list[TrackRecord], list[str]]:
    ordered_ids = tuple(ordered_track_ids)
    track_map = audit.track_map()
    departing = set(departing_track_ids)
    expected_ids = set(track_map) - departing
    blockers: list[str] = []
    if (
        not departing.issubset(track_map)
        or len(ordered_ids) != len(expected_ids)
        or set(ordered_ids) != expected_ids
    ):
        blockers.append("The arranged track list no longer matches the audited folder.")
        ordered_ids = tuple(item for item in audit.display_order if item in expected_ids)
    tracks = [track_map[track_id] for track_id in ordered_ids if track_id in track_map]
    return ordered_ids, tracks, blockers


def _filename_changes(
    audit: AuditResult,
    tracks: list[TrackRecord],
    should_rename: bool,
) -> tuple[
    dict[str, str],
    list[tuple[str, str]],
    list[RenameOperation],
    list[str],
]:
    width = max(3, len(str(max(1, len(tracks)))))
    final_by_original: dict[str, str] = {}
    filename_preview: list[tuple[str, str]] = []
    renames: list[RenameOperation] = []
    blockers: list[str] = []
    for position, track in enumerate(tracks, start=1):
        final_name = (
            f"{position:0{width}d} - {track.base_stem}{track.suffix}"
            if should_rename
            else track.original_name
        )
        final_by_original[track.original_name.casefold()] = final_name
        filename_preview.append((track.original_name, final_name))
        if not is_valid_windows_name(final_name):
            blockers.append(f"Proposed filename is not valid on Windows: {final_name}")
        if len(str(audit.folder / final_name)) >= 260:
            blockers.append(
                "Proposed path is too long for conservative Windows compatibility: "
                f"{final_name}"
            )
        if track.original_name != final_name:
            renames.append(
                RenameOperation(track.track_id, track.path, audit.folder / final_name)
            )
    return final_by_original, filename_preview, renames, blockers


def _folder_entries(
    audit: AuditResult,
    final_by_original: dict[str, str],
) -> tuple[tuple[Path, ...], list[str]]:
    folded_targets: dict[str, str] = {}
    blockers: list[str] = []
    for final_name in final_by_original.values():
        folded = final_name.casefold()
        if folded in folded_targets:
            blockers.append(
                f"Case-insensitive filename collision: {folded_targets[folded]} / {final_name}"
            )
        else:
            folded_targets[folded] = final_name
    try:
        entries = tuple(
            path for path in audit.folder.iterdir() if not is_managed_entry(path)
        )
    except OSError as exc:
        blockers.append(f"Could not re-read the selected folder: {exc}")
        return (), blockers
    source_names = {track.original_name.casefold() for track in audit.tracks}
    for path in entries:
        folded = path.name.casefold()
        if folded in folded_targets and folded not in source_names:
            blockers.append(
                f"A proposed filename is already occupied by an unmanaged item: {path.name}"
            )
    return entries, blockers


def _playlist_changes(
    audit: AuditResult,
    tracks: list[TrackRecord],
    storage_mode: StorageMode,
    playlist_target: Path | None,
    playlist_source: Path | None,
    final_by_original: dict[str, str],
) -> tuple[
    Path | None,
    bool,
    list[PlaylistOperation],
    tuple[FileSnapshot, ...],
    list[str],
]:
    operations: list[PlaylistOperation] = []
    snapshots: tuple[FileSnapshot, ...] = ()
    blockers: list[str] = []
    target: Path | None = None
    wants_playlist = storage_mode in {StorageMode.M3U8, StorageMode.BOTH}
    if storage_mode is StorageMode.FILENAMES:
        candidate = playlist_target or audit.selected_playlist
        wants_playlist = bool(candidate and candidate.exists())
    if not wants_playlist:
        return target, wants_playlist, operations, snapshots, blockers
    try:
        target = _resolve_playlist_target(audit, playlist_target)
    except ValueError as exc:
        blockers.append(str(exc))
        return target, wants_playlist, operations, snapshots, blockers
    available = {
        track.original_name.casefold(): track.original_name
        for track in audit.tracks
    }
    if target.exists():
        document = parse_playlist(target, audit.folder, available)
        referenced_names = {track.original_name for track in tracks}
        if storage_mode is StorageMode.FILENAMES and not document.fully_inspected:
            blockers.extend(document.errors)
        elif (
            storage_mode is StorageMode.FILENAMES
            and not playlist_references_any(document, referenced_names)
        ):
            return target, False, operations, snapshots, blockers
        elif document.errors:
            blockers.extend(document.errors)
        before = document.before_bytes
        expected_hash = document.sha256
        action = "update"
        try:
            target_stat = target.stat()
        except OSError as exc:
            blockers.append(f"Could not inspect {target.name}: {exc}")
        else:
            snapshots = (
                FileSnapshot(target, target_stat.st_size, target_stat.st_mtime_ns),
            )
    elif (
        playlist_source is not None
        and playlist_source.exists()
        and playlist_source.resolve(strict=False) != target
    ):
        source = playlist_source.resolve(strict=False)
        if (
            not is_direct_child(source, audit.folder)
            or source.suffix.casefold() not in SUPPORTED_PLAYLIST_EXTENSIONS
        ):
            blockers.append("The playlist migration source is outside the selected folder.")
            document = new_playlist_document(target)
        else:
            source_document = parse_playlist(source, audit.folder, available)
            if source_document.errors:
                blockers.extend(source_document.errors)
            document = migrated_playlist_document(source_document, target)
            try:
                source_stat = source.stat()
            except OSError as exc:
                blockers.append(f"Could not inspect {source.name}: {exc}")
            else:
                snapshots = (
                    FileSnapshot(source, source_stat.st_size, source_stat.st_mtime_ns),
                )
        before = None
        expected_hash = None
        action = "create"
    else:
        document = new_playlist_document(target)
        before = None
        expected_hash = None
        action = "create"
    if document.safe_to_rewrite:
        ordered_names = tuple(track.original_name for track in tracks)
        after = render_playlist(document, ordered_names, final_by_original)
        operations.append(
            PlaylistOperation(target, action, before, after, expected_hash)
        )
    return target, wants_playlist, operations, snapshots, blockers


def build_change_plan(
    audit: AuditResult,
    ordered_track_ids: tuple[str, ...] | list[str],
    storage_mode: StorageMode,
    authority: OrderAuthority,
    playlist_target: Path | None = None,
    *,
    departing_track_ids: tuple[str, ...] = (),
    playlist_source: Path | None = None,
) -> ChangePlan:
    track_map = audit.track_map()
    warnings: list[str] = []
    departing = set(departing_track_ids)
    ordered_ids, tracks, blockers = _ordered_tracks(
        audit,
        ordered_track_ids,
        departing_track_ids,
    )
    if audit.requires_authority_choice:
        blockers.append("Choose filename or playlist order before previewing changes.")
    if len(audit.playlists) > 1 and playlist_target is None:
        blockers.append("Choose the playlist TrackIndex should manage before previewing changes.")

    snapshots = tuple(FileSnapshot(track.path, track.size, track.mtime_ns) for track in tracks)
    should_rename = storage_mode in {StorageMode.FILENAMES, StorageMode.BOTH}
    final_by_original, filename_preview, renames, filename_blockers = (
        _filename_changes(audit, tracks, should_rename)
    )
    blockers.extend(filename_blockers)
    folder_entries, folder_blockers = _folder_entries(audit, final_by_original)
    blockers.extend(folder_blockers)

    (
        target,
        wants_playlist,
        playlist_operations,
        playlist_snapshots,
        playlist_blockers,
    ) = _playlist_changes(
        audit,
        tracks,
        storage_mode,
        playlist_target,
        playlist_source,
        final_by_original,
    )
    snapshots += playlist_snapshots
    blockers.extend(playlist_blockers)

    affected_names = {rename.source.name for rename in renames}
    affected_names.update(
        track_map[track_id].original_name
        for track_id in departing
        if track_id in track_map
    )
    if affected_names:
        audited_names = {track.original_name for track in audit.tracks}
        available = {name.casefold(): name for name in audited_names}
        for other in audit.playlists:
            if target is not None and other.resolve() == target:
                continue
            document = parse_playlist(other, audit.folder, available)
            if (
                playlist_references_any(document, affected_names)
                and (playlist_source is None or other.resolve() != playlist_source.resolve(strict=False))
            ):
                warnings.append(f"{other.name} is not managed and may contain stale paths after files are renamed.")

    for issue in audit.issues:
        if storage_mode is StorageMode.M3U8 and issue.code in {"duplicate-index", "index-gap"}:
            continue
        if issue.code == "authority-required" and not audit.requires_authority_choice:
            continue
        if issue.severity.value == "blocker" and issue.code != "authority-required":
            if issue.message not in blockers and wants_playlist:
                blockers.append(issue.message)
        elif issue.message not in warnings:
            warnings.append(issue.message)

    return ChangePlan(
        plan_id=uuid.uuid4().hex,
        folder=audit.folder,
        storage_mode=storage_mode,
        authority=authority,
        ordered_track_ids=ordered_ids,
        renames=tuple(renames),
        playlist_operations=tuple(playlist_operations),
        snapshots=snapshots,
        filename_preview=tuple(filename_preview),
        folder_inventory=tuple(sorted((path.name for path in folder_entries), key=str.casefold)),
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
    )


def build_import_plan(
    audit: AuditResult,
    sources: tuple[Path, ...],
    insertion: int,
    storage_mode: StorageMode,
    authority: OrderAuthority,
    playlist_target: Path | None = None,
) -> ChangePlan:


    if not sources:
        raise ValueError("No audio files were supplied.")
    folder = audit.folder.resolve(strict=False)
    existing_paths = {str(track.path.resolve(strict=False)).casefold() for track in audit.tracks}
    try:
        occupied = {path.name.casefold() for path in folder.iterdir()}
    except OSError as exc:
        raise ValueError(f"Could not inspect the selected library: {exc}") from exc

    provisional: list[TrackRecord] = []
    import_sources: dict[str, tuple[Path, int, int, str]] = {}
    seen_sources: set[str] = set()
    for raw_source in sources:
        raw_source = Path(raw_source)
        if is_link_like(raw_source):
            raise ValueError(f"Linked files cannot be imported: {raw_source.name}")
        source = raw_source.resolve(strict=True)
        source_key = str(source).casefold()
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        if source_key in existing_paths:
            raise ValueError(f"{source.name} is already in this library.")
        if (
            is_link_like(source)
            or not source.is_file()
            or source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
        ):
            raise ValueError(f"Unsupported or unavailable audio file: {source.name}")
        if not is_valid_windows_name(source.name):
            raise ValueError(f"The filename is not valid on Windows: {source.name}")
        stat = source.stat()
        candidate = source.name
        counter = 2
        while candidate.casefold() in occupied:
            candidate = f"{source.stem} ({counter}){source.suffix}"
            counter += 1
        if not is_valid_windows_name(candidate) or len(str(folder / candidate)) >= 260:
            raise ValueError(f"The imported filename is not safe for this library: {candidate}")
        occupied.add(candidate.casefold())
        track_id = uuid.uuid4().hex
        index, base_stem = parse_filename_index(Path(candidate))
        provisional.append(
            TrackRecord(
                track_id,
                folder / candidate,
                candidate,
                base_stem,
                source.suffix,
                index,
                None,
                True,
                False,
                stat.st_size,
                stat.st_mtime_ns,
            )
        )
        try:
            source_hash = file_sha256(source)
        except OSError as exc:
            raise ValueError(f"Could not verify the imported file {source.name}: {exc}") from exc
        import_sources[track_id] = (
            source,
            stat.st_size,
            stat.st_mtime_ns,
            source_hash,
        )

    if not provisional:
        raise ValueError("No new audio files were supplied.")
    insertion = max(0, min(len(audit.display_order), insertion))
    imported_ids = tuple(track.track_id for track in provisional)
    ordered_ids = (
        audit.display_order[:insertion]
        + imported_ids
        + audit.display_order[insertion:]
    )
    augmented = replace(
        audit,
        tracks=(*audit.tracks, *provisional),
        filename_order=(*audit.filename_order, *imported_ids),
        playlist_order=(*audit.playlist_order, *imported_ids) if audit.playlist_order else (),
        display_order=ordered_ids,
    )
    plan = build_change_plan(
        augmented,
        ordered_ids,
        storage_mode,
        authority,
        playlist_target,
    )
    imported_set = set(imported_ids)
    final_names = {
        track_id: after_name
        for track_id, (_before_name, after_name) in zip(
            plan.ordered_track_ids, plan.filename_preview, strict=True
        )
    }
    imports = tuple(
        ImportOperation(
            track_id,
            import_sources[track_id][0],
            folder / final_names[track_id],
            import_sources[track_id][1],
            import_sources[track_id][2],
            import_sources[track_id][3],
        )
        for track_id in imported_ids
    )
    return replace(
        plan,
        renames=tuple(item for item in plan.renames if item.track_id not in imported_set),
        snapshots=tuple(
            snapshot
            for snapshot in plan.snapshots
            if str(snapshot.path.resolve(strict=False)).casefold()
            not in {str(track.path.resolve(strict=False)).casefold() for track in provisional}
        ),
        imports=imports,
    )


def build_delete_plan(
    audit: AuditResult,
    track_ids: tuple[str, ...],
    storage_mode: StorageMode,
    authority: OrderAuthority,
    playlist_target: Path | None = None,
) -> ChangePlan:


    selected = tuple(dict.fromkeys(track_ids))
    track_map = audit.track_map()
    if not selected or any(track_id not in track_map for track_id in selected):
        raise ValueError("No available tracks were selected for removal.")
    selected_set = set(selected)
    remaining = tuple(
        track_id for track_id in audit.display_order if track_id not in selected_set
    )
    plan = build_change_plan(
        audit,
        remaining,
        storage_mode,
        authority,
        playlist_target,
        departing_track_ids=selected,
    )
    moves: list[PresenceOperation] = []
    for track_id in selected:
        track = track_map[track_id]
        recovery_name = f"{track_id}{track.suffix.casefold()}"
        moves.append(
            PresenceOperation(
                track_id,
                track.path,
                recovery_path(audit.folder, plan.plan_id, recovery_name),
                track.size,
                track.mtime_ns,
                "stash",
                file_sha256(track.path),
            )
        )
    selected_snapshots = tuple(
        FileSnapshot(
            track_map[track_id].path,
            track_map[track_id].size,
            track_map[track_id].mtime_ns,
        )
        for track_id in selected
    )
    return replace(
        plan,
        snapshots=(*plan.snapshots, *selected_snapshots),
        presence=tuple(moves),
    )


def build_session_change_plan(
    audit: AuditResult,
    command: SessionCommand,
    *,
    to_after: bool,
) -> ChangePlan:


    source_pairs = command.before_names if to_after else command.after_names
    target_pairs = command.after_names if to_after else command.before_names
    source_ids = {track_id for track_id, _name in source_pairs}
    target_names = dict(target_pairs)
    target_order = command.after_order if to_after else command.before_order
    current_ids = set(audit.track_map())
    folded_targets: dict[str, str] = {}
    target_collisions: list[str] = []
    for target_name in target_names.values():
        key = target_name.casefold()
        existing = folded_targets.get(key)
        if existing is not None:
            target_collisions.append(
                f"Case-insensitive filename collision: {existing} / {target_name}"
            )
        else:
            folded_targets[key] = target_name
    if current_ids != source_ids:
        fallback_order = tuple(item for item in target_order if item in current_ids)
        fallback_names = {
            item: name for item, name in target_pairs if item in current_ids
        }
        fallback = build_exact_change_plan(
            audit,
            fallback_order,
            fallback_names,
            command.playlist_path,
            command.after_playlist_bytes if to_after else command.before_playlist_bytes,
            allow_subset=True,
        )
        return replace(
            fallback,
            blockers=("The tracks on disk no longer match this session command.",),
        )

    common_ids = current_ids & set(target_names)
    common_order = tuple(item for item in target_order if item in common_ids)
    base = build_exact_change_plan(
        audit,
        common_order,
        {item: target_names[item] for item in common_ids},
        command.playlist_path,
        command.after_playlist_bytes if to_after else command.before_playlist_bytes,
        allow_subset=True,
    )
    presence_by_id = {item.track_id: item for item in command.presence}
    operations: list[PresenceOperation] = []
    blockers = [*base.blockers, *target_collisions]
    track_map = audit.track_map()
    for track_id in source_ids - set(target_names):
        state = presence_by_id.get(track_id)
        track = track_map[track_id]
        if state is None:
            blockers.append("The session command has no recovery location for a removed track.")
            continue
        operations.append(
            PresenceOperation(
                track_id,
                track.path,
                recovery_path(audit.folder, command.command_id, state.recovery_name),
                track.size,
                track.mtime_ns,
                "stash",
            )
        )
    for track_id in set(target_names) - source_ids:
        state = presence_by_id.get(track_id)
        if state is None:
            blockers.append("The session command has no recovery location for a restored track.")
            continue
        source = recovery_path(audit.folder, command.command_id, state.recovery_name)
        try:
            stat = source.stat()
        except OSError:
            blockers.append(f"Recovery data is unavailable for {target_names[track_id]}.")
            continue
        operations.append(
            PresenceOperation(
                track_id,
                source,
                audit.folder / target_names[track_id],
                state.size,
                stat.st_mtime_ns,
                "restore",
                state.sha256,
            )
        )

    preview_by_id = dict(zip(common_order, base.filename_preview, strict=True))
    source_name_map = dict(source_pairs)
    preview = tuple(
        preview_by_id.get(
            track_id,
            (source_name_map.get(track_id, ""), target_names[track_id]),
        )
        for track_id in target_order
    )
    return replace(
        base,
        ordered_track_ids=tuple(target_order),
        filename_preview=preview,
        presence=tuple(operations),
        blockers=tuple(dict.fromkeys(blockers)),
    )


def build_prefix_removal_plan(audit: AuditResult, playlist_target: Path) -> ChangePlan:

    target_names = {
        track.track_id: f"{track.base_stem}{track.suffix}"
        for track in audit.tracks
    }
    available = {track.original_name.casefold(): track.original_name for track in audit.tracks}
    try:
        playlist_target = _resolve_playlist_target(audit, playlist_target)
    except ValueError as exc:
        plan = build_exact_change_plan(
            audit, audit.display_order, target_names, None, None, OrderAuthority.PLAYLIST
        )
        return replace(
            plan,
            storage_mode=StorageMode.M3U8,
            blockers=tuple(dict.fromkeys((*plan.blockers, str(exc)))),
        )
    document = (
        parse_playlist(playlist_target, audit.folder, available)
        if playlist_target.exists()
        else new_playlist_document(playlist_target)
    )
    final_by_original = {
        track.original_name.casefold(): target_names[track.track_id]
        for track in audit.tracks
    }
    ordered_names = tuple(audit.track_map()[track_id].original_name for track_id in audit.display_order)
    after = (
        render_playlist(document, ordered_names, final_by_original)
        if document.safe_to_rewrite
        else document.before_bytes
    )
    plan = build_exact_change_plan(
        audit,
        audit.display_order,
        target_names,
        playlist_target,
        after,
        OrderAuthority.PLAYLIST,
    )
    blockers = list(plan.blockers)
    blockers.extend(message for message in document.errors if message not in blockers)
    warnings = list(plan.warnings)
    audited_names = {track.original_name for track in audit.tracks}
    for other in audit.playlists:
        if other.resolve() == playlist_target.resolve():
            continue
        other_document = parse_playlist(other, audit.folder, available)
        if playlist_references_any(other_document, audited_names):
            warnings.append(f"{other.name} is not managed and may contain stale paths after files are renamed.")
    return replace(
        plan,
        storage_mode=StorageMode.M3U8,
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
    )


def build_exact_change_plan(
    audit: AuditResult,
    ordered_track_ids: tuple[str, ...],
    target_names: dict[str, str],
    playlist_path: Path | None,
    playlist_after: bytes | None,
    authority: OrderAuthority = OrderAuthority.FILENAMES,
    *,
    allow_subset: bool = False,
) -> ChangePlan:

    track_map = audit.track_map()
    blockers: list[str] = []
    if (
        len(ordered_track_ids) != len(set(ordered_track_ids))
        or not set(ordered_track_ids).issubset(track_map)
        or (not allow_subset and set(ordered_track_ids) != set(track_map))
    ):
        blockers.append("The tracks on disk no longer match this session command.")
    renames: list[RenameOperation] = []
    preview: list[tuple[str, str]] = []
    folded: dict[str, str] = {}
    for track_id in ordered_track_ids:
        track = track_map.get(track_id)
        target_name = target_names.get(track_id, "")
        if track is None or not is_valid_windows_name(target_name):
            blockers.append("A session command contains an unavailable or invalid filename.")
            continue
        key = target_name.casefold()
        if key in folded:
            blockers.append(f"Case-insensitive filename collision: {folded[key]} / {target_name}")
        else:
            folded[key] = target_name
        if len(str(audit.folder / target_name)) >= 260:
            blockers.append(f"A session filename path is too long: {target_name}")
        preview.append((track.original_name, target_name))
        if track.original_name != target_name:
            renames.append(RenameOperation(track_id, track.path, audit.folder / target_name))
    source_names = {track.original_name.casefold() for track in audit.tracks}
    try:
        entries = tuple(
            path for path in audit.folder.iterdir() if not is_managed_entry(path)
        )
    except OSError as exc:
        blockers.append(f"Could not re-read the selected folder: {exc}")
        entries = ()
    for item in entries:
        if item.name.casefold() in folded and item.name.casefold() not in source_names:
            blockers.append(f"A restore target is occupied by an unmanaged item: {item.name}")
    playlist_operations: tuple[PlaylistOperation, ...] = ()
    if playlist_path is not None:
        try:
            safe_playlist_path = _resolve_playlist_target(audit, playlist_path)
        except ValueError as exc:
            blockers.append(str(exc))
        else:
            try:
                before = safe_playlist_path.read_bytes() if safe_playlist_path.exists() else None
            except OSError as exc:
                blockers.append(f"Could not read {safe_playlist_path.name}: {exc}")
            else:
                playlist_operations = (
                    PlaylistOperation(
                        safe_playlist_path,
                        "remove" if playlist_after is None else ("update" if before is not None else "create"),
                        before,
                        playlist_after,
                        _sha256(before) if before is not None else None,
                    ),
                )
    snapshots = [FileSnapshot(item.path, item.size, item.mtime_ns) for item in audit.tracks]
    if playlist_operations and playlist_operations[0].path.exists():
        try:
            playlist_stat = playlist_operations[0].path.stat()
        except OSError as exc:
            blockers.append(f"Could not inspect {playlist_operations[0].path.name}: {exc}")
        else:
            snapshots.append(
                FileSnapshot(
                    playlist_operations[0].path,
                    playlist_stat.st_size,
                    playlist_stat.st_mtime_ns,
                )
            )
    return ChangePlan(
        uuid.uuid4().hex,
        audit.folder,
        StorageMode.BOTH,
        authority,
        ordered_track_ids,
        tuple(renames),
        playlist_operations,
        tuple(snapshots),
        tuple(preview),
        tuple(sorted((item.name for item in entries), key=str.casefold)),
        blockers=tuple(dict.fromkeys(blockers)),
    )
