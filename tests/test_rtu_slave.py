"""通过独立主站字节和确定性驱动时钟验证从站、独立测点及故障恢复。"""
from pathlib import Path
import struct
import tempfile
import unittest

from modbus_dev.application.control import Control
from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.engines.rtu_slave import RtuSlaveEngine


def frame(data):
    crc = 0xffff
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return data + struct.pack("<H", crc)


class Clock(Control):
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


class Port:
    def __init__(self, clock, incoming=(), write_action=None):
        self.clock = clock
        self.incoming = list(incoming)
        self.writes = []
        self.timeout = self.write_timeout = 0
        self.write_action = write_action
        self.closed = False

    @property
    def in_waiting(self):
        return sum(len(data) for at, data in self.incoming if at <= self.clock.now())

    def read(self, count):
        end = self.clock.now() + self.timeout
        if not self.incoming or self.incoming[0][0] > end:
            self.clock.advance(self.timeout)
            return b""
        at, data = self.incoming.pop(0)
        self.clock.advance(at - self.clock.now())
        if len(data) > count:
            self.incoming.insert(0, (at, data[count:]))
        return data[:count]

    def write(self, data):
        self.writes.append((self.clock.now(), data))
        if self.write_action == "error":
            raise OSError("Disconnected")
        if self.write_action == "short":
            return len(data) - 1
        return len(data)

    def flush(self):
        raise AssertionError("No serial flush allowed")

    def close(self):
        self.closed = True


def config(**options):
    return TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST"), **options))


class RtuSlaveWireTests(unittest.TestCase):
    def test_pinned_library_framer_does_not_deliver_unknown_function(self):
        from pymodbus.framer.rtu import FramerRTU
        from pymodbus.pdu.decoders import DecodePDU
        # A silence-bounded valid unknown request still needs exception 01.
        packet = frame(b"\x01\x41\x00\x00\x00\x01")
        self.assertEqual(FramerRTU(DecodePDU(True)).decode(packet)[3], b"")

    def exercise(self, packets, end=.2, **options):
        clock = Clock()
        port = Port(clock, packets)
        events = []
        engine = RtuSlaveEngine(config(**options), clock, events.append, lambda _: port)
        engine.connect()
        engine.publish({0: 0x4148, 1: 0, 2: 0x4148, 3: 0})
        while clock.now() < end:
            engine.poll(min(end, clock.now() + .02))
        engine.close()
        self.assertTrue(port.closed)
        return port, events

    def test_fixed_crc_vectors_both_reads_and_segmented_delivery(self):
        request = bytes.fromhex("010300000002c40b")
        self.assertEqual(request, frame(b"\x01\x03\x00\x00\x00\x02"))
        port, _ = self.exercise([(0.01, request[:3]), (.011, request[3:]),
                                 (.08, frame(b"\x01\x04\x00\x00\x00\x02"))])
        self.assertEqual([data for _, data in port.writes],
                         [frame(bytes((1, fc, 4)) + struct.pack(">f", 12.5)) for fc in (3, 4)])
        self.assertGreaterEqual(port.writes[0][0], .011 + config().serial.gap_ms / 1000 - 1e-9)

    def test_non_read_functions_rejected_without_mutation(self):
        functions = [fc for fc in range(1, 128) if fc not in (3, 4)]
        packets = [(.01 + i * .05, frame(bytes((1, fc, 0, 0, 0, 1)))) for i, fc in enumerate(functions)]
        packets.append((6.5, frame(b"\x01\x03\x00\x00\x00\x02")))
        port, _ = self.exercise(packets, end=6.6)
        self.assertEqual([data for _, data in port.writes[:-1]],
                         [frame(bytes((1, fc | 128, 1))) for fc in functions])
        self.assertEqual(port.writes[-1][1], frame(b"\x01\x03\x04" + struct.pack(">f", 12.5)))

    def test_broadcast_and_other_units_are_silent(self):
        packets = [(.01 + i * .05, frame(bytes((unit, fc, 0, 0, 0, 2))))
                   for i, (unit, fc) in enumerate(((0, 3), (0, 16), (2, 3), (2, 6)))]
        port, events = self.exercise(packets, end=.25)
        self.assertEqual(port.writes, [])
        self.assertEqual(sum(e.code == "ignored_unit" for e in events), 3)
        self.assertEqual(sum(e.code == "broadcast_processed" for e in events), 1)

    def test_invalid_crc_noise_and_partial_frame_recover_after_silence(self):
        good = frame(b"\x01\x03\x00\x00\x00\x02")
        bad = good[:-1] + bytes((good[-1] ^ 1,))
        port, events = self.exercise([(.01, bad), (.05, b"\xff" + good),
                                     (.09, good[:3]), (.14, good)])
        self.assertEqual(len(port.writes), 1)
        self.assertEqual(sum(e.code == "rtu_frame_rejected" for e in events), 3)

    def test_overflow_discards_entire_burst_and_recovers(self):
        good = frame(b"\x01\x03\x00\x00\x00\x02")
        port, events = self.exercise([(.01, b"\x55" * 700 + good), (.1, good)])
        self.assertEqual(len(port.writes), 1)
        self.assertTrue(any(e.details.get("reason") == "frame exceeds 256 bytes" for e in events))

    def test_invalid_quantity_unmapped_address_and_malformed_pdu(self):
        pdus = [(b"\x03\x00\x00\x00\x00", 3), (b"\x03\x00\x03\x00\x02", 2),
                (b"\x04\x00\x04\x00\x01", 2), (b"\x03", 3)]
        port, _ = self.exercise([(.01 + i * .04, frame(b"\x01" + pdu)) for i, (pdu, _) in enumerate(pdus)])
        self.assertEqual([data for _, data in port.writes],
                         [frame(bytes((1, pdu[0] | 128, code))) for pdu, code in pdus])


