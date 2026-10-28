"""通过用户指定的虚拟 COM 或隔离 PTY 验证当前配置，绝不安装驱动。

Uses an independent peer and the actual CLI subprocess. Both named ports must
be identified as virtual/bridged by the installed driver before opening them.
"""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

import serial
from serial.tools.list_ports import comports
from modbus_dev.domain.config import CONFIG_VERSION


def frame(data):
    crc = 0xffff
    for item in data:
        crc ^= item
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return data + struct.pack("<H", crc)


def scenario(root, master, slave, name, *, period=100, points=1, seconds=3,
             faults=False, strict=False, readback=0, waveform="constant", supplied_peer=None):
    folder = root / name
    folder.mkdir()
    config = dict(schema_version=CONFIG_VERSION, mode="rtu-master", policy="strict" if strict else "continuous",
                  serial=dict(port=master, baud=9600),
                  points=[dict(address=address, operation="write-multiple", readback_every=readback,
                               source=dict(kind=waveform, value=1.25)) for address in range(0, points*2, 2)],
                  duration_s=seconds, period_ms=period, timeout_ms=80)
    path = folder / "input.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    stop = threading.Event()
    errors, seen = [], []
    peer = supplied_peer or serial.Serial(slave, 9600, timeout=.01, write_timeout=.1)

    def serve():
        buffer = bytearray()
        registers = {}
        try:
            while not stop.is_set():
                buffer.extend(peer.read(max(1, min(256, peer.in_waiting))))
                while len(buffer) >= 8:
                    size = 9 + buffer[6] if buffer[1] == 16 else 8
                    if len(buffer) < size:
                        break
                    data = bytes(buffer[:size])
                    del buffer[:size]
                    if data != frame(data[:-2]):
                        raise AssertionError("Invalid request CRC")
                    unit, function, address, count = struct.unpack(">BBHH", data[:6])
                    seen.append(dict(at=time.perf_counter(), hex=data.hex()))
                    if function == 16:
                        words = struct.unpack(">" + "H" * count, data[7:-2])
                        registers.update({address + i: v for i, v in enumerate(words)})
                        reply = frame(data[:6])
                    elif function in (3, 4):
                        reply = frame(bytes((unit, function, count * 2)) +
                                      struct.pack(">" + "H" * count, *(registers[address+i] for i in range(count))))
                    else:
                        raise AssertionError("Unexpected function")
                    if faults and len(seen) == 1:
                        continue
                    if faults and len(seen) == 2:
                        reply = reply[:-1] + bytes((reply[-1] ^ 1,))
                    if faults and len(seen) == 3:
                        reply = frame(bytes((unit, function | 0x80, 2)))
                    time.sleep(.005 if readback else .035)
                    seen[-1]["reply_started_at"] = time.perf_counter()
                    peer.write(reply)
                    seen[-1]["reply_finished_at"] = time.perf_counter()
        except BaseException as exc:
            errors.append(repr(exc))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        command = [sys.executable, "-m", "modbus_dev", "run", str(path), "--output", str(folder / "run")]
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=seconds+10,
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    finally:
        stop.set()
        thread.join(2)
        peer.close()
    (folder / "stdout.json").write_text(process.stdout, encoding="utf-8")
    (folder / "stderr.log").write_text(process.stderr, encoding="utf-8")
    (folder / "received.json").write_text(json.dumps(seen, indent=2), encoding="utf-8")
    if thread.is_alive() or errors:
        raise AssertionError(errors or "Peer did not stop")
    result = json.loads(process.stdout)
    expected_exit = 1 if strict and faults else 0
    assert process.returncode == expected_exit, result
    counters = result["counters"]
    expected = 1 if strict and faults else round(seconds * 1000 / period)
    assert counters["write_attempts"] == expected * (2 if readback else 1), result
    if not faults:
        assert counters["ack"] == expected, result
        assert not result["ack_correlation_uncertain"], result
        if readback:
            assert counters["read"] == expected, result
        else:
            assert abs(result["metrics"]["send_interval_ms"]["mean"] - period) < 3, result
    elif not strict:
        assert counters["timeout"] == 1 and counters["bad_reply"] == 1, result
        assert counters["exception_ambiguous"] == 1, result
        assert counters["ack_ambiguous"] == expected - 3, result
    summary = json.loads((folder / "run/summary.json").read_text(encoding="utf-8"))
    assert summary["evidence_complete"], summary
    print(f"PASS {name}: {counters}", flush=True)
    return dict(name=name, result=result)


class PtyPeer:
    """Real POSIX master endpoint; production CLI opens the slave via pySerial."""
    def __init__(self):
        import pty
        import tty
        self.fd, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)

    @property
    def in_waiting(self):
        import array
        import fcntl
        import termios
        count = array.array("i", [0])
        fcntl.ioctl(self.fd, termios.FIONREAD, count, True)
        return count[0]

    def read(self, size):
        import select
        if select.select([self.fd], [], [], .01)[0]:
            return os.read(self.fd, size)
        return b""

    def write(self, data):
        return os.write(self.fd, data)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            os.close(self.slave)
            self.fd = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master")
    parser.add_argument("--slave")
    parser.add_argument("--pty", action="store_true", help="create isolated Linux/Unix PTYs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.pty:
        if os.name != "posix" or args.master or args.slave:
            parser.error("--pty requires Unix and must not specify existing ports")
    else:
        if not args.master or not args.slave:
            parser.error("Provide --master and --slave, or --pty")
        descriptions = {p.device.upper(): p.description.lower() for p in comports()}
        for name in (args.master, args.slave):
            if not any(word in descriptions.get(name.upper(), "") for word in ("virtual", "bridged", "com0com")):
                parser.error(f"Port {name} is not identified as a virtual/bridged test port")
        if args.master.upper() == args.slave.upper():
            parser.error("A pair of distinct ports is required")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(platform=sys.platform, python=sys.version, cases=[])
    try:
        for name, settings in (
            ("100ms", dict(seconds=10)), ("100ms-four-points", dict(points=4)),
            ("200ms-sine", dict(period=200, waveform="sine")), ("readback", dict(readback=1)),
            ("faults", dict(faults=True)), ("strict-failure", dict(faults=True, strict=True))):
            peer = PtyPeer() if args.pty else None
            try:
                report["cases"].append(scenario(args.output, peer.port if peer else args.master,
                                               args.slave, name, supplied_peer=peer, **settings))
            finally:
                if peer:
                    peer.close()
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=repr(exc))
        raise
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
