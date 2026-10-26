from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QRegion
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QSizePolicy, QStyle, QStyleFactory, QStyleOptionComboBox,
    QStyleOptionViewItem, QStyledItemDelegate,
)


class ComboPopupDelegate(QStyledItemDelegate):

    def set_colors(self, theme) -> None:
        self._text = QColor(theme.text_primary)
        self._panel = QColor(theme.panel_bg)
        border = QColor(theme.border)
        self._hover = border.lighter(128) if theme.mode == "dark" else border.darker(108)
        self._selected = QColor(theme.accent)
        self._selected.setAlpha(42)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        scale = self.parent()._scale
        hint.setHeight(max(round(32 * scale), option.fontMetrics.height() + round(8 * scale)))
        return hint

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        rect = opt.rect
        if not rect.isValid():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        painter.setBrush(self._selected if selected else self._hover if hovered else self._panel)
        painter.drawRect(rect)
        text_rect = rect.adjusted(max(2, round(rect.height() * .25)), 0,
                                  -max(2, round(rect.height() * .16)), 0)
        color = QColor(self._text)
        if not opt.state & QStyle.StateFlag.State_Enabled:
            color.setAlpha(145)
        painter.setPen(color)
        font = opt.font
        font.setBold(True)
        painter.setFont(font)
        text = opt.fontMetrics.elidedText(opt.text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


class ChevronComboBox(QComboBox):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._native_style = QStyleFactory.create("windows11") or QStyleFactory.create("windowsvista")
        if self._native_style is not None:
            self._native_style.setParent(self)
            self.setStyle(self._native_style)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._popup_delegate = ComboPopupDelegate(self)
        view = self.view()
        view.setUniformItemSizes(True)
        view.setAutoScroll(False)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setWordWrap(False)
        view.setTextElideMode(Qt.TextElideMode.ElideRight)
        view.setItemDelegate(self._popup_delegate)
        view.clicked.connect(self._select_clicked_index)
        view.viewport().setAutoFillBackground(True)
        popup = view.window()
        popup.setObjectName("trackindexComboPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        popup.installEventFilter(self)
        view.viewport().installEventFilter(self)
        from ..theme import get_theme
        self.set_theme(get_theme("dark"))

    def set_theme(self, theme, scale=1.0) -> None:
        self._scale = scale
        self.setFixedHeight(round(30 * scale))
        self._idle = QColor(theme.text_secondary)
        self._active = QColor(theme.text_primary)
        self._popup_border = QColor(theme.border)
        self._popup_delegate.set_colors(theme)
        view = self.view()
        radius = max(4, round(8 * scale))
        popup = view.window()
        popup.setStyleSheet(f"""
            QFrame#trackindexComboPopup {{ background: {theme.panel_bg};
                border: 1px solid {theme.border}; border-radius: {radius}px; }}
        """)
        view.setStyleSheet(f"QListView {{ background: {theme.panel_bg}; border: none; padding: 0; }}")
        popup.layout().setContentsMargins(0, 0, 0, 0)
        self._round_popup(popup)
        self._round_popup(view.viewport())
        palette = view.palette()
        for role, color in ((QPalette.ColorRole.Base, theme.panel_bg),
                            (QPalette.ColorRole.Text, theme.text_primary),
                            (QPalette.ColorRole.Highlight, theme.accent),
                            (QPalette.ColorRole.HighlightedText, theme.text_primary)):
            palette.setColor(role, QColor(color))
        view.setPalette(palette)
        view.viewport().setPalette(palette)
        view.doItemsLayout()
        view.viewport().update()
        self.update()

    def _round_popup(self, widget) -> None:
        radius = max(4, round(8 * self._scale))
        if widget is self.view().viewport():
            radius -= 1
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(widget.rect()), radius, radius)
        widget.setMask(QRegion(outline.toFillPolygon().toPolygon()))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and watched in (self.view().window(), self.view().viewport()):
            self._round_popup(watched)
        elif event.type() == QEvent.Type.Paint and watched is self.view().window():
            painter = QPainter(watched)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(self._popup_delegate._panel)
            painter.setPen(QPen(self._popup_border, 1))
            radius = max(4, round(8 * self._scale))
            painter.drawRoundedRect(QRectF(watched.rect()).adjusted(.5, .5, -.5, -.5), radius, radius)
            painter.end()
            return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        opened = self.view().isVisible()
        color = QColor(self._active if self.hasFocus() or opened else self._idle)
        if not self.isEnabled():
            color.setAlpha(145)
        painter.setPen(QPen(color, max(1.5, 2 * self._scale),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                          QStyle.SubControl.SC_ComboBoxArrow, self)
        if rect.isValid():
            center = QRectF(rect).center()
            x, y = center.x(), center.y()
            span = 6 * self._scale
            offset = 3 * self._scale * (-1 if opened else 1)
            chevron = QPainterPath()
            chevron.moveTo(x - span, y - offset)
            chevron.lineTo(x, y + offset)
            chevron.lineTo(x + span, y - offset)
            painter.drawPath(chevron)

    def showPopup(self) -> None:
        self.view().setMinimumWidth(0)
        self.view().setMaximumWidth(16777215)
        super().showPopup()
        self._round_popup(self.view().window())
        self._round_popup(self.view().viewport())
        self.update()

    def hidePopup(self) -> None:
        super().hidePopup()
        self.update()

    def _select_clicked_index(self, index) -> None:
        if (not self.view().isVisible() or not index.isValid()
                or not index.flags() & Qt.ItemFlag.ItemIsEnabled
                or not index.flags() & Qt.ItemFlag.ItemIsSelectable):
            return
        self.hidePopup()
        self.setCurrentIndex(index.row())
        self.activated.emit(self.currentIndex())
        self.textActivated.emit(self.currentText())

    def wheelEvent(self, event) -> None:
        event.ignore()
