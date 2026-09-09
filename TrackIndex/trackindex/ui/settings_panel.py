from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .interaction import sync_pointer_cursor
from .theme import ThemePalette
from .widgets.custom_controls import RoundHandleSliderStyle, SquareCheckBoxStyle


class SettingsPanel(QWidget):
    uiScaleChanged = Signal(int)
    autoUpdatesChanged = Signal(bool)
    confirmTrackDeletionChanged = Signal(bool)
    checkUpdatesRequested = Signal()
    resetSettingsRequested = Signal()
    musicRootRequested = Signal()
    scanLibrariesRequested = Signal()
    _UI_SCALE_STEP = 5

    def __init__(
        self,
        theme: ThemePalette,
        ui_scale_percent: int,
        auto_updates: bool,
        music_root: str = "",
        parent: QWidget | None = None,
        confirm_track_deletion: bool = True,
    ) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("settingsBody")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._settings_card_layouts: list[QVBoxLayout] = []
        self._pending_ui_scale_percent: int | None = None
        self._scale_commit_timer = QTimer(self)
        self._scale_commit_timer.setSingleShot(True)
        self._scale_commit_timer.setInterval(160)
        self._scale_commit_timer.timeout.connect(self._commit_scale_change)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(7)
        self._root_layout = layout

        title = QLabel("Settings", self)
        title.setObjectName("title")
        layout.addWidget(title)

        interface_card, interface_layout = self._create_settings_card("Interface")
        scale_row = QHBoxLayout()
        scale_label = QLabel("UI size", interface_card)
        scale_label.setObjectName("settingsSubtext")
        scale_row.addWidget(scale_label)
        scale_row.addStretch(1)
        self.scale_value = QLabel(f"{ui_scale_percent}%", interface_card)
        self.scale_value.setObjectName("muted")
        self.scale_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scale_row.addWidget(self.scale_value)
        interface_layout.addLayout(scale_row)
        self.scale_slider = QSlider(Qt.Orientation.Horizontal, interface_card)
        self.scale_slider.setRange(75, 200)
        self.scale_slider.setSingleStep(self._UI_SCALE_STEP)
        self.scale_slider.setPageStep(self._UI_SCALE_STEP)
        self.scale_slider.setValue(ui_scale_percent)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)
        self.scale_slider.sliderReleased.connect(self._commit_scale_change)
        interface_layout.addWidget(self.scale_slider)
        self.confirm_deletion = QCheckBox("Confirm before removing tracks", interface_card)
        self.confirm_deletion.setChecked(confirm_track_deletion)
        self.confirm_deletion.toggled.connect(self.confirmTrackDeletionChanged.emit)
        interface_layout.addWidget(self.confirm_deletion)

        library_card, library_layout = self._create_settings_card("Music libraries")
        library_hint = QLabel("Choose the root folder used to discover playlists.", library_card)
        library_hint.setObjectName("settingsSubtext")
        library_hint.setWordWrap(True)
        library_hint.setMinimumWidth(0)
        library_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        library_layout.addWidget(library_hint)
        self.music_root_label = QLineEdit(library_card)
        self.music_root_label.setObjectName("settingsReadOnlyInput")
        self.music_root_label.setReadOnly(True)
        self.music_root_label.setMinimumWidth(0)
        self.music_root_label.setText(music_root or "Not configured")
        library_layout.addWidget(self.music_root_label)
        self.choose_root_button = QPushButton("Change music folder", library_card)
        self.choose_root_button.setObjectName("settingsActionButton")
        self.choose_root_button.clicked.connect(self.musicRootRequested.emit)
        library_layout.addWidget(self.choose_root_button)
        self.scan_button = QPushButton("Scan for libraries", library_card)
        self.scan_button.setObjectName("settingsActionButton")
        self.scan_button.clicked.connect(self.scanLibrariesRequested.emit)
        library_layout.addWidget(self.scan_button)

        updates_card, updates_layout = self._create_settings_card("Updates")
        self.auto_updates = QCheckBox("Automatic updates", updates_card)
        self.auto_updates.setChecked(auto_updates)
        self.auto_updates.toggled.connect(self.autoUpdatesChanged.emit)
        updates_layout.addWidget(self.auto_updates)
        self.check_updates = QPushButton("Check for updates now", updates_card)
        self.check_updates.setObjectName("settingsActionButton")
        self.check_updates.clicked.connect(self.checkUpdatesRequested.emit)
        updates_layout.addWidget(self.check_updates)
        self.reset_button = QPushButton("Reset all settings", updates_card)
        self.reset_button.setObjectName("settingsActionButton")
        self.reset_button.clicked.connect(self.resetSettingsRequested.emit)
        updates_layout.addWidget(self.reset_button)
        self.update_status = QLabel("Update status: ready", updates_card)
        self.update_status.setObjectName("muted")
        self.update_status.setWordWrap(True)
        self.update_status.setMinimumWidth(0)
        self.update_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        updates_layout.addWidget(self.update_status)

        layout.addStretch(1)
        self._slider_style = RoundHandleSliderStyle(
            handle_color=theme.text_primary,
            border_color=theme.border,
            groove_color=theme.border,
            fill_color=theme.accent,
            parent=self.scale_slider,
        )
        self.scale_slider.setStyle(self._slider_style)
        self._checkbox_style = SquareCheckBoxStyle(
            border_color=theme.border,
            fill_color=theme.accent,
            check_color=theme.text_primary,
            size=20,
            radius=4,
            parent=self.auto_updates,
        )
        self.auto_updates.setStyle(self._checkbox_style)
        self._delete_checkbox_style = SquareCheckBoxStyle(
            border_color=theme.border,
            fill_color=theme.accent,
            check_color=theme.text_primary,
            size=20,
            radius=4,
            parent=self.confirm_deletion,
        )
        self.confirm_deletion.setStyle(self._delete_checkbox_style)
        self.set_scale(ui_scale_percent / 100)
        self.refresh_cursor_state()

    def _create_settings_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(self)
        card.setObjectName("settingsCard")
        card.setMinimumWidth(0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(6)
        header = QLabel(title, card)
        header.setObjectName("settingsCardTitle")
        layout.addWidget(header)
        self._settings_card_layouts.append(layout)
        self._root_layout.addWidget(card)
        return card, layout

    @staticmethod
    def _scaled(value: int, scale: float, minimum: int = 1) -> int:
        return max(minimum, round(value * scale))

    def set_scale(self, scale: float) -> None:
        scale = max(0.5, min(2.0, float(scale)))
        margin = self._scaled(14, scale, 8)
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        self._root_layout.setSpacing(self._scaled(7, scale, 4))
        for layout in self._settings_card_layouts:
            layout.setContentsMargins(
                self._scaled(8, scale, 5), self._scaled(7, scale, 4),
                self._scaled(8, scale, 5), self._scaled(7, scale, 4),
            )
            layout.setSpacing(self._scaled(6, scale, 4))
        self._slider_style.set_metrics(
            handle_size=self._scaled(18, scale, 14), groove_height=self._scaled(6, scale, 4)
        )
        self._checkbox_style.set_metrics(
            size=self._scaled(20, scale, 14), radius=self._scaled(4, scale, 2)
        )
        self._delete_checkbox_style.set_metrics(
            size=self._scaled(20, scale, 14), radius=self._scaled(4, scale, 2)
        )
        self.music_root_label.setMinimumHeight(self._scaled(30, scale, 22))
        action_height = self._scaled(32, scale, 24)
        for button in (self.choose_root_button, self.scan_button, self.check_updates, self.reset_button):
            button.setMinimumHeight(action_height)
        max_width = QFontMetrics(self.scale_value.font()).horizontalAdvance("200%")
        self.scale_value.setMinimumWidth(max_width + self._scaled(12, scale, 8))
        self.scale_slider.update()
        self.auto_updates.update()
        self.confirm_deletion.update()

    def set_theme(self, theme: ThemePalette) -> None:
        self.theme = theme
        self._slider_style.set_colors(
            handle_color=theme.text_primary, border_color=theme.border,
            groove_color=theme.border, fill_color=theme.accent,
        )
        self._checkbox_style.set_colors(
            border_color=theme.border, fill_color=theme.accent, check_color=theme.text_primary,
        )
        self._delete_checkbox_style.set_colors(
            border_color=theme.border,
            fill_color=theme.accent,
            check_color=theme.text_primary,
        )


        self.scale_slider.setStyle(self._slider_style)
        self.auto_updates.setStyle(self._checkbox_style)
        self.confirm_deletion.setStyle(self._delete_checkbox_style)
        self.scale_slider.update()
        self.auto_updates.update()
        self.confirm_deletion.update()

    def _on_scale_changed(self, value: int) -> None:
        rounded = max(75, min(200, int(round(value / self._UI_SCALE_STEP) * self._UI_SCALE_STEP)))
        if rounded != value:
            self.scale_slider.blockSignals(True)
            self.scale_slider.setValue(rounded)
            self.scale_slider.blockSignals(False)
        self.scale_value.setText(f"{rounded}%")
        self._pending_ui_scale_percent = rounded
        if not self.scale_slider.isSliderDown():
            self._scale_commit_timer.start()

    def _commit_scale_change(self) -> None:
        self._scale_commit_timer.stop()
        if self._pending_ui_scale_percent is None:
            return
        value = self._pending_ui_scale_percent
        self._pending_ui_scale_percent = None
        self.uiScaleChanged.emit(value)

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def set_values(
        self,
        ui_scale_percent: int,
        auto_updates: bool,
        confirm_track_deletion: bool = True,
    ) -> None:
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(ui_scale_percent)
        self.scale_slider.blockSignals(False)
        self.scale_value.setText(f"{ui_scale_percent}%")
        self._pending_ui_scale_percent = None
        self._scale_commit_timer.stop()
        self.auto_updates.blockSignals(True)
        self.auto_updates.setChecked(auto_updates)
        self.auto_updates.blockSignals(False)
        self.confirm_deletion.blockSignals(True)
        self.confirm_deletion.setChecked(confirm_track_deletion)
        self.confirm_deletion.blockSignals(False)

    def set_music_root(self, path: str) -> None:
        self.music_root_label.setText(path or "Not configured")

    def refresh_cursor_state(self) -> None:
        self.setCursor(Qt.CursorShape.ArrowCursor)
        for widget in (
            self.scale_slider, self.choose_root_button, self.scan_button,
            self.auto_updates, self.confirm_deletion, self.check_updates, self.reset_button,
        ):
            sync_pointer_cursor(widget)
