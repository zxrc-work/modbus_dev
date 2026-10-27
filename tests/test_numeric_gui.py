"""验证逐点类型和排列的界面、配置草稿、完整整数显示及工作进程收发。"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import ConfigurationError
from test_tcp_slave import frame, receive


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class NumericGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="zh_CN")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.window = Workbench(remember_settings=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("GUI 工作进程等待超时")

    def test_mixed_point_config_roundtrip_and_width_conflicts(self):
        settings = TaskConfig.from_mapping(dict(mode="tcp-slave", points=[
            dict(address=0, data_type="int16", byte_order="BA", source=dict(value=-123)),
            dict(address=1, data_type="float32", byte_order="B3412", source=dict(value=12.5)),
            dict(address=3, data_type="uint32", source=dict(value=4294967295))]))
        self.window.apply_config(settings)
        self.assertEqual(self.window.configuration(), settings)
        self.assertIn("0～0 · int16", self.window.point_editors[0].range_label.text())
        self.assertNotIn("重叠", self.window.points_status.text())
        first = self.window.point_editors[0]
        first.fields["data_type"].setCurrentIndex(first.fields["data_type"].findData("int32"))
        self.assertIn("测点 1 与测点 2", self.window.points_status.text())
        self.assertIn("0～1 · int32", first.range_label.text())

    def test_inapplicable_orders_and_integer_sine_cannot_be_selected(self):
        editor = self.window.point_editors[0]
        kind, order = editor.fields["data_type"], editor.fields["byte_order"]
        kind.setCurrentIndex(kind.findData("uint16"))
        self.assertTrue(order.model().item(order.findData("AB")).isEnabled())
        self.assertTrue(order.model().item(order.findData("BA")).isEnabled())
        self.assertFalse(order.model().item(order.findData("B1234")).isEnabled())
        source = editor.fields["source.kind"]
        self.assertFalse(source.model().item(source.findData("sine")).isEnabled())
        kind.setCurrentIndex(kind.findData("float32"))
        self.assertFalse(order.model().item(order.findData("BA")).isEnabled())
        self.assertTrue(order.model().item(order.findData("B3412")).isEnabled())
        self.assertTrue(source.model().item(source.findData("sine")).isEnabled())

    def test_type_change_does_not_silently_clip_address_or_round_value(self):
        editor = self.window.point_editors[0]
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("uint16"))
        editor.fields["address"].setValue(65535)
        editor.fields["source.value"].setText("65535")
        self.assertEqual(self.window.configuration().points[0].source.value, 65535)
        editor.fields["source.value"].setText("123.5")
        with self.assertRaises(ValueError):
            self.window.configuration()
        editor.fields["source.value"].setText("123.0000000000000001")
        with self.assertRaises(ValueError):
            self.window.configuration()
        editor.fields["source.value"].setText("123")
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("uint32"))
        self.assertEqual(editor.fields["address"].value(), 65535)
        with self.assertRaises(ConfigurationError):
            self.window.configuration()

    def test_type_and_order_survive_draft_restore_and_language_change(self):
        editor = self.window.point_editors[0]
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("int16"))
        editor.fields["byte_order"].setCurrentIndex(editor.fields["byte_order"].findData("BA"))
        editor.fields["source.value"].setText("unfinished")
        draft = self.window.editor.capture_draft()
        self.window.editor.apply_config(TaskConfig(mode="tcp-slave"))
        self.window.editor.restore_draft(json.loads(json.dumps(draft)))
        self.assertEqual(self.window.editor.capture_draft(), draft)
        self.app.language_manager._install("en")
        self.window.retranslate_ui()
        self.assertEqual(editor.fields["data_type"].currentData(), "int16")
        self.assertEqual(editor.fields["byte_order"].currentData(), "BA")
        self.app.language_manager._install("zh_CN")
        self.window.retranslate_ui()

    def test_integer_values_are_displayed_without_scientific_rounding(self):
        from modbus_dev.ui.realtime_panel import RealtimePanel
        self.assertEqual(RealtimePanel.format_value(dict(value=4294967295)), "4294967295")
        self.assertEqual(RealtimePanel.format_value(dict(value=-2147483648)), "-2147483648")

    def test_mixed_types_reach_worker_and_tcp_reply(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = TaskConfig.from_mapping(dict(mode="tcp-slave", duration_s=0, port=port, points=[
            dict(address=0, data_type="int16", source=dict(value=-123)),
            dict(address=1, data_type="uint32", byte_order="B3412", source=dict(value=4294967295)),
            dict(address=3, data_type="float32", byte_order="B4321", source=dict(value=12.5))]))
        with tempfile.TemporaryDirectory() as root:
            self.window.output.setText(root)
            self.window.apply_config(settings)
            with patch.object(self.window, "error") as error:
                self.window.start()
                error.assert_not_called()
            self.wait_for(lambda: self.window.status.text() == "运行中")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as peer:
                    peer.sendall(frame(bytes.fromhex("0300000005")))
                    self.assertEqual(receive(peer), frame(bytes.fromhex("030aff85ffffffff00004841")))
            finally:
                self.window.stop()
                self.wait_for(lambda: not self.window.active())
            self.assertEqual(self.window.result["exit_code"], 130)
            saved = json.loads((self.window.last_output / "config.json").read_text())
            self.assertEqual(saved["schema_version"], 3)
            self.assertEqual([p["data_type"] for p in saved["points"][:3]], ["int16", "uint32", "float32"])


if __name__ == "__main__":
    unittest.main()