class RtuSlaveSessionTests(unittest.TestCase):
    def run_session(self, *, packets=(), stop_at=None, write_action=None, fail_first=False, **options):
        clock = Clock(stop_at)
        ports, attempts = [], []
        def open_port(_):
            attempts.append(clock.now())
            if fail_first and len(attempts) == 1:
                raise OSError("Port unavailable")
            port = Port(clock, [(clock.now() + at, data) for at, data in packets],
                        write_action if not ports else None)
            ports.append(port)
            return port
        options.setdefault("duration_s", .3)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config(**options), Path(root) / "run", control=clock,
                                   engine_factory=lambda c, t, e: RtuSlaveEngine(c, t, e, open_port))
            result = runner.run()
            self.assertTrue((runner.directory / "summary.json").exists())
        self.assertTrue(all(p.closed for p in ports))
        return result, ports, attempts, clock

    def test_data_clock_advances_without_requests(self):
        result, _, _, _ = self.run_session()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["counters"]["source_updates"], 3)

    def test_sine_values_and_byte_orders(self):
        read = frame(b"\x01\x04\x00\x00\x00\x02")
        orders = {"B1234": (0, 1, 2, 3), "B4321": (3, 2, 1, 0),
                  "B2143": (1, 0, 3, 2), "B3412": (2, 3, 0, 1)}
        for order, indexes in orders.items():
            result, ports, _, _ = self.run_session(packets=[(.01, read), (.21, read)], byte_order=order,
                                                 source=dict(kind="sine", value=10, amplitude=1, frequency_hz=1.25))
            self.assertEqual(result["exit_code"], 0)
            expected = [frame(b"\x01\x04\x04" + bytes(struct.pack(">f", value)[i] for i in indexes))
                        for value in (10, 11)]
            self.assertEqual([data for _, data in ports[0].writes], expected)

    def test_independent_point_snapshot_and_disabled_address_hole(self):
        packets = [(.01, frame(b"\x01\x03\x00\x00\x00\x04")),
                   (.11, frame(b"\x01\x04\x00\x04\x00\x02")),
                   (.21, frame(b"\x01\x04\x00\x06\x00\x02"))]
        result, ports, _, _ = self.run_session(packets=packets, points=[
            dict(address=6, source=dict(kind="sine", value=10, amplitude=2, frequency_hz=1.25)),
            dict(address=0, source=dict(value=12.5)),
            dict(enabled=False, address=4, source=dict(value=999)),
            dict(address=2, source=dict(value=-3.5))])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([data for _, data in ports[0].writes], [
            frame(b"\x01\x03\x08" + struct.pack(">ff", 12.5, -3.5)),
            frame(b"\x01\x84\x02"),
            frame(b"\x01\x04\x04" + struct.pack(">f", 12))])

    def test_short_write_and_disconnect_reconnect_at_bounded_rate(self):
        read = frame(b"\x01\x03\x00\x00\x00\x02")
        for action in ("short", "error"):
            result, ports, attempts, _ = self.run_session(packets=[(.01, read)], write_action=action, duration_s=1.3)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["counters"]["io_errors"], 1)
            self.assertEqual(len(ports), 2)
            self.assertGreaterEqual(attempts[1] - attempts[0], 1)
            self.assertEqual(len(ports[1].writes), 1)

    def test_strict_write_failure_and_open_failure_stop(self):
        read = frame(b"\x01\x03\x00\x00\x00\x02")
        for options in (dict(packets=[(.01, read)], write_action="short"), dict(fail_first=True)):
            result, _, attempts, _ = self.run_session(policy="strict", **options)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(len(attempts), 1)

    def test_initial_open_failure_recovers_and_preserves_clock(self):
        result, _, attempts, _ = self.run_session(fail_first=True, duration_s=1.3)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["counters"]["open_errors"], 1)
        self.assertGreaterEqual(attempts[1], 1)
        self.assertEqual(result["counters"]["source_updates"], 13)

    def test_stop_with_noise_or_partial_request_is_bounded(self):
        for packets in ([ (.01, b"\x01\x03") ],
                        [(.001 * i, b"\x55" * 32) for i in range(1, 100)]):
            result, ports, _, clock = self.run_session(packets=packets, stop_at=.035, duration_s=0)
            self.assertEqual(result["exit_code"], 130)
            self.assertLess(clock.now(), .05)
            self.assertFalse(ports[0].writes)

    def test_stop_during_reconnect(self):
        result, _, _, clock = self.run_session(fail_first=True, stop_at=.035, duration_s=0)
        self.assertEqual(result["exit_code"], 130)
        self.assertLessEqual(clock.now(), .1)
