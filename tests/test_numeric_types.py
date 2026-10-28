"""以固定寄存器样例验证类型契约、混合点表、真实读取和原始诊断边界。"""
from dataclasses import replace
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from modbus_dev.application.live import LiveValues
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError, Event
from modbus_dev.domain.mapping import point_write_requests
from modbus_dev.domain.numeric import NUMERIC_TYPES, decode_value, encode_value, parse_integer
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.wire import minimum_cycle_ms
from modbus_dev.engines.read_only import ReadOnlyRegisters
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.engines.rtu_registers import RtuRegisters
from modbus_dev.engines.tcp_slave import TcpSlaveEngine
from modbus_dev.sources.synthetic import SyntheticSource
from test_rtu_session import FakeControl, Peer
from test_rtu_slave import Clock
from test_tcp_slave import frame, receive


VECTORS = json.loads((Path(__file__).parent / "fixtures/numeric-vectors.json").read_text())


def config(points, mode="tcp-slave", **options):
    version = options.pop("schema_version", 4)
    return TaskConfig.from_mapping(dict(schema_version=version, mode=mode, serial=dict(port="TEST", baud=115200), points=points, **options))


def mixed_points():
    # 故意打乱地址；预期报文在测试中固定给出，不调用生产编码器生成。
    return [dict(address=3, data_type="uint32", byte_order="B3412", source=dict(value=305419896)),
            dict(address=0, data_type="int16", source=dict(value=-123)),
            dict(address=1, data_type="float32", byte_order="B2143", source=dict(value=12.5)),
            dict(address=5, data_type="uint16", byte_order="BA", source=dict(value=4660))]


MIXED_WORDS = (0xff85, 0x4841, 0, 0x5678, 0x1234, 0x3412)
MIXED_VALUES = (305419896, -123, 12.5, 4660)


