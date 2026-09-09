from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit, QPlainTextEdit, QTextEdit

from trackindex.core.config import SUPPORTED_AUDIO_EXTENSIONS
from trackindex.core.config_service import AppConfig, save_config
from trackindex.core.diagnostics import diagnostics_logger
from trackindex.core.models import (
    ApplyResult,
    AuditResult,
    ChangePlan,
    LibraryRecord,
    TrackMetadata,
)
from trackindex.core.playback import (
    PlaybackQueue,
    PlaybackSnapshot,
    PlaybackState,
    PlayerPresentation,
    QueueEntry,
    RepeatMode,
    SourceAudioInfo,
)
from trackindex.core.playback_backend import BackendEvent, PlaybackBackend, create_playback_backend
from trackindex.core.threading_contract import assert_qt_thread_affinity
from trackindex.ui.main_window import MainWindow

from .system_media import MediaContext, MediaPublication, SystemMediaBridge

_LOG = diagnostics_logger("playback")


class PlaybackLibraryCoordinator(Protocol):
    current_library: LibraryRecord | None
    audit: AuditResult | None

    def set_playback_coordinator(self, coordinator: PlaybackController) -> None: ...

    def library(self, library_id: str) -> LibraryRecord | None: ...

    def library_loading(self) -> bool: ...

    def select_library(self, library_id: str) -> None: ...


class PlaybackWorker(QObject):
    eventReady = Signal(object)
    finished = Signal()

    def __init__(self, backend: PlaybackBackend | None = None) -> None:
        super().__init__()
        self.backend = backend or create_playback_backend()
        self._stopping = False

    @Slot()
    def start(self) -> None:
        assert_qt_thread_affinity(self)
        try:
            self.backend.initialize(self.eventReady.emit)
        except Exception as exc:
            _LOG.exception("Playback backend initialization failed")
            self.eventReady.emit(BackendEvent("unavailable", str(exc)))

    @Slot(str, object)
    def execute(self, command: str, value: object = None) -> None:
        assert_qt_thread_affinity(self)
        if self._stopping and command != "shutdown":
            return
        generation = 0
        try:
            generation = self._execute_command(command, value)
        except Exception as exc:
            _LOG.exception("Playback backend command failed: %s", command)
            if command == "load":
                self.eventReady.emit(BackendEvent("error", str(exc), int(generation)))
            elif command == "prepare-mutation":
                self.eventReady.emit(BackendEvent("mutation-failed", (str(value), str(exc))))
            elif command == "recover-output":
                try:
                    generation = int(str(value))
                except (TypeError, ValueError):
                    generation = 0
                self.eventReady.emit(
                    BackendEvent("output-recovery-failed", str(exc), generation)
                )
            else:
                self.eventReady.emit(BackendEvent("command-error", (command, str(exc))))

    def _execute_command(self, command: str, value: object) -> int:
        handlers: dict[str, Callable[[object], int]] = {
            "load": self._execute_load,
            "lookahead": self._execute_lookahead,
            "pause": self._execute_pause,
            "seek": self._execute_seek,
            "volume": self._execute_volume,
            "mute": self._execute_mute,
            "recover-output": self._execute_output_recovery,
            "stop": self._execute_stop,
            "prepare-mutation": self._execute_mutation_preparation,
            "shutdown": self._execute_shutdown,
        }
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f"Unknown playback command: {command}")
        return handler(value)

    def _execute_load(self, value: object) -> int:
        if not isinstance(value, (tuple, list)) or len(value) != 3:
            raise ValueError("Playback load command is malformed.")
        raw_path, paused, raw_generation = value
        if not isinstance(raw_path, (str, Path)):
            raise ValueError("Playback path is malformed.")
        generation = int(raw_generation)
        self.backend.load(
            Path(raw_path),
            paused=bool(paused),
            generation=generation,
        )
        return generation

    def _execute_lookahead(self, value: object) -> int:
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                raise ValueError("Playback lookahead command is malformed.")
            path, raw_generation = value
            generation = int(raw_generation)
            if path is not None and not isinstance(path, (str, Path)):
                raise ValueError("Playback lookahead path is malformed.")
            self.backend.set_lookahead(
                Path(path) if path else None,
                generation=generation,
            )
            return generation
        if value is not None and not isinstance(value, (str, Path)):
            raise ValueError("Playback lookahead path is malformed.")
        self.backend.set_lookahead(Path(value) if value else None)
        return 0

    def _execute_pause(self, value: object) -> int:
        self.backend.set_paused(bool(value))
        return 0

    def _execute_seek(self, value: object) -> int:
        seconds = _finite_float(value)
        if seconds is None:
            raise ValueError("Playback seek position is malformed.")
        self.backend.seek(seconds)
        return 0

    def _execute_volume(self, value: object) -> int:
        self.backend.set_volume(int(str(value)))
        return 0

    def _execute_mute(self, value: object) -> int:
        self.backend.set_muted(bool(value))
        return 0

    def _execute_output_recovery(self, value: object) -> int:
        generation = int(str(value))
        self.backend.recover_output()
        self.eventReady.emit(BackendEvent("output-recovered", None, generation))
        return generation

    def _execute_stop(self, _value: object) -> int:
        self.backend.stop()
        return 0

    def _execute_mutation_preparation(self, value: object) -> int:
        token = str(value)
        self.backend.set_lookahead(None)
        self.eventReady.emit(BackendEvent("mutation-ready", token))
        return 0

    def _execute_shutdown(self, _value: object) -> int:
        self._stopping = True
        try:
            self.backend.shutdown()
        finally:
            self.finished.emit()
        return 0


