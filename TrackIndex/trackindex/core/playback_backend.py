from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class BackendEvent:
    kind: str
    value: object = None
    generation: int = 0


class PlaybackBackend(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def error(self) -> str: ...

    def initialize(self, callback: Callable[[BackendEvent], None]) -> None: ...

    def load(self, path: Path, *, paused: bool = False, generation: int = 0) -> None: ...

    def set_lookahead(self, path: Path | None, *, generation: int = 0) -> None: ...

    def set_paused(self, paused: bool) -> None: ...

    def seek(self, seconds: float) -> None: ...

    def set_volume(self, volume: int) -> None: ...

    def set_muted(self, muted: bool) -> None: ...

    def recover_output(self) -> None: ...

    def stop(self) -> None: ...

    def shutdown(self) -> None: ...


class UnavailableBackend:
    def __init__(self, message: str = "The TrackIndex audio runtime is not installed.") -> None:
        self._error = message
        self._callback: Callable[[BackendEvent], None] | None = None

    @property
    def available(self) -> bool:
        return False

    @property
    def error(self) -> str:
        return self._error

    def initialize(self, callback: Callable[[BackendEvent], None]) -> None:
        self._callback = callback
        callback(BackendEvent("unavailable", self._error))

    def load(self, path: Path, *, paused: bool = False, generation: int = 0) -> None:
        if self._callback is not None:
            self._callback(BackendEvent("error", self._error, generation))

    def set_lookahead(self, path: Path | None, *, generation: int = 0) -> None:
        return

    def set_paused(self, paused: bool) -> None:
        return

    def seek(self, seconds: float) -> None:
        return

    def set_volume(self, volume: int) -> None:
        return

    def set_muted(self, muted: bool) -> None:
        return

    def recover_output(self) -> None:
        return

    def stop(self) -> None:
        return

    def shutdown(self) -> None:
        self._callback = None


class LibMpvBackend:
    def __init__(self) -> None:
        self._player: Any = None
        self._callback: Callable[[BackendEvent], None] | None = None
        self._error = ""
        self._dll_handle: Any = None
        self._observers: list[tuple[str, Callable[..., None]]] = []
        self._event_handler: Callable[..., None] | None = None
        self._event_lock = RLock()
        self._entry_generations: dict[int, int] = {}
        self._active_entry_id = 0
        self._requested_current_generation = 0
        self._pending_current_generation = 0
        self._pending_lookahead_generation = 0

    @property
    def available(self) -> bool:
        return self._player is not None and not self._error

    @property
    def error(self) -> str:
        return self._error

    def initialize(self, callback: Callable[[BackendEvent], None]) -> None:
        self._callback = callback
        try:
            binding = Path(__file__).resolve().parents[2] / "vendor"
            if binding.is_dir() and str(binding) not in sys.path:
                sys.path.insert(0, str(binding))
            native = native_runtime_folder()
            native_text = str(native)
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if native.is_dir() and native_text.casefold() not in {part.casefold() for part in path_parts}:
                os.environ["PATH"] = native_text + os.pathsep + os.environ.get("PATH", "")
            if os.name == "nt" and native.is_dir() and hasattr(os, "add_dll_directory"):
                self._dll_handle = os.add_dll_directory(native_text)
            mpv = import_module("mpv")

            self._player = mpv.MPV(
                config=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                terminal=False,
                ytdl=False,
                vid="no",
                audio_display="no",
                vo="null",
                load_scripts=False,
                gapless_audio="weak",
                prefetch_playlist=True,
                audio_client_name="TrackIndex",
                keep_open=False,
            )
            self._observe("time-pos", "position")
            self._observe("duration", "duration")
            self._observe("pause", "pause")
            self._observe("audio-codec-name", "codec")
            self._observe("audio-bitrate", "bitrate")
            self._observe("audio-params", "source-params")
            self._observe("audio-out-params", "output-params")
            self._observe("audio-device", "device")
            self._event_handler = self._on_event
            self._player.register_event_callback(self._event_handler)
            callback(BackendEvent("ready"))
        except Exception as exc:
            self._error = str(exc) or exc.__class__.__name__
            self._callback = None
            self._teardown()
            callback(BackendEvent("unavailable", self._error))

    def _observe(self, property_name: str, event_name: str) -> None:
        player = self._require_player()

        def observer(_name: str, value: object) -> None:
            self._emit(event_name, value)

        player.observe_property(property_name, observer)
        self._observers.append((property_name, observer))

    def _on_event(self, event: object) -> None:
        event_id = _integer_value(getattr(event, "event_id", None))
        if event_id == 8:
            self._emit("loaded", generation=self._active_generation())
        elif event_id == 6:
            entry_id = _integer_value(
                getattr(getattr(event, "data", None), "playlist_entry_id", None)
            ) or 0
            with self._event_lock:
                self._active_entry_id = entry_id
                generation = self._entry_generations.get(entry_id, 0)
                if not generation:
                    if self._pending_current_generation:
                        generation = self._pending_current_generation
                        self._pending_current_generation = 0
                    elif self._pending_lookahead_generation:
                        generation = self._pending_lookahead_generation
                        self._pending_lookahead_generation = 0
                    elif not entry_id:
                        generation = self._requested_current_generation
                    if generation and entry_id:
                        self._entry_generations[entry_id] = generation
            self._emit("loading", generation=generation)
        elif event_id == 7:
            data = getattr(event, "data", None)
            entry_id = _integer_value(getattr(data, "playlist_entry_id", None)) or 0
            with self._event_lock:
                generation = self._entry_generations.get(entry_id, 0)
            outcome = classify_end_file(
                getattr(data, "reason", None),
                getattr(data, "error", None),
            )
            if outcome == "ended":
                self._emit("ended", "eof", generation)
            elif outcome == "error":
                error = _integer_value(getattr(data, "error", None))
                if error == -14:
                    self._emit(
                        "output-error",
                        "The active audio output device became unavailable.",
                        generation,
                    )
                    return
                self._emit(
                    "error",
                    f"The audio file could not be played (mpv error {error})."
                    if error not in {None, 0}
                    else "The audio file could not be played.",
                    generation,
                )

    def _active_generation(self) -> int:
        with self._event_lock:
            return self._entry_generations.get(
                self._active_entry_id,
                self._requested_current_generation if not self._active_entry_id else 0,
            )

    def _emit(self, kind: str, value: object = None, generation: int | None = None) -> None:
        callback = self._callback
        if callback is not None:
            callback(
                BackendEvent(
                    kind,
                    value,
                    self._active_generation() if generation is None else generation,
                )
            )

    def _require_player(self):
        if self._player is None:
            raise RuntimeError(self._error or "The TrackIndex audio runtime is unavailable.")
        return self._player

    @staticmethod
    def _playlist_entry_id(item: object) -> int:
        if not isinstance(item, dict):
            return 0
        return _integer_value(item.get("id")) or 0

    def load(self, path: Path, *, paused: bool = False, generation: int = 0) -> None:
        player = self._require_player()
        with self._event_lock:
            self._requested_current_generation = generation
            self._entry_generations.clear()
            self._active_entry_id = 0
            self._pending_current_generation = generation
            self._pending_lookahead_generation = 0
        try:
            player.command("loadfile", str(path), "replace")
            player.pause = bool(paused)
        except Exception:
            with self._event_lock:
                self._pending_current_generation = 0
            raise

    def set_lookahead(self, path: Path | None, *, generation: int = 0) -> None:
        player = self._require_player()
        self._remove_lookahead()
        if path is not None:
            with self._event_lock:
                self._pending_lookahead_generation = generation
            try:
                player.command("loadfile", str(path), "append")
            except Exception:
                with self._event_lock:
                    if self._pending_lookahead_generation == generation:
                        self._pending_lookahead_generation = 0
                raise

    def _remove_lookahead(self) -> None:
        player = self._require_player()
        player.command("playlist-clear")
        with self._event_lock:
            retained_entry_id = self._active_entry_id
            retained_generation = self._entry_generations.get(retained_entry_id, 0)
            self._entry_generations = (
                {retained_entry_id: retained_generation}
                if retained_entry_id and retained_generation
                else {}
            )
            self._pending_lookahead_generation = 0

    def set_paused(self, paused: bool) -> None:
        self._require_player().pause = bool(paused)

    def seek(self, seconds: float) -> None:
        self._require_player().command("seek", max(0.0, float(seconds)), "absolute", "exact")

    def set_volume(self, volume: int) -> None:
        self._require_player().volume = max(0, min(100, int(volume)))

    def set_muted(self, muted: bool) -> None:
        self._require_player().mute = bool(muted)

    def recover_output(self) -> None:
        player = self._require_player()
        player.audio_device = "auto"
        with suppress(Exception):
            player.command("audio-reload")

    def stop(self) -> None:
        player = self._require_player()
        self._remove_lookahead()
        player.command("stop")

    def shutdown(self) -> None:
        with self._event_lock:
            self._callback = None
        self._teardown()

    def _teardown(self) -> None:
        player = self._player
        if player is not None:
            for name, observer in self._observers:
                with suppress(Exception):
                    player.unobserve_property(name, observer)
            if self._event_handler is not None:
                with suppress(Exception):
                    player.unregister_event_callback(self._event_handler)
            with suppress(Exception):
                player.terminate()
        self._player = None
        self._observers.clear()
        self._event_handler = None
        with self._event_lock:
            self._entry_generations.clear()
            self._active_entry_id = 0
            self._requested_current_generation = 0
            self._pending_current_generation = 0
            self._pending_lookahead_generation = 0
        handle = self._dll_handle
        self._dll_handle = None
        if handle is not None:
            with suppress(Exception):
                handle.close()


def native_runtime_folder() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    bundled = root / "native" / "libmpv"
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[2] / "native" / "libmpv"


def create_playback_backend() -> PlaybackBackend:
    backend = LibMpvBackend()
    return backend


def _integer_value(value: object) -> int | None:
    raw = getattr(value, "value", value)
    if not isinstance(raw, (int, str, bytes, bytearray)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def classify_end_file(reason: object, error: object) -> str | None:
    reason_value = _integer_value(reason)
    error_value = _integer_value(error)
    if reason_value == 0:
        return "ended"
    if reason_value == 4 or (error_value is not None and error_value < 0):
        return "error"
    if isinstance(reason, str):
        normalized = reason.casefold().replace("_", "-")
        if normalized in {"eof", "end", "ended"}:
            return "ended"
        if normalized == "error":
            return "error"
    return None
