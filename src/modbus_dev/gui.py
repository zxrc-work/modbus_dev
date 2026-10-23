"""可选 PySide6 单会话工作台；通信留在工作进程，配置与日志状态共用业务模型。"""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from PySide6.QtCore import QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget, QHeaderView,
)

from .application.session import check_capabilities
from .domain.config import MAX_POINTS, PointConfig, TaskConfig, load_config, point_conflicts
from .gui_points import PointEditor
from .gui_theme import METRICS, application_palette, application_stylesheet, ui_font
from .gui_widgets import CheckBox, ComboBox, WorkbenchStyle, card, label, refresh_style
from .runtime import default_output_directory, worker_command


CHOICE_LABELS = {
    "tcp-slave": "TCP 只读从站", "rtu-slave": "RTU 只读从站", "rtu-master": "RTU 主站",
    "continuous": "连续运行", "strict": "严格模式", "constant": "常量", "sine": "正弦波",
}
COUNTER_LABELS = {
    "source_updates": "数据更新", "slave_request": "从站请求", "rx": "接收调用", "tx": "发送调用",
    "listener_opens": "监听打开", "port_opens": "串口打开", "open_errors": "打开失败",
    "io_errors": "I/O 错误", "write_attempts": "发送调用", "ack": "确认应答", "read": "读回应答",
    "ack_ambiguous": "关联不确定的确认", "read_ambiguous": "关联不确定的读回",
    "exception": "协议异常", "exception_ambiguous": "关联不确定的异常", "timeout": "应答超时",
    "schedule_late": "调度迟发", "schedule_missed": "错过交易预算", "skipped_slots": "跳过周期",
    "port_unavailable": "串口不可用", "readback_mismatch": "读回不一致",
}


