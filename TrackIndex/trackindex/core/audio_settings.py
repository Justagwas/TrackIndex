from __future__ import annotations

import cmath
from dataclasses import asdict, dataclass
from functools import lru_cache
import math

EQ_FREQUENCIES = (31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_PRESETS = {
    "Flat": (0.0,) * 10,
    "Bass warmth": (3, 3, 2, 1, 0, 0, 0, 0, 0, 0),
    "Vocal clarity": (0, 0, 0, 0, 0, 1, 2, 2, -1, 0),
}


def _number(value: object, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value))) if math.isfinite(value) else default


@dataclass(frozen=True, slots=True)
class AudioSettings:
    gapless: bool = True
    normalization: str = "track"
    analysis_enabled: bool = True
    peak_protection: bool = True
    eq_enabled: bool = False
    eq_gains: tuple[float, ...] = (0.0,) * 10
    eq_preamp: float = 0.0
    eq_auto_headroom: bool = True
    crossfade: bool = False
    crossfade_seconds: float = 5.0
    preserve_albums: bool = True
    device: str = "auto"

    @classmethod
    def from_dict(cls, value: object) -> AudioSettings:
        data = value if isinstance(value, dict) else {}
        defaults = cls()
        flags = {key: data[key] if type(data.get(key)) is bool else getattr(defaults, key)
                 for key in ("gapless", "peak_protection", "eq_enabled", "eq_auto_headroom", "crossfade", "preserve_albums")}
        gains = data.get("eq_gains")
        gains = gains if isinstance(gains, (list, tuple)) and len(gains) == 10 else defaults.eq_gains
        device = data.get("device", "auto")
        mode = data.get("normalization")
        analysis = data.get("analysis_enabled")
        if type(analysis) is not bool:
            analysis = mode not in ("track", "album")
        return cls(
            **flags,
            normalization=mode if mode in ("off", "track", "album") else defaults.normalization,
            analysis_enabled=analysis,
            eq_gains=tuple(round(_number(v, 0, -12, 12) * 2) / 2 for v in gains),
            eq_preamp=round(_number(data.get("eq_preamp"), 0, -12, 12) * 2) / 2,
            crossfade_seconds=round(_number(data.get("crossfade_seconds"), 5, 1, 12) * 2) / 2,
            device=device if isinstance(device, str) and 0 < len(device) <= 1024 and "\0" not in device else "auto",
        )

    def to_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=128)
def eq_headroom(gains: tuple[float, ...], sample_rate: int = 48000) -> float:
    sample_rate = max(48000, min(768000, sample_rate))
    if not any(gain > 0 for gain in gains):
        return 0.0
    if sum(gain != 0 for gain in gains) == 1:
        return max(gains)
    filters = []
    for frequency, gain in zip(EQ_FREQUENCIES, gains, strict=True):
        if not gain:
            continue
        w = 2 * math.pi * frequency / sample_rate
        amplitude = 10 ** (gain / 40)
        alpha = math.sin(w) * math.sinh(math.log(2) / 2 * w / math.sin(w))
        filters.append(((1 + alpha * amplitude, -2 * math.cos(w), 1 - alpha * amplitude),
                        (1 + alpha / amplitude, -2 * math.cos(w), 1 - alpha / amplitude)))
    peak = 1.0
    nyquist = sample_rate / 2
    frequencies = (0, nyquist, *EQ_FREQUENCIES,
                   *(20 * (nyquist / 20) ** (index / 2048) for index in range(2049)))
    for frequency in frequencies:
        z = cmath.exp(-2j * math.pi * frequency / sample_rate)
        response = 1.0
        for b, a in filters:
            response *= abs((b[0] + b[1] * z + b[2] * z*z) / (a[0] + a[1] * z + a[2] * z*z))
        peak = max(peak, response)
    return 20 * math.log10(peak)


@dataclass(frozen=True, slots=True)
class ProcessingPlan:
    preamp_db: float
    master_db: float
    normalization_db: float
    bands: tuple[float, ...]
    headroom_db: float
    sample_rate: int
    protection: bool
    bypass: bool

    @property
    def values(self) -> tuple[float, ...]:
        return (self.preamp_db, self.master_db, *self.bands)


