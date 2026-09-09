from __future__ import annotations

import json
import os
import uuid
from contextlib import suppress
from pathlib import Path


def write_bytes_durable(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    mode = "xb" if exclusive else "wb"
    with path.open(mode) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_bytes(path: Path, data: bytes) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        write_bytes_durable(temporary, data, exclusive=True)
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def atomic_create_bytes(path: Path, data: bytes) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        write_bytes_durable(temporary, data, exclusive=True)


        os.link(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
