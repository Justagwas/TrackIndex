from __future__ import annotations

from weakref import WeakKeyDictionary

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QScrollBar,
    QSlider,
    QStyle,
    QStyleFactory,
    QWidget,
)

_SCROLLBAR_STYLES: WeakKeyDictionary[QScrollBar, QStyle] = WeakKeyDictionary()


def is_pointer_control(widget: object) -> bool:
    if isinstance(widget, QWidget) and widget.property("trackindexRowPointerOnly"):
        return False
    return isinstance(widget, (QAbstractButton, QSlider, QComboBox))


def sync_pointer_cursor(widget: QWidget) -> None:
    if widget.isEnabled():
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
    else:
        widget.unsetCursor()


def apply_media_crate_scrollbar(scrollbar: QScrollBar) -> None:

    scrollbar.setObjectName("mediaCrateScrollBar")
    native_style = _SCROLLBAR_STYLES.get(scrollbar)
    if native_style is None:
        available = {
            name.casefold(): name for name in tuple(QStyleFactory.keys())
        }
        style_name = available.get("windows11") or available.get("windowsvista")
        native_style = QStyleFactory.create(style_name) if style_name else None
        if native_style is None:
            app = QApplication.instance()
            native_style = app.style() if isinstance(app, QApplication) else None
        elif native_style.parent() is None:
            native_style.setParent(scrollbar)
        if native_style is not None:
            _SCROLLBAR_STYLES[scrollbar] = native_style
    if native_style is not None:
        scrollbar.setStyle(native_style)
    scrollbar.setProperty("trackindexNativeScrollStyle", native_style is not None)
    scrollbar.setCursor(Qt.CursorShape.ArrowCursor)
