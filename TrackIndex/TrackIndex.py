
from __future__ import annotations

import ctypes
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from trackindex.core.config import (
    APP_NAME,
    APP_VERSION,
    MUTEX_NAME,
)


def _enable_high_dpi_rendering() -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError, ValueError):
        with suppress(AttributeError, OSError, ValueError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)


class SingleInstanceGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None
        self._kernel32: Any | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            return False
        self._kernel32 = kernel32
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def main() -> int:
    _enable_high_dpi_rendering()

    from trackindex.core.metadata_service import prepare_all_metadata_backends

    prepare_all_metadata_backends()

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QMessageBox

    from trackindex.controller.app_controller import AppController
    from trackindex.controller.playback_controller import PlaybackController
    from trackindex.core.config_service import load_config
    from trackindex.core.diagnostics import configure_diagnostics, register_private_path
    from trackindex.ui.main_window import MainWindow
    from trackindex.ui.theme import get_theme

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QCoreApplication.setOrganizationName("Justagwas")
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    guard = SingleInstanceGuard(MUTEX_NAME)
    if not guard.acquire():
        QMessageBox.information(None, APP_NAME, "TrackIndex is already running.")
        return 0

    config = load_config()
    configure_diagnostics()
    register_private_path(config.music_root)
    for library in config.libraries:
        register_private_path(library.folder)
    project_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon_path = project_root / "icon.ico"
    window = MainWindow(
        get_theme(config.theme_mode),
        theme_mode=config.theme_mode,
        ui_scale_percent=config.ui_scale_percent,
        auto_updates=config.auto_check_updates,
        confirm_track_deletion=config.confirm_track_deletion,
        window_pinned=config.window_pinned,
        music_root=config.music_root,
        icon_path=icon_path,
    )
    controller = AppController(app, window, config)
    with suppress(RuntimeError, TypeError):
        app.aboutToQuit.disconnect(controller.shutdown)
    playback = PlaybackController(app, window, controller, config)
    app.aboutToQuit.connect(playback.shutdown)
    app.aboutToQuit.connect(controller.shutdown)
    window.show()
    try:
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
