from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QImage, QImageWriter


@dataclass(frozen=True, slots=True)
class PreparedArtwork:
    key: str
    thumbnail_bytes: bytes


class ArtworkStore:
    def __init__(
        self,
        *,
        thumbnail_edge: int = 256,
        max_entries: int = 512,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.thumbnail_edge = max(32, int(thumbnail_edge))
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._values: OrderedDict[str, bytes] = OrderedDict()
        self._failed: set[str] = set()
        self._inflight: dict[str, Event] = {}
        self._bytes = 0
        self._decode_count = 0
        self._lock = Lock()

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._values)

    @property
    def byte_size(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def decode_count(self) -> int:
        with self._lock:
            return self._decode_count

    def prepare(
        self,
        source: bytes | None,
        cancelled: Callable[[], bool] | None = None,
        wait_timeout: float = 10.0,
    ) -> PreparedArtwork | None:
        if not source:
            return None
        key = hashlib.sha256(source).hexdigest()
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                self._values.move_to_end(key)
                return PreparedArtwork(key, cached)
            if key in self._failed:
                return None
            ready = self._inflight.get(key)
            prepare_here = ready is None
            if ready is None:
                ready = Event()
                self._inflight[key] = ready
                self._decode_count += 1

        if not prepare_here:
            deadline = time.monotonic() + max(0.0, wait_timeout)
            while not ready.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                if (cancelled is not None and cancelled()) or time.monotonic() >= deadline:
                    return None
            with self._lock:
                cached = self._values.get(key)
                if cached is not None:
                    self._values.move_to_end(key)
                    return PreparedArtwork(key, cached)
                return None

        try:
            thumbnail = self._make_thumbnail(source)
        except Exception:
            thumbnail = b""

        with self._lock:
            if not thumbnail:
                self._failed.add(key)
                result = None
            else:
                self._values[key] = thumbnail
                self._bytes += len(thumbnail)
                self._values.move_to_end(key)
                while (
                    len(self._values) > self.max_entries
                    or self._bytes > self.max_bytes
                ):
                    _discarded_key, discarded = self._values.popitem(last=False)
                    self._bytes -= len(discarded)
                result = PreparedArtwork(key, thumbnail)
            completed = self._inflight.pop(key, None)
            if completed is not None:
                completed.set()
            return result

    def _make_thumbnail(self, source: bytes) -> bytes:
        image = QImage.fromData(QByteArray(source))
        if image.isNull():
            return b""
        crop_size = min(image.width(), image.height())
        if crop_size <= 0:
            return b""
        crop = QRect(
            (image.width() - crop_size) // 2,
            (image.height() - crop_size) // 2,
            crop_size,
            crop_size,
        )
        thumbnail = image.copy(crop).scaled(
            self.thumbnail_edge,
            self.thumbnail_edge,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return b""
        writer = QImageWriter(buffer, QByteArray(b"PNG"))
        if not writer.write(thumbnail):
            return b""
        return bytes(buffer.data().data())
