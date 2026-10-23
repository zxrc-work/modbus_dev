"""用独立字节对端验证 RTU 交易、调度、异常恢复及日志轮转期间的持续通信。"""
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from modbus_dev.application.control import Control
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.mapping import write_requests
from modbus_dev.domain.transactions import Request, StopRequested
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.reporting.journal import Journal
from modbus_dev.reporting.segments import event_paths


def frame(data):
    crc = 0xffff
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return data + struct.pack("<H", crc)


class FakeControl(Control):
    def __init__(self, stop_at=None):
        super().__init__()
        self.time = 0.
        self.stop_at = stop_at

    def now(self):
        return self.time

    def advance(self, seconds):
        self.time += max(0, seconds)
        if self.stop_at is not None and self.time >= self.stop_at:
            self.stop.set()

    def wait_until(self, deadline):
        self.advance(deadline - self.time)
        self.check()


class Peer:
    def __init__(self, clock, actions=(), delay=.035):
        self.clock, self.actions, self.delay = clock, list(actions), delay
        self.timeout = self.write_timeout = 0
        self.pending, self.writes = [], []
        self.registers = {}
        self.closed = False

    @property
    def in_waiting(self):
        return sum(len(data) for at, data in self.pending if at <= self.clock.now())

    def read(self, size=1):
        self.pending.sort(key=lambda item: item[0])
        end = self.clock.now() + self.timeout
        if not self.pending or self.pending[0][0] > end:
            self.clock.advance(self.timeout)
            return b""
        at, data = self.pending.pop(0)
        self.clock.advance(at - self.clock.now())
        if len(data) > size:
            self.pending.insert(0, (at, data[size:]))
        return data[:size]

    def write(self, data):
        self.writes.append((self.clock.now(), data))
        if frame(data[:-2]) != data:
            raise AssertionError("Bad production request CRC")
        action = self.actions.pop(0) if self.actions else "ack"
        if action == "io_error":
            raise OSError("Cable unplugged")
        if action == "short":
            return len(data) - 1
        unit, function, address, count = struct.unpack(">BBHH", data[:6])
        if function == 16:
            words = struct.unpack(">" + "H" * count, data[7:-2])
            self.registers.update({address + i: value for i, value in enumerate(words)})
            response = frame(data[:6])
        else:
            words = [self.registers.get(address + i, 0) for i in range(count)]
            if action == "mismatch":
                words[0] ^= 1
            response = frame(bytes((unit, function, count * 2)) + struct.pack(">" + "H" * count, *words))
        at = self.clock.now() + self.delay
        if action == "timeout":
            return len(data)
        if action == "late":
            at = self.clock.now() + .099
        if action == "overrun":
            self.clock.advance(.35)
        if action == "crc":
            response = response[:-1] + bytes((response[-1] ^ 1,))
        if action == "wrong":
            response = frame(bytes((unit, function)) + struct.pack(">HH", address + 2, count))
        if action == "exception":
            response = frame(bytes((unit, function | 0x80, 2)))
        if action == "split":
            self.pending.extend(((at, response[:3]), (at + .01, response[3:])))
        else:
            self.pending.append((at, response))
        return len(data)

    def flush(self):
        raise AssertionError("Do not call Windows polling flush")

    def close(self):
        self.closed = True


def config(**kwargs):
    return TaskConfig.from_mapping(dict(serial=dict(port="TEST"), duration_s=.4, **kwargs))


