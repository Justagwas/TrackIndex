from __future__ import annotations

import sys
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QProcess, QThread, QThreadPool, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox

from trackindex.controller.coordination_services import (
    LibraryCoordinationService,
    LibrarySnapshot,
    LibraryWorkToken,
    MutationCoordinationService,
    PlanOperationContext,
    TransactionOperationContext,
    UpdateCoordinationService,
)
from trackindex.core import self_updater
from trackindex.core.artwork_service import ArtworkStore, PreparedArtwork
from trackindex.core.concurrency import ReadWriteLock
from trackindex.core.config import APP_VERSION, DOWNLOAD_PAGE_URL, default_playlist_name
from trackindex.core.config_service import AppConfig, default_config, save_config
from trackindex.core.diagnostics import diagnostics_logger, register_private_path
from trackindex.core.library_service import (
    create_library,
    normalize_folder,
)
from trackindex.core.metadata_service import MetadataCache
from trackindex.core.models import (
    ApplyResult,
    AuditResult,
    ChangePlan,
    LibraryRecord,
    OrderAuthority,
    SessionCommand,
    SessionPresence,
    SessionRecoveryRecord,
    StorageMode,
    TrackMetadata,
    TrackRecord,
)
from trackindex.core.planner import (
    build_change_plan,
    build_delete_plan,
    build_import_plan,
    build_prefix_removal_plan,
    build_session_change_plan,
)
from trackindex.core.scanner import audit_folder, project_audit_after_commit
from trackindex.core.session_service import SessionManager
from trackindex.core.threading_contract import assert_qt_thread_affinity
from trackindex.core.transaction import (
    apply_change_plan,
    pending_journals,
    recover_journal,
)
from trackindex.ui.dialogs import (
    DeleteTracksDialog,
    LibraryImportDialog,
    UpdateProgressDialog,
    ask_update_handoff,
    ask_update_install_preference,
    styled_message,
)
from trackindex.ui.main_window import MainWindow
from trackindex.ui.theme import get_theme
from trackindex.workers.library_workers import (
    AuditTask,
    AuditWorkerResult,
    DiscoveryWorker,
    MetadataTask,
    MetadataWorkerResult,
    PlanTask,
    PlanWorkerResult,
    TransactionTask,
    TransactionWorkerResult,
)
from trackindex.workers.update_workers import UpdateCheckWorker, UpdateInstallWorker

_LOG = diagnostics_logger("controller")


class _PlaybackCoordinator(Protocol):
    def prepare_filesystem_mutation(
        self,
        library_id: str,
        plan: ChangePlan,
        ready: Callable[[], None],
        failed: Callable[[str], None] | None = None,
    ) -> None: ...

    def complete_filesystem_mutation(self, library_id: str, plan: ChangePlan, result: ApplyResult) -> None: ...

    def library_removed(self, library_id: str) -> None: ...

    def reset_preferences(self) -> None: ...


