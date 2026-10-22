"""Optional PySide6 single-session workbench; communication stays in a worker."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import uuid

from PySide6.QtCore import QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QHeaderView,
)

from .application.session import check_capabilities
from .domain.config import TaskConfig, load_config


class Workbench(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Dev · 通信调试台")
        self.resize(1120, 830)
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
        self.base_config = TaskConfig(mode="tcp-slave", duration_s=0).to_mapping()
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(22, 18, 22, 18)
        title = QLabel("Modbus Dev")
        title.setStyleSheet("font-size: 25px; font-weight: 700; color: #142c46")
        outer.addWidget(title)
        subtitle = QLabel("单会话通信调试  /  RTU 主站 · RTU / TCP 只读从站")
        subtitle.setStyleSheet("color: #5a6b7d; margin-bottom: 10px")
        outer.addWidget(subtitle)
        splitter = QSplitter()
        outer.addWidget(splitter, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(410)
        self.editor = QWidget()
        form_layout = QVBoxLayout(self.editor)
        self.config_buttons = QWidget()
        config_actions = QHBoxLayout(self.config_buttons)
        config_actions.setContentsMargins(0, 0, 0, 0)
        for label, callback in (("加载配置", self.load), ("保存配置", self.save), ("校验", self.validate)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            config_actions.addWidget(button)
        form_layout.addWidget(self.config_buttons)

        def group(title, rows):
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setSpacing(9)
            for key, label, choices in rows:
                widget = QComboBox() if choices else QLineEdit()
                if choices:
                    widget.addItems(choices)
                widget.setObjectName(key)
                self.fields[key] = widget
                form.addRow(label, widget)
            form_layout.addWidget(box)
            return box

        group("会话与点表", [("mode", "通信模式", ["tcp-slave", "rtu-slave", "rtu-master"]),
                            ("policy", "运行策略", ["continuous", "strict"]),
                            ("unit", "站号", None), ("addresses", "float32 起始地址", None),
                            ("byte_order", "字节排列", ["B1234", "B4321", "B2143", "B3412"])])
        self.tcp_box = group("TCP 监听", [("host", "绑定地址", None), ("port", "端口", None)])
        self.serial_box = group("串口", [("serial.port", "端口名称", None), ("serial.baud", "波特率", None),
                                       ("serial.parity", "校验位", ["N", "E", "O"]),
                                       ("serial.stop_bits", "停止位", ["1", "2"])])
        self.rts = QCheckBox("驱动 RTS 切换（仅 Windows）")
        self.serial_box.layout().addRow(self.rts)
        refresh = QPushButton("查看可用串口")
        refresh.clicked.connect(self.ports)
        self.serial_box.layout().addRow(refresh)
        group("数据与时序", [("source.kind", "数据源", ["constant", "sine"]),
                            ("source.value", "常量 / 偏置", None), ("source.amplitude", "正弦振幅", None),
                            ("source.frequency_hz", "正弦频率 Hz", None), ("period_ms", "数据周期 ms", None),
                            ("timeout_ms", "I/O 超时 ms", None), ("duration_s", "时长 s（0 = 持续）", None)])
        self.master_box = group("主站读回与调度", [("readback_every", "每 N 周期读回（0 = 关闭）", None),
                                                ("read_function", "读回功能码", ["3", "4"]),
                                                ("schedule_tolerance_percent", "调度容差 %", None)])
        form_layout.addStretch()
        scroll.setWidget(self.editor)
        splitter.addWidget(scroll)
        right = QWidget()
        layout = QVBoxLayout(right)
        self.status = QLabel("就绪 · 尚未启动")
        self.status.setStyleSheet("background: #e8eff7; color: #14395e; padding: 15px; font-size: 17px; font-weight: 600")
        layout.addWidget(self.status)
        self.hint = QLabel("从站仅响应读取；数据时钟与主站轮询频率无关。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        location = QGroupBox("结果保存位置")
        location_layout = QVBoxLayout(location)
        self.output = QLineEdit(str(Path.cwd() / "results"))
        self.output.setObjectName("output")
        location_layout.addWidget(self.output)
        browse = QPushButton("选择父目录")
        browse.clicked.connect(self.choose_output)
        location_layout.addWidget(browse)
        self.output_box = location
        layout.addWidget(location)
        actions = QHBoxLayout()
        self.start_button = QPushButton("启动会话")
        self.start_button.setStyleSheet("QPushButton { background: #1768ac; color: white; font-weight: bold; padding: 10px; }"
                                       "QPushButton:disabled { background: #d5dce3; color: #697989; }")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止并保存")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.open_button = QPushButton("打开结果")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_output)
        for button in (self.start_button, self.stop_button, self.open_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.recording = QLabel("日志证据：未开始")
        self.recording.setWordWrap(True)
        layout.addWidget(self.recording)
        self.stats = QTableWidget(0, 2)
        self.stats.setHorizontalHeaderLabels(["统计项", "数值"])
        self.stats.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stats.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats.setMaximumHeight(220)
        layout.addWidget(self.stats)
        layout.addWidget(QLabel("运行日志 · 界面最多保留 2000 行，完整报文见结果目录"))
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        layout.addWidget(self.logs, 1)
        self.path_label = QLabel("每次运行自动创建独立目录，不覆盖已有结果。")
        self.path_label.setWordWrap(True)
        self.path_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.path_label)
        splitter.addWidget(right)
        splitter.setSizes([430, 650])
        self.setCentralWidget(container)
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self.force_stop)
        self.fields["mode"].currentTextChanged.connect(self.mode_changed)
        self.fields["source.kind"].currentTextChanged.connect(self.mode_changed)
        self.apply_config(TaskConfig.from_mapping(self.base_config))

    def text(self, key):
        field = self.fields[key]
        return field.currentText() if isinstance(field, QComboBox) else field.text().strip()

    def apply_config(self, config):
        check_capabilities(config)
        self.base_config = config.to_mapping()
        for key, field in self.fields.items():
            value = self.base_config
            for part in key.split("."):
                value = value[part]
            text = ", ".join(map(str, value)) if key == "addresses" else str(value)
            if isinstance(field, QComboBox):
                field.setCurrentText(text)
            else:
                field.setText(text)
        self.rts.setChecked(config.serial.rts_toggle)
        self.mode_changed()

    def configuration(self):
        data = json.loads(json.dumps(self.base_config))
        integers = {"unit", "port", "period_ms", "timeout_ms", "readback_every", "read_function", "serial.baud", "serial.stop_bits"}
        floats = {"duration_s", "schedule_tolerance_percent", "source.value", "source.amplitude", "source.frequency_hz"}
        for key in self.fields:
            value = self.text(key)
            try:
                if key == "addresses":
                    value = [int(item.strip()) for item in value.split(",")]
                elif key in integers:
                    value = int(value)
                elif key in floats:
                    value = float(value)
            except ValueError:
                raise ValueError(f"{key}: 请填写有效数字（地址用英文逗号分隔）") from None
            if "." in key:
                parent, name = key.split(".")
                data[parent][name] = value
            else:
                data[key] = value
        data["serial"]["rts_toggle"] = self.rts.isChecked()
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
        for key in ("source.amplitude", "source.frequency_hz"):
            self.fields[key].setEnabled(self.text("source.kind") == "sine")
        self.hint.setText("主站会使用 FC16 写入设备；请核对串口、站号与点表。" if mode == "rtu-master" else
                          "只读从站 · FC03/04 · 完整 float32 点 · 不接受写入")

    def error(self, error):
        self.status.setText("操作失败")
        self.logs.appendPlainText(str(error))
        QMessageBox.warning(self, "Modbus Dev", str(error))

    def validate(self):
        try:
            self.configuration()
            self.status.setText("配置有效 · 尚未打开通信端口")
        except Exception as exc:
            self.error(exc)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载配置", "", "JSON (*.json)")
        if path:
            try:
                self.apply_config(load_config(path))
                self.status.setText("配置已加载")
            except Exception as exc:
                self.error(exc)

    def save(self):
        try:
            config = self.configuration()
            path, _ = QFileDialog.getSaveFileName(self, "保存配置", "config.json", "JSON (*.json)")
            if path:
                Path(path).write_text(json.dumps(config.to_mapping(), ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("配置已保存")
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
            self.stats.setRowCount(0)
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
            self.status.setText("正在启动…")
            arguments = ["worker", str(path), "--output", str(self.last_output)]
            if not getattr(sys, "frozen", False):
                arguments = ["-m", "modbus_dev"] + arguments
            self.process.start(sys.executable, arguments)
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
        self.status.setText(states.get(packet["state"], packet["state"]))
        counters = packet.get("counters", {})
        self.show_counters(counters)
        recording = packet.get("recording", {})
        self.recording.setText(f"日志证据：{'完整' if recording.get('evidence_complete') else '未就绪 / 不完整'}  ·  "
                               f"丢失 {recording.get('dropped', 0)}  ·  界面预览丢失 {packet.get('preview_dropped', 0)}")

    def show_counters(self, counters):
        self.stats.setRowCount(len(counters))
        for row, (key, value) in enumerate(sorted(counters.items())):
            self.stats.setItem(row, 0, QTableWidgetItem(key))
            self.stats.setItem(row, 1, QTableWidgetItem(str(value)))

    def stop(self):
        if self.active():
            if self.stop_timer.isActive():
                return
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止并保存…")
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
            self.status.setText("异常结束 · 证据可能不完整")
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
            self.status.setText("已停止 · 用户请求" if code == 130 else "已完成" if code == 0 else "运行失败")
            self.logs.appendPlainText(json.dumps(self.result, ensure_ascii=False, indent=2))
            try:
                summary = json.loads((self.last_output / "summary.json").read_text(encoding="utf-8"))
                self.recording.setText("日志证据：" + ("完整" if summary.get("evidence_complete") else "不完整"))
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


def create_application():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet("QWidget { font-family: 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; font-size: 12px; }"
                      "QGroupBox { font-weight: 600; margin-top: 12px; padding-top: 14px; }"
                      "QGroupBox::title { subcontrol-origin: margin; left: 10px; }"
                      "QLineEdit, QComboBox { padding: 5px; min-height: 20px; } QPushButton { padding: 6px; }")
    return app


def main():
    app = create_application()
    window = Workbench()
    window.show()
    return app.exec()
