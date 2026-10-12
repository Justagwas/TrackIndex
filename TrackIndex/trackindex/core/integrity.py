from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .filesystem import is_link_like


@dataclass(frozen=True, slots=True)
class ContentEvidence:
    size: int
    sha256: str


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def inspect_content(path: Path) -> ContentEvidence:
    if is_link_like(path):
        raise OSError(f"Content verification refuses a link: {path.name}")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"Content verification requires a regular file: {path.name}")
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _identity(opened)[:4] != _identity(before)[:4]:
            raise OSError(f"File changed before verification: {path.name}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            count += len(chunk)
            digest.update(chunk)
        after_read = os.fstat(handle.fileno())
    after = path.lstat()
    if (is_link_like(path) or count != before.st_size
            or _identity(opened) != _identity(after_read)
            or _identity(before) != _identity(after)):
        raise OSError(f"File changed during verification: {path.name}")
    return ContentEvidence(count, digest.hexdigest())


def verify_content(path: Path, size: int | None, sha256: str) -> None:
    if (size is not None and (type(size) is not int or size < 0)
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None):
        raise OSError(f"Original content evidence is unavailable: {path.name}")
    evidence = inspect_content(path)
    if ((size is not None and evidence.size != size)
            or evidence.sha256.casefold() != sha256.casefold()):
        raise OSError(f"Content differs from the recorded original: {path.name}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