class NumericCodecTests(unittest.TestCase):
    def test_legacy_16_bit_aliases_remain_compatible_and_export_canonical_names(self):
        for old, new, words in (("AB", "B12", (0x1234,)), ("BA", "B21", (0x3412,))):
            with self.subTest(old=old):
                self.assertEqual(encode_value(0x1234, "uint16", old), words)
                self.assertEqual(decode_value(words, "uint16", old), 0x1234)
                settings = config([dict(data_type="uint16", byte_order=old)])
                self.assertEqual(settings.to_mapping()["points"][0]["byte_order"], new)

    def test_encode_against_independent_fixed_vectors(self):
        for vector in VECTORS:
            with self.subTest(vector=vector):
                self.assertEqual(encode_value(vector["value"], vector["data_type"], vector["byte_order"]),
                                 tuple(vector["registers"]))

    def test_decode_against_independent_fixed_vectors(self):
        for vector in VECTORS:
            with self.subTest(vector=vector):
                value = decode_value(vector["registers"], vector["data_type"], vector["byte_order"])
                self.assertEqual(value, vector["value"])
                self.assertIs(type(value), float if vector["data_type"] == "float32" else int)

    def test_fixture_covers_every_supported_type_and_order(self):
        self.assertEqual({(v["data_type"], v["byte_order"]) for v in VECTORS},
                         {(name, order) for name, specification in NUMERIC_TYPES.items() for order in specification.orders})

    def test_integer_boundaries_and_all_ones_are_not_invalid_codes(self):
        cases = [("int16", -32768, (0x8000,)), ("int16", 32767, (0x7fff,)),
                 ("int16", -1, (0xffff,)), ("uint16", 65535, (0xffff,)),
                 ("int32", -2147483648, (0x8000, 0)), ("int32", 2147483647, (0x7fff, 0xffff)),
                 ("int32", -1, (0xffff, 0xffff)), ("uint32", 4294967295, (0xffff, 0xffff))]
        for data_type, value, words in cases:
            with self.subTest(data_type=data_type, value=value):
                self.assertEqual(encode_value(value, data_type), words)
                self.assertEqual(decode_value(words, data_type), value)

    def test_rejects_fractional_overflow_nonfinite_and_invalid_types(self):
        for name, specification in NUMERIC_TYPES.items():
            values = [True, "123", float("nan"), float("inf"), -float("inf"), 10 ** 1000]
            if specification.integer:
                values += [1.5, specification.minimum - 1, specification.maximum + 1]
            else:
                values += [3.5e38, -3.5e38]
            for value in values:
                with self.subTest(data_type=name, value=value), self.assertRaises(ValueError):
                    encode_value(value, name)
        for name in ("float64", "bcd", None, []):
            with self.assertRaises(ValueError):
                encode_value(1, name)

    def test_rejects_wrong_width_order_and_nonfinite_payloads(self):
        for data_type, words, order in (("int16", (1, 2), "AB"), ("int16", (1,), "B1234"),
                                        ("float32", (1,), "B1234"), ("uint32", (1, 2), "BA"),
                                        ("int16", (65536,), "AB"), ("int16", (True,), "AB")):
            with self.subTest(data_type=data_type, words=words, order=order), self.assertRaises(ValueError):
                decode_value(words, data_type, order)
        for words in ((0x7f80, 0), (0xff80, 0), (0x7fc0, 0)):
            with self.assertRaises(ValueError):
                decode_value(words)

    def test_float_signed_zero_and_subnormal_are_preserved(self):
        self.assertEqual(encode_value(-0.0), (0x8000, 0))
        self.assertGreater(decode_value((0, 1)), 0)

    def test_integer_text_does_not_round_through_double(self):
        for text, expected in (("123", 123), ("123.0", 123), ("1e3", 1000), ("4294967295", 4294967295)):
            self.assertEqual(parse_integer(text, "uint32"), expected)
        for text in ("123.0000000000000001", "4294967295.00000001", "1e1000000", "NaN", "Inf", "draft"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_integer(text, "uint32")


class NumericConfigurationTests(unittest.TestCase):
    def test_json_integer_source_is_checked_before_float_rounding(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            for data_type, raw in (("uint16", "65535.0000000000000001"),
                                   ("uint16", "-1e-400"),
                                   ("int16", "-32768.0000000000000001"),
                                   ("uint32", "4294967295.00000001"),
                                   ("int32", "123.0000000000000001")):
                with self.subTest(data_type=data_type, raw=raw):
                    path.write_text('{"mode":"tcp-slave","points":[{"data_type":"' + data_type
                                    + '","source":{"value":' + raw + '}}]}', encoding="utf-8")
                    with self.assertRaises(ConfigurationError) as failure:
                        load_config(path)
                    self.assertEqual(failure.exception.path, "points[0].source.value")
            for raw, expected in (("65535", 65535), ("65535.0", 65535), ("6.5535e4", 65535), ("0e-400", 0)):
                with self.subTest(raw=raw):
                    path.write_text('{"mode":"tcp-slave","points":[{"data_type":"uint16","source":{"value":'
                                    + raw + '}}]}', encoding="utf-8")
                    settings = load_config(path)
                    self.assertEqual(settings.points[0].source.value, expected)
                    json.dumps(settings.to_mapping())

    def test_json_keeps_float_fields_and_inactive_integer_source_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text('{"mode":"rtu-master","access_mode":"read-only","serial":{"port":"TEST"},'
                            '"duration_s":1.5,"points":[{"data_type":"uint16",'
                            '"source":{"value":123.0000000000000001}}]}', encoding="utf-8")
            settings = load_config(path)
            self.assertEqual(settings.master_operation, "read-input")
            self.assertEqual(settings.duration_s, 1.5)
            json.dumps(settings.to_mapping())
            path.write_text('{"mode":"tcp-slave","points":[{"data_type":"float32",'
                            '"source":{"value":12.5}}]}', encoding="utf-8")
            self.assertEqual(load_config(path).points[0].source.value, 12.5)

    def test_legacy_versions_keep_float_type_and_shared_order(self):
        for version, fields in ((1, dict(addresses=[0])), (2, dict(points=[dict(address=0)]))):
            old = TaskConfig.from_mapping(dict(schema_version=version, mode="tcp-slave", byte_order="B4321", **fields))
            self.assertEqual(old.schema_version, 4)
            self.assertEqual(old.enabled_points[0].data_type, "float32")
            self.assertEqual(old.enabled_points[0].encoding_order(old.byte_order), "B4321")
            self.assertEqual(old, TaskConfig.from_mapping(json.loads(json.dumps(old.to_mapping()))))

    def test_per_point_orders_override_shared_default(self):
        settings = config([dict(address=0, data_type="int16"),
                           dict(address=1, data_type="float32"),
                           dict(address=3, data_type="int32", byte_order="B2143")], byte_order="B4321")
        self.assertEqual([p.encoding_order(settings.byte_order) for p in settings.enabled_points],
                         ["B12", "B4321", "B2143"])
        self.assertEqual(settings, TaskConfig.from_mapping(settings.to_mapping()))

    def test_width_drives_boundary_overlap_and_disabled_point_checks(self):
        self.assertEqual(config([dict(address=65535, data_type="uint16")]).enabled_points[0].register_count, 1)
        config([dict(address=0, data_type="int16"), dict(address=1, data_type="uint32")])
        config([dict(address=0), dict(enabled=False, address=0, data_type="unsupported")])
        for points in ([dict(address=65535, data_type="uint32")],
                       [dict(address=0), dict(address=1, data_type="int16")],
                       [dict(address=0, data_type="uint32"), dict(address=0, data_type="int16")]):
            with self.subTest(points=points), self.assertRaises(ConfigurationError):
                config(points)

    def test_invalid_point_types_orders_and_integer_sources_report_paths(self):
        cases = [(dict(data_type="float64"), ".data_type"),
                 (dict(data_type="int16", byte_order="B1234"), ".byte_order"),
                 (dict(data_type="float32", byte_order="AB"), ".byte_order"),
                 (dict(data_type="uint16", source=dict(value=-1)), ".source.value"),
                 (dict(data_type="int32", source=dict(value=1.5)), ".source.value"),
                 (dict(data_type="int16", source=dict(kind="sine")), ".source.kind"),
                 (dict(data_type="uint32", source=dict(value=10 ** 1000)), ".source.value")]
        for point, suffix in cases:
            with self.subTest(point=point), self.assertRaises(ConfigurationError) as failure:
                config([point])
            self.assertEqual(failure.exception.path, "points[0]" + suffix)

    def test_read_only_master_does_not_validate_unused_integer_source(self):
        settings = config([dict(data_type="int16", source=dict(kind="sine", value="draft"))],
                          mode="rtu-master", master_operation="read-holding")
        self.assertEqual(cycle_requests(settings)[0].count, 1)

    def test_no_scaling_fields_are_accepted(self):
        for field in ("scale", "offset"):
            with self.assertRaises(ConfigurationError):
                config([dict(data_type="int16", **{field: 0.1})])


class MixedCommunicationTests(unittest.TestCase):
    def test_mixed_source_encoding_read_grouping_and_wire_budget(self):
        settings = config(mixed_points(), mode="rtu-master", readback_every=1)
        values = SyntheticSource(settings).sample(0)
        self.assertEqual(values, MIXED_VALUES)
        requests = cycle_requests(settings, values, True)
        self.assertEqual([(r.function, r.address, r.count, r.registers) for r in requests],
                         [(16, 0, 6, MIXED_WORDS), (3, 0, 6, MIXED_WORDS)])
        reads = cycle_requests(replace(settings, points=tuple(replace(point, operation="read-input") for point in settings.points)))
        self.assertEqual([(r.function, r.address, r.count) for r in reads], [(4, 0, 6)])
        # FC16 请求 21 字节/应答 8，FC03 请求 8/应答 17；两笔有三段静默。
        self.assertAlmostEqual(minimum_cycle_ms(settings), 54 * 10000 / 115200 + 3 * 1.75)

    def test_single_word_fc06_and_multiword_raw_diagnostics_remain_available(self):
        with self.assertRaises(ConfigurationError):
            config(mixed_points(), mode="rtu-master", master_operation="write-single")
        settings = config([dict(data_type="uint16", source=dict(value=65535))],
                          mode="rtu-master", master_operation="write-single")
        self.assertEqual([(r.function, r.count, r.registers) for r in cycle_requests(settings, [65535])],
                         [(6, 1, (65535,))])

    def test_holes_are_not_merged_and_disabled_points_take_no_space(self):
        settings = config([dict(address=0, data_type="int16"), dict(enabled=False, address=1),
                           dict(address=3, data_type="int32")])
        self.assertEqual([(r.address, r.count) for r in point_write_requests(settings.enabled_points, [1, 2])],
                         [(0, 1), (3, 2)])

    def test_tcp_snapshot_accepts_odd_complete_width_but_not_half_points_or_holes(self):
        settings = config([dict(address=0, data_type="int16"), dict(address=1, data_type="float32"),
                           dict(address=4, data_type="uint16")])
        bank = ReadOnlyRegisters(settings, Clock(), lambda event: None)
        bank.publish({0: 123, 1: 0x4148, 2: 0, 4: 65535})
        for function in (3, 4):
            self.assertEqual(bank.response(1, struct.pack(">BHH", function, 0, 3)),
                             bytes((function, 6)) + bytes.fromhex("007b41480000"))
            self.assertEqual(bank.response(1, struct.pack(">BHH", function, 4, 1)),
                             bytes((function, 2, 255, 255)))
            for address, count in ((0, 2), (2, 1), (0, 5), (3, 1), (65535, 2)):
                self.assertEqual(bank.response(1, struct.pack(">BHH", function, address, count)),
                                 bytes((function | 128, 2)))

    def test_live_values_keep_integer_precision_partial_raw_and_same_value_updates(self):
        settings = config([dict(point, area="holding") for point in mixed_points()])
        live = LiveValues(settings)
        event = Event("register_values", "RX", details=dict(area="holding", direction="rx", address=0,
                      registers=MIXED_WORDS, function=16, sampled_s=.1, quality="received"))
        live.consume(event)
        rows = live.snapshot(.1, "running")["rows"]
        self.assertEqual(tuple(row["value"] for row in rows), MIXED_VALUES)
        self.assertEqual([row["data_type"] for row in rows], [p["data_type"] for p in mixed_points()])
        sequence = live.sequence
        live.consume(event)
        self.assertEqual(live.sequence, sequence + 4)
        live.consume(Event("register_values", "RX", details=dict(event.details, address=1, registers=(0x7f80,), function=6)))
        self.assertEqual(live.rows["holding:0:rx"]["value"], 305419896)  # 非相交点未改变。
        self.assertEqual(live.rows["holding:2:rx"]["data_type"], "float32")
        # 地址 1 的半点只显示原始寄存器，不跨请求拼接为浮点数。
        partial = next(row for row in live.snapshot(.1, "running")["rows"] if row["point"] == 2)
        self.assertEqual((partial["value"], partial["quality"], partial["registers"]), (None, "partial", [0x7f80]))

    def test_rtu_raw_writes_preserve_original_words_and_independent_input_bank(self):
        settings = config(mixed_points(), mode="rtu-slave", access_mode="read-write", schema_version=3)
        events = []
        bank = RtuRegisters(settings, Clock(), events.append)
        bank.publish(dict(enumerate(MIXED_WORDS)))
        self.assertEqual(bank.response(1, bytes.fromhex("060000ffff")), bytes.fromhex("060000ffff"))
        self.assertEqual(bank.response(1, bytes.fromhex("0300000001")), bytes.fromhex("0302ffff"))
        self.assertEqual(bank.response(1, bytes.fromhex("0400000001")), bytes.fromhex("0402ff85"))
        bank.response(1, bytes.fromhex("060000ffff"))
        self.assertEqual(sum(e.code == "register_values" for e in events), 2)
        # 测试从站仍可保存无效浮点位模式，供被测采集程序验证坏数处理。
        self.assertEqual(bank.response(1, bytes.fromhex("100001000204807f0000")), bytes.fromhex("1000010002"))
        self.assertEqual(bank.response(1, bytes.fromhex("0300010002")), bytes.fromhex("0304807f0000"))

    def test_rtu_master_mixed_write_readback_and_live_decode(self):
        clock = FakeControl()
        peer = Peer(clock, delay=.005)
        settings = config(mixed_points(), mode="rtu-master", duration_s=.2, readback_every=1)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(settings, Path(root) / "run", control=clock,
                engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
            result = runner.run()
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)
            self.assertEqual(peer.writes[0][1][1:-2], bytes.fromhex("10000000060cff8548410000567812343412"))
            self.assertEqual(tuple(peer.registers[index] for index in range(6)), MIXED_WORDS)
            rows = runner.live.snapshot(clock.now(), "stopped")["rows"]
            self.assertEqual([row["value"] for row in rows if row["direction"] == "rx"], list(MIXED_VALUES))

    def test_mixed_snapshot_over_actual_tcp(self):
        ready, stop = threading.Event(), threading.Event()
        engines, results = [], []
        settings = config(mixed_points(), duration_s=0, schema_version=3)
        def factory(c, control, emit):
            engine = TcpSlaveEngine(replace(c, port=0), control, emit)
            engines.append(engine)
            publish = engine.publish
            def signal(registers):
                publish(registers)
                ready.set()
            engine.publish = signal
            return engine
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(settings, Path(root) / "run", stop=stop, engine_factory=factory)
            thread = threading.Thread(target=lambda: results.append(runner.run()))
            thread.start()
            try:
                self.assertTrue(ready.wait(3), results)
                with socket.create_connection(engines[0].bound_address, timeout=1) as peer:
                    for function in (3, 4):
                        peer.sendall(frame(struct.pack(">BHH", function, 0, 6)))
                        self.assertEqual(receive(peer), frame(bytes((function, 12)) + bytes.fromhex("ff8548410000567812343412")))
            finally:
                stop.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]["exit_code"], 130)


if __name__ == "__main__":
    unittest.main()
