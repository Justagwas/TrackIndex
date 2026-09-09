from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link_like(path: Path) -> bool:

    try:
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            return True
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def regular_file_stat(entry: Any) -> Any | None:

    try:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            return None
        result = entry.stat(follow_symlinks=False)
        return None if getattr(result, "st_file_attributes", 0) & _REPARSE_POINT else result
    except OSError:
        return None


def safe_directory_entry(entry: Any) -> bool:

    try:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            return False
        result = entry.stat(follow_symlinks=False)
        return not bool(getattr(result, "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False
