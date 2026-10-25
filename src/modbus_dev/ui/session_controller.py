"""Own the worker lifecycle and its immutable per-session configuration."""
from .i18n import tr
from datetime import datetime
import json
from pathlib import Path
import tempfile
import uuid

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ..runtime import worker_command


class SessionController(QObject):
    busyChanged = Signal(bool)
    prepared = Signal(object, object)
    packetReceived = Signal(dict)
    stateChanged = Signal(str)
    message = Signal(str)
    failed = Signal(object)
    completed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self.process = None
        self.temporary = None
        self.last_output = None
        self.config = None
        self.result = None
        self.recording = None
        self.stderr = ""
        self.buffer = bytearray()
        self.forced = False
        self._finishing = False
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self.force_stop)

    def _set_busy(self, busy):
        self.busy = busy
        self.busyChanged.emit(busy)

    def start(self, config, parent):
        if self.busy:
            return
        self._set_busy(True)
        try:
            parent = Path(parent).expanduser().resolve()
            parent.mkdir(parents=True, exist_ok=True)
            directory = parent / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            self.temporary = tempfile.TemporaryDirectory(prefix="modbus-gui-")
            path = Path(self.temporary.name) / "config.json"
            path.write_text(json.dumps(config.to_mapping()), encoding="utf-8")
            self.last_output = directory
            self.config = config
            self.buffer.clear()
            self.stderr = ""
            self.result = None
            self.recording = None
            self.forced = False
            self._finishing = False
            if self.process:
                self.process.deleteLater()
            self.process = QProcess(self)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.readyReadStandardError.connect(self.read_error)
            self.process.finished.connect(self.finished)
            self.process.errorOccurred.connect(self.process_error)
            executable, arguments = worker_command(path, self.last_output)
            self.prepared.emit(config, self.last_output)
            self.stateChanged.emit("starting")
            self.process.start(executable, arguments)
        except Exception as exc:
            try:
                self._cleanup()
            except OSError as cleanup_error:
                self.message.emit(str(cleanup_error))
            self._set_busy(False)
            self.failed.emit(exc)

    def read_error(self):
        if self.process:
            chunk = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
            self.stderr = (self.stderr + chunk)[-16000:]

    def read_output(self):
        if not self.process:
            return
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            try:
                if len(line) > 262144:
                    raise ValueError(tr('Worker message exceeds the size limit'))
                packet = json.loads(line)
                if not isinstance(packet, dict) or packet.get("schema_version") != 1 or packet.get("type") not in ("status", "result"):
                    raise ValueError(tr('Incompatible worker protocol'))
                if packet["type"] == "result":
                    if not isinstance(packet.get("result"), dict):
                        raise ValueError(tr('Incompatible worker protocol'))
                    self.result = packet["result"]
                elif not isinstance(packet.get("state"), str):
                    raise ValueError(tr('Incompatible worker protocol'))
                self.packetReceived.emit(packet)
            except (ValueError, TypeError, AttributeError) as exc:
                self.message.emit(tr('Protocol error: {p0}').format(p0=exc))
                self.force_stop()
                return
        if len(self.buffer) > 262144:
            self.force_stop()

    def stop(self):
        if self.busy and self.process and self.process.state() != QProcess.NotRunning:
            if self.stop_timer.isActive():
                return
            self.stateChanged.emit("stopping")
            self.process.write(b'{"schema_version":1,"command":"stop"}\n')
            self.stop_timer.start(5000)

    def force_stop(self):
        if self.busy and self.process and self.process.state() != QProcess.NotRunning:
            self.forced = True
            self.message.emit(tr('The worker did not stop normally and was asked to terminate; result evidence may be incomplete.'))
            self.process.kill()

    def process_error(self, error):
        if error == QProcess.FailedToStart:
            self.stderr = self.process.errorString()
            self.finished(-1, QProcess.CrashExit)

    def _cleanup(self):
        temporary, self.temporary = self.temporary, None
        if temporary:
            temporary.cleanup()

    def finished(self, exit_code, exit_status):
        if not self.busy or self._finishing:
            return
        self._finishing = True
        self.stop_timer.stop()
        self.read_output()
        self.read_error()
        abnormal = (self.forced or self.result is None or exit_status == QProcess.CrashExit
                    or exit_code != self.result.get("exit_code"))
        if abnormal and self.last_output and self.last_output.is_dir():
            try:
                (self.last_output / "gui-worker-error.json").write_text(
                    json.dumps(dict(exit_code=exit_code, forced=self.forced, error=self.stderr,
                                    evidence_complete=False), ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                self.message.emit(tr('Cannot save the error record: {p0}').format(p0=exc))
        if self.last_output:
            try:
                self.recording = json.loads((self.last_output / "summary.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.recording = None
        try:
            self._cleanup()
        except OSError as exc:
            abnormal = True
            self.message.emit(str(exc))
        self.completed.emit(abnormal)
        # Unlock only after final output, result handling and temporary-file cleanup.
        self._set_busy(False)
        self._finishing = False
