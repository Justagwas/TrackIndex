from __future__ import annotations

from dataclasses import replace
import math
import os
from pathlib import Path
import re
from time import monotonic

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from .audio_settings import AlbumInfo, AudioSettings, audio_filters, processing_plan, fade_duration, fade_weights
from .diagnostics import diagnostics_logger
from .filesystem import regular_file_stat
from .playback_backend import BackendEvent, CrossfadeTransition, LibMpvBackend

_LOG = diagnostics_logger("audio")


def normalization_decision(mode, tags=None, analysis=None, *, analysis_enabled=True):
    mode = "track" if mode == "analyzed" else mode
    decision = {"mode": mode, "state": "off", "gain_db": 0.0, "source": "none",
                "text": "Normalization is off.", "peak_dbfs": None, "peak_source": "unknown"}
    data = {str(key).casefold(): value for key, value in tags.items()} if isinstance(tags, dict) else {}
    measured_peak = _tag_number(analysis.get("true_peak_dbfs")) if isinstance(analysis, dict) else None
    if measured_peak is not None and -150 <= measured_peak <= 50:
        decision.update(peak_dbfs=measured_peak, peak_source="measured")
    else:
        for kind in ("track", "album"):
            peak = _tag_number(data.get(f"replaygain_{kind}_peak"))
            if peak is not None and 0 < peak <= 10 ** 2.5:
                decision.update(peak_dbfs=max(-150.0, 20 * math.log10(peak)), peak_source="tags")
                break
    if mode == "off":
        return decision
    kind = "album" if mode == "album" else "track"
    gain = _tag_number(data.get(f"replaygain_{kind}_gain"))
    if gain is None:
        encoded = _r128_number(data.get(f"r128_{kind}_gain"))
        if encoded is not None:
            gain = encoded / 256 + 5
    fallback = False
    if gain is None and kind == "album":
        fallback = True
        kind = "track"
        gain = _tag_number(data.get("replaygain_track_gain"))
        if gain is None:
            encoded = _r128_number(data.get("r128_track_gain"))
            if encoded is not None:
                gain = encoded / 256 + 5
    if gain is None:
        if isinstance(analysis, dict):
            loudness = _tag_number(analysis.get("integrated_lufs"))
            if loudness is None or not -70 < loudness < 20:
                return {**decision, "state": "no-data", "source": "analysis",
                        "text": "No measurable loudness; no normalization adjustment."}
            gain = max(-24.0, min(24.0, -18.0 - loudness))
            label = "Track analysis (album gain unavailable)" if mode == "album" else "Track analysis"
            return {**decision, "state": "applied", "gain_db": gain,
                    "source": "analysis-fallback" if mode == "album" else "analysis",
                    "text": f"{label}: {gain:+.1f} dB requested."}
        return {**decision, "state": "pending" if analysis_enabled else "no-data",
                "text": ("No loudness data yet; background analysis is queued." if analysis_enabled
                         else "No usable loudness data; no normalization adjustment.")}
    gain = max(-60.0, min(24.0, gain))
    peak = _tag_number(data.get(f"replaygain_{kind}_peak"))
    if (kind == "album" or decision["peak_source"] != "measured") and peak is not None and 0 < peak <= 10 ** 2.5:
        decision.update(peak_dbfs=max(-150.0, 20 * math.log10(peak)), peak_source="tags")
    source = "track-fallback" if fallback else f"{kind}-tags"
    label = "Track fallback" if fallback else f"{kind.title()} tags"
    return {**decision, "state": "applied", "gain_db": gain, "source": source,
            "text": f"{label}: {gain:+.1f} dB requested."}