class SessionTests(unittest.TestCase):
    def exercise(self, actions=(), *, duration=.4, delay=.035, stop_at=None, setup=None,
                 journal_factory=Journal, **options):
        clock = FakeControl(stop_at)
        peer = Peer(clock, actions, delay)
        settings = TaskConfig.from_mapping(dict(serial=dict(port="TEST"), duration_s=duration, **options))
        factory = (setup(clock, peer) if setup else lambda _: peer)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(settings, Path(root) / "run", control=clock,
                                   engine_factory=lambda c, t, e: RtuEngine(c, t, e, factory),
                                   journal_factory=journal_factory)
            result = runner.run()
            summary = json.loads((runner.directory / "summary.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for path in event_paths(runner.directory)
                      for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(summary["run"]["exit_code"], result["exit_code"])
        self.assertTrue(peer.closed)
        return result, peer, events

    def test_log_retention_does_not_stop_communication(self):
        # 缩小分片触发多次淘汰，但生产通信路径与正常会话完全相同。
        result, peer, events = self.exercise(duration=1, delay=.005, logging=dict(max_files=2),
            journal_factory=lambda directory, config: Journal(directory, config, max_bytes=2048))
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["counters"]["ack"], 10)
        self.assertEqual(len(peer.writes), 10)
        self.assertGreater(events[0]["sequence"], 1)

    def test_100_and_200ms_absolute_schedule(self):
        for period, count in ((100, 4), (200, 2)):
            result, peer, _ = self.exercise(period_ms=period)
            self.assertEqual(result["counters"]["ack"], count)
            self.assertEqual(result["exit_code"], 0)
            self.assertAlmostEqual(result["metrics"]["send_interval_ms"]["mean"], period)
            self.assertAlmostEqual(result["metrics"]["response_ms"]["mean"], 35)

    def test_known_wire_frame(self):
        result, peer, _ = self.exercise(duration=.1)
        self.assertEqual(peer.writes[0][1].hex(), "0110000000020400000000f3af")

    def test_independent_points_keep_addresses_and_values_paired(self):
        result, peer, events = self.exercise(duration=.8, delay=.001, readback_every=1,
            period_ms=200, timeout_ms=170,
            points=[dict(address=6, source=dict(value=-3.5)),
                    dict(address=0, source=dict(kind="sine", value=10, amplitude=2, frequency_hz=1)),
                    dict(enabled=False, address=2, source=dict(value=999)),
                    dict(address=2, source=dict(value=12.5))])
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["counters"].get("readback_mismatch", 0), 0)
        # 直接解码独立串口对端的寄存器，验证排序后没有把数据串到另一点。
        words = peer.registers
        self.assertEqual(struct.unpack(">f", struct.pack(">HH", words[2], words[3]))[0], 12.5)
        self.assertEqual(struct.unpack(">f", struct.pack(">HH", words[6], words[7]))[0], -3.5)
        self.assertNotIn(4, words)
        samples = [e["details"]["values"] for e in events if e["code"] == "source_sample"]
        self.assertEqual(samples[0], [-3.5, 10.0, 12.5])
        self.assertGreater(samples[1][1], samples[0][1])

    def test_scheduling_tolerance_boundaries_and_continuation(self):
        for period, percent, delay_ms, warning in (
                (100, 10, 9, False), (100, 10, 10, False),
                (100, 10, 10.01, True), (200, 10, 20, False),
                (200, 10, 20.01, True), (100, 5, 6, True),
                (100, 20, 15, False), (100, 0, 0, False), (100, 0, 1, True)):
            with self.subTest(period=period, percent=percent, delay_ms=delay_ms):
                def setup(clock, peer):
                    original = clock.wait_until
                    def wait(deadline):
                        original(deadline)
                        if abs(deadline - .1) < 1e-9:
                            clock.advance(delay_ms / 1000)
                    clock.wait_until = wait
                    return lambda _: peer
                result, _, events = self.exercise(setup=setup, period_ms=period,
                                                 schedule_tolerance_percent=percent)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["counters"]["ack"], 400 // period)
                self.assertEqual(result["counters"].get("schedule_late", 0), int(warning))
                warnings = [e for e in events if e["code"] == "schedule_late"]
                self.assertEqual(len(warnings), int(warning))
                if warnings:
                    self.assertEqual(warnings[0]["level"], "WARNING")
                    self.assertAlmostEqual(warnings[0]["details"]["lateness_ms"], delay_ms)

    def test_readback_does_not_count_as_late_cycle(self):
        result, _, events = self.exercise(delay=.015, readback_every=1)
        self.assertEqual(result["counters"]["read"], 4)
        self.assertGreater(result["metrics"]["send_lateness_ms"]["max"], 10)
        self.assertNotIn("schedule_late", result["counters"])

    def test_tolerance_configuration_validation_and_roundtrip(self):
        self.assertEqual(config().schedule_tolerance_ms, 10)
        for value in (0, 5.5, 100):
            settings = config(schedule_tolerance_percent=value)
            self.assertEqual(TaskConfig.from_mapping(settings.to_mapping()), settings)
        for value in (-1, 101, True, "10", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(schedule_tolerance_percent=value)

    def test_split_reply(self):
        result, _, _ = self.exercise(["split"], duration=.1)
        self.assertEqual(result["counters"]["ack"], 1)
        self.assertAlmostEqual(result["metrics"]["response_ms"]["mean"], 45)

    def test_late_ack_gets_silence_and_remains_ambiguous(self):
        result, peer, events = self.exercise(["late"])
        self.assertEqual(result["counters"]["timeout"], 1)
        self.assertEqual(result["counters"]["ack_ambiguous"], 3)
        self.assertGreaterEqual(peer.writes[1][0], .2 + 3.5 * 10 / 9600 - 1e-12)
        self.assertTrue(any(e["code"] == "idle_rx" for e in events))

    def test_bad_crc_wrong_address_and_exceptions(self):
        for action in ("crc", "wrong", "exception"):
            result, peer, events = self.exercise([action])
            self.assertEqual(len(peer.writes), 4)
            if action == "exception":
                self.assertEqual(result["counters"]["exception"], 1)
                self.assertEqual(result["counters"]["ack"], 3)
                event = next(e for e in events if e["code"] == "transaction")
                self.assertEqual(event["details"]["exception_code"], 2)
            else:
                self.assertEqual(result["counters"]["bad_reply"], 1)
                self.assertEqual(result["counters"]["ack_ambiguous"], 3)

    def test_short_write_and_disconnect_reconnect(self):
        for action in ("short", "io_error"):
            result, peer, _ = self.exercise([action], duration=1.5)
            self.assertEqual(result["counters"]["io_errors"], 1)
            self.assertEqual(result["counters"]["port_opens"], 2)
            self.assertGreater(result["counters"]["ack_ambiguous"], 0)
            self.assertEqual(result["counters"]["write_attempts"], len(peer.writes))

    def test_missing_port_retries_after_one_second(self):
        def setup(clock, peer):
            calls = []
            def open_port(_):
                calls.append(clock.now())
                if len(calls) == 1:
                    raise OSError("Missing port")
                self.assertGreaterEqual(calls[1] - calls[0], 1)
                return peer
            return open_port
        result, _, _ = self.exercise(duration=1.5, setup=setup)
        self.assertEqual(result["counters"]["open_errors"], 1)
        self.assertGreater(result["counters"]["ack"], 0)

    def test_overrun_does_not_catch_up(self):
        result, peer, _ = self.exercise(["overrun"], duration=.8)
        self.assertGreaterEqual(result["counters"]["skipped_slots"], 3)
        self.assertGreaterEqual(peer.writes[1][0], .5)

    def test_strict_timeout_stops_immediately(self):
        result, peer, _ = self.exercise(["timeout"], policy="strict")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(len(peer.writes), 1)

    def test_fc03_and_fc04_readback(self):
        for function in (3, 4):
            result, peer, _ = self.exercise(duration=.1, delay=.01, readback_every=1, read_function=function)
            self.assertEqual(result["counters"]["read"], 1)
            self.assertEqual([data[1] for _, data in peer.writes], [16, function])
            self.assertGreaterEqual(peer.writes[1][0] - .11, 3.5 * 10 / 9600 - 1e-12)

    def test_readback_mismatch_fails_strict_policy(self):
        result, _, events = self.exercise(["ack", "mismatch"], delay=.01, readback_every=1, policy="strict")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["counters"]["readback_mismatch"], 1)
        self.assertTrue(any(e["code"] == "readback_mismatch" for e in events))

    def test_cooperative_stop_inside_receive_closes_port(self):
        result, peer, _ = self.exercise(["timeout"], stop_at=.125)
        self.assertEqual(result["exit_code"], 130)
        self.assertLess(result["elapsed_s"], .14)
        self.assertTrue(result["ack_correlation_uncertain"])

    def test_write_latency_is_inside_total_budget(self):
        def setup(clock, peer):
            original = peer.write
            def write(data):
                clock.advance(.06)
                return original(data)
            peer.write = write
            return lambda _: peer
        result, _, _ = self.exercise(duration=.1, setup=setup)
        self.assertEqual(result["counters"]["timeout"], 1)

    def test_split_idle_data_restarts_silence(self):
        def setup(clock, peer):
            ack = frame(bytes.fromhex("011000000002"))
            peer.pending = [(.199, ack[:4]), (.202, ack[4:])]
            return lambda _: peer
        result, peer, _ = self.exercise(["timeout"], setup=setup)
        self.assertGreaterEqual(peer.writes[1][0], .202 + 3.5 * 10 / 9600 - 1e-12)

    def test_log_failure_stops_and_releases_port(self):
        class FailedJournal(Journal):
            def emit(self, event):
                accepted = super().emit(event)
                if event.code == "tx":
                    self._error = "Injected disk failure"
                    self.failed.set()
                return accepted
        with tempfile.TemporaryDirectory() as root:
            clock = FakeControl()
            peer = Peer(clock)
            runner = SessionRunner(config(), Path(root) / "run", control=clock,
                                   journal_factory=FailedJournal,
                                   engine_factory=lambda c,t,e: RtuEngine(c,t,e,lambda _:peer))
            result = runner.run()
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(len(peer.writes), 1)
            self.assertTrue(peer.closed)
            report = json.loads((runner.directory / "summary.json").read_text())
            self.assertFalse(report["evidence_complete"])
            self.assertEqual(report["run"]["state"], "failed")

    def test_close_error_is_a_failure(self):
        def setup(clock, peer):
            def close():
                peer.closed = True
                raise OSError("Close failed")
            peer.close = close
            return lambda _: peer
        result, _, _ = self.exercise(duration=.1, setup=setup)
        self.assertEqual(result["exit_code"], 1)

    def test_continuous_noise_is_bounded(self):
        def setup(clock, peer):
            peer.pending = [(.199 + i * .002, b"\xff") for i in range(41)]
            return lambda _: peer
        result, peer, _ = self.exercise(["timeout"], setup=setup)
        self.assertEqual(result["counters"]["schedule_missed"], 1)
        self.assertAlmostEqual(peer.writes[1][0], .3)

    def test_noncontiguous_groups_keep_sending_after_ambiguity(self):
        result, peer, _ = self.exercise(["timeout"], addresses=[0, 4], delay=.005)
        # First slot loses one group; subsequent slots still write both points.
        self.assertEqual(len(peer.writes), 7)

    def test_single_session_guard_and_runner_cannot_reenter(self):
        with tempfile.TemporaryDirectory() as root:
            from modbus_dev.application.session import _ACTIVE_SESSION
            runner = SessionRunner(config(), Path(root) / "run")
            with _ACTIVE_SESSION, self.assertRaisesRegex(RuntimeError, "one session"):
                runner.run()

    def test_unsupported_run_modes_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as root:
            for settings in (config(source=dict(kind="dyn", path="missing.dyn", channels=["P"])),):
                with self.assertRaises(ValueError):
                    SessionRunner(settings, Path(root) / "run")
            self.assertFalse((Path(root) / "run").exists())


