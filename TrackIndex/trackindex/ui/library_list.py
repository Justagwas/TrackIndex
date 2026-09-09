from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QWidget,
)

from trackindex.core.models import LibraryRecord

from .interaction import apply_media_crate_scrollbar
from .theme import ThemePalette

LIBRARY_ROLE = Qt.ItemDataRole.UserRole + 1
AVAILABLE_ROLE = Qt.ItemDataRole.UserRole + 2


class LibraryDelegate(QStyledItemDelegate):
    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(parent)
        self.theme, self.scale = theme, scale

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        self.theme, self.scale = theme, scale

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), max(52, round(58 * self.scale)))

    def _paint_record(
        self,
        painter: QPainter,
        rect: QRect,
        record: LibraryRecord,
        available: bool,
        *,
        selected: bool,
        hovered: bool,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = rect.adjusted(3, 2, -3, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            QColor(
                self.theme.selection_bg
                if selected
                else (self.theme.disabled_bg if hovered else self.theme.panel_bg)
            )
        )
        painter.drawRoundedRect(card, 7, 7)
        if selected:
            painter.setBrush(QColor(self.theme.accent))
            painter.drawRoundedRect(
                QRect(card.left(), card.top(), 4, card.height()), 2, 2
            )
        left = card.left() + 11
        width = card.width() - 20
        title_font = QFont("Segoe UI", max(8, round(9 * self.scale)))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(
            QColor(self.theme.text_primary if available else self.theme.disabled_fg)
        )
        painter.drawText(
            QRect(left, card.top() + 6, width, card.height() // 2),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(record.name, Qt.TextElideMode.ElideRight, width),
        )
        sub_font = QFont("Segoe UI", max(7, round(7.5 * self.scale)))
        painter.setFont(sub_font)
        painter.setPen(
            QColor(self.theme.text_secondary if available else self.theme.danger)
        )
        subtitle = str(record.folder) if available else f"Unavailable - {record.folder}"
        painter.drawText(
            QRect(left, card.center().y(), width, card.height() // 2 - 5),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideMiddle, width),
        )
        painter.restore()

    def paint(self, painter: QPainter, option, index) -> None:
        record: LibraryRecord = index.data(LIBRARY_ROLE)
        view = self.parent()
        rect = QRect(option.rect)
        if isinstance(view, LibraryList) and view.drag_active:
            if record.library_id == view.dragged_library_id:
                return
            rect.moveTop(round(view.animated_item_top(record.library_id, rect.top())))
        self._paint_record(
            painter,
            rect,
            record,
            bool(index.data(AVAILABLE_ROLE)),
            selected=bool(option.state & QStyle.StateFlag.State_Selected),
            hovered=bool(option.state & QStyle.StateFlag.State_MouseOver),
        )

    def render_row(
        self,
        record: LibraryRecord,
        available: bool,
        width: int,
        height: int,
        *,
        selected: bool,
    ) -> QPixmap:
        view = self.parent()
        dpr = view.devicePixelRatioF() if isinstance(view, QListWidget) else 1.0
        pixmap = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self._paint_record(
            painter,
            QRect(0, 0, width, height),
            record,
            available,
            selected=selected,
            hovered=False,
        )
        painter.end()
        return pixmap


@dataclass(slots=True)
class _LibraryDrag:
    before: tuple[str, ...]
    dragged_id: str
    remaining: tuple[str, ...]
    pointer: QPointF
    hotspot_y: float
    insertion: int
    snapshot: QPixmap
    original_row: int
    phase: str = "dragging"
    phase_started: float = field(default_factory=monotonic)
    lift_started: float = field(default_factory=monotonic)
    reflow_started: float = field(default_factory=monotonic)
    row_from: dict[str, float] = field(default_factory=dict)
    row_targets: dict[str, float] = field(default_factory=dict)
    overlay_from_y: float = 0.0
    overlay_target_y: float = 0.0

    @property
    def after(self) -> tuple[str, ...]:
        return (
            *self.remaining[: self.insertion],
            self.dragged_id,
            *self.remaining[self.insertion :],
        )


class LibraryList(QListWidget):
    libraryActivated = Signal(str)
    libraryMenuRequested = Signal(str, object)
    librariesReordered = Signal(object)

    LIFT_DURATION = 0.10
    REFLOW_DURATION = 0.14
    SETTLE_DURATION = 0.16
    CANCEL_DURATION = 0.14

    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(parent)


        self._press_position: QPointF | None = None
        self._press_item: QListWidgetItem | None = None
        self._drag: _LibraryDrag | None = None
        self._releasing_mouse = False
        self._filtered_window: QWidget | None = None
        self._delegate = LibraryDelegate(theme, scale, self)
        self.setItemDelegate(self._delegate)
        self.setProperty("trackindexRowPointerOnly", True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setDragEnabled(False)
        apply_media_crate_scrollbar(self.verticalScrollBar())
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.currentItemChanged.connect(self._activated)
        self.customContextMenuRequested.connect(self._menu)

        self._animation_timer = QTimer(self)
        self._animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._animation_timer.setInterval(16)
        self._animation_timer.timeout.connect(self._animation_tick)

    @staticmethod
    def _out_cubic(progress: float) -> float:
        progress = max(0.0, min(1.0, progress))
        return 1.0 - (1.0 - progress) ** 3

    @staticmethod
    def _lerp(start: float, end: float, progress: float) -> float:
        return start + (end - start) * progress

    @property
    def drag_active(self) -> bool:
        return self._drag is not None

    @property
    def dragged_library_id(self) -> str:
        return self._drag.dragged_id if self._drag is not None else ""

    def _row_height(self) -> int:
        if self.count():
            height = self.sizeHintForRow(0)
            if height > 0:
                return height
        return max(52, round(58 * self._delegate.scale))

    def _content_origin(self) -> float:

        if not self.count():
            return 0.0
        return float(
            self.visualItemRect(self.item(0)).top()
            + self.verticalScrollBar().value()
        )

    def _ordered_ids(self) -> tuple[str, ...]:
        return tuple(
            self.item(row).data(LIBRARY_ROLE).library_id
            for row in range(self.count())
        )

    def _item_for_id(self, library_id: str) -> QListWidgetItem | None:
        for row in range(self.count()):
            item = self.item(row)
            if item.data(LIBRARY_ROLE).library_id == library_id:
                return item
        return None

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        self._cancel_drag(immediate=True)
        self._delegate.set_appearance(theme, scale)
        self.viewport().update()

    def set_libraries(
        self, libraries: list[LibraryRecord], selected_id: str = ""
    ) -> None:
        self._cancel_drag(immediate=True)
        previous = self.blockSignals(True)
        self.clear()
        chosen = None
        for record in libraries:
            item = QListWidgetItem()
            item.setData(LIBRARY_ROLE, record)
            item.setData(AVAILABLE_ROLE, record.folder.is_dir())
            item.setToolTip(str(record.folder))
            self.addItem(item)
            if record.library_id == selected_id:
                chosen = item
        if chosen:
            self.setCurrentItem(chosen)
        self.blockSignals(previous)
        self._sync_pointer_cursor(None)

    def select_library(self, library_id: str) -> None:
        item = self._item_for_id(library_id)
        if item is not None:
            self.setCurrentItem(item)

    def _activated(self, item, _previous) -> None:
        if item:
            self.libraryActivated.emit(item.data(LIBRARY_ROLE).library_id)

    def _menu(self, point) -> None:
        item = self.itemAt(point)
        if item:
            self.libraryMenuRequested.emit(
                item.data(LIBRARY_ROLE).library_id,
                self.viewport().mapToGlobal(point),
            )

    def _sync_pointer_cursor(self, point) -> None:
        if self._drag is not None:
            return
        item = self.itemAt(point) if point is not None else None
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor
            if self.isEnabled() and item is not None
            else Qt.CursorShape.ArrowCursor
        )

    def _sync_pointer_from_global_position(self) -> None:
        point = self.viewport().mapFromGlobal(QCursor.pos())
        self._sync_pointer_cursor(point if self.viewport().rect().contains(point) else None)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is None:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._press_position = QPointF(event.position())
                self._press_item = item
                event.accept()
                return
        self._clear_press()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            if self._drag.phase == "dragging":
                self._drag.pointer = QPointF(event.position())
                self._update_drag_destination()
            event.accept()
            return
        self._sync_pointer_cursor(event.position().toPoint())
        if (
            self._press_position is not None
            and self._press_item is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position() - self._press_position).manhattanLength()
            >= QApplication.startDragDistance()
            and self._begin_drag(event.position())
        ):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            if (
                self.viewport().rect().contains(event.position().toPoint())
                and self._drag.phase == "dragging"
            ):
                self._settle_drag()
            else:
                self._cancel_drag()
            self._clear_press()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_item is not None:
            item = self.itemAt(event.position().toPoint())
            if item is self._press_item:
                self.setCurrentItem(item)
            self._clear_press()
            event.accept()
            return
        self._clear_press()
        super().mouseReleaseEvent(event)

    def _clear_press(self) -> None:
        self._press_position = None
        self._press_item = None

    def _begin_drag(self, pointer: QPointF) -> bool:
        if not self.isEnabled() or self._press_item is None or self.count() < 2:
            return False
        record: LibraryRecord = self._press_item.data(LIBRARY_ROLE)
        original_row = self.row(self._press_item)
        row_rect = self.visualItemRect(self._press_item)
        row_height = self._row_height()
        press_position = self._press_position
        if press_position is None:
            return False
        hotspot = max(
            0.0,
            min(float(row_height), press_position.y() - row_rect.top()),
        )
        before = self._ordered_ids()
        remaining = tuple(item for item in before if item != record.library_id)
        positions = {
            library_id: self._content_origin() + row * row_height
            for row, library_id in enumerate(before)
            if library_id != record.library_id
        }
        snapshot = self._delegate.render_row(
            record,
            bool(self._press_item.data(AVAILABLE_ROLE)),
            self.viewport().width(),
            row_height,
            selected=self.currentItem() is self._press_item,
        )
        now = monotonic()
        self._drag = _LibraryDrag(
            before,
            record.library_id,
            remaining,
            QPointF(pointer),
            hotspot,
            max(0, min(len(remaining), original_row)),
            snapshot,
            original_row,
            phase_started=now,
            lift_started=now,
            reflow_started=now,
            row_from=positions.copy(),
            row_targets=positions.copy(),
        )
        self.viewport().grabMouse()
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self._update_drag_destination(force=True)
        self._animation_timer.start()
        self.viewport().update()
        return True

    def _current_reflow_positions(self, now: float | None = None) -> dict[str, float]:
        session = self._drag
        if session is None:
            return {}
        now = monotonic() if now is None else now
        progress = self._out_cubic(
            (now - session.reflow_started) / self.REFLOW_DURATION
        )
        return {
            library_id: self._lerp(
                session.row_from.get(library_id, target), target, progress
            )
            for library_id, target in session.row_targets.items()
        }

    def _set_reflow_targets(self, order: tuple[str, ...]) -> None:
        session = self._drag
        if session is None:
            return
        now = monotonic()
        current = self._current_reflow_positions(now)
        row_height = self._row_height()
        origin = self._content_origin()
        targets = {
            library_id: origin + row * row_height
            for row, library_id in enumerate(order)
            if library_id != session.dragged_id
        }
        session.row_from = current
        session.row_targets = targets
        session.reflow_started = now

    def animated_item_top(self, library_id: str, fallback: int) -> float:
        session = self._drag
        if session is None:
            return float(fallback)
        content_y = self._current_reflow_positions().get(
            library_id,
            float(fallback + self.verticalScrollBar().value()),
        )
        return content_y - self.verticalScrollBar().value()

    def _update_drag_destination(self, *, force: bool = False) -> None:
        session = self._drag
        if session is None or session.phase != "dragging":
            return
        row_height = self._row_height()
        content_top = (
            session.pointer.y()
            + self.verticalScrollBar().value()
            - session.hotspot_y
        )
        insertion = int(
            (content_top - self._content_origin() + row_height / 2) // row_height
        )
        insertion = max(0, min(len(session.remaining), insertion))
        if force or insertion != session.insertion:
            session.insertion = insertion
            self._set_reflow_targets(session.after)
        self.viewport().update()

    def _settle_drag(self) -> None:
        session = self._drag
        if session is None:
            return
        if session.after == session.before:
            self._cancel_drag()
            return
        session.phase = "settling"
        session.phase_started = monotonic()
        session.overlay_from_y = session.pointer.y() - session.hotspot_y
        session.overlay_target_y = (
            self._content_origin()
            + session.insertion * self._row_height()
            - self.verticalScrollBar().value()
        )
        self._release_drag_mouse()

    def _cancel_drag(self, *, immediate: bool = False) -> None:
        session = self._drag
        if session is None:
            return
        self._release_drag_mouse()
        if immediate:
            self._drag = None
            self._animation_timer.stop()
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self.viewport().update()
            return
        if session.phase == "cancelling":
            return
        session.phase = "cancelling"
        session.phase_started = monotonic()
        session.overlay_from_y = session.pointer.y() - session.hotspot_y
        session.overlay_target_y = (
            self._content_origin()
            + session.original_row * self._row_height()
            - self.verticalScrollBar().value()
        )
        self._set_reflow_targets(session.before)
        self._animation_timer.start()

    def _release_drag_mouse(self) -> None:
        if self.viewport().mouseGrabber() is self.viewport():
            self._releasing_mouse = True
            self.viewport().releaseMouse()
            self._releasing_mouse = False
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def _overlay_y(self, now: float) -> float:
        session = self._drag
        if session is None:
            return 0.0
        if session.phase == "dragging":
            return session.pointer.y() - session.hotspot_y
        duration = (
            self.SETTLE_DURATION
            if session.phase == "settling"
            else self.CANCEL_DURATION
        )
        progress = self._out_cubic((now - session.phase_started) / duration)
        return self._lerp(
            session.overlay_from_y, session.overlay_target_y, progress
        )

    def _edge_scroll(self) -> None:
        session = self._drag
        if session is None or session.phase != "dragging":
            return
        band = max(28.0, 42.0 * self._delegate.scale)
        y = session.pointer.y()
        speed = 0.0
        if y < band:
            speed = -max(
                1.0, (band - y) / band * 12.0 * self._delegate.scale
            )
        elif y > self.viewport().height() - band:
            distance = y - (self.viewport().height() - band)
            speed = max(1.0, distance / band * 12.0 * self._delegate.scale)
        if not speed:
            return
        bar = self.verticalScrollBar()
        previous = bar.value()
        bar.setValue(previous + round(speed))
        if bar.value() != previous:
            self._update_drag_destination(force=True)

    def _commit_drag(self, session: _LibraryDrag) -> None:
        old_row = session.before.index(session.dragged_id)
        new_row = session.after.index(session.dragged_id)
        selected_id = (
            self.currentItem().data(LIBRARY_ROLE).library_id
            if self.currentItem() is not None
            else ""
        )
        previous = self.blockSignals(True)
        item = self.takeItem(old_row)
        self.insertItem(new_row, item)
        selected = self._item_for_id(selected_id)
        if selected is not None:
            self.setCurrentItem(selected)
        self.blockSignals(previous)
        self.librariesReordered.emit(session.after)

    def _animation_tick(self) -> None:
        session = self._drag
        if session is None:
            self._animation_timer.stop()
            return
        now = monotonic()
        self._edge_scroll()
        if session.phase == "settling" and (
            now - session.phase_started >= self.SETTLE_DURATION
        ):
            self._commit_drag(session)
            self._drag = None
            self._animation_timer.stop()
            self._sync_pointer_from_global_position()
        elif session.phase == "cancelling" and (
            now - session.phase_started >= self.CANCEL_DURATION
        ):
            self._drag = None
            self._animation_timer.stop()
            self._sync_pointer_from_global_position()
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        session = self._drag
        if session is None:
            return
        now = monotonic()
        y = self._overlay_y(now)
        lift = min(1.0, (now - session.lift_started) / self.LIFT_DURATION)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow = QRectF(
            5,
            y + 3 + 2 * lift,
            max(1, self.viewport().width() - 10),
            max(1, self._row_height() - 4),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, round(70 * lift)))
        painter.drawRoundedRect(shadow, 8, 8)
        painter.setOpacity(0.96 + 0.04 * lift)
        painter.drawPixmap(0, round(y), session.snapshot)
        painter.end()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._drag is None or self._drag.phase != "dragging":
            super().wheelEvent(event)
            return
        delta = event.pixelDelta().y()
        if not delta:
            delta = round(event.angleDelta().y() / 120 * self._row_height())
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - delta)
        self._update_drag_destination(force=True)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self._cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.MouseMove and self._drag is None:
            self._sync_pointer_cursor(event.position().toPoint())
        elif event.type() == QEvent.Type.Leave:
            self._sync_pointer_cursor(None)
        elif (
            event.type() == QEvent.Type.UngrabMouse
            and self._drag is not None
            and not self._releasing_mouse
        ):
            self._cancel_drag()
        return super().viewportEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._cancel_drag(immediate=True)
            self._sync_pointer_cursor(None)
        super().changeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.window()
        if window is not self._filtered_window:
            if self._filtered_window is not None:
                self._filtered_window.removeEventFilter(self)
            window.installEventFilter(self)
            self._filtered_window = window

    def eventFilter(self, watched, event) -> bool:
        if watched is self._filtered_window and event.type() == QEvent.Type.WindowDeactivate:
            self._cancel_drag()
        return super().eventFilter(watched, event)
