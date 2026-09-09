from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class StorageMode(StrEnum):
    FILENAMES = "filenames"
    M3U8 = "m3u8"
    BOTH = "both"


class OrderAuthority(StrEnum):
    FILENAMES = "filenames"
    PLAYLIST = "playlist"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class LibraryRecord:
    library_id: str
    folder: Path
    canonical_playlist: str = ""
    storage_mode: str = StorageMode.BOTH.value
    setup_completed: bool = False

    @property
    def name(self) -> str:
        return self.folder.name or str(self.folder)


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    title: str
    artist: str = ""
    album: str = ""
    duration_seconds: float | None = None
    artwork_bytes: bytes | None = None
    error: str = ""
    artwork_key: str = ""
    container: str = field(default="", compare=False)
    codec: str = field(default="", compare=False)
    bitrate: int | None = field(default=None, compare=False)
    sample_rate: int | None = field(default=None, compare=False)
    bit_depth: int | None = field(default=None, compare=False)
    channels: int | None = field(default=None, compare=False)
    channel_layout: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class AuditIssue:
    severity: IssueSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class TrackRecord:
    track_id: str
    path: Path
    original_name: str
    base_stem: str
    suffix: str
    filename_index: int | None
    playlist_index: int | None
    unindexed: bool
    conflict: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class AuditResult:
    folder: Path
    tracks: tuple[TrackRecord, ...]
    playlists: tuple[Path, ...]
    selected_playlist: Path | None
    filename_order: tuple[str, ...]
    playlist_order: tuple[str, ...]
    display_order: tuple[str, ...]
    authority: OrderAuthority
    requires_authority_choice: bool
    issues: tuple[AuditIssue, ...] = ()

    def track_map(self) -> dict[str, TrackRecord]:
        return {track.track_id: track for track in self.tracks}


@dataclass(frozen=True, slots=True)
class RenameOperation:
    track_id: str
    source: Path
    target: Path


@dataclass(frozen=True, slots=True)
class ImportOperation:
    track_id: str
    source: Path
    target: Path
    size: int
    mtime_ns: int
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class PresenceOperation:


    track_id: str
    source: Path
    target: Path
    size: int
    mtime_ns: int
    action: str
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class SessionPresence:


    track_id: str
    recovery_name: str
    size: int
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class PlaylistOperation:
    path: Path
    action: str
    before_bytes: bytes | None
    after_bytes: bytes | None
    expected_sha256: str | None


@dataclass(frozen=True, slots=True)
class SessionCommand:
    command_id: str
    library_id: str
    folder: Path
    track_ids: tuple[str, ...]
    before_order: tuple[str, ...]
    after_order: tuple[str, ...]
    before_names: tuple[tuple[str, str], ...]
    after_names: tuple[tuple[str, str], ...]
    playlist_path: Path | None
    before_playlist_bytes: bytes | None
    after_playlist_bytes: bytes | None
    playlist_created: bool
    before_storage_mode: str = StorageMode.BOTH.value
    after_storage_mode: str = StorageMode.BOTH.value
    before_setup_completed: bool = True
    after_setup_completed: bool = True
    before_canonical_playlist: str = ""
    after_canonical_playlist: str = ""
    presence: tuple[SessionPresence, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionRecoveryRecord:
    library_id: str
    folder: Path
    commands: tuple[SessionCommand, ...]
    retained_commands: tuple[SessionCommand, ...] = ()
    unresolved_commands: tuple[SessionCommand, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionRecoveryResult:
    success: bool
    message: str
    restored_commands: int = 0
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangePlan:
    plan_id: str
    folder: Path
    storage_mode: StorageMode
    authority: OrderAuthority
    ordered_track_ids: tuple[str, ...]
    renames: tuple[RenameOperation, ...]
    playlist_operations: tuple[PlaylistOperation, ...]
    snapshots: tuple[FileSnapshot, ...]
    filename_preview: tuple[tuple[str, str], ...]
    folder_inventory: tuple[str, ...]
    imports: tuple[ImportOperation, ...] = ()
    presence: tuple[PresenceOperation, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def can_apply(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class CommittedTrackState:
    track_id: str
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class CommittedPlaylistState:
    path: Path
    exists: bool
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class CommitReceipt:


    tracks: tuple[CommittedTrackState, ...]
    playlists: tuple[CommittedPlaylistState, ...]
    folder_inventory: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    success: bool
    message: str
    renamed_count: int = 0
    playlists_written: int = 0
    recovery_journal: Path | None = None
    details: tuple[str, ...] = field(default_factory=tuple)
    receipt: CommitReceipt | None = None