def _tag_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        matched = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:dB)?\s*", value, re.IGNORECASE)
        if not matched:
            return None
        value = matched[1]
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _r128_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and len(value) <= 6 and re.fullmatch(r"[+-]?[0-9]+", value):
        number = int(value)
    else:
        return None
    return number if -32768 <= number <= 32767 else None


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
        self._loudness = {}
        self._processing_revision = 0

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
        deck.before_audio = self._before_audio
        deck._fade_multiplier = 0.0
        self._decks[identity] = deck
        delivery = self._delivery
        try:
            deck.initialize(lambda event: delivery.received.emit(identity, event))
            if deck.available:
                self._configure(deck)
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

    def _configure(self, deck):
        player = deck._require_player()
        configured = getattr(deck, "_native_values", {})
        for name, value in (("audio_samplerate", 0), ("audio_channels", "auto-safe"), ("volume_gain_min", -150),
                            ("audio_normalize_downmix", self._effective.peak_protection),
                            ("gapless_audio", "weak" if self._effective.gapless else "no")):
            if configured.get(name) != value:
                setattr(player, name, value)
                configured[name] = value
        if getattr(player, "audio_device", None) != self._device:
            player.audio_device = self._device
        for name, value in (("replaygain", "no"), ("replaygain_preamp", 0),
                            ("replaygain_fallback", 0), ("replaygain_clip", False)):
            if configured.get(name) != value:
                setattr(player, name, value)
                configured[name] = value
        deck._native_values = configured
        decision = getattr(deck, "_normalization", None)
        if (decision is None or decision["mode"] != self.settings.normalization
                or getattr(deck, "_analysis_enabled", None) != self.settings.analysis_enabled):
            decision = normalization_decision(self.settings.normalization,
                getattr(deck, "_normalization_tags", {}), getattr(deck, "_normalization_analysis", None),
                analysis_enabled=self.settings.analysis_enabled)
            deck._normalization = decision
        deck._analysis_enabled = self.settings.analysis_enabled
        self._configure_filters(deck, decision["gain_db"])

    @staticmethod
    def _gain_db(value):
        multiplier = value / 100
        return max(-150.0, 20 * math.log10(multiplier)) if multiplier > 0 else -150.0

    def _plan(self, deck, gain):
        return processing_plan(self._effective, gain,
            master_gain_db=self._gain_db(self._gain),
            source_peak_dbfs=getattr(deck, "_normalization", {}).get("peak_dbfs"),
            sample_rate=getattr(deck, "_source_rate", 48000),
            normalization_active=getattr(deck, "_normalization", {}).get("state") == "applied")

    def _set_native_gain(self, deck, gain_db, volume):
        state = (round(gain_db, 6), volume)
        if getattr(deck, "_gain_state", None) != state and deck.set_gain(gain_db, volume, immediate=True):
            deck._gain_state = state

    def _apply_native_level(self, deck, plan):
        fade = getattr(deck, "_fade_multiplier", 0.0)
        decibels = self._gain_db(fade * 100)
        if plan.bypass:
            decibels = max(-150.0, decibels + plan.master_db)
        self._set_native_gain(deck, decibels, 100 if fade > 0 and self._gain > 0 else 0)

    def _processing_status(self, deck, plan):
        return {"headroom_db": round(plan.headroom_db, 3),
                "net_preamp_db": round(plan.preamp_db, 3),
                "preamp_db": self.settings.eq_preamp if self.settings.eq_enabled else 0,
                "master_db": round(plan.master_db, 3), "protection": plan.protection,
                "bypass": plan.bypass, "sample_rate": plan.sample_rate,
                "peak_source": getattr(deck, "_normalization", {}).get("peak_source", "unknown")}

    def _publish_processing(self, deck, plan, *, force=False):
        if self._decks.get(self._active) is not deck:
            return
        status = self._processing_status(deck, plan)
        if force or getattr(deck, "_processing_status", None) != status:
            deck._processing_status = status
            self._emit("processing-status", status)
        decision = getattr(deck, "_normalization", {})
        current = {**decision, "requested_gain_db": decision.get("gain_db", 0),
                   "gain_db": plan.normalization_db}
        if current.get("state") == "applied":
            current["text"] = f"Loudness adjustment: {plan.normalization_db:+.1f} dB applied."
        if force or getattr(deck, "_published_normalization", None) != current:
            deck._published_normalization = current
            self._emit("normalization-status", current)

    def _configure_filters(self, deck, gain, *, immediate=False):
        player = deck._require_player()
        settings = self._effective
        revision = self._processing_revision
        plan = self._plan(deck, gain)
        values = plan.values
        topology = (plan.bypass, settings.eq_enabled, plan.protection, plan.sample_rate)
        if getattr(deck, "_filter_topology", None) != topology or immediate:
            filters = audio_filters(settings, gain, master_gain_db=plan.master_db,
                source_peak_dbfs=getattr(deck, "_normalization", {}).get("peak_dbfs"),
                sample_rate=getattr(deck, "_source_rate", 48000),
                normalization_active=getattr(deck, "_normalization", {}).get("state") == "applied")
            if plan.bypass:
                self._apply_native_level(deck, plan)
            if getattr(deck, "_filter_spec", None) != filters or getattr(deck, "_filter_values", None) != values:
                player.af = filters
            if not plan.bypass:
                self._apply_native_level(deck, plan)
            deck._filter_spec = filters
            deck._filter_topology = topology
            deck._filter_values = values
            deck._filter_target = values
            deck._filter_from = values
            deck._filter_started = self._clock()
        elif getattr(deck, "_filter_target", None) != values:
            current = getattr(deck, "_filter_values", values)
            if current[1] != values[1]:
                self._set_master_filter(deck, plan.master_db)
                current = deck._filter_values
            deck._filter_from = current
            deck._filter_target = values
            deck._filter_started = self._clock()
        self._publish_processing(deck, plan)
        self._apply_native_level(deck, plan)
        if revision != self._processing_revision:
            deck._normalization = normalization_decision(self.settings.normalization,
                getattr(deck, "_normalization_tags", {}), getattr(deck, "_normalization_analysis", None),
                analysis_enabled=self.settings.analysis_enabled)
            self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)

    def _set_master_filter(self, deck, gain_db):
        player = deck._require_player()
        if getattr(deck, "_filter_topology", (True,))[0]:
            self._apply_native_level(deck, self._plan(deck, deck._normalization["gain_db"]))
            return
        if getattr(player, "audio_params", None):
            try:
                player.command("af-command", "trackindex", "volume", f"{gain_db:.6f}dB", "volume@master")
            except Exception:
                self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)
                return
        else:
            self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)
            return
        self._apply_native_level(deck, self._plan(deck, deck._normalization["gain_db"]))
        for name in ("_filter_values", "_filter_target", "_filter_from"):
            values = getattr(deck, name, None)
            if values is not None:
                setattr(deck, name, (values[0], gain_db, *values[2:]))

    def _ramp_filters(self, deck, now):
        revision = (self._processing_revision, getattr(deck, "_file_revision", 0))
        target = getattr(deck, "_filter_target", None)
        current = getattr(deck, "_filter_values", None)
        if target is None or target == current or getattr(deck, "_filter_topology", (True,))[0]:
            return
        fraction = min(1.0, max(0.0, (now - deck._filter_started) / .1))
        start = deck._filter_from
        values = tuple(a + (b - a) * fraction for a, b in zip(start, target, strict=True))
        player = deck._require_player()
        if not getattr(player, "audio_params", None):
            return
        try:
            for index, (old, new) in enumerate(zip(current, values, strict=True)):
                if abs(old - new) < .00001:
                    continue
                if index == 0:
                    player.command("af-command", "trackindex", "volume", f"{new:.6f}dB", "volume@preamp")
                elif index == 1:
                    player.command("af-command", "trackindex", "volume", f"{new:.6f}dB", "volume@master")
                elif self._effective.eq_enabled:
                    player.command("af-command", "trackindex", "g", f"{new:.6f}", f"equalizer@band{index-2}")
            deck._filter_values = values
            if revision != (self._processing_revision, getattr(deck, "_file_revision", 0)):
                self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)
        except Exception:
            _LOG.exception("Live equalizer command failed; rebuilding its protected filter graph")
            self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path))

    def set_loudness(self, results):
        self._loudness = dict(results) if isinstance(results, dict) else {}
        if self.available:
            deck = self._decks[self._active]
            decision = getattr(deck, "_normalization", {})
            path = getattr(deck, "_normalization_path", "")
            result = self._analysis_for(path) if path else None
            if (decision.get("mode") != "off" and decision.get("state") in ("pending", "no-data")
                    and result is not None):
                measured = _tag_number(result.get("integrated_lufs"))
                deck._normalization = {**decision, "state": "ready" if measured is not None else "no-data",
                    "text": ("Analysis ready; loudness adjustment applies next playback." if measured is not None
                             else "No measurable loudness; no normalization adjustment.")}
                self._emit("normalization-status", deck._normalization)
        self._refresh_standby_analysis()

    def _refresh_standby_analysis(self):
        if self._standby is None or self._lookahead is None or not self._standby_ready:
            return
        deck = self._decks[self._standby]
        result = self._analysis_for(self._lookahead[0])
        if (self.settings.normalization != "off" and deck._normalization.get("state") in ("pending", "no-data")
                and result is not None and result != getattr(deck, "_normalization_analysis", None)):
            self._standby_ready = False
            try:
                deck.load(self._lookahead[0], paused=True, generation=self._lookahead[1])
            except Exception:
                _LOG.exception("Could not refresh prepared loudness; retaining native handoff")
                self._discard_standby()

    def _analysis_for(self, path, *, allow_disk=False):
        result = self._loudness.get(self._path_key(path))
        file_stat = regular_file_stat(Path(path))
        if file_stat is None:
            return None
        expected = {"size": file_stat.st_size, "mtime_ns": file_stat.st_mtime_ns,
                    "device": file_stat.st_dev, "inode": file_stat.st_ino,
                    "birthtime_ns": getattr(file_stat, "st_birthtime_ns", 0)}
        if isinstance(result, dict) and all(type(result.get(key)) is int and result[key] == value
                                           for key, value in expected.items()):
            return result
        if allow_disk:
            from .loudness_analysis import read_cached_result
            return read_cached_result(path)
        return None

    def _before_audio(self, deck):
        deck._file_revision = getattr(deck, "_file_revision", 0) + 1
        player = deck._require_player()
        path = str(player.path or "")
        deck._normalization_path = path
        deck._normalization_tags = player.metadata or {}
        tracks = getattr(player, "track_list", None) or []
        rates = [track.get("demux-samplerate") for track in tracks
                 if isinstance(track, dict) and track.get("type") == "audio"]
        deck._source_rate = max((rate for rate in rates if type(rate) is int and rate > 0), default=48000)
        deck._normalization_analysis = (self._analysis_for(path, allow_disk=True)
            if path and (self.settings.normalization != "off"
                         or (self.settings.eq_enabled and self.settings.eq_auto_headroom)) else None)
        deck._normalization = normalization_decision(self.settings.normalization,
            deck._normalization_tags, deck._normalization_analysis,
            analysis_enabled=self.settings.analysis_enabled)
        deck._analysis_enabled = self.settings.analysis_enabled
        self._configure_filters(deck, deck._normalization["gain_db"], immediate=True)

    def configure_audio(self, settings: AudioSettings):
        previous = self.settings
        self.settings = settings
        if settings.device != previous.device:
            self._failed_devices.discard(settings.device)
            self._device = settings.device
            self._cancel_overlap()
        if self._tail is None:
            self._effective = settings
        else:
            self._effective = replace(self._effective, eq_enabled=settings.eq_enabled,
                                      eq_gains=settings.eq_gains, eq_preamp=settings.eq_preamp,
                                      eq_auto_headroom=settings.eq_auto_headroom, device=settings.device,
                                      normalization=settings.normalization, analysis_enabled=settings.analysis_enabled,
                                      peak_protection=settings.peak_protection)
        self._processing_revision += 1
        for identity, deck in tuple(self._decks.items()):
            if deck.available:
                path = getattr(deck, "_normalization_path", "")
                if path and settings.normalization != previous.normalization:
                    deck._normalization_analysis = self._analysis_for(path, allow_disk=True)
                elif path and settings.eq_enabled and not previous.eq_enabled:
                    result = self._analysis_for(path, allow_disk=True)
                    if result is not None:
                        peak = _tag_number(result.get("true_peak_dbfs"))
                        if peak is not None and -150 <= peak <= 50:
                            deck._normalization = {**deck._normalization,
                                                   "peak_dbfs": peak, "peak_source": "measured"}
                if identity == self._active:
                    # Confirm the applied settings even when the reported gains are identical.
                    deck._processing_status = None
                self._configure(deck)
        if not settings.crossfade and self._tail is None:
            self._discard_standby()
        self._set_gain_target()
        if settings.device != previous.device:
            self.refresh_devices()
        self._prepare_standby()

    def _set_gain_target(self):
        self._ramp_from = self._gain
        self._ramp_to = 100 * (self._volume / 100) ** 3
        self._ramp_started = self._clock()

    def _write_gain(self, identity, value, *, immediate=False):
        deck = self._decks[identity]
        fade = max(0.0, min(1.0, value / self._gain)) if self._gain > 0 else 0.0
        state = (round(self._gain_db(self._gain), 6), round(fade, 9), self._processing_revision,
                 getattr(deck, "_file_revision", 0))
        if getattr(deck, "_master_state", None) == state:
            return
        deck._fade_multiplier = fade
        self._configure_filters(deck, deck._normalization["gain_db"], immediate=immediate)
        deck._master_state = state

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
        has_loaded_track = bool(self._decks[self._active]._require_player().path)
        self._failed_devices.add(missing)
        self._device = "auto"
        self.settings = replace(self.settings, device="auto")
        self._effective = replace(self._effective, device="auto")
        self._cancel_overlap()
        for deck in self._decks.values():
            if deck.available:
                deck._require_player().audio_device = "auto"
        self._emit("device-fallback", missing)
        if has_loaded_track:
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
        self._configure(deck)
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
            self._effective = self.settings
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
            missing = self._device
            self._failed_devices.add(missing)
            self._device = "auto"
            self.settings = replace(self.settings, device="auto")
            self._effective = replace(self._effective, device="auto")
            self._emit("device-fallback", missing)
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
                self._refresh_standby_analysis()
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
            self._configure(self._decks[identity])
            self._set_gain_target()
        if event.generation == self._generation:
            if event.kind == "position":
                self._position = self._finite(event.value)
            elif event.kind == "duration":
                self._duration = self._finite(event.value)
        self._callback(event)
        if event.kind == "loaded":
            deck = self._decks[identity]
            self._publish_processing(deck, self._plan(deck, deck._normalization["gain_db"]), force=True)

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
        for deck in tuple(self._decks.values()):
            if deck.available:
                self._ramp_filters(deck, now)
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
        if duration <= 0 or remaining > duration or remaining <= 0.05:
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
        deck = self._decks[self._active]
        self._publish_processing(deck, self._plan(deck, deck._normalization["gain_db"]), force=True)
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
