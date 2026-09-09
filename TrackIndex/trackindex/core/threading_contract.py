from __future__ import annotations

from PySide6.QtCore import QObject


def assert_qt_thread_affinity(owner: QObject) -> None:
    expected = owner.thread()
    if expected is None or not expected.isCurrentThread():
        raise RuntimeError(
            f"{owner.__class__.__name__} executed outside its owning Qt thread."
        )
