from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QProxyStyle,
    QStyle,
    QStyleOptionButton,
    QStyleOptionSlider,
    QWidget,
)


class RoundHandleSliderStyle(QProxyStyle):
    def __init__(
        self,
        *,
        handle_color: str,
        border_color: str,
        groove_color: str,
        fill_color: str,
        handle_size: int = 18,
        groove_height: int = 6,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__()
        self._handle_color = QColor(handle_color)
        self._border_color = QColor(border_color)
        self._groove_color = QColor(groove_color)
        self._fill_color = QColor(fill_color)
        self._handle_size = max(7, int(handle_size))
        self._groove_height = max(3, int(groove_height))
        self._vertical_offset = 0
        self._handle_opacity = 1.0

    def set_colors(self, *, handle_color: str, border_color: str, groove_color: str, fill_color: str) -> None:
        self._handle_color = QColor(handle_color)
        self._border_color = QColor(border_color)
        self._groove_color = QColor(groove_color)
        self._fill_color = QColor(fill_color)

    def set_metrics(self, *, handle_size: int, groove_height: int, vertical_offset: int = 0) -> None:
        self._handle_size = max(7, int(handle_size))
        self._groove_height = max(3, int(groove_height))
        self._vertical_offset = int(vertical_offset)

    def set_handle_opacity(self, opacity: float) -> None:
        self._handle_opacity = max(0.0, min(1.0, float(opacity)))

    def set_fill_color(self, color: str) -> None:
        self._fill_color = QColor(color)

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_SliderLength:
            return self._handle_size
        if metric == QStyle.PixelMetric.PM_SliderThickness:
            return max(self._handle_size + 6, self._groove_height + 10)
        return super().pixelMetric(metric, option, widget)

    def _groove_rect(self, option: QStyleOptionSlider) -> QRect:
        inset = max(1, self._handle_size // 2) + 1
        width = max(2, int(option.rect.width()) - inset * 2)
        return QRect(
            int(option.rect.left()) + inset,
            int(option.rect.center().y() - self._groove_height // 2 + self._vertical_offset),
            width,
            self._groove_height,
        )

    def _handle_rect(self, option: QStyleOptionSlider) -> QRect:
        groove = self._groove_rect(option)
        available = max(0, groove.width() - 1)
        position = QStyle.sliderPositionFromValue(
            int(option.minimum),
            int(option.maximum),
            int(option.sliderPosition),
            int(available),
            bool(option.upsideDown),
        )
        half = self._handle_size // 2
        return QRect(
            int(groove.left()) + int(position) - half,
            int(option.rect.center().y()) - half + self._vertical_offset,
            self._handle_size,
            self._handle_size,
        )

    def subControlRect(self, control, option, sub_control, widget=None):
        if (
            control == QStyle.ComplexControl.CC_Slider
            and isinstance(option, QStyleOptionSlider)
            and option.orientation == Qt.Orientation.Horizontal
        ):
            if sub_control == QStyle.SubControl.SC_SliderGroove:
                return self._groove_rect(option)
            if sub_control == QStyle.SubControl.SC_SliderHandle:
                return self._handle_rect(option)
        return super().subControlRect(control, option, sub_control, widget)

    def drawComplexControl(self, control, option, painter, widget=None):
        if control != QStyle.ComplexControl.CC_Slider or not isinstance(option, QStyleOptionSlider):
            super().drawComplexControl(control, option, painter, widget)
            return
        if option.orientation != Qt.Orientation.Horizontal:
            super().drawComplexControl(control, option, painter, widget)
            return

        groove = self._groove_rect(option)
        handle = self._handle_rect(option)
        groove_rect = QRectF(
            float(groove.left()),
            float(option.rect.center().y()) - self._groove_height / 2.0 + self._vertical_offset,
            float(groove.width()),
            float(self._groove_height),
        )
        handle_rect = QRectF(handle)
        handle_center = QPointF(handle_rect.center())
        border_width = max(1, round(self._handle_size / 16))
        half_border = border_width / 2.0
        handle_rect.adjust(half_border, half_border, -half_border, -half_border)
        radius = max(1.0, groove_rect.height() / 2.0)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        groove_color = QColor(self._groove_color)
        fill_color = QColor(self._fill_color)
        handle_color = QColor(self._handle_color)
        border_color = QColor(self._border_color)
        if not enabled:
            groove_color.setAlpha(125)
            fill_color = QColor(self._groove_color).lighter(112)
            fill_color.setAlpha(165)
            handle_color = QColor(self._border_color).lighter(128)
            handle_color.setAlpha(185)
            border_color.setAlpha(165)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(groove_color)
        painter.drawRoundedRect(groove_rect, radius, radius)
        if handle.isValid():
            if option.upsideDown:
                fill_left = handle_center.x()
                fill_width = float(groove.right()) - handle_center.x()
            else:
                fill_left = groove_rect.left()
                fill_width = handle_center.x() - groove_rect.left()
            if fill_width > 0 and option.sliderPosition > option.minimum:
                painter.setBrush(fill_color)
                painter.drawRoundedRect(
                    QRectF(fill_left, groove_rect.top(), fill_width, groove_rect.height()),
                    radius,
                    radius,
                )
        if handle.isValid() and self._handle_opacity > 0.0:
            handle_color.setAlpha(round(handle_color.alpha() * self._handle_opacity))
            border_color.setAlpha(round(border_color.alpha() * self._handle_opacity))
            painter.setBrush(handle_color)
            painter.setPen(QPen(border_color, border_width))
            painter.drawEllipse(handle_rect)
        painter.restore()


class SquareCheckBoxStyle(QProxyStyle):
    def __init__(
        self,
        *,
        border_color: str,
        fill_color: str,
        check_color: str,
        size: int = 16,
        radius: int = 4,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__()
        self._border_color = QColor(border_color)
        self._fill_color = QColor(fill_color)
        self._check_color = QColor(check_color)
        self._size = max(12, int(size))
        self._radius = max(2, int(radius))

    def set_colors(self, *, border_color: str, fill_color: str, check_color: str) -> None:
        self._border_color = QColor(border_color)
        self._fill_color = QColor(fill_color)
        self._check_color = QColor(check_color)

    def set_metrics(self, *, size: int, radius: int) -> None:
        self._size = max(12, int(size))
        self._radius = max(2, int(radius))

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in {QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight}:
            return self._size
        return super().pixelMetric(metric, option, widget)

    def _indicator_rect(self, option) -> QRect:
        return QRect(
            int(option.rect.left()) + 2, int(option.rect.center().y() - self._size // 2), self._size, self._size
        )

    def subElementRect(self, element, option, widget=None):
        if isinstance(option, QStyleOptionButton):
            if element == QStyle.SubElement.SE_CheckBoxIndicator:
                return self._indicator_rect(option)
            if element == QStyle.SubElement.SE_CheckBoxContents:
                indicator = self._indicator_rect(option)
                left = int(indicator.right() + 7)
                return QRect(
                    left, int(option.rect.top()), max(0, int(option.rect.right()) - left + 1), int(option.rect.height())
                )
        return super().subElementRect(element, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            super().drawPrimitive(element, option, painter, widget)
            return

        rect = option.rect.adjusted(1, 1, -2, -2)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        border = QColor(self._border_color)
        fill = QColor(self._fill_color if checked else "transparent")
        check = QColor(self._check_color)
        if not enabled:
            border.setAlpha(130)
            fill.setAlpha(110 if checked else 0)
            check.setAlpha(170)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect), float(self._radius), float(self._radius))
        if checked:
            pen = QPen(check, max(2, round(self._size / 9)), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x, y, width, height = float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height())
            painter.drawLine(QPointF(x + width * 0.24, y + height * 0.56), QPointF(x + width * 0.44, y + height * 0.74))
            painter.drawLine(QPointF(x + width * 0.44, y + height * 0.74), QPointF(x + width * 0.78, y + height * 0.34))
        painter.restore()