class Workbench(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Dev · 通信调试台")
        # These are logical pixels. Qt handles per-monitor scaling; do not multiply by DPR.
        bounds = self.screen().availableGeometry()
        width, height = max(1, bounds.width() - 32), max(1, bounds.height() - 48)
        self.setMinimumSize(min(940, width), min(580, height))
        self.resize(min(METRICS.window_width, width), min(METRICS.window_height, height))
        self.process = None
        self.temporary = None
        self.buffer = bytearray()
        self.stderr = ""
        self.result = None
        self.forced = False
        self.closing = False
        self.last_output = None
        self.preview_dropped = 0
        self.fields = {}
        self.last_counters = {}
        self.counter_mode = None
        self.base_config = TaskConfig(mode="tcp-slave", duration_s=0).to_mapping()
        self._build_ui()
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self.force_stop)
        self.fields["mode"].currentIndexChanged.connect(self.mode_changed)
        for key in ("host", "port", "unit", "serial.port", "serial.baud"):
            self.fields[key].textChanged.connect(self.update_connection_summary)
        self.apply_config(TaskConfig.from_mapping(self.base_config))

    def _build_ui(self):
        container = QWidget()
        container.setObjectName("appRoot")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(*([METRICS.page_padding] * 4))
        outer.setSpacing(METRICS.page_spacing)
        header = QHBoxLayout()
        header.setSpacing(METRICS.section_spacing)
        mark = label("M")
        mark.setObjectName("brandMark")
        mark.setFixedSize(44, 44)
        mark.setAlignment(Qt.AlignCenter)
        header.addWidget(mark)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(label("Modbus Dev", "title"))
        heading.addWidget(label("通信调试工作台", "caption"))
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(label("RTU / TCP  ·  float32", "caption"))
        outer.addLayout(header)
        self.workspace = QSplitter(Qt.Horizontal)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.setHandleWidth(METRICS.page_spacing)
        self._build_editor()
        self._build_runtime()
        self.workspace.setStretchFactor(0, 0)
        self.workspace.setStretchFactor(1, 1)
        self.workspace.setSizes([430, 770])
        outer.addWidget(self.workspace, 1)
        self.setCentralWidget(container)

    def _build_editor(self):
        self.editor, form_layout = card()
        self.editor.setMinimumWidth(374)
        form_layout.addWidget(label("会话配置", "section"))
        form_layout.addWidget(label("配置设备连接、统一时序与四个独立测点。", "caption"))
        self.config_buttons = QWidget()
        config_actions = QHBoxLayout(self.config_buttons)
        config_actions.setContentsMargins(0, 0, 0, 0)
        config_actions.setSpacing(METRICS.spacing)
        for caption, callback in (("加载配置", self.load), ("保存配置", self.save), ("校验", self.validate)):
            button = QPushButton(caption)
            button.clicked.connect(callback)
            config_actions.addWidget(button)
        form_layout.addWidget(self.config_buttons)
        self.config_tabs = QTabWidget()
        form_layout.addWidget(self.config_tabs, 1)

        def page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            body.setObjectName("configPage")
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, METRICS.section_spacing, METRICS.spacing, METRICS.spacing)
            layout.setSpacing(METRICS.page_spacing)
            scroll.setWidget(body)
            self.config_tabs.addTab(scroll, title)
            return layout

        def group(layout, title, rows):
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setContentsMargins(0, METRICS.section_spacing, 0, 0)
            form.setHorizontalSpacing(METRICS.section_spacing)
            form.setVerticalSpacing(METRICS.spacing)
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            for key, caption, choices in rows:
                widget = ComboBox() if choices else QLineEdit()
                if choices:
                    for value in choices:
                        widget.addItem(CHOICE_LABELS.get(value, value), value)
                widget.setObjectName(key)
                widget.setAccessibleName(caption)
                self.fields[key] = widget
                form.addRow(caption, widget)
            layout.addWidget(box)
            return box

        connection = page("连接与时序")
        group(connection, "通信模式", [("mode", "模式", ["tcp-slave", "rtu-slave", "rtu-master"]),
                                      ("policy", "运行策略", ["continuous", "strict"])])
        self.tcp_box = group(connection, "TCP 监听", [("host", "绑定地址", None), ("port", "端口", None)])
        self.serial_box = group(connection, "串口连接", [("serial.port", "串口", None), ("serial.baud", "波特率", None),
                                                       ("serial.parity", "校验位", ["N", "E", "O"]),
                                                       ("serial.stop_bits", "停止位", ["1", "2"])])
        self.rts = CheckBox("驱动 RTS 切换（仅 Windows）")
        self.serial_box.layout().addRow(self.rts)
        refresh = QPushButton("查看可用串口")
        refresh.clicked.connect(self.ports)
        self.serial_box.layout().addRow(refresh)
        group(connection, "设备", [("unit", "站号", None)])
        group(connection, "统一运行时序", [("period_ms", "数据周期 ms", None), ("timeout_ms", "I/O 超时 ms", None),
                                ("duration_s", "运行时长 s", None)])
        self.fields["period_ms"].setToolTip("所有启用测点共用一个数据时钟，各点独立计算数值。")
        self.fields["timeout_ms"].setToolTip("主站为整周期交易预算；从站为单次写等待上限，不是轮询周期。")
        self.fields["duration_s"].setToolTip("0 表示持续运行，直到手动停止。")
        self.fields["duration_s"].setPlaceholderText("0 = 持续运行")
        self.master_box = group(connection, "主站读回与调度", [("readback_every", "读回间隔 / 周期", None),
                                                      ("read_function", "读回功能码", ["3", "4"]),
                                                      ("schedule_tolerance_percent", "调度容差 %", None)])
        self.fields["readback_every"].setToolTip("每 N 周期读回一次；0 表示关闭读回。")
        connection.addStretch()

        data = page("数据设置")
        group(data, "公共编码", [("byte_order", "字节序", ["B1234", "B4321", "B2143", "B3412"])])
        note = label("同一设备 · 每点独立数据源 · 共用周期与 float32 字节序", "caption")
        note.setWordWrap(True)
        data.addWidget(note)
        self.points_status = label("", "caption")
        self.points_status.setWordWrap(True)
        data.addWidget(self.points_status)
        # 按统一模型创建四个编辑区，不再用逗号字符串表达多个测点。
        self.point_editors = [PointEditor(i) for i in range(MAX_POINTS)]
        for editor in self.point_editors:
            data.addWidget(editor)
            editor.changed.connect(self.update_points_summary)
        data.addStretch()

        other = page("其他设置")
        retention = group(other, "日志保留", [("logging.max_files", "最多保留分片", None)])
        self.fields["logging.max_files"].setToolTip("1～10000，包含当前文件；每片不超过 32 MiB，超限删除最旧分片。")
        retention_note = label("每片最多 32 MiB；仅保留最新分片，淘汰历史后会标明证据不完整。", "caption")
        retention_note.setWordWrap(True)
        retention.layout().addRow(retention_note)
        other.addStretch()
        self.workspace.addWidget(self.editor)

    def _build_runtime(self):
        self.runtime_scroll = QScrollArea()
        self.runtime_scroll.setWidgetResizable(True)
        self.runtime_scroll.setMinimumWidth(430)
        self.runtime_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right = QWidget()
        right.setObjectName("runtimeContent")
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.page_spacing)
        control, controls = card()
        heading = QHBoxLayout()
        self.connection_title = label("TCP 只读从站", "section")
        heading.addWidget(self.connection_title)
        heading.addStretch()
        self.status = label("就绪 · 尚未启动")
        self.status.setObjectName("sessionStatus")
        heading.addWidget(self.status)
        controls.addLayout(heading)
        self.connection_summary = label("", "caption")
        self.connection_summary.setWordWrap(True)
        controls.addWidget(self.connection_summary)
        self.hint = label("")
        self.hint.setObjectName("sessionHint")
        self.hint.setWordWrap(True)
        controls.addWidget(self.hint)
        actions = QHBoxLayout()
        actions.setSpacing(METRICS.spacing)
        self.start_button = QPushButton("启动会话")
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止并保存")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.open_button = QPushButton("打开结果")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_output)
        for button in (self.start_button, self.stop_button, self.open_button):
            actions.addWidget(button, 1)
        controls.addLayout(actions)
        location = QWidget()
        location_layout = QVBoxLayout(location)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(6)
        location_layout.addWidget(label("结果父目录", "caption"))
        location_row = QHBoxLayout()
        location_row.setSpacing(METRICS.spacing)
        self.output = QLineEdit(str(default_output_directory()))
        self.output.setObjectName("output")
        self.output.setAccessibleName("结果父目录")
        location_row.addWidget(self.output, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self.choose_output)
        location_row.addWidget(browse)
        location_layout.addLayout(location_row)
        self.output_box = location
        controls.addWidget(location)
        layout.addWidget(control)
        metrics, metrics_layout = card()
        row = QHBoxLayout()
        row.setSpacing(METRICS.section_spacing)
        self.metric_labels, self.metric_values = [], []
        for _ in range(4):
            column = QVBoxLayout()
            column.setSpacing(3)
            value, caption = label("—", "metric"), label("", "caption")
            self.metric_values.append(value)
            self.metric_labels.append(caption)
            column.addWidget(value)
            column.addWidget(caption)
            row.addLayout(column, 1)
        metrics_layout.addLayout(row)
        layout.addWidget(metrics)
        monitor, monitor_layout = card()
        monitor_layout.addWidget(label("运行记录", "section"))
        self.monitor_tabs = QTabWidget()
        self.stats = QTableWidget(0, 2)
        self.stats.setHorizontalHeaderLabels(["统计项", "数值"])
        self.stats.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.stats.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.stats.verticalHeader().hide()
        self.stats.verticalHeader().setDefaultSectionSize(36)
        self.stats.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats.setSelectionBehavior(QTableWidget.SelectRows)
        self.stats.setShowGrid(False)
        self.logs = QPlainTextEdit()
        self.logs.setFont(ui_font(monospace=True))
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        self.logs.setMinimumHeight(140)
        self.logs.setPlaceholderText("启动会话后，运行日志将在这里显示。\n界面保留最近 2000 行，完整日志自动保存到结果目录。")
        self.monitor_tabs.addTab(self.logs, "运行日志")
        self.monitor_tabs.addTab(self.stats, "详细统计")
        monitor_layout.addWidget(self.monitor_tabs, 1)
        self.recording = label("日志证据：未开始", "caption")
        self.recording.setWordWrap(True)
        monitor_layout.addWidget(self.recording)
        self.path_label = label("每次运行自动创建独立目录。", "caption")
        self.path_label.setWordWrap(True)
        monitor_layout.addWidget(self.path_label)
        layout.addWidget(monitor, 1)
        self.runtime_scroll.setWidget(right)
        self.workspace.addWidget(self.runtime_scroll)

    def text(self, key):
        field = self.fields[key]
        return str(field.currentData()) if isinstance(field, QComboBox) else field.text().strip()

    def apply_config(self, config):
        config = TaskConfig.from_mapping(config.to_mapping())
        check_capabilities(config)
        self.base_config = config.to_mapping()
        for key, field in self.fields.items():
            value = self.base_config
            for part in key.split("."):
                value = value[part]
            text = str(value)
            if isinstance(field, QComboBox):
                field.setCurrentIndex(field.findData(text))
            else:
                field.setText(text)
        self.rts.setChecked(config.serial.rts_toggle)
        for editor, point in zip(self.point_editors, config.points):
            editor.apply_point(point)
        self.mode_changed()

    def configuration(self):
        data = json.loads(json.dumps(self.base_config))
        integers = {"unit", "port", "period_ms", "timeout_ms", "readback_every", "read_function",
                    "serial.baud", "serial.stop_bits", "logging.max_files"}
        floats = {"duration_s", "schedule_tolerance_percent"}
        for key in self.fields:
            value = self.text(key)
            try:
                if key in integers:
                    value = int(value)
                elif key in floats:
                    value = float(value)
            except ValueError:
                raise ValueError(f"{self.fields[key].accessibleName()}: 请填写有效数字") from None
            if "." in key:
                parent, name = key.split(".")
                data[parent][name] = value
            else:
                data[key] = value
        data["serial"]["rts_toggle"] = self.rts.isChecked()
        data["points"] = [editor.configuration() for editor in self.point_editors]
        if data["mode"] != "rtu-master":
            data["readback_every"] = 0
        config = TaskConfig.from_mapping(data)
        check_capabilities(config)
        return config

    def mode_changed(self, *_):
        mode = self.text("mode")
        self.tcp_box.setVisible(mode == "tcp-slave")
        self.serial_box.setVisible(mode != "tcp-slave")
        self.master_box.setVisible(mode == "rtu-master")
        self.hint.setText("主站会使用 FC16 写入设备；请核对串口、站号与点表。" if mode == "rtu-master" else
                          "只读从站 · FC03/04 · 完整 float32 点 · 不接受写入")
        self.hint.setProperty("writing", mode == "rtu-master")
        refresh_style(self.hint)
        self.update_connection_summary()
        self.update_metrics()

    def update_points_summary(self):
        # 即时检查只读取启用和地址，避免用户正在输入波形参数时弹出错误。
        points = [PointConfig(enabled=editor.enabled.isChecked(), address=editor.fields["address"].value())
                  for editor in self.point_editors]
        conflicts = point_conflicts(points)
        overlapping = {index for pair in conflicts for index in pair}
        for i, editor in enumerate(self.point_editors):
            editor.set_conflict(i in overlapping)
        count = sum(point.enabled for point in points)
        if conflicts:
            text = "；".join(f"测点 {a + 1} 与测点 {b + 1} 地址重叠" for a, b in conflicts)
        else:
            text = f"已启用 {count} / {MAX_POINTS} 个测点" if count else "请至少启用一个测点"
        self.points_status.setText(text)
        self.points_status.setProperty("tone", "error" if conflicts or not count else "idle")
        refresh_style(self.points_status)

    def update_connection_summary(self, *_):
        mode = self.text("mode")
        self.connection_title.setText(CHOICE_LABELS.get(mode, mode))
        endpoint = (f"{self.text('host')}:{self.text('port')}" if mode == "tcp-slave" else
                    f"{self.text('serial.port') or '未选择串口'}  ·  {self.text('serial.baud')} baud")
        self.connection_summary.setText(f"{endpoint}  ·  站号 {self.text('unit')}")

    def set_status(self, text, tone="idle"):
        self.status.setText(text)
        self.status.setProperty("tone", tone)
        refresh_style(self.status)

    def error(self, error):
        self.set_status("操作失败", "error")
        self.monitor_tabs.setCurrentIndex(0)
        self.logs.appendPlainText(str(error))
        QMessageBox.warning(self, "Modbus Dev", str(error))

    def validate(self):
        try:
            self.configuration()
            self.set_status("配置有效 · 尚未连接", "success")
        except Exception as exc:
            self.error(exc)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载配置", "", "JSON (*.json)")
        if path:
            try:
                self.apply_config(load_config(path))
                self.set_status("配置已加载", "success")
            except Exception as exc:
                self.error(exc)

    def save(self):
        try:
            config = self.configuration()
            path, _ = QFileDialog.getSaveFileName(self, "保存配置", "config.json", "JSON (*.json)")
            if path:
                Path(path).write_text(json.dumps(config.to_mapping(), ensure_ascii=False, indent=2), encoding="utf-8")
                self.set_status("配置已保存", "success")
        except Exception as exc:
            self.error(exc)

    def ports(self):
        from serial.tools.list_ports import comports
        try:
            ports = list(comports())
            QMessageBox.information(self, "可用串口", "\n".join(f"{p.device} — {p.description}" for p in ports) or "未发现串口")
        except Exception as exc:
            self.error(exc)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择结果父目录", self.output.text())
        if path:
            self.output.setText(path)

    def open_output(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def active(self):
        return self.process is not None and self.process.state() != QProcess.NotRunning

    def set_running(self, running):
        self.editor.setEnabled(not running)
        self.output_box.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.start_button.setText("会话进行中" if running else "启动会话")
        self.stop_button.setEnabled(running)

    def start(self):
        if self.active():
            return
        try:
            config = self.configuration()
            if not self.output.text().strip():
                raise ValueError("请选择结果父目录")
            parent = Path(self.output.text()).expanduser().resolve()
            parent.mkdir(parents=True, exist_ok=True)
            self.last_output = parent / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            self.temporary = tempfile.TemporaryDirectory(prefix="modbus-gui-")
            path = Path(self.temporary.name) / "config.json"
            path.write_text(json.dumps(config.to_mapping()), encoding="utf-8")
            self.buffer.clear()
            self.stderr = ""
            self.result = None
            self.preview_dropped = 0
            self.forced = False
            self.logs.clear()
            self.counter_mode = config.mode
            self.show_counters({})
            self.monitor_tabs.setCurrentIndex(0)
            self.recording.setText("日志证据：等待工作进程")
            self.path_label.setText(str(self.last_output))
            self.open_button.setEnabled(True)
            if self.process:
                self.process.deleteLater()
            self.process = QProcess(self)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.readyReadStandardError.connect(self.read_error)
            self.process.finished.connect(self.finished)
            self.process.errorOccurred.connect(self.process_error)
            self.set_running(True)
            self.set_status("正在启动…", "active")
            executable, arguments = worker_command(path, self.last_output)
            # QProcess 默认不为 Windows 子进程创建新控制台；保留 stdin/stdout/stderr 管道。
            self.process.start(executable, arguments)
        except Exception as exc:
            self.set_running(False)
            if self.temporary:
                self.temporary.cleanup()
                self.temporary = None
            self.error(exc)

    def read_error(self):
        chunk = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self.stderr = (self.stderr + chunk)[-16000:]

    def read_output(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            try:
                if len(line) > 262144:
                    raise ValueError("工作进程消息超出长度限制")
                packet = json.loads(line)
                if packet.get("schema_version") != 1 or packet.get("type") not in ("status", "result"):
                    raise ValueError("工作进程协议不兼容")
                self.display(packet)
            except (ValueError, TypeError, AttributeError) as exc:
                self.logs.appendPlainText(f"协议错误：{exc}")
                self.force_stop()
                return
        if len(self.buffer) > 262144:
            self.force_stop()

    def display(self, packet):
        dropped = packet.get("preview_dropped", 0)
        if dropped > self.preview_dropped:
            self.logs.appendPlainText(f"界面日志预览累计丢失 {dropped} 条；完整证据以结果目录为准。")
            self.preview_dropped = dropped
        for event in packet.get("events", [])[-100:]:
            self.logs.appendPlainText(f"[{event['level']}] {event['code']} · {event['message']}")
        if packet["type"] == "result":
            self.result = packet["result"]
            self.show_counters(self.result.get("counters", {}))
            return
        states = {"idle": "准备中", "starting": "启动中", "running": "运行中", "reconnecting": "等待重连",
                  "stopping": "正在停止", "stopped": "已停止", "failed": "运行失败"}
        state = packet["state"]
        tone = {"running": "active", "starting": "active", "reconnecting": "warning",
                "stopping": "warning", "failed": "error"}.get(state, "idle")
        self.set_status(states.get(state, state), tone)
        counters = packet.get("counters", {})
        self.show_counters(counters)
        recording = packet.get("recording", {})
        self.show_recording(recording)

    def show_recording(self, recording):
        """历史淘汰、队列丢失和界面预览丢失分别显示，不能把滚动历史当完整会话。"""
        segments = recording.get("segments", {})
        retained = segments.get("retained_files", 0)
        deleted = segments.get("deleted_files", 0)
        state = "完整" if recording.get("evidence_complete") else "未就绪 / 不完整"
        if recording.get("retention_truncated"):
            state = f"不完整 · 历史已淘汰 {deleted} 片，保留最新 {retained} 片"
        self.recording.setText(f"日志证据：{state}  ·  队列丢失 {recording.get('dropped', 0)}  ·  "
                               f"界面预览丢失 {self.preview_dropped}")

    def show_counters(self, counters):
        self.last_counters = dict(counters)
        self.stats.setRowCount(len(counters))
        for row, (key, value) in enumerate(sorted(counters.items())):
            item = QTableWidgetItem(COUNTER_LABELS.get(key, key))
            item.setToolTip(key)
            self.stats.setItem(row, 0, item)
            self.stats.setItem(row, 1, QTableWidgetItem(str(value)))
        self.update_metrics()

    def update_metrics(self):
        mode = self.counter_mode or self.text("mode")
        keys = (("write_attempts", "ack", "read", "schedule_late") if mode == "rtu-master" else
                ("source_updates", "slave_request", "rx", "tx"))
        for key, caption, value in zip(keys, self.metric_labels, self.metric_values):
            caption.setText(COUNTER_LABELS[key])
            caption.setToolTip(key)
            value.setText(str(self.last_counters.get(key, 0)) if self.last_counters else "—")
            value.setToolTip(f"{COUNTER_LABELS[key]} ({key})")

    def stop(self):
        if self.active():
            if self.stop_timer.isActive():
                return
            self.stop_button.setEnabled(False)
            self.set_status("正在停止并保存…", "warning")
            self.process.write(b'{"schema_version":1,"command":"stop"}\n')
            self.stop_timer.start(5000)

    def force_stop(self):
        if self.active():
            self.forced = True
            self.logs.appendPlainText("工作进程未正常结束，已请求强制终止；结果证据可能不完整。")
            self.process.kill()

    def process_error(self, error):
        if error == QProcess.FailedToStart:
            self.stderr = self.process.errorString()
            self.finished(-1, QProcess.CrashExit)

    def finished(self, exit_code, exit_status):
        self.stop_timer.stop()
        self.read_output()
        self.read_error()
        abnormal = (self.forced or self.result is None or exit_status == QProcess.CrashExit
                    or exit_code != self.result.get("exit_code"))
        if abnormal:
            self.set_status("异常结束 · 证据可能不完整", "error")
            self.monitor_tabs.setCurrentIndex(0)
            self.recording.setText("日志证据：工作进程异常退出，请检查结果文件")
            if self.last_output and self.last_output.is_dir():
                try:
                    (self.last_output / "gui-worker-error.json").write_text(
                        json.dumps(dict(exit_code=exit_code, forced=self.forced, error=self.stderr,
                                        evidence_complete=False), ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    self.logs.appendPlainText(f"无法保存异常记录：{exc}")
        else:
            code = self.result.get("exit_code")
            self.set_status("已停止 · 用户请求" if code == 130 else "已完成" if code == 0 else "运行失败",
                            "success" if code == 0 else "idle" if code == 130 else "error")
            if code not in (0, 130):
                self.monitor_tabs.setCurrentIndex(0)
            self.logs.appendPlainText(json.dumps(self.result, ensure_ascii=False, indent=2))
            try:
                summary = json.loads((self.last_output / "summary.json").read_text(encoding="utf-8"))
                self.show_recording(summary)
            except (OSError, ValueError):
                self.recording.setText("日志证据：无摘要文件")
        if self.stderr:
            self.logs.appendPlainText(self.stderr)
        self.set_running(False)
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
        if self.closing:
            self.close()

    def closeEvent(self, event):
        if self.active():
            self.closing = True
            self.stop()
            event.ignore()
        else:
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "workspace"):
            return
        orientation = Qt.Vertical if self.width() < 920 else Qt.Horizontal
        if self.workspace.orientation() != orientation:
            self.workspace.setOrientation(orientation)
            self.editor.setMinimumHeight(220 if orientation == Qt.Vertical else 0)
            self.runtime_scroll.setMinimumHeight(220 if orientation == Qt.Vertical else 0)
            self.workspace.setSizes([300, 450] if orientation == Qt.Vertical else [430, 770])


def create_application():
    existing = QApplication.instance()
    if existing is not None:
        return existing
    # Use pixel-aligned Windows text rendering. Respect an
    # explicit platform (e.g. offscreen) and never disable Qt's high-DPI scaling.
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")
    if "QT_SCALE_FACTOR_ROUNDING_POLICY" not in os.environ:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ModbusDev")
    app.setApplicationName("Modbus Dev")
    app.setStyle(WorkbenchStyle())
    app.setPalette(application_palette())
    font = ui_font()
    if sys.platform == "win32":
        font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)
    app.setStyleSheet(application_stylesheet())
    return app


def main():
    app = create_application()
    window = Workbench()
    window.show()
    return app.exec()
