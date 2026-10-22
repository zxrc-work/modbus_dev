"""Worker subprocess protocol and optional offscreen Qt lifecycle verification."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event
from modbus_dev.worker import EventPreview


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class WorkerTests(unittest.TestCase):
    def test_preview_is_bounded_and_counts_loss(self):
        preview = EventPreview()
        for i in range(120):
            preview.add(Event("failure", str(i), "WARNING"))
        events, dropped = preview.drain()
        self.assertEqual(len(events), 100)
        self.assertEqual(dropped, 20)
        self.assertEqual(events[0]["message"], "20")

    def exercise(self, duration, command=None, close_input=False, occupied=False):
        with tempfile.TemporaryDirectory() as root, socket.socket() as reserved:
            if occupied:
                reserved.bind(("127.0.0.1", 0))
                reserved.listen()
                port = reserved.getsockname()[1]
            else:
                port = available_port()
            config = Path(root) / "input.json"
            config.write_text(json.dumps(dict(mode="tcp-slave", port=port, duration_s=duration)))
            process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(config),
                                        "--output", str(Path(root) / "run")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            records = queue.Queue()
            def read():
                for line in process.stdout:
                    records.put(json.loads(line))
            reader = threading.Thread(target=read)
            reader.start()
            try:
                first = records.get(timeout=5)
                self.assertEqual(first["schema_version"], 1)
                if command:
                    process.stdin.write(json.dumps(command) + "\n")
                    process.stdin.flush()
                if close_input:
                    process.stdin.close()
                process.wait(6)
                reader.join(2)
                packets = [first]
                while not records.empty():
                    packets.append(records.get_nowait())
                terminal = packets[-1]
                self.assertEqual(terminal["type"], "result")
                self.assertEqual(process.returncode, terminal["result"]["exit_code"])
                return terminal["result"]
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(3)
                reader.join(2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_natural_completion(self):
        result = self.exercise(.25)
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(result["counters"]["source_updates"], 0)

    def test_stop_command_and_parent_pipe_closure(self):
        self.assertEqual(self.exercise(0, command=dict(schema_version=1, command="stop"))["exit_code"], 130)
        self.assertEqual(self.exercise(0, close_input=True)["exit_code"], 130)

    def test_start_failure_delivers_terminal_result(self):
        self.assertEqual(self.exercise(.25, occupied=True)["exit_code"], 1)

    def test_unread_status_pipe_does_not_block_session_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps(dict(mode="tcp-slave", port=available_port(), duration_s=2.5)))
            process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(path),
                                        "--output", str(Path(root) / "run")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                # Deliberately do not read stdout until after the process exits.
                process.wait(7)
                summary = json.loads((Path(root) / "run/summary.json").read_text())
                self.assertEqual(summary["run"]["exit_code"], 0)
                self.assertGreaterEqual(summary["run"]["counters"]["source_updates"], 20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "Install the optional GUI dependency to run Qt tests")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application()

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.root = tempfile.TemporaryDirectory()
        self.window = Workbench()
        self.window.output.setText(self.root.name)
        self.window.fields["port"].setText(str(available_port()))
        self.window.show()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=6):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Qt lifecycle timed out")

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: self.window.temporary is None)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.root.cleanup()

    def test_configuration_roundtrip_all_modes(self):
        for mode in ("rtu-master", "rtu-slave", "tcp-slave"):
            config = TaskConfig.from_mapping(dict(mode=mode, serial=dict(port="TEST"),
                                                 source=dict(kind="sine", value=2), addresses=[0, 4],
                                                 period_ms=200, timeout_ms=150,
                                                 readback_every=2 if mode == "rtu-master" else 0))
            self.window.apply_config(config)
            self.assertEqual(self.window.configuration(), config)

    def test_mode_and_source_choices_keep_protocol_values(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        mode = self.window.fields["mode"]
        mode.setFocus()
        QTest.keyClick(mode, Qt.Key_Down)
        self.assertEqual(self.window.text("mode"), "rtu-slave")
        self.assertFalse(self.window.serial_box.isHidden())
        self.assertTrue(self.window.tcp_box.isHidden())
        before = mode.currentIndex()
        mode.showPopup()
        self.app.processEvents()
        QTest.keyClick(mode.view(), Qt.Key_Down)
        QTest.keyClick(mode.view(), Qt.Key_Escape)
        self.assertEqual(mode.currentIndex(), before)
        mode.showPopup()
        self.app.processEvents()
        QTest.keyClick(mode.view(), Qt.Key_End)
        QTest.keyClick(mode.view(), Qt.Key_Return)
        self.assertEqual(self.window.text("mode"), "rtu-master")
        self.assertFalse(self.window.master_box.isHidden())
        source = self.window.fields["source.kind"]
        source.setCurrentIndex(source.findData("sine"))
        self.assertTrue(self.window.fields["source.amplitude"].isEnabled())
        source.setCurrentIndex(source.findData("constant"))
        self.assertFalse(self.window.fields["source.amplitude"].isEnabled())

    def test_summary_uses_counters_from_the_completed_session(self):
        self.window.counter_mode = "tcp-slave"
        self.window.show_counters(dict(source_updates=17, slave_request=3, rx=4, tx=3))
        self.assertEqual([label.text() for label in self.window.metric_values], ["17", "3", "4", "3"])
        self.window.apply_config(TaskConfig.from_mapping(dict(mode="rtu-master", serial=dict(port="TEST"))))
        self.assertEqual([label.text() for label in self.window.metric_values], ["17", "3", "4", "3"])
        self.assertEqual(self.window.metric_labels[0].text(), "数据更新")

    def test_visible_controls_share_the_same_height(self):
        from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton
        from modbus_dev.gui_theme import METRICS
        for mode in ("tcp-slave", "rtu-slave", "rtu-master"):
            self.window.apply_config(TaskConfig.from_mapping(dict(mode=mode, serial=dict(port="TEST"))))
            for tab in range(self.window.config_tabs.count()):
                self.window.config_tabs.setCurrentIndex(tab)
                self.app.processEvents()
                widgets = [widget for kind in (QComboBox, QLineEdit, QPushButton)
                           for widget in self.window.findChildren(kind)]
                for widget in widgets:
                    if widget.isVisible():
                        self.assertEqual(widget.height(), METRICS.control_height, widget.objectName() or widget.text())

    def test_invalid_configuration_never_starts_worker(self):
        self.window.fields["addresses"].setText("0, 1")
        with patch.object(self.window, "error") as error:
            self.window.start()
        error.assert_called_once()
        self.assertIsNone(self.window.process)

    def test_start_stop_and_restart(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.assertFalse(self.window.editor.isEnabled())
        with socket.create_connection(("127.0.0.1", int(self.window.text("port"))), timeout=1) as peer:
            peer.sendall(bytes.fromhex("000700000006010300000002"))
            response = b""
            while len(response) < 13:
                response += peer.recv(13 - len(response))
            self.assertEqual(response.hex(), "00070000000701030400000000")
        self.window.stop()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 130)
        self.assertTrue(self.window.editor.isEnabled())
        first = self.window.last_output
        self.window.fields["duration_s"].setText("0.25")
        self.window.start()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 0)
        self.assertNotEqual(first, self.window.last_output)

    def test_worker_crash_is_visible_and_releases_ui(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.window.monitor_tabs.setCurrentIndex(1)
        self.window.process.kill()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertIn("异常结束", self.window.status.text())
        self.assertEqual(self.window.status.property("tone"), "error")
        self.assertEqual(self.window.monitor_tabs.currentIndex(), 0)
        self.assertTrue(self.window.start_button.isEnabled())
        marker = json.loads((self.window.last_output / "gui-worker-error.json").read_text(encoding="utf-8"))
        self.assertFalse(marker["evidence_complete"])

    def test_close_waits_for_worker_cleanup(self):
        self.window.start()
        self.wait_for(lambda: self.window.status.text() == "运行中")
        self.window.close()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertEqual(self.window.result["exit_code"], 130)
        self.assertFalse(self.window.isVisible())

    def test_failed_process_launch_restores_controls(self):
        with patch("modbus_dev.gui.sys.executable", str(Path(self.root.name) / "missing-python.exe")):
            self.window.start()
        self.wait_for(lambda: self.window.temporary is None)
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertIn("异常结束", self.window.status.text())

    def test_log_preview_is_bounded(self):
        for _ in range(25):
            self.window.display(dict(type="status", state="running", counters={},
                                     events=[dict(level="WARNING", code="test", message="message")] * 100))
        self.assertEqual(self.window.logs.document().blockCount(), 2000)
