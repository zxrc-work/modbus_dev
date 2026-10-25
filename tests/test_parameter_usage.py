"""按角色和操作验证参数适用性，覆盖无关草稿、实际请求和界面切换。"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from modbus_dev.application.session import check_capabilities
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.wire import minimum_cycle_ms
from modbus_dev.sources.synthetic import SyntheticSource
import test_rtu_session


def master(operation="read-input", **options):
    return TaskConfig.from_mapping(dict(mode="rtu-master", master_operation=operation,
                                       serial=dict(port="TEST", baud=115200), **options))


class ParameterTests(unittest.TestCase):
    def test_reads_ignore_source_values_in_both_config_versions(self):
        source = dict(kind="dyn", value="unused", amplitude=-1, frequency_hz="unused",
                      path="", channels="unused")
        for operation in ("read-input", "read-holding"):
            for data in (dict(source=source), dict(points=[dict(source=source)])):
                with self.subTest(operation=operation, data=data):
                    config = master(operation, **data)
                    check_capabilities(config)
                    self.assertEqual(config.points[0].source.value, "unused")
                    self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))), config)

    def test_read_plan_and_budget_do_not_encode_generated_values(self):
        config = master(points=[dict(address=6), dict(address=0), dict(address=2),
                                dict(enabled=False, address=4)])
        with patch("modbus_dev.domain.operations.write_requests", side_effect=AssertionError("source encoding")):
            requests = cycle_requests(config, ())
            self.assertGreater(minimum_cycle_ms(config), 0)
        self.assertEqual([(r.function, r.address, r.count, r.registers) for r in requests],
                         [(4, 0, 4, ()), (4, 6, 2, ())])

    def test_read_session_never_constructs_or_samples_a_source(self):
        helper = test_rtu_session.SessionTests()
        for operation in ("read-input", "read-holding"):
            with self.subTest(operation=operation), patch("modbus_dev.application.session.SyntheticSource",
                    side_effect=AssertionError("source construction")):
                result, peer, events = helper.exercise(duration=.3, delay=.005,
                    master_operation=operation, source=dict(kind="dyn", value="unused", channels="unused"),
                    readback_every="unused", read_function="unused")
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(result["counters"]["read"], 3)
                self.assertNotIn("source_sample", {event["code"] for event in events})
                self.assertTrue(all(packet[1] == (4 if operation == "read-input" else 3)
                                    for _, packet in peer.writes))

    def test_inactive_connection_and_master_fields_do_not_block_validation(self):
        check_capabilities(master(host="", port="unused", readback_every="unused", read_function="unused"))
        config = TaskConfig.from_mapping(dict(mode="tcp-slave", serial=dict(port="", baud="unused"),
            master_operation="unused", access_mode="unused", readback_every="unused",
            read_function="unused", schedule_tolerance_percent="unused"))
        check_capabilities(config)
        config = TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"), host="", port="unused",
            master_operation="unused", readback_every="unused", read_function="unused",
            schedule_tolerance_percent="unused"))
        check_capabilities(config)
        check_capabilities(master("write-multiple", readback_every=0, read_function="unused"))

    def test_constant_and_disabled_sources_skip_unused_parameters(self):
        config = TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"), points=[
            dict(source=dict(kind="constant", value=1.25, amplitude="unused", frequency_hz=-10,
                             channels="unused")),
            dict(enabled=False, address="unused", source=dict(kind="unused", value="unused"))]))
        check_capabilities(config)
        self.assertEqual(SyntheticSource(config).sample(1), (1.25,))

    def test_active_source_connection_and_readback_errors_still_fail(self):
        cases = [dict(master_operation="write-multiple", source=dict(value="invalid")),
                 dict(master_operation="write-multiple", readback_every=1, read_function=4),
                 dict(master_operation="write-multiple", readback_every="invalid"),
                 dict(master_operation="read-input", unit=0),
                 dict(master_operation="read-input", points=[dict(address=-1)])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(dict(mode="rtu-master", serial=dict(port="TEST", baud=115200), **data))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"), access_mode="read-only",
                source=dict(kind="sine", frequency_hz=100)))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(dict(mode="tcp-slave", port="invalid"))


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class ParameterGuiTests(unittest.TestCase):
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
            until = time.monotonic() + 5
            while self.window.active() and time.monotonic() < until:
                self.app.processEvents()
                time.sleep(.01)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_read_source_is_disabled_but_slave_readonly_source_remains_active(self):
        self.window.apply_config(master())
        point = self.window.point_editors[0]
        self.assertTrue(point.fields["address"].isEnabled())
        for key in ("source.kind", "source.value", "source.amplitude", "source.frequency_hz"):
            self.assertFalse(point.fields[key].isEnabled(), key)
        self.assertIn("读取", point.form.labelForField(point.fields["address"]).text())
        mode = self.window.fields["mode"]
        mode.setCurrentIndex(mode.findData("rtu-slave"))
        self.window.fields["access_mode"].setCurrentIndex(self.window.fields["access_mode"].findData("read-only"))
        self.assertTrue(point.fields["source.kind"].isEnabled())
        self.assertTrue(point.fields["source.value"].isEnabled())
        self.assertIn("本机", point.form.labelForField(point.fields["address"]).text())

    def test_invalid_source_draft_is_ignored_for_read_and_restored_for_write(self):
        self.window.apply_config(master("write-multiple"))
        point = self.window.point_editors[0]
        point.fields["source.value"].setText("unfinished")
        self.window.fields["host"].setText("")
        self.window.fields["port"].setText("unused")
        self.window.fields["readback_every"].setText("unfinished")
        operation = self.window.fields["master_operation"]
        operation.setCurrentIndex(operation.findData("read-input"))
        config = self.window.configuration()
        self.assertEqual(config.master_operation, "read-input")
        self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))).points[0].source.value,
                         "unfinished")
        self.assertEqual(point.fields["source.value"].text(), "unfinished")
        self.window.fields["readback_every"].setText("0")
        operation.setCurrentIndex(operation.findData("write-multiple"))
        self.assertTrue(point.fields["source.value"].isEnabled())
        with self.assertRaisesRegex(ValueError, "数值"):
            self.window.configuration()

    def test_tcp_ignores_serial_drafts_but_checks_its_own_port(self):
        self.window.apply_config(TaskConfig.from_mapping(dict(mode="tcp-slave",
            serial=dict(baud="unfinished", rts_toggle="unused"))))
        self.window.fields["schedule_tolerance_percent"].setText("unfinished")
        self.window.configuration()
        self.window.fields["port"].setText("unfinished")
        with self.assertRaises(ValueError):
            self.window.configuration()

    def test_inactive_serial_draft_reaches_actual_tcp_worker(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            self.window.output.setText(root)
            self.window.fields["port"].setText(str(port))
            self.window.fields["serial.baud"].setEditText("unfinished")
            self.window.fields["schedule_tolerance_percent"].setText("unfinished")
            self.window.fields["duration_s"].setText(".25")
            with patch.object(self.window, "error") as error:
                self.window.start()
                error.assert_not_called()
            until = time.monotonic() + 6
            while self.window.temporary is not None and time.monotonic() < until:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNone(self.window.temporary)
            self.assertEqual(self.window.result["exit_code"], 0, self.window.result)
            saved = json.loads((self.window.last_output / "config.json").read_text(encoding="utf-8"))
            summary = json.loads((self.window.last_output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["serial"]["baud"], "unfinished")
            self.assertTrue(summary["evidence_complete"])

    def test_constant_ignores_sine_drafts_until_sine_is_selected(self):
        point = self.window.point_editors[0]
        point.fields["source.amplitude"].setText("unfinished")
        point.fields["source.frequency_hz"].setText("unfinished")
        self.window.configuration()
        point.fields["source.kind"].setCurrentIndex(point.fields["source.kind"].findData("sine"))
        with self.assertRaisesRegex(ValueError, "振幅"):
            self.window.configuration()

    def test_curve_choices_follow_actual_numeric_directions(self):
        cases = [(master(), {"input:0:rx"}),
                 (master("write-multiple"), {"holding:0:tx"}),
                 (master("write-multiple", readback_every=1), {"holding:0:tx", "holding:0:rx"}),
                 (TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"))),
                  {"holding:0:tx", "input:0:tx"}),
                 (TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"), access_mode="read-write")),
                  {"holding:0:tx", "holding:0:rx", "input:0:tx"})]
        for config, expected in cases:
            with self.subTest(config=config):
                self.window.apply_config(config)
                actual = {key for key, checkbox in self.window.live_panel.selectors.items() if checkbox.isEnabled()}
                self.assertEqual(actual, expected)
