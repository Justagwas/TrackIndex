from __future__ import annotations

import random
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path


class PlaybackState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class PlayerPresentation(StrEnum):
    HIDDEN = "hidden"
    MINI = "mini"
    EXPANDED = "expanded"


class RepeatMode(StrEnum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


@dataclass(frozen=True, slots=True)
class SourceAudioInfo:
    extension: str = ""
    container: str = ""
    codec: str = ""
    bitrate: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    channel_layout: str = ""


@dataclass(frozen=True, slots=True)
class OutputAudioInfo:
    device: str = "System default"
    sample_rate: int | None = None
    sample_format: str = ""
    channels: int | None = None
    channel_layout: str = ""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    library_id: str
    track_id: str
    path: Path
    title: str
    artist: str = ""
    album: str = ""
    duration_seconds: float | None = None
    artwork_bytes: bytes | None = None
    source: SourceAudioInfo = SourceAudioInfo()


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    state: PlaybackState = PlaybackState.IDLE
    presentation: PlayerPresentation = PlayerPresentation.HIDDEN
    entry: QueueEntry | None = None
    position_seconds: float = 0.0
    duration_seconds: float | None = None
    volume: int = 75
    muted: bool = False
    shuffle: bool = False
    repeat: RepeatMode = RepeatMode.OFF
    output: OutputAudioInfo = OutputAudioInfo()
    loading: bool = False
    error: str = ""


class PlaybackQueue:
    def __init__(
        self,
        *,
        shuffle: bool = False,
        repeat: RepeatMode = RepeatMode.OFF,
        random_seed: int | None = None,
    ) -> None:
        self.entries: tuple[QueueEntry, ...] = ()
        self.current_id = ""
        self.shuffle = bool(shuffle)
        self.repeat = repeat
        self.unavailable: set[str] = set()
        self.history: list[str] = []
        self._future: list[str] = []
        self._random = random.Random(random_seed)

    def clear(self) -> None:
        self.entries = ()
        self.current_id = ""
        self.unavailable.clear()
        self.history.clear()
        self._future.clear()

    def start(self, entries: tuple[QueueEntry, ...], track_id: str) -> QueueEntry | None:
        self.entries = self._deduplicated(entries)
        self.unavailable.intersection_update(entry.track_id for entry in self.entries)
        if not any(entry.track_id == track_id for entry in self.entries):
            self.current_id = ""
            self.history.clear()
            self._future.clear()
            return None
        self.current_id = track_id
        self.history = []
        self._rebuild_future()
        return self.current

    @property
    def current(self) -> QueueEntry | None:
        return next((entry for entry in self.entries if entry.track_id == self.current_id), None)

    @property
    def lookahead(self) -> QueueEntry | None:
        track_id = self.peek_next_id(natural=True)
        return self.entry(track_id) if track_id else None

    def entry(self, track_id: str) -> QueueEntry | None:
        return next((entry for entry in self.entries if entry.track_id == track_id), None)

    def set_shuffle(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.shuffle:
            return
        self.shuffle = enabled
        self._rebuild_future()

    def set_repeat(self, mode: RepeatMode) -> None:
        self.repeat = mode
        if self.shuffle and mode is RepeatMode.ALL and not self._future:
            self._rebuild_shuffle_cycle()

    def mark_unavailable(self, track_id: str) -> None:
        self.unavailable.add(track_id)
        self.history = [item for item in self.history if item != track_id]
        self._future = [item for item in self._future if item != track_id]

    def peek_next_id(self, *, natural: bool = False) -> str:
        if not self.current_id:
            return ""
        if natural and self.repeat is RepeatMode.ONE and self.current_id not in self.unavailable:
            return self.current_id
        if self.shuffle:
            candidate = next((item for item in self._future if item not in self.unavailable), "")
            if candidate:
                return candidate
            if self.repeat is RepeatMode.ALL:
                self._rebuild_shuffle_cycle()
                return self._future[0] if self._future else ""
            return ""
        ids = [entry.track_id for entry in self.entries]
        try:
            start = ids.index(self.current_id) + 1
        except ValueError:
            return ""
        for item in ids[start:]:
            if item not in self.unavailable:
                return item
        if self.repeat is RepeatMode.ALL:
            for item in ids[:start]:
                if item not in self.unavailable:
                    return item
        return ""

    def advance(self, *, natural: bool = False) -> QueueEntry | None:
        track_id = self.peek_next_id(natural=natural)
        if not track_id:
            return None
        if track_id != self.current_id:
            if self.current_id not in self.unavailable:
                self.history.append(self.current_id)
                self.history = self.history[-len(self.entries) :]
            self._future = [item for item in self._future if item != track_id]
            self.current_id = track_id
            if self.shuffle and self.repeat is RepeatMode.ALL and not self._future:
                self._rebuild_shuffle_cycle()
        return self.current

    def previous(self) -> QueueEntry | None:
        if self.shuffle:
            while self.history:
                previous_id = self.history.pop()
                if previous_id in self.unavailable or self.entry(previous_id) is None:
                    continue
                self._future = [item for item in self._future if item != previous_id]
                if (
                    self.current_id
                    and self.current_id not in self.unavailable
                    and self.current_id not in self._future
                ):
                    self._future.insert(0, self.current_id)
                self.current_id = previous_id
                return self.current
            return None
        ids = [entry.track_id for entry in self.entries]
        try:
            position = ids.index(self.current_id)
        except ValueError:
            return None
        for item in reversed(ids[:position]):
            if item not in self.unavailable:
                self.current_id = item
                return self.current
        if self.repeat is RepeatMode.ALL:
            for item in reversed(ids[position + 1 :]):
                if item not in self.unavailable:
                    self.current_id = item
                    return self.current
        return None

    def reconcile(self, entries: tuple[QueueEntry, ...]) -> None:
        incoming = self._deduplicated(entries)
        ids = {entry.track_id for entry in incoming}
        old_future = [
            item for item in self._future if item in ids and item not in self.unavailable
        ]
        self.entries = incoming
        self.unavailable.intersection_update(ids)
        self.history = [
            item for item in self.history if item in ids and item not in self.unavailable
        ][-len(incoming) :]
        if self.current_id not in ids:
            self.current_id = ""
            self._future.clear()
            return
        if self.shuffle:
            used = set(self.history) | {self.current_id} | set(old_future) | self.unavailable
            added = [entry.track_id for entry in incoming if entry.track_id not in used]
            self._random.shuffle(added)
            self._future = old_future + added
            if self.repeat is RepeatMode.ALL and not self._future:
                self._rebuild_shuffle_cycle()
        else:
            self._rebuild_future()

    def replace_paths(self, paths: dict[str, Path]) -> None:
        self.entries = tuple(
            replace(entry, path=paths.get(entry.track_id, entry.path)) for entry in self.entries
        )

    def _rebuild_future(self) -> None:
        ids = [entry.track_id for entry in self.entries if entry.track_id not in self.unavailable]
        if self.shuffle:
            self._future = [item for item in ids if item != self.current_id]
            self._random.shuffle(self._future)
            return
        try:
            position = ids.index(self.current_id)
        except ValueError:
            self._future = []
            return
        self._future = ids[position + 1 :]

    def _rebuild_shuffle_cycle(self) -> None:
        self._future = [
            entry.track_id
            for entry in self.entries
            if entry.track_id != self.current_id and entry.track_id not in self.unavailable
        ]
        self._random.shuffle(self._future)

    @staticmethod
    def _deduplicated(entries: tuple[QueueEntry, ...]) -> tuple[QueueEntry, ...]:
        seen: set[str] = set()
        result: list[QueueEntry] = []
        for entry in entries:
            if not entry.track_id or entry.track_id in seen:
                continue
            seen.add(entry.track_id)
            result.append(entry)
        return tuple(result)
