"""轻量实时数值与趋势组件：数值 5Hz，曲线 2Hz，隐藏时停止曲线定时器。"""
from datetime import datetime, timedelta
import time

from PySide6.QtCore import QTimer, Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .domain.live_policy import CURVE_REFRESH_HZ, CURVE_WINDOW_S, VALUE_REFRESH_HZ
from .domain.trend import TrendHistory
from .gui_theme import COLORS
from .gui_widgets import CheckBox, label

SERIES_COLORS = ("#4269C7", "#338164", "#A87426", "#8455A6", "#248797", "#BD586B", "#787430", "#666CB0")
AREA_NAMES = {"holding": "保持", "input": "输入"}
QUALITY_NAMES = {"submitted": "已提交发送", "received": "已接收", "confirmed": "写入已确认",
                 "partial": "单寄存器 / 原始值", "ambiguous": "应答关联不确定",
                 "timeout": "应答超时", "exception": "异常应答", "bad_reply": "无效应答",
                 "readback-mismatch": "读回不一致", "nonfinite": "非有限 float32 / 原始值"}


class TrendPlot(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMinimumHeight(180)
        self.setAccessibleName("最近十秒实时趋势，TX 实线，RX 虚线")
        self.timer = QTimer(self)
        self.timer.setInterval(round(1000 / CURVE_REFRESH_HZ))
        self.timer.timeout.connect(self.tick)

    def sync_timer(self):
        if self.isVisible() and self.owner.running:
            self.timer.start()
        else:
            self.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_timer()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def tick(self):
        # 只在可见页抽取最新快照；缺失区间由趋势模型产生断点。
        if self.isVisible() and self.owner.running:
            self.owner.history.sample(self.owner.snapshot, self.owner.clock())
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["panel"]))
        bounds = QRectF(62, 16, max(10, self.width() - 82), max(10, self.height() - 50))
        painter.setPen(QPen(QColor(COLORS["line"]), 1))
        now = self.owner.clock()
        visible = {key: points for key, points in self.owner.history.series.items()
                   if self.owner.selected(key)}
        values = [value for points in visible.values() for at, value in points
                  if now - CURVE_WINDOW_S <= at <= now and value is not None]
        low, high = (min(values), max(values)) if values else (-1., 1.)
        padding = max((high - low) * .1, abs(high) * .02, .001)
        low, high = low - padding, high + padding
        for index in range(5):
            y = bounds.top() + bounds.height() * index / 4
            painter.setPen(QPen(QColor(COLORS["line"]), 1))
            painter.drawLine(QPointF(bounds.left(), y), QPointF(bounds.right(), y))
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(0, y - 10, 55, 20), Qt.AlignRight | Qt.AlignVCenter,
                             f"{high - (high - low) * index / 4:.4g}")
        for index in range(6):
            x = bounds.left() + bounds.width() * index / 5
            painter.drawText(QRectF(x - 20, bounds.bottom() + 8, 40, 22), Qt.AlignCenter,
                             f"{-(CURVE_WINDOW_S - index * 2):g}s")
        painter.save()
        painter.setClipRect(bounds)
        for key, points in visible.items():
            color, style = self.owner.series_style(key)
            painter.setPen(QPen(QColor(color), 2, style))
            path, connected = QPainterPath(), False
            for at, value in points:
                if at < now - CURVE_WINDOW_S or value is None:
                    connected = False
                    continue
                x = bounds.right() - (now - at) / CURVE_WINDOW_S * bounds.width()
                y = bounds.bottom() - (value - low) / (high - low) * bounds.height()
                if connected:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                connected = True
                painter.drawEllipse(QRectF(x - 1.5, y - 1.5, 3, 3))
            painter.drawPath(path)
        painter.restore()
        if not values:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(bounds, Qt.AlignCenter, "等待有效通信数值 · 不使用源计划值代替收发值")


