"""在隔离 PTY 上运行两个真实工作进程，验证四种操作与双方数值快照。"""
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest


@unittest.skipUnless(os.name == "posix", "PTY 联调只用于 POSIX；Windows 需另验真实 COM")
class DuplexTests(unittest.TestCase):
    def exercise(self, operation, reverse):
        import tty
        fds = [os.openpty(), os.openpty()]
        for _, slave_fd in fds:
            tty.setraw(slave_fd)
        ports = [os.ttyname(slave_fd) for _, slave_fd in fds]
        stop = threading.Event()

        def bridge():
            peers = {fds[0][0]: fds[1][0], fds[1][0]: fds[0][0]}
            while not stop.is_set():
                try:
                    ready, _, _ = select.select(list(peers), [], [], .02)
                    for source in ready:
                        data = os.read(source, 4096)
                        while data:
                            data = data[os.write(peers[source], data):]
                except OSError:
                    if not stop.is_set():
                        time.sleep(.005)

        thread = threading.Thread(target=bridge, daemon=True)
        thread.start()
        process = None
        records = queue.Queue()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                common = dict(schema_version=2, serial=dict(baud=115200), access_mode="read-write",
                              unit=1, period_ms=100, timeout_ms=80, points=[dict(address=0,
                              name="压力", source=dict(value=12.5))])
                slave = dict(common, mode="rtu-slave", duration_s=0,
                             serial=dict(port=ports[int(reverse)], baud=115200))
                master = dict(common, mode="rtu-master", duration_s=.4, master_operation=operation,
                              serial=dict(port=ports[1 - int(reverse)], baud=115200),
                              readback_every=int(operation.startswith("write")),
                              points=[dict(address=0, source=dict(value=1.25))])
                for name, config in (("slave", slave), ("master", master)):
                    (root / f"{name}.json").write_text(json.dumps(config))
                process = subprocess.Popen([sys.executable, "-m", "modbus_dev", "worker", str(root / "slave.json"),
                                            "--output", str(root / "slave")], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                def read():
                    for line in process.stdout:
                        records.put(json.loads(line))
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    packet = records.get(timeout=5)
                    if packet.get("state") == "running":
                        break
                    self.assertNotEqual(packet["type"], "result", packet)
                else:
                    self.fail("从站未启动")
                result = subprocess.run([sys.executable, "-m", "modbus_dev", "run", str(root / "master.json"),
                                         "--output", str(root / "master")], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = json.loads(result.stdout)
                counters = summary["counters"]
                self.assertGreaterEqual(counters.get("read", 0), 2)
                self.assertFalse(any(counters.get(key) for key in
                                     ("timeout", "exception", "readback_mismatch", "bad_reply")), counters)
                if operation.startswith("write"):
                    self.assertGreaterEqual(counters.get("ack", 0), 2)
                process.stdin.write('{"schema_version":1,"command":"stop"}\n')
                process.stdin.flush()
                process.wait(timeout=5)
                reader.join(2)
                packets = []
                while not records.empty():
                    packets.append(records.get_nowait())
                final = next(packet for packet in packets if packet["type"] == "result")
                self.assertEqual(final["result"]["exit_code"], 130)
                self.assertTrue(final["live"]["rows"])
                values = [row["value"] for row in final["live"]["rows"] if row["value"] is not None]
                if operation in ("read-input", "read-holding"):
                    self.assertIn(12.5, values)
                elif operation == "write-multiple":
                    self.assertIn(1.25, values)
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            stop.set()
            thread.join(1)
            for pair in fds:
                for fd in pair:
                    os.close(fd)

    def test_four_operations_with_roles_swapped(self):
        for index, operation in enumerate(("read-holding", "read-input", "write-single", "write-multiple")):
            with self.subTest(operation=operation):
                self.exercise(operation, bool(index % 2))
