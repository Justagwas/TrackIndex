from __future__ import annotations

from dataclasses import replace
from time import monotonic

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from trackindex.core.playback import PlaybackSnapshot, PlaybackState, PlayerPresentation, RepeatMode

from .interaction import sync_pointer_cursor
from .theme import ThemePalette
from .track_list import format_duration
from .widgets.custom_controls import RoundHandleSliderStyle

_ICON_CACHE: dict[tuple[object, ...], QIcon] = {}


def _animation_float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _icon(
    theme: ThemePalette,
    name: str,
    size: int,
    device_pixel_ratio: float,
    active: bool = False,
    primary: bool = False,
) -> QIcon:
    ratio = max(1.0, float(device_pixel_ratio))
    key = (
        theme.mode,
        theme.text_primary,
        theme.accent,
        name,
        int(size),
        round(ratio, 2),
        bool(active),
        bool(primary),
    )
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_ICON_CACHE) >= 192:
        _ICON_CACHE.clear()
    physical_size = max(1, round(size * ratio))
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(ratio, ratio)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#FFFFFF" if primary else theme.accent if active else theme.text_primary)
    pen = QPen(color, max(1.35, size * 0.078), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(color)
    center = size / 2
    if name == "play":
        path = QPainterPath()
        path.moveTo(size * 0.38, size * 0.27)
        path.lineTo(size * 0.73, center)
        path.lineTo(size * 0.38, size * 0.73)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "pause":
        painter.drawRoundedRect(QRectF(size * 0.34, size * 0.27, size * 0.1, size * 0.46), size * 0.03, size * 0.03)
        painter.drawRoundedRect(QRectF(size * 0.56, size * 0.27, size * 0.1, size * 0.46), size * 0.03, size * 0.03)
    elif name in {"previous", "next"}:
        previous = name == "previous"
        bar_x = size * (0.29 if previous else 0.71)
        painter.drawLine(QPoint(round(bar_x), round(size * 0.25)), QPoint(round(bar_x), round(size * 0.75)))
        path = QPainterPath()
        if previous:
            path.moveTo(size * 0.69, size * 0.24)
            path.lineTo(size * 0.35, center)
            path.lineTo(size * 0.69, size * 0.76)
        else:
            path.moveTo(size * 0.31, size * 0.24)
            path.lineTo(size * 0.65, center)
            path.lineTo(size * 0.31, size * 0.76)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "shuffle":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        upper = QPainterPath()
        upper.moveTo(size * 0.18, size * 0.31)
        upper.cubicTo(size * 0.36, size * 0.31, size * 0.42, size * 0.69, size * 0.68, size * 0.69)
        lower = QPainterPath()
        lower.moveTo(size * 0.18, size * 0.69)
        lower.cubicTo(size * 0.36, size * 0.69, size * 0.42, size * 0.31, size * 0.68, size * 0.31)
        painter.drawPath(upper)
        painter.drawPath(lower)
        painter.drawLine(QPoint(round(size * 0.68), round(size * 0.31)), QPoint(round(size * 0.81), round(size * 0.31)))
        painter.drawLine(QPoint(round(size * 0.68), round(size * 0.69)), QPoint(round(size * 0.81), round(size * 0.69)))
        painter.drawLine(QPoint(round(size * 0.74), round(size * 0.23)), QPoint(round(size * 0.82), round(size * 0.31)))
        painter.drawLine(QPoint(round(size * 0.74), round(size * 0.39)), QPoint(round(size * 0.82), round(size * 0.31)))
        painter.drawLine(QPoint(round(size * 0.74), round(size * 0.61)), QPoint(round(size * 0.82), round(size * 0.69)))
        painter.drawLine(QPoint(round(size * 0.74), round(size * 0.77)), QPoint(round(size * 0.82), round(size * 0.69)))
    elif name in {"repeat", "repeat-one"}:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        top = QPainterPath()
        top.moveTo(size * 0.22, size * 0.38)
        top.cubicTo(size * 0.3, size * 0.24, size * 0.47, size * 0.23, size * 0.62, size * 0.27)
        top.cubicTo(size * 0.7, size * 0.29, size * 0.75, size * 0.34, size * 0.79, size * 0.4)
        bottom = QPainterPath()
        bottom.moveTo(size * 0.78, size * 0.62)
        bottom.cubicTo(size * 0.7, size * 0.76, size * 0.53, size * 0.77, size * 0.38, size * 0.73)
        bottom.cubicTo(size * 0.3, size * 0.71, size * 0.25, size * 0.66, size * 0.21, size * 0.6)
        painter.drawPath(top)
        painter.drawPath(bottom)
        painter.drawLine(QPoint(round(size * 0.69), round(size * 0.4)), QPoint(round(size * 0.79), round(size * 0.4)))
        painter.drawLine(QPoint(round(size * 0.79), round(size * 0.4)), QPoint(round(size * 0.79), round(size * 0.3)))
        painter.drawLine(QPoint(round(size * 0.31), round(size * 0.6)), QPoint(round(size * 0.21), round(size * 0.6)))
        painter.drawLine(QPoint(round(size * 0.21), round(size * 0.6)), QPoint(round(size * 0.21), round(size * 0.7)))
        if name == "repeat-one":
            digit_pen = QPen(QColor(theme.accent), max(1.2, size * 0.075), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(digit_pen)
            painter.drawLine(
                QPoint(round(size * 0.47), round(size * 0.45)),
                QPoint(round(size * 0.53), round(size * 0.39)),
            )
            painter.drawLine(
                QPoint(round(size * 0.53), round(size * 0.39)),
                QPoint(round(size * 0.53), round(size * 0.63)),
            )
    elif name in {"volume", "muted"}:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(size * 0.19, size * 0.42)
        path.lineTo(size * 0.35, size * 0.42)
        path.lineTo(size * 0.53, size * 0.27)
        path.lineTo(size * 0.53, size * 0.73)
        path.lineTo(size * 0.35, size * 0.58)
        path.lineTo(size * 0.19, size * 0.58)
        path.closeSubpath()
        painter.drawPath(path)
        if name == "muted":
            painter.drawLine(
                QPoint(round(size * 0.66), round(size * 0.37)),
                QPoint(round(size * 0.83), round(size * 0.63)),
            )
            painter.drawLine(
                QPoint(round(size * 0.83), round(size * 0.37)),
                QPoint(round(size * 0.66), round(size * 0.63)),
            )
        else:
            painter.drawArc(QRectF(size * 0.49, size * 0.32, size * 0.27, size * 0.36), -55 * 16, 110 * 16)
            painter.drawArc(QRectF(size * 0.45, size * 0.22, size * 0.43, size * 0.56), -52 * 16, 104 * 16)
    elif name == "collapse":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPoint(round(size * 0.27), round(size * 0.39)), QPoint(round(center), round(size * 0.61)))
        painter.drawLine(QPoint(round(center), round(size * 0.61)), QPoint(round(size * 0.73), round(size * 0.39)))
    elif name == "expand":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPoint(round(size * 0.27), round(size * 0.61)), QPoint(round(center), round(size * 0.39)))
        painter.drawLine(QPoint(round(center), round(size * 0.39)), QPoint(round(size * 0.73), round(size * 0.61)))
    elif name == "close":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPoint(round(size * 0.29), round(size * 0.29)), QPoint(round(size * 0.71), round(size * 0.71)))
        painter.drawLine(QPoint(round(size * 0.71), round(size * 0.29)), QPoint(round(size * 0.29), round(size * 0.71)))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    icon = QIcon(pixmap)
    _ICON_CACHE[key] = icon
    return icon


