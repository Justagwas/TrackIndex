from __future__ import annotations

import os
import re
from pathlib import Path

from .config import SESSION_RECOVERY_FOLDER, SUPPORTED_AUDIO_EXTENSIONS
from .filesystem import is_link_like
from .paths import is_direct_child
from .windows_names import is_valid_windows_name

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def recovery_directory(folder: Path, command_id: str) -> Path:
    if not _SAFE_ID.fullmatch(command_id):
        raise ValueError("Command identifier cannot be used for recovery storage.")
    return folder / SESSION_RECOVERY_FOLDER / command_id


def recovery_path(folder: Path, command_id: str, name: str) -> Path:
    if not is_valid_windows_name(name) or Path(name).suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
        raise ValueError("Recovery filename is unsafe.")
    return recovery_directory(folder, command_id) / name


def is_recovery_path(path: Path, folder: Path) -> bool:
    if ".." in path.parts:
        return False
    try:
        relative = path.absolute().relative_to((folder / SESSION_RECOVERY_FOLDER).absolute())
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        len(relative.parts) == 2
        and bool(_SAFE_ID.fullmatch(relative.parts[0]))
        and is_valid_windows_name(relative.parts[1])
        and Path(relative.parts[1]).suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS
    )


def validate_recovery_ancestors(path: Path, folder: Path) -> bool:
    if not is_recovery_path(path, folder):
        return False
    root = folder / SESSION_RECOVERY_FOLDER
    command = path.parent
    if any(is_link_like(candidate) for candidate in (folder, root, command, path)):
        return False
    try:
        if not folder.is_dir() or any(
            candidate.exists() and not candidate.is_dir() for candidate in (root, command)
        ):
            return False
        if path.exists() and not path.is_file():
            return False
        expected_parent = folder.resolve(strict=False) / SESSION_RECOVERY_FOLDER / command.name
        return path.resolve(strict=False).parent == expected_parent
    except (OSError, RuntimeError):
        return False


def ensure_recovery_parent(path: Path, folder: Path) -> None:
    if not validate_recovery_ancestors(path, folder):
        raise OSError("Recovery storage is unsafe.")
    root = folder / SESSION_RECOVERY_FOLDER
    root.mkdir(exist_ok=True)
    path.parent.mkdir(exist_ok=True)
    if not validate_recovery_ancestors(path, folder):
        raise OSError("Recovery storage became unsafe.")
    if os.name == "nt":
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(root))
        if attributes != 0xFFFFFFFF:
            ctypes.windll.kernel32.SetFileAttributesW(
                str(root), attributes | 0x2
            )


def is_managed_entry(path: Path) -> bool:
    return path.name.casefold() == SESSION_RECOVERY_FOLDER.casefold()


def direct_or_recovery(path: Path, folder: Path) -> bool:
    return is_direct_child(path, folder) or is_recovery_path(path, folder)
