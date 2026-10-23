"""覆盖四种 RTU 操作、权限、独立存储、异常、广播以及实际发送证据。"""
import struct
from pathlib import Path
import tempfile
import unittest

from modbus_dev.application.live import LiveValues, decode_float
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event, ConfigurationError
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.trend import TrendHistory
from modbus_dev.domain.live_policy import CURVE_MAX_POINTS
from modbus_dev.domain.transactions import Request
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.engines.rtu_registers import RtuRegisters

from test_rtu_slave import Clock, frame
from test_rtu_session import Peer
import test_rtu_session


def config(mode="rtu-slave", **options):
    return TaskConfig.from_mapping(dict(mode=mode, serial=dict(port="TEST", baud=115200),
                                       access_mode="read-write", **options))


class RegisterTests(unittest.TestCase):
    def bank(self, **options):
        self.events = []
        bank = RtuRegisters(config(**options), Clock(), self.events.append)
        bank.publish({0: 0x4120, 1: 0, 2: 0x41a0, 3: 0})
        return bank

    def test_fc16_updates_holding_and_fc03_reads_back_without_touching_inputs(self):
        bank = self.bank()
        self.assertEqual(bank.response(1, bytes.fromhex("1000000002043fa00000")), bytes.fromhex("1000000002"))
        self.assertEqual(bank.response(1, bytes.fromhex("0300000002")), bytes.fromhex("03043fa00000"))
        self.assertEqual(bank.response(1, bytes.fromhex("0400000002")), bytes.fromhex("040441200000"))
        bank.publish({0: 0x4130, 1: 0, 2: 0x41b0, 3: 0})
        self.assertEqual(bank.holding[0], 0x3fa0)
        self.assertEqual(bank.registers[0], 0x4130)

    def test_fc06_echo_and_16bit_reads_allow_half_float_registers(self):
        bank = self.bank()
        request = bytes.fromhex("0600011234")
        self.assertEqual(bank.response(1, request), request)
        self.assertEqual(bank.response(1, bytes.fromhex("0300010001")), bytes.fromhex("03021234"))
        self.assertEqual(bank.response(1, bytes.fromhex("0400010001")), bytes.fromhex("04020000"))

    def test_readonly_returns_exception_and_preserves_banks(self):
        bank = RtuRegisters(TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"))),
                            Clock(), lambda _: None)
        bank.publish({0: 0, 1: 0})
        for request in (bytes.fromhex("0600001234"), bytes.fromhex("1000000002043fa00000")):
            self.assertEqual(bank.response(1, request), bytes((request[0] | 128, 1)))
        self.assertEqual(bank.holding, {0: 0, 1: 0})

    def test_invalid_fc16_is_atomic_and_rejects_byte_count_length_holes_and_overflow(self):
        bank = self.bank()
        before = dict(bank.holding)
        cases = [("1000000002033fa00000", 3), ("1000000002043fa000", 3),
                 ("1000000002043fa0000000", 3), ("100000000000", 3),
                 ("1000030002043fa00000", 2), ("10ffff0002043fa00000", 2),
                 ("0300000000", 3), ("0400040001", 2)]
        for raw, code in cases:
            with self.subTest(raw=raw):
                request = bytes.fromhex(raw)
                self.assertEqual(bank.response(1, request), bytes((request[0] | 128, code)))
                self.assertEqual(bank.holding, before)

    def test_broadcast_applies_write_silently_and_other_unit_does_not(self):
        bank = self.bank()
        self.assertIsNone(bank.response(0, bytes.fromhex("0600003fa0")))
        self.assertEqual(bank.holding[0], 0x3fa0)
        self.assertIsNone(bank.response(2, bytes.fromhex("0600001234")))
        self.assertIsNone(bank.response(0, bytes.fromhex("060000")))
        self.assertEqual(bank.holding[0], 0x3fa0)

    def test_slave_tx_values_are_not_emitted_until_full_driver_submission(self):
        bank = self.bank()
        bank.response(1, bytes.fromhex("0400000002"))
        self.assertFalse(any(e.code == "register_values" for e in self.events))
        bank.response_submitted(.1)
        self.assertEqual(self.events[-1].details["direction"], "tx")
        self.assertEqual(self.events[-1].details["registers"], (0x4120, 0))

    def test_cross_point_reads_and_writes_keep_only_each_points_registers(self):
        settings = config(points=[dict(address=address) for address in (0, 2, 4)])
        for function in (3, 4, 16):
            with self.subTest(function=function):
                live = LiveValues(settings)
                bank = RtuRegisters(settings, Clock(), live.consume)
                bank.publish(dict(enumerate((0x3f80, 0x1111, 0x4000, 0, 0x3333, 0))))
                request = struct.pack(">BHH", function, 1, 4)
                if function == 16:
                    request += bytes.fromhex("081111400000003333")
                bank.response(1, request)
                if function in (3, 4):
                    bank.response_submitted(.1)
                rows = live.snapshot(.1, "running")["rows"]
                self.assertEqual([row["point"] for row in rows], [0, 1, 2])
                self.assertEqual([row["registers"] for row in rows],
                                 [[0x1111], [0x4000, 0], [0x3333]])
                self.assertEqual([row["value"] for row in rows], [None, 2., None])
                self.assertEqual([row["quality"] for row in rows],
                                 ["partial", "received" if function == 16 else "submitted", "partial"])
                self.assertTrue(all(row["direction"] == ("rx" if function == 16 else "tx")
                                    for row in rows))