class PlaybackController(QObject):
    commandRequested = Signal(str, object)

    def __init__(
        self,
        app: QApplication,
        window: MainWindow,
        app_controller: PlaybackLibraryCoordinator,
        config: AppConfig,
        backend: PlaybackBackend | None = None,
    ) -> None:
        super().__init__(window)
        self.app = app
        self.window = window
        self.app_controller = app_controller
        self.config = config
        self.queue = PlaybackQueue(
            shuffle=config.playback_shuffle,
            repeat=RepeatMode(config.playback_repeat),
        )
        self.snapshot = PlaybackSnapshot(
            volume=config.playback_volume,
            muted=config.playback_muted,
            shuffle=config.playback_shuffle,
            repeat=RepeatMode(config.playback_repeat),
        )
        self._available = False
        self._availability_error = ""
        self._play_requested_before_ready = False
        self._intentional_stop = False
        self._stopping = False
        self._error_attempts = 0
        self._output_recovery_attempts = 0
        self._output_recovery_entry_id = ""
        self._output_recovery_message = ""
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
        self._next_backend_generation = 1
        self._active_backend_generation = 0
        self._awaiting_prefetched_load = False
        self._ignore_ended_until_loaded = False
        self._play_after_load = True
        self._seek_after_load: float | None = None
        self._mutation_callbacks: dict[
            str,
            tuple[Callable[[], None], Callable[[str], None] | None],
        ] = {}
        self._mutation_current_id = ""
        self._mutation_position = 0.0
        self._mutation_was_playing = False
        self._mutation_active = False
        self._mutation_unloaded_current = False
        self._mutation_successor = ""
        self._mutation_library_id = ""
        self._reveal_request = 0
        self._config_timer = QTimer(self)
        self._config_timer.setSingleShot(True)
        self._config_timer.setInterval(250)
        self._config_timer.timeout.connect(self._save_preferences)
        self._pending_position: float | None = None
        self._position_event_timer = QTimer(self)
        self._position_event_timer.setSingleShot(True)
        self._position_event_timer.setInterval(50)
        self._position_event_timer.timeout.connect(self._flush_backend_position)
        self._shortcuts: list[QShortcut] = []
        self._media_shortcuts: list[QShortcut] = []
        self._playback_thread = QThread(window)
        self.worker = PlaybackWorker(backend)
        self.worker.moveToThread(self._playback_thread)
        self._playback_thread.started.connect(self.worker.start)
        self.commandRequested.connect(self.worker.execute, Qt.ConnectionType.QueuedConnection)
        self.worker.eventReady.connect(self._backend_event, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._playback_thread.quit, Qt.ConnectionType.DirectConnection)
        self._playback_thread.finished.connect(self.worker.deleteLater)
        self._connect_ui()
        self._install_shortcuts()
        self.app.installEventFilter(self)
        self.app_controller.set_playback_coordinator(self)
        self.window.player_panel.set_snapshot(self.snapshot)
        self._playback_thread.start()
        self._system_media: SystemMediaBridge | None = None
        if backend is None and self.app.platformName() == "windows":
            self._system_media = SystemMediaBridge(
                lambda: int(self.window.winId()), self._media_publication, self,
            )
            self._system_media.commandRequested.connect(
                self._system_media_command, Qt.ConnectionType.QueuedConnection,
            )
            self._system_media.availabilityChanged.connect(self._system_media_available)
            QTimer.singleShot(0, self._system_media.start)

    def _media_context(self) -> MediaContext:
        entry = self.snapshot.entry
        return (
            self._active_backend_generation,
            entry.library_id if entry else "",
            entry.track_id if entry else "",
        )

    def _media_publication(self) -> MediaPublication:
        return self.snapshot, self._media_context(), not self._stopping and not self._mutation_library_id

    @Slot(str, object, object)
    def _system_media_command(self, command: str, value: object, context: object) -> None:
        if context == self._media_context():
            self.dispatch_transport(command, value)

    @Slot(bool)
    def _system_media_available(self, active: bool) -> None:
        for shortcut in self._media_shortcuts:
            shortcut.setEnabled(not active and not self._stopping)

    def _connect_ui(self) -> None:
        panel = self.window.player_panel
        table = self.window.track_table
        table.playbackRequested.connect(self.start_track)
        table.dragStateChanged.connect(panel.set_drag_compact)
        panel.playPauseRequested.connect(self.toggle_play_pause)
        panel.previousRequested.connect(self.previous)
        panel.nextRequested.connect(self.next)
        panel.stopRequested.connect(self.stop)
        panel.seekRequested.connect(self.seek)
        panel.volumeChanged.connect(self.set_volume)
        panel.muteChanged.connect(self.set_muted)
        panel.shuffleChanged.connect(self.set_shuffle)
        panel.repeatChanged.connect(self.set_repeat)
        panel.closeRequested.connect(self.close)
        panel.presentationRequested.connect(self.set_presentation)
        panel.currentTrackRequested.connect(self.reveal_current_track)

    def _install_shortcuts(self) -> None:
        bindings = (
            (Qt.Key.Key_MediaTogglePlayPause, "toggle"),
            (Qt.Key.Key_MediaPlay, "play"),
            (Qt.Key.Key_MediaPause, "pause"),
            (Qt.Key.Key_MediaNext, "next"),
            (Qt.Key.Key_MediaPrevious, "previous"),
            (Qt.Key.Key_MediaStop, "stop"),
        )
        for key, command in bindings:
            shortcut = QShortcut(QKeySequence(key), self.window)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda command=command: self.dispatch_transport(command))
            self._media_shortcuts.append(shortcut)
            self._shortcuts.append(shortcut)
        for keys, command in (
            ("Ctrl+Space", "toggle"), ("Ctrl+Right", "next"), ("Ctrl+Left", "previous"),
            ("Ctrl+Shift+Right", "seek-forward"), ("Ctrl+Shift+Left", "seek-backward"),
            ("Ctrl+Up", "volume-up"), ("Ctrl+Down", "volume-down"), ("Ctrl+M", "mute"),
            ("Ctrl+Alt+S", "shuffle"), ("Ctrl+Alt+R", "repeat"), ("Ctrl+Alt+X", "stop"),
        ):
            shortcut = QShortcut(QKeySequence(keys), self.window)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda command=command: self._keyboard_command(command))
            self._shortcuts.append(shortcut)
        space = QShortcut(QKeySequence(Qt.Key.Key_Space), self.window.track_table)
        space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        space.setAutoRepeat(False)
        space.activated.connect(self._space_pressed)
        self._shortcuts.append(space)
        panel = self.window.player_panel
        for button, hint in (
            (panel.play_button, "Play or pause (Ctrl+Space)"),
            (panel.mini_play_button, "Play or pause (Ctrl+Space)"),
            (panel.previous_button, "Previous (Ctrl+Left)"),
            (panel.mini_previous_button, "Previous (Ctrl+Left)"),
            (panel.next_button, "Next (Ctrl+Right)"),
            (panel.mini_next_button, "Next (Ctrl+Right)"),
            (panel.mute_button, "Mute or unmute (Ctrl+M)"),
            (panel.shuffle_button, "Shuffle (Ctrl+Alt+S)"),
        ):
            button.setToolTip(hint)

    def _space_pressed(self) -> None:
        focus = self.app.focusWidget()
        table = self.window.track_table
        if focus is None or focus is table or focus is table.viewport():
            self.dispatch_transport("toggle")

    def _keyboard_command(self, command: str) -> None:
        focus = self.app.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)):
            return
        if self.app.activeModalWidget() is None and self.app.activePopupWidget() is None:
            self.dispatch_transport(command)

    def eventFilter(self, watched, event) -> bool:
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and isinstance(watched, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox))
            and watched.window() is self.window
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def dispatch_transport(self, command: str, value: object = None) -> None:
        if self._stopping or self._mutation_library_id:
            return
        if command in {"toggle", "play", "pause"}:
            if self.snapshot.entry is None and command != "pause":
                rows = self.window.track_table.selectionModel().selectedRows()
                if rows:
                    tracks = self.window.track_table.track_model.tracks
                    self.start_track(tracks[rows[0].row()].track_id)
                    return
            if self.snapshot.loading:
                paused = command == "pause" or (command == "toggle" and self._play_after_load)
                self._play_after_load = not paused
                self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED if paused else PlaybackState.LOADING)
                self.window.player_panel.set_snapshot(self.snapshot)
                self.commandRequested.emit("pause", paused)
            elif command == "toggle" or (
                command == "play" and self.snapshot.state is not PlaybackState.PLAYING
            ) or (command == "pause" and self.snapshot.state is PlaybackState.PLAYING):
                self.toggle_play_pause()
            return
        handlers: dict[str, Callable[[], None]] = {
            "next": self.next, "previous": self.previous, "stop": self.stop,
            "seek-forward": lambda: self.seek(self.snapshot.position_seconds + 5),
            "seek-backward": lambda: self.seek(self.snapshot.position_seconds - 5),
            "volume-up": lambda: self.set_volume(self.snapshot.volume + 5),
            "volume-down": lambda: self.set_volume(self.snapshot.volume - 5),
            "mute": lambda: self.set_muted(not self.snapshot.muted),
            "shuffle": lambda: self.set_shuffle(not self.snapshot.shuffle),
            "repeat": self.window.player_panel._cycle_repeat,
        }
        if command == "seek":
            seconds = _finite_float(value)
            if seconds is not None:
                self.seek(seconds)
        elif command in handlers:
            handlers[command]()
            self.window.player_panel.set_snapshot(self.snapshot)

    def _entries_for_current_library(self) -> tuple[QueueEntry, ...]:
        record = self.app_controller.current_library
        audit = self.app_controller.audit
        if record is None or audit is None:
            return ()
        track_map = audit.track_map()
        metadata_map = self.window.track_table.track_model.metadata
        entries: list[QueueEntry] = []
        for track_id in self.window.track_table.track_model.ordered_track_ids():
            track = track_map.get(track_id)
            if track is None:
                continue
            metadata = metadata_map.get(track_id)
            entries.append(self._queue_entry(record.library_id, track, metadata))
        return tuple(entries)

    @staticmethod
    def _queue_entry(library_id, track, metadata: TrackMetadata | None) -> QueueEntry:
        source = SourceAudioInfo(
            extension=track.path.suffix.removeprefix(".").casefold(),
            container=metadata.container if metadata else track.path.suffix.removeprefix(".").upper(),
            codec=metadata.codec if metadata else "",
            bitrate=metadata.bitrate if metadata else None,
            sample_rate=metadata.sample_rate if metadata else None,
            bit_depth=metadata.bit_depth if metadata else None,
            channels=metadata.channels if metadata else None,
            channel_layout=metadata.channel_layout if metadata else "",
        )
        return QueueEntry(
            library_id=library_id,
            track_id=track.track_id,
            path=track.path,
            title=metadata.title if metadata else track.base_stem,
            artist=metadata.artist if metadata else "",
            album=metadata.album if metadata else "",
            duration_seconds=metadata.duration_seconds if metadata else None,
            artwork_bytes=metadata.artwork_bytes if metadata else None,
            source=source,
        )

    @Slot(str)
    def start_track(self, track_id: str) -> None:
        if self._stopping:
            return
        if self._availability_error:
            self.window.show_toast("Playback runtime unavailable", None)
            return
        if (
            self.snapshot.entry is not None
            and self.snapshot.entry.track_id == track_id
            and self.snapshot.state in {PlaybackState.PLAYING, PlaybackState.PAUSED}
        ):
            self.toggle_play_pause()
            return
        entries = self._entries_for_current_library()
        entry = self.queue.start(entries, track_id)
        if entry is None or not self._valid_entry(entry):
            self.window.show_toast("This track is no longer available", None)
            return
        self._error_attempts = 0
        self._play_requested_before_ready = True
        self._load_entry(entry)

    @staticmethod
    def _valid_entry(entry: QueueEntry) -> bool:
        return entry.path.suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS

    def _load_entry(self, entry: QueueEntry, *, paused: bool = False) -> None:
        self._intentional_stop = False
        self._awaiting_prefetched_load = False
        self._ignore_ended_until_loaded = True
        self._play_after_load = not paused
        state = PlaybackState.PAUSED if paused else PlaybackState.LOADING
        self.snapshot = replace(
            self.snapshot,
            state=state,
            presentation=PlayerPresentation.EXPANDED,
            entry=entry,
            position_seconds=0.0,
            duration_seconds=entry.duration_seconds,
            loading=not paused,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.player_panel.set_presentation(PlayerPresentation.EXPANDED)
        self.window.track_table.set_playback_state(entry.track_id, not paused)
        generation = self._new_backend_generation()
        self._active_backend_generation = generation
        self.commandRequested.emit("load", (str(entry.path), paused, generation))
        self._refresh_lookahead()

    def _new_backend_generation(self) -> int:
        generation = self._next_backend_generation
        self._next_backend_generation += 1
        return generation

    @Slot()
    def toggle_play_pause(self) -> None:
        if self.snapshot.entry is None:
            return
        entry = self.snapshot.entry
        if self.snapshot.state in {PlaybackState.STOPPED, PlaybackState.ERROR}:
            if not self._valid_entry(entry):
                self._handle_entry_error(
                    entry,
                    "The audio file is no longer available.",
                )
                return
            self.queue.current_id = entry.track_id
            self._load_entry(entry)
            return
        paused = self.snapshot.state is PlaybackState.PLAYING
        state = PlaybackState.PAUSED if paused else PlaybackState.PLAYING
        self.snapshot = replace(self.snapshot, state=state, loading=False)
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.track_table.set_playback_state(entry.track_id, not paused)
        self.commandRequested.emit("pause", paused)

    @Slot()
    def next(self, *, natural: bool = False) -> None:
        entry = self.queue.advance(natural=natural)
        if entry is None:
            self.stop()
            return
        if not self._valid_entry(entry):
            self._handle_entry_error(entry, "The audio file is missing.")
            return
        if natural and entry.track_id == self._backend_lookahead_id:
            self._adopt_prefetched_entry(entry)
            return
        self._load_entry(entry)

    def _adopt_prefetched_entry(self, entry: QueueEntry) -> None:
        self._backend_lookahead_id = ""
        self._active_backend_generation = self._backend_lookahead_generation
        self._backend_lookahead_generation = 0
        self._awaiting_prefetched_load = True
        self._ignore_ended_until_loaded = True
        self._play_after_load = True
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.LOADING,
            presentation=PlayerPresentation.EXPANDED,
            entry=entry,
            position_seconds=0.0,
            duration_seconds=entry.duration_seconds,
            loading=True,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.track_table.set_playback_state(entry.track_id, True)

    @Slot()
    def previous(self) -> None:
        if self.snapshot.position_seconds >= 3:
            self.seek(0.0)
            return
        entry = self.queue.previous()
        if entry is not None and self._valid_entry(entry):
            self._load_entry(entry)

    @Slot()
    def stop(self) -> None:
        if self.snapshot.entry is None:
            return
        entry = self.snapshot.entry
        self._intentional_stop = True
        self._awaiting_prefetched_load = False
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
        self._output_recovery_attempts = 0
        self._output_recovery_entry_id = ""
        self._output_recovery_message = ""
        self._active_backend_generation = self._new_backend_generation()
        self.commandRequested.emit("stop", None)
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.STOPPED,
            presentation=PlayerPresentation.MINI,
            position_seconds=0.0,
            loading=False,
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.player_panel.set_presentation(PlayerPresentation.MINI)
        self.window.track_table.set_playback_state(entry.track_id, False)

    @Slot()
    def close(self) -> None:
        self._intentional_stop = True
        self._awaiting_prefetched_load = False
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
        self._output_recovery_attempts = 0
        self._output_recovery_entry_id = ""
        self._output_recovery_message = ""
        self._active_backend_generation = self._new_backend_generation()
        self.commandRequested.emit("stop", None)
        self.queue.clear()
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.IDLE,
            presentation=PlayerPresentation.HIDDEN,
            entry=None,
            position_seconds=0.0,
            duration_seconds=None,
            loading=False,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.player_panel.set_presentation(PlayerPresentation.HIDDEN)
        self.window.track_table.set_playback_state("", False)

    @Slot(float)
    def seek(self, seconds: float) -> None:
        if self.snapshot.entry is None:
            return
        converted = _finite_float(seconds)
        if converted is None:
            return
        value = max(0.0, converted)
        if self.snapshot.duration_seconds is not None:
            value = min(value, self.snapshot.duration_seconds)
        self.snapshot = replace(self.snapshot, position_seconds=value)
        self.window.player_panel.set_snapshot(self.snapshot)
        self.commandRequested.emit("seek", value)

    @Slot(int)
    def set_volume(self, volume: int) -> None:
        value = max(0, min(100, int(volume)))
        self.snapshot = replace(self.snapshot, volume=value)
        self.config.playback_volume = value
        self.commandRequested.emit("volume", value)
        self._config_timer.start()

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        value = bool(muted)
        self.snapshot = replace(self.snapshot, muted=value)
        self.config.playback_muted = value
        self.window.player_panel.set_snapshot(self.snapshot)
        self.commandRequested.emit("mute", value)
        self._config_timer.start()

    @Slot(bool)
    def set_shuffle(self, enabled: bool) -> None:
        self.queue.set_shuffle(enabled)
        self.snapshot = replace(self.snapshot, shuffle=bool(enabled))
        self.config.playback_shuffle = bool(enabled)
        self.window.player_panel.set_snapshot(self.snapshot)
        self._refresh_lookahead()
        self._config_timer.start()

    @Slot(str)
    def set_repeat(self, value: str) -> None:
        mode = RepeatMode(value)
        self.queue.set_repeat(mode)
        self.snapshot = replace(self.snapshot, repeat=mode)
        self.config.playback_repeat = mode.value
        self.window.player_panel.set_snapshot(self.snapshot)
        self._refresh_lookahead()
        self._config_timer.start()

    @Slot(str)
    def set_presentation(self, value: str) -> None:
        presentation = PlayerPresentation(value)
        self.snapshot = replace(self.snapshot, presentation=presentation)
        self.window.player_panel.set_presentation(presentation)

    def _refresh_lookahead(self) -> None:
        entry = self.queue.lookahead
        if entry is not None and self._valid_entry(entry):
            self._backend_lookahead_id = entry.track_id
            generation = self._new_backend_generation()
            value: tuple[str | None, int] = (str(entry.path), generation)
        else:
            self._backend_lookahead_id = ""
            generation = 0
            value = (None, 0)
        self._backend_lookahead_generation = generation
        self.commandRequested.emit("lookahead", value)

    def reveal_current_track(self) -> None:
        entry = self.snapshot.entry
        if entry is None:
            return
        record = self.app_controller.library(entry.library_id)
        if record is None:
            return
        if self.app_controller.current_library is None or self.app_controller.current_library.library_id != entry.library_id:
            self.app_controller.select_library(entry.library_id)

        self._reveal_request += 1
        request = self._reveal_request
        deadline = monotonic() + 10.0

        def reveal() -> None:
            active = self.snapshot.entry
            current_library = self.app_controller.current_library
            if (
                self._stopping
                or request != self._reveal_request
                or active is None
                or active.track_id != entry.track_id
                or active.library_id != entry.library_id
                or current_library is None
                or current_library.library_id != entry.library_id
            ):
                return
            row = self.window.track_table.track_model._row_by_id.get(entry.track_id)
            if row is None:
                if self.app_controller.library_loading() and monotonic() < deadline:
                    QTimer.singleShot(80, reveal)
                return
            index = self.window.track_table.track_model.index(row, 1)
            self.window.track_table.selectRow(row)
            self.window.track_table.scrollTo(index)

        QTimer.singleShot(0, reveal)

    @Slot(object)
    def _backend_event(self, event: BackendEvent) -> None:
        assert_qt_thread_affinity(self)
        if self._stopping:
            return
        if self._stale_backend_event(event):
            return
        if self._handle_worker_event(event):
            return
        if self._handle_output_event(event):
            return
        if not self._apply_playback_event(event):
            return
        self._publish_playback_snapshot()

    def _stale_backend_event(self, event: BackendEvent) -> bool:
        return event.kind in {
            "loading",
            "loaded",
            "pause",
            "position",
            "duration",
            "codec",
            "bitrate",
            "source-params",
            "output-params",
            "device",
            "output-error",
            "output-recovered",
            "output-recovery-failed",
            "ended",
            "error",
        } and event.generation != self._active_backend_generation

    def _handle_worker_event(self, event: BackendEvent) -> bool:
        kind, value = event.kind, event.value
        if kind == "ready":
            self._available = True
            self.commandRequested.emit("volume", self.snapshot.volume)
            self.commandRequested.emit("mute", self.snapshot.muted)
            return True
        if kind == "unavailable":
            self._available = False
            self._availability_error = str(value or "Playback is unavailable.")
            if self._play_requested_before_ready:
                self.window.show_toast("Playback runtime unavailable", None)
                self.close()
            return True
        if kind == "mutation-ready":
            self._mutation_ready(str(value))
            return True
        if kind == "mutation-failed":
            if isinstance(value, tuple) and len(value) == 2:
                self._mutation_failed(str(value[0]), str(value[1]))
            return True
        if kind == "command-error":
            command = value[0] if isinstance(value, tuple) and value else ""
            if command == "lookahead":
                self._backend_lookahead_id = ""
                self._backend_lookahead_generation = 0
            return True
        return False

    def _handle_output_event(self, event: BackendEvent) -> bool:
        kind, value = event.kind, event.value
        if kind == "output-error":
            entry = self.snapshot.entry
            if entry is not None:
                self._begin_output_recovery(
                    entry,
                    str(value or "The audio output device is unavailable."),
                )
            return True
        if kind == "output-recovered":
            entry = self.snapshot.entry
            if entry is not None and entry.track_id == self._output_recovery_entry_id:
                self._load_entry(entry, paused=not self._play_after_load)
            return True
        if kind == "output-recovery-failed":
            self._retry_output_recovery()
            return True
        return False

    def _apply_playback_event(self, event: BackendEvent) -> bool:
        handlers: dict[str, Callable[[object], bool]] = {
            "loading": self._event_loading,
            "loaded": self._event_loaded,
            "pause": self._event_pause,
            "position": self._event_position,
            "duration": self._event_duration,
            "codec": self._event_codec,
            "bitrate": self._event_bitrate,
            "source-params": self._event_source_params,
            "output-params": self._event_output_params,
            "device": self._event_device,
            "ended": self._event_ended,
            "error": self._event_error,
        }
        handler = handlers.get(event.kind)
        return handler(event.value) if handler is not None else False

    def _event_loading(self, _value: object) -> bool:
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.LOADING,
            loading=True,
        )
        return True

    def _event_loaded(self, _value: object) -> bool:
        self._ignore_ended_until_loaded = False
        state = PlaybackState.PLAYING if self._play_after_load else PlaybackState.PAUSED
        self.snapshot = replace(self.snapshot, state=state, loading=False)
        self._play_requested_before_ready = False
        self._error_attempts = 0
        self._output_recovery_attempts = 0
        self._output_recovery_entry_id = ""
        self._output_recovery_message = ""
        self.commandRequested.emit("pause", not self._play_after_load)
        if self._seek_after_load is not None:
            position = self._seek_after_load
            self._seek_after_load = None
            self.snapshot = replace(self.snapshot, position_seconds=position)
            self.commandRequested.emit("seek", position)
        if self._awaiting_prefetched_load:
            self._awaiting_prefetched_load = False
            self._refresh_lookahead()
        return True

    def _event_pause(self, value: object) -> bool:
        if self._ignore_ended_until_loaded or self.snapshot.state is PlaybackState.LOADING:
            return False
        state = PlaybackState.PAUSED if bool(value) else PlaybackState.PLAYING
        self.snapshot = replace(self.snapshot, state=state, loading=False)
        return True

    def _event_position(self, value: object) -> bool:
        pending_position = _finite_float(value)
        if pending_position is None:
            return False
        self._pending_position = max(0.0, pending_position)
        if not self._position_event_timer.isActive():
            self._position_event_timer.start()
        return False

    def _event_duration(self, value: object) -> bool:
        duration = _finite_float(value)
        if value is not None and duration is None:
            return False
        self.snapshot = replace(
            self.snapshot,
            duration_seconds=(
                max(0.0, duration)
                if duration is not None
                else self.snapshot.duration_seconds
            ),
        )
        return True

    def _event_codec(self, value: object) -> bool:
        if self.snapshot.entry is None:
            return False
        self._replace_current_source(
            replace(self.snapshot.entry.source, codec=str(value or ""))
        )
        return True

    def _event_bitrate(self, value: object) -> bool:
        if self.snapshot.entry is None:
            return False
        self._replace_current_source(
            replace(self.snapshot.entry.source, bitrate=_positive_int(value))
        )
        return True

    def _event_source_params(self, value: object) -> bool:
        self._update_source_params(value)
        return True

    def _event_output_params(self, value: object) -> bool:
        self._update_output_params(value)
        return True

    def _event_device(self, value: object) -> bool:
        output = replace(
            self.snapshot.output,
            device=str(value or "System default"),
        )
        self.snapshot = replace(self.snapshot, output=output)
        return True

    def _event_ended(self, _value: object) -> bool:
        if self._ignore_ended_until_loaded or self._mutation_unloaded_current:
            return False
        if self._intentional_stop or self.snapshot.state in {
            PlaybackState.LOADING,
            PlaybackState.IDLE,
        }:
            self._intentional_stop = False
            return False
        self.next(natural=True)
        return False

    def _event_error(self, value: object) -> bool:
        entry = self.snapshot.entry
        if entry is not None:
            self._handle_entry_error(
                entry,
                str(value or "The track could not be played."),
            )
        return False

    def _publish_playback_snapshot(self) -> None:
        self.window.player_panel.set_snapshot(self.snapshot)
        if self.snapshot.entry is not None:
            self.window.track_table.set_playback_state(
                self.snapshot.entry.track_id,
                self.snapshot.state is PlaybackState.PLAYING,
            )

    def _flush_backend_position(self) -> None:
        value = self._pending_position
        self._pending_position = None
        if value is None or self._stopping:
            return
        self.snapshot = replace(self.snapshot, position_seconds=value)
        self.window.player_panel.set_position(value)

    def _replace_current_source(self, source: SourceAudioInfo) -> None:
        entry = self.snapshot.entry
        if entry is None:
            return
        replacement = replace(entry, source=source)
        self.snapshot = replace(self.snapshot, entry=replacement)
        self.queue.entries = tuple(replacement if item.track_id == replacement.track_id else item for item in self.queue.entries)

    def _update_source_params(self, value: object) -> None:
        if self.snapshot.entry is None or not isinstance(value, dict):
            return
        source = self.snapshot.entry.source
        source = replace(
            source,
            sample_rate=_positive_int(value.get("samplerate")) or source.sample_rate,
            channels=_positive_int(value.get("channels")) or source.channels,
            channel_layout=str(value.get("channel-layout") or source.channel_layout),
            bit_depth=_bit_depth(str(value.get("format") or "")) or source.bit_depth,
        )
        self._replace_current_source(source)

    def _update_output_params(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        output = self.snapshot.output
        output = replace(
            output,
            sample_rate=_positive_int(value.get("samplerate")) or output.sample_rate,
            sample_format=str(value.get("format") or output.sample_format),
            channels=_positive_int(value.get("channels")) or output.channels,
            channel_layout=str(value.get("channel-layout") or output.channel_layout),
        )
        self.snapshot = replace(self.snapshot, output=output)

    def _handle_entry_error(self, entry: QueueEntry, message: str) -> None:
        self.queue.mark_unavailable(entry.track_id)
        self._error_attempts += 1
        self.window.show_toast(f"Could not play {entry.title}; skipping", None)
        if self._error_attempts >= max(1, len(self.queue.entries)):
            self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR, error=message, loading=False)
            self.stop()
            return
        self.next(natural=True)

    def _begin_output_recovery(self, entry: QueueEntry, message: str) -> None:
        if self._output_recovery_entry_id != entry.track_id:
            self._output_recovery_attempts = 0
        self._output_recovery_entry_id = entry.track_id
        self._output_recovery_message = message
        self._play_after_load = self.snapshot.state is not PlaybackState.PAUSED
        self._seek_after_load = self.snapshot.position_seconds
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.LOADING,
            loading=True,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self._request_output_recovery()

    def _request_output_recovery(self) -> None:
        if self._stopping or not self._output_recovery_entry_id:
            return
        if self._output_recovery_attempts >= 3:
            self.window.show_toast(self._output_recovery_message, None)
            self.stop()
            return
        self._output_recovery_attempts += 1
        self.commandRequested.emit(
            "recover-output",
            self._active_backend_generation,
        )

    def _retry_output_recovery(self) -> None:
        if self._output_recovery_attempts >= 3:
            self._request_output_recovery()
            return
        generation = self._active_backend_generation
        track_id = self._output_recovery_entry_id

        def retry() -> None:
            if (
                not self._stopping
                and generation == self._active_backend_generation
                and track_id == self._output_recovery_entry_id
            ):
                self._request_output_recovery()

        QTimer.singleShot(250 * self._output_recovery_attempts, retry)

    def prepare_filesystem_mutation(
        self,
        library_id: str,
        plan: ChangePlan,
        callback: Callable[[], None],
        error_callback: Callable[[str], None] | None = None,
    ) -> None:
        active = self.queue.current
        if active is None or active.library_id != library_id:
            QTimer.singleShot(0, callback)
            return
        token = uuid.uuid4().hex
        self._mutation_callbacks[token] = (callback, error_callback)
        self._mutation_library_id = library_id
        self._mutation_current_id = self.queue.current_id
        self._mutation_position = self.snapshot.position_seconds
        self._mutation_was_playing = self.snapshot.state is PlaybackState.PLAYING
        self._mutation_active = self.snapshot.state in {
            PlaybackState.LOADING,
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        }
        self._mutation_successor = self.queue.peek_next_id()
        departing = {
            item.track_id
            for item in plan.presence
            if item.action == "stash"
        }
        self._mutation_unloaded_current = (
            self._mutation_active and self.queue.current_id in departing
        )
        if self._mutation_unloaded_current:
            self._intentional_stop = True
            self._backend_lookahead_id = ""
            self._backend_lookahead_generation = 0
            self._active_backend_generation = self._new_backend_generation()
            self.commandRequested.emit("stop", None)
        self.commandRequested.emit("prepare-mutation", token)
        QTimer.singleShot(
            2000,
            lambda token=token: self._mutation_failed(
                token,
                "Playback did not release the active library in time. Nothing was changed.",
            ),
        )

    def _mutation_ready(self, token: str) -> None:
        callbacks = self._mutation_callbacks.pop(token, None)
        if callbacks is not None:
            callbacks[0]()

    def _mutation_failed(self, token: str, message: str) -> None:
        callbacks = self._mutation_callbacks.pop(token, None)
        if callbacks is None:
            return
        self._mutation_library_id = ""
        self._restore_prepared_playback()
        failed = callbacks[1]
        if failed is not None:
            failed(message)
        else:
            self.window.show_toast(message, None)

    def _restore_prepared_playback(self) -> None:
        if self._mutation_unloaded_current:
            current = self.queue.entry(self._mutation_current_id)
            if current is not None and self._valid_entry(current):
                self._seek_after_load = self._mutation_position
                self._load_entry(current, paused=not self._mutation_was_playing)
        self._mutation_unloaded_current = False
        self._mutation_active = False
        self._refresh_lookahead()

    def complete_filesystem_mutation(
        self,
        library_id: str,
        plan: ChangePlan,
        result: ApplyResult,
    ) -> None:
        if self._mutation_library_id != library_id:
            return
        self._mutation_library_id = ""
        if not result.success or result.receipt is None:
            self._restore_prepared_playback()
            return
        paths = {item.track_id: item.path for item in result.receipt.tracks}
        self.queue.replace_paths(paths)
        current_record = self.app_controller.current_library
        if (
            current_record is not None
            and self.queue.current is not None
            and current_record.library_id == library_id
        ):
            self.queue.reconcile(self._entries_for_current_library())
        else:
            by_id = {entry.track_id: entry for entry in self.queue.entries}
            ordered = tuple(by_id[item] for item in plan.ordered_track_ids if item in by_id)
            self.queue.reconcile(ordered)
        current = self.queue.current
        if current is None and self._mutation_successor:
            successor = self.queue.entry(self._mutation_successor)
            if successor is not None:
                self.queue.current_id = successor.track_id
                if self._mutation_active:
                    self._load_entry(successor, paused=not self._mutation_was_playing)
                else:
                    self.snapshot = replace(
                        self.snapshot,
                        state=PlaybackState.STOPPED,
                        presentation=PlayerPresentation.MINI,
                        entry=successor,
                        position_seconds=0.0,
                        duration_seconds=successor.duration_seconds,
                        loading=False,
                    )
                    self.window.player_panel.set_snapshot(self.snapshot)
                self._mutation_unloaded_current = False
                self._mutation_active = False
                return
        if current is not None:
            self.snapshot = replace(self.snapshot, entry=current)
            self.window.player_panel.set_snapshot(self.snapshot)
        elif self._mutation_active:
            self.close()
        self._mutation_unloaded_current = False
        self._mutation_active = False
        self._refresh_lookahead()

    def library_removed(self, library_id: str) -> None:
        entry = self.snapshot.entry
        if entry is not None and entry.library_id == library_id:
            self.close()

    def _save_preferences(self) -> None:
        if not self._stopping:
            try:
                save_config(self.config)
            except (OSError, ValueError):
                _LOG.exception("Playback preferences could not be saved")

    def reset_preferences(self) -> None:
        self.set_volume(75)
        self.set_muted(False)
        self.set_shuffle(False)
        self.set_repeat(RepeatMode.OFF.value)

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._reveal_request += 1
        self.app.removeEventFilter(self)
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
        self._config_timer.stop()
        self._position_event_timer.stop()
        self._pending_position = None
        if self._system_media is not None:
            self._system_media.close()
        self.window.player_panel.set_presentation(PlayerPresentation.HIDDEN)
        self.commandRequested.emit("shutdown", None)
        if not self._playback_thread.wait(5000):
            _LOG.error("Playback backend did not stop before the shutdown deadline")
            self._playback_thread.quit()
            if not self._playback_thread.wait(1000):
                _LOG.error("Playback thread remained active after its final shutdown deadline")
        try:
            save_config(self.config)
        except (OSError, ValueError):
            _LOG.exception("Playback preferences could not be saved during shutdown")


def _bit_depth(sample_format: str) -> int | None:
    value = sample_format.casefold()
    for depth in (64, 32, 24, 16, 8):
        if str(depth) in value:
            return depth
    return None


def _finite_float(value: object) -> float | None:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None