class RealtimePanel(QWidget):
    def __init__(self, clock=time.monotonic):
        super().__init__()
        self.clock = clock
        self.snapshot = {}
        self.pending = None
        self.running = False
        self.history = TrendHistory()
        self.selectors = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(label("实时数值", "section"))
        heading.addStretch()
        heading.addWidget(label("数值 5Hz · 曲线 2Hz · 最近 10 秒", "caption"))
        layout.addLayout(heading)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("liveValues")
        self.table.setAccessibleName("实际收发数值")
        self.table.setHorizontalHeaderLabels(["测点 / 区域", "地址", "TX 发送值", "RX 接收值", "最近更新", "状态"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(84)
        self.table.setMaximumHeight(168)
        layout.addWidget(self.table)
        self.value_timer = QTimer(self)
        self.value_timer.setInterval(round(1000 / VALUE_REFRESH_HZ))
        self.value_timer.timeout.connect(self.refresh_values)
        self.curve_page = QWidget()
        curve_layout = QVBoxLayout(self.curve_page)
        curve_layout.setContentsMargins(0, 8, 0, 0)
        self.legend = QGridLayout()
        curve_layout.addLayout(self.legend)
        self.plot = TrendPlot(self)
        curve_layout.addWidget(self.plot, 1)
        note = label("TX 实线：已提交驱动 · RX 虚线：已解析数值 · 降频趋势不代表原始采样", "caption")
        note.setWordWrap(True)
        curve_layout.addWidget(note)

    def configure(self, config):
        self.snapshot, self.pending = {}, None
        self.history.clear()
        self.table.setRowCount(0)
        for checkbox in self.selectors.values():
            self.legend.removeWidget(checkbox)
            checkbox.hide()
            checkbox.deleteLater()
        self.selectors.clear()
        areas = (("input",) if config.master_function == 4 else ("holding",)) if config.mode == "rtu-master" else ("holding", "input")
        for index, point in enumerate(config.points):
            if not point.enabled:
                continue
            for area in areas:
                for direction in ("tx", "rx"):
                    key = f"{area}:{index}:{direction}"
                    checkbox = CheckBox(f"{point.name or f'测点 {index + 1}'} · {AREA_NAMES[area]} · {direction.upper()}")
                    if config.mode == "rtu-master":
                        used = config.usage.writing if direction == "tx" else not config.usage.writing or config.usage.readback
                    elif config.mode == "rtu-slave":
                        used = direction == "tx" or (area == "holding" and config.access_mode == "read-write")
                    else:
                        used = False  # TCP 当前未发布实时寄存器数值快照。
                    checkbox.setEnabled(used)
                    checkbox.setChecked(used)
                    if not used:
                        checkbox.setToolTip("当前操作不提供该方向的实时数值；原始通信记录仍保存到事件日志。")
                    color, _ = self.series_style(key)
                    checkbox.setStyleSheet(f"color: {color if used else COLORS['muted']};")
                    checkbox.toggled.connect(lambda _: self.plot.update())
                    self.legend.addWidget(checkbox, len(self.selectors) // 2, len(self.selectors) % 2)
                    self.selectors[key] = checkbox
        self.set_state("idle")

    def series_style(self, key):
        area, index, direction = key.split(":")
        color = SERIES_COLORS[int(index) * 2 + int(area == "input")]
        return color, Qt.SolidLine if direction == "tx" else Qt.DashLine

    def selected(self, key):
        return key in self.selectors and self.selectors[key].isEnabled() and self.selectors[key].isChecked()

    def ingest(self, snapshot):
        # 状态管道只保存最后一份，不将每个报文推入界面队列。
        self.pending = snapshot
        self.set_state(snapshot.get("state", "idle"))

    def set_state(self, state):
        self.running = state in ("starting", "running", "reconnecting")
        if self.snapshot:
            self.snapshot["state"] = state
        if self.running:
            if not self.value_timer.isActive():
                self.value_timer.start()
        else:
            self.value_timer.stop()
            self.refresh_values()
        self.plot.sync_timer()

    def refresh_values(self):
        if self.pending is not None:
            self.snapshot, self.pending = self.pending, None
        grouped = {}
        for row in self.snapshot.get("rows", []):
            grouped.setdefault((row["point"], row["area"]), {})[row["direction"]] = row
        self.table.setRowCount(len(grouped))
        self.table.setFixedHeight(max(84, self.table.horizontalHeader().height() + min(4, len(grouped)) * 30 + 4))
        now = self.clock()
        for index, (_, directions) in enumerate(sorted(grouped.items())):
            last = max(directions.values(), key=lambda row: row["sampled_s"])
            age = max(0, now - last["sampled_s"])
            quality = QUALITY_NAMES.get(last["quality"], last["quality"])
            stale = self.snapshot.get("state") != "running" or age > self.snapshot.get("stale_after_s", 1.)
            if stale:
                quality = "已停止 / 已过期" if not self.running else "已过期 / 等待数据"
            values = [f"{last['name']} · {AREA_NAMES[last['area']]}", str(last["address"]),
                      self.format_value(directions.get("tx")), self.format_value(directions.get("rx")),
                      (datetime.now() - timedelta(seconds=age)).strftime("%H:%M:%S.%f")[:-3], quality]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (2, 3):
                    row = directions.get("tx" if column == 2 else "rx")
                    if row:
                        item.setToolTip(f"FC{row['function']:02d} · {QUALITY_NAMES.get(row['quality'], row['quality'])}\n"
                                        f"原始寄存器：{' '.join(f'{word:04X}' for word in row['registers'])}\n"
                                        f"距今 {max(0, now - row['sampled_s']):.3f}s")
                        if now - row["sampled_s"] > self.snapshot.get("stale_after_s", 1.) or stale:
                            item.setForeground(QColor(COLORS["muted"]))
                self.table.setItem(index, column, item)

    @staticmethod
    def format_value(row):
        if row is None:
            return "—"
        if row["value"] is None:
            return " ".join(f"0x{word:04X}" for word in row["registers"])
        return f"{row['value']:.7g}"
