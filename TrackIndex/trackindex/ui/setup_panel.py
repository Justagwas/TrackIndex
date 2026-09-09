from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from trackindex.core.config import default_playlist_name
from trackindex.core.models import (
    AuditResult,
    ChangePlan,
    LibraryRecord,
    OrderAuthority,
    StorageMode,
)
from trackindex.ui.interaction import apply_media_crate_scrollbar


class StorageModeCard(QPushButton):
    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("setupModeCard")
        self.setCheckable(True)
        self.setText("")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(138)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)

        layout = QVBoxLayout(self)
        self._card_layout = layout
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("setupModeTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.description_label = QLabel(description, self)
        self.description_label.setObjectName("setupModeDescription")
        self.description_label.setWordWrap(True)
        self.description_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.description_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.description_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title_label)
        layout.addWidget(self.description_label, 1)

    def set_scale(self, scale: float) -> None:
        scale = max(0.75, min(2.0, scale))
        self.setFixedHeight(max(104, round(138 * scale)))
        self._card_layout.setContentsMargins(round(13 * scale), round(12 * scale), round(13 * scale), round(12 * scale))
        self._card_layout.setSpacing(max(4, round(6 * scale)))


class SetupScrollContent(QWidget):
    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        self._full_text = ""
        super().__init__(parent)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._refresh_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        margins = self.contentsMargins()
        return QSize(
            metrics.horizontalAdvance(self._full_text) + margins.left() + margins.right(),
            metrics.height() + margins.top() + margins.bottom(),
        )

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(0, hint.height())

    def _refresh_text(self) -> None:
        metrics = self.fontMetrics()
        available = max(1, self.contentsRect().width())
        shown = self._full_text
        if metrics.horizontalAdvance(shown) > available:
            suffix = "..."
            while shown and metrics.horizontalAdvance(shown + suffix) > available:
                shown = shown[:-1]
            shown = (shown.rstrip() + suffix) if shown else suffix
        QLabel.setText(self, shown)


