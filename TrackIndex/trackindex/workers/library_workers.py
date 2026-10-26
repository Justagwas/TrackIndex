from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from trackindex.core.artwork_service import ArtworkStore, PreparedArtwork
from trackindex.core.config import default_playlist_name
from trackindex.core.diagnostics import diagnostics_logger
from trackindex.core.library_service import canonical_playlist_for, discover_library_folders
from trackindex.core.metadata_service import (
    folder_artwork,
    prepare_metadata_backend,
    read_track_metadata,
)
from trackindex.core.models import (
    ApplyResult, AuditResult, ChangePlan, OrderAuthority,
    SessionCommand, SessionRecoveryRecord, TrackMetadata,
)
from trackindex.core.planner import build_session_change_plan
from trackindex.core.scanner import audit_folder, parse_filename_index
from trackindex.core.threading_contract import assert_qt_thread_affinity
from trackindex.core.transaction import apply_change_plan, recover_journal
from trackindex.core.session_service import SessionManager

_LOG = diagnostics_logger("workers.library")


class WorkerSignals(QObject):
    metadataReady = Signal(object)
    auditReady = Signal(object)
    transactionDone = Signal(object)
    planReady = Signal(object)
    acceptanceDone = Signal(object)
    recoveryDone = Signal(object)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    task_id: str
    results: tuple[ApplyResult, ...]


class RecoveryTask(QRunnable):
    def __init__(self, journals: tuple[Path, ...], filesystem_access) -> None:
        super().__init__()
        self.journals = journals
        self.filesystem_access = filesystem_access
        self.task_id = uuid.uuid4().hex
        self.signals = WorkerSignals()
        self.result: RecoveryResult | None = None

    @Slot()
    def run(self) -> None:
        results: list[ApplyResult] = []
        try:
            with self.filesystem_access():
                for path in self.journals:
                    try:
                        result = recover_journal(path)
                    except Exception as exc:
                        _LOG.exception("Interrupted operation recovery failed for one journal")
                        result = ApplyResult(False, str(exc), recovery_journal=path)
                    results.append(result)
        except Exception as exc:
            _LOG.exception("Interrupted operation recovery could not acquire or release filesystem access")
            results.extend(
                ApplyResult(False, str(exc), recovery_journal=path)
                for path in self.journals[len(results):]
            )
        self.result = RecoveryResult(self.task_id, tuple(results))
        self.signals.recoveryDone.emit(self.result)


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    task_id: str
    accepted: tuple[str, ...]
    requested: tuple[str, ...]
    error: str = ""
    restored_commands: tuple[SessionCommand, ...] = ()
    finalized: bool = False


class AcceptanceTask(QRunnable):
    def __init__(self, root: Path, library_ids: tuple[str, ...], filesystem_access=None,
                 restore_records: tuple[SessionRecoveryRecord, ...] = (), *, finalize: bool = False) -> None:
        super().__init__()
        self.root, self.library_ids = root, tuple(dict.fromkeys(library_ids))
        self.filesystem_access = filesystem_access
        self.task_id = uuid.uuid4().hex
        self.signals = WorkerSignals()
        self.result: AcceptanceResult | None = None
        self.restore_records = restore_records
        self.finalize = finalize
        self._stop_event = Event()

    def stop(self) -> None:
        self._stop_event.set()

    @Slot()
    def run(self) -> None:
        accepted, error = (), ""
        restored = []
        try:
            context = self.filesystem_access() if self.filesystem_access is not None else nullcontext()
            with context:
                if self._stop_event.is_set():
                    raise InterruptedError("Acceptance canceled; recovery data retained.")
                for record in self.restore_records:
                    for command in reversed(record.commands):
                        if self._stop_event.is_set():
                            raise InterruptedError("Restoration canceled; recovery data retained.")
                        ids = {name.casefold(): track_id for track_id, name in command.after_names}
                        audit = audit_folder(record.folder, command.playlist_path, OrderAuthority.FILENAMES, ids)
                        plan = build_session_change_plan(audit, command, to_after=False)
                        result = apply_change_plan(plan)
                        if not result.success:
                            raise OSError(result.message + " " + " ".join(result.details))
                        restored.append(command)
                accepted = SessionManager(self.root).accept_libraries(self.library_ids, self._stop_event.is_set, finalize=self.finalize)
        except Exception as exc:
            _LOG.exception("Session acceptance failed")
            error = str(exc)
        self.result = AcceptanceResult(self.task_id, accepted, self.library_ids, error, tuple(restored), self.finalize)
        self.signals.acceptanceDone.emit(self.result)


