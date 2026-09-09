from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from trackindex.core.config import SUPPORTED_AUDIO_EXTENSIONS
from trackindex.core.models import TrackMetadata, TrackRecord

from .interaction import apply_media_crate_scrollbar
from .theme import ThemePalette

TRACK_ROLE = Qt.ItemDataRole.UserRole + 1
METADATA_ROLE = Qt.ItemDataRole.UserRole + 2
EMPTY_MODEL_INDEX = QModelIndex()


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    value = round(seconds)
    return f"{value // 60}:{value % 60:02d}"


class TrackTableModel(QAbstractTableModel):
    orderChanged = Signal(object, object, int)
    orderLayoutChanged = Signal(object, object)
    HEADERS = ("#", "Title", "Length")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tracks: list[TrackRecord] = []
        self.metadata: dict[str, TrackMetadata] = {}
        self._row_by_id: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = EMPTY_MODEL_INDEX) -> int:
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = EMPTY_MODEL_INDEX) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.tracks):
            return None
        track = self.tracks[index.row()]
        metadata = self.metadata.get(track.track_id)
        if role == TRACK_ROLE:
            return track
        if role == METADATA_ROLE:
            return metadata
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return index.row() + 1
            if index.column() == 1:
                return metadata.title if metadata else track.base_stem
            return format_duration(metadata.duration_seconds if metadata else None)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (0, 2):
            return Qt.AlignmentFlag.AlignCenter
        return None

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable if index.isValid() else Qt.ItemFlag.NoItemFlags

    def set_tracks(self, tracks: list[TrackRecord]) -> None:
        self.beginResetModel()
        self.tracks = list(tracks)
        self._rebuild_row_index()
        valid = {item.track_id for item in tracks}
        self.metadata = {key: value for key, value in self.metadata.items() if key in valid}
        self.endResetModel()

    def reconcile_tracks(self, tracks: list[TrackRecord]) -> bool:


        incoming = {track.track_id: track for track in tracks}
        if len(incoming) != len(tracks):
            self.set_tracks(tracks)
            return False

        incoming_ids = set(incoming)
        removed_rows = [row for row, track in enumerate(self.tracks) if track.track_id not in incoming_ids]
        removed_ranges: list[tuple[int, int]] = []
        if removed_rows:
            start = end = removed_rows[0]
            for row in removed_rows[1:]:
                if row == end + 1:
                    end = row
                else:
                    removed_ranges.append((start, end))
                    start = end = row
            removed_ranges.append((start, end))
        for start, end in reversed(removed_ranges):
            self.beginRemoveRows(EMPTY_MODEL_INDEX, start, end)
            removed = self.tracks[start : end + 1]
            del self.tracks[start : end + 1]
            for track in removed:
                self.metadata.pop(track.track_id, None)
            self.endRemoveRows()
        self._rebuild_row_index()

        added = [track for track in tracks if track.track_id not in self._row_by_id]
        if added:
            first = len(self.tracks)
            self.beginInsertRows(EMPTY_MODEL_INDEX, first, first + len(added) - 1)
            self.tracks.extend(added)
            self.endInsertRows()
            self._rebuild_row_index()

        order = tuple(track.track_id for track in tracks)
        self._apply_order(order)
        self.tracks = [incoming[track_id] for track_id in order]
        self._rebuild_row_index()
        if self.tracks:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.tracks) - 1, self.columnCount() - 1),
                [Qt.ItemDataRole.DisplayRole, TRACK_ROLE, METADATA_ROLE],
            )
        return True

    def set_metadata(self, track_id: str, value: TrackMetadata) -> None:
        self.set_metadata_batch({track_id: value})

    def set_metadata_batch(self, values: dict[str, TrackMetadata]) -> None:
        changed_rows: list[int] = []
        for track_id, value in values.items():
            row = self._row_by_id.get(track_id)
            if row is None:
                continue
            self.metadata[track_id] = value
            changed_rows.append(row)
        if not changed_rows:
            return
        rows = sorted(set(changed_rows))
        start = previous = rows[0]
        for row in rows[1:]:
            if row != previous + 1:
                self.dataChanged.emit(self.index(start, 1), self.index(previous, 2))
                start = row
            previous = row
        self.dataChanged.emit(self.index(start, 1), self.index(previous, 2))

    def _rebuild_row_index(self) -> None:
        self._row_by_id = {track.track_id: row for row, track in enumerate(self.tracks)}

    def ordered_track_ids(self) -> tuple[str, ...]:
        return tuple(item.track_id for item in self.tracks)

    @staticmethod
    def calculate_grouped_order(
        order: tuple[str, ...], selected_ids: tuple[str, ...], destination: int
    ) -> tuple[str, ...]:


        selected = set(selected_ids)
        moving = [track_id for track_id in order if track_id in selected]
        if not moving:
            return order
        destination = max(0, min(len(order), destination))
        offset = sum(track_id in selected for track_id in order[:destination])
        remaining = [track_id for track_id in order if track_id not in selected]
        insertion = max(0, min(len(remaining), destination - offset))
        return tuple(remaining[:insertion] + moving + remaining[insertion:])

    @staticmethod
    def group_at_insertion(order: tuple[str, ...], selected_ids: tuple[str, ...], insertion: int) -> tuple[str, ...]:


        selected = set(selected_ids)
        moving = [track_id for track_id in order if track_id in selected]
        remaining = [track_id for track_id in order if track_id not in selected]
        insertion = max(0, min(len(remaining), insertion))
        return tuple(remaining[:insertion] + moving + remaining[insertion:])

    def _apply_order(
        self,
        after: tuple[str, ...],
        *,
        moved_count: int = 0,
        emit_reorder: bool = False,
    ) -> bool:
        before = self.ordered_track_ids()
        if after == before:
            return False
        by_id = {item.track_id: item for item in self.tracks}
        if len(after) != len(before) or set(after) != set(by_id):
            return False


        persistent = tuple(self.persistentIndexList())
        old_indexes = [QModelIndex(index) for index in persistent]
        new_row_by_id = {track_id: row for row, track_id in enumerate(after)}
        new_indexes = [
            self.index(new_row_by_id[before[index.row()]], index.column())
            for index in persistent
        ]
        self.layoutAboutToBeChanged.emit()
        self.tracks = [by_id[track_id] for track_id in after]
        self._rebuild_row_index()
        self.changePersistentIndexList(old_indexes, new_indexes)
        self.layoutChanged.emit()
        self.orderLayoutChanged.emit(before, after)
        if emit_reorder:
            self.orderChanged.emit(before, after, moved_count)
        return True

    def apply_reorder(self, after: tuple[str, ...], moved_count: int) -> bool:
        return self._apply_order(after, moved_count=moved_count, emit_reorder=True)

    def reorder(self, selected_ids: tuple[str, ...], destination: int) -> bool:
        before = self.ordered_track_ids()
        after = self.calculate_grouped_order(before, selected_ids, destination)
        selected = set(selected_ids)
        moved_count = sum(track_id in selected for track_id in before)
        return self._apply_order(after, moved_count=moved_count, emit_reorder=True)

    def restore_order(self, order: tuple[str, ...]) -> None:
        self._apply_order(order)


