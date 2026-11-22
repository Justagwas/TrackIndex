from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from trackindex.core.loudness_analysis import LoudnessAnalysisService, path_key
from trackindex.ui.track_list import TRACK_ROLE

if TYPE_CHECKING:
    from .playback_controller import PlaybackController


class LoudnessController(QObject):
    def __init__(self, playback: PlaybackController) -> None:
        super().__init__(playback)
        self.playback = playback
        self._service: LoudnessAnalysisService | None = None
        self._saving = False
        self._stopped = False
        self._invalidated = False
        self._backend_status: dict = {}
        self._analysis_status: dict = {}
        self._failures: set[str] = set()
        self._paths: set[str] = set()
        self._file_stamps: dict[str, tuple[int, int]] = {}
        self._results: dict[str, dict] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.setInterval(50)
        self._publish_timer.timeout.connect(self._publish_results)
        window = playback.window
        window.librarySavingChanged.connect(self._saving_changed)
        model = window.track_table.track_model
        model.modelReset.connect(self.schedule_refresh)
        model.dataChanged.connect(self._model_data_changed)

    def schedule_refresh(self, *_args) -> None:
        if not self._stopped and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _model_data_changed(self, _first, _last, roles) -> None:
        if TRACK_ROLE in roles:
            self.schedule_refresh()

    def track_changed(self) -> None:
        self._backend_status = {}
        self.schedule_refresh()
        self.show_status()

    def _ensure_service(self) -> LoudnessAnalysisService:
        if self._service is None:
            gate = getattr(self.playback.app_controller, "_filesystem_gate", None)
            self._service = LoudnessAnalysisService(
                filesystem_access=gate.read if gate is not None else nullcontext,
                parent=self,
            )
            self._service.resultReady.connect(self._result_ready)
            self._service.statusChanged.connect(self._status_changed)
        return self._service

    def refresh(self) -> None:
        self._refresh_timer.stop()
        p = self.playback
        if self._stopped:
            return
        enabled = (p._available and p.config.audio.normalization != "off"
                   and p.config.audio.analysis_enabled)
        candidates = p.queue.upcoming_entries(8) if enabled else ()
        selected = tuple(dict.fromkeys(path_key(entry.path) for entry in candidates))
        self._paths = set(selected)
        self._failures.intersection_update(self._paths)
        self._results = {key: value for key, value in self._results.items() if key in self._paths}
        if not enabled or self._saving or not selected:
            if self._service is not None:
                self._service.set_paused(True)
            self.show_status()
            return
        service = self._ensure_service()
        self._file_stamps = {key: stamp for key, stamp in self._file_stamps.items() if key in self._paths}
        model = p.window.track_table.track_model
        changed = []
        for entry in candidates:
            track = model.track_for_id(entry.track_id)
            if track is None or track.path != entry.path:
                continue
            key = path_key(entry.path)
            stamp = (track.size, track.mtime_ns)
            if key in self._file_stamps and self._file_stamps[key] != stamp:
                changed.append(key)
                self._results.pop(key, None)
                self._failures.discard(key)
            self._file_stamps[key] = stamp
        if self._invalidated:
            service.invalidate()
            self._results.clear()
            self._invalidated = False
        elif changed:
            service.invalidate(changed)
        service.set_paths(tuple(Path(key) for key in selected),
                          priority=tuple(Path(key) for key in selected[:2]),
                          policy=p.config.audio.normalization)
        self._results.update(service.snapshot())
        service.set_paused(False)
        self._publish_results()

    def _saving_changed(self, saving: bool) -> None:
        self._saving = bool(saving)
        if saving:
            self._refresh_timer.stop()
            self._publish_timer.stop()
            self._invalidated = True
            if self._service is not None:
                self._service.set_paused(True)
            self._results.clear()
            self._paths.clear()
            self._failures.clear()
            self.playback.commandRequested.emit("loudness", {})
        else:
            self.schedule_refresh()
        self.show_status()

    def _result_ready(self, path: str, result: object) -> None:
        key = path_key(path)
        if self._stopped or self._saving or key not in self._paths:
            return
        value = result.to_dict() if hasattr(result, "to_dict") else result
        if isinstance(value, dict):
            self._results[key] = value
            self._failures.discard(key)
            if not self._publish_timer.isActive():
                self._publish_timer.start()
        self.show_status()

    def _status_changed(self, status: object) -> None:
        if not self._stopped and isinstance(status, dict):
            self._analysis_status = status
            key = path_key(status.get("path") or ".")
            if status.get("state") == "failed" and key in self._paths:
                self._failures.add(key)
            self.show_status()

    def _publish_results(self) -> None:
        self._publish_timer.stop()
        if not self._stopped and not self._saving:
            self.playback.commandRequested.emit("loudness", dict(self._results))

    def backend_status(self, value: object) -> None:
        if isinstance(value, dict):
            self._backend_status = value
            entry = self.playback.snapshot.entry
            if (value.get("state") == "pending" and entry is not None
                    and self.playback.config.audio.analysis_enabled and self._service is not None):
                key = path_key(entry.path)
                if (key in self._results or key in self._service.snapshot()
                        or key in self._service.tagged_snapshot()):
                    self._service.invalidate((entry.path,))
                    self._results.pop(key, None)
                    self.schedule_refresh()
            self.show_status()

    def show_status(self) -> None:
        panel = self.playback._audio_panel
        if panel is not None:
            panel.set_normalization_status(self.status_text())

    def status_text(self) -> str:
        p = self.playback
        mode = p.config.audio.normalization
        if mode == "off":
            return "Normalization is off."
        if self._saving:
            return "Background analysis is paused while library changes are saved."
        if p.snapshot.entry is None:
            return ("Missing loudness data is analyzed ahead of playback and saved locally."
                    if p.config.audio.analysis_enabled else "Play a song to see its loudness adjustment.")
        status = self._backend_status
        if status.get("mode") == mode and status.get("state") == "applied":
            gain = status.get("gain_db", 0.0)
            if isinstance(gain, (int, float)):
                source = {"analysis": "measured loudness", "album-tags": "album tags",
                          "track-tags": "track tags", "track-fallback": "track tags; album data unavailable",
                          "analysis-fallback": "measured track loudness; album data unavailable"}
                return f"Current song: {gain:+.1f} dB ({source.get(status.get('source'), 'loudness adjustment')})."
        key = path_key(p.snapshot.entry.path)
        result = self._results.get(key)
        if result is not None:
            if result.get("integrated_lufs") is None:
                return "No measurable loudness for this song; no normalization gain is applied."
            return "Analysis ready. Its loudness adjustment applies the next time this song starts."
        if key in self._failures:
            return "This song could not be analyzed; no normalization gain is applied. Other songs will still be checked."
        if not p.config.audio.analysis_enabled:
            return str(status.get("text") or "No loudness data is available for this song. Automatic analysis is off.")
        if self._analysis_status.get("state") == "analyzing" and path_key(self._analysis_status.get("path") or ".") == key:
            return "Analyzing this song. Its adjustment applies the next time it starts."
        return "No loudness data yet. Playback continues while upcoming songs are analyzed."

    def shutdown(self) -> None:
        self._stopped = True
        self._refresh_timer.stop()
        self._publish_timer.stop()
        if self._service is not None:
            self._service.shutdown()

    def drained(self) -> bool:
        return self._service is None or self._service.wait_for_done(0)
