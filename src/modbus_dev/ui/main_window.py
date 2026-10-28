"""组合配置和监视界面，统一管理工作进程的生命周期。"""
from .i18n import tr, bind, retranslate_tree
import json
from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QMainWindow, QMessageBox, QSplitter, QVBoxLayout, QWidget
from ..domain.config import TaskConfig, load_config
from .configuration_panel import ConfigurationPanel
from .monitor_panel import MonitorPanel
from .session_controller import SessionController
from .theme import METRICS
from .widgets import label
from .errors import format_error
from .settings_controller import SettingsController


class Workbench(QMainWindow):
    def __init__(self, *, remember_settings=True):
        super().__init__()
        bind(self, 'setWindowTitle', tr('Modbus Dev · Communication Workbench'))
        bounds = self.screen().availableGeometry()
        width, height = max(1, bounds.width() - 32), max(1, bounds.height() - 48)
        self.setMinimumSize(min(940, width), min(580, height))
        self.resize(min(METRICS.window_width, width), min(METRICS.window_height, height))
        self.closing = False
        self.controller = SessionController(self)
        self.language_manager = QApplication.instance().language_manager
        self._build_ui()
        self.settings = SettingsController(QApplication.instance().preference_store, self.language_manager,
                                          busy=self.active, capture=self.capture_settings, restore=self.restore_settings,
                                          remember=remember_settings, parent=self)
        self.settings.failed.connect(self.settings_failed)
        self.settings.saved.connect(lambda: self.set_status(tr("Configuration saved automatically"), "success"))
        self._sync_language()
        self.editor.language.currentIndexChanged.connect(self._language_selected)
        self.language_manager.changed.connect(self.retranslate_ui)
        self.editor.changed.connect(self.update_connection_summary)
        self.editor.validityChanged.connect(self.refresh_start_availability)
        self.editor.loadRequested.connect(self.load)
        self.editor.saveRequested.connect(self.save)
        self.editor.validateRequested.connect(self.validate)
        self.monitor.startRequested.connect(self.start)
        self.monitor.stopRequested.connect(self.stop)
        self.monitor.openRequested.connect(self.open_output)
        self.monitor.browseRequested.connect(self.choose_output)
        self.controller.busyChanged.connect(self.set_running)
        self.controller.prepared.connect(self.prepare_session)
        self.controller.packetReceived.connect(self.display)
        self.controller.stateChanged.connect(self.worker_state)
        self.controller.message.connect(self.logs.appendPlainText)
        self.controller.failed.connect(self.error)
        self.controller.completed.connect(self.session_completed)
        self.apply_config(TaskConfig(mode="tcp-slave", access_mode="read-only", duration_s=0), persist=False)
        try:
            self.settings.restore()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.logs.appendPlainText(tr("Saved settings could not be restored: {detail}").format(detail=str(exc)))
        self.editor.edited.connect(self.settings.save)
        self.output.textChanged.connect(lambda *_: self.settings.save())
        if self.settings.store.warning:
            self.logs.appendPlainText(tr("Saved settings could not be restored: {detail}").format(detail=self.settings.store.warning))
        if self.language_manager.warning:
            self.logs.appendPlainText(tr("Translations could not be loaded; using English. {detail}").format(detail=self.language_manager.warning))

    def _build_editor(self):
        self.editor = ConfigurationPanel()
        self.workspace.addWidget(self.editor)
        for name in ("fields", "config_tabs", "config_buttons", "point_editors", "point_tabs", "tcp_box", "serial_box",
                     "access_box", "master_box", "timing_box", "rts", "data_note", "points_status"):
            setattr(self, name, getattr(self.editor, name))

    def _build_runtime(self):
        self.monitor = MonitorPanel()
        self.workspace.addWidget(self.monitor)
        for name in ("runtime_scroll", "connection_title", "connection_summary", "hint", "status", "start_button",
                     "stop_button", "open_button", "output", "output_box", "live_panel", "monitor_tabs", "stats", "logs",
                     "recording", "path_label", "metric_labels", "metric_values"):
            setattr(self, name, getattr(self.monitor, name))

    @property
    def process(self):
        return self.controller.process

    @property
    def temporary(self):
        return self.controller.temporary

    @property
    def result(self):
        return self.controller.result

    @property
    def forced(self):
        return self.controller.forced

    @property
    def last_output(self):
        return self.controller.last_output

    @property
    def stderr(self):
        return self.controller.stderr

    @property
    def counter_mode(self):
        return self.monitor.counter_mode

    @counter_mode.setter
    def counter_mode(self, value):
        self.monitor.counter_mode = value

    @property
    def last_counters(self):
        return self.monitor.last_counters

    def active(self):
        return self.controller.busy

    def _sync_language(self):
        with QSignalBlocker(self.editor.language):
            self.editor.language.setCurrentIndex(self.editor.language.findData(self.language_manager.mode))

    def _language_selected(self, *_):
        self.set_language(self.editor.language.currentData())

    def set_language(self, mode):
        if self.active():
            self._sync_language()
            return False
        changed = self.settings.change_language(mode)
        self._sync_language()
        return changed

    def capture_settings(self):
        return dict(configuration=self.editor.capture_draft(), output_directory=self.output.text())

    def restore_settings(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("output_directory"), str):
            raise ValueError("Invalid saved session settings")
        self.editor.restore_draft(state["configuration"])
        bind(self.output, "setText", state["output_directory"])
        try:
            preview = self.configuration()
        except ValueError:
            preview = TaskConfig.from_mapping(self.editor.base_config)
        self.live_panel.configure(preview)
        self.update_connection_summary()

    def settings_failed(self, error):
        self.error(tr("Configuration changes were not saved: {detail}").format(detail=str(error)))

    def retranslate_ui(self):
        retranslate_tree(self)
        self.editor.retranslate_ui()
        self.monitor.retranslate_ui()
        self.update_connection_summary()
        self._sync_language()

    def set_running(self, running):
        self.editor.set_busy(running)
        self.monitor.set_running(running)
        if not running:
            self.editor.update_budget()
        if not running and self.closing:
            self.close()

    def refresh_start_availability(self, valid):
        self.start_button.setEnabled(bool(valid) and not self.active())

    def text(self, key):
        return self.editor.text(key)

    def configuration(self):
        return self.editor.configuration()

    def apply_config(self, config, *, persist=True):
        if self.active():
            return False
        self.editor.apply_config(config)
        self.live_panel.configure(config)
        self.update_connection_summary()
        if persist:
            return self.settings.save()
        return True

    def update_connection_summary(self, *_):
        self.monitor.update_connection_summary(self.editor)
        self.monitor.update_hint(self.editor.parameter_usage(), self.text("access_mode") == "read-only")
        self.monitor.update_metrics()

    def set_status(self, text, tone="idle"):
        self.monitor.set_status(text, tone)

    def show_counters(self, counters):
        self.monitor.show_counters(counters)

    def show_recording(self, recording):
        self.monitor.show_recording(recording)

    def display(self, packet):
        self.monitor.display(packet)

    def start(self):
        if self.active():
            return
        try:
            config = self.configuration()
            if not self.output.text().strip():
                raise ValueError(tr('Select a results parent directory'))
            self.controller.start(config, self.output.text())
        except Exception as exc:
            self.error(exc)

    def prepare_session(self, config, directory):
        self.monitor.result = None
        self.monitor.counter_mode = config.mode
        self.monitor.preview_dropped = 0
        self.logs.clear()
        self.live_panel.configure(config)
        self.live_panel.set_state("starting")
        self.show_counters({})
        self.monitor_tabs.setCurrentIndex(0)
        bind(self.recording, 'setText', tr('Log evidence: waiting for worker'))
        bind(self.path_label, 'setText', str(directory))
        self.open_button.setEnabled(True)

    def worker_state(self, state):
        if state == "starting":
            self.set_status(tr('Starting…'), "active")
        elif state == "stopping":
            self.stop_button.setEnabled(False)
            self.set_status(tr('Stopping and saving…'), "warning")

    def stop(self):
        self.controller.stop()

    def force_stop(self):
        self.controller.force_stop()


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
        heading.addWidget(label(tr('Communication workbench'), "caption"))
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(label("RTU / TCP  ·  16 / 32-bit", "caption"))
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


    def error(self, error):
        self.set_status(tr('Operation failed'), "error")
        self.monitor_tabs.setCurrentIndex(0)
        message = str(format_error(error))
        self.logs.appendPlainText(message)
        QMessageBox.warning(self, "Modbus Dev", message)


    def validate(self):
        if self.active():
            return
        try:
            self.configuration()
            self.set_status(tr('Configuration valid · Not connected'), "success")
        except Exception as exc:
            self.error(exc)


    def load(self):
        if self.active():
            return
        path, _ = QFileDialog.getOpenFileName(self, tr('Import Config'), "", "JSON (*.json)")
        if path and not self.active():
            try:
                if self.apply_config(load_config(path)):
                    self.set_status(tr('Config imported'), "success")
            except Exception as exc:
                self.error(exc)


    def save(self):
        if self.active():
            return
        try:
            config = self.configuration()
            path, _ = QFileDialog.getSaveFileName(self, tr('Export Config'), "config.json", "JSON (*.json)")
            if path and not self.active():
                self.settings.export_configuration(path, config)
                self.set_status(tr('Config exported'), "success")
        except Exception as exc:
            self.error(exc)


    def choose_output(self):
        if self.active():
            return
        path = QFileDialog.getExistingDirectory(self, tr('Select the results parent directory'), self.output.text())
        if path and not self.active():
            bind(self.output, 'setText', path)


    def open_output(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))


    def session_completed(self, abnormal):
        if abnormal:
            self.set_status(tr('Abnormal exit · Evidence may be incomplete'), "error")
            self.monitor_tabs.setCurrentIndex(0)
            bind(self.recording, 'setText', tr('Log evidence: worker exited abnormally; check the result files'))
        else:
            code = self.result.get("exit_code")
            self.set_status(tr('Stopped · User request') if code == 130 else tr('Completed') if code == 0 else tr('Failed'),
                            "success" if code == 0 else "idle" if code == 130 else "error")
            if code not in (0, 130):
                self.monitor_tabs.setCurrentIndex(0)
            self.logs.appendPlainText(json.dumps(self.result, ensure_ascii=False, indent=2))
            if self.controller.recording is not None:
                self.show_recording(self.controller.recording)
            else:
                bind(self.recording, 'setText', tr('Log evidence: no summary file'))
        if self.stderr:
            self.logs.appendPlainText(self.stderr)
        self.live_panel.set_state("failed" if abnormal else "stopped")

    def closeEvent(self, event):
        if self.active():
            self.closing = True
            self.stop()
            event.ignore()
        else:
            self.live_panel.set_state("stopped")
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
