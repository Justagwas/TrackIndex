from __future__ import annotations

import logging
import re
import sys
import threading
from contextlib import suppress
from pathlib import Path

from .filesystem import is_link_like
from .paths import product_data_dir

_LOCK = threading.Lock()
_PRIVATE_PATHS: set[str] = set()
_CONFIGURED = False


class _PrivatePathFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_diagnostic_text(super().format(record))


def register_private_path(path: str | Path) -> None:
    try:
        value = str(Path(path).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        value = str(path)
    if value:
        with _LOCK:
            _PRIVATE_PATHS.add(value)


def redact_diagnostic_text(value: str) -> str:
    with _LOCK:
        private_paths = tuple(_PRIVATE_PATHS)
    rendered = value
    for private_path in sorted(private_paths, key=len, reverse=True):
        for form in {private_path, private_path.replace("\\", "/")}:
            if form:
                rendered = re.sub(
                    re.escape(form),
                    "<music-folder>",
                    rendered,
                    flags=re.IGNORECASE,
                )
    return rendered


def configure_diagnostics() -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("trackindex")
    with _LOCK:
        if _CONFIGURED:
            return logger
        if sys.stderr is not None:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                _PrivatePathFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    "%Y-%m-%dT%H:%M:%S",
                )
            )
        else:
            handler = logging.NullHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _CONFIGURED = True
    return logger


def cleanup_legacy_logs() -> None:
    storage = product_data_dir()
    folder = storage / "logs"
    if is_link_like(storage) or is_link_like(folder) or not folder.is_dir():
        return
    for name in ("trackindex.log", "trackindex.log.1", "trackindex.log.2", "trackindex.log.3"):
        path = folder / name
        if not is_link_like(path) and path.is_file():
            with suppress(OSError):
                path.unlink(missing_ok=True)
    with suppress(OSError):
        folder.rmdir()


def diagnostics_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trackindex.{name}")
