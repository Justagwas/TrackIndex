from __future__ import annotations

import ctypes
import os
from typing import NoReturn


def emergency_exit() -> NoReturn:
    try:
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.argtypes = ()
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel.TerminateProcess.restype = ctypes.c_int
            kernel.TerminateProcess(kernel.GetCurrentProcess(), 2)
    finally:
        os._exit(2)
