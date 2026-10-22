"""Independent TCP wire client: expected frames do not use PyModbus codecs."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.engines.tcp_slave import TcpSlaveEngine


def frame(pdu, tid=7, unit=1):
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def exact(sock, size):
    result = b""
    while len(result) < size:
        data = sock.recv(size - len(result))
        if not data:
            raise EOFError()
        result += data
    return result


def receive(sock):
    header = exact(sock, 7)
    return header + exact(sock, int.from_bytes(header[4:6], "big") - 1)


class TcpSlaveTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.result = None
        config = TaskConfig.from_mapping(dict(mode="tcp-slave", duration_s=0,
                                             addresses=[0, 2, 6], source=dict(value=12.5)))
        def factory(c, control, emit):
            self.engine = TcpSlaveEngine(replace(c, port=0), control, emit)
            original = self.engine.publish
            def publish(registers):
                original(registers)
                self.ready.set()
            self.engine.publish = publish
            return self.engine
        self.runner = SessionRunner(config, Path(self.root.name) / "run", stop=self.stop,
                                    engine_factory=factory)
        def run():
            self.result = self.runner.run()
        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.assertTrue(self.ready.wait(3))
        self.client = socket.create_connection(self.engine.bound_address, timeout=1)

    def tearDown(self):
        self.client.close()
        self.stop.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result["exit_code"], 130)
        summary = json.loads((self.runner.directory / "summary.json").read_text())
        self.assertEqual(summary["run"]["exit_code"], 130)
        self.root.cleanup()

    def request(self, pdu):
        self.client.sendall(frame(pdu))
        return receive(self.client)

    def test_both_read_functions_and_zero_based_addresses(self):
        for fc in (3, 4):
            self.assertEqual(self.request(struct.pack(">BHH", fc, 0, 4)),
                             frame(bytes((fc, 8)) + struct.pack(">ff", 12.5, 12.5)))

    def test_all_non_read_function_codes_are_rejected(self):
        for fc in range(1, 128):
            if fc in (3, 4):
                continue
            self.assertEqual(self.request(bytes((fc, 0, 0, 0, 1))), frame(bytes((fc | 128, 1))))
        self.assertEqual(self.request(b"\x03\x00\x00\x00\x02"), frame(b"\x03\x04" + struct.pack(">f", 12.5)))

    def test_invalid_ranges_and_partial_points(self):
        for address, count, error in ((0, 0, 3), (0, 126, 3), (1, 2, 2), (0, 1, 2),
                                      (0, 8, 2), (65535, 2, 2)):
            self.assertEqual(self.request(struct.pack(">BHH", 3, address, count)), frame(bytes((131, error))))
        self.assertEqual(self.request(b"\x03"), frame(b"\x83\x03"))

    def test_fragmented_and_coalesced_frames_keep_transaction_ids(self):
        data = frame(b"\x03\x00\x00\x00\x02", tid=1)
        self.client.sendall(data[:4])
        self.client.sendall(data[4:] + frame(b"\x04\x00\x06\x00\x02", tid=2))
        self.assertEqual(receive(self.client), frame(b"\x03\x04" + struct.pack(">f", 12.5), tid=1))
        self.assertEqual(receive(self.client), frame(b"\x04\x04" + struct.pack(">f", 12.5), tid=2))

    def test_wrong_unit_and_broadcast_do_not_respond_or_mutate(self):
        self.client.sendall(frame(b"\x06\x00\x00\x12\x34", unit=0)
                            + frame(b"\x03\x00\x00\x00\x02", unit=2)
                            + frame(b"\x03\x00\x00\x00\x02"))
        self.assertEqual(receive(self.client), frame(b"\x03\x04" + struct.pack(">f", 12.5)))

    def test_malformed_mbap_closes_only_that_client(self):
        self.client.sendall(b"\x00\x01\x00\x01\x00\x06\x01\x03\x00\x00\x00\x02")
        self.assertEqual(self.client.recv(1), b"")
        with socket.create_connection(self.engine.bound_address, timeout=1) as other:
            other.sendall(frame(b"\x03\x00\x00\x00\x02"))
            self.assertEqual(receive(other), frame(b"\x03\x04" + struct.pack(">f", 12.5)))

    def test_source_clock_advances_without_reads(self):
        time.sleep(.23)
        self.assertGreaterEqual(self.runner.counters["source_updates"], 3)
        before = self.runner.counters["source_updates"]
        for _ in range(20):
            self.request(b"\x03\x00\x00\x00\x02")
        self.assertLess(self.runner.counters["source_updates"] - before, 20)

    def test_stop_closes_active_clients_and_listener(self):
        self.stop.set()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        try:
            self.assertEqual(self.client.recv(1), b"")
        except ConnectionResetError:
            pass  # Windows may reset a connection still pending in the accept queue.
        with self.assertRaises(OSError):
            socket.create_connection(self.engine.bound_address, timeout=.2)


class TcpLifecycleTests(unittest.TestCase):
    def test_bind_failure_is_recorded_and_releases_session_lock(self):
        with socket.socket() as occupied, tempfile.TemporaryDirectory() as root:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            config = TaskConfig.from_mapping(dict(mode="tcp-slave", port=occupied.getsockname()[1]))
            runner = SessionRunner(config, Path(root) / "run")
            result = runner.run()
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["state"], "failed")
            self.assertTrue((runner.directory / "summary.json").exists())
            from modbus_dev.application.session import _ACTIVE_SESSION
            self.assertTrue(_ACTIVE_SESSION.acquire(blocking=False))
            _ACTIVE_SESSION.release()

    def test_duration_completes_without_clients(self):
        with tempfile.TemporaryDirectory() as root:
            config = TaskConfig.from_mapping(dict(mode="tcp-slave", duration_s=.12))
            runner = SessionRunner(config, Path(root) / "run",
                                   engine_factory=lambda c, t, e: TcpSlaveEngine(replace(c, port=0), t, e))
            result = runner.run()
            self.assertEqual(result["exit_code"], 0)
            self.assertGreaterEqual(result["counters"]["source_updates"], 1)


if __name__ == "__main__":
    unittest.main()