class LibraryEvaluationTests(unittest.TestCase):
    def test_stock_send_drains_late_data_without_waiting_for_silence(self):
        from pymodbus.client import ModbusSerialClient
        clock = FakeControl()
        peer = Peer(clock)
        peer.pending = [(0, frame(bytes.fromhex("011000000002")))]
        client = ModbusSerialClient("TEST", timeout=.08, retries=0)
        client.socket = peer
        client.send(bytes.fromhex("0110000000020400000000f3af"))
        self.assertEqual(peer.writes[0][0], 0.)
        self.assertEqual(peer.pending[0][0], .035)  # Old reply was silently drained.

    def test_stock_transaction_restarts_receive_wait_without_total_deadline(self):
        from pymodbus.client import ModbusSerialClient
        from pymodbus.exceptions import ModbusIOException
        client = ModbusSerialClient("TEST", timeout=.08, retries=0)
        elapsed, writes = [], []
        client.connect = lambda: True
        def recv(_):
            elapsed.append(.07)
            return b"\xff" if len(elapsed) < 4 else b""
        client.recv = recv
        client.transaction.low_level_send = lambda data, **_: writes.append(data)
        from pymodbus.pdu.register_message import WriteMultipleRegistersRequest
        with self.assertRaises(ModbusIOException):
            client.execute(False, WriteMultipleRegistersRequest(dev_id=1, address=0, registers=[0, 0]))
        self.assertEqual(len(writes), 1)  # retries=0 works, but is not an overall deadline.
        self.assertGreater(sum(elapsed), .08)


class MappingTests(unittest.TestCase):
    def test_independent_expected_byte_orders(self):
        expected = {"B1234": (0x3f80, 0), "B4321": (0, 0x803f),
                    "B2143": (0x803f, 0), "B3412": (0, 0x3f80)}
        for order, words in expected.items():
            self.assertEqual(write_requests([0], [1.0], order)[0].registers, words)


if __name__ == "__main__":
    unittest.main()
