from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThreadPool, QTimer, Slot

from trackindex.core.track_search import TrackSearchIndex
from trackindex.workers.search_worker import SearchBatch, SearchMetadataTask

if TYPE_CHECKING:
    from .app_controller import AppController


class TrackFinder(QObject):
    def __init__(self, controller: AppController) -> None:
        super().__init__(controller)
        self.controller = controller
        self.window = controller.window
        self.index = TrackSearchIndex()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._revision = 0
        self._task: SearchMetadataTask | None = None
        self._opened = False
        self._current_id = ""
        self._matches: tuple[str, ...] = ()
        self._query = ""
        self._needs_initial_selection = False
        self._query_timer = QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.setInterval(100)
        self._query_timer.timeout.connect(self._query_changed)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)
        self._results_timer = QTimer(self)
        self._results_timer.setSingleShot(True)
        self._results_timer.setInterval(80)
        self._results_timer.timeout.connect(self._update_results)
        self.window.findRequested.connect(self.open)
        self.window.findQueryChanged.connect(self._schedule_query)
        self.window.findNavigateRequested.connect(self.navigate)
        self.window.findClosed.connect(self.close)
        self.window.librarySavingChanged.connect(self._saving_changed)
        model = self.window.track_table.track_model
        model.modelReset.connect(self._model_changed)
        model.layoutChanged.connect(self._model_changed)
        model.rowsInserted.connect(self._model_changed)
        model.rowsRemoved.connect(self._model_changed)
        model.dataChanged.connect(self._metadata_changed)
        self.window.set_find_available(False)

    def _available(self) -> bool:
        c = self.controller
        return (not c._shutdown_started and not c._metadata_paused
                and not c._operation_busy() and c._audit_task is None
                and not c._setup_active and c.audit is not None
                and self.window.track_table.isEnabled())

    @Slot(bool)
    def _saving_changed(self, saving: bool) -> None:
        if saving:
            self.invalidate()
        else:
            self.refresh()

    def invalidate(self, *, disable: bool = True) -> None:
        self._revision += 1
        if self._task is not None:
            self._task.stop()
            self._task = None
        self.pool.clear()
        self._query_timer.stop()
        self._results_timer.stop()
        self._refresh_timer.stop()
        self._matches = ()
        if disable:
            self.window.set_find_available(False)

    def stop(self) -> None:
        self._opened = False
        self.invalidate()

    def _model_changed(self, *_args) -> None:
        self.invalidate()
        if not self.controller._shutdown_started:
            self._refresh_timer.start()

    def refresh(self) -> None:
        query = self.window.find_bar.query
        if query != self._query:
            self._query = query
            self._current_id = ""
            self._needs_initial_selection = bool(query.strip())
        self.invalidate(disable=False)
        available = self._available()
        self.window.set_find_available(available)
        if not available:
            self.window.set_find_results(0, 0)
            return
        if not self._opened:
            return
        model = self.window.track_table.track_model
        values = dict(model.metadata)
        for track in model.tracks:
            if track.track_id not in values:
                cached = self.controller.metadata_cache.get(track.path, track.size, track.mtime_ns)
                if cached is not None:
                    values[track.track_id] = cached
        self.index.reset(tuple(model.tracks), values)
        if self._opened:
            self._start_metadata()
            self._update_results()

    @Slot()
    def open(self) -> None:
        self._opened = True
        self._query = self.window.find_bar.query
        self._needs_initial_selection = bool(self._query.strip())
        self.refresh()

    @Slot()
    def close(self) -> None:
        self._opened = False
        self.invalidate()
        self.window.set_find_available(self._available())

    @Slot(str)
    def _schedule_query(self, _query: str) -> None:
        self._query_timer.start()

    def _query_changed(self) -> None:
        self._query = self.window.find_bar.query
        self._current_id = ""
        self._needs_initial_selection = bool(self._query.strip())
        if not self._query.strip() and self._task is not None:
            self._task.stop()
            self._task = None
            self.pool.clear()
        if self._opened and self._available():
            self._start_metadata()
            self._update_results()

    def _start_metadata(self) -> None:
        if self._task is not None or not self._query.strip() or not self._available():
            return
        missing = self.index.missing_tracks()
        if not missing:
            return
        task = SearchMetadataTask(self._revision, missing, self.controller._filesystem_gate.read)
        task.signals.ready.connect(self._metadata_ready)
        self._task = task
        self.pool.start(task)

    @Slot(object)
    def _metadata_ready(self, batch: SearchBatch) -> None:
        if (not self._opened or not self._available() or self._task is None
                or batch.task_id != self._task.task_id or batch.revision != self._revision):
            return
        for track, metadata in batch.values:
            self.index.put(track, metadata)
        if batch.finished:
            self._task = None
        if not self._results_timer.isActive():
            self._results_timer.start()

    def _metadata_changed(self, first, last, roles=()) -> None:
        if not self._opened or not self._available():
            return
        model = self.window.track_table.track_model
        if roles:
            self._model_changed()
            return
        for track in model.tracks[first.row():last.row() + 1]:
            metadata = model.metadata.get(track.track_id)
            if metadata is not None:
                self.index.put(track, metadata)
        if not self._results_timer.isActive():
            self._results_timer.start()

    def _update_results(self) -> None:
        if not self._opened or not self._available() or self._query_timer.isActive():
            return
        self._matches = self.index.matches(self._query)
        if self._current_id not in self._matches:
            self._current_id = ""
        if self._needs_initial_selection and self._matches:
            if self.window.select_find_track(self._matches[0]):
                self._current_id = self._matches[0]
                self._needs_initial_selection = False
        current = self._matches.index(self._current_id) + 1 if self._current_id in self._matches else 0
        self.window.set_find_results(current, len(self._matches), pending=self._task is not None)

    @Slot(int)
    def navigate(self, direction: int) -> None:
        if self._query_timer.isActive():
            self._query_timer.stop()
            self._query_changed()
        else:
            self._update_results()
        if not self._matches:
            return
        if self._current_id in self._matches:
            position = (self._matches.index(self._current_id) + direction) % len(self._matches)
        else:
            position = 0 if direction > 0 else len(self._matches) - 1
        match = self._matches[position]
        if self.window.select_find_track(match):
            self._current_id = match
            self._needs_initial_selection = False
            self._update_results()