class OperationTests(unittest.TestCase):
    def test_write_confirmation_keeps_uncertainty_after_timeout(self):
        for actions, quality in (((), "confirmed"), (("timeout",), "ambiguous")):
            with self.subTest(actions=actions), tempfile.TemporaryDirectory() as root:
                clock = test_rtu_session.FakeControl()
                peer = Peer(clock, actions, delay=.005)
                runner = SessionRunner(config("rtu-master", duration_s=.3), Path(root) / "run", control=clock,
                    engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
                result = runner.run()
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(result["counters"].get("ack_ambiguous" if actions else "ack"),
                                 2 if actions else 3)
                self.assertEqual(result["counters"].get("timeout", 0), 1 if actions else 0)
                snapshot = runner.live.snapshot(clock.now(), "running")
                self.assertEqual(len(snapshot["rows"]), 1)
                row = snapshot["rows"][0]
                self.assertEqual(row["quality"], quality)
                self.assertEqual(row["direction"], "tx")
                self.assertEqual(row["registers"], [0, 0])
                history = TrendHistory()
                history.sample(snapshot, clock.now())
                self.assertEqual(history.series[row["key"]][-1][1], None if actions else 0.)

    def test_each_operation_builds_correct_requests_and_budgets(self):
        for operation, functions in (("read-holding", [3]), ("read-input", [4]),
                                     ("write-single", [6, 6]), ("write-multiple", [16])):
            settings = config("rtu-master", master_operation=operation)
            requests = cycle_requests(settings, [1.25])
            self.assertEqual([r.function for r in requests], functions)
            if functions[0] in (3, 4):
                self.assertFalse(requests[0].registers)
        settings = config("rtu-master", master_operation="write-single", readback_every=1)
        self.assertEqual([r.function for r in cycle_requests(settings, [1.25], True)], [6, 6, 3])

    def test_permissions_and_readback_validation(self):
        for operation in ("write-single", "write-multiple"):
            with self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(dict(mode="rtu-master", serial=dict(port="TEST"),
                                             access_mode="read-only", master_operation=operation))
        with self.assertRaises(ConfigurationError):
            config("rtu-master", master_operation="read-input", readback_every=1)
        with self.assertRaises(ConfigurationError):
            config("rtu-master", readback_every=1, read_function=4)
        with self.assertRaises(ConfigurationError):
            config("rtu-master", master_operation=[])

    def test_fc06_codec_echo_validation(self):
        clock = Clock()
        settings = config("rtu-master", master_operation="write-single")
        request = Request(6, 0, 1, (0x3fa0,))
        class EchoPeer(Peer):
            def write(self, data):
                self.writes.append((clock.now(), data))
                self.pending.append((clock.now() + .005, data))
                return len(data)
        port = EchoPeer(clock)
        engine = RtuEngine(settings, clock, lambda _: None, lambda _: port)
        engine.connect()
        self.assertEqual(engine.packet(request), frame(bytes.fromhex("010600003fa0")))
        self.assertEqual(engine.exchange(request, .08).status, "ack")
        engine.close()

    def test_read_polling_does_not_write_or_compare_with_local_source(self):
        helper = test_rtu_session.SessionTests()
        result, peer, events = helper.exercise(duration=.3, master_operation="read-input",
                                              source=dict(value=999), delay=.005)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["counters"]["read"], 3)
        self.assertNotIn("readback_mismatch", result["counters"])
        self.assertTrue(all(packet[1] == 4 for _, packet in peer.writes))
        live = [e for e in events if e["code"] == "register_values"]
        self.assertTrue(live)
        self.assertTrue(all(e["details"]["direction"] == "rx" for e in live))


class LiveTests(unittest.TestCase):
    def event(self, registers=(0x3fa0, 0), **options):
        return Event("register_values", "数值", "DEBUG", details=dict(
            area="holding", direction="rx", address=0, registers=registers,
            function=3, sampled_s=0., quality="received", **options))

    def test_all_byte_orders_decode_and_nonfinite_values_are_json_safe(self):
        self.assertEqual(decode_float((0x3fa0, 0), "B1234"), 1.25)
        self.assertEqual(decode_float((0, 0xa03f), "B4321"), 1.25)
        self.assertEqual(decode_float((0xa03f, 0), "B2143"), 1.25)
        self.assertEqual(decode_float((0, 0x3fa0), "B3412"), 1.25)
        live = LiveValues(config())
        live.consume(self.event((0x7f80, 0)))
        row = live.snapshot(1., "running")["rows"][0]
        self.assertIsNone(row["value"])
        self.assertEqual(row["quality"], "nonfinite")

    def test_partial_registers_are_not_fabricated_as_float_and_state_is_bounded(self):
        live = LiveValues(config())
        for _ in range(1000):
            live.consume(self.event((0x3fa0,)))
        snapshot = live.snapshot(0., "running")
        self.assertEqual(len(snapshot["rows"]), 1)
        self.assertIsNone(snapshot["rows"][0]["value"])
        self.assertEqual(snapshot["rows"][0]["quality"], "partial")

    def test_trend_is_bounded_ten_seconds_and_stale_samples_are_gaps(self):
        live = LiveValues(config())
        history = TrendHistory()
        for index in range(1000):
            now = index / 2
            event = self.event()
            event.details["sampled_s"] = now
            live.consume(event)
            history.sample(live.snapshot(now, "running"), now)
        points = next(iter(history.series.values()))
        self.assertLessEqual(len(points), CURVE_MAX_POINTS)
        self.assertGreaterEqual(points[0][0], now - 10)
        history.sample(live.snapshot(now + 2, "running"), now + 2)
        self.assertIsNone(points[-1][1])
        history.sample(live.snapshot(now + 2.5, "stopped"), now + 2.5)
        self.assertIsNone(points[-1][1])
