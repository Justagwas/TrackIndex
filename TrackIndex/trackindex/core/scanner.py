from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import SUPPORTED_AUDIO_EXTENSIONS, SUPPORTED_PLAYLIST_EXTENSIONS
from .filesystem import is_link_like, regular_file_stat
from .models import (
    AuditIssue,
    AuditResult,
    ChangePlan,
    CommitReceipt,
    IssueSeverity,
    OrderAuthority,
    StorageMode,
    TrackRecord,
)
from .playlist_service import parse_playlist

_PREFIX_RE = re.compile(
    r"^\s*(?P<index>\d+)\s*(?:-\s*|[.)]\s*(?:-\s*)?)(?P<name>\S(?:.*?\S)?)\s*$"
)
_NATURAL_RE = re.compile(r"(\d+)")


@dataclass(slots=True)
class _ScannedTrack:
    track_id: str
    path: Path
    original_name: str
    base_stem: str
    suffix: str
    filename_index: int | None
    size: int
    mtime_ns: int
    playlist_index: int | None = None


def natural_key(value: str) -> tuple[tuple[object, ...], ...]:
    parts = _NATURAL_RE.split(value.casefold())
    result: list[tuple[object, ...]] = []
    for part in parts:
        if part.isdigit():
            normalized = part.lstrip("0") or "0"
            result.append((1, len(normalized), normalized, len(part)))
        else:
            result.append((0, part))
    return tuple(result)


def parse_filename_index(path: Path) -> tuple[int | None, str]:
    match = _PREFIX_RE.match(path.stem)
    if not match:
        return None, path.stem
    return int(match.group("index")), match.group("name").strip()


def _orders_disagree(filename_order: tuple[str, ...], playlist_order: tuple[str, ...]) -> bool:
    playlist_set = set(playlist_order)
    common = set(filename_order) & playlist_set
    if len(common) < 2:
        return False
    return tuple(item for item in filename_order if item in common) != tuple(item for item in playlist_order if item in common)


def _index_issues(indexes: list[int]) -> tuple[list[AuditIssue], set[int]]:
    counts = Counter(indexes)
    issues: list[AuditIssue] = []
    duplicates = {index for index, count in counts.items() if count > 1}
    if duplicates:
        values = ", ".join(str(value) for value in sorted(duplicates))
        issues.append(
            AuditIssue(
                IssueSeverity.WARNING,
                "duplicate-index",
                f"Duplicate filename indexes: {values}.",
            )
        )
    if 0 in counts:
        issues.append(
            AuditIssue(
                IssueSeverity.WARNING,
                "index-range",
                "Filename indexes should start at 1; index 0 was found.",
            )
        )
    positive_indexes = sorted(index for index in counts if index > 0)
    gaps: list[int] = []
    gap_count = 0
    previous = 0
    for index in positive_indexes:
        if index > previous + 1:
            missing = index - previous - 1
            gap_count += missing
            remaining_preview = 8 - len(gaps)
            if remaining_preview > 0:
                gaps.extend(
                    range(previous + 1, previous + 1 + min(missing, remaining_preview))
                )
        previous = index
    if gap_count:
        preview = ", ".join(str(value) for value in gaps)
        suffix = "..." if gap_count > len(gaps) else ""
        issues.append(
            AuditIssue(
                IssueSeverity.WARNING,
                "index-gap",
                f"Filename index gaps: {preview}{suffix}.",
            )
        )
    return issues, duplicates