class AppController(QObject):
    _SHORT_SHUTDOWN_WAIT_MS = 5000
    _TRANSACTION_SHUTDOWN_WAIT_MS = 30000
    _UPDATE_SHUTDOWN_WAIT_MS = 30000

    def __init__(self, app: QApplication, window: MainWindow, config: AppConfig) -> None:
        super().__init__(window)
        self.app, self.window, self.config = app, window, config
        register_private_path(config.music_root)
        for library in config.libraries:
            register_private_path(library.folder)
        self.audit: AuditResult | None = None
        self.current_library: LibraryRecord | None = None
        self.selected_authority: OrderAuthority | None = None
        self.metadata_cache = MetadataCache()
        self.artwork_store = ArtworkStore()
        self._libraries = LibraryCoordinationService(12)
        self._filesystem_gate = ReadWriteLock()
        self._metadata_paused = False
        self.generation = 0
        self.metadata_pool = QThreadPool(window)
        self.metadata_pool.setMaxThreadCount(2)
        self.audit_pool = QThreadPool(window)
        self.audit_pool.setMaxThreadCount(1)
        self.transaction_pool = QThreadPool(window)
        self.transaction_pool.setMaxThreadCount(1)
        self.prefetch_pool = QThreadPool(window)
        self.prefetch_pool.setMaxThreadCount(1)
        self.sessions = SessionManager()
        self._transaction_task: TransactionTask | None = None
        self._transaction_preparing = False
        self._playback_coordinator: _PlaybackCoordinator | None = None
        self._plan_task: PlanTask | None = None
        self._mutations = MutationCoordinationService()
        self._audit_task: AuditTask | None = None
        self._audit_token: LibraryWorkToken | None = None
        self._reconcile_task: AuditTask | None = None
        self._reconcile_token: LibraryWorkToken | None = None
        self._prefetch_tasks: dict[int, tuple[LibraryWorkToken, AuditTask]] = {}
        self._next_prefetch_token = -1
        self._folder_artwork: bytes | PreparedArtwork | None = None
        self._metadata_tasks: dict[tuple[int, str], MetadataTask] = {}
        self._metadata_queue: deque[str] = deque()
        self._metadata_pending: dict[
            str, tuple[int, TrackRecord, bytes | PreparedArtwork | None]
        ] = {}
        self._metadata_candidates: dict[
            str, tuple[int, TrackRecord, bytes | PreparedArtwork | None]
        ] = {}
        self._pending_metadata: dict[str, TrackMetadata] = {}
        self._metadata_flush_timer = QTimer(window)
        self._metadata_flush_timer.setSingleShot(True)
        self._metadata_flush_timer.setInterval(16)
        self._metadata_flush_timer.timeout.connect(self._flush_metadata)
        self._reconcile_timer = QTimer(window)
        self._reconcile_timer.setSingleShot(True)
        self._reconcile_timer.setInterval(1000)
        self._reconcile_timer.timeout.connect(self._start_silent_reconciliation)
        self._discovery_thread: QThread | None = None
        self._discovery_worker: DiscoveryWorker | None = None
        self._warned_other_playlists: set[str] = set()
        self._update_thread: QThread | None = None
        self._update_worker: UpdateCheckWorker | UpdateInstallWorker | None = None
        self._updates = UpdateCoordinationService()
        self._progress_dialog: UpdateProgressDialog | None = None
        self._setup_active = False
        self._setup_reconfiguring = False
        self._setup_mode = StorageMode.BOTH
        self._setup_playlist_name = ""
        self._setup_source_playlist: Path | None = None
        self._setup_plan: ChangePlan | None = None
        self._reconfigure_after_load = False
        self._shutdown_started = False
        self._connect()
        self._refresh_libraries()
        self.app.aboutToQuit.connect(self.shutdown)
        QTimer.singleShot(0, self._startup)
        if self.config.auto_check_updates:
            QTimer.singleShot(1200, lambda: self.check_for_updates(manual=False))

    def _connect(self) -> None:
        w = self.window
        w.openFolderRequested.connect(self.open_folder)
        w.createPlaylistRequested.connect(self.create_playlist)
        w.librarySelected.connect(self.select_library)
        w.libraryRescanRequested.connect(self._rescan_library)
        w.libraryExplorerRequested.connect(self.open_in_explorer)
        w.currentLibraryExplorerRequested.connect(self.open_current_library)
        w.libraryRemoveRequested.connect(self.remove_library)
        w.libraryOrderChanged.connect(self.reorder_libraries)
        w.authoritySelected.connect(self.select_authority)
        w.track_table.tracksDropped.connect(self.commit_reorder)
        w.track_table.externalFilesDropped.connect(self.import_external_files)
        w.track_table.deleteTracksRequested.connect(self.delete_tracks)
        w.track_table.visibleRowsChanged.connect(self._metadata_viewport_changed)
        w.undoRequested.connect(self.undo)
        w.redoRequested.connect(self.redo)
        w.themeModeChanged.connect(self.set_theme_mode)
        w.pinChanged.connect(self._set_pinned)
        w.uiScaleChanged.connect(self.set_ui_scale)
        w.autoUpdatesChanged.connect(self._set_auto_updates)
        w.confirmTrackDeletionChanged.connect(self._set_confirm_track_deletion)
        w.checkUpdatesRequested.connect(lambda: self.check_for_updates(manual=True))
        w.resetSettingsRequested.connect(self.reset_settings)
        w.musicRootRequested.connect(self.change_music_root)
        w.scanLibrariesRequested.connect(lambda: self.scan_for_libraries(manual=True))
        w.setupSelectionChanged.connect(self._setup_selection_changed)
        w.setupPlaylistChanged.connect(self._setup_playlist_changed)
        w.setupSubmitRequested.connect(self._apply_setup)
        w.setupCancelRequested.connect(self._cancel_setup)
        w.orderingMethodRequested.connect(self.change_ordering_method)
        w.prefixRemovalRequested.connect(self.remove_filename_prefixes)

    def _startup(self) -> None:
        self._recover_pending_operations()
        self._recover_sessions()
        if not self.config.initial_discovery_completed:
            self.scan_for_libraries(manual=False)
        else:
            self._start_snapshot_prefetch()

    def _library(self, library_id: str) -> LibraryRecord | None:
        return next((item for item in self.config.libraries if item.library_id == library_id), None)

    def library(self, library_id: str) -> LibraryRecord | None:
        return self._library(library_id)

    def library_loading(self) -> bool:
        return self._audit_task is not None

    @staticmethod
    def _snapshot_record_key(record: LibraryRecord) -> tuple[str, str, str, bool]:
        return LibraryCoordinationService.record_key(record)

    def _capture_current_snapshot(self) -> None:
        record = self.current_library
        audit = self.audit
        if record is None or audit is None:
            return
        if str(audit.folder).casefold() != str(record.folder).casefold():
            return
        self._flush_metadata()
        self._libraries.store(
            record,
            audit,
            dict(self.window.track_table.track_model.metadata),
            self.selected_authority,
            self.window.track_table.verticalScrollBar().value(),
        )

    def _snapshot_for(self, record: LibraryRecord) -> LibrarySnapshot | None:
        return self._libraries.snapshot_for(record)

    def _restore_snapshot(self, snapshot: LibrarySnapshot, generation: int) -> None:
        record = self.current_library
        if record is None:
            return
        self.audit = snapshot.audit
        if not self._setup_active or self.selected_authority is None:
            self.selected_authority = snapshot.selected_authority
        self.window.set_audit(snapshot.audit, record)
        if snapshot.metadata:
            self.window.track_table.track_model.set_metadata_batch(snapshot.metadata)
        if self._setup_active:
            self._refresh_setup_view()
        self.window.set_library_validating(True)

        def restore_scroll() -> None:
            if generation == self.generation:
                self.window.track_table.verticalScrollBar().setValue(
                    snapshot.scroll_value
                )

        QTimer.singleShot(0, restore_scroll)

    def _operation_busy(self) -> bool:
        return self._plan_task is not None or self._transaction_task is not None or self._transaction_preparing

    def set_playback_coordinator(self, coordinator: _PlaybackCoordinator) -> None:
        self._playback_coordinator = coordinator

    def _start_snapshot_prefetch(self) -> None:
        if self._shutdown_started:
            return
        active_ids = {
            token.library_id for token, _task in self._prefetch_tasks.values()
        }
        available_slots = self._libraries.available_prefetch_slots(active_ids)
        if not available_slots:
            return
        records = list(self.config.libraries)
        last_id = self.config.last_library_id
        records.sort(key=lambda record: record.library_id != last_id)
        for record in records:
            if available_slots <= 0:
                break
            if (
                not self._libraries.should_prefetch(record, active_ids)
            ):
                continue
            self._libraries.mark_prefetch_attempted(record.library_id)
            available_slots -= 1
            mode = StorageMode(record.storage_mode)
            authority = (
                OrderAuthority.FILENAMES
                if mode is StorageMode.FILENAMES
                else (
                    OrderAuthority.PLAYLIST
                    if mode is StorageMode.M3U8
                    else None
                )
            )
            canonical = record.folder / (
                record.canonical_playlist or default_playlist_name(record.folder)
            )
            token = self._next_prefetch_token
            self._next_prefetch_token -= 1
            task = AuditTask(
                token,
                record.folder,
                canonical,
                authority,
                None,
                include_artwork=False,
                filesystem_access=self._filesystem_gate.read,
                resolve_canonical=True,
                remembered_playlist=record.canonical_playlist,
            )
            task.signals.auditReady.connect(self._snapshot_prefetch_ready)
            self._prefetch_tasks[token] = (
                LibraryWorkToken(token, record.library_id),
                task,
            )
            self.prefetch_pool.start(task)

    @Slot(object)
    def _snapshot_prefetch_ready(
        self,
        event: AuditWorkerResult,
    ) -> None:
        assert_qt_thread_affinity(self)
        entry = self._prefetch_tasks.pop(event.generation, None)
        if (
            entry is None
            or event.error
            or event.audit is None
            or self._shutdown_started
        ):
            return
        work_token, _task = entry
        library_id = work_token.library_id
        record = self._library(library_id)
        if record is None or self._snapshot_for(record) is not None:
            return
        mode = StorageMode(record.storage_mode)
        authority = (
            OrderAuthority.FILENAMES
            if mode is StorageMode.FILENAMES
            else (
                OrderAuthority.PLAYLIST
                if mode is StorageMode.M3U8
                else None
            )
        )
        self._libraries.store(
            record,
            event.audit,
            {},
            authority,
        )

    def _refresh_libraries(self, selected_id: str | None = None) -> None:
        if selected_id is None:
            selected_id = (
                self.current_library.library_id
                if self.current_library is not None
                else ""
            )
        self.window.set_libraries(self.config.libraries, selected_id)

    def reorder_libraries(self, ordered_ids: object) -> None:

        if not isinstance(ordered_ids, (tuple, list)):
            self._refresh_libraries()
            return
        ids = tuple(str(item) for item in ordered_ids)
        records = {record.library_id: record for record in self.config.libraries}
        if (
            len(ids) != len(records)
            or len(set(ids)) != len(ids)
            or set(ids) != set(records)
        ):
            self._refresh_libraries()
            return
        self.config.libraries = [records[library_id] for library_id in ids]
        save_config(self.config)

    def _add_library(self, folder: Path, *, select: bool = True) -> LibraryRecord:
        normalized = normalize_folder(folder)
        register_private_path(normalized)
        existing = next((item for item in self.config.libraries if str(normalize_folder(item.folder)).casefold() == str(normalized).casefold()), None)
        if existing:
            record = existing
            if str(existing.folder) != str(normalized):
                record = LibraryRecord(
                    existing.library_id,
                    normalized,
                    existing.canonical_playlist,
                    existing.storage_mode,
                    existing.setup_completed,
                )
                self.config.libraries[self.config.libraries.index(existing)] = record
        else:
            record = LibraryRecord(uuid.uuid4().hex, normalized)
            self.config.libraries.append(record)
        if select:
            self.config.last_library_id = record.library_id
        save_config(self.config)
        self._refresh_libraries(record.library_id if select else None)
        if select:
            self.select_library(record.library_id)
        return record

    def open_folder(self) -> None:
        start = self.current_library.folder if self.current_library else Path(self.config.music_root)
        selected = QFileDialog.getExistingDirectory(self.window, "Open music folder", str(start))
        if selected:
            self._add_library(Path(selected))

    def create_playlist(self) -> None:
        name, accepted = QInputDialog.getText(self.window, "Create playlist", "Playlist name:")
        if not accepted:
            return
        try:
            record = create_library(Path(self.config.music_root), name)
        except Exception as exc:
            styled_message(self.window, QMessageBox.Icon.Warning, "Could not create playlist", str(exc)).exec()
            return
        self.config.libraries.append(record)
        self.config.last_library_id = record.library_id
        save_config(self.config)
        self._refresh_libraries(record.library_id)
        self.select_library(record.library_id)

    def select_library(self, library_id: str) -> None:
        if self._operation_busy():
            return
        record = self._library(library_id)
        if record is None:
            return
        for token, (work_token, task) in tuple(self._prefetch_tasks.items()):
            if work_token.library_id == library_id:
                task.stop()
                self._prefetch_tasks.pop(token, None)
        self._capture_current_snapshot()
        self._invalidate_library_work()
        self.current_library = record
        selection_changed = self.config.last_library_id != library_id
        self.config.last_library_id = library_id
        if selection_changed:
            save_config(self.config)
        self.window.library_list.select_library(library_id)
        canonical = record.folder / (
            record.canonical_playlist or default_playlist_name(record.folder)
        )
        self._setup_active = not record.setup_completed
        self._setup_reconfiguring = False
        self._setup_mode = StorageMode.BOTH if self._setup_active else StorageMode(record.storage_mode)
        self._setup_source_playlist = None
        self._setup_playlist_name = (
            canonical.with_suffix(".m3u8").name
            if self._setup_active and canonical.suffix.casefold() == ".m3u"
            else canonical.name
        )
        self._setup_plan = None
        if record.setup_completed and self._setup_mode is StorageMode.FILENAMES:
            self.selected_authority = OrderAuthority.FILENAMES
        elif record.setup_completed and self._setup_mode is StorageMode.M3U8:
            self.selected_authority = OrderAuthority.PLAYLIST
        else:
            self.selected_authority = None
        self._load_current(canonical, resolve_canonical=True)

    def _replace_library(self, replacement: LibraryRecord) -> None:
        self._libraries.invalidate_if_changed(replacement)
        self.config.libraries = [replacement if item.library_id == replacement.library_id else item for item in self.config.libraries]
        save_config(self.config)
        self._refresh_libraries(replacement.library_id)

    def _load_current(
        self,
        playlist: Path | None = None,
        ids_by_name: dict[str, str] | None = None,
        *,
        resolve_canonical: bool = False,
    ) -> None:
        if not self.current_library:
            return
        playlist_name = (
            self.current_library.canonical_playlist or self._setup_playlist_name or
            default_playlist_name(self.current_library.folder)
        )
        target = playlist or self.current_library.folder / playlist_name
        generation = self._invalidate_library_work()
        snapshot = self._snapshot_for(self.current_library)
        remembered_ids = ids_by_name
        if snapshot is not None:
            self._restore_snapshot(snapshot, generation)
            if remembered_ids is None:
                remembered_ids = {
                    track.original_name.casefold(): track.track_id
                    for track in snapshot.audit.tracks
                }
        else:
            self.window.set_library_loading(self.current_library.folder)
        task = AuditTask(
            generation,
            self.current_library.folder,
            target,
            self.selected_authority,
            remembered_ids,
            artwork_store=self.artwork_store,
            filesystem_access=self._filesystem_gate.read,
            resolve_canonical=resolve_canonical,
            remembered_playlist=self.current_library.canonical_playlist,
        )
        task.signals.auditReady.connect(self._audit_ready)
        self._audit_task = task
        self._audit_token = LibraryWorkToken(
            generation,
            self.current_library.library_id,
        )
        self.audit_pool.start(task)

    def _invalidate_library_work(self) -> int:
        self.generation += 1
        generation = self.generation
        self.audit = None
        if self._audit_task is not None:
            self._audit_task.stop()
        self._audit_token = None
        if self._reconcile_task is not None:
            self._reconcile_task.stop()
            self._reconcile_task = None
        self._reconcile_token = None
        self._reconcile_timer.stop()
        self.audit_pool.clear()
        for metadata_task in self._metadata_tasks.values():
            metadata_task.stop()
        self.metadata_pool.clear()
        self._metadata_tasks.clear()
        self._metadata_queue.clear()
        self._metadata_pending.clear()
        self._metadata_candidates.clear()
        self._pending_metadata.clear()
        self._metadata_flush_timer.stop()
        self._metadata_paused = True
        self._folder_artwork = None
        return generation

    @Slot(object)
    def _audit_ready(self, event: AuditWorkerResult) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        generation = event.generation
        result = event.audit
        task = self._audit_task
        token = self._audit_token
        if not self._libraries.owns_result(
            token,
            task.generation if task is not None else None,
            generation,
            self.generation,
            self.current_library.library_id if self.current_library is not None else None,
        ):
            return
        self._audit_task = None
        self._audit_token = None
        had_snapshot = self.audit is not None
        if event.error or result is None:
            self._show_audit_failure(event.error, had_snapshot)
            return
        self._adopt_audit_playlist(result)
        self.audit = result
        self._folder_artwork = event.artwork
        if had_snapshot:
            self.window.reconcile_audit(result, self.current_library)
        else:
            self.window.set_audit(result, self.current_library)
        if self._setup_active and self.current_library is not None:
            if self.selected_authority is None and not result.requires_authority_choice:
                self.selected_authority = result.authority
            self._refresh_setup_view()
        self.window.set_library_validating(False)
        self._load_metadata(generation, event.artwork)
        self._capture_current_snapshot()

    def _show_audit_failure(self, error: str, had_snapshot: bool) -> None:
        if had_snapshot:
            self.window.set_library_validation_error(
                "TrackIndex could not verify the playlist. Reordering is disabled until it can be refreshed."
            )
        else:
            self.audit = None
            if self.current_library is not None:
                self.window.set_library_error(
                    self.current_library.folder,
                    "Could not scan this library",
                )
        styled_message(
            self.window,
            QMessageBox.Icon.Critical,
            "Library audit failed",
            error or "Unknown audit error",
        ).exec()

    def _adopt_audit_playlist(self, result: AuditResult) -> None:
        selected_playlist = result.selected_playlist
        selected_exists = bool(
            selected_playlist
            and any(
                item.name.casefold() == selected_playlist.name.casefold()
                for item in result.playlists
            )
        )
        if self._setup_active:
            self._setup_source_playlist = selected_playlist if selected_exists else None
            if selected_playlist is not None:
                self._setup_playlist_name = (
                    selected_playlist.with_suffix(".m3u8").name
                    if selected_playlist.suffix.casefold() == ".m3u"
                    else selected_playlist.name
                )
        elif (
            self.current_library is not None
            and selected_playlist is not None
            and self.current_library.canonical_playlist != selected_playlist.name
        ):
            replacement = LibraryRecord(
                self.current_library.library_id,
                self.current_library.folder,
                selected_playlist.name,
                self.current_library.storage_mode,
                self.current_library.setup_completed,
            )
            self._replace_library(replacement)
            self.current_library = replacement

    def _schedule_silent_reconciliation(self, delay_ms: int = 1000) -> None:
        if self._shutdown_started or self.current_library is None or self.audit is None:
            return
        self._reconcile_timer.start(max(0, delay_ms))

    def _start_silent_reconciliation(self) -> None:
        if (
            self._shutdown_started
            or self._operation_busy()
            or self.current_library is None
            or self.audit is None
        ):
            return
        if self._reconcile_task is not None:
            self._reconcile_task.stop()
        record = self.current_library
        playlist_name = (
            record.canonical_playlist
            or self._setup_playlist_name
            or default_playlist_name(record.folder)
        )
        target = record.folder / playlist_name
        ids_by_name = {
            track.original_name.casefold(): track.track_id for track in self.audit.tracks
        }
        task = AuditTask(
            self.generation,
            record.folder,
            target,
            self.selected_authority,
            ids_by_name,
            include_artwork=False,
            filesystem_access=self._filesystem_gate.read,
        )
        task.signals.auditReady.connect(self._silent_reconciliation_ready)
        self._reconcile_task = task
        self._reconcile_token = LibraryWorkToken(
            self.generation,
            record.library_id,
        )
        self.audit_pool.start(task)

    @Slot(object)
    def _silent_reconciliation_ready(
        self,
        event: AuditWorkerResult,
    ) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        generation = event.generation
        result = event.audit
        task = self._reconcile_task
        token = self._reconcile_token
        if (
            task is None
            or token is None
            or generation != task.generation
            or generation != token.generation
        ):
            return
        self._reconcile_task = None
        self._reconcile_token = None
        if (
            generation != self.generation
            or self._operation_busy()
            or self.current_library is None
            or token.library_id != self.current_library.library_id
            or self.audit is None
            or event.error
            or result is None
        ):
            return
        if result == self.audit:
            return
        self.audit = result
        self.window.reconcile_audit(result, self.current_library)
        if self._setup_active:
            self._refresh_setup_view()
        self._load_metadata(generation, self._folder_artwork)
        self._capture_current_snapshot()

    def _load_metadata(
        self,
        generation: int,
        art: bytes | PreparedArtwork | None = None,
    ) -> None:
        if not self.audit:
            return
        self.metadata_pool.clear()
        self._metadata_tasks.clear()
        self._metadata_queue.clear()
        self._metadata_pending.clear()
        self._metadata_candidates.clear()
        self._pending_metadata.clear()
        self._metadata_flush_timer.stop()
        self._metadata_paused = False
        by_id = self.audit.track_map()
        cached_values: dict[str, TrackMetadata] = {}
        live_metadata = self.window.track_table.track_model.metadata
        for track_id in self.audit.display_order:
            track = by_id[track_id]
            existing = live_metadata.get(track.track_id)
            if existing is not None:
                self.metadata_cache.put(track.path, existing, track.size, track.mtime_ns)
                continue
            cached = self.metadata_cache.get(track.path, track.size, track.mtime_ns)
            if cached:
                cached_values[track.track_id] = cached
                continue
            self._metadata_candidates[track.track_id] = (generation, track, art)
        if cached_values:
            self.window.track_table.track_model.set_metadata_batch(cached_values)
        first, last = self.window.track_table.visible_row_range()
        self._activate_metadata_window(first, last)
        self._pump_metadata_tasks()

    @staticmethod
    def _prioritized_metadata_ids(
        display_order: tuple[str, ...],
        pending_ids: set[str],
        first: int,
        last: int,
        overscan: int = 8,
    ) -> tuple[str, ...]:
        if not pending_ids:
            return ()
        start = max(0, first - overscan)
        stop = min(len(display_order), max(first, last) + overscan + 1)
        priority = [
            track_id
            for track_id in display_order[start:stop]
            if track_id in pending_ids
        ]
        priority_set = set(priority)
        remaining = (
            track_id
            for track_id in display_order
            if track_id in pending_ids and track_id not in priority_set
        )
        return (*priority, *remaining)

    def _reprioritize_metadata(self, first: int, last: int) -> None:
        if self.audit is None or not self._metadata_pending:
            return
        self._metadata_queue = deque(
            self._prioritized_metadata_ids(
                self.audit.display_order,
                set(self._metadata_pending),
                first,
                last,
            )
        )

    def _activate_metadata_window(
        self,
        first: int,
        last: int,
        overscan: int = 8,
    ) -> None:


        if self.audit is None or not self._metadata_candidates:
            self._reprioritize_metadata(first, last)
            return
        count = len(self.audit.display_order)
        if count <= 0:
            return
        first = max(0, min(first, count - 1))
        last = max(first, min(max(first, last), count - 1))
        start = max(0, first - overscan)
        stop = min(count, last + overscan + 1)
        for track_id in self.audit.display_order[start:stop]:
            payload = self._metadata_candidates.pop(track_id, None)
            if payload is not None:
                self._metadata_pending[track_id] = payload
        self._reprioritize_metadata(first, last)

    def _metadata_viewport_changed(self, first: int, last: int) -> None:
        if self._metadata_paused or self.audit is None:
            return
        self._activate_metadata_window(first, last)
        self._pump_metadata_tasks()

    def _pump_metadata_tasks(self) -> None:
        if self._metadata_paused:
            return
        while self._metadata_queue and len(self._metadata_tasks) < 2:
            track_id = self._metadata_queue.popleft()
            payload = self._metadata_pending.pop(track_id, None)
            if payload is None:
                continue
            generation, track, art = payload
            task = MetadataTask(
                generation,
                track.track_id,
                track.path,
                track.size,
                track.mtime_ns,
                art,
                self.artwork_store,
                filesystem_access=self._filesystem_gate.read,
            )
            task.signals.metadataReady.connect(self._metadata_ready)
            self._metadata_tasks[(generation, track.track_id)] = task
            self.metadata_pool.start(task)

    @Slot(object)
    def _metadata_ready(self, event: MetadataWorkerResult) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        generation = event.generation
        track_id = event.track_id
        value = event.metadata
        task = self._metadata_tasks.pop((generation, track_id), None)
        if task is None:
            return
        self.metadata_cache.put(task.path, value, task.size, task.mtime_ns)
        if generation == self.generation and not self._metadata_paused:
            self._pending_metadata[track_id] = value
            if not self._metadata_flush_timer.isActive():
                self._metadata_flush_timer.start()
            self._pump_metadata_tasks()

    def _flush_metadata(self) -> None:
        if not self._pending_metadata:
            return
        values = dict(self._pending_metadata)
        self._pending_metadata.clear()
        self.window.track_table.track_model.set_metadata_batch(values)
        if self.current_library is not None:
            self._libraries.update_metadata(self.current_library, values)

    def select_authority(self, value: str) -> None:
        if not self.current_library:
            return
        self.selected_authority = OrderAuthority(value)
        self._load_current()

    def _refresh_setup_view(self) -> None:
        if not self._setup_active or self.audit is None or self.current_library is None:
            return
        target = self.current_library.folder / self._setup_playlist_name
        if self._setup_mode is StorageMode.FILENAMES and self._setup_source_playlist is not None:
            target = self._setup_source_playlist
        authority = self.selected_authority
        plan = None
        if not (self.audit.requires_authority_choice and authority is None):
            try:
                plan = build_change_plan(
                    self.audit,
                    self.audit.display_order,
                    self._setup_mode,
                    authority or self.audit.authority,
                    target,
                    playlist_source=(
                        self._setup_source_playlist
                        if self._setup_mode is not StorageMode.FILENAMES
                        else None
                    ),
                )
            except Exception:
                _LOG.exception("Setup planning failed")
                plan = None
        self._setup_plan = plan
        self.window.show_library_setup(
            self.audit,
            self.current_library,
            self._setup_mode,
            authority,
            self._setup_playlist_name,
            self._setup_reconfiguring,
            plan,
        )

    def _setup_selection_changed(self, mode_value: str, authority_value: str) -> None:
        if not self._setup_active or self.current_library is None:
            return
        self._setup_mode = StorageMode(mode_value)
        authority = OrderAuthority(authority_value) if authority_value else None
        if authority != self.selected_authority:
            self.selected_authority = authority
            self._load_current(self._setup_source_playlist or self.current_library.folder / self._setup_playlist_name)
            return
        self._refresh_setup_view()

    def _setup_playlist_changed(self, playlist_name: str) -> None:
        if not self._setup_active or self.current_library is None or not playlist_name:
            return
        self._setup_playlist_name = playlist_name
        target = self.current_library.folder / playlist_name
        source = target if target.is_file() else None
        if source is None and target.suffix.casefold() == ".m3u8":
            legacy = target.with_suffix(".m3u")
            if legacy.is_file():
                source = legacy
        source_changed = source is not None and source != self._setup_source_playlist
        if source is not None:
            self._setup_source_playlist = source
        self.selected_authority = None
        if source_changed:
            self._load_current(source)
        else:
            self._refresh_setup_view()

    def change_ordering_method(self, library_id: str) -> None:
        record = self._library(library_id)
        if record is None or not record.folder.is_dir() or self._operation_busy():
            return
        if self.current_library is None or self.current_library.library_id != library_id:
            self.select_library(library_id)
        if self.current_library is None:
            return
        self._setup_active = True
        self._setup_reconfiguring = self.current_library.setup_completed
        self._setup_mode = StorageMode(self.current_library.storage_mode)
        if self._setup_mode is StorageMode.FILENAMES:
            self.selected_authority = OrderAuthority.FILENAMES
        elif self._setup_mode is StorageMode.M3U8:
            self.selected_authority = OrderAuthority.PLAYLIST
        if self.audit is not None:
            self._refresh_setup_view()

    def _cancel_setup(self) -> None:
        if self.current_library is None or not self.current_library.setup_completed:
            return
        self._setup_active = False
        self._setup_reconfiguring = False
        mode = StorageMode(self.current_library.storage_mode)
        self.selected_authority = (
            OrderAuthority.FILENAMES if mode is StorageMode.FILENAMES else
            (OrderAuthority.PLAYLIST if mode is StorageMode.M3U8 else None)
        )
        playlist_name = self.current_library.canonical_playlist or default_playlist_name(self.current_library.folder)
        self._setup_playlist_name = playlist_name
        candidate = self.current_library.folder / playlist_name
        self._setup_source_playlist = candidate if candidate.is_file() else None
        self._load_current(self.current_library.folder / playlist_name)

    def _confirm_plan_warnings(self, plan, title: str) -> bool:
        if not plan.warnings or self.current_library is None:
            return True
        answer = styled_message(
            self.window,
            QMessageBox.Icon.Warning,
            title,
            "\n".join(plan.warnings) + "\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ).exec()
        return answer == QMessageBox.StandardButton.Yes

    def _command_for_plan(
        self,
        plan,
        before_order: tuple[str, ...],
        after_order: tuple[str, ...],
        *,
        after_mode: StorageMode,
        after_setup_completed: bool,
        after_canonical: str,
    ) -> SessionCommand:
        assert self.current_library is not None and self.audit is not None
        operation = plan.playlist_operations[0] if plan.playlist_operations else None
        after_names = tuple(
            (track_id, new)
            for track_id, (_old, new) in zip(
                plan.ordered_track_ids, plan.filename_preview, strict=True
            )
        )
        before_names = tuple(
            (track.track_id, track.original_name) for track in self.audit.tracks
        )
        before_ids = {track_id for track_id, _name in before_names}
        after_ids = {track_id for track_id, _name in after_names}
        changed_ids = before_ids ^ after_ids
        operation_by_id = {item.track_id: item for item in plan.presence}
        import_by_id = {item.track_id: item for item in plan.imports}
        track_by_id = self.audit.track_map()
        presence: list[SessionPresence] = []
        for track_id in changed_ids:
            move = operation_by_id.get(track_id)
            imported = import_by_id.get(track_id)
            track = track_by_id.get(track_id)
            if move is not None:
                recovery_name = (
                    move.target.name if move.action == "stash" else move.source.name
                )
                size = move.size
                sha256 = move.sha256
            elif imported is not None:
                recovery_name = f"{track_id}{imported.target.suffix.casefold()}"
                size = imported.size
                sha256 = imported.sha256
            elif track is not None:
                recovery_name = f"{track_id}{track.suffix.casefold()}"
                size = track.size
                sha256 = ""
            else:
                raise ValueError("A changed track has no recoverable file state.")
            presence.append(SessionPresence(track_id, recovery_name, size, sha256))
        return SessionCommand(
            plan.plan_id,
            self.current_library.library_id,
            self.audit.folder,
            tuple(dict.fromkeys((*before_order, *after_order))),
            tuple(before_order),
            tuple(after_order),
            before_names,
            after_names,
            operation.path if operation else None,
            operation.before_bytes if operation else None,
            operation.after_bytes if operation else None,
            bool(operation and operation.before_bytes is None),
            self.current_library.storage_mode,
            after_mode.value,
            self.current_library.setup_completed,
            after_setup_completed,
            self.current_library.canonical_playlist,
            after_canonical,
            tuple(presence),
        )

    def _apply_setup(self) -> None:
        if (
            not self._setup_active or self._setup_plan is None or
            self.current_library is None or self.audit is None or self._operation_busy()
        ):
            return
        plan = self._setup_plan
        if plan.blockers:
            self.window.update_setup_plan(plan)
            return
        if not self._confirm_plan_warnings(plan, "Review library warnings"):
            return
        order = tuple(self.audit.display_order)
        command = self._command_for_plan(
            plan,
            order,
            order,
            after_mode=self._setup_mode,
            after_setup_completed=True,
            after_canonical=(
                plan.playlist_operations[0].path.name
                if plan.playlist_operations
                else (
                    self._setup_source_playlist.name
                    if self._setup_mode is StorageMode.FILENAMES and self._setup_source_playlist is not None
                    else self._setup_playlist_name
                )
            ),
        )
        self._prepare_transaction(
            TransactionOperationContext(
                "forward",
                command,
                plan,
                before=order,
                moved=0,
                action="setup",
            )
        )

    def _rescan_library(self, library_id: str) -> None:
        if self.current_library and self.current_library.library_id == library_id:
            self._load_current()

    def open_in_explorer(self, library_id: str) -> None:
        record = self._library(library_id)
        if record and record.folder.exists():
            QProcess.startDetached("explorer.exe", [str(record.folder)])

    def open_current_library(self) -> None:
        if self.current_library is not None:
            self.open_in_explorer(self.current_library.library_id)

    def remove_library(self, library_id: str) -> None:
        if self._operation_busy():
            return
        if self._playback_coordinator is not None:
            self._playback_coordinator.library_removed(library_id)
        self.config.libraries = [item for item in self.config.libraries if item.library_id != library_id]
        self._libraries.discard(library_id)
        if self.config.last_library_id == library_id:
            self.config.last_library_id = ""
        save_config(self.config)
        if self.current_library and self.current_library.library_id == library_id:
            self._invalidate_library_work()
            self.current_library = None
            self.audit = None
            self.window.clear_library()
        self._refresh_libraries()

    def commit_reorder(self, before: tuple[str, ...], after: tuple[str, ...], moved_count: int) -> None:
        if self._operation_busy() or not self.audit or not self.current_library:
            self.window.track_table.track_model.restore_order(before)
            return
        mode = StorageMode(self.current_library.storage_mode)
        playlist_name = self.current_library.canonical_playlist or default_playlist_name(self.current_library.folder)
        target = self.current_library.folder / playlist_name
        authority = (
            OrderAuthority.FILENAMES if mode is StorageMode.FILENAMES else
            (OrderAuthority.PLAYLIST if mode is StorageMode.M3U8 else (self.selected_authority or self.audit.authority))
        )
        audit = self.audit
        self._start_plan(
            lambda: build_change_plan(audit, after, mode, authority, target),
            PlanOperationContext(
                "reorder",
                self.generation,
                self.current_library.library_id,
                before,
                mode,
                playlist_name,
                after,
                moved_count,
            ),
        )

    def import_external_files(self, paths, insertion: int) -> None:
        if self._operation_busy() or self.audit is None or self.current_library is None:
            return
        try:
            sources = tuple(Path(path) for path in paths)
            by_path = {
                str(track.path.absolute()).casefold(): track.track_id
                for track in self.audit.tracks
            }
            existing_ids = tuple(
                by_path[key]
                for path in sources
                if (key := str(path.absolute()).casefold()) in by_path
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            styled_message(
                self.window,
                QMessageBox.Icon.Warning,
                "Could not add tracks",
                "One or more dropped file paths are invalid or unavailable.",
            ).exec()
            return
        if existing_ids:
            if len(existing_ids) != len(sources):
                styled_message(
                    self.window,
                    QMessageBox.Icon.Warning,
                    "Could not add tracks",
                    "Files already in this library cannot be mixed with new files in one drop.",
                ).exec()
                return
            before = self.window.track_table.track_model.ordered_track_ids()
            after = self.window.track_table.track_model.calculate_grouped_order(
                before, existing_ids, insertion
            )
            self.window.track_table.track_model.apply_reorder(
                after,
                len(existing_ids),
            )
            return

        mode = StorageMode(self.current_library.storage_mode)
        playlist_name = (
            self.current_library.canonical_playlist
            or default_playlist_name(self.current_library.folder)
        )
        target = self.current_library.folder / playlist_name
        authority = (
            OrderAuthority.FILENAMES
            if mode is StorageMode.FILENAMES
            else (
                OrderAuthority.PLAYLIST
                if mode is StorageMode.M3U8
                else (self.selected_authority or self.audit.authority)
            )
        )
        audit = self.audit
        self._start_plan(
            lambda: build_import_plan(
                audit,
                sources,
                insertion,
                mode,
                authority,
                target,
            ),
            PlanOperationContext(
                "import",
                self.generation,
                self.current_library.library_id,
                audit.display_order,
                mode,
                playlist_name,
            ),
        )

    def delete_tracks(self, track_ids) -> None:
        if self._operation_busy() or self.audit is None or self.current_library is None:
            return
        available = self.audit.track_map()
        selected = tuple(
            dict.fromkeys(str(track_id) for track_id in track_ids if str(track_id) in available)
        )
        if not selected:
            return
        if self.config.confirm_track_deletion:
            dialog = DeleteTracksDialog(len(selected), self.window)
            dialog.setStyleSheet(self.window.styleSheet())
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if dialog.dont_ask_again.isChecked():
                self.config.confirm_track_deletion = False
                self.window.settings_panel.set_values(
                    self.config.ui_scale_percent,
                    self.config.auto_check_updates,
                    False,
                )
                save_config(self.config)

        mode = StorageMode(self.current_library.storage_mode)
        playlist_name = (
            self.current_library.canonical_playlist
            or default_playlist_name(self.current_library.folder)
        )
        target = self.current_library.folder / playlist_name
        authority = (
            OrderAuthority.FILENAMES
            if mode is StorageMode.FILENAMES
            else (
                OrderAuthority.PLAYLIST
                if mode is StorageMode.M3U8
                else (self.selected_authority or self.audit.authority)
            )
        )
        audit = self.audit
        self._start_plan(
            lambda: build_delete_plan(
                audit,
                selected,
                mode,
                authority,
                target,
            ),
            PlanOperationContext(
                "delete",
                self.generation,
                self.current_library.library_id,
                audit.display_order,
                mode,
                playlist_name,
                moved=len(selected),
            ),
        )

    def _start_plan(
        self,
        builder: Callable[[], ChangePlan],
        context: PlanOperationContext,
    ) -> None:
        if self._operation_busy():
            return
        self._reconcile_timer.stop()
        self.window.set_saving(True)
        task = PlanTask(builder, self._filesystem_gate.read)
        task.signals.planReady.connect(self._plan_ready)
        self._plan_task = task
        self._mutations.begin_plan(task.task_id, context)
        self.transaction_pool.start(task)

    @Slot(object)
    def _plan_ready(self, event: PlanWorkerResult) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started or self._plan_task is None:
            return
        if event.task_id != self._plan_task.task_id:
            return
        context = self._mutations.accept_plan(event.task_id)
        if context is None:
            return
        plan = event.plan
        error = event.error
        self._plan_task = None
        stale = (
            context.generation != self.generation
            or self.current_library is None
            or context.library_id != self.current_library.library_id
            or self.audit is None
        )
        if error or not isinstance(plan, ChangePlan) or stale or plan.blockers:
            message = error or (
                "\n".join(plan.blockers)
                if isinstance(plan, ChangePlan)
                else "The library changed while preparing the operation."
            )
            self._reject_prepared_plan(context, message, bool(error))
            return
        library = self.current_library
        if library is None or self.audit is None:
            return
        if not self._approve_plan_warnings(plan, library.library_id, context):
            return
        if context.operation == "reorder" and context.after is None:
            self.window.track_table.track_model.restore_order(context.before)
            self.window.set_saving(False, "Not saved")
            return
        try:
            transaction = self._transaction_for_plan(plan, context)
        except (KeyError, OSError, ValueError) as exc:
            self._reject_prepared_plan(
                context,
                str(exc),
                True,
                track_change=context.operation in {"import", "delete"},
            )
            return
        self._prepare_transaction(transaction)

    def _reject_prepared_plan(
        self,
        context: PlanOperationContext,
        message: str,
        critical: bool,
        *,
        track_change: bool = False,
    ) -> None:
        if context.operation == "reorder":
            self.window.track_table.track_model.restore_order(context.before)
        self.window.set_saving(False, "Not saved")
        styled_message(
            self.window,
            QMessageBox.Icon.Critical if critical else QMessageBox.Icon.Warning,
            "Could not prepare track changes" if track_change else "Could not prepare changes",
            message,
        ).exec()

    def _approve_plan_warnings(
        self,
        plan: ChangePlan,
        library_id: str,
        context: PlanOperationContext,
    ) -> bool:
        if not plan.warnings or library_id in self._warned_other_playlists:
            return True
        answer = styled_message(
            self.window,
            QMessageBox.Icon.Warning,
            "Other playlists may become stale",
            "\n".join(plan.warnings) + "\n\nContinue for this session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ).exec()
        if answer != QMessageBox.StandardButton.Yes:
            if context.operation == "reorder":
                self.window.track_table.track_model.restore_order(context.before)
            self.window.set_saving(False, "Not saved")
            return False
        self._warned_other_playlists.add(library_id)
        return True

    def _transaction_for_plan(
        self,
        plan: ChangePlan,
        context: PlanOperationContext,
    ) -> TransactionOperationContext:
        after = (
            plan.ordered_track_ids
            if context.operation in {"import", "delete"}
            else context.after
        )
        if after is None:
            raise ValueError("The prepared order is unavailable.")
        command = self._command_for_plan(
            plan,
            context.before,
            after,
            after_mode=context.mode,
            after_setup_completed=True,
            after_canonical=context.playlist_name,
        )
        moved = (
            len(plan.imports)
            if context.operation == "import"
            else context.moved
        )
        return TransactionOperationContext(
            "forward",
            command,
            plan,
            before=context.before,
            moved=moved,
            action=context.operation if context.operation != "reorder" else "",
        )

    def remove_filename_prefixes(self) -> None:
        if (
            self._operation_busy() or self.audit is None or self.current_library is None or
            StorageMode(self.current_library.storage_mode) is not StorageMode.M3U8
        ):
            return
        affected = sum(track.filename_index is not None for track in self.audit.tracks)
        if not affected:
            return
        playlist_name = self.current_library.canonical_playlist or default_playlist_name(self.current_library.folder)
        target = self.current_library.folder / playlist_name
        try:
            plan = build_prefix_removal_plan(self.audit, target)
        except Exception as exc:
            styled_message(self.window, QMessageBox.Icon.Critical, "Could not remove prefixes", str(exc)).exec()
            return
        if plan.blockers:
            styled_message(self.window, QMessageBox.Icon.Warning, "Prefix removal blocked", "\n".join(plan.blockers)).exec()
            return
        text = f"Remove recognized numeric prefixes from {affected} file(s)?\n\nThe M3U8 order will not change."
        if plan.warnings:
            text += "\n\n" + "\n".join(plan.warnings)
        if styled_message(
            self.window, QMessageBox.Icon.Question, "Remove filename prefixes", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ).exec() != QMessageBox.StandardButton.Yes:
            return
        order = tuple(self.audit.display_order)
        command = self._command_for_plan(
            plan,
            order,
            order,
            after_mode=StorageMode.M3U8,
            after_setup_completed=True,
            after_canonical=playlist_name,
        )
        self._prepare_transaction(
            TransactionOperationContext(
                "forward",
                command,
                plan,
                before=order,
                moved=0,
                action="prefix-removal",
            )
        )

    def undo(self) -> None:
        if self._operation_busy() or not self.audit or not self.current_library:
            return
        history = self.sessions.history(self.current_library.library_id)
        if not history.undo:
            self.window.show_toast("Nothing to undo", False)
            return
        command = history.undo[-1]
        try:
            plan = build_session_change_plan(self.audit, command, to_after=False)
        except Exception as exc:
            styled_message(self.window, QMessageBox.Icon.Critical, "Could not prepare undo", str(exc)).exec()
            return
        if plan.blockers:
            styled_message(self.window, QMessageBox.Icon.Warning, "Undo blocked", "\n".join(plan.blockers)).exec()
            return
        self._prepare_transaction(TransactionOperationContext("undo", command, plan))

    def redo(self) -> None:
        if self._operation_busy() or not self.audit or not self.current_library:
            return
        history = self.sessions.history(self.current_library.library_id)
        if not history.redo:
            self.window.show_toast("Nothing to redo", False)
            return
        command = history.redo[-1]
        try:
            plan = build_session_change_plan(self.audit, command, to_after=True)
        except Exception as exc:
            styled_message(self.window, QMessageBox.Icon.Critical, "Could not prepare redo", str(exc)).exec()
            return
        if plan.blockers:
            styled_message(self.window, QMessageBox.Icon.Warning, "Redo blocked", "\n".join(plan.blockers)).exec()
            return
        self._prepare_transaction(TransactionOperationContext("redo", command, plan))

    def _prepare_transaction(self, context: TransactionOperationContext) -> None:
        self._start_transaction(
            context,
            lambda: self.sessions.prepare(context.command, context.kind),
        )

    def _start_transaction(
        self,
        context: TransactionOperationContext,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        plan = context.plan
        self._reconcile_timer.stop()
        if self._reconcile_task is not None:
            self._reconcile_task.stop()
            self._reconcile_task = None
        self._metadata_paused = True
        for metadata_task in self._metadata_tasks.values():
            metadata_task.stop()
        self.metadata_pool.clear()
        self._metadata_tasks.clear()
        self._metadata_queue.clear()
        self._metadata_pending.clear()
        self._metadata_candidates.clear()
        self._pending_metadata.clear()
        self._metadata_flush_timer.stop()
        self.window.set_saving(True)
        coordinator = self._playback_coordinator
        if coordinator is not None:
            self._transaction_preparing = True

            def ready() -> None:
                self._transaction_preparing = False
                if self._shutdown_started:
                    return
                self._launch_transaction(context, prepare)

            def failed(message: str) -> None:
                self._transaction_preparing = False
                if self._shutdown_started:
                    return
                if context.before is not None:
                    self.window.track_table.track_model.restore_order(context.before)
                self.window.set_saving(False, "Not saved")
                self._metadata_paused = False
                self._load_metadata(self.generation, self._folder_artwork)
                self._schedule_silent_reconciliation(0)
                styled_message(
                    self.window,
                    QMessageBox.Icon.Critical,
                    "Could not prepare playback",
                    message,
                ).exec()

            coordinator.prepare_filesystem_mutation(
                context.library_id,
                plan,
                ready,
                failed,
            )
            return
        self._launch_transaction(context, prepare)

    def _launch_transaction(
        self,
        context: TransactionOperationContext,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        transaction_task = TransactionTask(
            context.plan,
            self._filesystem_gate.write,
            prepare,
        )
        transaction_task.signals.transactionDone.connect(self._transaction_done)
        self._mutations.begin_transaction(transaction_task.task_id, context)
        self._transaction_task = transaction_task
        self.transaction_pool.start(transaction_task)

    @Slot(object)
    def _transaction_done(self, event: TransactionWorkerResult) -> None:
        assert_qt_thread_affinity(self)
        task = self._transaction_task
        if self._shutdown_started or task is None:
            return
        if event.task_id != task.task_id:
            return
        context = self._mutations.accept_transaction(event.task_id)
        if context is None:
            return
        result = event.result
        self._transaction_task = None
        command = context.command
        kind = context.kind
        plan = context.plan
        if not result.success:
            if kind == "forward" and context.before is not None:
                self.window.track_table.track_model.restore_order(context.before)
            self.window.set_saving(False, "Not saved")
            details = "\n".join(result.details)
            styled_message(self.window, QMessageBox.Icon.Critical, "Could not save order", f"{result.message}\n{details}").exec()
            self._metadata_paused = False
            self._load_metadata(self.generation, self._folder_artwork)
            self._schedule_silent_reconciliation(0)
            if self._playback_coordinator is not None:
                self._playback_coordinator.complete_filesystem_mutation(
                    context.library_id,
                    plan,
                    result,
                )
            return
        bookkeeping_error: Exception | None = None
        try:
            _names, toast = self._commit_successful_command(context, command)
        except Exception as exc:
            bookkeeping_error = exc
            _LOG.exception("Committed change could not be recorded in session history")
            toast = "Changes saved"
        projected = self._apply_committed_result(context, command, result)
        if self._playback_coordinator is not None:
            self._playback_coordinator.complete_filesystem_mutation(
                context.library_id,
                plan,
                result,
            )
        self.window.set_saving(
            False,
            "Saved" if bookkeeping_error is None else "Saved - recovery pending",
        )
        if not projected:
            self._metadata_paused = False
            self._load_metadata(self.generation, self._folder_artwork)
            self._schedule_silent_reconciliation(0)
        if bookkeeping_error is not None:
            styled_message(
                self.window,
                QMessageBox.Icon.Warning,
                "Changes saved; recovery record pending",
                "The files were updated safely, but TrackIndex could not finish its session "
                "bookkeeping. It will reconcile this operation at the next start."
                f"\n\n{bookkeeping_error}",
            ).exec()
        toast_action = "redo" if kind == "undo" else "undo"
        self.window.show_toast(toast, toast_action)

    def _apply_committed_result(
        self,
        context: TransactionOperationContext,
        command: SessionCommand,
        result: ApplyResult,
    ) -> bool:
        after = context.kind != "undo"
        mode = StorageMode(
            command.after_storage_mode if after else command.before_storage_mode
        )
        setup_completed = (
            command.after_setup_completed if after else command.before_setup_completed
        )
        return self._apply_plan_result(context, result, mode, setup_completed)

    def _apply_plan_result(
        self,
        context: TransactionOperationContext,
        result: ApplyResult,
        mode: StorageMode,
        setup_completed: bool,
    ) -> bool:
        plan = context.plan
        if result.receipt is None or self.audit is None:
            return False
        previous = self.audit
        try:
            projected = project_audit_after_commit(
                previous,
                plan,
                result.receipt,
                storage_mode=mode,
                setup_completed=setup_completed,
            )
        except (KeyError, ValueError):
            return False

        old_tracks = previous.track_map()
        for track in projected.tracks:
            old = old_tracks.get(track.track_id)
            if old is not None and old.path != track.path:
                self.metadata_cache.rekey(
                    old.path,
                    track.path,
                    old.size,
                    old.mtime_ns,
                )

        self.audit = projected
        self._setup_active = not setup_completed
        self._setup_reconfiguring = False
        self._setup_mode = StorageMode.BOTH if self._setup_active else mode
        if mode is StorageMode.FILENAMES and setup_completed:
            self.selected_authority = OrderAuthority.FILENAMES
        elif mode is StorageMode.M3U8 and setup_completed:
            self.selected_authority = OrderAuthority.PLAYLIST
        else:
            self.selected_authority = None
        if self.current_library is not None:
            playlist_name = (
                self.current_library.canonical_playlist
                or self._setup_playlist_name
                or default_playlist_name(self.current_library.folder)
            )
            self._setup_playlist_name = playlist_name
        self.window.reconcile_audit(projected, self.current_library)
        if self._setup_active:
            self._refresh_setup_view()
        self._load_metadata(self.generation, self._folder_artwork)
        self._capture_current_snapshot()
        self._schedule_silent_reconciliation()
        return True

    def _commit_successful_command(
        self,
        context: TransactionOperationContext,
        command: SessionCommand,
    ) -> tuple[tuple[tuple[str, str], ...], str]:
        kind = context.kind
        if kind == "forward":
            self.sessions.commit_forward(command)
            self._apply_command_library_state(command, True)
            count = context.moved
            count_text = f"{count} {'track' if count == 1 else 'tracks'}"
            if context.action == "setup":
                toast = "Playlist set up"
            elif context.action == "prefix-removal":
                toast = "Filename prefixes removed"
            elif context.action == "import":
                toast = f"Added {count_text}"
            elif context.action == "delete":
                toast = f"Removed {count_text}"
            else:
                toast = f"Moved {count_text}"
            return command.after_names, toast
        if kind == "undo":
            self.sessions.commit_undo(command)
            self._apply_command_library_state(command, False)
            return command.before_names, "Order restored"
        if kind == "redo":
            self.sessions.commit_redo(command)
            self._apply_command_library_state(command, True)
            return command.after_names, "Order reapplied"
        raise ValueError("Unknown transaction completion state.")

    def _apply_command_library_state(self, command: SessionCommand, after: bool) -> None:
        record = self._library(command.library_id)
        if record is None:
            return
        replacement = LibraryRecord(
            record.library_id,
            record.folder,
            command.after_canonical_playlist if after else command.before_canonical_playlist,
            command.after_storage_mode if after else command.before_storage_mode,
            command.after_setup_completed if after else command.before_setup_completed,
        )
        if replacement == record:
            return
        self.config.libraries = [replacement if item.library_id == replacement.library_id else item for item in self.config.libraries]
        if self.current_library and self.current_library.library_id == replacement.library_id:
            self.current_library = replacement
        save_config(self.config)
        self._refresh_libraries(replacement.library_id)

    def change_music_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self.window, "Choose music folder", self.config.music_root)
        if not selected:
            return
        self.config.music_root = str(Path(selected).resolve())
        self.window.settings_panel.set_music_root(self.config.music_root)
        save_config(self.config)
        self.scan_for_libraries(manual=True)

    def scan_for_libraries(self, *, manual: bool) -> None:
        if self._discovery_thread is not None:
            return
        root = Path(self.config.music_root)
        if not root.is_dir():
            if manual:
                styled_message(self.window, QMessageBox.Icon.Warning, "Music folder unavailable", str(root)).exec()
            self.config.initial_discovery_completed = True
            save_config(self.config)
            return
        self.window.settings_panel.set_update_status("Scanning music folders...")
        thread = QThread(self.window)
        worker = DiscoveryWorker(root)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._discovery_completed)
        worker.failed.connect(self._discovery_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_discovery_thread)
        thread.finished.connect(thread.deleteLater)
        self._discovery_thread, self._discovery_worker = thread, worker
        thread.start()

    @Slot(object)
    def _discovery_completed(self, folders: tuple[Path, ...]) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        self.config.initial_discovery_completed = True
        save_config(self.config)
        known = {str(normalize_folder(item.folder)).casefold() for item in self.config.libraries}
        new = tuple(item for item in folders if str(normalize_folder(item)).casefold() not in known)
        self.window.settings_panel.set_update_status(f"Found {len(folders)} music libraries.")
        if not new:
            self._start_snapshot_prefetch()
            return
        dialog = LibraryImportDialog(new, self.window)
        dialog.setStyleSheet(self.window.styleSheet())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for folder in dialog.selected_folders():
            self._add_library(folder, select=False)
        self._refresh_libraries()
        self._start_snapshot_prefetch()

    @Slot(str)
    def _discovery_failed(self, error: str) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        self.config.initial_discovery_completed = True
        save_config(self.config)
        self.window.settings_panel.set_update_status("Library scan failed.")
        styled_message(
            self.window,
            QMessageBox.Icon.Warning,
            "Could not scan music folders",
            error or "The music folder could not be scanned.",
        ).exec()

    @Slot()
    def _clear_discovery_thread(self) -> None:
        assert_qt_thread_affinity(self)
        self._discovery_thread = None
        self._discovery_worker = None

    def _recover_pending_operations(self) -> None:
        for journal in pending_journals():
            result = recover_journal(journal)
            if not result.success:
                styled_message(self.window, QMessageBox.Icon.Critical, "Recovery incomplete", result.message).exec()

    def _recover_sessions(self) -> None:
        records = self.sessions.pending_recovery()
        if not records:
            return
        records, unresolved = self._partition_recovery_records(records)
        if unresolved:
            styled_message(
                self.window,
                QMessageBox.Icon.Critical,
                "Session recovery needs attention",
                "TrackIndex could not safely determine whether an interrupted operation completed. "
                "Its recovery data was left untouched. Do not remove it until the affected library "
                "has been checked.",
            ).exec()
        if not records:
            return
        box = styled_message(self.window, QMessageBox.Icon.Warning, "Previous session found",
                             "TrackIndex closed before the previous session was accepted. Keep its changes, or restore the session start?",
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("Keep Changes")
        box.button(QMessageBox.StandardButton.No).setText("Restore Session Start")
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._keep_recovery_records(records)
            return
        failures = self._restore_recovery_records(records)
        if failures:
            styled_message(
                self.window,
                QMessageBox.Icon.Critical,
                "Session restore incomplete",
                "\n".join(failures),
            ).exec()

    def _partition_recovery_records(
        self,
        records: tuple[SessionRecoveryRecord, ...],
    ) -> tuple[tuple[SessionRecoveryRecord, ...], tuple[SessionRecoveryRecord, ...]]:
        configured = {
            record.library_id: record.folder for record in self.config.libraries
        }
        unresolved = tuple(
            record
            for record in records
            if record.unresolved_commands
            or not self._same_recovery_library(
                configured.get(record.library_id),
                record.folder,
            )
        )
        return tuple(record for record in records if record not in unresolved), unresolved

    def _keep_recovery_records(
        self,
        records: tuple[SessionRecoveryRecord, ...],
    ) -> None:
        for record in records:
            if record.commands:
                self._apply_command_library_state(record.commands[-1], True)
            self.sessions.keep_recovery(record.library_id)

    def _restore_recovery_records(
        self,
        records: tuple[SessionRecoveryRecord, ...],
    ) -> list[str]:
        failures: list[str] = []
        for record in records:
            error = self._restore_recovery_record(record)
            if error:
                failures.append(error)
            else:
                self.sessions.keep_recovery(record.library_id)
        return failures

    def _restore_recovery_record(self, record: SessionRecoveryRecord) -> str:
        for command in reversed(record.commands):
            try:
                ids = {
                    name.casefold(): track_id
                    for track_id, name in command.after_names
                }
                audit = audit_folder(
                    record.folder,
                    command.playlist_path,
                    OrderAuthority.FILENAMES,
                    ids,
                )
                plan = build_session_change_plan(audit, command, to_after=False)
                result = apply_change_plan(plan)
                if not result.success:
                    return result.message
                self._apply_command_library_state(command, False)
            except Exception as exc:
                return str(exc)
        return ""

    @staticmethod
    def _same_recovery_library(configured: Path | None, recovered: Path) -> bool:
        if configured is None:
            return False
        try:
            return configured.resolve(strict=False) == recovered.resolve(strict=False)
        except (OSError, RuntimeError):
            return False

    def set_theme_mode(self, mode: str) -> None:
        self.config.theme_mode = "light" if mode == "light" else "dark"
        self.window.apply_appearance(get_theme(self.config.theme_mode), self.config.theme_mode, self.config.ui_scale_percent)
        save_config(self.config)

    def set_ui_scale(self, value: int) -> None:
        self.config.ui_scale_percent = max(75, min(200, int(value)))
        self.window.apply_appearance(get_theme(self.config.theme_mode), self.config.theme_mode, self.config.ui_scale_percent)
        save_config(self.config)

    def _set_pinned(self, pinned: bool) -> None:
        self.config.window_pinned = bool(pinned)
        save_config(self.config)

    def _set_auto_updates(self, enabled: bool) -> None:
        self.config.auto_check_updates = bool(enabled)
        save_config(self.config)

    def _set_confirm_track_deletion(self, enabled: bool) -> None:
        self.config.confirm_track_deletion = bool(enabled)
        save_config(self.config)

    def reset_settings(self) -> None:
        if styled_message(self.window, QMessageBox.Icon.Question, "Reset settings",
                          "Reset appearance and update preferences? Remembered libraries are preserved.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No).exec() != QMessageBox.StandardButton.Yes:
            return
        defaults = default_config()
        self.config.theme_mode = defaults.theme_mode
        self.config.ui_scale_percent = defaults.ui_scale_percent
        self.config.window_pinned = defaults.window_pinned
        self.config.storage_mode = StorageMode.BOTH.value
        self.config.auto_check_updates = defaults.auto_check_updates
        self.config.confirm_track_deletion = defaults.confirm_track_deletion
        self.config.playback_volume = defaults.playback_volume
        self.config.playback_muted = defaults.playback_muted
        self.config.playback_shuffle = defaults.playback_shuffle
        self.config.playback_repeat = defaults.playback_repeat
        if self._playback_coordinator is not None:
            self._playback_coordinator.reset_preferences()
        save_config(self.config)
        self.window.settings_panel.set_values(
            defaults.ui_scale_percent,
            defaults.auto_check_updates,
            defaults.confirm_track_deletion,
        )
        self.window.set_pinned(False)
        self.window.apply_appearance(get_theme("dark"), "dark", defaults.ui_scale_percent)

    def check_for_updates(self, *, manual: bool) -> None:
        if self._update_thread is not None:
            return
        self._updates.begin_check(manual)
        self.window.settings_panel.set_update_status("Checking for updates...")
        thread = QThread(self.window)
        worker = UpdateCheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._update_check_completed)
        worker.failed.connect(self._update_check_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_update_thread)
        thread.finished.connect(thread.deleteLater)
        self._update_thread, self._update_worker = thread, worker
        thread.start()

    def _update_check_completed(self, check) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        if not check.update_available:
            self.window.settings_panel.set_update_status(f"TrackIndex {APP_VERSION} is current.")
            if self._updates.manual:
                styled_message(self.window, QMessageBox.Icon.Information, "No update available", f"TrackIndex {APP_VERSION} is up to date.").exec()
            return
        self.window.settings_panel.set_update_status(f"TrackIndex {check.latest_version} is available.")
        self._updates.record_check(check)
        notes = [f"- {line}" for line in check.notes[:8] if str(line).strip()]
        details = "\n\nWhat's new:\n" + "\n".join(notes) if notes else ""
        can_install = bool(check.install_supported and getattr(sys, "frozen", False))
        if check.requires_manual_update:
            can_install = False
            details += (
                "\n\nYour current version is below the minimum supported "
                f"auto-update baseline ({check.minimum_supported_version})."
            )
        proceed, auto_install = ask_update_install_preference(
            self.window,
            latest_version=check.latest_version,
            details_text=details,
            install_supported=can_install,
        )
        if not proceed:
            return
        if can_install and auto_install:
            self._start_update_install(check)
            return
        self._open_update_page(check.page_url)

    def _update_check_failed(self, message: str) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        self.window.settings_panel.set_update_status("Update check unavailable.")
        if self._updates.manual:
            styled_message(self.window, QMessageBox.Icon.Warning, "Update check failed", message).exec()

    def _start_update_install(self, check) -> None:
        if self._update_thread is not None:
            QTimer.singleShot(100, lambda: self._start_update_install(check))
            return
        dialog = UpdateProgressDialog(self.window)
        dialog.setStyleSheet(self.window.styleSheet())
        thread = QThread(self.window)
        worker = UpdateInstallWorker(check)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(dialog.set_progress)
        worker.completed.connect(self._update_install_completed)
        worker.failed.connect(self._update_install_failed)
        worker.canceled.connect(self._update_install_canceled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_update_thread)
        thread.finished.connect(thread.deleteLater)
        dialog.rejected.connect(worker.stop)
        self._progress_dialog = dialog
        self._update_thread, self._update_worker = thread, worker
        thread.start()
        dialog.show()

    def _update_install_completed(self, prepared) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            self_updater.discard_prepared_update(prepared)
            return
        self._updates.stage(prepared)
        self._close_update_progress()

    def _show_update_handoff(self, prepared: self_updater.PreparedUpdate) -> None:
        if self._shutdown_started:
            self_updater.discard_prepared_update(prepared)
            return
        proceed, restart_after = ask_update_handoff(
            self.window,
            version=prepared.latest_version,
            default_restart=True,
            requires_elevation=prepared.requires_elevation,
        )
        if not proceed:
            self_updater.discard_prepared_update(prepared)
            self.window.settings_panel.set_update_status("Update canceled before installation.")
            styled_message(
                self.window,
                QMessageBox.Icon.Information,
                "Update canceled",
                "Update was aborted. No installer was launched.",
            ).exec()
            return
        try:
            self_updater.launch_prepared_update(
                prepared,
                restart_after_update=restart_after,
            )
        except Exception as exc:
            self_updater.discard_prepared_update(prepared)
            styled_message(self.window, QMessageBox.Icon.Critical, "Update failed", str(exc)).exec()
            self._offer_update_page_fallback()
            return
        self.app.quit()

    @Slot(str)
    def _update_install_failed(self, message: str) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        self._close_update_progress()
        styled_message(
            self.window,
            QMessageBox.Icon.Critical,
            "Update failed",
            message,
        ).exec()
        self._offer_update_page_fallback()

    @Slot()
    def _update_install_canceled(self) -> None:
        assert_qt_thread_affinity(self)
        if self._shutdown_started:
            return
        self._close_update_progress()
        self.window.settings_panel.set_update_status("Update canceled.")
        styled_message(
            self.window,
            QMessageBox.Icon.Information,
            "Update canceled",
            "Update was canceled before installation started.",
        ).exec()

    def _close_update_progress(self) -> None:
        dialog = self._progress_dialog
        if dialog is None:
            return
        self._progress_dialog = None
        dialog.accept()
        dialog.deleteLater()

    def _open_update_page(self, url: str = "") -> None:
        target = str(url or DOWNLOAD_PAGE_URL).strip() or DOWNLOAD_PAGE_URL
        if not QDesktopServices.openUrl(QUrl(target)):
            styled_message(
                self.window,
                QMessageBox.Icon.Warning,
                "Update page",
                "Windows could not open the TrackIndex download page.",
            ).exec()

    def _offer_update_page_fallback(self) -> None:
        answer = styled_message(
            self.window,
            QMessageBox.Icon.Question,
            "Update install",
            "Would you like to open the download page instead?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        answer.setDefaultButton(QMessageBox.StandardButton.Yes)
        if answer.exec() == QMessageBox.StandardButton.Yes:
            self._open_update_page(self._updates.fallback_url())

    def _clear_update_thread(self) -> None:
        assert_qt_thread_affinity(self)
        self._update_thread = self._update_worker = None
        prepared = self._updates.take_prepared()
        if prepared is not None:
            QTimer.singleShot(0, lambda prepared=prepared: self._show_update_handoff(prepared))

    def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._transaction_preparing = False
        self._reconcile_timer.stop()
        self._finish_plan_for_shutdown()
        self._request_worker_shutdown()
        self._finish_transaction_for_shutdown()
        self._stop_library_work()
        self._wait_for_worker_shutdown()
        self._persist_shutdown_state()

    def _finish_plan_for_shutdown(self) -> None:
        active_plan = self._plan_task
        if active_plan is None:
            return
        plan_finished = self.transaction_pool.waitForDone(
            self._SHORT_SHUTDOWN_WAIT_MS
        )
        if not plan_finished:
            _LOG.error("Planning worker did not stop before the shutdown deadline")
            return
        if self._plan_task is active_plan:
            with suppress(RuntimeError, TypeError):
                active_plan.signals.planReady.disconnect()
            self._plan_task = None
            self._mutations.abandon_plan()

    def _request_worker_shutdown(self) -> None:
        if self._discovery_worker is not None:
            self._discovery_worker.stop()
        if self._discovery_thread is not None:
            self._discovery_thread.quit()
        if self._update_worker is not None:
            self._update_worker.stop()
        if self._update_thread is not None:
            self._update_thread.quit()
        for _token, task in self._prefetch_tasks.values():
            task.stop()
        self.prefetch_pool.clear()
        self._prefetch_tasks.clear()

    def _finish_transaction_for_shutdown(self) -> None:
        active_transaction = self._transaction_task
        if active_transaction is None:
            return
        transaction_finished = self.transaction_pool.waitForDone(
            self._TRANSACTION_SHUTDOWN_WAIT_MS
        )
        if not transaction_finished:
            _LOG.error(
                "Filesystem transaction did not stop before the shutdown deadline; recovery state was retained"
            )
            return
        if self._transaction_task is not active_transaction or active_transaction.result is None:
            return
        with suppress(RuntimeError, TypeError):
            active_transaction.signals.transactionDone.disconnect(self._transaction_done)
        context = self._mutations.transaction_context
        if active_transaction.result.success and context is not None:
            try:
                self._commit_successful_command(context, context.command)
            except Exception:
                _LOG.exception(
                    "Successful filesystem transaction could not be committed to session history"
                )
                return
        elif active_transaction.result.success:
            return
        self._transaction_task = None
        self._mutations.abandon_transaction()

    def _stop_library_work(self) -> None:
        if self._audit_task is not None:
            self._audit_task.stop()
        if self._reconcile_task is not None:
            self._reconcile_task.stop()
        for metadata_task in self._metadata_tasks.values():
            metadata_task.stop()
        self.audit_pool.clear()
        self.metadata_pool.clear()
        self._metadata_queue.clear()
        self._metadata_pending.clear()
        self._metadata_candidates.clear()

    def _wait_for_worker_shutdown(self) -> None:
        if not self.audit_pool.waitForDone(self._SHORT_SHUTDOWN_WAIT_MS):
            _LOG.error("Audit worker did not stop before the shutdown deadline")
        if not self.metadata_pool.waitForDone(self._SHORT_SHUTDOWN_WAIT_MS):
            _LOG.error("Metadata workers did not stop before the shutdown deadline")
        if not self.prefetch_pool.waitForDone(self._SHORT_SHUTDOWN_WAIT_MS):
            _LOG.error("Prefetch worker did not stop before the shutdown deadline")
        if (
            self._discovery_thread is not None
            and not self._discovery_thread.wait(self._SHORT_SHUTDOWN_WAIT_MS)
        ):
            _LOG.error("Discovery worker did not stop before the shutdown deadline")
        if (
            self._update_thread is not None
            and not self._update_thread.wait(self._UPDATE_SHUTDOWN_WAIT_MS)
        ):
            _LOG.error("Update worker did not stop before the shutdown deadline")

    def _persist_shutdown_state(self) -> None:
        if self._transaction_task is None:
            try:
                self.sessions.accept_all()
            except (OSError, ValueError):
                _LOG.exception("Session manifests could not be accepted during shutdown")
        try:
            save_config(self.config)
        except (OSError, ValueError):
            _LOG.exception("Configuration could not be saved during shutdown")
