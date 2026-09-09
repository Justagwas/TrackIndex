from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from .config import APP_SHORT_NAME


@lru_cache(maxsize=1)
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def product_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_SHORT_NAME
    return Path.home() / ".trackindex"


def config_path() -> Path:
    from .config import CONFIG_FILENAME

    return product_data_dir() / CONFIG_FILENAME


def recovery_dir() -> Path:
    return product_data_dir() / "recovery"


def updates_dir() -> Path:
    return product_data_dir() / "updates"


def sessions_dir() -> Path:
    return product_data_dir() / "sessions"


def is_direct_child(path: Path, folder: Path, *, suffix: str | None = None) -> bool:

    try:
        resolved_path = path.resolve(strict=False)
        resolved_folder = folder.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved_path.parent != resolved_folder:
        return False
    return suffix is None or resolved_path.suffix.casefold() == suffix.casefold()
