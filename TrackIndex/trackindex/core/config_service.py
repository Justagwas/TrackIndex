from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_json
from .config import (
    CONFIG_SCHEMA_VERSION,
    SUPPORTED_PLAYLIST_EXTENSIONS,
    UI_SCALE_PERCENT_MAX,
    UI_SCALE_PERCENT_MIN,
)
from .models import LibraryRecord, StorageMode
from .paths import config_path
from .windows_names import is_valid_windows_name

_SAFE_LIBRARY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MAX_LIBRARIES = 2048


@dataclass(slots=True)
class AppConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    theme_mode: str = "dark"
    ui_scale_percent: int = 100
    window_pinned: bool = False
    storage_mode: str = StorageMode.BOTH.value
    auto_check_updates: bool = True
    music_root: str = ""
    libraries: list[LibraryRecord] = field(default_factory=list)
    last_library_id: str = ""
    initial_discovery_completed: bool = False
    confirm_track_deletion: bool = True
    playback_volume: int = 75
    playback_muted: bool = False
    playback_shuffle: bool = False
    playback_repeat: str = "off"


def default_config() -> AppConfig:
    return AppConfig(music_root=str(default_music_root()))


def default_music_root() -> Path:
    try:
        from PySide6.QtCore import QStandardPaths

        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
        if location:
            return Path(location).resolve()
    except Exception:
        pass
    candidate = Path.home() / "Music"
    return candidate.resolve()


def _parse_libraries(value: object) -> list[LibraryRecord]:
    if not isinstance(value, list):
        return []
    records: list[LibraryRecord] = []
    seen_folders: set[str] = set()
    seen_ids: set[str] = set()
    for item in value:
        if len(records) >= _MAX_LIBRARIES:
            break
        if not isinstance(item, dict):
            continue
        raw_folder = str(item.get("folder", "")).strip()
        if not raw_folder:
            continue
        try:
            folder = Path(raw_folder).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        folded = str(folder).casefold()
        if folded in seen_folders:
            continue
        seen_folders.add(folded)
        library_id = str(item.get("library_id", "")).strip()
        if not _SAFE_LIBRARY_ID.fullmatch(library_id) or library_id.casefold() in seen_ids:
            library_id = uuid.uuid4().hex
        seen_ids.add(library_id.casefold())
        raw_mode = str(item.get("storage_mode", StorageMode.BOTH.value))
        mode = raw_mode if raw_mode in {item.value for item in StorageMode} else StorageMode.BOTH.value
        canonical = str(item.get("canonical_playlist", "")).strip()
        if not (
            canonical
            and Path(canonical).suffix.casefold()
            in SUPPORTED_PLAYLIST_EXTENSIONS
            and is_valid_windows_name(canonical)
        ):
            canonical = ""
        records.append(
            LibraryRecord(
                library_id,
                folder,
                canonical,
                mode,
                _coerce_bool(item.get("setup_completed"), False),
            )
        )
    return records


def _coerce_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value) if isinstance(value, (str, bytes, bytearray, int, float)) else default
    except (TypeError, ValueError):
        return default


def _coerce_repeat(value: object) -> str:
    candidate = str(value or "off").casefold()
    return candidate if candidate in {"off", "all", "one"} else "off"


def _parse_music_root(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return str(default_music_root())
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return str(default_music_root())
        return str(candidate.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(default_music_root())


def load_config(path: Path | None = None) -> AppConfig:
    target = path or config_path()
    try:
        with target.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("configuration file is too large")
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("configuration root is not an object")
    except (OSError, ValueError, RecursionError):
        return default_config()


    mode = StorageMode.BOTH.value
    theme = "light" if str(payload.get("theme_mode", "dark")).lower() == "light" else "dark"
    try:
        scale = int(payload.get("ui_scale_percent", 100))
    except (TypeError, ValueError):
        scale = 100
    libraries = _parse_libraries(payload.get("libraries"))
    known_library_ids = {item.library_id for item in libraries}
    last_library_id = str(payload.get("last_library_id", ""))
    if last_library_id not in known_library_ids:
        last_library_id = ""
    return AppConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        theme_mode=theme,
        ui_scale_percent=max(UI_SCALE_PERCENT_MIN, min(UI_SCALE_PERCENT_MAX, scale)),
        window_pinned=_coerce_bool(payload.get("window_pinned"), False),
        storage_mode=mode,
        auto_check_updates=_coerce_bool(payload.get("auto_check_updates"), True),
        music_root=_parse_music_root(payload.get("music_root")),
        libraries=libraries,
        last_library_id=last_library_id,
        initial_discovery_completed=_coerce_bool(payload.get("initial_discovery_completed"), False),
        confirm_track_deletion=_coerce_bool(payload.get("confirm_track_deletion"), True),
        playback_volume=max(0, min(100, _coerce_int(payload.get("playback_volume"), 75))),
        playback_muted=_coerce_bool(payload.get("playback_muted"), False),
        playback_shuffle=_coerce_bool(payload.get("playback_shuffle"), False),
        playback_repeat=_coerce_repeat(payload.get("playback_repeat")),
    )


def save_config(config: AppConfig, path: Path | None = None) -> None:
    target = path or config_path()
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "theme_mode": config.theme_mode,
        "ui_scale_percent": config.ui_scale_percent,
        "window_pinned": config.window_pinned,
        "storage_mode": StorageMode.BOTH.value,
        "auto_check_updates": config.auto_check_updates,
        "music_root": config.music_root,
        "libraries": [
            {
                "library_id": item.library_id,
                "folder": str(item.folder),
                "canonical_playlist": item.canonical_playlist,
                "storage_mode": item.storage_mode,
                "setup_completed": item.setup_completed,
            }
            for item in config.libraries
        ],
        "last_library_id": config.last_library_id,
        "initial_discovery_completed": config.initial_discovery_completed,
        "confirm_track_deletion": config.confirm_track_deletion,
        "playback_volume": max(0, min(100, int(config.playback_volume))),
        "playback_muted": bool(config.playback_muted),
        "playback_shuffle": bool(config.playback_shuffle),
        "playback_repeat": _coerce_repeat(config.playback_repeat),
    }
    atomic_write_json(target, payload)
