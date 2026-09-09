from __future__ import annotations

import os
from pathlib import Path

from .filesystem import is_link_like


def recycle_files(paths: tuple[Path, ...]) -> tuple[str, ...]:


    candidates = tuple(
        path.resolve(strict=False)
        for path in paths
        if path.is_file() and not is_link_like(path)
    )
    if not candidates:
        return ()
    if os.name != "nt":
        return ("Recycle Bin integration is unavailable on this platform.",)

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = (
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        )

    source = "\0".join(str(path) for path in candidates) + "\0\0"
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3
    operation.pFrom = source
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        return (f"Windows could not move recovery data to the Recycle Bin (code {result}).",)
    remaining = tuple(path for path in candidates if path.exists())
    return tuple(f"Recovery file remains: {path.name}" for path in remaining)