class TrackHeaderView(QHeaderView):


    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.theme = theme
        self.scale = scale
        self.setSectionsClickable(False)
        self.setHighlightSections(False)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _scaled(value: int, scale: float, minimum: int = 1) -> int:
        return max(minimum, round(value * scale))

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        self.theme = theme
        self.scale = scale
        self.setFixedHeight(max(24, round(28 * scale)))
        self.viewport().update()

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:
        if not rect.isValid():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = QFont("Segoe UI")
        font.setPointSizeF(max(7.0, 8.4 * self.scale))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(self.theme.text_secondary))
        label = TrackTableModel.HEADERS[logical_index]
        text_rect, alignment = self.label_geometry(rect, logical_index)
        painter.drawText(text_rect, alignment, label)
        painter.restore()

    def label_geometry(self, rect: QRect, logical_index: int) -> tuple[QRect, Qt.AlignmentFlag]:
        if logical_index == 1:


            left = (
                rect.left() + self._scaled(8, self.scale) + self._scaled(40, self.scale) + self._scaled(10, self.scale)
            )
            text_rect = QRect(left, rect.top(), max(1, rect.right() - left), rect.height())
            alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        else:
            text_rect = rect
            alignment = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        return text_rect, alignment


class TrackDelegate(QStyledItemDelegate):
    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(parent)
        self.theme, self.scale = theme, scale
        self._artwork_cache: OrderedDict[tuple[object, ...], tuple[bytes, QPixmap, QRect]] = OrderedDict()
        self._artwork_cache_limit = 256
        self._card_cache: OrderedDict[tuple[object, ...], QPixmap] = OrderedDict()
        self._card_cache_limit = 32
        self._row_cache: OrderedDict[tuple[object, ...], tuple[QPixmap, int]] = OrderedDict()
        self._row_cache_bytes = 0
        self._row_cache_byte_limit = 64 * 1024 * 1024

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        if abs(self.scale - scale) > 0.001:
            self._artwork_cache.clear()
        self._card_cache.clear()
        self.clear_row_cache()
        self.theme, self.scale = theme, scale

    def clear_artwork_cache(self) -> None:
        self._artwork_cache.clear()

    def clear_card_cache(self) -> None:
        self._card_cache.clear()
        self.clear_row_cache()

    def clear_row_cache(self) -> None:
        self._row_cache.clear()
        self._row_cache_bytes = 0

    def invalidate_track(self, track_id: str) -> None:
        for key in tuple(self._row_cache):
            if key and key[0] == track_id:
                _pixmap, cost = self._row_cache.pop(key)
                self._row_cache_bytes -= cost

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.row_height())

    def _scaled(self, value: int, minimum: int = 1) -> int:
        return max(minimum, round(value * self.scale))

    def _device_pixel_ratio(self) -> float:
        view = self.parent()
        if isinstance(view, QTableView):
            return max(1.0, float(view.viewport().devicePixelRatioF()))
        return 1.0

    def row_height(self) -> int:
        return self._scaled(60, 45)

    def card_rect(self, option) -> QRectF:
        view = self.parent()
        viewport_width = view.viewport().width() if isinstance(view, QTableView) else option.rect.right() + 1
        horizontal_gap = self._scaled(4, 3)
        card_height = min(option.rect.height(), self._scaled(54))
        vertical_gap = (option.rect.height() - card_height) / 2
        return QRectF(
            horizontal_gap,
            option.rect.top() + vertical_gap,
            max(1, viewport_width - horizontal_gap * 2 - 1),
            max(1, card_height),
        )

    def artwork_rect(self, option) -> QRect:
        card = self.card_rect(option)
        size = self._scaled(40, 30)
        left = option.rect.left() + self._scaled(8, 5)
        top = round(card.top() + (card.height() - size) / 2)
        return QRect(left, top, size, size)

    def paint_card(
        self,
        painter: QPainter,
        option,
        index,
        track: TrackRecord,
        hovered: bool | None = None,
    ) -> QRectF:
        card = self.card_rect(option)
        view = self.parent()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if hovered is None:
            hovered = isinstance(view, TrackTableView) and view.hover_row == index.row()
        fill = self.theme.selection_bg if selected else (self.theme.disabled_bg if hovered else self.theme.panel_bg)
        border = self.theme.accent if selected else self.theme.border
        radius = self._scaled(8, 5)
        line_width = self._scaled(1)
        viewport_width = max(1, round(card.right()) + self._scaled(4, 3) + 1)
        row_height = max(1, option.rect.height())
        dpr = self._device_pixel_ratio()
        key = (viewport_width, row_height, fill, border, radius, line_width, round(dpr, 3))
        cached = self._card_cache.get(key)
        if cached is None:
            cached = QPixmap(round(viewport_width * dpr), round(row_height * dpr))
            cached.setDevicePixelRatio(dpr)
            cached.fill(Qt.GlobalColor.transparent)
            cache_painter = QPainter(cached)
            cache_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            cache_painter.setBrush(QColor(fill))
            cache_painter.setPen(QPen(QColor(border), line_width))
            inset = line_width / 2
            local_card = card.translated(0, -option.rect.top())
            cache_painter.drawRoundedRect(local_card.adjusted(inset, inset, -inset, -inset), radius, radius)
            cache_painter.end()
            self._card_cache[key] = cached
            self._card_cache.move_to_end(key)
            while len(self._card_cache) > self._card_cache_limit:
                self._card_cache.popitem(last=False)
        else:
            self._card_cache.move_to_end(key)
        painter.save()
        painter.setClipRect(option.rect)
        painter.drawPixmap(0, option.rect.top(), cached)
        painter.restore()
        return card

    def _artwork_pixmap(self, metadata: TrackMetadata | None, size: int) -> tuple[QPixmap, QRect]:
        artwork = metadata.artwork_bytes if metadata else None
        if not artwork:
            return QPixmap(), QRect()
        dpr = self._device_pixel_ratio()
        artwork_identity = metadata.artwork_key if metadata else ""
        key = (
            artwork_identity or id(artwork),
            len(artwork),
            size,
            round(dpr, 3),
        )
        cached = self._artwork_cache.get(key)
        if cached is not None and (cached[0] is artwork or cached[0] == artwork):
            self._artwork_cache.move_to_end(key)
            return cached[1], cached[2]
        image = QImage.fromData(QByteArray(artwork))
        if image.isNull():
            return QPixmap(), QRect()
        crop_size = min(image.width(), image.height())
        if crop_size <= 0:
            return QPixmap(), QRect()
        crop = QRect(
            (image.width() - crop_size) // 2,
            (image.height() - crop_size) // 2,
            crop_size,
            crop_size,
        )
        physical_size = max(1, round(size * dpr))
        pixmap = QPixmap.fromImage(image.copy(crop)).scaled(
            physical_size, physical_size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap.setDevicePixelRatio(dpr)
        source = QRect(0, 0, size, size)
        self._artwork_cache[key] = (artwork, pixmap, source)
        self._artwork_cache.move_to_end(key)
        while len(self._artwork_cache) > self._artwork_cache_limit:
            self._artwork_cache.popitem(last=False)
        return pixmap, source

    def _paint_artwork(self, painter: QPainter, rect: QRect, metadata: TrackMetadata | None) -> None:
        radius = self._scaled(6, 4)
        pixmap, _source = self._artwork_pixmap(metadata, rect.width())
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rect), radius, radius)
        painter.save()
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        if pixmap.isNull():
            painter.fillRect(rect, QColor(self.theme.disabled_bg))
            disc_size = self._scaled(20, 15)
            disc_rect = QRect(
                rect.center().x() - disc_size // 2,
                rect.center().y() - disc_size // 2,
                disc_size,
                disc_size,
            )
            painter.setPen(QPen(QColor(self.theme.text_secondary), max(1, round(1.5 * self.scale))))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(disc_rect)
            center_size = self._scaled(4, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.theme.text_secondary))
            painter.drawEllipse(rect.center(), center_size, center_size)
        else:
            painter.drawPixmap(rect.topLeft(), pixmap)
        painter.restore()

    def _paint_cell(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        display_number: int,
        hovered: bool,
    ) -> None:
        track: TrackRecord = index.data(TRACK_ROLE)
        metadata: TrackMetadata | None = index.data(METADATA_ROLE)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        card = self.paint_card(painter, option, index, track, hovered)
        if index.column() == 0:
            painter.setPen(QColor(self.theme.text_secondary))
            number_rect = QRect(option.rect.left(), round(card.top()), option.rect.width(), round(card.height()))
            painter.drawText(number_rect, Qt.AlignmentFlag.AlignCenter, str(display_number))
        elif index.column() == 1:
            art_rect = self.artwork_rect(option)
            self._paint_artwork(painter, art_rect, metadata)
            view = self.parent()
            if hovered and isinstance(view, TrackTableView):
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0, 150))
                diameter = self._scaled(24, 18)
                artwork_center = QRectF(art_rect).center()
                circle = QRectF(
                    artwork_center.x() - diameter / 2,
                    artwork_center.y() - diameter / 2,
                    diameter,
                    diameter,
                )
                painter.drawEllipse(circle)
                painter.setBrush(QColor(self.theme.text_primary))
                if view.playing_track_id == track.track_id and view.playback_playing:
                    bar_width = max(2, self._scaled(3, 2))
                    bar_height = self._scaled(10, 8)
                    gap = max(2, self._scaled(2, 1))
                    center = circle.center()
                    painter.drawRoundedRect(
                        QRectF(
                            center.x() - gap / 2 - bar_width,
                            center.y() - bar_height / 2,
                            bar_width,
                            bar_height,
                        ),
                        1,
                        1,
                    )
                    painter.drawRoundedRect(
                        QRectF(center.x() + gap / 2, center.y() - bar_height / 2, bar_width, bar_height),
                        1,
                        1,
                    )
                else:
                    triangle = QPainterPath()
                    center = circle.center()
                    back = self._scaled(4, 3)
                    tip = back * 2
                    half_height = self._scaled(6, 4)
                    triangle.moveTo(center.x() - back, center.y() - half_height)
                    triangle.lineTo(center.x() + tip, center.y())
                    triangle.lineTo(center.x() - back, center.y() + half_height)
                    triangle.closeSubpath()
                    painter.drawPath(triangle)
                painter.restore()
            text_left = art_rect.right() + self._scaled(10, 6)
            title = metadata.title if metadata else track.base_stem
            subtitle = " | ".join(value for value in ((metadata.artist, metadata.album) if metadata else ()) if value)
            font = QFont("Segoe UI")
            font.setPointSizeF(max(8.0, 9.0 * self.scale))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(self.theme.text_primary))
            text_width = max(1, option.rect.right() - text_left - self._scaled(8, 5))
            if subtitle:
                title_rect = QRect(text_left, round(card.top()) + self._scaled(3), text_width, round(card.height() / 2))
            else:
                title_rect = QRect(text_left, round(card.top()), text_width, round(card.height()))
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
            )
            if subtitle:
                font.setBold(False)
                font.setPointSizeF(max(7.0, 8.0 * self.scale))
                painter.setFont(font)
                painter.setPen(QColor(self.theme.text_secondary))
                sub_rect = QRect(
                    text_left,
                    round(card.center().y()),
                    text_width,
                    max(1, round(card.height() / 2) - self._scaled(3)),
                )
                painter.drawText(
                    sub_rect,
                    Qt.AlignmentFlag.AlignVCenter,
                    painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, sub_rect.width()),
                )
        else:
            painter.setPen(QColor(self.theme.text_secondary))
            length_rect = QRect(option.rect.left(), round(card.top()), option.rect.width(), round(card.height()))
            painter.drawText(
                length_rect,
                Qt.AlignmentFlag.AlignCenter,
                format_duration(metadata.duration_seconds if metadata else None),
            )
        painter.restore()

    def _row_cache_key(
        self,
        track: TrackRecord,
        metadata: TrackMetadata | None,
        display_number: int,
        selected: bool,
        hovered: bool,
        width: int,
        sections: tuple[tuple[int, int], ...],
    ) -> tuple[object, ...]:
        metadata_key: tuple[object, ...] = ()
        if metadata is not None:
            metadata_key = (
                metadata.title,
                metadata.artist,
                metadata.album,
                metadata.duration_seconds,
                metadata.artwork_key or id(metadata.artwork_bytes),
                len(metadata.artwork_bytes or b""),
            )
        return (
            track.track_id,
            track.path.name,
            metadata_key,
            display_number,
            selected,
            hovered,
            width,
            sections,
            self.theme.mode,
            round(self.scale, 3),
            round(self._device_pixel_ratio(), 3),
            bool(
                getattr(self.parent(), "playback_playing", False)
                and getattr(self.parent(), "playing_track_id", "") == track.track_id
            ),
        )

    def render_row(
        self,
        row: int,
        display_number: int,
        *,
        selected: bool,
        hovered: bool = False,
    ) -> QPixmap:


        view = self.parent()
        if not isinstance(view, QTableView) or not 0 <= row < view.model().rowCount():
            return QPixmap()
        width = max(1, view.viewport().width())
        height = self.row_height()
        header = view.horizontalHeader()
        sections = tuple(
            (header.sectionViewportPosition(column), header.sectionSize(column))
            for column in range(view.model().columnCount())
        )
        track: TrackRecord = view.model().index(row, 0).data(TRACK_ROLE)
        metadata: TrackMetadata | None = view.model().index(row, 0).data(METADATA_ROLE)
        key = self._row_cache_key(track, metadata, display_number, selected, hovered, width, sections)
        cached = self._row_cache.get(key)
        if cached is not None:
            self._row_cache.move_to_end(key)
            return cached[0]

        dpr = self._device_pixel_ratio()
        pixmap = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        for column, (left, section_width) in enumerate(sections):
            option = QStyleOptionViewItem()
            option.initFrom(view)
            option.rect = QRect(left, 0, section_width, height)
            option.widget = view
            option.state |= QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Active
            if selected:
                option.state |= QStyle.StateFlag.State_Selected
            if hovered:
                option.state |= QStyle.StateFlag.State_MouseOver
            self._paint_cell(painter, option, view.model().index(row, column), display_number, hovered)
        painter.end()
        cost = pixmap.width() * pixmap.height() * 4
        self._row_cache[key] = (pixmap, cost)
        self._row_cache_bytes += cost
        self._row_cache.move_to_end(key)
        while self._row_cache_bytes > self._row_cache_byte_limit and len(self._row_cache) > 1:
            _discarded_key, (_discarded_pixmap, discarded_cost) = self._row_cache.popitem(last=False)
            self._row_cache_bytes -= discarded_cost
        return pixmap

    def paint(self, painter: QPainter, option, index) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        view = self.parent()
        hovered = isinstance(view, TrackTableView) and view.hover_row == index.row()
        row = self.render_row(index.row(), index.row() + 1, selected=selected, hovered=hovered)
        if row.isNull():
            return
        painter.save()
        painter.setClipRect(option.rect)


        painter.drawPixmap(0, option.rect.top(), row)
        painter.restore()


