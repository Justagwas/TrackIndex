from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import Condition, Event, Thread
from time import monotonic, time

from PySide6.QtCore import QObject, Signal

from .diagnostics import diagnostics_logger
from .paths import product_data_dir
from .playback_backend import LibMpvBackend

_LOG = diagnostics_logger("loudness")
_MAX_QUEUE = 4096
_SCHEMA = 2
_ANALYSIS_FILTER = ("@trackindex-loudness:lavfi=[ebur128=metadata=1:framelog=quiet,aresample=192000,"
                    "astats=metadata=1:reset=0:measure_perchannel=none:measure_overall=Peak_level]")


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


@dataclass(frozen=True, slots=True)
class FileIdentity:
    size: int
    mtime_ns: int
    device: int
    inode: int
    birthtime_ns: int = 0

    @classmethod
    def from_path(cls, path: str | Path) -> FileIdentity:
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("The audio source is not a regular local file.")
        return cls(info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino,
                   getattr(info, "st_birthtime_ns", 0))

    def cache_key(self, path: str | Path) -> str:
        identity = f"{self.device}:{self.inode}:{self.birthtime_ns}" if self.inode else path_key(path)
        return f"{_SCHEMA}:{identity}:{self.size}:{self.mtime_ns}"

    def matches_result(self, value: dict) -> bool:
        return all(value.get(name) == item for name, item in asdict(self).items())


@dataclass(frozen=True, slots=True)
class LoudnessResult:
    integrated_lufs: float | None
    true_peak_dbfs: float
    identity: FileIdentity

    def to_dict(self) -> dict:
        return {"integrated_lufs": self.integrated_lufs, "true_peak_dbfs": self.true_peak_dbfs,
                **asdict(self.identity)}


class AnalysisCancelled(Exception):
    pass


class NativeLoudnessAnalyzer:
    def __init__(self, *, reuse: bool = False):
        self._reuse = reuse
        self._deck: LibMpvBackend | None = None
        self._generation = 0
        self._loaded = Event()
        self._failed = Event()

    def _event(self, value) -> None:
        if value.generation != self._generation:
            return
        if value.kind == "loaded":
            self._loaded.set()
        elif value.kind in ("error", "unavailable"):
            self._failed.set()

    def _ensure_player(self) -> LibMpvBackend:
        if self._deck is None:
            deck = LibMpvBackend()
            deck.initialize(self._event)
            if not deck.available:
                deck.shutdown()
                raise RuntimeError(deck.error or "The audio analysis runtime is unavailable.")
            self._deck = deck
            try:
                player = deck._require_player()
                player.ao = "null"
                player.ao_null_untimed = True
                player.speed = 1
                player.audio_pitch_correction = False
                player.ad_lavc_threads = 1
                player.demuxer_max_bytes = 16 * 1024 * 1024
                player.demuxer_readahead_secs = 1
                player.replaygain = "no"
            except Exception:
                self.close()
                raise
        return self._deck

    def __call__(self, path: Path, cancel: Event) -> tuple[float | None, float]:
        if cancel.is_set():
            raise AnalysisCancelled
        metadata = {}
        deck = self._ensure_player()
        self._generation += 1
        self._loaded.clear()
        self._failed.clear()
        try:
            player = deck._require_player()
            deck.stop()
            deck.wait_until_idle()
            player.af = ""
            player.af = _ANALYSIS_FILTER
            deck.load(path, paused=True, generation=self._generation)
            player.pause = False
            started = monotonic()
            while True:
                if cancel.is_set():
                    raise AnalysisCancelled
                if self._failed.is_set():
                    raise RuntimeError("The audio could not be decoded for loudness analysis.")
                if self._loaded.is_set() and player.eof_reached:
                    metadata.update(player._get_property("af-metadata/trackindex-loudness") or {})
                    break
                duration = player.duration or 0
                limit = max(60.0, min(1800.0, float(duration) + 30))
                if monotonic() - started > limit:
                    raise RuntimeError("Audio analysis took too long.")
                self._failed.wait(.01)
        finally:
            try:
                deck.stop()
                deck.wait_until_idle()
            except Exception:
                self.close()
            if not self._reuse:
                self.close()
        if "lavfi.astats.Overall.Peak_level" not in metadata:
            raise RuntimeError("The audio runtime did not return a complete loudness measurement.")
        loudness = float(metadata.get("lavfi.r128.I", "-70"))
        maximum = float(metadata["lavfi.astats.Overall.Peak_level"])
        if maximum == -math.inf:
            maximum = -150.0
        elif not math.isfinite(maximum):
            raise RuntimeError("The audio runtime returned an invalid true-peak measurement.")
        else:
            maximum = max(-150.0, maximum + .000001)
        if not math.isfinite(loudness) or loudness <= -70:
            loudness = None
        if maximum > 50 or maximum < -150 or (loudness is not None and not -70 < loudness < 20):
            raise RuntimeError("The audio runtime returned invalid loudness measurements.")
        return loudness, maximum

    def close(self) -> None:
        deck, self._deck = self._deck, None
        if deck is not None:
            deck.shutdown()


