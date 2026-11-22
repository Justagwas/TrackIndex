from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from time import monotonic
import uuid

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from trackindex.core.metadata_service import read_track_metadata
from trackindex.core.models import TrackMetadata, TrackRecord


@dataclass(frozen=True, slots=True)
class SearchBatch:
    task_id: str
    revision: int
    values: tuple[tuple[TrackRecord, TrackMetadata], ...]
    finished: bool = False


class SearchSignals(QObject):
    ready = Signal(object)


class SearchMetadataTask(QRunnable):
    def __init__(self, revision: int, tracks: tuple[TrackRecord, ...], filesystem_access) -> None:
        super().__init__()
        self.task_id = uuid.uuid4().hex
        self.revision = revision
        self.tracks = tracks
        self.filesystem_access = filesystem_access
        self.signals = SearchSignals()
        self._stop = Event()

    def stop(self) -> None:
        self._stop.set()

    @Slot()
    def run(self) -> None:
        batch = []
        delivered = monotonic()
        try:
            for track in self.tracks:
                if self._stop.is_set():
                    return
                with self.filesystem_access():
                    if self._stop.is_set():
                        return
                    try:
                        stat = track.path.stat()
                        if (stat.st_size, stat.st_mtime_ns) != (track.size, track.mtime_ns):
                            continue
                        metadata = read_track_metadata(track.path, include_artwork=False)
                        stat = track.path.stat()
                        if (stat.st_size, stat.st_mtime_ns) != (track.size, track.mtime_ns):
                            continue
                    except OSError:
                        continue
                if self._stop.is_set():
                    return
                batch.append((track, metadata))
                if len(batch) >= 32 or monotonic() - delivered >= 0.1:
                    self.signals.ready.emit(SearchBatch(self.task_id, self.revision, tuple(batch)))
                    batch.clear()
                    delivered = monotonic()
        finally:
            if not self._stop.is_set():
                self.signals.ready.emit(SearchBatch(self.task_id, self.revision, tuple(batch), finished=True))
