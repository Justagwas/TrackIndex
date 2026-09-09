from __future__ import annotations

import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

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


def configure_diagnostics(root: Path | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("trackindex")
    with _LOCK:
        if _CONFIGURED:
            return logger
        destination = root or product_data_dir() / "logs"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                destination / "trackindex.log",
                maxBytes=1024 * 1024,
                backupCount=3,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(
                _PrivatePathFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    "%Y-%m-%dT%H:%M:%S",
                )
            )
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _CONFIGURED = True
    return logger


def diagnostics_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trackindex.{name}")