class LibrarySetupPanel(QWidget):
    selectionChanged = Signal(str, str)
    playlistChanged = Signal(str)
    submitRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("librarySetupPage")
        self._updating = False
        self._mode = StorageMode.BOTH
        self._authority: OrderAuthority | None = None
        self._playlist_name = ""
        self._source_playlist_name = ""
        self._playlist_names: list[str] = []
        self._track_count = 0
        self._horizontal_margin = 18

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("setupScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        apply_media_crate_scrollbar(self.scroll_area.verticalScrollBar())
        page_layout.addWidget(self.scroll_area)

        content = SetupScrollContent(self.scroll_area)
        content.setObjectName("setupScrollContent")
        outer = QHBoxLayout(content)
        self.outer_layout = outer
        outer.setContentsMargins(18, 10, 18, 18)

        card = QFrame(content)
        card.setObjectName("librarySetupCard")
        self.card = card
        card.setFixedSize(640, 440)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(card)
        self.card_layout = layout
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(13)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        heading = QVBoxLayout()
        heading.setSpacing(6)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = ElidedLabel("Set up playlist", card)
        self.title.setObjectName("title")
        self.title.setMaximumWidth(200)
        title_row.addWidget(self.title)
        self.heading_separator = QLabel("|", card)
        self.heading_separator.setObjectName("setupHeadingSeparator")
        title_row.addWidget(self.heading_separator)
        self.detected = ElidedLabel("", card)
        self.detected.setObjectName("setupHeadingMeta")
        self.detected.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.detected, 1)
        heading.addLayout(title_row)
        self.subtitle = QLabel("Choose how TrackIndex should keep this playlist ordered.", card)
        self.subtitle.setObjectName("muted")
        self.subtitle.setWordWrap(True)
        heading.addWidget(self.subtitle)
        layout.addLayout(heading)

        mode_label = QLabel("Choose how to save the order", card)
        mode_label.setObjectName("setupSectionLabel")
        layout.addWidget(mode_label)
        mode_row = QHBoxLayout()
        self.mode_row = mode_row
        mode_row.setSpacing(12)
        mode_row.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[StorageMode, StorageModeCard] = {}
        mode_text = {
            StorageMode.FILENAMES: (
                "Filename Prefixes",
                "Rename files with numbered prefixes such as 001, 002, and 003. The filenames become the saved order.",
            ),
            StorageMode.M3U8: (
                "Playlist File (.m3u8)",
                "Keep filenames unchanged and save the track order in a dedicated playlist file.",
            ),
            StorageMode.BOTH: (
                "Filename Prefixes + Playlist File",
                "Save the same order in both numbered filenames and a dedicated playlist file.",
            ),
        }
        for mode in StorageMode:
            title, description = mode_text[mode]
            button = StorageModeCard(title, description, card)
            button.clicked.connect(lambda _checked=False, selected=mode: self._mode_changed(selected))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            mode_row.addWidget(button, 1)
        layout.addLayout(mode_row)

        self.playlist_section = QWidget(card)
        playlist_layout = QVBoxLayout(self.playlist_section)
        playlist_layout.setContentsMargins(0, 0, 0, 0)
        playlist_layout.setSpacing(7)
        self.playlist_label = QLabel("Playlist file", self.playlist_section)
        self.playlist_label.setObjectName("setupSectionLabel")
        playlist_layout.addWidget(self.playlist_label)
        self.playlist_combo = QComboBox(self.playlist_section)
        self.playlist_combo.currentIndexChanged.connect(self._playlist_changed)
        playlist_layout.addWidget(self.playlist_combo)
        layout.addWidget(self.playlist_section)

        self.source_section = QWidget(card)
        source_layout = QVBoxLayout(self.source_section)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(7)
        source_label = QLabel("Starting order", self.source_section)
        source_label.setObjectName("setupSectionLabel")
        source_layout.addWidget(source_label)
        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        self.filename_source = QPushButton("Use filename order", self.source_section)
        self.filename_source.setObjectName("setupChoiceButton")
        self.filename_source.setCheckable(True)
        self.playlist_source = QPushButton("Use playlist order", self.source_section)
        self.playlist_source.setObjectName("setupChoiceButton")
        self.playlist_source.setCheckable(True)
        self.source_group = QButtonGroup(self)
        self.source_group.setExclusive(True)
        self.source_group.addButton(self.filename_source)
        self.source_group.addButton(self.playlist_source)
        self.filename_source.clicked.connect(lambda: self._source_changed(OrderAuthority.FILENAMES))
        self.playlist_source.clicked.connect(lambda: self._source_changed(OrderAuthority.PLAYLIST))
        source_row.addWidget(self.filename_source)
        source_row.addWidget(self.playlist_source)
        source_layout.addLayout(source_row)
        layout.addWidget(self.source_section)

        self.summary = QFrame(card)
        self.summary.setObjectName("setupSummary")
        self.summary.setFixedHeight(100)
        summary_layout = QHBoxLayout(self.summary)
        self.summary_layout = summary_layout
        summary_layout.setContentsMargins(13, 11, 13, 11)
        summary_layout.setSpacing(14)
        summary_text = QVBoxLayout()
        summary_text.setSpacing(6)
        self.summary_title = QLabel("What TrackIndex will do", self.summary)
        self.summary_title.setObjectName("setupSummaryTitle")
        summary_text.addWidget(self.summary_title)
        self.impact = QLabel("", self.summary)
        self.impact.setObjectName("setupImpact")
        self.impact.setWordWrap(True)
        self.impact.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        summary_text.addWidget(self.impact)
        summary_layout.addLayout(summary_text, 1)

        self.cancel_button = QPushButton("Cancel", self.summary)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        self.submit_button = QPushButton("Set up playlist", self.summary)
        self.submit_button.setObjectName("primaryButton")
        self.submit_button.clicked.connect(self.submitRequested.emit)
        summary_layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignVCenter)
        summary_layout.addWidget(self.submit_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.summary)

        self.issues_scroll = QScrollArea(card)
        self.issues_scroll.setObjectName("setupIssuesScroll")
        self.issues_scroll.setWidgetResizable(True)
        self.issues_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.issues_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.issues_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        apply_media_crate_scrollbar(self.issues_scroll.verticalScrollBar())
        issues_content = QWidget(self.issues_scroll)
        issues_layout = QVBoxLayout(issues_content)
        issues_layout.setContentsMargins(0, 0, 6, 0)
        self.issues = QLabel("", issues_content)
        self.issues.setObjectName("warning")
        self.issues.setWordWrap(True)
        self.issues.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        issues_layout.addWidget(self.issues)
        self.issues_scroll.setWidget(issues_content)
        self.issues_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.issues_scroll.hide()
        layout.addWidget(self.issues_scroll)

        outer.addWidget(card, 1, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(content)

    def set_compact_viewport(self, compact: bool) -> None:
        self._horizontal_margin = 0 if compact else 18
        self._sync_top_anchor()

    def _sync_top_anchor(self) -> None:
        scale = getattr(self, "_scale", 1.0)
        baseline = max(440, round(440 * scale))
        viewport_height = self.scroll_area.viewport().height()
        top = max(10, round((viewport_height - baseline - 8) / 2))
        self.outer_layout.setContentsMargins(self._horizontal_margin, top, self._horizontal_margin, 18)

    def _schedule_card_height(self) -> None:
        QTimer.singleShot(0, self._sync_card_height)

    def _sync_card_height(self) -> None:
        base_summary_height = max(75, round(100 * getattr(self, "_scale", 1.0)))
        margins = self.summary_layout.contentsMargins()
        summary_width = max(
            1,
            self.card.width() - self.card_layout.contentsMargins().left() - self.card_layout.contentsMargins().right(),
        )
        action_buttons = tuple(button for button in (self.cancel_button, self.submit_button) if button.isVisible())
        impact_width = max(
            180,
            summary_width
            - margins.left()
            - margins.right()
            - sum(button.sizeHint().width() for button in action_buttons)
            - self.summary_layout.spacing() * len(action_buttons),
        )
        impact_height = max(
            self.impact.fontMetrics().lineSpacing() * max(1, self.impact.text().count("\n") + 1),
            self.impact.fontMetrics()
            .boundingRect(
                QRect(0, 0, impact_width, 10000),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                self.impact.text(),
            )
            .height(),
        )
        summary_height = max(
            base_summary_height,
            margins.top() + margins.bottom() + self.summary_title.sizeHint().height() + 6 + impact_height,
        )
        self.summary.setFixedHeight(summary_height)
        self.card_layout.activate()
        baseline = max(440, round(440 * getattr(self, "_scale", 1.0)))
        extra = max(0, summary_height - base_summary_height)
        spacing = self.card_layout.spacing()
        if self.source_section.isVisible():
            extra += self.source_section.sizeHint().height() + spacing
        if self.issues_scroll.isVisible():
            extra += self.issues_scroll.height() + spacing
        self.card.setFixedHeight(baseline + extra)
        self.card.updateGeometry()
        self._sync_top_anchor()
        QTimer.singleShot(0, self._sync_top_anchor)
        content = self.scroll_area.widget()
        if content is not None:
            content.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_top_anchor)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_top_anchor)

    def set_context(
        self,
        audit: AuditResult,
        record: LibraryRecord,
        mode: StorageMode,
        authority: OrderAuthority | None,
        playlist_name: str,
        reconfiguring: bool,
    ) -> None:
        self._updating = True
        self._mode = mode
        self._authority = authority
        self._playlist_name = playlist_name
        self._source_playlist_name = audit.selected_playlist.name if audit.selected_playlist else ""
        self._track_count = len(audit.tracks)
        self.title.setText("Change ordering method" if reconfiguring else f"Set up {record.name}")
        self.subtitle.setText(
            "Choose how TrackIndex should maintain this library. Changes are validated and can be undone."
        )
        indexed = sum(track.filename_index is not None for track in audit.tracks)
        self.detected.setText(
            f"Detected {len(audit.tracks)} tracks, {indexed} filename prefixes, and "
            f"{len(audit.playlists)} playlist file(s)."
        )

        default_name = default_playlist_name(audit.folder)
        names: list[str] = []
        seen_names: set[str] = set()
        for path in audit.playlists:
            name = path.name if path.suffix.casefold() == ".m3u8" else path.with_suffix(".m3u8").name
            if name.casefold() not in seen_names:
                names.append(name)
                seen_names.add(name.casefold())
        if not any(name.casefold() == default_name.casefold() for name in names):
            names.append(default_name)
        self._playlist_names = names
        self.playlist_combo.clear()
        for name in names:
            suffix = " (new)" if not (audit.folder / name).exists() else ""
            self.playlist_combo.addItem(name + suffix, name)
        chosen = self.playlist_combo.findData(playlist_name)
        self.playlist_combo.setCurrentIndex(max(0, chosen))
        shown_name = playlist_name or default_name
        if mode is StorageMode.FILENAMES:
            self.playlist_combo.clear()
            self.playlist_combo.addItem("None", "")
            self.playlist_combo.setEnabled(False)
        else:
            chosen = self.playlist_combo.findData(shown_name)
            self.playlist_combo.setCurrentIndex(max(0, chosen))
            shown_name = str(self.playlist_combo.currentData() or default_name)
            self.playlist_combo.setEnabled(True)
        self._playlist_name = shown_name
        source_name = self._source_playlist_name or shown_name
        self.playlist_source.setText(f"Use {source_name} order")

        has_playlist_order = bool(audit.playlist_order and audit.selected_playlist is not None)
        self.source_section.setVisible(has_playlist_order)
        self.filename_source.setChecked(authority is OrderAuthority.FILENAMES)
        self.playlist_source.setChecked(authority is OrderAuthority.PLAYLIST)
        self.mode_buttons[mode].setChecked(True)
        self.playlist_section.show()
        self.cancel_button.setVisible(reconfiguring)
        self.submit_button.setText("Apply method" if reconfiguring else "Set up playlist")
        self._updating = False
        self._schedule_card_height()

    def set_scale(self, scale: float) -> None:
        scale = max(0.75, min(2.0, scale))
        self._scale = scale
        self.card.setFixedWidth(640)
        self.summary.setFixedHeight(max(75, round(100 * scale)))
        self.issues_scroll.setFixedHeight(max(58, round(82 * scale)))
        for button in self.mode_buttons.values():
            button.set_scale(scale)
        self._schedule_card_height()

    def set_plan(self, plan: ChangePlan | None, needs_source: bool = False) -> None:
        if plan is None:
            self.impact.setText("Select a starting order before TrackIndex can prepare the changes.")
            self.issues.setText("Filename and playlist orders disagree. Choose which order to use.")
            self.issues_scroll.setVisible(needs_source)
            self.submit_button.setEnabled(False)
            self._schedule_card_height()
            return

        self._authority = plan.authority
        lines = [
            "Use filename order as the starting order."
            if plan.authority is OrderAuthority.FILENAMES
            else f"Use {self._source_playlist_name or self._playlist_name or 'the playlist file'} as the starting order."
        ]
        if plan.storage_mode is StorageMode.M3U8:
            noun = "track filename" if self._track_count == 1 else "track filenames"
            lines.append(f"Keep all {self._track_count} {noun} unchanged.")
        elif plan.renames:
            noun = "file" if len(plan.renames) == 1 else "files"
            lines.append(f"Rename {len(plan.renames)} {noun} with contiguous numeric prefixes.")
        else:
            lines.append("The filename prefixes already match; no files will be renamed.")

        operation = plan.playlist_operations[0] if plan.playlist_operations else None
        if operation is not None:
            if plan.storage_mode is StorageMode.FILENAMES:
                lines.append(f"Update references in {operation.path.name} so renamed tracks remain valid.")
            else:
                verb = "Create" if operation.action == "create" else "Update"
                noun = "track" if self._track_count == 1 else "tracks"
                if (
                    verb == "Create"
                    and self._source_playlist_name.casefold().endswith(".m3u")
                    and operation.path.suffix.casefold() == ".m3u8"
                ):
                    lines.append(
                        f"Create {operation.path.name} from {self._source_playlist_name} with "
                        f"{self._track_count} {noun}."
                    )
                    lines.append(f"Keep legacy {self._source_playlist_name} unchanged.")
                else:
                    lines.append(f"{verb} {operation.path.name} with {self._track_count} {noun}.")
        elif plan.storage_mode is StorageMode.FILENAMES:
            lines.append("Do not create or update a playlist file.")
        else:
            lines.append("No playlist-file change could be prepared.")
        self.impact.setText("\n".join(lines))

        messages = (*plan.blockers, *plan.warnings)
        self.issues.setText("\n".join(messages))
        self.issues_scroll.setVisible(bool(messages))
        self.submit_button.setEnabled(plan.can_apply)
        self._schedule_card_height()

    def selected_mode(self) -> StorageMode:
        for mode, button in self.mode_buttons.items():
            if button.isChecked():
                return mode
        return StorageMode.BOTH

    def selected_authority(self) -> OrderAuthority | None:
        if self.filename_source.isChecked():
            return OrderAuthority.FILENAMES
        if self.playlist_source.isChecked():
            return OrderAuthority.PLAYLIST
        return None

    def selected_playlist_name(self) -> str:
        return str(self.playlist_combo.currentData() or "")

    def _mode_changed(self, mode: StorageMode) -> None:
        self._mode = mode
        self.playlist_section.show()
        self.playlist_combo.blockSignals(True)
        if mode is StorageMode.FILENAMES:
            self.playlist_combo.clear()
            self.playlist_combo.addItem("None", "")
            self.playlist_combo.setEnabled(False)
        else:
            self.playlist_combo.clear()
            for name in self._playlist_names:
                self.playlist_combo.addItem(name, name)
            selected = self.playlist_combo.findData(self._playlist_name)
            self.playlist_combo.setCurrentIndex(max(0, selected))
            self.playlist_combo.setEnabled(True)
        self.playlist_combo.blockSignals(False)
        if not self._updating:
            authority = self.selected_authority()
            self.selectionChanged.emit(mode.value, authority.value if authority else "")

    def _source_changed(self, authority: OrderAuthority) -> None:
        self._authority = authority
        if not self._updating:
            self.selectionChanged.emit(self.selected_mode().value, authority.value)

    def _playlist_changed(self) -> None:
        if self._updating:
            return
        name = self.selected_playlist_name()
        self._playlist_name = name
        self.playlistChanged.emit(name)
