from __future__ import annotations

from .update_dialog import ask_update_install_preference, ask_update_handoff

from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .interaction import RestoreCursorOnHide, apply_media_crate_scrollbar
from .widgets.custom_controls import SquareCheckBoxStyle
from .windows_titlebar import apply_windows_titlebar_theme


class _LibraryImportList(QListWidget):


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("trackindexRowPointerOnly", True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def _sync_item_cursor(self, position) -> None:
        cursor = (
            Qt.CursorShape.PointingHandCursor
            if self.itemAt(position) is not None
            else Qt.CursorShape.ArrowCursor
        )
        self.viewport().setCursor(cursor)

    def mouseMoveEvent(self, event) -> None:
        self._sync_item_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and item is not None:
            item.setCheckState(
                Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
            )
            self.clearSelection()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            self._sync_item_cursor(event.position().toPoint())
        elif event.type() == QEvent.Type.Leave:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        return super().viewportEvent(event)


class _CursorRestoringMessageBox(RestoreCursorOnHide, QMessageBox):
    pass


def styled_message(parent: QWidget, icon: QMessageBox.Icon, title: str, text: str, buttons=QMessageBox.StandardButton.Ok) -> QMessageBox:
    box = _CursorRestoringMessageBox(icon, title, text, buttons, parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    return box


def _apply_checkbox_style(widget: QWidget, parent: QWidget | None) -> None:
    theme = getattr(parent, "theme", None)
    if theme is None:
        return
    scale = max(0.75, min(2.0, float(getattr(parent, "ui_scale_percent", 100)) / 100.0))
    style = SquareCheckBoxStyle(
        border_color=theme.border,
        fill_color=theme.accent,
        check_color=theme.text_primary,
        size=max(14, round(20 * scale)),
        radius=max(2, round(4 * scale)),
        parent=widget,
    )
    style.setParent(widget)
    widget.setStyle(style)


class DeleteTracksDialog(RestoreCursorOnHide, QDialog):


    def __init__(self, count: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remove tracks")
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        title = QLabel(
            f"Remove {count} selected track{'s' if count != 1 else ''}?",
            self,
        )
        title.setObjectName("title")
        layout.addWidget(title)
        detail = QLabel(
            "The audio files will be removed from this playlist folder. "
            "TrackIndex keeps recoverable session data so Ctrl+Z can restore "
            "them until the application closes normally.",
            self,
        )
        detail.setWordWrap(True)
        detail.setObjectName("settingsSubtext")
        layout.addWidget(detail)
        self.dont_ask_again = QCheckBox("Don't show this confirmation again", self)
        _apply_checkbox_style(self.dont_ask_again, parent)
        layout.addWidget(self.dont_ask_again)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No, self)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("Remove")
        buttons.button(QDialogButtonBox.StandardButton.No).setText("Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)


class LibraryImportDialog(RestoreCursorOnHide, QDialog):
    def __init__(self, folders: tuple[Path, ...], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import music libraries")
        self.resize(620, 440)
        layout = QVBoxLayout(self)
        title = QLabel("Libraries found", self)
        title.setObjectName("title")
        layout.addWidget(title)
        hint = QLabel("Choose the folders to remember in TrackIndex. No music files will be changed.", self)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = _LibraryImportList(self)
        _apply_checkbox_style(self.list, parent)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        apply_media_crate_scrollbar(self.list.verticalScrollBar())
        apply_media_crate_scrollbar(self.list.horizontalScrollBar())
        for folder in folders:
            item = QListWidgetItem(f"{folder.name}\n{folder}")
            item.setData(Qt.ItemDataRole.UserRole, str(folder))
            item.setCheckState(Qt.CheckState.Checked)
            item.setFlags(
                (item.flags() | Qt.ItemFlag.ItemIsEnabled)
                & ~Qt.ItemFlag.ItemIsSelectable
                & ~Qt.ItemFlag.ItemIsUserCheckable
            )
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_folders(self) -> tuple[Path, ...]:
        return tuple(
            Path(self.list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list.count())
            if self.list.item(row).checkState() == Qt.CheckState.Checked
        )