def project_audit_after_commit(
    audit: AuditResult,
    plan: ChangePlan,
    receipt: CommitReceipt,
    *,
    storage_mode: StorageMode,
    setup_completed: bool,
) -> AuditResult:


    track_map = audit.track_map()
    receipt_map = {track.track_id: track for track in receipt.tracks}
    expected = set(plan.ordered_track_ids)
    imported = {operation.track_id for operation in plan.imports}
    removed = {
        operation.track_id
        for operation in plan.presence
        if operation.action == "stash"
    }
    restored = {
        operation.track_id
        for operation in plan.presence
        if operation.action == "restore"
    }
    if (
        len(plan.ordered_track_ids) != len(expected)
        or expected != (set(track_map) - removed) | imported | restored
        or expected != set(receipt_map)
    ):
        raise ValueError("The transaction receipt does not match the active library.")

    parsed: dict[str, tuple[int | None, str]] = {}
    for track_id, state in receipt_map.items():
        parsed[track_id] = parse_filename_index(state.path)
    index_issues, duplicates = _index_issues(
        [index for index, _base in parsed.values() if index is not None]
    )

    filename_order = tuple(
        sorted(
            expected,
            key=lambda track_id: (
                parsed[track_id][0] is None,
                parsed[track_id][0] or 0,
                natural_key(receipt_map[track_id].path.name),
            ),
        )
    )

    playlist_paths = list(audit.playlists)
    playlist_order = tuple(
        track_id for track_id in audit.playlist_order if track_id in expected
    )
    playlist_positions = {
        track.track_id: track.playlist_index
        for track in audit.tracks
        if track.playlist_index is not None
    }
    selected_playlist = audit.selected_playlist
    if plan.playlist_operations:
        operation = plan.playlist_operations[-1]
        selected_playlist = operation.path
        playlist_paths = [
            path for path in playlist_paths if path.name.casefold() != operation.path.name.casefold()
        ]
        if operation.after_bytes is None:
            playlist_order = ()
            playlist_positions = {}
        else:
            playlist_paths.append(operation.path)
            playlist_order = plan.ordered_track_ids
            playlist_positions = {
                track_id: position
                for position, track_id in enumerate(playlist_order, start=1)
            }

    if storage_mode is StorageMode.FILENAMES:
        authority = OrderAuthority.FILENAMES
    elif storage_mode is StorageMode.M3U8:
        authority = OrderAuthority.PLAYLIST
    else:
        authority = plan.authority
    if authority is OrderAuthority.PLAYLIST and not playlist_order:
        authority = OrderAuthority.FILENAMES

    mismatch = bool(playlist_order) and _orders_disagree(filename_order, playlist_order)
    requires_choice = mismatch and (not setup_completed or storage_mode is StorageMode.BOTH)
    display_order = plan.ordered_track_ids
    tracks: list[TrackRecord] = []
    for track_id in plan.ordered_track_ids:
        state = receipt_map[track_id]
        filename_index, base_stem = parsed[track_id]
        playlist_index = playlist_positions.get(track_id)
        tracks.append(
            TrackRecord(
                track_id=track_id,
                path=state.path,
                original_name=state.path.name,
                base_stem=base_stem,
                suffix=state.path.suffix,
                filename_index=filename_index,
                playlist_index=playlist_index,
                unindexed=(
                    filename_index is None
                    if authority is OrderAuthority.FILENAMES
                    else playlist_index is None
                ),
                conflict=filename_index is not None and filename_index in duplicates,
                size=state.size,
                mtime_ns=state.mtime_ns,
            )
        )

    derived_codes = {"duplicate-index", "index-range", "index-gap", "authority-required"}
    if plan.playlist_operations:
        derived_codes.add("playlist-unsafe")
    issues = [issue for issue in audit.issues if issue.code not in derived_codes]
    issues.extend(index_issues)
    if requires_choice:
        issues.append(
            AuditIssue(
                IssueSeverity.BLOCKER,
                "authority-required",
                "Filename prefixes and the selected playlist disagree. Choose which order to use.",
            )
        )

    return AuditResult(
        folder=audit.folder,
        tracks=tuple(tracks),
        playlists=tuple(sorted(playlist_paths, key=lambda path: natural_key(path.name))),
        selected_playlist=selected_playlist,
        filename_order=filename_order,
        playlist_order=playlist_order,
        display_order=display_order,
        authority=authority,
        requires_authority_choice=requires_choice,
        issues=tuple(issues),
    )


