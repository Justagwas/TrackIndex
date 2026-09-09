from __future__ import annotations

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

from .interaction import apply_media_crate_scrollbar
from .widgets.custom_controls import SquareCheckBoxStyle
from .windows_titlebar import apply_windows_titlebar_theme


def _prepare_update_dialog(dialog: QDialog, parent: QWidget | None) -> None:
    if parent is None:
        return
    icon = parent.windowIcon()
    if not icon.isNull():
        dialog.setWindowIcon(icon)
    theme = getattr(parent, "theme", None)
    scale = max(0.75, min(2.0, float(getattr(parent, "ui_scale_percent", 100)) / 100.0))
    styles: list[SquareCheckBoxStyle] = []
    if theme is not None:
        dialog.setStyleSheet(f"""
QDialog {{ background: {theme.panel_bg}; color: {theme.text_primary}; }}
QLabel {{ color: {theme.text_primary}; background: transparent; font: 600 {8 * scale:.1f}pt 'Segoe UI'; }}
QLabel#title {{ font: 700 {9 * scale:.1f}pt 'Segoe UI'; }}
QCheckBox {{ color: {theme.text_primary}; background: transparent; font: 600 {9 * scale:.1f}pt 'Segoe UI'; }}
QCheckBox:disabled {{ color: {theme.disabled_fg}; }}
QPushButton {{ background: {theme.panel_bg}; color: {theme.text_primary}; border: 1px solid {theme.border};
border-radius: {round(6 * scale)}px; padding: {round(5 * scale)}px {round(10 * scale)}px;
font: 600 {9.5 * scale:.1f}pt 'Segoe UI'; min-height: {round(24 * scale)}px; }}
QPushButton:hover {{ background: {theme.accent}; color: {theme.text_primary}; }}
QPushButton:disabled {{ background: {theme.disabled_bg}; color: {theme.disabled_fg}; }}
QProgressBar {{ color: {theme.text_primary}; background: {theme.app_bg}; border: 1px solid {theme.border};
border-radius: {round(6 * scale)}px; text-align: center; min-height: {round(18 * scale)}px; }}
QProgressBar::chunk {{ background: {theme.success}; border-radius: {round(5 * scale)}px; }}
""")
        size = max(14, round(20 * scale))
        radius = max(2, round(4 * scale))
        for checkbox in dialog.findChildren(QCheckBox):
            style = SquareCheckBoxStyle(
                border_color=theme.border,
                fill_color=theme.accent,
                check_color=theme.text_primary,
                size=size,
                radius=radius,
                parent=checkbox,
            )
            checkbox.setStyle(style)
            styles.append(style)
    else:
        dialog.setStyleSheet(parent.styleSheet())
    layout = dialog.layout()
    if layout is not None:
        layout.setContentsMargins(round(14 * scale), round(12 * scale), round(14 * scale), round(12 * scale))
        layout.setSpacing(round(10 * scale))
    for row in dialog.findChildren(QHBoxLayout):
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(round(8 * scale))
    dialog.setMinimumWidth(round(420 * scale))
    for label in dialog.findChildren(QLabel):
        label.setTextFormat(Qt.TextFormat.PlainText)
    for button in dialog.findChildren(QPushButton):
        button.setCursor(Qt.CursorShape.PointingHandCursor if button.isEnabled() else Qt.CursorShape.ArrowCursor)
    for checkbox in dialog.findChildren(QCheckBox):
        checkbox.setCursor(Qt.CursorShape.PointingHandCursor if checkbox.isEnabled() else Qt.CursorShape.ArrowCursor)
    apply_windows_titlebar_theme(dialog, getattr(parent, "theme_mode", "dark") != "light")


