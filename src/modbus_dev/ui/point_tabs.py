"""紧凑测点页签；只切换现有编辑器，不重建表单或改变测点启用状态。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QTabBar, QVBoxLayout, QWidget

from .i18n import bind, tr
from .theme import COLORS, METRICS


class _PointTabBar(QTabBar):
    """为各数字提供相同的宽度基准，实际宽度由 Qt 按可用空间均分。"""

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        size.setWidth(METRICS.point_tab_min_width)
        return size

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)


class _PointPages(QFrame):
    """仅布局当前可见页，避免 Qt 堆叠布局把隐藏页的换行高度也计入。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self._current = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    def addWidget(self, page):
        self.layout().addWidget(page)
        first = self._current < 0
        page.setVisible(first)
        if first:
            self._current = 0

    def currentWidget(self):
        item = self.layout().itemAt(self._current)
        return item.widget() if item is not None else None

    def setCurrentIndex(self, index):
        if index == self._current or not 0 <= index < self.layout().count():
            return
        page = self.currentWidget()
        if page is not None:
            page.hide()
        self._current = index
        self.currentWidget().show()


class PointTabs(QWidget):
    currentChanged = Signal(int)

    def __init__(self, editors, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tab_bar = _PointTabBar()
        self.tab_bar.setObjectName("pointTabBar")
        bind(self.tab_bar, "setAccessibleName", tr("Points"))
        # 标签铺满内容区；取消 Qt 默认基线，避免数字右侧出现延伸横线。
        self.tab_bar.setExpanding(True)
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setUsesScrollButtons(False)
        self.tab_bar.setElideMode(Qt.ElideNone)
        self.pages = _PointPages()
        self.pages.setObjectName("pointPages")
        layout.addWidget(self.tab_bar)
        layout.addWidget(self.pages)
        for index, editor in enumerate(editors):
            self.pages.addWidget(editor)
            self.tab_bar.addTab(str(index + 1))
            self.tab_bar.setTabData(index, dict(enabled=False, error=False))
        self.tab_bar.currentChanged.connect(self._select_page)
        self._refresh_colors()

    def currentIndex(self):
        return self.tab_bar.currentIndex()

    def setCurrentIndex(self, index):
        self.tab_bar.setCurrentIndex(index)

    def _select_page(self, index):
        # 导航不发出配置编辑信号，未完成的输入原样留在各自控件中。
        self.pages.setCurrentIndex(index)
        self.pages.updateGeometry()
        self.updateGeometry()
        self._refresh_colors()
        self.currentChanged.emit(index)

    def update_point(self, index, *, enabled, error, tooltip):
        self.tab_bar.setTabData(index, dict(enabled=enabled, error=error))
        self.tab_bar.setTabToolTip(index, str(tooltip))
        self._set_color(index)

    def _refresh_colors(self):
        for index in range(self.tab_bar.count()):
            self._set_color(index)

    def _set_color(self, index):
        state = self.tab_bar.tabData(index)
        # 禁用测点仍可访问，用字色而不是禁用页签来区分；错误色优先于选中色。
        color = ("error" if state["error"] else "disabled_ink" if not state["enabled"]
                 else "accent" if index == self.currentIndex() else "ink")
        self.tab_bar.setTabTextColor(index, QColor(COLORS[color]))
