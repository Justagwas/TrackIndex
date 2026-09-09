from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from .filesystem import regular_file_stat
from .models import TrackMetadata
from .scanner import parse_filename_index

_COVER_STEMS = ("cover", "folder", "front", "album")
_COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_MAX_ARTWORK_BYTES = 16 * 1024 * 1024
_BACKEND_LOCK = Lock()
_metadata_backends: dict[str, tuple[Any | None, tuple[type, ...], str]] = {}


class _TagMapping(Protocol):
    def get(self, key: str, default: object = ...) -> object: ...


def prepare_all_metadata_backends() -> None:
    for suffix in (".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".aiff"):
        prepare_metadata_backend(suffix)


def prepare_metadata_backend(suffix: str = "") -> tuple[Any | None, tuple[type, ...], str]:
    key = suffix.casefold()
    cached = _metadata_backends.get(key)
    if cached is None:
        with _BACKEND_LOCK:
            cached = _metadata_backends.get(key)
            if cached is None:
                try:
                    import mutagen

                    options: tuple[type, ...]
                    if key == ".mp3":
                        from mutagen.mp3 import MP3
                        options = (MP3,)
                    elif key == ".wav":
                        from mutagen.wave import WAVE
                        options = (WAVE,)
                    elif key == ".flac":
                        from mutagen.flac import FLAC
                        options = (FLAC,)
                    elif key == ".opus":
                        from mutagen.oggopus import OggOpus
                        options = (OggOpus,)
                    elif key == ".ogg":
                        from mutagen.oggflac import OggFLAC
                        from mutagen.oggspeex import OggSpeex
                        from mutagen.oggvorbis import OggVorbis
                        options = (OggVorbis, OggFLAC, OggSpeex)
                    elif key == ".m4a":
                        from mutagen.mp4 import MP4
                        options = (MP4,)
                    elif key == ".aac":
                        from mutagen.aac import AAC
                        options = (AAC,)
                    elif key == ".wma":
                        from mutagen.asf import ASF
                        options = (ASF,)
                    elif key in {".aif", ".aiff"}:
                        from mutagen.aiff import AIFF
                        options = (AIFF,)
                    else:
                        options = ()
                    cached = (mutagen.File, options, "")
                except Exception as exc:
                    cached = (None, (), str(exc))
                _metadata_backends[key] = cached
    return cached


def folder_artwork(folder: Path) -> bytes | None:
    try:
        files: dict[str, tuple[Path, int]] = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                entry_stat = regular_file_stat(entry)
                if entry_stat is None:
                    continue
                files[entry.name.casefold()] = (folder / entry.name, entry_stat.st_size)
        for stem in _COVER_STEMS:
            for suffix in _COVER_SUFFIXES:
                match = files.get(f"{stem}{suffix}")
                if match:
                    candidate, size = match
                    if size > _MAX_ARTWORK_BYTES:
                        continue
                    with candidate.open("rb") as handle:
                        data = handle.read(_MAX_ARTWORK_BYTES + 1)
                    if len(data) <= _MAX_ARTWORK_BYTES:
                        return data
    except OSError:
        pass
    return None


def _first(tags: _TagMapping | None, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            if value:
                return str(value)
        except Exception:
            continue
    return ""


def _embedded_art(audio: object) -> bytes | None:
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    try:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            data = bytes(pictures[0].data)
            return data if len(data) <= _MAX_ARTWORK_BYTES else None
        for value in tags.values():
            embedded_data = getattr(value, "data", None)
            if embedded_data and value.__class__.__name__.upper().startswith("APIC"):
                result = bytes(embedded_data)
                return result if len(result) <= _MAX_ARTWORK_BYTES else None
        covers = tags.get("covr")
        if covers:
            data = bytes(covers[0])
            return data if len(data) <= _MAX_ARTWORK_BYTES else None
        picture = tags.get("metadata_block_picture")
        if picture:
            import base64

            from mutagen.flac import Picture

            decoded = base64.b64decode(picture[0])
            parsed = Picture(decoded)
            data = bytes(parsed.data)
            return data if len(data) <= _MAX_ARTWORK_BYTES else None
    except Exception:
        return None
    return None


def read_track_metadata(
    path: Path,
    fallback_artwork: bytes | None = None,
    backend: tuple[Any | None, tuple[type, ...], str] | None = None,
) -> TrackMetadata:
    _, fallback_title = parse_filename_index(path)
    try:
        file_loader, options, backend_error = backend or prepare_metadata_backend(path.suffix)
        if file_loader is None:
            raise RuntimeError(backend_error or "Mutagen is unavailable")
        if not options:
            raise RuntimeError(f"No metadata reader is available for {path.suffix or 'this file'}")
        audio = file_loader(path, options=options, easy=False)
        tags = getattr(audio, "tags", None)
        title = _first(tags, "title", "TITLE", "TIT2", "\xa9nam", "Title") or fallback_title
        artist = _first(
            tags, "artist", "ARTIST", "TPE1", "\xa9ART", "Author",
            "albumartist", "ALBUMARTIST", "TPE2", "aART", "WM/AlbumArtist",
        )
        album = _first(tags, "album", "ALBUM", "TALB", "\xa9alb", "WM/AlbumTitle")
        info = getattr(audio, "info", None)
        length = getattr(info, "length", None)
        bitrate = getattr(info, "bitrate", None)
        sample_rate = getattr(info, "sample_rate", None)
        bit_depth = getattr(info, "bits_per_sample", None)
        channels = getattr(info, "channels", None)
        codec = audio.__class__.__name__
        return TrackMetadata(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=float(length) if length else None,
            artwork_bytes=_embedded_art(audio) or fallback_artwork,
            container=path.suffix.removeprefix(".").upper(),
            codec=codec,
            bitrate=int(bitrate) if bitrate else None,
            sample_rate=int(sample_rate) if sample_rate else None,
            bit_depth=int(bit_depth) if bit_depth else None,
            channels=int(channels) if channels else None,
        )
    except Exception as exc:
        return TrackMetadata(fallback_title, artwork_bytes=fallback_artwork, error=str(exc))


class MetadataCache:
    def __init__(self, max_entries: int = 1024, max_artwork_bytes: int = 64 * 1024 * 1024) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_artwork_bytes = max(0, int(max_artwork_bytes))
        self._values: OrderedDict[tuple[str, int, int], TrackMetadata] = OrderedDict()
        self._artwork_bytes = 0
        self._artwork_references: dict[object, tuple[int, int]] = {}

    @staticmethod
    def _artwork_identity(value: TrackMetadata) -> object | None:
        if not value.artwork_bytes:
            return None
        return value.artwork_key or id(value.artwork_bytes)

    def _retain_artwork(self, value: TrackMetadata) -> None:
        identity = self._artwork_identity(value)
        if identity is None or value.artwork_bytes is None:
            return
        count, size = self._artwork_references.get(
            identity,
            (0, len(value.artwork_bytes)),
        )
        if count == 0:
            self._artwork_bytes += size
        self._artwork_references[identity] = (count + 1, size)

    def _release_artwork(self, value: TrackMetadata) -> None:
        identity = self._artwork_identity(value)
        if identity is None:
            return
        reference = self._artwork_references.get(identity)
        if reference is None:
            return
        count, size = reference
        if count <= 1:
            self._artwork_references.pop(identity, None)
            self._artwork_bytes -= size
        else:
            self._artwork_references[identity] = (count - 1, size)

    @staticmethod
    def key(path: Path, size: int | None = None, mtime_ns: int | None = None) -> tuple[str, int, int]:
        if size is None or mtime_ns is None:
            stat = path.stat()
            size, mtime_ns = stat.st_size, stat.st_mtime_ns
        return str(path).casefold(), int(size), int(mtime_ns)

    def get(self, path: Path, size: int | None = None, mtime_ns: int | None = None) -> TrackMetadata | None:
        try:
            key = self.key(path, size, mtime_ns)
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value
        except OSError:
            return None

    def put(
        self,
        path: Path,
        value: TrackMetadata,
        size: int | None = None,
        mtime_ns: int | None = None,
    ) -> None:
        try:
            key = self.key(path, size, mtime_ns)
            artwork_size = len(value.artwork_bytes) if value.artwork_bytes else 0
            if artwork_size > self.max_artwork_bytes:
                return
            previous = self._values.pop(key, None)
            if previous is not None:
                self._release_artwork(previous)
            self._values[key] = value
            self._retain_artwork(value)
            while (
                len(self._values) > self.max_entries
                or self._artwork_bytes > self.max_artwork_bytes
            ):
                _, removed = self._values.popitem(last=False)
                self._release_artwork(removed)
        except OSError:
            pass

    def rekey(
        self,
        old_path: Path,
        new_path: Path,
        size: int,
        mtime_ns: int,
    ) -> bool:


        old_key = self.key(old_path, size, mtime_ns)
        value = self._values.pop(old_key, None)
        if value is None:
            return False
        new_key = self.key(new_path, size, mtime_ns)
        replaced = self._values.pop(new_key, None)
        if replaced is not None:
            self._release_artwork(replaced)
        self._values[new_key] = value
        self._values.move_to_end(new_key)
        return True