def ask_update_install_preference(
    parent: QWidget,
    *,
    latest_version: str,
    details_text: str = "",
    install_supported: bool = True,
) -> tuple[bool, bool]:
    dialog = QDialog(parent)
    dialog.setModal(True)
    dialog.setWindowTitle("Update available")
    dialog.setMinimumWidth(max(420, round(420 * getattr(parent, "ui_scale_percent", 100) / 100)))
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(10)

    title = QLabel(f"Version {latest_version or 'latest'!s} is available.", dialog)
    title.setObjectName("title")
    layout.addWidget(title)

    auto_install = QCheckBox("Install update automatically", dialog)
    auto_install.setChecked(bool(install_supported))
    auto_install.setEnabled(bool(install_supported))
    layout.addWidget(auto_install)

    mode_hint = QLabel("", dialog)
    mode_hint.setObjectName("settingsSubtext")
    mode_hint.setWordWrap(True)

    def refresh_hint() -> None:
        if auto_install.isEnabled() and auto_install.isChecked():
            mode_hint.setText(
                "TrackIndex will download, verify, install the update, and relaunch automatically."
            )
        elif auto_install.isEnabled():
            mode_hint.setText(
                "TrackIndex will open the download page so you can install the update manually."
            )
        else:
            mode_hint.setText(
                "Automatic install is unavailable here. TrackIndex will open the download page."
            )

    auto_install.toggled.connect(lambda _checked=False: refresh_hint())
    refresh_hint()
    layout.addWidget(mode_hint)

    extra = str(details_text or "").strip()
    if extra:
        details = QLabel(extra, dialog)
        details.setObjectName("settingsSubtext")
        details.setWordWrap(True)
        layout.addWidget(details)

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    later = QPushButton("Not now", dialog)
    update = QPushButton("Update", dialog)
    update.setDefault(True)
    buttons.addWidget(later)
    buttons.addWidget(update)
    layout.addLayout(buttons)
    later.clicked.connect(dialog.reject)
    update.clicked.connect(dialog.accept)

    _prepare_update_dialog(dialog, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    restore_cursor = getattr(parent, "restore_cursor_state_after_modal", None)
    if callable(restore_cursor):
        restore_cursor()
    return accepted, bool(auto_install.isEnabled() and auto_install.isChecked())


def ask_update_handoff(
    parent: QWidget,
    *,
    version: str,
    default_restart: bool = True,
    requires_elevation: bool = False,
) -> tuple[bool, bool]:
    dialog = QDialog(parent)
    dialog.setModal(True)
    dialog.setWindowTitle("Install update")
    dialog.setMinimumWidth(max(420, round(420 * getattr(parent, "ui_scale_percent", 100) / 100)))
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(10)

    title = QLabel("Update is ready to install", dialog)
    title.setObjectName("title")
    body = QLabel(
        f"TrackIndex v{version or 'latest'!s} has been downloaded.\n\n"
        "The installer will now take over.\n\n"
        "Click Update to close TrackIndex and begin installation.",
        dialog,
    )
    body.setWordWrap(True)
    uac_hint = QLabel(
        "Windows may ask for administrator permission to continue this update.", dialog
    )
    uac_hint.setObjectName("settingsSubtext")
    uac_hint.setWordWrap(True)
    uac_hint.setVisible(bool(requires_elevation))
    restart = QCheckBox("Restart TrackIndex after update", dialog)
    restart.setChecked(bool(default_restart))

    layout.addWidget(title)
    layout.addWidget(body)
    layout.addWidget(uac_hint)
    layout.addWidget(restart)
    buttons = QHBoxLayout()
    buttons.addStretch(1)
    abort = QPushButton("Abort", dialog)
    update = QPushButton("Update", dialog)
    update.setDefault(True)
    buttons.addWidget(abort)
    buttons.addWidget(update)
    layout.addLayout(buttons)
    abort.clicked.connect(dialog.reject)
    update.clicked.connect(dialog.accept)

    _prepare_update_dialog(dialog, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    restore_cursor = getattr(parent, "restore_cursor_state_after_modal", None)
    if callable(restore_cursor):
        restore_cursor()
    return accepted, bool(restart.isChecked())


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


class UpdateProgressDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.canceled = False
        self.setWindowTitle("Updating TrackIndex")
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self.label = QLabel("Preparing update...", self)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignRight)
        _prepare_update_dialog(self, parent)

    def _cancel(self) -> None:
        self.canceled = True
        self.reject()

    def set_progress(self, value: float, message: str) -> None:
        self.progress.setValue(round(value))
        self.label.setText(message)

    def set_cancel_enabled(self, enabled: bool) -> None:
        self.cancel_button.setEnabled(bool(enabled))


def styled_message(parent: QWidget, icon: QMessageBox.Icon, title: str, text: str, buttons=QMessageBox.StandardButton.Ok) -> QMessageBox:
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    return box


class DeleteTracksDialog(QDialog):


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
        layout.addWidget(self.dont_ask_again)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No, self)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("Remove")
        buttons.button(QDialogButtonBox.StandardButton.No).setText("Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)


class LibraryImportDialog(QDialog):
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
