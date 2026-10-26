from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

from trackindex.core.config import SUPPORTED_AUDIO_EXTENSIONS
from trackindex.core.audio_settings import AudioSettings
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
from trackindex.core.playback_backend import BackendEvent, CrossfadeTransition, PlaybackBackend, create_playback_backend
from trackindex.core.threading_contract import assert_qt_thread_affinity
from trackindex.ui.main_window import MainWindow
from trackindex.workers.library_workers import MetadataTask, MetadataWorkerResult

from .system_media import MediaContext, MediaPublication, SystemMediaBridge

_LOG = diagnostics_logger("playback")


@dataclass(frozen=True, slots=True)
class _PendingPosition:
    generation: int
    library_id: str
    track_id: str
    seconds: float


@dataclass(frozen=True, slots=True)
class _PendingResume:
    generation: int
    library_id: str
    track_id: str
    seconds: float


class PlaybackLibraryCoordinator(Protocol):
    current_library: LibraryRecord | None
    audit: AuditResult | None

    def set_playback_coordinator(self, coordinator: PlaybackController) -> None: ...

    def library(self, library_id: str) -> LibraryRecord | None: ...

    def library_loading(self) -> bool: ...

    def select_library(self, library_id: str) -> None: ...

    def create_playback_artwork_task(
        self, generation: int, track_id: str, path: Path,
    ) -> MetadataTask: ...


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
            if command == "load" and isinstance(value, (tuple, list)) and len(value) == 3:
                generation = int(value[2])
            generation = self._execute_command(command, value)
        except Exception as exc:
            _LOG.exception("Playback backend command failed: %s", command)
            if command == "load":
                self.eventReady.emit(BackendEvent("error", str(exc), int(generation)))
            elif command == "prepare-mutation":
                token = value[0] if isinstance(value, tuple) and len(value) == 2 else value
                self.eventReady.emit(BackendEvent("mutation-failed", (str(token), str(exc))))
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
            "audio-settings": self._execute_audio_settings,
            "audio-devices": self._execute_audio_devices,
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
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("Playback seek command is malformed.")
        raw_seconds, raw_generation = value
        seconds = _finite_float(raw_seconds)
        if seconds is None:
            raise ValueError("Playback seek position is malformed.")
        generation = int(raw_generation)
        self.backend.seek(seconds, generation=generation)
        return generation

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
        token, expected = value if isinstance(value, tuple) and len(value) == 2 else (str(value), None)
        interrupted = False
        if hasattr(self.backend, "prepare_mutation"):
            interrupted = self.backend.prepare_mutation(expected_generation=expected)
        else:
            self.backend.set_lookahead(None)
        self.eventReady.emit(BackendEvent("mutation-ready", (str(token), True) if interrupted else str(token)))
        return 0

    def _execute_audio_settings(self, value: object) -> int:
        if isinstance(value, AudioSettings) and hasattr(self.backend, "configure_audio"):
            self.backend.configure_audio(value)
        return 0

    def _execute_audio_devices(self, _value: object) -> int:
        if hasattr(self.backend, "refresh_devices"):
            self.backend.refresh_devices()
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
        super().__init__(app)
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
        self._audio_panel = None
        self._audio_devices = []
        self._deferred_audio_baseline: AudioSettings | None = None
        self._artwork_pool = QThreadPool(self)
        self._artwork_pool.setMaxThreadCount(1)
        self._artwork_request: tuple[MetadataTask, MediaContext, Path] | None = None
        self._output_recovery_attempts = 0
        self._output_recovery_identity: tuple[str, str] | None = None
        self._output_recovery_owner: MediaContext | None = None
        self._output_recovery_message = ""
        self._deferred_output_recovery: tuple[MediaContext, str] | None = None
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
        self._next_backend_generation = 1
        self._active_backend_generation = 0
        self._awaiting_prefetched_load = False
        self._ignore_ended_until_loaded = False
        self._play_after_load = True
        self._pending_resume: _PendingResume | None = None
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
        self._pending_position: _PendingPosition | None = None
        self._position_event_timer = QTimer(self)
        self._position_event_timer.setSingleShot(True)
        self._position_event_timer.setInterval(50)
        self._position_event_timer.timeout.connect(self._flush_backend_position)
        self._shortcuts: list[QShortcut] = []
        self._media_shortcuts: list[QShortcut] = []
        self._playback_thread = QThread(self)
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
        panel.playPauseRequested.connect(lambda: self.dispatch_transport("toggle"))
        panel.previousRequested.connect(lambda: self.dispatch_transport("previous"))
        panel.nextRequested.connect(lambda: self.dispatch_transport("next"))
        panel.stopRequested.connect(lambda: self.dispatch_transport("stop"))
        panel.seekRequested.connect(lambda value: self.dispatch_transport("seek", value))
        panel.volumeChanged.connect(lambda value: self.dispatch_transport("set-volume", value))
        panel.muteChanged.connect(lambda value: self.dispatch_transport("set-muted", value))
        panel.shuffleChanged.connect(lambda value: self.dispatch_transport("set-shuffle", value))
        panel.repeatChanged.connect(lambda value: self.dispatch_transport("set-repeat", value))
        panel.closeRequested.connect(lambda: self.dispatch_transport("close"))
        panel.presentationRequested.connect(self.set_presentation)
        panel.currentTrackRequested.connect(self.reveal_current_track)
        panel.audioSettingsRequested.connect(self.toggle_audio_settings)
        self.window.settings_panel.audioSettingsRequested.connect(self.show_audio_settings)

    def show_audio_settings(self) -> None:
        if self._stopping:
            return
        if self._audio_panel is None:
            self._audio_panel = self.window.ensure_audio_panel()
            self._audio_panel.settingsChanged.connect(self.set_audio_settings)
        if self.window._requested_surface != "audio":
            self._audio_panel.flush_pending_changes()
            self._audio_panel.set_settings(self.config.audio)
        self._audio_panel.set_devices(self._audio_devices)
        self._audio_panel.set_output(self.snapshot.output)
        self.window.show_audio_panel()
        self.commandRequested.emit("audio-devices", None)

    def toggle_audio_settings(self) -> None:
        if self.window._requested_surface == "audio":
            self.window.hide_audio_panel()
        else:
            self.show_audio_settings()

    @Slot(object)
    def set_audio_settings(self, settings: AudioSettings) -> None:
        if self._stopping:
            return
        previous = self.config.audio
        self.config.audio = settings
        self._config_timer.start()
        if self._mutation_library_id:
            if self._deferred_audio_baseline is None:
                self._deferred_audio_baseline = previous
            return
        self.commandRequested.emit("audio-settings", settings)
        if settings.device != previous.device and self.snapshot.entry is not None:
            entry = self.snapshot.entry
            if self.snapshot.state in {PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.LOADING}:
                self._load_entry(entry, paused=self.snapshot.state is PlaybackState.PAUSED,
                                 resume_position=self.snapshot.position_seconds)

    def _flush_deferred_audio_settings(self) -> bool:
        previous = self._deferred_audio_baseline
        self._deferred_audio_baseline = None
        if self._stopping or previous is None or previous == self.config.audio:
            return False
        self.commandRequested.emit("audio-settings", self.config.audio)
        return previous.device != self.config.audio.device

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
            (panel.play_button, "Play or pause (Space / Ctrl+Space)"),
            (panel.mini_play_button, "Play or pause (Space / Ctrl+Space)"),
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
            event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease}
            and event.key() == Qt.Key.Key_Space
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and isinstance(watched, QWidget)
            and watched.window() is self.window
            and self.snapshot.entry is not None
            and self.snapshot.presentation is not PlayerPresentation.HIDDEN
            and not self.window.audio_owns_widget(self.app.focusWidget())
            and not isinstance(self.app.focusWidget(), (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox))
            and self.app.activeModalWidget() is None
            and self.app.activePopupWidget() is None
        ):
            event.accept()
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self.dispatch_transport("toggle")
            return True
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
            self.window.player_panel.set_snapshot(self.snapshot)
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
            "close": self.close,
            "set-volume": lambda: self.set_volume(int(str(value))),
            "set-muted": lambda: self.set_muted(bool(value)),
            "set-shuffle": lambda: self.set_shuffle(bool(value)),
            "set-repeat": lambda: self.set_repeat(str(value)),
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
        if self._stopping or self._mutation_library_id:
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

    def _clear_resume_intent(self, *, preserve_deferred_recovery: bool = False) -> None:
        if not preserve_deferred_recovery:
            self._deferred_output_recovery = None
        self._pending_resume = None
        self._output_recovery_owner = None
        self._output_recovery_identity = None
        self._output_recovery_attempts = 0
        self._output_recovery_message = ""

    def _owned_resume(self) -> _PendingResume | None:
        pending = self._pending_resume
        if pending is not None and (pending.generation, pending.library_id, pending.track_id) == self._media_context():
            return pending
        return None

    def _load_entry(
        self, entry: QueueEntry, *, paused: bool = False,
        resume_position: float | None = None, recovering: bool = False,
    ) -> None:
        self._clear_pending_position()
        if not recovering:
            self._clear_resume_intent()
        else:
            self._pending_resume = None
            self._output_recovery_owner = None
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
        position = _finite_float(resume_position)
        if position is not None:
            self._pending_resume = _PendingResume(generation, entry.library_id, entry.track_id, max(0.0, position))
        self.commandRequested.emit("load", (str(entry.path), paused, generation))
        self._request_current_artwork()
        self._refresh_lookahead()

    def _cancel_artwork_request(self) -> None:
        if self._artwork_request is not None:
            self._artwork_request[0].stop()
            self._artwork_request = None
        self._artwork_pool.clear()

    def _request_current_artwork(self) -> None:
        self._cancel_artwork_request()
        entry = self.snapshot.entry
        if self._stopping or self._mutation_library_id or entry is None or entry.artwork_bytes:
            return
        task = self.app_controller.create_playback_artwork_task(
            self._active_backend_generation, entry.track_id, entry.path,
        )
        self._artwork_request = (task, self._media_context(), entry.path)
        task.signals.metadataReady.connect(self._artwork_ready)
        self._artwork_pool.start(task)

    @Slot(object)
    def _artwork_ready(self, event: MetadataWorkerResult) -> None:
        assert_qt_thread_affinity(self)
        request = self._artwork_request
        if request is None or event.task_id != request[0].task_id:
            return
        self._artwork_request = None
        entry = self.snapshot.entry
        if (
            self._stopping or self._mutation_library_id or entry is None
            or request[1] != self._media_context() or request[2] != entry.path
            or event.generation != self._active_backend_generation
            or event.track_id != entry.track_id or not event.metadata.artwork_bytes
        ):
            return
        self.snapshot = replace(
            self.snapshot, entry=replace(entry, artwork_bytes=event.metadata.artwork_bytes),
        )
        self._publish_playback_snapshot()

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
        paused = natural and self.snapshot.state is PlaybackState.PAUSED
        entry = self.queue.advance(natural=natural)
        if entry is None:
            self.stop()
            return
        if not self._valid_entry(entry):
            self._handle_entry_error(entry, "The audio file is missing.")
            return
        if natural and entry.track_id == self._backend_lookahead_id:
            self._adopt_prefetched_entry(entry, paused=paused)
            return
        self._load_entry(entry, paused=paused)

    def _adopt_prefetched_entry(
        self, entry: QueueEntry, *, paused: bool = False, generation: int | None = None,
    ) -> None:
        self._clear_pending_position()
        self._clear_resume_intent()
        self._backend_lookahead_id = ""
        self._active_backend_generation = self._backend_lookahead_generation if generation is None else generation
        self._backend_lookahead_generation = 0
        self._awaiting_prefetched_load = True
        self._ignore_ended_until_loaded = True
        self._play_after_load = not paused
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.PAUSED if paused else PlaybackState.LOADING,
            presentation=PlayerPresentation.EXPANDED,
            entry=entry,
            position_seconds=0.0,
            duration_seconds=entry.duration_seconds,
            loading=True,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self.window.track_table.set_playback_state(entry.track_id, not paused)
        if paused:
            self.commandRequested.emit("pause", True)
        self._request_current_artwork()

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
        self._cancel_artwork_request()
        self._clear_pending_position()
        self._clear_resume_intent()
        if self.snapshot.entry is None:
            return
        entry = self.snapshot.entry
        self._intentional_stop = True
        self._awaiting_prefetched_load = False
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
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
        self._cancel_artwork_request()
        self._clear_pending_position()
        self._clear_resume_intent()
        self._intentional_stop = True
        self._awaiting_prefetched_load = False
        self._backend_lookahead_id = ""
        self._backend_lookahead_generation = 0
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
        self._clear_pending_position()
        pending = self._owned_resume()
        if pending is not None:
            self._pending_resume = replace(pending, seconds=value)
        self.snapshot = replace(self.snapshot, position_seconds=value)
        self.window.player_panel.set_snapshot(self.snapshot)
        self.commandRequested.emit("seek", (value, self._active_backend_generation))

    @Slot(int)
    def set_volume(self, volume: int) -> None:
        value = max(0, min(100, int(volume)))
        self.snapshot = replace(self.snapshot, volume=value)
        self.config.playback_volume = value
        self.window.player_panel.set_snapshot(self.snapshot)
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
        if event.kind == "crossfade-started":
            self._adopt_crossfade(event)
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

    def _adopt_crossfade(self, event: BackendEvent) -> None:
        transition = event.value
        current = self.snapshot.entry
        if (not isinstance(transition, CrossfadeTransition) or self._mutation_library_id
                or current is None or transition.outgoing_generation != self._active_backend_generation):
            return
        incoming = next((entry for entry in self.queue.entries
                         if entry.library_id == current.library_id and entry.path == transition.incoming_path), None)
        if incoming is not None:
            self.queue.adopt(incoming.track_id)
            self._adopt_prefetched_entry(incoming, paused=self.snapshot.state is PlaybackState.PAUSED,
                                        generation=event.generation)
        elif self.queue.current is not None:
            self._load_entry(self.queue.current, paused=self.snapshot.state is PlaybackState.PAUSED,
                             resume_position=self.snapshot.position_seconds)
        else:
            self.stop()

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
            self.commandRequested.emit("audio-settings", self.config.audio)
            self.commandRequested.emit("volume", self.snapshot.volume)
            self.commandRequested.emit("mute", self.snapshot.muted)
            return True
        if kind == "devices":
            self._audio_devices = value if isinstance(value, list) else []
            if self._audio_panel is not None:
                self._audio_panel.set_devices(self._audio_devices)
            return True
        if kind == "audio-notice":
            self.window.show_toast(str(value), None)
            return True
        if kind == "unavailable":
            self._available = False
            self._availability_error = str(value or "Playback is unavailable.")
            if self._play_requested_before_ready:
                self.window.show_toast("Playback runtime unavailable", None)
                self.close()
            return True
        if kind == "mutation-ready":
            if isinstance(value, tuple) and len(value) == 2:
                self._mutation_ready(str(value[0]), interrupted=bool(value[1]))
            else:
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
        if self._mutation_library_id and kind in {"output-error", "output-recovered", "output-recovery-failed"}:
            self._deferred_output_recovery = (
                self._media_context(),
                str(value or self._output_recovery_message or "The audio output device is unavailable."),
            )
            return True
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
            pending = self._owned_resume()
            if entry is not None and self._output_recovery_owner == self._media_context():
                self._load_entry(entry, paused=not self._play_after_load,
                                 resume_position=pending.seconds if pending else None, recovering=True)
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
        entry = self.snapshot.entry
        restored = entry is not None and entry.track_id in self.queue.unavailable
        if entry is not None:
            self.queue.mark_available(entry.track_id)
        self._ignore_ended_until_loaded = False
        state = PlaybackState.PLAYING if self._play_after_load else PlaybackState.PAUSED
        self.snapshot = replace(self.snapshot, state=state, loading=False)
        self._play_requested_before_ready = False
        self._error_attempts = 0
        self.commandRequested.emit("pause", not self._play_after_load)
        pending = self._owned_resume()
        self._clear_resume_intent(preserve_deferred_recovery=True)
        if pending is not None:
            self.seek(pending.seconds)
        if self._awaiting_prefetched_load or restored:
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
        entry = self.snapshot.entry
        if pending_position is None or entry is None or self.snapshot.state in {
            PlaybackState.IDLE, PlaybackState.STOPPED, PlaybackState.ERROR,
        }:
            return False
        self._pending_position = _PendingPosition(
            self._active_backend_generation, entry.library_id, entry.track_id,
            max(0.0, pending_position),
        )
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
        if self._audio_panel is not None:
            self._audio_panel.set_output(self.snapshot.output)
        if self.snapshot.entry is not None:
            self.window.track_table.set_playback_state(
                self.snapshot.entry.track_id,
                self.snapshot.state is PlaybackState.PLAYING,
            )

    def _clear_pending_position(self) -> None:
        self._position_event_timer.stop()
        self._pending_position = None

    def _flush_backend_position(self) -> None:
        pending = self._pending_position
        self._pending_position = None
        entry = self.snapshot.entry
        if (
            pending is None or self._stopping or entry is None
            or pending.generation != self._active_backend_generation
            or (pending.library_id, pending.track_id) != (entry.library_id, entry.track_id)
            or self.snapshot.state in {PlaybackState.IDLE, PlaybackState.STOPPED, PlaybackState.ERROR}
        ):
            return
        self.snapshot = replace(self.snapshot, position_seconds=pending.seconds)
        self.window.player_panel.set_position(pending.seconds)

    def _replace_current_source(self, source: SourceAudioInfo) -> None:
        entry = self.snapshot.entry
        if entry is None:
            return
        replacement = replace(entry, source=source)
        self.snapshot = replace(self.snapshot, entry=replacement)
        self.queue.entries = tuple(
            replace(item, source=source) if item.track_id == entry.track_id else item
            for item in self.queue.entries
        )

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
        self._clear_resume_intent()
        self.queue.mark_unavailable(entry.track_id)
        self._error_attempts += 1
        self.window.show_toast(f"Could not play {entry.title}; skipping", None)
        if self._error_attempts >= max(1, len(self.queue.entries)):
            self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR, error=message, loading=False)
            self.stop()
            return
        self.next(natural=True)

    def _begin_output_recovery(self, entry: QueueEntry, message: str) -> None:
        identity = (entry.library_id, entry.track_id)
        if self._output_recovery_identity != identity:
            self._output_recovery_attempts = 0
        self._output_recovery_identity = identity
        self._output_recovery_owner = self._media_context()
        self._output_recovery_message = message
        pending = self._owned_resume()
        if pending is None:
            self._play_after_load = self.snapshot.state is not PlaybackState.PAUSED
            position = _finite_float(self.snapshot.position_seconds)
            if position is not None:
                self._pending_resume = _PendingResume(self._active_backend_generation,
                                                      *identity, max(0.0, position))
        self.snapshot = replace(
            self.snapshot,
            state=PlaybackState.LOADING,
            loading=True,
            error="",
        )
        self.window.player_panel.set_snapshot(self.snapshot)
        self._request_output_recovery()

    def _request_output_recovery(self) -> None:
        if self._stopping or self._output_recovery_owner != self._media_context():
            return
        if self._mutation_library_id:
            self._deferred_output_recovery = (self._media_context(), self._output_recovery_message)
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

    def _resume_deferred_output_recovery(self) -> bool:
        pending = self._deferred_output_recovery
        self._deferred_output_recovery = None
        if (pending is None or self._stopping or pending[0] != self._media_context()
                or self.snapshot.entry is None
                or self.snapshot.state not in {PlaybackState.LOADING, PlaybackState.PLAYING, PlaybackState.PAUSED}):
            return False
        self._begin_output_recovery(self.snapshot.entry, pending[1])
        return True

    def _retry_output_recovery(self) -> None:
        if self._output_recovery_attempts >= 3:
            self._request_output_recovery()
            return
        generation = self._active_backend_generation
        owner = self._output_recovery_owner
        attempt = self._output_recovery_attempts

        def retry() -> None:
            if (
                not self._stopping
                and generation == self._active_backend_generation
                and attempt == self._output_recovery_attempts
                and owner is not None
                and owner == self._output_recovery_owner
                and owner == self._media_context()
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
        self._cancel_artwork_request()
        token = uuid.uuid4().hex
        self._mutation_callbacks[token] = (callback, error_callback)
        self._mutation_library_id = library_id
        self._mutation_current_id = self.queue.current_id
        self._mutation_position = self.snapshot.position_seconds
        self._mutation_was_playing = (self._play_after_load if self.snapshot.state is PlaybackState.LOADING
                                      else self.snapshot.state is PlaybackState.PLAYING)
        self._mutation_active = self.snapshot.state in {
            PlaybackState.LOADING,
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        }
        self._mutation_successor = self.queue.peek_next_id()
        expected_generation = self._active_backend_generation
        departing = {
            item.track_id
            for item in plan.presence
            if item.action == "stash"
        }
        self._mutation_unloaded_current = (
            self._mutation_active and self.queue.current_id in departing
        )
        if self._mutation_unloaded_current:
            self._clear_pending_position()
            self._clear_resume_intent()
            self._intentional_stop = True
            self._backend_lookahead_id = ""
            self._backend_lookahead_generation = 0
            self._active_backend_generation = self._new_backend_generation()
            self.commandRequested.emit("stop", None)
        self.commandRequested.emit("prepare-mutation", (token, expected_generation))
        QTimer.singleShot(
            2000,
            lambda token=token: self._mutation_failed(
                token,
                "Playback did not release the active library in time. Nothing was changed.",
            ),
        )

    def _mutation_ready(self, token: str, *, interrupted: bool = False) -> None:
        callbacks = self._mutation_callbacks.pop(token, None)
        if callbacks is not None:
            if interrupted:
                self._mutation_unloaded_current = True
            callbacks[0]()

    def _mutation_failed(self, token: str, message: str) -> None:
        callbacks = self._mutation_callbacks.pop(token, None)
        if callbacks is None:
            return
        self._mutation_library_id = ""
        self._mutation_unloaded_current |= self._mutation_active
        self._restore_prepared_playback()
        failed = callbacks[1]
        if failed is not None:
            failed(message)
        else:
            self.window.show_toast(message, None)

    def _restore_prepared_playback(self) -> None:
        device_changed = self._flush_deferred_audio_settings()
        if self._mutation_unloaded_current or (device_changed and self._mutation_active):
            current = self.queue.entry(self._mutation_current_id)
            if current is not None and self._valid_entry(current):
                self._load_entry(current, paused=not self._mutation_was_playing,
                                 resume_position=(self._mutation_position if self._mutation_unloaded_current
                                                  else self.snapshot.position_seconds))
        self._mutation_unloaded_current = False
        self._mutation_active = False
        if not self._resume_deferred_output_recovery():
            self._refresh_lookahead()
        self._request_current_artwork()

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
        device_changed = self._flush_deferred_audio_settings()
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
                    self._request_current_artwork()
                self._mutation_unloaded_current = False
                self._mutation_active = False
                return
        if current is not None:
            if (self._mutation_unloaded_current or device_changed) and self._mutation_active:
                self._load_entry(current, paused=not self._mutation_was_playing,
                                 resume_position=(self._mutation_position if self._mutation_unloaded_current
                                                  else self.snapshot.position_seconds))
            else:
                self.snapshot = replace(self.snapshot, entry=current)
                self.window.player_panel.set_snapshot(self.snapshot)
        elif self._mutation_active:
            self.close()
        self._mutation_unloaded_current = False
        self._mutation_active = False
        if not self._resume_deferred_output_recovery():
            self._refresh_lookahead()
        self._request_current_artwork()

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
        self.set_audio_settings(AudioSettings())
        if self._audio_panel is not None:
            self._audio_panel.set_settings(self.config.audio)
        self.set_volume(75)
        self.set_muted(False)
        self.set_shuffle(False)
        self.set_repeat(RepeatMode.OFF.value)

    def shutdown(self) -> None:
        if self._stopping:
            return
        self.window.finish_audio_interaction()
        self._stopping = True
        self._deferred_audio_baseline = None
        self._cancel_artwork_request()
        self._reveal_request += 1
        self.app.removeEventFilter(self)
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
        self._config_timer.stop()
        self._clear_pending_position()
        self._clear_resume_intent()
        if self._system_media is not None:
            self._system_media.close()
        self.window.player_panel.set_presentation(PlayerPresentation.HIDDEN)
        self.commandRequested.emit("shutdown", None)
    def shutdown_drained(self) -> bool:
        return self._playback_thread.wait(0) and self._artwork_pool.waitForDone(0)

    def persist_shutdown_state(self) -> bool:
        try:
            save_config(self.config)
        except (OSError, ValueError):
            _LOG.exception("Playback preferences could not be saved during shutdown")
            return False
        return True


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
