"""验证固定步长三角波的上下限、时钟、精确整数和通信路径，不依赖外部设备。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from modbus_dev.application.session import SessionRunner, check_capabilities
from modbus_dev.domain.config import TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.numeric import NUMERIC_TYPES, decode_value, encode_value
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.sources.synthetic import SyntheticSource, triangle_value
from test_rtu_session import FakeControl, Peer
import test_rtu_slave


def configuration(data_type="int16", lower=0, upper=3, **options):
    return TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[dict(data_type=data_type,
        source=dict(kind="triangle", lower=lower, upper=upper))], **options))


class TriangleSourceTests(unittest.TestCase):
    def test_all_numeric_types_start_at_lower_and_reverse_with_fixed_step(self):
        for data_type in NUMERIC_TYPES:
            with self.subTest(data_type=data_type):
                settings = configuration(data_type)
                check_capabilities(settings)
                source = SyntheticSource(settings)
                values = [source.sample(slot)[0] for slot in range(9)]
                self.assertEqual(values, [0, 1, 2, 3, 2, 1, 0, 1, 2])
                for value in values:
                    self.assertEqual(decode_value(encode_value(value, data_type), data_type), value)
                    if NUMERIC_TYPES[data_type].integer:
                        self.assertIs(type(value), int)

    def test_float_fractional_span_clamps_at_bounds_without_duplicate_peaks(self):
        source = SyntheticSource(configuration("float32", .25, 2.75))
        self.assertEqual([source.sample(slot)[0] for slot in range(9)],
                         [.25, 1.25, 2.25, 2.75, 1.75, .75, .25, 1.25, 2.25])
        self.assertEqual([triangle_value(slot, .25, .75) for slot in range(5)], [.25, .75, .25, .75, .25])

    def test_integer_type_boundaries_are_not_clipped_or_overflowed(self):
        for kind, spec in NUMERIC_TYPES.items():
            if not spec.integer:
                continue
            for lower, upper in ((spec.minimum, spec.minimum + 2), (spec.maximum - 2, spec.maximum)):
                with self.subTest(kind=kind, lower=lower):
                    source = SyntheticSource(configuration(kind, lower, upper))
                    self.assertEqual([source.sample(i)[0] for i in range(5)], [lower, lower+1, upper, upper-1, lower])

    def test_sampling_is_deterministic_and_uses_logical_slots_after_skips(self):
        source = SyntheticSource(configuration())
        self.assertEqual([source.sample(i)[0] for i in (0, 2, 3, 8, 8, 1, 10**12)], [0, 2, 3, 2, 2, 1, 2])
        faster = SyntheticSource(configuration(period_ms=50))
        self.assertEqual(source.sample(11), faster.sample(11))

    def test_invalid_bounds_and_configurable_step_are_rejected(self):
        cases = [("int16", 1, 1), ("int16", 2, 1), ("int16", .5, 2),
                 ("uint16", -1, 2), ("int16", 0, 32768), ("uint32", 0, 4294967296),
                 ("float32", float("nan"), 2), ("float32", 0, float("inf")), ("float32", 0, 3.5e38)]
        for kind, lower, upper in cases:
            with self.subTest(kind=kind, lower=lower, upper=upper), self.assertRaises(ConfigurationError):
                configuration(kind, lower, upper)
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[dict(source=dict(kind="triangle", step=1))]))

    def test_unused_source_fields_do_not_block_triangle_and_roundtrip(self):
        settings = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[dict(data_type="int16",
            source=dict(kind="triangle", lower=-2, upper=1, value="draft", amplitude="draft", frequency_hz="draft"))]))
        self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(settings.to_mapping()))), settings)
        self.assertEqual(SyntheticSource(settings).sample(1), (-1,))

    def test_json_checks_integer_bounds_before_float_rounding(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "triangle.json"
            for field, number in (("lower", "0.0000000000000000001"), ("upper", "32767.0000000000001")):
                fields = dict(lower="0", upper="32767")
                fields[field] = number
                # 直接写入十进制文本，避免测试数据先被 Python 浮点舍入。
                path.write_text('{"schema_version":4,"mode":"tcp-slave","points":[{"data_type":"int16","source":'
                    '{"kind":"triangle","lower":' + fields["lower"] + ',"upper":' + fields["upper"] + '}}]}')
                with self.subTest(field=field), self.assertRaises(ConfigurationError) as error:
                    load_config(path)
                self.assertEqual(error.exception.path, "points[0].source." + field)

    def test_mixed_sources_keep_point_order_and_register_widths(self):
        settings = TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=[
            dict(address=0, data_type="int16", source=dict(kind="triangle", lower=-1, upper=1)),
            dict(address=1, data_type="uint32", source=dict(kind="triangle", lower=4294967293, upper=4294967295)),
            dict(address=3, source=dict(value=12.5)),
            dict(address=5, source=dict(kind="sine", value=2, amplitude=1, frequency_hz=1))]))
        self.assertEqual(SyntheticSource(settings).sample(0), (-1, 4294967293, 12.5, 2))
        self.assertEqual(SyntheticSource(settings).sample(5)[:3], (0, 4294967294, 12.5))

    def test_rtu_write_readback_transmits_actual_triangle_samples(self):
        clock = FakeControl()
        peer = Peer(clock, delay=.005)
        settings = replace(configuration(), mode="rtu-master", access_mode="read-write", duration_s=.5,
                           points=tuple(replace(point, operation="write-multiple", readback_every=1)
                                        for point in configuration().points),
                           serial=replace(configuration().serial, port="TEST", baud=115200))
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(settings, Path(root) / "run", control=clock,
                engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
            result = runner.run()
            self.assertEqual(result["exit_code"], 0, result)
            writes = [packet for _, packet in peer.writes if packet[1] == 16]
            self.assertEqual([int.from_bytes(packet[7:9], "big") for packet in writes], [0, 1, 2, 3, 2])
            self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)

    def test_rtu_slave_input_triangle_advances_while_holding_keeps_initial_value(self):
        read_input = test_rtu_slave.frame(bytes.fromhex("010400000001"))
        read_holding = test_rtu_slave.frame(bytes.fromhex("010300000001"))
        result, ports, _, _ = test_rtu_slave.RtuSlaveSessionTests().run_session(
            duration_s=.4, packets=[(.01, read_input), (.11, read_input), (.21, read_input),
                                   (.31, read_input), (.35, read_holding)],
            points=[dict(area="input", data_type="int16", source=dict(kind="triangle", lower=-1, upper=1)),
                    dict(area="holding", data_type="int16", source=dict(value=-1))])
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual([packet[3:5] for _, packet in ports[0].writes],
                         [bytes.fromhex(word) for word in ("ffff", "0000", "0001", "0000", "ffff")])


if __name__ == "__main__":
    unittest.main()