def processing_plan(settings: AudioSettings, normalization_gain: float = 0.0, *,
                    master_gain_db: float = 0.0, source_peak_dbfs: float | None = None,
                    sample_rate: int = 48000,
                    normalization_active: bool | None = None) -> ProcessingPlan:
    gain = _number(normalization_gain, 0, -60, 24) if settings.normalization != "off" else 0.0
    master = _number(master_gain_db, 0, -150, 0)
    rate = max(48000, min(768000, sample_rate)) if settings.eq_enabled else 0
    bands = settings.eq_gains if settings.eq_enabled else (0.0,) * 10
    peak = (_number(source_peak_dbfs, 0, -150, 50)
            if source_peak_dbfs is not None else 0.0)
    manual = settings.eq_preamp if settings.eq_enabled else 0.0
    if normalization_active is None:
        normalization_active = bool(gain)
    if (settings.peak_protection and settings.normalization != "off"
            and normalization_active and source_peak_dbfs is not None):
        gain = max(-60.0, min(gain, -1.0 - peak - manual))
    normalized = gain
    reduction = 0.0
    if settings.eq_enabled:
        gain += manual
        response = eq_headroom(settings.eq_gains, rate)
        if settings.eq_auto_headroom and manual + response > 0:
            ceiling = -1.0 if settings.peak_protection else 0.0
            reduction = max(0.0, peak + gain + response - ceiling)
            gain -= reduction
    bypass = not settings.eq_enabled and settings.normalization == "off" and not settings.peak_protection
    return ProcessingPlan(gain, master, normalized, bands, reduction, rate, settings.peak_protection, bypass)


def processing_values(settings: AudioSettings, normalization_gain: float = 0.0, **kwargs) -> tuple[float, ...]:
    return processing_plan(settings, normalization_gain, **kwargs).values


def audio_filters(settings: AudioSettings, normalization_gain: float = 0.0, **kwargs) -> str:
    plan = processing_plan(settings, normalization_gain, **kwargs)
    if plan.bypass:
        return ""
    filters = ([f"aresample={plan.sample_rate}:osf=fltp"] if settings.eq_enabled
               else ["aformat=sample_fmts=fltp"])
    filters.append(f"volume@preamp={plan.preamp_db:.6f}dB")
    if settings.eq_enabled:
        filters.extend(f"equalizer@band{index}=f={frequency}:t=o:w=1:g={gain}"
                       for index, (frequency, gain) in enumerate(zip(EQ_FREQUENCIES, plan.bands, strict=True)))
    filters.append(f"volume@master={plan.master_db:.6f}dB")
    if plan.protection:
        filters.append("alimiter=limit=0.891250938:level=false:latency=true")
    return "@trackindex:lavfi=[" + ",".join(filters) + "]"


@dataclass(frozen=True, slots=True)
class AlbumInfo:
    album: str = ""
    artist: str = ""
    disc: int = 1
    track: int = 0
    total_tracks: int = 0

    @classmethod
    def from_tags(cls, tags: object) -> AlbumInfo:
        data = {str(k).casefold().replace("_", "").replace(" ", ""): str(v).strip()
                for k, v in tags.items()} if isinstance(tags, dict) else {}
        def number(value):
            try:
                return max(0, int(value))
            except (ValueError, TypeError):
                return 0
        track = data.get("track", data.get("tracknumber", "")).split("/")
        disc = data.get("disc", data.get("discnumber", "1")).split("/")[0]
        return cls(data.get("album", "").casefold(),
                   data.get("albumartist", data.get("artist", "")).casefold(),
                   number(disc) or 1, number(track[0]),
                   number(track[1] if len(track) > 1 else data.get("tracktotal", data.get("totaltracks", "0"))))

    def followed_by(self, other: AlbumInfo) -> bool:
        if not self.album or not self.artist or (self.album, self.artist) != (other.album, other.artist):
            return False
        return self.track > 0 and (
            (self.disc == other.disc and other.track == self.track + 1)
            or (other.disc == self.disc + 1 and other.track == 1 and self.track == self.total_tracks)
        )


def fade_duration(requested: float, outgoing: float, incoming: float) -> float:
    if not all(math.isfinite(v) and v > 0 for v in (requested, outgoing, incoming)):
        return 0.0
    duration = min(requested, outgoing / 4, incoming / 4)
    return duration if duration >= 0.5 else 0.0


def fade_weights(progress: float) -> tuple[float, float]:
    angle = max(0.0, min(1.0, progress)) * math.pi / 2
    outgoing, incoming = math.cos(angle), math.sin(angle)
    total = outgoing + incoming
    return outgoing / total, incoming / total
