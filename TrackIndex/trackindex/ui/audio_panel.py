from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from trackindex.core.audio_settings import AudioSettings, EQ_FREQUENCIES, EQ_PRESETS
from .theme import build_stylesheet
from .interaction import RestoreCursorOnHide, apply_media_crate_scrollbar
from .widgets.custom_controls import RoundHandleSliderStyle, SquareCheckBoxStyle
from .widgets.combo_box import ChevronComboBox


class AudioPanel(RestoreCursorOnHide, QFrame):
    settingsChanged = Signal(object)
    closeRequested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("audioSettingsPanel")
        self.setAccessibleName("Audio settings")
        self._settings = AudioSettings()
        self._updating = False
        self._devices = []
        self._control_styles = {}
        self._sections = []
        self._scale = 1.0
        self._eq_columns = 10
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(100)
        self._commit_timer.timeout.connect(self._commit)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("Audio settings", self)
        title.setObjectName("title")
        header.addWidget(title, 1)
        self.close_button = QPushButton("Close", self)
        self.close_button.setAccessibleName("Close audio settings")
        self.close_button.clicked.connect(self.closeRequested.emit)
        header.addWidget(self.close_button)
        root.addLayout(header)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_media_crate_scrollbar(self.scroll.verticalScrollBar())
        self.body = QWidget(self.scroll)
        self.body.setObjectName("audioSettingsBody")
        self.box = QVBoxLayout(self.body)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll)
        self._headings = []
        transitions = self._section("Transitions")
        self.gapless = QCheckBox("Gapless playback", self)
        transitions.addWidget(self.gapless)
        self._hint("Play consecutive songs without an added pause.", transitions)
        transitions.addSpacing(6)
        self.crossfade = QCheckBox("Crossfade between songs", self)
        transitions.addWidget(self.crossfade)
        self._hint("Fade the ending song into the next. Manual skips stay immediate.", transitions)
        self.fade_options = QWidget(self)
        self.fade_layout = QVBoxLayout(self.fade_options)
        transitions.addWidget(self.fade_options)
        row = QHBoxLayout()
        duration_title = QLabel("Duration", self)
        duration_title.setObjectName("audioSettingLabel")
        self.duration = QSlider(Qt.Orientation.Horizontal, self)
        self.duration.setRange(2, 24)
        self.duration.setAccessibleName("Crossfade duration")
        duration_title.setBuddy(self.duration)
        self.duration_label = QLabel(self)
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(duration_title)
        row.addWidget(self.duration, 1)
        row.addWidget(self.duration_label)
        self.fade_layout.addLayout(row)
        self.preserve = QCheckBox("Preserve album transitions", self)
        self.fade_layout.addWidget(self.preserve)
        self._hint("Consecutive tracks from the same album play without overlap.", self.fade_layout)
        loudness = self._section()
        row = QHBoxLayout()
        label = self._heading("Loudness normalization")
        self.normalization = ChevronComboBox(self)
        for title, value in (("Off", "off"), ("Track", "track"), ("Album", "album")):
            self.normalization.addItem(title, value)
        label.setBuddy(self.normalization)
        self.normalization.setAccessibleName(label.text())
        row.addWidget(label, 1)
        row.addWidget(self.normalization)
        loudness.addLayout(row)
        self.normalization_hint = self._hint("", loudness)
        eq = self._section()
        row = QHBoxLayout()
        self.equalizer = QCheckBox("Equalizer", self)
        self.equalizer.setObjectName("audioSectionTitle")
        self.equalizer.setToolTip("Enable the equalizer. Turning it off keeps your band settings.")
        self.preset = ChevronComboBox(self)
        self.preset.addItems([*EQ_PRESETS, "Custom"])
        self.preset.setAccessibleName("Equalizer preset")
        self.reset = QPushButton("Reset bands", self)
        self.reset.setAutoDefault(False)
        row.addWidget(self.equalizer)
        row.addStretch(1)
        row.addWidget(self.reset)
        eq.addLayout(row)
        preset_row = QHBoxLayout()
        preset_label = QLabel("Preset", self)
        preset_label.setObjectName("audioSettingLabel")
        preset_label.setBuddy(self.preset)
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset, 1)
        eq.addLayout(preset_row)
        self.eq_box = QWidget(self)
        self.eq_grid = QGridLayout(self.eq_box)
        self.bands, self.values, self.captions = [], [], []
        for column, frequency in enumerate(EQ_FREQUENCIES):
            text = f"{frequency:g}" if frequency < 1000 else f"{frequency/1000:g}k"
            value = QLabel("0", self)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = QSlider(Qt.Orientation.Vertical, self)
            slider.setRange(-24, 24)
            slider.setPageStep(2)
            slider.setAccessibleName(f"Equalizer {frequency:g} Hz")
            slider.setTickPosition(QSlider.TickPosition.TicksBothSides)
            slider.setTickInterval(12)
            caption = QLabel(text, self)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.eq_grid.addWidget(value, 0, column)
            self.eq_grid.addWidget(slider, 1, column, Qt.AlignmentFlag.AlignHCenter)
            self.eq_grid.addWidget(caption, 2, column)
            self.eq_grid.setColumnStretch(column, 1)
            self.bands.append(slider)
            self.values.append(value)
            self.captions.append(caption)
            slider.valueChanged.connect(self._bands_changed)
            slider.sliderReleased.connect(self._commit)
        eq.addWidget(self.eq_box)
        self._hint("Bass on the left, treble on the right. Values are in dB; headroom helps prevent distortion.", eq)
        output = self._section("Audio output")
        self.device = ChevronComboBox(self)
        self.device.setAccessibleName("Audio output device")
        self.device.setMinimumContentsLength(20)
        output.addWidget(self.device)
        self.output = self._hint("Output format appears during playback.", output)
        self.box.addStretch(1)
        for check in (self.gapless, self.crossfade, self.preserve, self.equalizer):
            check.toggled.connect(self._controls_changed)
        self.normalization.currentIndexChanged.connect(self._controls_changed)
        self.device.currentIndexChanged.connect(self._controls_changed)
        self.duration.valueChanged.connect(self._controls_changed)
        self.duration.sliderReleased.connect(self._commit)
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.reset.clicked.connect(lambda: self._preset_changed("Flat"))
        self.set_settings(self._settings)

    def _section(self, title=None):
        if self._sections:
            line = QFrame(self.body)
            line.setObjectName("audioDivider")
            line.setFixedHeight(1)
            self.box.addWidget(line)
        section = QVBoxLayout()
        self._sections.append(section)
        self.box.addLayout(section)
        if title:
            section.addWidget(self._heading(title))
        return section

    def _heading(self, text):
        heading = QLabel(text, self)
        heading.setObjectName("audioSectionTitle")
        heading.setWordWrap(True)
        self._headings.append(heading)
        return heading

    def _hint(self, text, layout):
        label = QLabel(text, self)
        label.setObjectName("muted")
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        layout.addWidget(label)
        return label

    def set_settings(self, settings):
        self._commit_timer.stop()
        self._settings = settings
        self._updating = True
        self.gapless.setChecked(settings.gapless)
        self.crossfade.setChecked(settings.crossfade)
        self.preserve.setChecked(settings.preserve_albums)
        self.equalizer.setChecked(settings.eq_enabled)
        self.duration.setValue(round(settings.crossfade_seconds * 2))
        self.normalization.setCurrentIndex(self.normalization.findData(settings.normalization))
        for slider, gain in zip(self.bands, settings.eq_gains, strict=True):
            slider.setValue(round(gain * 2))
        self._updating = False
        self.set_devices(self._devices)
        self._refresh_labels()

    def set_devices(self, devices):
        selected = self.device.currentData() if self._commit_timer.isActive() else self._settings.device
        self._devices = devices
        self._updating = True
        self.device.clear()
        self.device.addItem("System default", "auto")
        for item in devices:
            if isinstance(item, dict) and item.get("name") and item["name"] != "auto":
                self.device.addItem(str(item.get("description") or item["name"]), item["name"])
        index = self.device.findData(selected)
        if index < 0:
            self.device.addItem("Selected device unavailable (using System default)", selected)
            index = self.device.count() - 1
        self.device.setCurrentIndex(index)
        self._updating = False

    def set_output(self, output):
        if output.sample_rate:
            self.output.setText(f"Playing at {output.sample_rate / 1000:g} kHz · {output.sample_format or 'audio'} · {output.channels or '?'} channels")
        else:
            self.output.setText("Output format appears during playback.")

    def _refresh_labels(self):
        self.duration_label.setText(f"{self.duration.value()/2:g} s")
        self.fade_options.setEnabled(self.crossfade.isChecked())
        self.eq_box.setEnabled(self.equalizer.isChecked())
        self.preset.setEnabled(self.equalizer.isChecked())
        self.reset.setEnabled(self.equalizer.isChecked())
        self.normalization_hint.setText({
            "off": "Play songs at their original loudness.",
            "track": "Balance loudness between songs, useful for shuffled or mixed playlists.",
            "album": "Balance loudness between albums while preserving quieter and louder songs within each album.",
        }.get(self.normalization.currentData(), ""))
        for slider, label in zip(self.bands, self.values, strict=True):
            label.setText(f"{slider.value()/2:+g}" if slider.value() else "0")
        gains = tuple(slider.value()/2 for slider in self.bands)
        preset = next((name for name, values in EQ_PRESETS.items() if gains == values), "Custom")
        self.preset.blockSignals(True)
        self.preset.setCurrentText(preset)
        self.preset.blockSignals(False)

    def _controls_changed(self, *_args):
        if not self._updating:
            self._refresh_labels()
            self._commit_timer.start()

    def _bands_changed(self):
        self._controls_changed()

    def _preset_changed(self, name):
        if self._updating or name not in EQ_PRESETS:
            return
        self._updating = True
        for slider, gain in zip(self.bands, EQ_PRESETS[name], strict=True):
            slider.setValue(round(gain * 2))
        self._updating = False
        self._refresh_labels()
        self._commit()

    def _commit(self):
        self._commit_timer.stop()
        if self._updating:
            return
        value = AudioSettings(
            gapless=self.gapless.isChecked(), normalization=self.normalization.currentData(),
            eq_enabled=self.equalizer.isChecked(), eq_gains=tuple(s.value()/2 for s in self.bands),
            crossfade=self.crossfade.isChecked(), crossfade_seconds=self.duration.value()/2,
            preserve_albums=self.preserve.isChecked(), device=self.device.currentData() or "auto",
        )
        self._settings = value
        self.settingsChanged.emit(value)

    def set_appearance(self, theme, scale):
        self._scale = scale
        self.setStyleSheet(build_stylesheet(theme, scale) + f"""
            QFrame#audioSettingsPanel {{ background: {theme.app_bg}; border: 1px solid {theme.border}; border-radius: {round(8*scale)}px; }}
            QWidget#audioSettingsBody {{ background: {theme.app_bg}; }}
            QFrame#audioDivider {{ background: {theme.border}; border: none; }}
            QLabel#audioSectionTitle, QCheckBox#audioSectionTitle {{ font: 700 {max(7.5, 10*scale):.1f}pt "Segoe UI"; }}
            QLabel#audioSettingLabel {{ font: 600 {max(7, 9.1*scale):.1f}pt "Segoe UI"; }}
        """)
        self.layout().setContentsMargins(round(12*scale), round(10*scale), round(12*scale), round(10*scale))
        self.layout().setSpacing(round(8*scale))
        self.box.setContentsMargins(round(14*scale), round(6*scale), round(14*scale), round(10*scale))
        self.box.setSpacing(round(14*scale))
        for section in self._sections:
            section.setSpacing(round(6*scale))
        self.fade_layout.setContentsMargins(round(24*scale), round(4*scale), 0, 0)
        self.fade_layout.setSpacing(round(6*scale))
        self.normalization.setFixedWidth(round(126*scale))
        self.duration_label.ensurePolished()
        metrics = self.duration_label.fontMetrics()
        self.duration_label.setFixedWidth(max(
            metrics.horizontalAdvance(f"{value/2:g} s")
            for value in range(self.duration.minimum(), self.duration.maximum() + 1)
        ) + round(2*scale))
        for combo in (self.normalization, self.preset, self.device):
            combo.set_theme(theme, scale)
        for check in (self.gapless, self.crossfade, self.preserve, self.equalizer):
            if check not in self._control_styles:
                style = SquareCheckBoxStyle(border_color=theme.border, fill_color=theme.accent,
                                            check_color=theme.text_primary)
                style.setParent(check)
                self._control_styles[check] = style
                check.setStyle(style)
            style = self._control_styles[check]
            style.set_colors(border_color=theme.border, fill_color=theme.accent, check_color=theme.text_primary)
            style.set_metrics(size=round(18*scale), radius=3)
        for slider in (self.duration,):
            if slider not in self._control_styles:
                style = RoundHandleSliderStyle(handle_color=theme.text_primary, border_color=theme.border,
                                               groove_color=theme.border, fill_color=theme.accent, parent=slider)
                self._control_styles[slider] = style
                slider.setStyle(style)
            style = self._control_styles[slider]
            style.set_colors(handle_color=theme.text_primary, border_color=theme.border,
                             groove_color=theme.border, fill_color=theme.accent)
            style.set_metrics(handle_size=round(18*scale), groove_height=round(6*scale))
        self.eq_box.setStyleSheet(f"""
            QSlider::groove:vertical {{ background: {theme.border}; width: {round(4*scale)}px; border-radius: {round(2*scale)}px; }}
            QSlider::handle:vertical {{ background: {theme.text_primary}; border: {max(1, round(scale))}px solid {theme.border};
                height: {round(14*scale)-2*max(1, round(scale))}px; margin: 0 -{round(5*scale)}px; border-radius: {round(7*scale)}px; }}
            QSlider::add-page:vertical {{ background: {theme.accent}; border-radius: {round(2*scale)}px; }}
            QSlider::handle:vertical:disabled {{ background: {theme.disabled_fg}; }}
            QSlider::add-page:vertical:disabled {{ background: {theme.border}; }}
        """)
        for slider in self.bands:
            slider.setMinimumHeight(round(130*scale))
            slider.setMinimumWidth(round(24*scale))
        self._layout_bands()

    def natural_height(self) -> int:
        self.layout().activate()
        self.box.activate()
        return self.layout().sizeHint().height() - self.scroll.sizeHint().height() + self.box.sizeHint().height()

    def flush_pending_changes(self) -> None:
        if self._commit_timer.isActive():
            self._commit()

    def close_dropdowns(self) -> None:
        for combo in self.findChildren(ChevronComboBox):
            if combo.view().isVisible():
                combo.hidePopup()

    def focusNextPrevChild(self, next: bool) -> bool:
        current = QApplication.focusWidget() or self
        candidate = current
        while True:
            candidate = candidate.nextInFocusChain() if next else candidate.previousInFocusChain()
            if candidate is current:
                return False
            if (self.isAncestorOf(candidate) and candidate.isVisibleTo(self)
                    and candidate.isEnabled() and candidate.focusPolicy() & Qt.FocusPolicy.TabFocus):
                candidate.setFocus(Qt.FocusReason.TabFocusReason if next else Qt.FocusReason.BacktabFocusReason)
                return True

    def _layout_bands(self):
        columns = 5 if self.width() < round(600*self._scale) else 10
        if columns == self._eq_columns:
            return
        self._eq_columns = columns
        for index, (slider, value, caption) in enumerate(zip(self.bands, self.values, self.captions, strict=True)):
            row, column = divmod(index, columns)
            self.eq_grid.addWidget(value, row*3, column)
            self.eq_grid.addWidget(slider, row*3+1, column, Qt.AlignmentFlag.AlignHCenter)
            self.eq_grid.addWidget(caption, row*3+2, column)
        for column in range(10):
            self.eq_grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_bands()

    def closeEvent(self, event):
        self.flush_pending_changes()
        event.ignore()
        self.closeRequested.emit()
