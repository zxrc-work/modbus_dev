"""Small shared controls; native Qt selection and keyboard behavior are retained."""
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QFrame, QLabel, QListView, QProxyStyle, QStyle,
    QStyledItemDelegate, QStyleOptionComboBox, QStyleOptionViewItem, QVBoxLayout,
)

from .gui_theme import COLORS, METRICS


class WorkbenchStyle(QProxyStyle):
    def __init__(self):
        super().__init__("Fusion")

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class _ItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        item = QStyleOptionViewItem(option)
        item.state &= ~QStyle.State_HasFocus
        super().paint(painter, item, index)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 34))


class _PopupSurface(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Paint:
            painter = QPainter(watched)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor(COLORS['line']), 1))
            painter.setBrush(QColor(COLORS['panel']))
            painter.drawRoundedRect(watched.rect().adjusted(0, 0, -1, -1), METRICS.radius, METRICS.radius)
            painter.end()
            return True
        return super().eventFilter(watched, event)


class ComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        view = QListView(self)
        view.setItemDelegate(_ItemDelegate(view))
        view.setMouseTracking(True)
        self.setView(view)
        self.setMaxVisibleItems(10)
        self.setMinimumContentsLength(8)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = COLORS['muted'] if self.isEnabled() else COLORS['disabled_ink']
        painter.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        center = arrow.center()
        painter.drawPolyline([center + QPoint(-4, -2), center + QPoint(0, 2), center + QPoint(4, -2)])
        painter.end()

    def showPopup(self):
        popup = self.view().window()
        if not hasattr(popup, '_workbench_surface'):
            popup.setWindowFlag(Qt.FramelessWindowHint, True)
            popup.setWindowFlag(Qt.NoDropShadowWindowHint, True)
            popup.setAttribute(Qt.WA_TranslucentBackground, True)
            popup.setContentsMargins(4, 4, 4, 4)
            if isinstance(popup, QFrame):
                popup.setFrameShape(QFrame.NoFrame)
            popup._workbench_surface = _PopupSurface(popup)
            popup.installEventFilter(popup._workbench_surface)
        super().showPopup()
        bounds = self.screen().availableGeometry()
        anchor = self.mapToGlobal(QPoint(0, self.height() + 4))
        x = max(bounds.left(), min(anchor.x(), bounds.right() - popup.width() + 1))
        y = anchor.y()
        if y + popup.height() > bounds.bottom() + 1:
            y = self.mapToGlobal(QPoint(0, -popup.height() - 4)).y()
        popup.move(x, max(bounds.top(), y))


def label(text, role=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if role:
        widget.setProperty('role', role)
    return widget


def card():
    frame = QFrame()
    frame.setProperty('card', True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*([METRICS.card_padding] * 4))
    layout.setSpacing(METRICS.section_spacing)
    return frame, layout


def refresh_style(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