@dataclass(slots=True)
class _DragSession:
    before: tuple[str, ...]
    selected_ids: tuple[str, ...]
    remaining_ids: tuple[str, ...]
    grabbed_id: str
    pointer: QPointF
    press_x: float
    hotspot_y: float
    insertion: int
    phase: str = "dragging"
    phase_started: float = field(default_factory=monotonic)
    lift_started: float = field(default_factory=monotonic)
    reflow_started: float = field(default_factory=monotonic)
    row_from: dict[str, float] = field(default_factory=dict)
    row_targets: dict[str, float] = field(default_factory=dict)
    overlay_from_y: float = 0.0
    overlay_target_y: float = 0.0
    scroll_residual: float = 0.0

    @property
    def after(self) -> tuple[str, ...]:
        return self.remaining_ids[: self.insertion] + self.selected_ids + self.remaining_ids[self.insertion :]


@dataclass(slots=True)
class _OrderTransition:
    after: tuple[str, ...]
    selected_ids: frozenset[str]
    from_y: dict[str, float]
    target_y: dict[str, float]
    started: float = field(default_factory=monotonic)
    duration: float = 0.14


class TrackTableView(QTableView):
    tracksDropped = Signal(object, object, int)
    externalFilesDropped = Signal(object, int)
    deleteTracksRequested = Signal(object)
    visibleRowsChanged = Signal(int, int)
    dragStateChanged = Signal(bool)
    playbackRequested = Signal(str)

    LIFT_DURATION = 0.10
    REFLOW_DURATION = 0.14
    SETTLE_DURATION = 0.16
    CANCEL_DURATION = 0.14

    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("trackTable")
        self.setProperty("trackindexRowPointerOnly", True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.track_model = TrackTableModel(self)
        self.setModel(self.track_model)
        self._delegate = TrackDelegate(theme, scale, self)
        self.setItemDelegate(self._delegate)
        self._hover_row = -1
        self._press_position: QPointF | None = None
        self._press_index = QModelIndex()
        self._deferred_single_selection = False
        self._drag: _DragSession | None = None
        self._order_transition: _OrderTransition | None = None
        self._suppress_next_order_transition = False
        self._releasing_mouse = False
        self._external_drop_insertion = -1
        self._external_drag_active = False
        self.playing_track_id = ""
        self.playback_playing = False
        self._suppress_artwork_activation_once = False
        self._filtered_window: QWidget | None = None
        self._layout_selected_ids: tuple[str, ...] = ()
        self._layout_current_id = ""
        self._animation_timer = QTimer(self)
        self._animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._animation_timer.setInterval(16)
        self._animation_timer.timeout.connect(self._animation_tick)
        self._visible_range_timer = QTimer(self)
        self._visible_range_timer.setSingleShot(True)
        self._visible_range_timer.setInterval(0)
        self._visible_range_timer.timeout.connect(self._emit_visible_rows)

        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setAutoScroll(True)
        self.setDropIndicatorShown(False)
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        apply_media_crate_scrollbar(self.verticalScrollBar())
        self.verticalHeader().hide()
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setDefaultSectionSize(self._delegate.row_height())
        header = TrackHeaderView(theme, scale, self)
        self.setHorizontalHeader(header)
        header.setObjectName("trackHeader")
        header.setFixedHeight(max(24, round(28 * scale)))
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 58)
        self.setColumnWidth(2, 80)
        self.track_model.orderChanged.connect(self.tracksDropped.emit)
        self.track_model.layoutAboutToBeChanged.connect(
            self._capture_layout_selection
        )
        self.track_model.layoutChanged.connect(self._restore_layout_selection)
        self.track_model.orderLayoutChanged.connect(self._model_order_changed)
        self.track_model.modelReset.connect(self._schedule_visible_rows)
        self.track_model.rowsInserted.connect(self._schedule_visible_rows)
        self.verticalScrollBar().valueChanged.connect(self._schedule_visible_rows)

    @property
    def hover_row(self) -> int:
        return self._hover_row

    @property
    def drag_active(self) -> bool:
        return self._drag is not None

    @property
    def preview_order(self) -> tuple[str, ...]:
        return self._drag.after if self._drag is not None else self.track_model.ordered_track_ids()

    @staticmethod
    def _out_cubic(progress: float) -> float:
        progress = max(0.0, min(1.0, progress))
        return 1.0 - (1.0 - progress) ** 3

    @staticmethod
    def _lerp(start: float, end: float, progress: float) -> float:
        return start + (end - start) * progress

    def _scaled(self, value: int, minimum: int = 1) -> int:
        return max(minimum, round(value * self._delegate.scale))

    @staticmethod
    def _external_audio_paths(mime_data) -> tuple[Path, ...]:
        if not mime_data.hasUrls():
            return ()
        paths: list[Path] = []
        seen: set[str] = set()
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            try:
                path = Path(url.toLocalFile())
                key = str(path.absolute()).casefold()
            except (OSError, RuntimeError, ValueError):
                continue
            if key in seen or path.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            seen.add(key)
            paths.append(path)
        return tuple(paths)

    def _external_insertion_at(self, y: float) -> int:
        content_y = y + self.verticalScrollBar().value()
        row_height = self._delegate.row_height()
        return max(
            0,
            min(self.track_model.rowCount(), int((content_y + row_height / 2) // row_height)),
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        paths = self._external_audio_paths(event.mimeData())
        if self.isEnabled() and paths:
            self._external_drop_insertion = self._external_insertion_at(event.position().y())
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            if not self._external_drag_active:
                self._external_drag_active = True
                self.dragStateChanged.emit(True)
            self.viewport().update()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        paths = self._external_audio_paths(event.mimeData())
        if self.isEnabled() and paths:
            self._external_drop_insertion = self._external_insertion_at(event.position().y())
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.viewport().update()
            return
        self._clear_external_drop()
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._clear_external_drop()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._external_audio_paths(event.mimeData())
        insertion = self._external_drop_insertion
        self._clear_external_drop()
        if self.isEnabled() and paths and insertion >= 0:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.externalFilesDropped.emit(paths, insertion)
            return
        event.ignore()

    def _clear_external_drop(self) -> None:
        if self._external_drop_insertion < 0 and not self._external_drag_active:
            return
        self._external_drop_insertion = -1
        if self._external_drag_active:
            self._external_drag_active = False
            self.dragStateChanged.emit(False)
        self.viewport().update()

    def set_playback_state(self, track_id: str, playing: bool) -> None:
        if track_id == self.playing_track_id and bool(playing) == self.playback_playing:
            return
        previous_id = self.playing_track_id
        self.playing_track_id = track_id
        self.playback_playing = bool(playing)
        for changed_id in {previous_id, track_id}:
            if not changed_id:
                continue
            self._delegate.invalidate_track(changed_id)
            row = self.track_model._row_by_id.get(changed_id)
            if row is not None:
                rect = self.visualRect(self.track_model.index(row, 0))
                self.viewport().update(QRect(0, rect.top(), self.viewport().width(), rect.height()))

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        self._cancel_drag(immediate=True)
        self._order_transition = None
        self._delegate.set_appearance(theme, scale)
        self.verticalHeader().setDefaultSectionSize(self._delegate.row_height())
        header = self.horizontalHeader()
        if isinstance(header, TrackHeaderView):
            header.set_appearance(theme, scale)
        else:
            header.setFixedHeight(max(24, round(28 * scale)))
        self.viewport().update()

    def set_tracks(self, tracks: list[TrackRecord]) -> None:
        self._cancel_drag(immediate=True)
        self._order_transition = None
        self._hover_row = -1
        self._delegate.clear_card_cache()
        self.track_model.set_tracks(tracks)
        self._schedule_visible_rows()

    def reconcile_tracks(self, tracks: list[TrackRecord]) -> bool:
        self._cancel_drag(immediate=True)
        self._order_transition = None
        self._hover_row = -1
        self._delegate.clear_card_cache()
        preserved = self.track_model.reconcile_tracks(tracks)
        self.viewport().update()
        return preserved

    def ordered_track_ids(self) -> tuple[str, ...]:
        return self.track_model.ordered_track_ids()

    def visible_row_range(self) -> tuple[int, int]:
        count = self.track_model.rowCount()
        if count <= 0:
            return 0, -1
        row_height = max(1, self.verticalHeader().defaultSectionSize())
        estimated_visible = max(1, self.viewport().height() // row_height + 1)
        first = self.rowAt(0)
        if first < 0:
            first = 0
        last = self.rowAt(max(0, self.viewport().height() - 1))
        if last < 0:
            last = first + estimated_visible - 1
        return max(0, first), min(count - 1, last)

    def _schedule_visible_rows(self, *_args) -> None:
        if not self._visible_range_timer.isActive():
            self._visible_range_timer.start()

    def _emit_visible_rows(self) -> None:
        first, last = self.visible_row_range()
        self.visibleRowsChanged.emit(first, last)

    def _selected_track_ids(self) -> tuple[str, ...]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        return tuple(self.track_model.tracks[row].track_id for row in rows)

    def _capture_layout_selection(self, *_args) -> None:
        self._layout_selected_ids = self._selected_track_ids()
        current = self.selectionModel().currentIndex()
        self._layout_current_id = (
            self.track_model.tracks[current.row()].track_id
            if current.isValid() and 0 <= current.row() < len(self.track_model.tracks)
            else ""
        )

    def _restore_layout_selection(self, *_args) -> None:
        selected = self._layout_selected_ids
        current = self._layout_current_id
        self._layout_selected_ids = ()
        self._layout_current_id = ""
        if selected or current:
            self._select_track_ids(selected, current)

    def _select_track_ids(self, track_ids: tuple[str, ...], current_id: str = "") -> None:
        selected = set(track_ids)
        selection_model = self.selectionModel()
        selection_model.clearSelection()
        for row, track in enumerate(self.track_model.tracks):
            if track.track_id in selected:
                selection_model.select(
                    self.track_model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
        current_row = self.track_model._row_by_id.get(current_id)
        if current_row is not None:
            selection_model.setCurrentIndex(self.track_model.index(current_row, 0), QItemSelectionModel.SelectionFlag.NoUpdate)

    def _set_hover_row(self, row: int) -> None:
        row = row if 0 <= row < self.track_model.rowCount() else -1
        if self._drag is None:
            self.viewport().setCursor(
                Qt.CursorShape.PointingHandCursor
                if row >= 0 and self.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )
        if row == self._hover_row:
            return
        previous = self._hover_row
        self._hover_row = row
        for changed in (previous, row):
            if changed >= 0:
                rect = self.visualRect(self.track_model.index(changed, 0))
                self.viewport().update(QRect(0, rect.top(), self.viewport().width(), rect.height()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._drag is not None:
            super().mousePressEvent(event)
            return
        index = self.indexAt(event.position().toPoint())
        self._press_position = QPointF(event.position())
        self._press_index = index
        self._deferred_single_selection = False
        if not index.isValid():
            super().mousePressEvent(event)
            return

        selected_rows = {item.row() for item in self.selectionModel().selectedRows()}
        no_modifier = event.modifiers() == Qt.KeyboardModifier.NoModifier
        if no_modifier:
            if index.row() not in selected_rows or len(selected_rows) <= 1:


                self.selectionModel().select(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )


            self.selectionModel().setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            self._deferred_single_selection = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            if self._drag.phase == "dragging":
                self._drag.pointer = QPointF(event.position())
                self._update_drag_destination()
            event.accept()
            return

        if (
            self._press_position is not None
            and self._press_index.isValid()
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position() - self._press_position).manhattanLength() >= QApplication.startDragDistance()
            and self._begin_drag(event.position())
        ):
            event.accept()
            return

        index = self.indexAt(event.position().toPoint())
        self._set_hover_row(index.row() if index.isValid() else -1)
        if not self._deferred_single_selection:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            inside = self.viewport().rect().contains(event.position().toPoint())
            if inside and self._drag.phase == "dragging":
                self._settle_drag()
            else:
                self._cancel_drag()
            self._clear_press_state()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._deferred_single_selection and self._press_index.isValid():
            track_id = self.track_model.tracks[self._press_index.row()].track_id
            artwork_hit = self._artwork_hit(self._press_index.row(), event.position().toPoint())
            self.selectionModel().select(
                self._press_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )
            self.selectionModel().setCurrentIndex(self._press_index, QItemSelectionModel.SelectionFlag.NoUpdate)
            self._clear_press_state()
            if artwork_hit and not self._suppress_artwork_activation_once:
                self.playbackRequested.emit(track_id)
            self._suppress_artwork_activation_once = False
            event.accept()
            return
        self._clear_press_state()
        super().mouseReleaseEvent(event)

    def _clear_press_state(self) -> None:
        self._press_position = None
        self._press_index = QModelIndex()
        self._deferred_single_selection = False

    def _artwork_hit(self, row: int, point) -> bool:
        if not 0 <= row < self.track_model.rowCount():
            return False
        index = self.track_model.index(row, 1)
        option = QStyleOptionViewItem()
        option.rect = self.visualRect(index)
        return self._delegate.artwork_rect(option).contains(point)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid() and self.isEnabled():
            if self._artwork_hit(index.row(), event.position().toPoint()):
                self._suppress_artwork_activation_once = True
            else:
                self.playbackRequested.emit(self.track_model.tracks[index.row()].track_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _begin_drag(self, pointer: QPointF) -> bool:
        press_position = self._press_position
        if not self.isEnabled() or not self._press_index.isValid() or press_position is None:
            return False
        before = self.track_model.ordered_track_ids()
        selected_ids = self._selected_track_ids()
        grabbed_id = self.track_model.tracks[self._press_index.row()].track_id
        if grabbed_id not in selected_ids:
            selected_ids = (grabbed_id,)
            self._select_track_ids(selected_ids, grabbed_id)
        selected_set = set(selected_ids)
        remaining = tuple(track_id for track_id in before if track_id not in selected_set)
        selected_position = selected_ids.index(grabbed_id)
        row_rect = self.visualRect(self.track_model.index(self._press_index.row(), 0))
        local_y = max(0.0, min(float(self._delegate.row_height()), press_position.y() - row_rect.top()))
        hotspot = selected_position * self._delegate.row_height() + local_y
        original_positions = {
            track_id: float(row * self._delegate.row_height())
            for row, track_id in enumerate(before)
            if track_id not in selected_set
        }
        insertion = max(0, min(len(remaining), self._press_index.row() - selected_position))
        now = monotonic()
        self._drag = _DragSession(
            before=before,
            selected_ids=selected_ids,
            remaining_ids=remaining,
            grabbed_id=grabbed_id,
            pointer=QPointF(pointer),
            press_x=press_position.x(),
            hotspot_y=hotspot,
            insertion=insertion,
            phase_started=now,
            lift_started=now,
            reflow_started=now,
            row_from=original_positions.copy(),
            row_targets=original_positions.copy(),
        )
        self.dragStateChanged.emit(True)
        self._set_hover_row(-1)
        self.viewport().grabMouse()
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self._update_drag_destination(force=True)
        self._ensure_animation_timer()
        self.viewport().update()
        return True

    def _current_reflow_positions(self, now: float | None = None) -> dict[str, float]:
        session = self._drag
        if session is None:
            return {}
        now = monotonic() if now is None else now
        progress = self._out_cubic((now - session.reflow_started) / self.REFLOW_DURATION)
        return {
            track_id: self._lerp(session.row_from.get(track_id, target), target, progress)
            for track_id, target in session.row_targets.items()
        }

    def _set_reflow_targets(self, after: tuple[str, ...], *, immediate: bool = False) -> None:
        session = self._drag
        if session is None:
            return
        now = monotonic()
        current = self._current_reflow_positions(now)
        selected = set(session.selected_ids)
        targets = {
            track_id: float(row * self._delegate.row_height())
            for row, track_id in enumerate(after)
            if track_id not in selected
        }
        session.row_from = targets.copy() if immediate else current
        session.row_targets = targets
        session.reflow_started = now - self.REFLOW_DURATION if immediate else now

    def _update_drag_destination(self, *, force: bool = False) -> None:
        session = self._drag
        if session is None or session.phase != "dragging":
            return
        row_height = self._delegate.row_height()
        content_top = session.pointer.y() + self.verticalScrollBar().value() - session.hotspot_y
        insertion = int((content_top + row_height / 2) // row_height)
        insertion = max(0, min(len(session.remaining_ids), insertion))
        if force or insertion != session.insertion:
            session.insertion = insertion
            self._set_reflow_targets(session.after)
        self.viewport().update()

    def _settle_drag(self) -> None:
        session = self._drag
        if session is None or session.phase != "dragging":
            return
        if session.after == session.before:
            self._cancel_drag()
            return
        session.phase = "settling"
        session.phase_started = monotonic()
        session.overlay_from_y = session.pointer.y() - session.hotspot_y
        session.overlay_target_y = session.insertion * self._delegate.row_height() - self.verticalScrollBar().value()
        self._release_drag_mouse()
        self._ensure_animation_timer()

    def _cancel_drag(self, *, immediate: bool = False) -> None:
        session = self._drag
        if session is None:
            return
        self._release_drag_mouse()
        if immediate:
            self._drag = None
            self.dragStateChanged.emit(False)
            self._stop_animation_timer_if_idle()
            self.viewport().update()
            return
        if session.phase == "cancelling":
            return
        session.phase = "cancelling"
        session.phase_started = monotonic()
        session.overlay_from_y = session.pointer.y() - session.hotspot_y
        original_targets = tuple(session.before)
        self._set_reflow_targets(original_targets)
        self._ensure_animation_timer()
        self.viewport().update()

    def _release_drag_mouse(self) -> None:
        if self.viewport().mouseGrabber() is self.viewport():
            self._releasing_mouse = True
            self.viewport().releaseMouse()
            self._releasing_mouse = False
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._drag is None or self._drag.phase != "dragging":
            super().wheelEvent(event)
            return
        delta = event.pixelDelta().y()
        if not delta:
            delta = round(event.angleDelta().y() / 120 * self._delegate.row_height() * 1.5)
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - delta)
        self._update_drag_destination(force=True)
        event.accept()

    def _edge_scroll(self) -> None:
        session = self._drag
        if session is None or session.phase != "dragging":
            return
        band = float(self._scaled(56, 36))
        y = session.pointer.y()
        speed = 0.0
        if y < band:
            speed = -self._scaled(18, 10) * min(1.0, max(0.0, (band - y) / band))
        elif y > self.viewport().height() - band:
            speed = self._scaled(18, 10) * min(1.0, max(0.0, (y - (self.viewport().height() - band)) / band))
        if not speed:
            session.scroll_residual = 0.0
            return
        session.scroll_residual += speed
        step = int(session.scroll_residual)
        if not step:
            return
        session.scroll_residual -= step
        bar = self.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + step)
        if bar.value() != before:
            self._update_drag_destination(force=True)

    def _ensure_animation_timer(self) -> None:
        if not self._animation_timer.isActive():
            self._animation_timer.start()

    def _stop_animation_timer_if_idle(self) -> None:
        if self._drag is None and self._order_transition is None:
            self._animation_timer.stop()

    def _animation_tick(self) -> None:
        now = monotonic()
        session = self._drag
        if session is not None:
            if session.phase == "dragging":
                self._edge_scroll()
            elif session.phase == "settling" and now - session.phase_started >= self.SETTLE_DURATION:
                self._finish_drop()
            elif session.phase == "cancelling" and now - session.phase_started >= self.CANCEL_DURATION:
                self._drag = None
                self.dragStateChanged.emit(False)
                self._sync_pointer_from_global_position()
        transition = self._order_transition
        if transition is not None and now - transition.started >= transition.duration:
            self._order_transition = None
        self._stop_animation_timer_if_idle()
        self.viewport().update()

    def _finish_drop(self) -> None:
        session = self._drag
        if session is None:
            return
        after = session.after
        selected_ids = session.selected_ids
        grabbed_id = session.grabbed_id
        self._drag = None
        self.dragStateChanged.emit(False)
        self._sync_pointer_from_global_position()
        self._suppress_next_order_transition = True
        changed = self.track_model.apply_reorder(after, len(selected_ids))
        if not changed:
            self._suppress_next_order_transition = False
        self._select_track_ids(selected_ids, grabbed_id)

    def _sync_pointer_from_global_position(self) -> None:
        point = self.viewport().mapFromGlobal(QCursor.pos())
        row = self.indexAt(point).row() if self.viewport().rect().contains(point) else -1
        self._set_hover_row(row)

    def _model_order_changed(self, before: tuple[str, ...], after: tuple[str, ...]) -> None:
        self._delegate.clear_row_cache()
        if self._suppress_next_order_transition:
            self._suppress_next_order_transition = False
            self.viewport().update()
            return
        if not before or len(before) != len(after):
            return
        row_height = self._delegate.row_height()
        self._order_transition = _OrderTransition(
            after=after,
            selected_ids=frozenset(self._selected_track_ids()),
            from_y={track_id: float(row * row_height) for row, track_id in enumerate(before)},
            target_y={track_id: float(row * row_height) for row, track_id in enumerate(after)},
        )
        self._ensure_animation_timer()
        self.viewport().update()

    def _paint_animated_rows(self, painter: QPainter, now: float) -> None:
        session = self._drag
        if session is None:
            return
        row_height = self._delegate.row_height()
        offset = self.verticalScrollBar().value()
        viewport_height = self.viewport().height()
        progress = self._out_cubic((now - session.reflow_started) / self.REFLOW_DURATION)

        def position(track_id: str) -> float:
            target = session.row_targets[track_id]
            return self._lerp(session.row_from.get(track_id, target), target, progress)

        top = offset - row_height
        bottom = offset + viewport_height + row_height
        first = bisect_left(session.remaining_ids, float(top), key=position)
        last = bisect_right(session.remaining_ids, float(bottom), key=position)
        cancelling = session.phase == "cancelling"
        selected_count = len(session.selected_ids)
        for remaining_index in range(first, last):
            track_id = session.remaining_ids[remaining_index]
            content_y = position(track_id)
            y = content_y - offset
            row = self.track_model._row_by_id.get(track_id)
            if row is None:
                continue
            if cancelling:
                display_number = row + 1
            else:
                display_number = remaining_index + 1
                if remaining_index >= session.insertion:
                    display_number += selected_count
            pixmap = self._delegate.render_row(row, display_number, selected=False, hovered=False)
            painter.drawPixmap(0, round(y), pixmap)

        if session.phase == "dragging":
            block_top = session.pointer.y() - session.hotspot_y
            lift = self._out_cubic((now - session.lift_started) / self.LIFT_DURATION)
            lateral = max(
                -self._scaled(18, 10),
                min(self._scaled(18, 10), (session.pointer.x() - session.press_x) * 0.08),
            )
            self._paint_lifted_block(painter, session, block_top, lateral, lift)
            return

        duration = self.SETTLE_DURATION if session.phase == "settling" else self.CANCEL_DURATION
        progress = self._out_cubic((now - session.phase_started) / duration)
        if session.phase == "settling":
            block_top = self._lerp(session.overlay_from_y, session.overlay_target_y, progress)
            self._paint_lifted_block(painter, session, block_top, 0.0, 1.0 - progress)
            return


        original_rows = {track_id: row for row, track_id in enumerate(session.before)}
        for selected_index, track_id in enumerate(session.selected_ids):
            start_y = session.overlay_from_y + selected_index * row_height
            target_y = original_rows[track_id] * row_height - offset
            y = self._lerp(start_y, target_y, progress)
            self._paint_lifted_track(
                painter,
                track_id,
                self.track_model._row_by_id.get(track_id, 0) + 1,
                y,
                0.0,
                max(0.0, 1.0 - progress),
            )

    def _paint_lifted_block(
        self,
        painter: QPainter,
        session: _DragSession,
        top: float,
        lateral: float,
        lift: float,
    ) -> None:
        row_height = self._delegate.row_height()
        first = max(0, int((-top) // row_height) - 1)
        last = min(
            len(session.selected_ids),
            int((self.viewport().height() - top) // row_height) + 2,
        )
        for selected_index in range(first, last):
            track_id = session.selected_ids[selected_index]
            self._paint_lifted_track(
                painter,
                track_id,
                session.insertion + selected_index + 1,
                top + selected_index * row_height,
                lateral,
                lift,
            )

    def _paint_lifted_track(
        self,
        painter: QPainter,
        track_id: str,
        number: int,
        y: float,
        lateral: float,
        lift: float,
    ) -> None:
        row = self.track_model._row_by_id.get(track_id)
        if row is None:
            return
        row_height = self._delegate.row_height()
        if y + row_height < 0 or y > self.viewport().height():
            return
        shadow_alpha = round(70 * max(0.0, min(1.0, lift)))
        if shadow_alpha:
            gap = self._scaled(4, 3)
            card_height = self._scaled(54, 40)
            vertical_gap = (row_height - card_height) / 2
            shadow = QRectF(
                gap + lateral + self._scaled(2),
                y + vertical_gap + self._scaled(4),
                max(1, self.viewport().width() - gap * 2 - 1),
                card_height,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, shadow_alpha))
            painter.drawRoundedRect(shadow, self._scaled(8, 5), self._scaled(8, 5))
        pixmap = self._delegate.render_row(row, number, selected=True, hovered=False)
        painter.drawPixmap(round(lateral), round(y - self._scaled(2) * lift), pixmap)

    def _paint_order_transition(self, painter: QPainter, now: float) -> None:
        transition = self._order_transition
        if transition is None:
            return
        progress = self._out_cubic((now - transition.started) / transition.duration)
        offset = self.verticalScrollBar().value()
        row_height = self._delegate.row_height()
        viewport_height = self.viewport().height()
        for number, track_id in enumerate(transition.after, 1):
            y = self._lerp(transition.from_y[track_id], transition.target_y[track_id], progress) - offset
            if y + row_height < 0 or y > viewport_height:
                continue
            row = self.track_model._row_by_id.get(track_id)
            if row is None:
                continue
            pixmap = self._delegate.render_row(row, number, selected=track_id in transition.selected_ids, hovered=False)
            painter.drawPixmap(0, round(y), pixmap)

    def _paint_static_rows(self, painter: QPainter) -> None:
        first, last = self.visible_row_range()
        if last < first:
            return
        row_height = self._delegate.row_height()
        offset = self.verticalScrollBar().value()
        selected_rows = {index.row() for index in self.selectionModel().selectedRows()}
        for row in range(first, last + 1):
            pixmap = self._delegate.render_row(
                row,
                row + 1,
                selected=row in selected_rows,
                hovered=row == self._hover_row,
            )
            painter.drawPixmap(0, row * row_height - offset, pixmap)

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(event.rect(), QColor(self._delegate.theme.app_bg))
        now = monotonic()
        if self._drag is not None:
            self._paint_animated_rows(painter, now)
        elif self._order_transition is not None:
            self._paint_order_transition(painter, now)
        else:
            self._paint_static_rows(painter)
        if self._external_drop_insertion >= 0:
            row_height = self._delegate.row_height()
            y = (
                self._external_drop_insertion * row_height
                - self.verticalScrollBar().value()
            )
            inset = self._scaled(4, 3)
            painter.setPen(
                QPen(
                    QColor(self._delegate.theme.accent),
                    self._scaled(3, 2),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(inset, y, self.viewport().width() - inset, y)
        painter.end()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self._cancel_drag()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete and self._drag is None:
            selected = self._selected_track_ids()
            if selected and self.isEnabled():
                self.deleteTracksRequested.emit(selected)
                event.accept()
                return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        index = self.indexAt(event.pos())
        if not index.isValid() or not self.isEnabled() or self._drag is not None:
            return
        track_id = self.track_model.tracks[index.row()].track_id
        selected = self._selected_track_ids()
        if track_id not in selected:
            self._select_track_ids((track_id,), current_id=track_id)
            selected = (track_id,)
        menu = QMenu(self)
        label = "Remove selected track" if len(selected) == 1 else f"Remove {len(selected)} selected tracks"
        menu.addAction(label, lambda: self.deleteTracksRequested.emit(selected))
        menu.exec(event.globalPos())

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.Leave:
            self._set_hover_row(-1)
        elif event.type() == QEvent.Type.UngrabMouse and self._drag is not None and not self._releasing_mouse:
            self._cancel_drag()
        return super().viewportEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._drag is not None and self._drag.phase == "dragging":
            self._update_drag_destination(force=True)
        self._schedule_visible_rows()

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

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._cancel_drag()
            self._clear_external_drop()
        super().changeEvent(event)