def _valid_measurement(loudness: object, peak: object) -> bool:
    return ((loudness is None or (isinstance(loudness, (int, float))
                                 and math.isfinite(loudness) and -70 < loudness < 20))
            and isinstance(peak, (int, float)) and math.isfinite(peak) and -150 <= peak <= 50)


def read_cached_result(path: str | Path, cache_path: Path | None = None) -> dict | None:
    connection = None
    try:
        identity = FileIdentity.from_path(path)
        database = cache_path or product_data_dir() / "cache" / "loudness.sqlite3"
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=.01)
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT integrated, peak FROM loudness WHERE identity=?",
                                 (identity.cache_key(path),)).fetchone()
        if (row is None or not _valid_measurement(*row)
                or FileIdentity.from_path(path) != identity):
            return None
        return LoudnessResult(*row, identity).to_dict()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


class _ResultCache:
    def __init__(self, path: Path):
        self._connection = None
        self._touches = {}
        self._memory: OrderedDict[str, tuple[float | None, float]] = OrderedDict()
        connection = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=1)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint=256")
            connection.execute("CREATE TABLE IF NOT EXISTS loudness "
                               "(identity TEXT PRIMARY KEY, integrated REAL, peak REAL NOT NULL, used REAL NOT NULL)")
            connection.commit()
            self._connection = connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            _LOG.exception("Loudness disk cache is unavailable; using this session's results")

    def get(self, key: str) -> tuple[float | None, float] | None:
        if key in self._memory:
            self._memory.move_to_end(key)
            self._touches[key] = time()
            if len(self._touches) >= 128:
                self._flush_touches()
            return self._memory[key]
        if self._connection is None:
            return None
        try:
            row = self._connection.execute("SELECT integrated, peak FROM loudness WHERE identity=?", (key,)).fetchone()
            if row is None:
                return None
            loudness, peak = row
            if not _valid_measurement(loudness, peak):
                return None
            self._touches[key] = time()
            if len(self._touches) >= 128:
                self._flush_touches()
            self._remember(key, (loudness, peak))
            return loudness, peak
        except sqlite3.Error:
            _LOG.exception("Could not read the loudness cache")
            return None

    def put(self, key: str, value: tuple[float | None, float]) -> None:
        self._remember(key, value)
        if self._connection is None:
            return
        try:
            with self._connection:
                self._connection.execute("INSERT OR REPLACE INTO loudness VALUES (?, ?, ?, ?)", (key, *value, time()))
        except sqlite3.Error:
            _LOG.exception("Could not save the loudness measurement")

    def close(self) -> None:
        if self._connection is not None:
            self._flush_touches()
            self._connection.close()

    def _flush_touches(self) -> None:
        if self._connection is None:
            self._touches.clear()
            return
        if not self._touches:
            return
        try:
            with self._connection:
                self._connection.executemany("UPDATE loudness SET used=? WHERE identity=?",
                                             ((used, key) for key, used in self._touches.items()))
            self._touches.clear()
        except sqlite3.Error:
            self._touches.clear()
            _LOG.exception("Could not update loudness cache ages")

    def _remember(self, key: str, value: tuple[float | None, float]) -> None:
        self._memory[key] = value
        self._memory.move_to_end(key)
        while len(self._memory) > _MAX_QUEUE:
            self._memory.popitem(last=False)


def _lower_thread_priority() -> None:
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentThread.restype = ctypes.c_void_p
            kernel32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)
        except (AttributeError, OSError):
            pass


