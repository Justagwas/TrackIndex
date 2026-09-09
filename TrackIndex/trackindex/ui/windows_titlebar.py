from __future__ import annotations

import ctypes
import os


def apply_windows_titlebar_theme(window, dark: bool) -> None:
    if os.name != "nt":
        return
    try:
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            if result == 0:
                break
    except Exception:
        return