def audit_folder(
    folder: Path,
    selected_playlist: Path | None = None,
    authority: OrderAuthority | None = None,
    track_ids_by_name: dict[str, str] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> AuditResult:
    if is_link_like(folder):
        raise ValueError(f"Folder does not exist: {folder}")
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")

    audio_entries: list[tuple[Path, os.stat_result]] = []
    playlist_paths: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if stop_requested and stop_requested():
                raise InterruptedError("Folder audit canceled.")
            stat_result = regular_file_stat(entry)
            if stat_result is None:
                continue
            path = folder / entry.name
            suffix = path.suffix.casefold()
            if suffix in SUPPORTED_AUDIO_EXTENSIONS:
                audio_entries.append((path, stat_result))
            elif suffix in SUPPORTED_PLAYLIST_EXTENSIONS:
                playlist_paths.append(path)
    audio_entries.sort(key=lambda item: natural_key(item[0].name))
    audio_paths = [path for path, _ in audio_entries]
    playlists = tuple(sorted(playlist_paths, key=lambda item: natural_key(item.name)))
    if selected_playlist is not None:
        selected_playlist = selected_playlist.resolve()
        if (
            selected_playlist.parent != folder
            or selected_playlist.suffix.casefold()
            not in SUPPORTED_PLAYLIST_EXTENSIONS
        ):
            raise ValueError("Playlist target must be an M3U or M3U8 file in the selected folder")
    elif len(playlists) == 1:
        selected_playlist = playlists[0]

    parsed: list[tuple[Path, int | None, str, int, int]] = []
    for path, stat_result in audio_entries:
        if stop_requested and stop_requested():
            raise InterruptedError("Folder audit canceled.")
        index, base_stem = parse_filename_index(path)
        parsed.append((path, index, base_stem, stat_result.st_size, stat_result.st_mtime_ns))

    issues, duplicates = _index_issues(
        [index for _, index, _, _, _ in parsed if index is not None]
    )

    mutable: list[_ScannedTrack] = []
    used_track_ids: set[str] = set()
    for path, index, base_stem, size, mtime_ns in parsed:
        remembered_id = (track_ids_by_name or {}).get(path.name.casefold(), "").strip()
        track_id = remembered_id if remembered_id and remembered_id not in used_track_ids else uuid.uuid4().hex
        used_track_ids.add(track_id)
        mutable.append(
            _ScannedTrack(
                track_id=track_id,
                path=path,
                original_name=path.name,
                base_stem=base_stem,
                suffix=path.suffix,
                filename_index=index,
                size=size,
                mtime_ns=mtime_ns,
            )
        )
    by_name = {item.original_name.casefold(): item for item in mutable}

    indexed = sorted(
        (item for item in mutable if item.filename_index is not None),
        key=lambda item: (item.filename_index or 0, natural_key(item.original_name)),
    )
    unindexed = sorted(
        (item for item in mutable if item.filename_index is None),
        key=lambda item: natural_key(item.original_name),
    )
    filename_order = tuple(item.track_id for item in (*indexed, *unindexed))

    playlist_order_items: list[_ScannedTrack] = []
    if selected_playlist is not None and selected_playlist.exists():
        names = {path.name.casefold(): path.name for path in audio_paths}
        document = parse_playlist(selected_playlist, folder, names)
        for message in document.errors:
            issues.append(AuditIssue(IssueSeverity.BLOCKER, "playlist-unsafe", message))
        seen: set[str] = set()
        for position, name in enumerate(document.track_names, start=1):
            item = by_name.get(name.casefold())
            if item is not None:
                item.playlist_index = position
                playlist_order_items.append(item)
                seen.add(item.track_id)
        playlist_order_items.extend(
            sorted(
                (item for item in mutable if item.track_id not in seen),
                key=lambda item: natural_key(item.original_name),
            )
        )
    playlist_order = tuple(item.track_id for item in playlist_order_items)

    mismatch = bool(playlist_order) and _orders_disagree(filename_order, playlist_order)
    if mismatch:
        issues.append(
            AuditIssue(
                IssueSeverity.BLOCKER,
                "authority-required",
                "Filename prefixes and the selected playlist disagree. Choose which order to use.",
            )
        )

    chosen = authority
    if chosen is None:
        chosen = OrderAuthority.PLAYLIST if playlist_order and not mismatch else OrderAuthority.FILENAMES
    if chosen is OrderAuthority.PLAYLIST and not playlist_order:
        chosen = OrderAuthority.FILENAMES
    display_order = playlist_order if chosen is OrderAuthority.PLAYLIST else filename_order

    playlist_ids = {item.track_id for item in playlist_order_items if item.playlist_index is not None}
    tracks: list[TrackRecord] = []
    for item in mutable:
        filename_index = item.filename_index
        track_id = item.track_id
        unindexed_state = filename_index is None if chosen is OrderAuthority.FILENAMES else track_id not in playlist_ids
        tracks.append(
            TrackRecord(
                track_id=track_id,
                path=item.path,
                original_name=item.original_name,
                base_stem=item.base_stem,
                suffix=item.suffix,
                filename_index=filename_index,
                playlist_index=item.playlist_index,
                unindexed=unindexed_state,
                conflict=filename_index is not None and filename_index in duplicates,
                size=item.size,
                mtime_ns=item.mtime_ns,
            )
        )

    return AuditResult(
        folder=folder,
        tracks=tuple(tracks),
        playlists=playlists,
        selected_playlist=selected_playlist,
        filename_order=filename_order,
        playlist_order=playlist_order,
        display_order=display_order,
        authority=chosen,
        requires_authority_choice=mismatch and authority is None,
        issues=tuple(issues),
    )
