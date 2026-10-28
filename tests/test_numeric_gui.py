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
        settings = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[
            dict(address=0, data_type="int16", byte_order="B21", source=dict(value=-123)),
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
        self.assertTrue(order.model().item(order.findData("B12")).isEnabled())
        self.assertTrue(order.model().item(order.findData("B21")).isEnabled())
        self.assertFalse(order.model().item(order.findData("B1234")).isEnabled())
        source = editor.fields["source.kind"]
        self.assertFalse(source.model().item(source.findData("sine")).isEnabled())
        kind.setCurrentIndex(kind.findData("float32"))
        self.assertFalse(order.model().item(order.findData("B21")).isEnabled())
        self.assertTrue(order.model().item(order.findData("B3412")).isEnabled())
        self.assertTrue(source.model().item(source.findData("sine")).isEnabled())

    def visible_choices(self, combo):
        return [combo.itemData(index) for index in range(combo.count()) if not combo.view().isRowHidden(index)]

    def test_integer_popup_only_shows_two_orders_and_compatible_sources(self):
        editor = self.window.point_editors[0]
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("int16"))
        orders = editor.fields["byte_order"]
        self.assertEqual(self.visible_choices(orders), ["B12", "B21"])
        self.assertEqual(orders.currentText(), "12")
        self.assertEqual(self.visible_choices(editor.fields["source.kind"]), ["constant", "triangle"])
        self.assertNotIn("byte_order", self.window.fields)
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("float32"))
        self.assertEqual(self.visible_choices(orders), ["B1234", "B4321", "B2143", "B3412"])
        self.assertEqual(orders.currentData(), 'B1234')
        self.assertEqual(orders.currentText(), '1234')

    def test_triangle_shows_only_bounds_and_fixed_step_note(self):
        editor = self.window.point_editors[0]
        for kind in ("int16", "uint16", "int32", "uint32", "float32"):
            with self.subTest(kind=kind):
                editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData(kind))
                source = editor.fields["source.kind"]
                source.setCurrentIndex(source.findData("triangle"))
                self.assertEqual(source.currentText(), "三角波")
                for key in ("lower", "upper"):
                    self.assertFalse(editor.fields["source." + key].isHidden())
                for key in ("value", "amplitude", "frequency_hz"):
                    self.assertTrue(editor.fields["source." + key].isHidden())
                self.assertNotIn("source.step", editor.fields)
                self.assertIn("步长固定为 1", editor.source_note.text())
                editor.fields["source.lower"].setText("0")
                editor.fields["source.upper"].setText("3")
                settings = self.window.configuration()
                self.assertEqual(settings.points[0].source.kind, "triangle")
                self.assertEqual((settings.points[0].source.lower, settings.points[0].source.upper), (0, 3))

    def test_triangle_bounds_validation_and_unused_fields(self):
        editor = self.window.point_editors[0]
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("int16"))
        source = editor.fields["source.kind"]
        source.setCurrentIndex(source.findData("triangle"))
        for key in ("value", "amplitude", "frequency_hz"):
            editor.fields["source." + key].setText("unfinished")
        editor.fields["source.lower"].setText("-2")
        editor.fields["source.upper"].setText("3")
        self.window.configuration()
        editor.fields["source.lower"].setText("0.00000000000000001")
        with self.assertRaises(ValueError):
            self.window.configuration()
        editor.fields["source.lower"].setText("3")
        with self.assertRaises(ConfigurationError):
            self.window.configuration()
        source.setCurrentIndex(source.findData("constant"))
        editor.fields["source.value"].setText("1")
        editor.fields["source.lower"].setText("unfinished")
        self.window.configuration()

    def test_incompatible_selections_are_explicit_and_survive_restore(self):
        editor = self.window.point_editors[0]
        kind, order, source = (editor.fields[key] for key in ("data_type", "byte_order", "source.kind"))
        source.setCurrentIndex(source.findData("sine"))
        order.setCurrentIndex(order.findData("B3412"))
        kind.setCurrentIndex(kind.findData("int16"))
        self.assertEqual(source.currentIndex(), -1)
        self.assertEqual(order.currentData(), "B12")
        self.assertIn("原数据源不适用", editor.selection_note.text())
        self.assertIn("不兼容", self.window.points_status.text())
        with self.assertRaises(ValueError):
            self.window.configuration()
        draft = self.window.editor.capture_draft()
        self.window.apply_config(TaskConfig(mode="tcp-slave"))
        self.window.editor.restore_draft(json.loads(json.dumps(draft)))
        self.assertEqual(self.window.editor.capture_draft(), draft)
        kind.setCurrentIndex(kind.findData("float32"))
        self.assertEqual(order.currentData(), "B1234")
        self.assertEqual(source.currentData(), "sine")
        self.window.configuration()

    def test_old_alias_draft_and_triangle_draft_survive_language_changes(self):
        editor = self.window.point_editors[0]
        kind = editor.fields["data_type"]
        kind.setCurrentIndex(kind.findData("int16"))
        source = editor.fields["source.kind"]
        source.setCurrentIndex(source.findData("triangle"))
        editor.fields["source.lower"].setText("unfinished")
        draft = self.window.editor.capture_draft()
        draft["values"]["points.0.byte_order"] = "BA"
        self.window.editor.restore_draft(draft)
        self.assertEqual(editor.fields["byte_order"].currentData(), "B21")
        self.app.language_manager._install("en")
        self.window.retranslate_ui()
        self.assertEqual(source.currentText(), "Triangle wave")
        self.assertEqual(editor.form.labelForField(editor.fields["source.lower"]).text(), "Lower bound")
        self.assertEqual(editor.fields["source.lower"].text(), "unfinished")
        self.app.language_manager._install("zh_CN")
        self.window.retranslate_ui()

    def test_filtered_popups_keyboard_cannot_reach_hidden_choices(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        editor = self.window.point_editors[0]
        self.window.config_tabs.setCurrentIndex(1)
        editor.fields["data_type"].setCurrentIndex(editor.fields["data_type"].findData("int16"))
        for key, expected in (("byte_order", "B21"), ("source.kind", "triangle")):
            combo = editor.fields[key]
            self.window.config_tabs.widget(1).ensureWidgetVisible(combo)
            self.app.processEvents()
            self.assertTrue(combo.isVisible())
            combo.showPopup()
            self.app.processEvents()
            QTest.keyClick(combo.view(), Qt.Key_End)
            QTest.keyClick(combo.view(), Qt.Key_Return)
            self.assertEqual(combo.currentData(), expected)

    def test_triangle_reaches_tcp_worker_and_returns_changing_integer_words(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", port=port, duration_s=0, points=[
            dict(data_type="int16", byte_order="B12", source=dict(kind="triangle", lower=-1, upper=1))]))
        with tempfile.TemporaryDirectory() as root:
            self.window.output.setText(root)
            self.window.apply_config(settings)
            with patch.object(self.window, "error") as error:
                self.window.start()
                error.assert_not_called()
            self.wait_for(lambda: self.window.status.text() == "运行中")
            try:
                values = set()
                with socket.create_connection(("127.0.0.1", port), timeout=1) as peer:
                    deadline = time.monotonic() + 1
                    while len(values) < 3 and time.monotonic() < deadline:
                        peer.sendall(frame(bytes.fromhex("0400000001")))
                        response = receive(peer)
                        self.assertEqual(response[:9], frame(bytes.fromhex("04020000"))[:9])
                        values.add(int.from_bytes(response[9:11], "big", signed=True))
                        self.app.processEvents()
                        time.sleep(.03)
                self.assertEqual(values, {-1, 0, 1})
            finally:
                self.window.stop()
                self.wait_for(lambda: not self.window.active())
            saved = json.loads((self.window.last_output / "config.json").read_text())
            self.assertEqual(saved["points"][0]["source"]["kind"], "triangle")
            self.assertNotIn("step", saved["points"][0]["source"])


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
        editor.fields["byte_order"].setCurrentIndex(editor.fields["byte_order"].findData("B21"))
        editor.fields["source.value"].setText("unfinished")
        draft = self.window.editor.capture_draft()
        self.window.editor.apply_config(TaskConfig(mode="tcp-slave"))
        self.window.editor.restore_draft(json.loads(json.dumps(draft)))
        self.assertEqual(self.window.editor.capture_draft(), draft)
        self.app.language_manager._install("en")
        self.window.retranslate_ui()
        self.assertEqual(editor.fields["data_type"].currentData(), "int16")
        self.assertEqual(editor.fields["byte_order"].currentData(), "B21")
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
                    peer.sendall(frame(bytes.fromhex("0400000005")))
                    self.assertEqual(receive(peer), frame(bytes.fromhex("040aff85ffffffff00004841")))
            finally:
                self.window.stop()
                self.wait_for(lambda: not self.window.active())
            self.assertEqual(self.window.result["exit_code"], 130)
            saved = json.loads((self.window.last_output / "config.json").read_text())
            self.assertEqual(saved["schema_version"], 4)
            self.assertEqual([p["data_type"] for p in saved["points"][:3]], ["int16", "uint32", "float32"])


if __name__ == "__main__":
    unittest.main()