def read_loudness_source(path: Path, policy: str) -> str:
    try:
        from .metadata_service import prepare_metadata_backend
        loader, options, _error = prepare_metadata_backend(path.suffix)
        audio = loader(path, options=options, easy=False) if loader is not None else None
        tags = getattr(audio, "tags", None)
        values = {}
        if tags is not None:
            for name, value in tags.items():
                key = str(name).casefold().rsplit(":", 1)[-1]
                if key not in {"replaygain_track_gain", "replaygain_album_gain", "r128_track_gain", "r128_album_gain"}:
                    continue
                value = getattr(value, "text", getattr(value, "value", value))
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else ""
                if isinstance(value, bytes):
                    value = value.decode("utf-8")
                if isinstance(value, bool):
                    continue
                if key.startswith("r128_"):
                    text = str(value)
                    if (len(text) <= 6 and re.fullmatch(r"[+-]?[0-9]+", text) is not None
                            and -32768 <= int(text) <= 32767):
                        values[key] = True
                    continue
                match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:dB)?\s*", str(value), re.IGNORECASE)
                if match is not None and math.isfinite(float(match[1])):
                    values[key] = True
        if policy == "album" and any(values.get(key) for key in ("replaygain_album_gain", "r128_album_gain")):
            return "album-tags"
        if any(values.get(key) for key in ("replaygain_track_gain", "r128_track_gain")):
            return "track-fallback" if policy == "album" else "track-tags"
    except Exception:
        pass
    return ""


