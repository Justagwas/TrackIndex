from __future__ import annotations

from collections import OrderedDict
import unicodedata

from .models import TrackMetadata, TrackRecord


def normalized_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def track_search_key(track: TrackRecord) -> tuple[str, int, int]:
    return str(track.path).casefold(), track.size, track.mtime_ns


class TrackSearchIndex:
    def __init__(self, cache_size: int = 8192) -> None:
        self.cache_size = max(1, cache_size)
        self.tracks: tuple[TrackRecord, ...] = ()
        self._by_id: dict[str, TrackRecord] = {}
        self._fields: dict[str, tuple[str, ...]] = {}
        self._known: set[str] = set()
        self._cache: OrderedDict[tuple[str, int, int], tuple[str, ...]] = OrderedDict()

    def reset(self, tracks: tuple[TrackRecord, ...], metadata: dict[str, TrackMetadata]) -> None:
        previous = {
            track_id: (track_search_key(track), self._fields[track_id][1:])
            for track_id, track in self._by_id.items() if track_id in self._known
        }
        self.tracks = tracks
        self._by_id = {track.track_id: track for track in tracks}
        self._fields = {}
        self._known = set()
        for track in tracks:
            self._fields[track.track_id] = (normalized_search_text(track.original_name),)
            value = metadata.get(track.track_id)
            if value is not None:
                self.put(track, value)
                continue
            key = track_search_key(track)
            old = previous.get(track.track_id)
            fields = old[1] if old is not None and old[0] == key else self._cache.get(key)
            if fields is not None:
                self._fields[track.track_id] += fields
                self._known.add(track.track_id)
                if key in self._cache:
                    self._cache.move_to_end(key)

    def put(self, track: TrackRecord, metadata: TrackMetadata) -> bool:
        current = self._by_id.get(track.track_id)
        if current is None or track_search_key(current) != track_search_key(track):
            return False
        fields = tuple(normalized_search_text(value) for value in (metadata.title, metadata.artist, metadata.album))
        self._fields[track.track_id] = (normalized_search_text(current.original_name), *fields)
        self._known.add(track.track_id)
        key = track_search_key(track)
        self._cache[key] = fields
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return True

    def missing_tracks(self) -> tuple[TrackRecord, ...]:
        return tuple(track for track in self.tracks if track.track_id not in self._known)

    def matches(self, query: str) -> tuple[str, ...]:
        needle = normalized_search_text(query.strip())
        if not needle:
            return ()
        return tuple(track.track_id for track in self.tracks
                     if any(needle in field for field in self._fields[track.track_id]))