@dataclass(frozen=True, slots=True)
class AuditWorkerResult:
    generation: int
    audit: AuditResult | None
    artwork: bytes | PreparedArtwork | None
    error: str = ""
    task_id: str = ""


@dataclass(frozen=True, slots=True)
class MetadataWorkerResult:
    generation: int
    track_id: str
    metadata: TrackMetadata
    task_id: str


@dataclass(frozen=True, slots=True)
class PlanWorkerResult:
    task_id: str
    plan: ChangePlan | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class TransactionWorkerResult:
    task_id: str
    result: ApplyResult


class AuditTask(QRunnable):
    def __init__(
        self,
        generation: int,
        folder: Path,
        playlist,
        authority,
        ids_by_name,
        *,
        include_artwork: bool = True,
        artwork_store: ArtworkStore | None = None,
        filesystem_access=None,
        resolve_canonical: bool = False,
        remembered_playlist: str = "",
    ) -> None:
        super().__init__()
        self.task_id = uuid.uuid4().hex
        self.generation = generation
        self.folder = folder
        self.playlist = playlist
        self.authority = authority
        self.ids_by_name = ids_by_name
        self.include_artwork = include_artwork
        self.artwork_store = artwork_store
        self.filesystem_access = filesystem_access
        self.resolve_canonical = resolve_canonical
        self.remembered_playlist = remembered_playlist
        self._stop_event = Event()
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            context = self.filesystem_access() if self.filesystem_access is not None else nullcontext()
            with context:
                if self.resolve_canonical:
                    canonical, playlists = canonical_playlist_for(
                        self.folder,
                        self.remembered_playlist,
                    )
                    self.playlist = canonical or (
                        playlists[0]
                        if playlists
                        else self.folder / default_playlist_name(self.folder)
                    )
                result = audit_folder(
                    self.folder,
                    self.playlist,
                    self.authority,
                    self.ids_by_name,
                    self._stop_event.is_set,
                )
                raw_artwork = (
                    folder_artwork(self.folder) if self.include_artwork else None
                )
                artwork: bytes | PreparedArtwork | None = raw_artwork
                if raw_artwork is not None and self.artwork_store is not None:
                    artwork = self.artwork_store.prepare(
                        raw_artwork,
                        self._stop_event.is_set,
                    )
        except InterruptedError:
            return
        except Exception as exc:
            _LOG.exception("Library audit failed")
            self.signals.auditReady.emit(
                AuditWorkerResult(self.generation, None, None, str(exc), self.task_id)
            )
            return
        if not self._stop_event.is_set():
            self.signals.auditReady.emit(
                AuditWorkerResult(self.generation, result, artwork, task_id=self.task_id)
            )

    def stop(self) -> None:
        self._stop_event.set()


