from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from .audio_settings import AlbumInfo, AudioSettings, audio_filters, fade_duration, fade_weights
from .diagnostics import diagnostics_logger
from .playback_backend import BackendEvent, CrossfadeTransition, LibMpvBackend

_LOG = diagnostics_logger("audio")


class _Delivery(QObject):
    received = Signal(int, object)

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.received.connect(self.dispatch, Qt.ConnectionType.QueuedConnection)

    @Slot(int, object)
    def dispatch(self, deck, event):
        self.callback(deck, event)


class ListeningBackend:

    def __init__(self, factory=LibMpvBackend, clock=monotonic):
        self._factory, self._clock = factory, clock
        self.settings = AudioSettings()
        self._effective = self.settings
        self._decks: dict[int, LibMpvBackend] = {}
        self._sequence = 0
        self._active = 0
        self._standby: int | None = None
        self._tail: int | None = None
        self._standby_ready = False
        self._lookahead: tuple[Path, int] | None = None
        self._generation = 0
        self._position = 0.0
        self._duration = 0.0
        self._volume = 75.0
        self._muted = False
        self._paused = True
        self._fade_started = 0.0
        self._fade_seconds = 0.0
        self._pause_started: float | None = None
        self._callback = None
        self._delivery = None
        self._timer = None
        self._device = "auto"
        self._failed_devices: set[str] = set()
        self._attempted_candidate = 0
        self._ramp_from = 100 * (75 / 100) ** 3
        self._ramp_to = self._ramp_from
        self._ramp_started = 0.0
        self._gain = self._ramp_from

    @property
    def available(self):
        return self._active in self._decks and self._decks[self._active].available

    @property
    def error(self):
        return self._decks[self._active].error if self._active in self._decks else "Playback runtime unavailable"

    def initialize(self, callback):
        self._callback = callback
        self._delivery = _Delivery(self._route)
        self._active = self._create_deck()
        self._timer = QTimer(self._delivery)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _emit(self, kind, value=None, generation=None):
        if self._callback is not None:
            self._callback(BackendEvent(kind, value, self._generation if generation is None else generation))

    def _create_deck(self):
        self._sequence += 1
        identity = self._sequence
        deck = self._factory()
        self._decks[identity] = deck
        delivery = self._delivery
        try:
            deck.initialize(lambda event: delivery.received.emit(identity, event))
            if deck.available:
                self._configure(deck, normalization=True)
                deck._observe("audio-device-list", "devices")
                deck.set_volume(0)
                deck.set_muted(self._muted)
        except Exception:
            self._retire(identity)
            raise
        return identity

    def _retire(self, identity):
        deck = self._decks.pop(identity, None)
        if deck is not None:
            deck.shutdown()

    def _configure(self, deck, *, normalization=False):
        player = deck._require_player()
        for name, value in (("audio_samplerate", 48000), ("audio_format", "float"), ("volume_gain_min", -150),
                            ("gapless_audio", self._effective.gapless),
                            ("audio_device", self._device)):
            if getattr(player, name, None) != value:
                setattr(player, name, value)
        if normalization:
            player.replaygain = "no" if self.settings.normalization == "off" else self.settings.normalization
            player.replaygain_preamp = 0
            player.replaygain_fallback = 0
            player.replaygain_clip = False
        effective = replace(self._effective, normalization=self.settings.normalization) if normalization else self._effective
        filters = audio_filters(effective)
        if getattr(deck, "_filter_spec", None) != filters:
            player.af = filters
            deck._filter_spec = filters

    def configure_audio(self, settings: AudioSettings):
        previous = self.settings
        self.settings = settings
        if settings.device != previous.device:
            self._failed_devices.discard(settings.device)
            self._device = settings.device
            self._cancel_overlap()
        if self._tail is None:
            self._effective = replace(settings, normalization=self._effective.normalization)
        else:
            self._effective = replace(self._effective, eq_enabled=settings.eq_enabled,
                                      eq_gains=settings.eq_gains, device=settings.device)
        for identity, deck in tuple(self._decks.items()):
            if deck.available:
                self._configure(deck, normalization=identity == self._standby)
        if (self._lookahead is not None and settings.normalization != previous.normalization
                and self._decks[self._active]._active_generation() == self._generation):
            deck = self._decks[self._active]
            deck.next_file_options = self._file_options()
            deck.set_lookahead(self._lookahead[0], generation=self._lookahead[1])
        if not settings.crossfade and self._tail is None:
            self._discard_standby()
        self._set_gain_target()
        self.refresh_devices()
        self._prepare_standby()

    def _set_gain_target(self):
        self._ramp_from = self._gain
        self._ramp_to = 100 * (self._volume / 100) ** 3 * (10 ** (-3.1 / 20) if self._effective.crossfade else 1)
        self._ramp_started = self._clock()

    def _write_gain(self, identity, value, *, immediate=False):
        deck = self._decks[identity]
        multiplier = value / 100
        gain_db = max(-150.0, 20 * math.log10(multiplier)) if multiplier > 0 else -150.0
        volume = 100 if value > 0 else 0
        state = (round(gain_db, 6), volume)
        previous = getattr(deck, "_gain_state", None)
        if previous != state and deck.set_gain(gain_db, volume, immediate=immediate):
            deck._gain_state = state

    def _file_options(self):
        return {"replaygain": "no" if self.settings.normalization == "off" else self.settings.normalization,
                "replaygain-preamp": "0", "replaygain-fallback": "0", "replaygain-clip": "no"}

    def refresh_devices(self):
        if not self.available:
            return
        devices = self._decks[self._active]._require_player().audio_device_list or []
        self._emit("devices", devices)
        names = {item.get("name") for item in devices if isinstance(item, dict)}
        if self._device != "auto" and self._device not in names:
            self._fallback_device()

    def _fallback_device(self):
        missing = self._device
        if missing == "auto" or missing in self._failed_devices:
            return
        self._failed_devices.add(missing)
        self._device = "auto"
        self._cancel_overlap()
        for deck in self._decks.values():
            if deck.available:
                deck._require_player().audio_device = "auto"
        self._emit("audio-notice", "Audio device unavailable. Using System default.")
        self._emit("output-error", "The audio output changed. Restoring playback.")

    def load(self, path, *, paused=False, generation=0):
        self._cancel_overlap()
        self._discard_standby()
        self._lookahead = None
        self._effective = self.settings
        self._generation, self._position, self._duration = generation, 0.0, 0.0
        self._paused = bool(paused)
        deck = self._decks[self._active]
        self._configure(deck, normalization=True)
        self._set_gain_target()
        self._gain = self._ramp_to
        self._ramp_from = self._gain
        self._write_gain(self._active, self._gain, immediate=True)
        deck.load(path, paused=paused, generation=generation)

    def set_lookahead(self, path, *, generation=0):
        if self._lookahead == (path, generation):
            return
        self._discard_standby()
        self._lookahead = (Path(path), generation) if path is not None else None
        self._attempted_candidate = 0
        deck = self._decks[self._active]
        deck.next_file_options = self._file_options()
        deck.set_lookahead(path, generation=generation)
        self._prepare_standby()

    def prepare_mutation(self, *, expected_generation=None):
        if expected_generation is not None and expected_generation != self._generation:
            self.stop()
            self._decks[self._active].wait_until_idle()
            return True
        self._cancel_overlap()
        self.set_lookahead(None)
        return False

    def _prepare_standby(self):
        if (not self._effective.crossfade or self._lookahead is None or self._tail is not None
                or self._standby is not None or self._attempted_candidate == self._lookahead[1]
                or not self.available):
            return
        path, generation = self._lookahead
        active_path = self._decks[self._active]._require_player().path
        if active_path and str(path).casefold() == str(active_path).casefold():
            return
        self._attempted_candidate = generation
        try:
            self._standby = self._create_deck()
            deck = self._decks[self._standby]
            if not deck.available:
                raise RuntimeError(deck.error)
            deck.load(path, paused=True, generation=generation)
        except Exception:
            _LOG.exception("Crossfade preparation failed; retaining native handoff")
            self._discard_standby()

    def _discard_standby(self):
        if self._standby is not None:
            self._retire(self._standby)
            self._standby = None
        self._standby_ready = False

    def _cancel_overlap(self):
        if self._tail is not None:
            self._retire(self._tail)
            self._tail = None
            self._effective = replace(self.settings, normalization=self._effective.normalization)
            if self.available:
                self._configure(self._decks[self._active])
            self._set_gain_target()
        self._pause_started = None
        self._fade_seconds = 0.0
        if self.available:
            self._write_gain(self._active, self._gain, immediate=True)

    def set_paused(self, paused):
        paused = bool(paused)
        if paused != self._paused and self._tail is not None:
            if paused:
                self._pause_started = self._clock()
            elif self._pause_started is not None:
                self._fade_started += self._clock() - self._pause_started
                self._pause_started = None
        self._paused = paused
        for identity in (self._active, self._tail):
            if identity in self._decks:
                self._decks[identity].set_paused(paused)

    def seek(self, seconds, *, generation=None):
        deck = self._decks[self._active]
        if generation is not None and (generation != self._generation or generation != deck._active_generation()):
            return
        self._cancel_overlap()
        self._position = float(seconds)
        deck.seek(seconds, generation=generation)

    def set_volume(self, volume):
        self._volume = max(0, min(100, float(volume)))
        self._set_gain_target()

    def set_muted(self, muted):
        self._muted = bool(muted)
        for deck in self._decks.values():
            deck.set_muted(muted)

    def recover_output(self):
        self._cancel_overlap()
        self._discard_standby()
        if self._device != "auto":
            self._failed_devices.add(self._device)
            self._device = "auto"
            self._emit("audio-notice", "Audio device unavailable. Using System default.")
        self._decks[self._active].recover_output()

    def stop(self):
        self._paused = True
        self._lookahead = None
        self._cancel_overlap()
        self._discard_standby()
        self._decks[self._active].stop()

    def _route(self, identity, event):
        if identity not in self._decks or self._callback is None:
            return
        if identity == self._standby:
            if event.kind == "loaded":
                self._standby_ready = True
            elif event.kind in ("error", "unavailable", "output-error"):
                self._discard_standby()
            return
        if identity != self._active:
            return
        if event.kind == "devices":
            self.refresh_devices()
            return
        if self._lookahead is not None and event.generation == self._lookahead[1] and event.kind in ("loading", "loaded"):
            self._discard_standby()
            self._generation = event.generation
            self._lookahead = None
            self._effective = self.settings
            self._configure(self._decks[identity], normalization=True)
            self._set_gain_target()
        if event.generation == self._generation:
            if event.kind == "position":
                self._position = self._finite(event.value)
            elif event.kind == "duration":
                self._duration = self._finite(event.value)
        self._callback(event)

    @staticmethod
    def _finite(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _tick(self):
        try:
            self._advance()
        except Exception:
            _LOG.exception("Crossfade failed; returning to ordinary playback")
            self._cancel_overlap()
            self._discard_standby()

    def _advance(self):
        if not self.available:
            return
        now = self._clock()
        fraction = min(1, max(0, (now - self._ramp_started) / 0.1))
        self._gain = self._ramp_from + (self._ramp_to - self._ramp_from) * fraction
        if self._tail is not None:
            at = self._pause_started if self._pause_started is not None else now
            progress = min(1, max(0, (at - self._fade_started) / self._fade_seconds))
            outgoing, incoming = fade_weights(progress)
            self._write_gain(self._tail, self._gain * outgoing)
            self._write_gain(self._active, self._gain * incoming)
            if progress >= 1:
                self._cancel_overlap()
                self._prepare_standby()
            return
        self._write_gain(self._active, self._gain)
        if self._paused or not self._standby_ready or self._standby is None or self._lookahead is None:
            return
        current = self._decks[self._active]._require_player()
        incoming = self._decks[self._standby]._require_player()
        position = getattr(current, "time_pos", None)
        if position is not None:
            self._position = self._finite(position)
        duration = fade_duration(self._effective.crossfade_seconds, self._duration, self._finite(incoming.duration))
        remaining = self._duration - self._position
        if duration <= 0 or remaining > duration or remaining < 0.5:
            return
        if self._effective.preserve_albums and AlbumInfo.from_tags(current.metadata).followed_by(AlbumInfo.from_tags(incoming.metadata)):
            self._discard_standby()
            return
        old_generation = self._generation
        candidate_path, candidate_generation = self._lookahead
        if self._decks[self._active]._active_generation() != old_generation:
            self._discard_standby()
            return
        self._decks[self._active].set_lookahead(None)
        self._tail, self._active = self._active, self._standby
        self._standby, self._standby_ready = None, False
        self._generation = candidate_generation
        self._effective = replace(self._effective, normalization=self.settings.normalization)
        self._lookahead = None
        self._position, self._duration = 0.0, self._finite(incoming.duration)
        self._fade_started, self._fade_seconds = now, min(duration, remaining)
        self._decks[self._active].set_paused(False)
        self._emit("crossfade-started", CrossfadeTransition(old_generation, candidate_path))
        self._emit("loaded")
        self._emit("duration", self._duration)
        self._emit("source-params", incoming.audio_params)
        self._emit("output-params", incoming.audio_out_params)

    def shutdown(self):
        self._callback = None
        if self._timer is not None:
            self._timer.stop()
        for identity in tuple(self._decks):
            self._retire(identity)
        if self._delivery is not None:
            self._delivery.deleteLater()
        self._delivery = None