class PlayerIconButton(QPushButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._previous_icon = QIcon()
        self._icon_progress = 1.0
        self._icon_animation = QVariantAnimation(self)
        self._icon_animation.setDuration(120)
        self._icon_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._icon_animation.valueChanged.connect(self._set_icon_progress)
        self._icon_animation.finished.connect(self._finish_icon_animation)

    def set_player_icon(self, icon: QIcon, animate: bool = False) -> None:
        current = self.icon()
        if current.cacheKey() == icon.cacheKey():
            return
        self._icon_animation.stop()
        self._previous_icon = current if animate else QIcon()
        self.setIcon(icon)
        if animate and not self._previous_icon.isNull():
            self._icon_progress = 0.0
            self._icon_animation.setStartValue(0.0)
            self._icon_animation.setEndValue(1.0)
            self._icon_animation.start()
        else:
            self._icon_progress = 1.0
            self.update()

    def _set_icon_progress(self, value: object) -> None:
        self._icon_progress = max(0.0, min(1.0, _animation_float(value)))
        self.update()

    def _finish_icon_animation(self) -> None:
        self._previous_icon = QIcon()
        self._icon_progress = 1.0
        self.update()

    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.icon = QIcon()
        option.text = ""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        icon_size = self.iconSize()
        rect = QRectF(
            (self.width() - icon_size.width()) / 2,
            (self.height() - icon_size.height()) / 2,
            icon_size.width(),
            icon_size.height(),
        ).toRect()
        if not self._previous_icon.isNull() and self._icon_progress < 1.0:
            painter.save()
            painter.setOpacity(1.0 - self._icon_progress)
            self._previous_icon.paint(painter, rect)
            painter.restore()
        painter.save()
        painter.setOpacity(self._icon_progress)
        self.icon().paint(painter, rect)
        painter.restore()


class MetadataLabel(QLabel):
    activated = Signal()

    def __init__(self, object_name: str, parent=None, *, interactive: bool = True) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._interactive = interactive
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        if interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_full_text(self, text: str | None) -> None:
        self._full_text = str(text or "")
        self._refresh_text()

    def full_text(self) -> str:
        return self._full_text

    def _refresh_text(self) -> None:
        width = max(0, self.contentsRect().width())
        elided = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width) if width else self._full_text
        QLabel.setText(self, elided)
        self.setToolTip(self._full_text if elided != self._full_text else "")
        self.setAccessibleName(self._full_text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def mouseReleaseEvent(self, event) -> None:
        if (
            self._interactive
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
            and self._full_text
        ):
            self.activated.emit()
        super().mouseReleaseEvent(event)


class HoverSeekSlider(QSlider):
    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._player_style: RoundHandleSliderStyle | None = None
        self._idle_fill = ""
        self._active_fill = ""
        self._handle_opacity = 0.0
        self._handle_target = False
        self._pressed = False
        self._keyboard_focus = False
        self._handle_animation = QVariantAnimation(self)
        self._handle_animation.setDuration(95)
        self._handle_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._handle_animation.valueChanged.connect(self._set_handle_opacity)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def handle_opacity(self) -> float:
        return self._handle_opacity

    def set_player_style(self, style: RoundHandleSliderStyle, idle_fill: str, active_fill: str) -> None:
        self._player_style = style
        self._idle_fill = idle_fill
        self._active_fill = active_fill
        style.set_handle_opacity(self._handle_opacity)
        style.set_fill_color(active_fill if self._handle_target else idle_fill)
        self.setStyle(style)

    def _set_handle_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if self._handle_target == visible:
            return
        self._handle_target = visible
        if self._player_style is not None:
            self._player_style.set_fill_color(self._active_fill if visible else self._idle_fill)
        self._handle_animation.stop()
        self._handle_animation.setStartValue(self._handle_opacity)
        self._handle_animation.setEndValue(1.0 if visible else 0.0)
        self._handle_animation.start()
        self.update()

    def _set_handle_opacity(self, value: object) -> None:
        self._handle_opacity = max(0.0, min(1.0, _animation_float(value)))
        if self._player_style is not None:
            self._player_style.set_handle_opacity(self._handle_opacity)
        self.update()

    def enterEvent(self, event) -> None:
        self._set_handle_visible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._pressed and not self._keyboard_focus:
            self._set_handle_visible(False)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        if self._keyboard_focus:
            self._set_handle_visible(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._keyboard_focus = False
        if not self.underMouse() and not self._pressed:
            self._set_handle_visible(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        self._keyboard_focus = True
        self._set_handle_visible(True)
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        self._pressed = True
        self._set_handle_visible(True)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._pressed = False
        if not self.underMouse() and not self._keyboard_focus:
            self._set_handle_visible(False)


class PlayerArtwork(QWidget):
    activated = Signal()

    def __init__(self, theme: ThemePalette, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme
        self._artwork_data = b""
        self._artwork_image = QImage()
        self._previous_artwork_image = QImage()
        self._transition = 1.0
        self._artwork_animation = QVariantAnimation(self)
        self._artwork_animation.setDuration(180)
        self._artwork_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._artwork_animation.valueChanged.connect(self._set_transition)
        self._artwork_animation.finished.connect(self._finish_transition)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Return to playing track")

    def set_artwork(self, value: bytes | None) -> None:
        incoming = value or b""
        if incoming == self._artwork_data:
            return
        self._artwork_animation.stop()
        self._previous_artwork_image = self._artwork_image
        self._artwork_data = incoming
        image = QImage.fromData(incoming) if incoming else QImage()
        if not image.isNull():
            side = min(image.width(), image.height())
            image = image.copy(
                (image.width() - side) // 2,
                (image.height() - side) // 2,
                side,
                side,
            )
        self._artwork_image = image
        if not self._previous_artwork_image.isNull():
            self._artwork_animation.setStartValue(0.0)
            self._artwork_animation.setEndValue(1.0)
            self._artwork_animation.start()
        else:
            self._transition = 1.0
            self.update()

    def _set_transition(self, value: object) -> None:
        self._transition = max(0.0, min(1.0, _animation_float(value)))
        self.update()

    def _finish_transition(self) -> None:
        self._previous_artwork_image = QImage()
        self._transition = 1.0
        self.update()

    def _paint_artwork(self, painter: QPainter, image: QImage, rect: QRectF, opacity: float) -> None:
        painter.save()
        painter.setOpacity(opacity)
        if image.isNull():
            painter.fillRect(rect, QColor(self.theme.disabled_bg))
            painter.setPen(QPen(QColor(self.theme.text_secondary), 1.5))
            diameter = min(rect.width(), rect.height()) * 0.48
            painter.drawEllipse(
                QRectF(rect.center().x() - diameter / 2, rect.center().y() - diameter / 2, diameter, diameter)
            )
            painter.setBrush(QColor(self.theme.text_secondary))
            painter.drawEllipse(rect.center(), 2.5, 2.5)
        else:
            painter.drawImage(rect, image)
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = max(4, round(min(rect.width(), rect.height()) * 0.12))
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        if not self._previous_artwork_image.isNull() and self._transition < 1.0:
            self._paint_artwork(painter, self._previous_artwork_image, rect, 1.0 - self._transition)
        self._paint_artwork(painter, self._artwork_image, rect, self._transition)
        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(self.theme.border), 1))
        painter.drawRoundedRect(rect, radius, radius)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit()
        super().mouseReleaseEvent(event)


class AudioInfoPopup(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("audioInfoPopup")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        self.source_title = QLabel("Source", self)
        self.source_title.setObjectName("sectionTitle")
        self.source_text = QLabel("", self)
        self.source_text.setObjectName("muted")
        self.output_title = QLabel("Output", self)
        self.output_title.setObjectName("sectionTitle")
        self.output_text = QLabel("", self)
        self.output_text.setObjectName("muted")
        for widget in (self.source_title, self.source_text, self.output_title, self.output_text):
            layout.addWidget(widget)
        self.setMinimumWidth(270)

    def set_snapshot(self, snapshot: PlaybackSnapshot) -> None:
        entry = snapshot.entry
        source = entry.source if entry is not None else None
        source_lines = []
        if source is not None:
            if source.extension or source.container:
                source_lines.append(f"Format: {source.container or source.extension.upper()}")
            if source.codec:
                source_lines.append(f"Codec: {source.codec}")
            if source.bitrate:
                source_lines.append(f"Bitrate: {round(source.bitrate / 1000)} kbps")
            if source.sample_rate:
                source_lines.append(f"Sample rate: {source.sample_rate / 1000:g} kHz")
            if source.bit_depth:
                source_lines.append(f"Bit depth: {source.bit_depth}-bit")
            if source.channels or source.channel_layout:
                source_lines.append(f"Channels: {source.channel_layout or source.channels}")
        output = snapshot.output
        output_lines = [f"Device: {output.device or 'System default'}"]
        if output.sample_rate:
            output_lines.append(f"Sample rate: {output.sample_rate / 1000:g} kHz")
        if output.sample_format:
            output_lines.append(f"Format: {output.sample_format}")
        if output.channels or output.channel_layout:
            output_lines.append(f"Channels: {output.channel_layout or output.channels}")
        self.source_text.setText("\n".join(source_lines) or "Details unavailable")
        self.output_text.setText("\n".join(output_lines))
        self.adjustSize()


class PlayerPanel(QFrame):
    playPauseRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()
    stopRequested = Signal()
    seekRequested = Signal(float)
    volumeChanged = Signal(int)
    muteChanged = Signal(bool)
    shuffleChanged = Signal(bool)
    repeatChanged = Signal(str)
    closeRequested = Signal()
    presentationRequested = Signal(str)
    currentTrackRequested = Signal()
    resizedDuringAnimation = Signal()

    def __init__(self, theme: ThemePalette, scale: float, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.scale = scale
        self.snapshot = PlaybackSnapshot()
        self.presentation = PlayerPresentation.HIDDEN
        self._base_presentation = PlayerPresentation.HIDDEN
        self._drag_compact = False
        self._scrubbing = False
        self._position_anchor = monotonic()
        self._position_at_anchor = 0.0
        self._last_icon_dpr = 0.0
        self._expansion = 1.0
        self._content_animation = QVariantAnimation(self)
        self._content_animation.valueChanged.connect(self._set_expansion)
        self._height_animation = QVariantAnimation(self)
        self._height_animation.valueChanged.connect(self._animate_height)
        self._height_animation.finished.connect(self._animation_finished)
        self._position_timer = QTimer(self)
        self._position_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._position_timer.setInterval(33)
        self._position_timer.timeout.connect(self._interpolate_position)
        self._info_popup = AudioInfoPopup(self)
        self.setObjectName("playerPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._build()
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)
        self.hide()
        self.set_appearance(theme, scale)

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(10, 7, 10, 7)
        root.setSpacing(14)
        self._root_layout = root

        left = QWidget(self)
        left.setObjectName("playerLeftZone")
        left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        left_layout = QHBoxLayout(left)
        self._left_layout = left_layout
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        self.artwork = PlayerArtwork(self.theme, left)
        self.artwork.activated.connect(self.currentTrackRequested.emit)
        left_layout.addWidget(self.artwork, 0, Qt.AlignmentFlag.AlignVCenter)
        info = QWidget(left)
        info.setObjectName("playerMetadata")
        info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self.title = MetadataLabel("playerTrackTitle", info)
        self.title.activated.connect(self.currentTrackRequested.emit)
        self.track_metadata = MetadataLabel("playerTrackMetadata", info, interactive=False)
        self.technical_metadata = MetadataLabel("playerTechnicalLink", info)
        self.technical_metadata.activated.connect(self._show_audio_info)
        info_layout.addStretch(1)
        info_layout.addWidget(self.title)
        info_layout.addWidget(self.track_metadata)
        info_layout.addWidget(self.technical_metadata)
        info_layout.addStretch(1)
        left_layout.addWidget(info, 1)
        left.setMinimumWidth(150)
        root.addWidget(left, 3)
        self._left = left
        self._metadata = info

        center = QWidget(self)
        center.setObjectName("playerCenterZone")
        center.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addStretch(1)
        timeline_widget = QWidget(center)
        timeline = QHBoxLayout(timeline_widget)
        timeline.setContentsMargins(0, 0, 0, 0)
        timeline.setSpacing(8)
        self.elapsed = QLabel("0:00", center)
        self.elapsed.setObjectName("playerTime")
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seek = HoverSeekSlider(center)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(self._begin_scrub)
        self.seek.sliderReleased.connect(self._finish_scrub)
        self.seek.sliderMoved.connect(self._preview_scrub)
        self.duration = QLabel("--", center)
        self.duration.setObjectName("playerTime")
        self.duration.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timeline.addWidget(self.elapsed, 0, Qt.AlignmentFlag.AlignVCenter)
        timeline.addWidget(self.seek, 1, Qt.AlignmentFlag.AlignVCenter)
        timeline.addWidget(self.duration, 0, Qt.AlignmentFlag.AlignVCenter)
        self._timeline_slot = QWidget(center)
        center_layout.addWidget(self._timeline_slot)
        transport = QHBoxLayout()
        self._transport_layout = transport
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(5)
        transport.addStretch(1)
        self.shuffle_button = self._button("shuffle", "Shuffle")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.toggled.connect(self.shuffleChanged.emit)
        self.shuffle_button.toggled.connect(self._refresh_icons)
        self.previous_button = self._button("previous", "Previous")
        self.previous_button.clicked.connect(self.previousRequested.emit)
        self.play_button = self._button("play", "Play or pause", primary=True)
        self.play_button.clicked.connect(self.playPauseRequested.emit)
        self.next_button = self._button("next", "Next")
        self.next_button.clicked.connect(self.nextRequested.emit)
        self.repeat_button = self._button("repeat", "Repeat")
        self.repeat_button.clicked.connect(self._cycle_repeat)
        for button in (
            self.shuffle_button,
            self.previous_button,
            self.play_button,
            self.next_button,
            self.repeat_button,
        ):
            transport.addWidget(button)
        transport.addStretch(1)
        center_layout.addLayout(transport)
        center_layout.addStretch(1)
        middle = QWidget(self)
        middle.setObjectName("playerTimelineZone")
        middle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        stack = QStackedLayout(middle)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        stack.addWidget(center)
        root.addWidget(middle, 5)
        self._middle = middle
        self._center = center
        timeline_page = QWidget(middle)
        timeline_layout = QVBoxLayout(timeline_page)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        timeline_layout.addStretch(1)
        timeline_layout.addWidget(timeline_widget)
        self._timeline_tail = QWidget(timeline_page)
        timeline_layout.addWidget(self._timeline_tail)
        timeline_layout.addStretch(1)
        stack.addWidget(timeline_page)
        stack.setCurrentWidget(timeline_page)
        self._timeline_page = timeline_page
        self._timeline = timeline_widget
        timeline_widget.installEventFilter(self)
        self.mini_elapsed = self.elapsed
        self.mini_seek = self.seek
        self.mini_duration = self.duration
        self.mini_play_button = self._button("play", "Play or pause", primary=True)
        self.mini_play_button.setObjectName("playerMiniPrimaryButton")
        self.mini_play_button.clicked.connect(self.playPauseRequested.emit)
        self.mini_previous_button = self._button("previous", "Previous")
        self.mini_previous_button.clicked.connect(self.previousRequested.emit)
        self.mini_next_button = self._button("next", "Next")
        self.mini_next_button.clicked.connect(self.nextRequested.emit)

        right = QWidget(self)
        right.setObjectName("playerRightZone")
        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addStretch(1)
        self._right_layout = right_layout
        top_controls = QWidget(right)
        top_controls.setObjectName("playerRightTop")
        top_layout = QHBoxLayout(top_controls)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)
        top_layout.addStretch(1)
        self._top_layout = top_layout
        mini_transport = QWidget(top_controls)
        mini_transport_layout = QHBoxLayout(mini_transport)
        mini_transport_layout.setContentsMargins(0, 0, 0, 0)
        mini_transport_layout.setSpacing(4)
        for button in (self.mini_play_button, self.mini_previous_button, self.mini_next_button):
            mini_transport_layout.addWidget(button)
        top_layout.addWidget(mini_transport)
        self._mini_transport = mini_transport
        self._mini_center = mini_transport
        self._mini_transport_layout = mini_transport_layout
        self.collapse_button = self._button("collapse", "Compact player")
        self.collapse_button.setObjectName("playerUtilityButton")
        self.collapse_button.clicked.connect(self._toggle_presentation)
        self.close_button = self._button("close", "Close player")
        self.close_button.setObjectName("playerUtilityButton")
        self.close_button.clicked.connect(self.closeRequested.emit)
        top_layout.addWidget(self.collapse_button)
        top_layout.addWidget(self.close_button)
        right_layout.addWidget(top_controls)
        volume_row = QWidget(right)
        volume_row.setObjectName("playerVolumeRow")
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(4)
        self._volume_layout = volume_layout
        volume_layout.addStretch(1)
        self.mute_button = self._button("volume", "Mute", parent=volume_row)
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self.muteChanged.emit)
        self.mute_button.toggled.connect(self._refresh_icons)
        self.volume = HoverSeekSlider(volume_row)
        self.volume.setRange(0, 100)
        self.volume.setValue(75)
        self.volume.valueChanged.connect(self.volumeChanged.emit)
        volume_layout.addWidget(self.mute_button, 0, Qt.AlignmentFlag.AlignVCenter)
        volume_layout.addWidget(self.volume, 0, Qt.AlignmentFlag.AlignVCenter)
        right_layout.addWidget(volume_row)
        right_layout.addStretch(1)
        right.setMinimumWidth(150)
        root.addWidget(right, 3)
        self._right = right
        self._right_top = top_controls
        self._volume_row = volume_row
        self._fade_effects: dict[QWidget, QGraphicsOpacityEffect] = {}
        for widget in (center, mini_transport, volume_row, self.technical_metadata):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            self._fade_effects[widget] = effect

    def _button(self, icon_name: str, tooltip: str, primary: bool = False, parent=None) -> PlayerIconButton:
        button = PlayerIconButton(parent or self)
        button.setObjectName("playerPrimaryButton" if primary else "playerIconButton")
        button.setProperty("iconName", icon_name)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFlat(True)
        return button

    def set_appearance(self, theme: ThemePalette, scale: float) -> None:
        self.theme = theme
        self.scale = max(0.75, min(2.0, scale))
        self.artwork.theme = theme
        self.artwork.update()
        margin_x = max(8, round(10 * self.scale))
        margin_y = max(5, round(7 * self.scale))
        self._root_layout.setContentsMargins(margin_x, margin_y, margin_x, margin_y)
        self._root_layout.setSpacing(max(8, round(14 * self.scale)))
        self._left_layout.setSpacing(max(6, round(10 * self.scale)))
        self._seek_style = RoundHandleSliderStyle(
            handle_color=theme.text_primary,
            border_color=theme.border,
            groove_color=theme.border,
            fill_color=theme.text_secondary,
            parent=self.seek,
        )
        self._seek_style.set_metrics(
            handle_size=max(10, round(12 * self.scale)),
            groove_height=max(2, round(3 * self.scale)),
        )
        self.seek.set_player_style(self._seek_style, theme.text_secondary, theme.accent)
        self._volume_style = RoundHandleSliderStyle(
            handle_color=theme.text_primary,
            border_color=theme.border,
            groove_color=theme.border,
            fill_color=theme.accent,
            parent=self.volume,
        )
        self._volume_style.set_metrics(
            handle_size=max(10, round(12 * self.scale)),
            groove_height=max(3, round(4 * self.scale)),
            vertical_offset=max(1, round(self.scale)),
        )
        self.volume.set_player_style(self._volume_style, theme.accent, theme.accent)
        self._mini_seek_style = self._seek_style
        secondary_size = max(22, round(26 * self.scale))
        primary_size = max(30, round(38 * self.scale))
        mini_primary_size = max(26, round(32 * self.scale))
        side_width = max(150, mini_primary_size + 4 * secondary_size + 4 * max(3, round(4 * self.scale)))
        self._left.setMinimumWidth(side_width)
        self._right.setMinimumWidth(side_width)
        self._top_layout.setSpacing(max(3, round(4 * self.scale)))
        self._mini_transport_layout.setSpacing(max(3, round(4 * self.scale)))
        for button in (
            self.shuffle_button,
            self.previous_button,
            self.next_button,
            self.mini_previous_button,
            self.mini_next_button,
            self.repeat_button,
            self.mute_button,
        ):
            button.setFixedSize(secondary_size, secondary_size)
        utility_width = secondary_size
        utility_height = secondary_size
        self.collapse_button.setFixedSize(utility_width, utility_height)
        self.close_button.setFixedSize(utility_width, utility_height)
        self.play_button.setFixedSize(primary_size, primary_size)
        self.mini_play_button.setFixedSize(mini_primary_size, mini_primary_size)
        self._right_top.setFixedHeight(utility_height)
        self._volume_row.setFixedHeight(primary_size)
        self._right_layout.setContentsMargins(0, 0, 0, 0)
        self._volume_layout.setContentsMargins(0, 0, max(8, round(12 * self.scale)), 0)
        self.volume.setFixedWidth(max(70, round(92 * self.scale)))
        self.seek.setFixedHeight(secondary_size)
        self._timeline.setFixedHeight(secondary_size)
        self._timeline_slot.setFixedHeight(secondary_size)
        self.volume.setFixedHeight(secondary_size)
        self.elapsed.setFixedWidth(max(28, round(34 * self.scale)))
        self.duration.setFixedWidth(max(28, round(34 * self.scale)))
        self.elapsed.setFixedHeight(secondary_size)
        self.duration.setFixedHeight(secondary_size)
        time_lift = max(1, round(2 * self.scale))
        self.elapsed.setContentsMargins(0, 0, 0, time_lift)
        self.duration.setContentsMargins(0, 0, 0, time_lift)
        self._last_icon_dpr = 0.0
        self._refresh_icons()
        self._apply_presentation_widgets()
        self._info_popup.setStyleSheet(self.window().styleSheet() if self.window() else "")
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (QPushButton, QSlider, PlayerArtwork)) or (
                isinstance(widget, MetadataLabel) and widget._interactive
            ):
                sync_pointer_cursor(widget)

    def _device_pixel_ratio(self) -> float:
        screen = self.screen()
        return max(1.0, float(screen.devicePixelRatio()))

    def _refresh_icons(self, *_args, animate_play: bool = False) -> None:
        ratio = self._device_pixel_ratio()
        self._last_icon_dpr = ratio
        for button in self.findChildren(PlayerIconButton):
            name = str(button.property("iconName") or "")
            if not name:
                continue
            active = button.isChecked() if button.isCheckable() else False
            primary = button in (self.play_button, self.mini_play_button)
            if button in (self.play_button, self.mini_play_button):
                name = "pause" if self.snapshot.state is PlaybackState.PLAYING else "play"
            elif button is self.mute_button:
                name = "muted" if self.snapshot.muted else "volume"
            elif button is self.collapse_button:
                name = "expand" if self.presentation is PlayerPresentation.MINI else "collapse"
            elif button is self.repeat_button:
                if self.snapshot.repeat is RepeatMode.ONE:
                    name = "repeat-one"
                    active = False
                else:
                    active = self.snapshot.repeat is RepeatMode.ALL
            if primary:
                logical_icon_size = 18 if button is self.play_button else 16
            elif button in (self.collapse_button, self.close_button):
                logical_icon_size = 14
            else:
                logical_icon_size = 16
            icon_size = max(13, round(logical_icon_size * self.scale))
            icon = _icon(self.theme, name, icon_size, ratio, active, primary)
            button.setIconSize(QSize(icon_size, icon_size))
            button.set_player_icon(icon, animate_play and primary)

    def set_snapshot(self, snapshot: PlaybackSnapshot) -> None:
        previous_state = self.snapshot.state
        self.snapshot = snapshot
        entry = snapshot.entry
        if entry is not None:
            title = entry.title or entry.path.stem
            self.title.set_full_text(title)
            self.track_metadata.set_full_text(" | ".join(value for value in (entry.artist, entry.album) if value))
            source = entry.source
            format_name = (source.container or source.extension or entry.path.suffix.removeprefix(".")).upper()
            technical = [format_name] if format_name else []
            if source.sample_rate:
                technical.append(f"{source.sample_rate / 1000:g} kHz")
            if source.bit_depth:
                technical.append(f"{source.bit_depth}-bit")
            self.technical_metadata.set_full_text(" | ".join(technical))
            self.artwork.set_artwork(entry.artwork_bytes)
        else:
            self.title.set_full_text("")
            self.track_metadata.set_full_text("")
            self.technical_metadata.set_full_text("")
            self.artwork.set_artwork(None)
        self.duration.setText(format_duration(snapshot.duration_seconds))
        self.mini_duration.setText(format_duration(snapshot.duration_seconds))
        self.volume.blockSignals(True)
        self.volume.setValue(snapshot.volume)
        self.volume.blockSignals(False)
        self.mute_button.blockSignals(True)
        self.mute_button.setChecked(snapshot.muted)
        self.mute_button.blockSignals(False)
        self.shuffle_button.blockSignals(True)
        self.shuffle_button.setChecked(snapshot.shuffle)
        self.shuffle_button.blockSignals(False)
        self.repeat_button.setChecked(snapshot.repeat is not RepeatMode.OFF)
        self.repeat_button.setToolTip(f"Repeat: {snapshot.repeat.value}")
        self._position_at_anchor = max(0.0, snapshot.position_seconds)
        self._position_anchor = monotonic()
        self._update_position(self._position_at_anchor)
        self._info_popup.set_snapshot(snapshot)
        self._apply_metadata_visibility()
        self._refresh_icons(animate_play=previous_state is not snapshot.state)
        if snapshot.state is PlaybackState.PLAYING and self.presentation is not PlayerPresentation.HIDDEN:
            self._position_timer.start()
        else:
            self._position_timer.stop()

    def set_position(self, position: float) -> None:
        value = max(0.0, float(position))
        self.snapshot = replace(self.snapshot, position_seconds=value)
        self._position_at_anchor = value
        self._position_anchor = monotonic()
        self._update_position(value)

    def set_presentation(self, presentation: PlayerPresentation, *, temporary: bool = False) -> None:
        if temporary:
            self._drag_compact = presentation is PlayerPresentation.MINI
        else:
            self._base_presentation = presentation
        target = (
            PlayerPresentation.MINI
            if self._drag_compact and self._base_presentation is not PlayerPresentation.HIDDEN
            else self._base_presentation
        )
        if target is self.presentation and self._height_animation.state() != QAbstractAnimation.State.Running:
            return
        self.presentation = target
        if target is not PlayerPresentation.EXPANDED:
            self._info_popup.hide()
        self._apply_presentation_widgets()
        self._animate_to(self._target_height(target))

    def end_temporary_compaction(self) -> None:
        self._drag_compact = False
        self.set_presentation(self._base_presentation)

    def _target_height(self, presentation: PlayerPresentation) -> int:
        if presentation is PlayerPresentation.HIDDEN:
            return 0
        return max(40, round((54 if presentation is PlayerPresentation.MINI else 84) * self.scale))

    def _animate_to(self, target: int) -> None:
        current = self.height() if self.isVisible() else 0
        self._height_animation.stop()
        if target > 0:
            self.show()
        self._height_animation.setStartValue(current)
        self._height_animation.setEndValue(target)
        self._height_animation.setDuration(190 if target > current else 145)
        self._height_animation.setEasingCurve(QEasingCurve.Type.OutCubic if target > current else QEasingCurve.Type.InOutCubic)
        self._content_animation.stop()
        if self.presentation is not PlayerPresentation.HIDDEN:
            self._content_animation.setStartValue(self._expansion)
            self._content_animation.setEndValue(1.0 if self.presentation is PlayerPresentation.EXPANDED else 0.0)
            self._content_animation.setDuration(self._height_animation.duration())
            self._content_animation.setEasingCurve(self._height_animation.easingCurve())
            self._content_animation.start()
        self._height_animation.start()

    def _animate_height(self, value: object) -> None:
        height = max(0, round(_animation_float(value)))
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.resizedDuringAnimation.emit()

    def _animation_finished(self) -> None:
        if self.presentation is PlayerPresentation.HIDDEN:
            self._position_timer.stop()
            self.hide()

    def _apply_presentation_widgets(self) -> None:
        mini = self.presentation is PlayerPresentation.MINI
        margin_x = max(8, round(10 * self.scale))
        margin_y = max(5, round(7 * self.scale))
        self._root_layout.setContentsMargins(margin_x, margin_y, margin_x, margin_y)
        self._set_expansion(self._expansion)
        self.collapse_button.setToolTip("Expand player" if mini else "Compact player")
        self._apply_metadata_visibility()
        self._refresh_icons()
        if self.snapshot.state is PlaybackState.PLAYING and self.presentation is not PlayerPresentation.HIDDEN:
            self._position_timer.start()
        elif self.presentation is PlayerPresentation.HIDDEN:
            self._position_timer.stop()

    def _set_expansion(self, value: object) -> None:
        self._expansion = max(0.0, min(1.0, _animation_float(value)))
        expanded = self._expansion
        for widget, opacity in (
            (self._center, expanded),
            (self._mini_transport, 1.0 - expanded), (self._volume_row, expanded),
            (self.technical_metadata, expanded),
        ):
            effect = self._fade_effects[widget]
            effect.setOpacity(opacity)
            effect.setEnabled(opacity < 1.0)
            widget.setVisible(opacity > 0.0)
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, opacity < 0.5)
        secondary = max(22, round(26 * self.scale))
        mini_primary = max(26, round(32 * self.scale))
        self._right_top.setFixedHeight(round(secondary * expanded + mini_primary * (1.0 - expanded)))
        self._volume_row.setFixedHeight(round(max(30, round(38 * self.scale)) * expanded))
        self._timeline_tail.setFixedHeight(round(max(30, round(38 * self.scale)) * expanded))
        self.technical_metadata.setMaximumHeight(round(self.technical_metadata.sizeHint().height() * expanded))
        self.artwork.setFixedSize(max(30, round((34 + 30 * expanded) * self.scale)),
                                  max(30, round((34 + 30 * expanded) * self.scale)))
        self._apply_metadata_visibility()

    def _apply_metadata_visibility(self) -> None:
        expanded = self.presentation is PlayerPresentation.EXPANDED
        mini = self.presentation is PlayerPresentation.MINI
        self.title.setVisible((expanded or mini) and bool(self.title.full_text()))
        self.track_metadata.setVisible((expanded or mini) and bool(self.track_metadata.full_text()))
        self.technical_metadata.setVisible(self._expansion > 0.0 and bool(self.technical_metadata.full_text()))
        self._adapt_width()

    def _adapt_width(self) -> None:
        margins = self._root_layout.contentsMargins()
        spacing = self._root_layout.spacing()
        available = self.width() - margins.left() - margins.right()
        secondary = self.close_button.width()
        right_width = self.mini_play_button.width() + 4 * secondary + 4 * self._top_layout.spacing()
        center_width = max(self._transport_layout.minimumSize().width(),
                           self.elapsed.width() + self.duration.width() + 16 + round(64 * self.scale))
        artwork_width = max(30, round(64 * self.scale))
        side_width = max(right_width, artwork_width + self._left_layout.spacing() + round(100 * self.scale))
        show_text = available >= 2 * side_width + center_width + 2 * spacing
        show_artwork = show_text or available >= artwork_width + center_width + right_width + 2 * spacing
        self._metadata.setVisible(show_text)
        self._left.setVisible(show_artwork)
        if not show_text:
            self._info_popup.hide()
        for widget in (self._left, self._right):
            widget.setMaximumWidth(16777215)
        self._left.setMinimumWidth(side_width if show_text else artwork_width)
        self._right.setMinimumWidth(right_width)
        self._middle.setMinimumWidth(center_width)
        self._root_layout.setStretch(0, 3 if show_text else 0)
        self._root_layout.setStretch(1, 5 if show_text else 1)
        self._root_layout.setStretch(2, 3 if show_text else 0)
        if not show_text:
            self._left.setMaximumWidth(artwork_width)
            self._right.setMaximumWidth(right_width)
        volume_width = max(round(48 * self.scale), min(round(92 * self.scale),
                           round((available - center_width - right_width) / 2)))
        self.volume.setFixedWidth(volume_width)

    def set_drag_compact(self, active: bool) -> None:
        if active:
            self.set_presentation(PlayerPresentation.MINI, temporary=True)
        else:
            self.end_temporary_compaction()

    def _toggle_presentation(self) -> None:
        target = (
            PlayerPresentation.EXPANDED if self.presentation is PlayerPresentation.MINI else PlayerPresentation.MINI
        )
        self.presentationRequested.emit(target.value)

    def _cycle_repeat(self) -> None:
        order = (RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE)
        current = self.snapshot.repeat
        target = order[(order.index(current) + 1) % len(order)]
        self.repeatChanged.emit(target.value)

    def _begin_scrub(self) -> None:
        self._scrubbing = True

    def _preview_scrub(self, value: int) -> None:
        duration = self.snapshot.duration_seconds or 0.0
        if duration > 0:
            text = format_duration(duration * value / 1000)
            self.elapsed.setText(text)
            self.mini_elapsed.setText(text)

    def _finish_scrub(self) -> None:
        duration = self.snapshot.duration_seconds or 0.0
        self._scrubbing = False
        if duration > 0:
            sender = self.sender()
            slider = sender if isinstance(sender, QSlider) else self.seek
            self.seekRequested.emit(duration * slider.value() / 1000)

    def _interpolate_position(self) -> None:
        if self.snapshot.state is not PlaybackState.PLAYING or self._scrubbing:
            return
        position = self._position_at_anchor + monotonic() - self._position_anchor
        duration = self.snapshot.duration_seconds
        if duration is not None:
            position = min(position, duration)
        self._update_position(position)

    def _update_position(self, position: float) -> None:
        if self._scrubbing:
            return
        duration = self.snapshot.duration_seconds or 0.0
        text = format_duration(position)
        self.elapsed.setText(text)
        self.mini_elapsed.setText(text)
        self.seek.blockSignals(True)
        self.mini_seek.blockSignals(True)
        value = round(1000 * position / duration) if duration > 0 else 0
        self.seek.setValue(value)
        self.mini_seek.setValue(value)
        self.seek.blockSignals(False)
        self.mini_seek.blockSignals(False)

    def _show_audio_info(self) -> None:
        if self.snapshot.entry is None or self.presentation is not PlayerPresentation.EXPANDED:
            return
        self._info_popup.set_snapshot(self.snapshot)
        gap = max(6, round(10 * self.scale))
        point = self.mapToGlobal(QPoint(
            self._metadata.mapTo(self, QPoint(0, 0)).x() - round(20 * self.scale),
            -self._info_popup.height() - gap,
        ))
        screen = QApplication.screenAt(point)
        if screen is not None:
            available = screen.availableGeometry()
            point.setX(max(available.left(), min(point.x(), available.right() - self._info_popup.width() + 1)))
            point.setY(max(available.top(), min(point.y(), available.bottom() - self._info_popup.height() + 1)))
        self._info_popup.move(point)
        self._info_popup.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adapt_width()
        ratio = self._device_pixel_ratio()
        if abs(ratio - self._last_icon_dpr) >= 0.01:
            self._refresh_icons()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._timeline and event.type() in {QEvent.Type.Move, QEvent.Type.Resize}:
            self._timeline_page.setMask(QRegion(self._timeline.geometry()))
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        result = super().event(event)
        refresh_types = {QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.ScreenChangeInternal}
        device_change = getattr(QEvent.Type, "DevicePixelRatioChange", None)
        if device_change is not None:
            refresh_types.add(device_change)
        if event.type() in refresh_types:
            QTimer.singleShot(0, self._refresh_icons)
        return result
