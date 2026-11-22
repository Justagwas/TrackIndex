from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPointF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QLayout, QLineEdit, QSizePolicy

from .footer import FooterButton, describe_footer_button
from .theme import ThemePalette


def build_find_icon(name: str, color: str, size: int) -> QIcon:
    screen = QGuiApplication.primaryScreen()
    dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24.0, size / 24.0)
    painter.setPen(QPen(QColor(color), 2.0, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == "search":
        painter.drawEllipse(QPointF(10.0, 10.0), 6.0, 6.0)
        painter.drawLine(QPointF(14.5, 14.5), QPointF(20.0, 20.0))
    elif name == "close":
        painter.drawLine(QPointF(6.0, 6.0), QPointF(18.0, 18.0))
        painter.drawLine(QPointF(18.0, 6.0), QPointF(6.0, 18.0))
    elif name in {"previous", "next"}:
        point_y, ends_y = (8.0, 15.0) if name == "previous" else (16.0, 9.0)
        painter.drawLine(QPointF(6.0, ends_y), QPointF(12.0, point_y))
        painter.drawLine(QPointF(12.0, point_y), QPointF(18.0, ends_y))
    else:
        painter.end()
        raise ValueError(f"Unknown find icon: {name}")
    painter.end()
    return QIcon(pixmap)


class _FindInput(QLineEdit):
    navigateRequested = Signal(int)

    def keyPressEvent(self, event) -> None:
        if (event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
                and not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            self.navigateRequested.emit(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class FindBar(QFrame):
    queryChanged = Signal(str)
    navigateRequested = Signal(int)
    closeRequested = Signal()
    resizedDuringAnimation = Signal()
    expansionFinished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("findBar")
        self.setAccessibleName("Find tracks")
        self.row = QGridLayout(self)
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.row.setColumnStretch(0, 1)
        self._scale = 1.0
        self._compact = False
        self._expanded = False
        self._height_animation = QVariantAnimation(self)
        self._height_animation.valueChanged.connect(self._animate_height)
        self._height_animation.finished.connect(self._animation_finished)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._sync_expanded_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.input = _FindInput(self)
        self.input.setObjectName("findInput")
        self.input.setPlaceholderText("Find tracks...")
        self.input.setAccessibleName("Find tracks in this playlist")
        self.input.setToolTip("Search filenames, titles, artists and albums. Enter: next match. Shift+Enter: previous match.")
        self.input.setClearButtonEnabled(True)
        self.input.setMinimumWidth(0)
        self.input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.input.textChanged.connect(self.queryChanged.emit)
        self.input.navigateRequested.connect(self.navigateRequested.emit)
        self.count = QLabel("", self)
        self.count.setObjectName("findCount")
        self.count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.count.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.previous_button = FooterButton("", self)
        self.next_button = FooterButton("", self)
        self.close_button = FooterButton("", self)
        for button, name, description in (
            (self.previous_button, "Previous match", "Previous match (Shift+Enter)"),
            (self.next_button, "Next match", "Next match (Enter)"),
            (self.close_button, "Close Find", "Close Find (Escape)"),
        ):
            button.setObjectName("findIcon")
            describe_footer_button(button, name, description)
        self.previous_button.clicked.connect(lambda: self.navigateRequested.emit(-1))
        self.next_button.clicked.connect(lambda: self.navigateRequested.emit(1))
        self.close_button.clicked.connect(self.closeRequested.emit)
        for column, widget in enumerate(self._controls()):
            self.row.addWidget(widget, 0, column)
        self._close_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._close_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._close_shortcut.activated.connect(self.closeRequested.emit)
        self.set_results(0, 0)
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)
        self.hide()

    @property
    def query(self) -> str:
        return self.input.text()

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        self._expanded = bool(expanded)
        self._height_timer.stop()
        if self._expanded:
            self.show()
            parent_layout = self.parentWidget().layout() if self.parentWidget() is not None else None
            if parent_layout is not None:
                parent_layout.activate()
            self._sync_layout()
        self._animate_to(self.sizeHint().height() if self._expanded else 0, animate=animate)

    def _animate_to(self, target: int, *, animate: bool = True) -> None:
        target = max(0, target)
        if (animate and self._height_animation.state() == QAbstractAnimation.State.Running
                and self._height_animation.endValue() == target):
            return
        current = self.height() if not self.isHidden() else 0
        self._height_animation.stop()
        if not animate or current == target:
            self._animate_height(target)
            self._animation_finished()
            return
        self._height_animation.setStartValue(current)
        self._height_animation.setEndValue(target)
        self._height_animation.setDuration(190 if target > current else 145)
        self._height_animation.setEasingCurve(QEasingCurve.Type.OutCubic if target > current else QEasingCurve.Type.InOutCubic)
        self._height_animation.start()

    def _animate_height(self, value) -> None:
        height = max(0, round(float(value)))
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.resizedDuringAnimation.emit()

    def _animation_finished(self) -> None:
        if not self._expanded:
            self.hide()
        else:
            self.expansionFinished.emit()

    def _sync_expanded_height(self) -> None:
        if self._expanded:
            self._animate_to(self.sizeHint().height())

    def set_results(self, current: int, total: int, *, pending: bool = False) -> None:
        total = max(0, total)
        current = max(0, min(current, total))
        if not self.query.strip():
            text = ""
        elif total:
            text = f"{current} of {total}" if current else f"{total} matches"
            if pending:
                text += "+"
        else:
            text = "Searching..." if pending else "No matches"
        self.count.setText(text)
        self.count.setAccessibleName(text or "Enter text to find tracks")
        self.count.setToolTip("Still checking track information." if pending else "")
        self.previous_button.setEnabled(total > 0)
        self.next_button.setEnabled(total > 0)
        self._sync_layout()

    def _controls(self):
        return self.input, self.count, self.previous_button, self.next_button, self.close_button

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_layout()

    def _sync_layout(self) -> None:
        margins = self.row.contentsMargins()
        required = (round(160 * self._scale) + self.count.sizeHint().width()
                    + sum(button.width() for button in (self.previous_button, self.next_button, self.close_button))
                    + self.row.horizontalSpacing() * 4 + margins.left() + margins.right())
        compact = self.width() < required
        if compact == self._compact:
            return
        self._compact = compact
        for widget in self._controls():
            self.row.removeWidget(widget)
        if compact:
            self.row.addWidget(self.input, 0, 0, 1, 5)
            self.row.addWidget(self.count, 1, 0, 1, 2)
            for column, button in enumerate((self.previous_button, self.next_button, self.close_button), 2):
                self.row.addWidget(button, 1, column)
        else:
            for column, widget in enumerate(self._controls()):
                self.row.addWidget(widget, 0, column)
        self.count.setAlignment((Qt.AlignmentFlag.AlignLeft if compact else Qt.AlignmentFlag.AlignRight)
                                | Qt.AlignmentFlag.AlignVCenter)
        self.updateGeometry()
        if self._expanded:
            self._height_timer.start(0)

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        old_scale = self._scale
        self._scale = scale
        px = lambda value: max(1, round(value * scale))
        self.row.setContentsMargins(px(10), px(8), px(10), px(8))
        self.row.setSpacing(px(6))
        size = px(28)
        icon_size = px(16)
        for button, name in ((self.previous_button, "previous"), (self.next_button, "next"),
                             (self.close_button, "close")):
            button.setFixedSize(size, size)
            button.setIconSize(QSize(icon_size, icon_size))
            button.setIcon(build_find_icon(name, theme.text_primary, icon_size))
        self.input.setMinimumHeight(px(28))
        self._sync_layout()
        if scale != old_scale:
            self._height_timer.stop()
            self._animate_to(self.sizeHint().height() if self._expanded else 0, animate=False)
