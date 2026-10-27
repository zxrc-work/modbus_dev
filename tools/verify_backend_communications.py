"""用独立字节对端验证 CLI/worker 通信；Windows 使用虚拟串口对，POSIX 使用隔离 PTY。"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

import serial
from serial.tools.list_ports import comports
from verify_virtual_serial import PtyPeer, frame as rtu_frame
from modbus_dev.reporting.segments import event_paths


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Backend:
    def __init__(self, folder, config, entry="worker", cli=None):
        self.folder, self.entry = folder, entry
        folder.mkdir(parents=True)
        save(folder / "input.json", config)
        self.stdout = (folder / "stdout.jsonl").open("wb")
        self.stderr = (folder / "stderr.log").open("wb")
        command = ([sys.executable, str(Path(__file__).with_name("verify_gui_session.py")),
                    str(folder / "input.json"), "--output", str(folder / "run")]
                   if entry == "gui" else
                   ([str(cli)] if cli else [sys.executable, "-m", "modbus_dev"]) +
                   [entry, str(folder / "input.json"),
                    "--output", str(folder / "run")])
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        if cli:
            for name in list(environment):
                if name.startswith(("PYTHON", "QT_", "QML")) or name in {
                        "VIRTUAL_ENV", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"}:
                    environment.pop(name, None)
            system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            environment["PATH"] = (os.pathsep.join((str(system / "System32"), str(system)))
                                   if os.name == "nt" else "/usr/bin:/bin:/usr/sbin:/sbin")
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE,
            stdout=self.stdout, stderr=self.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=environment)
        self.result = None
        self.stop_latency_ms = None

    def events(self):
        result = []
        for path in event_paths(self.folder / "run"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue  # 轮转可能在枚举后淘汰旧分片，不影响剩余证据读取。
            for line in lines:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # 当前分片末行可能尚未写完。
        return result

    def started(self):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if any(e["code"] == "session_started" for e in self.events()):
                return
            if self.process.poll() is not None:
                raise RuntimeError("Backend exited before session_started; inspect stderr/events")
            time.sleep(.02)
        raise TimeoutError("Backend startup not recorded within 10 seconds")

    def finish(self, stop=False, timeout=25):
        if stop and self.process.poll() is None:
            require(self.entry == "worker", "Only worker supports the JSON stop command")
            began = time.perf_counter()
            self.process.stdin.write(b'{"schema_version":1,"command":"stop"}\n')
            self.process.stdin.flush()
        else:
            began = None
        try:
            self.process.wait(timeout)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(5)
            self.process.stdin.close()
            self.stdout.close()
            self.stderr.close()
        if began is not None:
            self.stop_latency_ms = (time.perf_counter() - began) * 1000
        path = self.folder / "run/summary.json"
        require(path.exists(), "No final summary")
        summary = json.loads(path.read_text(encoding="utf-8"))
        self.result = summary["run"]
        require(summary["evidence_complete"], "Raw evidence was lost")
        expected = 130 if stop else 0
        require(self.process.returncode == expected == self.result["exit_code"],
                f"Unexpected exit: process={self.process.returncode}, result={self.result}")
        if self.entry == "worker":
            records = [json.loads(line) for line in (self.folder / "stdout.jsonl").read_text().splitlines()]
            require(records[-1]["type"] == "result", "Worker did not deliver a final result")
            require(records[-1]["result"] == self.result, "Worker result differs from saved summary")
        return self.result

    def cleanup(self):
        if self.process.poll() is None:
            if self.entry == "worker":
                try:
                    self.finish(stop=True, timeout=5)
                    return
                except Exception:
                    pass
            self.process.kill()
            self.process.wait(5)
        for stream in (self.process.stdin, self.stdout, self.stderr):
            if not stream.closed:
                stream.close()


class SerialWire:
    def __init__(self, peer, trace):
        self.peer, self.trace = peer, trace

    def exchange(self, pdu, *, unit=1, silence=False, packet=None, split=False):
        packet = packet if packet is not None else rtu_frame(bytes((unit,)) + pdu)
        began = time.perf_counter()
        if split:
            self.peer.write(packet[:3])
            split_at = time.perf_counter()
            self.peer.write(packet[3:])
        else:
            self.peer.write(packet)
            split_at = None
        data = bytearray()
        deadline = time.monotonic() + (.08 if silence else .5)
        while time.monotonic() < deadline:
            part = self.peer.read(max(1, min(256, self.peer.in_waiting)))
            data.extend(part)
            if len(data) >= 3 and not silence:
                # 写入 ACK 的长度固定为八字节，不能将地址高字节误当成数据字节数。
                size = 5 if data[1] & 0x80 else 8 if data[1] in (6, 16) else 5 + data[2]
                if len(data) >= size:
                    break
        at = time.perf_counter()
        self.trace.append(dict(sent_s=began, received_s=at, request=packet.hex(),
                               response=data.hex(), split_at_s=split_at))
        if silence:
            require(not data, "Unexpected response to ignored/corrupt serial request")
            return b""
        require(len(data) >= 5 and bytes(data) == rtu_frame(bytes(data[:-2])),
                f"Missing/corrupt serial reply: {data.hex()}")
        require(data[0] == unit, "Wrong reply unit")
        return bytes(data[1:-2])


def exact(sock, length):
    data = bytearray()
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise EOFError("TCP peer closed")
        data.extend(part)
    return bytes(data)


class TcpWire:
    def __init__(self, host, port, trace):
        self.host, self.port, self.trace = host, port, trace
        self.socket = socket.create_connection((host, port), timeout=2)
        self.socket.settimeout(.5)
        self.tid = 0

    @staticmethod
    def packet(pdu, unit, tid):
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu

    def receive(self):
        header = exact(self.socket, 7)
        return header + exact(self.socket, int.from_bytes(header[4:6], "big") - 1)

    def exchange(self, pdu, *, unit=1, silence=False, split=False):
        self.tid += 1
        packet = self.packet(pdu, unit, self.tid)
        began = time.perf_counter()
        if split:
            self.socket.sendall(packet[:4])
            self.socket.sendall(packet[4:])
        else:
            self.socket.sendall(packet)
        self.socket.settimeout(.08 if silence else .5)
        try:
            reply = self.receive()
        except socket.timeout:
            if not silence:
                raise
            reply = b""
        finally:
            self.socket.settimeout(.5)
        self.trace.append(dict(sent_s=began, received_s=time.perf_counter(),
                               request=packet.hex(), response=reply.hex()))
        if silence:
            require(not reply, "Unexpected TCP response to ignored unit")
            return b""
        require(reply[:2] == packet[:2] and reply[2:4] == b"\0\0" and reply[6] == unit,
                "TCP response MBAP does not match")
        return reply[7:]

    def close(self):
        self.socket.close()


def read_pdu(function=3, address=0, count=8):
    return struct.pack(">BHH", function, address, count)


def check_protocol(wire, trace):
    expected = struct.pack(">ffff", 12.5, 12.5, 12.5, 12.5)
    checks = 0
    for fc in (3, 4):
        require(wire.exchange(read_pdu(fc)) == bytes((fc, 16)) + expected, "Incorrect full snapshot")
        checks += 1
    require(wire.exchange(read_pdu(), split=True) == b"\x03\x10" + expected, "Split request failed")
    checks += 1
    for fc in range(1, 128):
        if fc in (3, 4):
            continue
        # Valid FC06/16 writes verify rejection and absence of mutation.
        pdu = bytes((fc, 0, 0, 0, 1))
        if fc == 16:
            pdu = b"\x10\x00\x00\x00\x02\x04\x3f\x80\x00\x00"
        require(wire.exchange(pdu) == bytes((fc | 128, 1)), f"FC{fc} was not rejected as read-only")
        checks += 1
    for address, count, code in ((0, 0, 3), (0, 126, 3), (0, 1, 2), (1, 2, 2), (8, 2, 2), (65535, 2, 2)):
        require(wire.exchange(read_pdu(3, address, count)) == bytes((131, code)), "Range/quantity policy failed")
        checks += 1
    require(wire.exchange(b"\x03") == b"\x83\x03", "Malformed PDU not rejected")
    for unit in (0, 2):
        wire.exchange(read_pdu(), unit=unit, silence=True)
        checks += 1
    require(wire.exchange(read_pdu()) == b"\x03\x10" + expected, "Data changed after rejected writes")
    checks += 2
    if isinstance(wire, SerialWire):
        good = rtu_frame(b"\x01" + read_pdu())
        for packet in (good[:-1] + bytes((good[-1] ^ 1,)), b"\xff" + good,
                       good[:3], b"\x55" * 700 + good):
            wire.exchange(b"", packet=packet, silence=True)
            require(wire.exchange(read_pdu()) == b"\x03\x10" + expected, "Serial recovery after invalid burst failed")
            checks += 2
    else:
        # Fragmented/coalesced real socket writes retain transaction identifiers.
        packets = wire.packet(read_pdu(), 1, 1001) + wire.packet(read_pdu(4), 1, 1002)
        wire.socket.sendall(packets)
        for tid, fc in ((1001, 3), (1002, 4)):
            reply = wire.receive()
            trace.append(dict(request="coalesced", response=reply.hex()))
            require(reply == wire.packet(bytes((fc, 16)) + expected, 1, tid), "Coalesced response mismatch")
            checks += 1
        for malformed in (b"\x00\x01\x00\x01\x00\x06\x01", b"\x00\x01\x00\x00\x00\x01\x01"):
            with socket.create_connection((wire.host, wire.port), timeout=1) as bad:
                bad.settimeout(1)
                bad.sendall(malformed)
                try:
                    require(bad.recv(1) == b"", "Invalid MBAP connection not closed")
                except ConnectionResetError:
                    pass
            require(wire.exchange(read_pdu()) == b"\x03\x10" + expected, "Bad client disrupted healthy client")
            checks += 2
        with socket.create_connection((wire.host, wire.port), timeout=1) as partial:
            partial.settimeout(2)
            partial.sendall(b"\x00\x01")
            require(wire.exchange(read_pdu()) == b"\x03\x10" + expected, "Partial client blocked healthy client")
            require(partial.recv(1) == b"", "Partial request deadline not enforced")
            checks += 2
    return checks


def reserve_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Matrix:
    def __init__(self, args):
        self.args = args
        self.root = args.output.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.report = dict(platform=sys.platform, python=sys.version, tested_commit=args.commit,
                           dependencies={x: importlib.metadata.version(x) for x in ("pymodbus", "pyserial")},
                           backend_executable=str(args.cli) if args.cli else None,
                           cases=[], status="running")

    def serial(self):
        if self.args.pty:
            peer = PtyPeer()
            return peer.port, peer
        return self.args.master, serial.Serial(self.args.slave, 9600, timeout=.005, write_timeout=.1)

    def case(self, name, operation):
        folder = self.root / name
        folder.mkdir()
        trace = []
        began = time.perf_counter()
        try:
            detail = operation(folder, trace)
            record = dict(name=name, status="passed", detail=detail)
        except Exception as exc:
            record = dict(name=name, status="failed", error=repr(exc), traceback=traceback.format_exc())
        finally:
            save(folder / "peer-trace.json", trace)
        record["elapsed_s"] = time.perf_counter() - began
        self.report["cases"].append(record)
        save(self.root / "report.json", self.report)
        print(record["status"].upper(), name, record.get("error", ""), flush=True)

    def slave(self, folder, trace, mode, entry):
        peer = wire = backend = None
        config = dict(mode=mode, addresses=[0, 2, 4, 6], period_ms=100, timeout_ms=80,
                      source=dict(kind="constant", value=12.5), duration_s=15 if entry == "run" else 0)
        if mode == "rtu-slave":
            port, peer = self.serial()
            config["serial"] = dict(port=port, baud=9600)
        else:
            config.update(host="127.0.0.1", port=reserve_port())
        try:
            backend = Backend(folder / "backend", config, entry, cli=self.args.cli)
            backend.started()
            wire = SerialWire(peer, trace) if peer else TcpWire(config["host"], config["port"], trace)
            checks = check_protocol(wire, trace)
            result = backend.finish(stop=entry == "worker", timeout=20)
            require(result["counters"].get("slave_request", 0) >= 130, "Slave request accounting is incomplete")
            return dict(checks=checks, result=result, stop_latency_ms=backend.stop_latency_ms)
        finally:
            if backend:
                backend.cleanup()
            if isinstance(wire, TcpWire):
                wire.close()
            if peer:
                peer.close()

    def source_clock(self, folder, trace, mode):
        peer = wire = backend = None
        config = dict(mode=mode, addresses=[0, 2, 4, 6], duration_s=0, period_ms=100,
                      source=dict(kind="sine", value=10, amplitude=1, frequency_hz=.5))
        if mode == "rtu-slave":
            port, peer = self.serial()
            config["serial"] = dict(port=port, baud=9600)
        else:
            config.update(host="127.0.0.1", port=reserve_port())
        try:
            backend = Backend(folder / "backend", config, cli=self.args.cli)
            backend.started()
            wire = SerialWire(peer, trace) if peer else TcpWire(config["host"], config["port"], trace)
            samples = []
            for _ in range(5):
                # Long periods with no poll must still advance the source clock.
                time.sleep(.23)
                data = wire.exchange(read_pdu())
                require(data[:2] == b"\x03\x10", "Missing sine snapshot")
                values = struct.unpack(">ffff", data[2:])
                require(len(set(values)) == 1, "Snapshot contains different source generations")
                require(all(9 <= v <= 11 for v in values), "Sine outside configured bounds")
                samples.append(values[0])
            require(len(set(samples)) >= 3, "Source was advanced by polls instead of its own clock")
            result = backend.finish(stop=True)
            published = {struct.pack(">f", e["details"]["values"][0])
                         for e in backend.events() if e["code"] == "source_sample"}
            require(all(struct.pack(">f", value) in published for value in samples), "Read value was never published")
            require(result["counters"]["source_updates"] > len(samples), "No independent source updates")
            return dict(samples=samples, result=result, stop_latency_ms=backend.stop_latency_ms)
        finally:
            if backend:
                backend.cleanup()
            if isinstance(wire, TcpWire):
                wire.close()
            if peer:
                peer.close()

    def master_peer(self, folder, trace, entry):
        port, peer = self.serial()
        stopped = threading.Event()
        errors, registers = [], {}
        backend = None
        def serve():
            buffer = bytearray()
            try:
                while not stopped.is_set():
                    buffer.extend(peer.read(max(1, min(256, peer.in_waiting))))
                    while len(buffer) >= 8:
                        length = 9 + buffer[6] if buffer[1] == 16 else 8
                        if len(buffer) < length:
                            break
                        packet = bytes(buffer[:length])
                        del buffer[:length]
                        require(packet == rtu_frame(packet[:-2]), "Invalid master request CRC")
                        unit, fc, address, count = struct.unpack(">BBHH", packet[:6])
                        if fc == 16:
                            words = struct.unpack(">" + "H" * count, packet[7:-2])
                            registers.update({address+i: word for i, word in enumerate(words)})
                            reply = rtu_frame(packet[:6])
                        else:
                            require(fc in (3, 4), "Unexpected master function")
                            reply = rtu_frame(bytes((unit, fc, count*2)) +
                                              struct.pack(">" + "H" * count, *(registers[address+i] for i in range(count))))
                        at = time.perf_counter()
                        time.sleep(.005)
                        peer.write(reply)
                        trace.append(dict(request_received_s=at, reply_sent_s=time.perf_counter(),
                                          request=packet.hex(), reply=reply.hex()))
            except Exception as exc:
                errors.append(repr(exc))
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:
            config = dict(mode="rtu-master", serial=dict(port=port, baud=9600),
                          addresses=[0, 2, 4, 6], source=dict(kind="sine", value=10, amplitude=1, frequency_hz=.5),
                          duration_s=self.args.seconds, period_ms=100, timeout_ms=80, readback_every=1)
            backend = Backend(folder / "master", config, entry, cli=self.args.cli)
            result = backend.finish(timeout=self.args.seconds+15)
            require(not errors, str(errors))
            counters = result["counters"]
            require(counters.get("ack", 0) + counters.get("ack_ambiguous", 0) > 0, "No ACK received")
            require(counters.get("read", 0) + counters.get("read_ambiguous", 0) > 0, "No readback received")
            require(not counters.get("readback_mismatch", 0), "Readback differs from values actually written")
            return dict(expected_cycles=round(self.args.seconds*10), result=result)
        finally:
            if backend:
                backend.cleanup()
            stopped.set()
            server.join(2)
            peer.close()
            require(not server.is_alive(), "Independent peer did not stop")

    def contention(self, folder, trace):
        port, peer = self.serial()
        options = dict(exclusive=True) if os.name == "posix" else {}
        holder = serial.Serial(port, 9600, timeout=.01, **options)
        backend = None
        try:
            backend = Backend(folder / "slave", dict(mode="rtu-slave", duration_s=0,
                serial=dict(port=port, baud=9600), addresses=[0, 2, 4, 6],
                source=dict(kind="constant", value=12.5)), cli=self.args.cli)
            backend.started()
            time.sleep(1.2)
            holder.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(e["code"] == "state_changed" and e["details"].get("state") == "running"
                       for e in backend.events()):
                    break
                time.sleep(.02)
            else:
                raise TimeoutError("No recovery after occupied port was released")
            wire = SerialWire(peer, trace)
            require(wire.exchange(read_pdu()) == b"\x03\x10" + struct.pack(">ffff", *([12.5]*4)),
                    "Recovered slave did not serve data")
            result = backend.finish(stop=True)
            require(result["counters"].get("open_errors", 0) >= 1, "No actual port contention observed")
            require(result["counters"]["source_updates"] >= 10, "Source clock stopped while reconnecting")
            errors = [e for e in backend.events() if e["code"] == "port_open_failed"]
            intervals = [(b["monotonic_ns"] - a["monotonic_ns"])/1e9 for a,b in zip(errors, errors[1:])]
            require(all(value >= .98 for value in intervals), "Reconnect attempts were not rate limited")
            return dict(result=result, retry_intervals_s=intervals, stop_latency_ms=backend.stop_latency_ms)
        finally:
            if backend:
                backend.cleanup()
            holder.close()
            peer.close()

    def run(self):
        if self.args.contention_only:
            self.case("rtu-slave-port-contention", self.contention)
            self.report["status"] = self.report["cases"][0]["status"]
            save(self.root / "report.json", self.report)
            return int(self.report["status"] != "passed")
        if self.args.gui_only:
            self.case("rtu-master-readback-gui", lambda f, t: self.master_peer(f, t, "gui"))
            self.report["status"] = self.report["cases"][0]["status"]
            save(self.root / "report.json", self.report)
            return int(self.report["status"] != "passed")
        for entry in ("run", "worker"):
            for mode in ("rtu-slave", "tcp-slave"):
                self.case(mode + "-" + entry, lambda f, t, m=mode, e=entry: self.slave(f, t, m, e))
        for mode in ("rtu-slave", "tcp-slave"):
            self.case(mode + "-source-clock", lambda f, t, m=mode: self.source_clock(f, t, m))
        for entry in ("run", "worker"):
            self.case("rtu-master-readback-" + entry, lambda f, t, e=entry: self.master_peer(f, t, e))
        self.report["status"] = "failed" if any(c["status"] == "failed" for c in self.report["cases"]) else "passed"
        save(self.root / "report.json", self.report)
        return 1 if self.report["status"] == "failed" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master")
    parser.add_argument("--slave")
    parser.add_argument("--pty", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--cli", type=Path, help="test this extracted release CLI instead of the source module")
    parser.add_argument("--gui-only", action="store_true", help="optional offscreen Qt + actual serial peer")
    parser.add_argument("--contention-only", action="store_true", help="actual exclusive-open failure and recovery")
    args = parser.parse_args()
    if args.gui_only and args.contention_only:
        parser.error("Choose only one supplemental matrix")
    if args.cli:
        args.cli = args.cli.resolve()
        if not args.cli.is_file():
            parser.error("--cli must name an existing release executable")
        if args.gui_only:
            parser.error("--gui-only uses the source GUI and cannot be combined with --cli")
    if args.pty:
        if os.name != "posix" or args.master or args.slave:
            parser.error("--pty requires POSIX and no existing serial ports")
    else:
        descriptions = {p.device.upper(): p.description.lower() for p in comports()}
        for port in (args.master, args.slave):
            if not port or not any(word in descriptions.get(port.upper(), "") for word in ("virtual", "bridged", "com0com")):
                parser.error("Both ports must be identified by their drivers as virtual/bridged")
        if args.master.upper() == args.slave.upper():
            parser.error("Two different paired ports are required")
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be 1..3600")
    return Matrix(args).run()


if __name__ == "__main__":
    sys.exit(main())