class LoudnessAnalysisService(QObject):
    resultReady = Signal(str, object)
    statusChanged = Signal(object)

    def __init__(self, filesystem_access: Callable = nullcontext, *, cache_path: Path | None = None,
                 analyzer: Callable | None = None, tag_reader: Callable | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._filesystem_access = filesystem_access
        self._cache_path = cache_path or product_data_dir() / "cache" / "loudness.sqlite3"
        self._analyzer = analyzer or NativeLoudnessAnalyzer(reuse=True)
        self._tag_reader = tag_reader or read_loudness_source
        self._condition = Condition()
        self._selected: OrderedDict[str, Path] = OrderedDict()
        self._paths: OrderedDict[str, Path] = OrderedDict()
        self._results: dict[str, LoudnessResult] = {}
        self._tagged: dict[str, str] = {}
        self._errors: dict[str, tuple[float, str]] = {}
        self._failures: OrderedDict[str, float] = OrderedDict()
        self._cancel = Event()
        self._closed = False
        self._paused = False
        self._request_id = 0
        self._current = ""
        self._policy = "track"
        self._thread = Thread(target=self._run, name="TrackIndex loudness analysis", daemon=True)
        self._thread.start()

    def set_paths(self, paths: Iterable[str | Path], *, priority: Iterable[str | Path] = (),
                  policy: str = "track") -> int:
        selected: OrderedDict[str, Path] = OrderedDict()
        for group in (priority, paths):
            for path in group:
                key = path_key(path)
                if key not in selected:
                    selected[key] = Path(key)
                if len(selected) >= _MAX_QUEUE:
                    break
            if len(selected) >= _MAX_QUEUE:
                break
        with self._condition:
            if self._closed:
                return self._request_id
            self._request_id += 1
            if policy != self._policy:
                self._tagged.clear()
                self._errors.clear()
                self._cancel.set()
            self._policy = policy
            if self._current not in selected:
                self._cancel.set()
            self._selected = selected
            self._results = {key: value for key, value in self._results.items() if key in selected}
            self._tagged = {key: value for key, value in self._tagged.items() if key in selected}
            self._errors = {key: value for key, value in self._errors.items()
                            if key in selected and monotonic() < value[0]}
            self._paths = OrderedDict((key, path) for key, path in selected.items()
                                      if key not in self._results and key not in self._tagged
                                      and key not in self._errors
                                      and (key != self._current or self._cancel.is_set()))
            serial = self._request_id
            errors = tuple(self._errors.items())
            self._condition.notify_all()
        for key, (_until, error) in errors:
            self._status("failed", error=error, path=key, request_id=serial)
        self._status("queued" if self._paths or self._current else "complete", request_id=serial)
        return serial

    def invalidate(self, paths: Iterable[str | Path] | None = None) -> None:
        with self._condition:
            invalid = set(self._selected) if paths is None else {path_key(path) for path in paths}
            for key in invalid:
                self._results.pop(key, None)
                self._tagged.pop(key, None)
                self._errors.pop(key, None)
            if self._current in invalid:
                self._cancel.set()
            self._paths = OrderedDict((key, path) for key, path in self._selected.items()
                                      if key in invalid or key in self._paths)
            self._condition.notify_all()

    def set_paused(self, paused: bool) -> None:
        with self._condition:
            if self._closed:
                return
            if self._paused == bool(paused):
                return
            self._paused = bool(paused)
            if paused:
                self._cancel.set()
                if self._current in self._selected:
                    self._paths[self._current] = Path(self._current)
                    self._paths.move_to_end(self._current, last=False)
            self._condition.notify_all()
        self._status("paused" if paused else "queued")

    def snapshot(self) -> dict[str, dict]:
        with self._condition:
            return {key: value.to_dict() for key, value in self._results.items()}

    def tagged_snapshot(self) -> dict[str, str]:
        with self._condition:
            return dict(self._tagged)

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            self._selected.clear()
            self._paths.clear()
            self._cancel.set()
            self._condition.notify_all()

    def wait_for_done(self, milliseconds: int = -1) -> bool:
        self._thread.join(None if milliseconds < 0 else milliseconds / 1000)
        return not self._thread.is_alive()

    def _status(self, state: str, *, error: str = "", path: str | None = None,
                request_id: int | None = None) -> None:
        with self._condition:
            value = {"state": state, "path": self._current if path is None else path,
                     "completed": len(self._results) + len(self._tagged) + len(self._errors),
                     "total": len(self._selected), "queued": len(self._paths), "error": error,
                     "request_id": self._request_id if request_id is None else request_id}
        self.statusChanged.emit(value)

    def _run(self) -> None:
        _lower_thread_priority()
        cache = _ResultCache(self._cache_path)
        try:
            while True:
                with self._condition:
                    while not self._closed and (self._paused or not self._paths):
                        self._condition.wait()
                    if self._closed:
                        return
                    key, path = self._paths.popitem(last=False)
                    self._current = key
                    request_id = self._request_id
                    policy = self._policy
                    cancel = self._cancel = Event()
                self._status("analyzing", request_id=request_id)
                result = None
                error = ""
                cache_key = ""
                tagged = ""
                work_seconds = 0.0
                try:
                    with self._filesystem_access():
                        if cancel.is_set():
                            raise AnalysisCancelled
                        identity = FileIdentity.from_path(path)
                        cache_key = identity.cache_key(path)
                        failed_at = self._failures.get(cache_key, 0)
                        if failed_at and monotonic() - failed_at < 300:
                            raise RuntimeError("Loudness analysis is unavailable for this file.")
                        measured = cache.get(cache_key)
                        if measured is None:
                            tagged = self._tag_reader(path, policy)
                            if tagged:
                                fresh = False
                            else:
                                work_started = monotonic()
                                measured = self._analyzer(path, cancel)
                                work_seconds = monotonic() - work_started
                                fresh = True
                        else:
                            fresh = False
                        if cancel.is_set():
                            raise AnalysisCancelled
                        if FileIdentity.from_path(path) != identity:
                            raise AnalysisCancelled
                        if fresh:
                            cache.put(cache_key, measured)
                        if measured is not None:
                            result = LoudnessResult(*measured, identity)
                except AnalysisCancelled:
                    with self._condition:
                        self._current = ""
                    continue
                except Exception as exc:
                    error = str(exc) or "Loudness analysis is unavailable for this file."
                    if cache_key:
                        self._failures[cache_key] = monotonic()
                        while len(self._failures) > _MAX_QUEUE:
                            self._failures.popitem(last=False)
                with self._condition:
                    if self._closed or cancel.is_set() or key not in self._selected:
                        self._current = ""
                        continue
                    if result is not None:
                        self._results[key] = result
                    elif tagged:
                        self._tagged[key] = tagged
                    elif error:
                        self._errors[key] = (monotonic() + 300, error)
                    self._current = ""
                    pending = bool(self._paths)
                    request_id = self._request_id
                if result is not None:
                    self.resultReady.emit(key, result)
                self._status("failed" if error else "tagged" if tagged else "queued" if pending else "complete",
                             error=error, path=key if error or tagged else "", request_id=request_id)
                if (error or tagged) and not pending:
                    self._status("complete", request_id=request_id)
                if work_seconds and pending:
                    with self._condition:
                        if not self._closed and not self._paused:
                            self._condition.wait(min(.25, work_seconds * .25))
        finally:
            try:
                close = getattr(self._analyzer, "close", None)
                if callable(close):
                    close()
            finally:
                cache.close()
