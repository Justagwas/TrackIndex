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
    normalization: str = "off"
    eq_enabled: bool = False
    eq_gains: tuple[float, ...] = (0.0,) * 10
    crossfade: bool = False
    crossfade_seconds: float = 5.0
    preserve_albums: bool = True
    device: str = "auto"

    @classmethod
    def from_dict(cls, value: object) -> AudioSettings:
        data = value if isinstance(value, dict) else {}
        defaults = cls()
        flags = {key: data[key] if type(data.get(key)) is bool else getattr(defaults, key)
                 for key in ("gapless", "eq_enabled", "crossfade", "preserve_albums")}
        gains = data.get("eq_gains")
        gains = gains if isinstance(gains, (list, tuple)) and len(gains) == 10 else defaults.eq_gains
        device = data.get("device", "auto")
        return cls(
            **flags,
            normalization=data.get("normalization") if data.get("normalization") in ("off", "track", "album") else "off",
            eq_gains=tuple(round(_number(v, 0, -12, 12) * 2) / 2 for v in gains),
            crossfade_seconds=round(_number(data.get("crossfade_seconds"), 5, 1, 12) * 2) / 2,
            device=device if isinstance(device, str) and 0 < len(device) <= 1024 and "\0" not in device else "auto",
        )

    def to_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=128)
def eq_headroom(gains: tuple[float, ...]) -> float:
    if not any(gains):
        return 0.0
    filters = []
    for frequency, gain in zip(EQ_FREQUENCIES, gains, strict=True):
        w = 2 * math.pi * frequency / 48000
        amplitude = 10 ** (gain / 40)
        alpha = math.sin(w) * math.sinh(math.log(2) / 2 * w / math.sin(w))
        filters.append(((1 + alpha * amplitude, -2 * math.cos(w), 1 - alpha * amplitude),
                        (1 + alpha / amplitude, -2 * math.cos(w), 1 - alpha / amplitude)))
    peak = 1.0
    frequencies = (0, 24000, *EQ_FREQUENCIES,
                   *(20 * 1200 ** (index / 2048) for index in range(2049)))
    for frequency in frequencies:
        z = cmath.exp(-2j * math.pi * frequency / 48000)
        response = 1.0
        for b, a in filters:
            response *= abs((b[0] + b[1] * z + b[2] * z*z) / (a[0] + a[1] * z + a[2] * z*z))
        peak = max(peak, response)
    return 20 * math.log10(peak) + 1.0


def audio_filters(settings: AudioSettings) -> str:
    filters = []
    if settings.eq_enabled and any(settings.eq_gains):
        filters.append(f"volume={-eq_headroom(settings.eq_gains):.4f}dB")
        filters.extend(f"equalizer=f={frequency}:t=o:w=1:g={gain}"
                       for frequency, gain in zip(EQ_FREQUENCIES, settings.eq_gains, strict=True) if gain)
    if settings.eq_enabled or settings.normalization != "off" or settings.crossfade:
        filters.append("alimiter=limit=0.891250938:level=false:latency=true")
    return "@trackindex:lavfi=[" + ",".join(filters) + "]" if filters else ""


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
    return math.cos(angle), math.sin(angle)