class MetadataTask(QRunnable):
    def __init__(
        self,
        generation: int,
        track_id: str,
        path: Path,
        size: int,
        mtime_ns: int,
        fallback_artwork: bytes | PreparedArtwork | None,
        artwork_store: ArtworkStore | None = None,
        filesystem_access=None,
        *,
        discover_folder_artwork: bool = False,
    ) -> None:
        super().__init__()
        self.task_id = uuid.uuid4().hex
        self.generation, self.track_id, self.path = generation, track_id, path
        self.size, self.mtime_ns = size, mtime_ns
        self.fallback_artwork = fallback_artwork
        self.artwork_store = artwork_store
        self.filesystem_access = filesystem_access
        self.discover_folder_artwork = discover_folder_artwork
        self._stop_event = Event()
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            backend = prepare_metadata_backend(self.path.suffix)
            if self._stop_event.is_set():
                return
            context = self.filesystem_access() if self.filesystem_access is not None else nullcontext()
            with context:
                if self._stop_event.is_set():
                    return
                value: TrackMetadata = read_track_metadata(self.path, None, backend)
                if self.discover_folder_artwork and not value.artwork_bytes:
                    if self._stop_event.is_set():
                        return
                    raw_artwork = folder_artwork(self.path.parent)
                    self.fallback_artwork = (
                        self.artwork_store.prepare(raw_artwork, self._stop_event.is_set)
                        if raw_artwork and self.artwork_store is not None
                        else raw_artwork
                    )
                if self.artwork_store is not None and value.artwork_bytes:
                    prepared = self.artwork_store.prepare(
                        value.artwork_bytes,
                        self._stop_event.is_set,
                    )
                    value = replace(
                        value,
                        artwork_bytes=(
                            prepared.thumbnail_bytes if prepared is not None else None
                        ),
                        artwork_key=prepared.key if prepared is not None else "",
                    )
                elif isinstance(self.fallback_artwork, PreparedArtwork):
                    value = replace(
                        value,
                        artwork_bytes=self.fallback_artwork.thumbnail_bytes,
                        artwork_key=self.fallback_artwork.key,
                    )
                elif self.fallback_artwork:
                    prepared = (
                        self.artwork_store.prepare(
                            self.fallback_artwork,
                            self._stop_event.is_set,
                        )
                        if self.artwork_store is not None
                        else None
                    )
                    value = replace(
                        value,
                        artwork_bytes=(
                            prepared.thumbnail_bytes
                            if prepared is not None
                            else self.fallback_artwork
                        ),
                        artwork_key=prepared.key if prepared is not None else "",
                    )
        except Exception as exc:
            _LOG.exception("Track metadata extraction failed")
            _, title = parse_filename_index(self.path)
            fallback = self.fallback_artwork
            value = TrackMetadata(
                title,
                artwork_bytes=(
                    fallback.thumbnail_bytes
                    if isinstance(fallback, PreparedArtwork)
                    else fallback
                ),
                artwork_key=(
                    fallback.key if isinstance(fallback, PreparedArtwork) else ""
                ),
                error=str(exc),
            )
        if not self._stop_event.is_set():
            self.signals.metadataReady.emit(
                MetadataWorkerResult(self.generation, self.track_id, value, self.task_id)
            )

    def stop(self) -> None:
        self._stop_event.set()


class PlanTask(QRunnable):
    def __init__(self, builder: Callable[[], ChangePlan], filesystem_access=None) -> None:
        super().__init__()
        self.builder = builder
        self.filesystem_access = filesystem_access
        self.task_id = uuid.uuid4().hex
        self.signals = WorkerSignals()
        self._stop_event = Event()

    def stop(self) -> None:
        self._stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            context = self.filesystem_access() if self.filesystem_access is not None else nullcontext()
            with context:
                plan = None if self._stop_event.is_set() else self.builder()
        except Exception as exc:
            _LOG.exception("Change planning failed")
            self.signals.planReady.emit(
                PlanWorkerResult(self.task_id, None, str(exc))
            )
            return
        self.signals.planReady.emit(PlanWorkerResult(self.task_id, None if self._stop_event.is_set() else plan))


class TransactionTask(QRunnable):
    def __init__(
        self,
        plan: ChangePlan,
        filesystem_access=None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.plan = plan
        self.filesystem_access = filesystem_access
        self.prepare = prepare
        self.task_id = uuid.uuid4().hex
        self.result: ApplyResult | None = None
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        if self.prepare is not None:
            try:
                self.prepare()
            except Exception as exc:
                _LOG.exception("Session recovery preparation failed")
                result = ApplyResult(
                    False,
                    "TrackIndex could not create its recovery record, so nothing was changed.",
                    details=(str(exc),),
                )
                self.result = result
                self.signals.transactionDone.emit(
                    TransactionWorkerResult(self.task_id, result)
                )
                return
        try:
            context = self.filesystem_access() if self.filesystem_access is not None else nullcontext()
            with context:
                result = apply_change_plan(self.plan)
        except Exception as exc:
            _LOG.exception("Filesystem transaction failed outside its recovery boundary")
            result = ApplyResult(
                False,
                "TrackIndex could not complete the filesystem transaction.",
                details=(str(exc),),
            )
        self.result = result
        self.signals.transactionDone.emit(
            TransactionWorkerResult(self.task_id, result)
        )


class DiscoveryWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._stop_event = Event()

    @Slot()
    def run(self) -> None:
        assert_qt_thread_affinity(self)
        try:
            result = discover_library_folders(self.root, self._stop_event.is_set)
            if not self._stop_event.is_set():
                self.completed.emit(result)
        except Exception as exc:
            if not self._stop_event.is_set():
                _LOG.exception("Library discovery failed")
                self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def stop(self) -> None:
        self._stop_event.set()
