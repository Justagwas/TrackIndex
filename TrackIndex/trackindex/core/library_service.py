from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

from .atomic_io import atomic_create_bytes
from .config import (
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_PLAYLIST_EXTENSIONS,
    default_playlist_name,
)
from .filesystem import is_link_like, regular_file_stat, safe_directory_entry
from .models import LibraryRecord
from .scanner import natural_key
from .windows_names import windows_name_error


def normalize_folder(path: Path) -> Path:


    return Path(os.path.abspath(path))


def valid_library_name(name: str) -> tuple[bool, str]:
    value = name.strip()
    if not value:
        return False, "Enter a playlist name."
    error = windows_name_error(value, max_length=120)
    if error:
        return False, error
    return True, ""


def canonical_playlist_for(folder: Path, remembered: str = "") -> tuple[Path | None, tuple[Path, ...]]:
    if not folder.is_dir() or is_link_like(folder):
        return None, ()
    found: list[Path] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if (
                    regular_file_stat(entry) is not None
                    and Path(entry.name).suffix.casefold()
                    in SUPPORTED_PLAYLIST_EXTENSIONS
                ):
                    found.append(folder / entry.name)
    except OSError:
        return None, ()
    playlists = tuple(sorted(found, key=lambda item: natural_key(item.name)))
    if remembered:
        match = next((item for item in playlists if item.name.casefold() == remembered.casefold()), None)
        if match is not None:
            return match, playlists
    preferred = folder / default_playlist_name(folder)
    matches = [item for item in playlists if item.name.casefold() == preferred.name.casefold()]
    if matches:
        return matches[0], playlists
    if len(playlists) == 1:
        return playlists[0], playlists
    if not playlists:
        return preferred, playlists
    return None, playlists


def discover_library_folders(
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[Path, ...]:
    if is_link_like(root):
        return ()
    try:
        root = root.resolve()
    except (OSError, RuntimeError):
        return ()
    if not root.is_dir() or is_link_like(root):
        return ()
    found: list[Path] = []
    stack = [root]
    while stack:
        if stop_requested and stop_requested():
            break
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        has_audio = False
        children: list[Path] = []
        for entry in entries:
            if stop_requested and stop_requested():
                break
            if regular_file_stat(entry) is not None:
                if Path(entry.name).suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS:
                    has_audio = True
            elif safe_directory_entry(entry):
                children.append(Path(entry.path))
        if has_audio:
            try:
                found.append(current.resolve())
            except (OSError, RuntimeError):
                continue
        stack.extend(sorted(children, key=lambda item: natural_key(item.name), reverse=True))
    unique = {str(item).casefold(): item for item in found}
    return tuple(sorted(unique.values(), key=lambda item: (natural_key(item.name), str(item).casefold())))


def create_library(root: Path, name: str) -> LibraryRecord:
    valid, message = valid_library_name(name)
    if not valid:
        raise ValueError(message)
    if is_link_like(root):
        raise ValueError("The configured music folder is unavailable.")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("The configured music folder is unavailable.")
    folder = root / name.strip()
    if any(child.name.casefold() == folder.name.casefold() for child in root.iterdir()):
        raise ValueError("A file or folder with that name already exists.")
    playlist = folder / f"{folder.name}.m3u8"
    created_folder = False
    try:
        folder.mkdir()
        created_folder = True
        atomic_create_bytes(playlist, b"#EXTM3U\r\n")
    except Exception:
        try:
            if created_folder:
                playlist.unlink(missing_ok=True)
            if created_folder and folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
        raise
    return LibraryRecord(uuid.uuid4().hex, folder.resolve(), playlist.name)
