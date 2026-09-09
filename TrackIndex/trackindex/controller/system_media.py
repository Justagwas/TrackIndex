from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from trackindex.core.diagnostics import diagnostics_logger
from trackindex.core.playback import PlaybackSnapshot, PlaybackState

_LOG = diagnostics_logger("system-media")
MediaContext = tuple[int, str, str]
MediaPublication = tuple[PlaybackSnapshot, MediaContext, bool]


class MediaSession(Protocol):
    def publish(self, snapshot: PlaybackSnapshot, enabled: bool) -> None: ...

    def close(self) -> None: ...


class WindowsMediaSession:
    def __init__(self, handle: int, command: Callable[[str, object], None]) -> None:
        from winrt import runtime
        from winrt.windows import media
        from winrt.windows.media.interop import get_for_window

        self._runtime = runtime
        self._media = media
        self._controls: Any = None
        self._tokens: list[tuple[Callable, Any]] = []
        self._apartment = False
        self._metadata: object = None
        self._timeline: object = None
        self._closed = False
        self._command = command
        try:
            runtime.init_apartment(runtime.ApartmentType.SINGLE_THREADED)
            self._apartment = True
            self._controls = get_for_window(handle)
            self._tokens.append((self._controls.remove_button_pressed,
                                 self._controls.add_button_pressed(self._button_pressed)))
            self._tokens.append((self._controls.remove_playback_position_change_requested,
                                 self._controls.add_playback_position_change_requested(self._seek_requested)))
        except Exception:
            self.close()
            raise

    def _button_pressed(self, _sender, args) -> None:
        commands = {0: "play", 1: "pause", 2: "stop", 6: "next", 7: "previous"}
        command = commands.get(int(args.button))
        if command is not None and not self._closed:
            self._command(command, None)

    def _seek_requested(self, _sender, args) -> None:
        if not self._closed:
            self._command("seek", args.requested_playback_position.total_seconds())

    def publish(self, snapshot: PlaybackSnapshot, enabled: bool) -> None:
        if self._closed:
            return
        controls = self._controls
        entry = snapshot.entry
        controls.is_enabled = entry is not None
        for name in ("play", "pause", "stop", "next", "previous"):
            setattr(controls, f"is_{name}_enabled", entry is not None and enabled)
        status = self._media.MediaPlaybackStatus
        controls.playback_status = {
            PlaybackState.IDLE: status.CLOSED,
            PlaybackState.LOADING: status.CHANGING,
            PlaybackState.PLAYING: status.PLAYING,
            PlaybackState.PAUSED: status.PAUSED,
            PlaybackState.STOPPED: status.STOPPED,
            PlaybackState.ERROR: status.STOPPED,
        }[snapshot.state]
        metadata = (entry.library_id, entry.track_id, entry.title, entry.artist, entry.album) if entry else ()
        if metadata != self._metadata:
            updater = controls.display_updater
            updater.clear_all()
            if entry:
                updater.type = self._media.MediaPlaybackType.MUSIC
                updater.music_properties.title = entry.title or entry.path.stem
                updater.music_properties.artist = entry.artist
                updater.music_properties.album_title = entry.album
            updater.update()
            self._metadata = metadata
        duration = max(0.0, snapshot.duration_seconds or 0.0)
        position = max(0.0, min(snapshot.position_seconds, duration))
        timeline = (int(position), duration, snapshot.state, enabled)
        if timeline != self._timeline:
            properties = self._media.SystemMediaTransportControlsTimelineProperties()
            properties.start_time = timedelta(0)
            properties.min_seek_time = timedelta(0)
            properties.end_time = timedelta(seconds=duration)
            properties.max_seek_time = timedelta(seconds=duration if enabled else 0)
            properties.position = timedelta(seconds=position)
            controls.update_timeline_properties(properties)
            self._timeline = timeline

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while self._tokens:
            remove, token = self._tokens.pop()
            try:
                remove(token)
            except Exception:
                _LOG.exception("Windows media event removal failed")
            finally:
                del remove
        if self._controls is not None:
            try:
                self._controls.is_enabled = False
                self._controls.display_updater.clear_all()
                self._controls.display_updater.update()
            except Exception:
                _LOG.exception("Windows media display teardown failed")
            self._controls = None
        if self._apartment:
            self._runtime.uninit_apartment()
            self._apartment = False


class SystemMediaBridge(QObject):
    commandRequested = Signal(str, object, object)
    availabilityChanged = Signal(bool)

    def __init__(
        self,
        handle: Callable[[], int],
        publication: Callable[[], MediaPublication],
        parent: QObject,
        factory: Callable[[int, Callable[[str, object], None]], MediaSession] = WindowsMediaSession,
    ) -> None:
        super().__init__(parent)
        self._handle = handle
        self._publication = publication
        self._factory = factory
        self._session: MediaSession | None = None
        self._native_handle = 0
        self._context: MediaContext = (0, "", "")
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)

    def start(self) -> None:
        if not self._closed:
            self._timer.start()
            self.refresh()

    def _receive(self, command: str, value: object) -> None:
        if not self._closed:
            self.commandRequested.emit(command, value, self._context)

    def refresh(self) -> None:
        if self._closed:
            return
        try:
            handle = self._handle()
            snapshot, context, enabled = self._publication()
            self._context = context
            if handle != self._native_handle:
                if self._session is not None:
                    self._session.close()
                    self._session = None
                self._session = self._factory(handle, self._receive)
                self._native_handle = handle
                self.availabilityChanged.emit(True)
            if self._session is not None:
                self._session.publish(snapshot, enabled)
        except Exception:
            _LOG.exception("Windows media controls unavailable")
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                _LOG.exception("Windows media session teardown failed")
            self._session = None
        self.availabilityChanged.emit(False)
