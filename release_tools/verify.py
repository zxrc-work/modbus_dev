"""用解压后的真实程序独立验收，不从源码导入通信引擎或业务编码器。"""
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def clean_environment():
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith(("PYTHON", "QT_", "QML")) or name in {"VIRTUAL_ENV", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"}:
            environment.pop(name, None)
    if os.name == "nt":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    else:
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return environment


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def crc_frame(data):
    """独立的标准 CRC16 实现，避免复用被测应用的编码器。"""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return data + crc.to_bytes(2, "little")


class ProcessCase:
    def __init__(self, cli, root, config, environment, entry="worker"):
        self.root, self.entry = root, entry
        root.mkdir()
        save(root / "input.json", config)
        self.out = (root / "stdout.jsonl").open("wb")
        self.err = (root / "stderr.log").open("wb")
        self.process = subprocess.Popen([str(cli), entry, str(root / "input.json"), "--output", str(root / "run")],
            cwd=root, env=environment, stdin=subprocess.PIPE if entry == "worker" else subprocess.DEVNULL,
            stdout=self.out, stderr=self.err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def __enter__(self):
        return self

    def started(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            for path in (self.root / "run").glob("events*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(line)["code"] == "session_started":
                            return
                    except (ValueError, KeyError):
                        pass
            if self.process.poll() is not None:
                raise RuntimeError(f"被测程序提前退出：{self.root}")
            time.sleep(.02)
        raise TimeoutError(f"被测服务未就绪：{self.root}")

    def finish(self, expected=130, stop=True, close_input=False):
        if stop and self.process.poll() is None:
            if close_input:
                self.process.stdin.close()
            else:
                self.process.stdin.write(b'{"schema_version":1,"command":"stop"}\n')
                self.process.stdin.flush()
        self.process.wait(12)
        self.out.flush()
        self.err.flush()
        require(self.process.returncode == expected, f"退出码不符：{self.root}: {self.process.returncode}")
        result = None
        if self.entry == "worker":
            packets = [json.loads(line) for line in (self.root / "stdout.jsonl").read_text(encoding="utf-8").splitlines()]
            require(bool(packets) and all(item["schema_version"] == 1 for item in packets), "worker 协议不完整")
            require(packets[-1]["type"] == "result", "worker 缺少终态结果")
            result = packets[-1]["result"]
            require(result["exit_code"] == expected, "worker 结果与退出码不一致")
        summary_path = self.root / "run/summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            require(summary["evidence_complete"], "发布会话证据不完整")
            require(summary["run"]["exit_code"] == expected, "摘要退出码错误")
            if result is not None:
                require(summary["run"] == result, "管道与落盘摘要不一致")
            result = summary["run"]
        else:
            require(expected == 2, "发布会话缺少摘要")
        return result

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(5)
        for stream in (self.process.stdin, self.out, self.err):
            if stream is not None and not stream.closed:
                stream.close()


def tcp_exchange(port, pdu, expected):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
        peer.sendall(struct.pack(">HHHB", 21, 0, len(pdu) + 1, 1) + pdu)
        response = bytearray()
        length = 7
        while len(response) < length:
            part = peer.recv(length - len(response))
            require(bool(part), "TCP 回复提前结束")
            response.extend(part)
            if len(response) == 7:
                length = 6 + int.from_bytes(response[4:6], "big")
        require(bytes(response[:4]) == struct.pack(">HH", 21, 0), "TCP 事务号或协议号错误")
        require(response[6] == 1 and bytes(response[7:]) == expected, f"TCP 回复错误：{response.hex()}")


def verify_tcp(cli, root, environment):
    cases = []
    port = available_port()
    config = dict(mode="tcp-slave", port=port, duration_s=0, addresses=[0, 2, 4, 6], source=dict(value=12.5))
    with ProcessCase(cli, root / "tcp-worker", config, environment) as run:
        run.started()
        for function in (3, 4):
            tcp_exchange(port, struct.pack(">BHH", function, 0, 8), bytes((function, 16)) + struct.pack(">ffff", *([12.5] * 4)))
        tcp_exchange(port, bytes.fromhex("0600000000"), bytes.fromhex("8601"))
        tcp_exchange(port, bytes.fromhex("0300010002"), bytes.fromhex("8302"))
        run.finish()
    cases.append("worker-tcp-fc03-fc04-reject-write-invalid-address-stop")
    with ProcessCase(cli, root / "tcp-cli-restart", dict(config, duration_s=1), environment, "run") as run:
        run.started()
        tcp_exchange(port, bytes.fromhex("0300000002"), b"\x03\x04" + struct.pack(">f", 12.5))
        run.finish(expected=0, stop=False)
    cases.append("cli-same-port-restart-and-natural-end")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        occupied = reserved.getsockname()[1]
        with ProcessCase(cli, root / "occupied-port", dict(config, port=occupied), environment) as run:
            run.finish(expected=1, stop=False)
    with ProcessCase(cli, root / "after-failure", dict(config, port=occupied, duration_s=.4), environment) as run:
        run.finish(expected=0, stop=False)
    cases.append("occupied-port-failure-and-recovery")
    with ProcessCase(cli, root / "parent-pipe-closed", dict(config, port=available_port()), environment) as run:
        run.started()
        run.finish(close_input=True)
    cases.append("parent-pipe-closure-stops-worker")
    with ProcessCase(cli, root / "invalid-config", dict(config, period_ms=0), environment) as run:
        run.finish(expected=2, stop=False)
    cases.append("invalid-config-terminal-error")
    return cases


class Pty:
    def __enter__(self):
        import pty
        import tty
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        return self

    def read(self, timeout=.02):
        return os.read(self.master, 4096) if select.select([self.master], [], [], timeout)[0] else b""

    def exchange(self, request, size):
        os.write(self.master, crc_frame(request))
        response = bytearray()
        deadline = time.monotonic() + 2
        while len(response) < size and time.monotonic() < deadline:
            response.extend(self.read())
        require(len(response) == size and bytes(response) == crc_frame(bytes(response[:-2])), "RTU CRC 或长度错误")
        return bytes(response)

    def __exit__(self, *_):
        os.close(self.master)
        os.close(self.slave)


def verify_pty(cli, root, environment):
    with Pty() as peer:
        config = dict(mode="rtu-slave", duration_s=0, serial=dict(port=peer.port), source=dict(value=12.5))
        with ProcessCase(cli, root / "rtu-slave", config, environment) as run:
            run.started()
            for function in (3, 4):
                reply = peer.exchange(struct.pack(">BBHH", 1, function, 0, 2), 9)
                require(reply == crc_frame(bytes((1, function, 4)) + struct.pack(">f", 12.5)), "RTU 从站读值错误")
            run.finish()
    with Pty() as peer:
        stop, errors, trace = threading.Event(), [], []

        def master_peer():
            buffer, registers = bytearray(), {}
            try:
                while not stop.is_set():
                    buffer.extend(peer.read())
                    while len(buffer) >= 8:
                        function = buffer[1]
                        size = 9 + buffer[6] if function == 16 else 8
                        if len(buffer) < size:
                            break
                        request = bytes(buffer[:size])
                        del buffer[:size]
                        require(crc_frame(request[:-2]) == request, "RTU 主站请求 CRC 错误")
                        unit, function, address, count = struct.unpack(">BBHH", request[:6])
                        if function == 16:
                            words = struct.unpack(">" + "H" * count, request[7:-2])
                            registers.update({address + index: value for index, value in enumerate(words)})
                            response = request[:6]
                        else:
                            require(function in (3, 4), "RTU 主站发出非预期功能码")
                            response = bytes((unit, function, count * 2)) + struct.pack(">" + "H" * count,
                                *(registers[address + index] for index in range(count)))
                        time.sleep(.005)
                        response = crc_frame(response)
                        os.write(peer.master, response)
                        trace.append(dict(request=request.hex(), response=response.hex()))
            except Exception as error:
                errors.append(repr(error))

        thread = threading.Thread(target=master_peer)
        thread.start()
        try:
            config = dict(mode="rtu-master", duration_s=1.2, period_ms=200, timeout_ms=150,
                          serial=dict(port=peer.port), readback_every=1, source=dict(value=12.5))
            with ProcessCase(cli, root / "rtu-master", config, environment, "run") as run:
                result = run.finish(expected=0, stop=False)
                require(result["counters"].get("ack", 0) >= 3 and result["counters"].get("read", 0) >= 3,
                        "RTU 主站写入和读回不足")
                require(not result["counters"].get("readback_mismatch", 0), "RTU 主站读回不一致")
        finally:
            stop.set()
            thread.join(3)
            save(root / "rtu-master/peer-trace.json", dict(exchanges=trace, errors=errors))
        require(not errors and not thread.is_alive(), f"独立 RTU 对端失败：{errors}")
    return ["rtu-slave-pty-fc03-fc04-stop", "rtu-master-pty-fc16-and-readback"]


def verify_release(config, bundle, root):
    root.mkdir(parents=True, exist_ok=False)
    cli, gui = config.executables(bundle)
    environment = clean_environment()
    cases = []

    def execute(name, command, env=None, timeout=90):
        completed = subprocess.run(command, cwd=root, env=env or environment, capture_output=True, timeout=timeout)
        (root / f"{name}.stdout").write_bytes(completed.stdout)
        (root / f"{name}.stderr").write_bytes(completed.stderr)
        require(completed.returncode == 0, f"实际产物自检失败：{name}，请看 {root}")

    execute("version", [str(cli), "--version"])
    execute("ports", [str(cli), "ports"])
    execute("native-modules", [str(cli), "self-check", "--output", str(root / "native-modules.json")])
    for sample in (bundle.parent / "examples").glob("*.json"):
        execute("validate-" + sample.stem, [str(cli), "validate", str(sample)])
    cases.append("version-ports-examples-and-native-modules")
    cases += verify_tcp(cli, root, environment)
    if os.name == "posix":
        cases += verify_pty(cli, root, environment)
    for name, env in (("gui-native", environment), ("gui-offscreen", dict(environment, QT_QPA_PLATFORM="offscreen"))):
        report = root / f"{name}.json"
        execute(name, [str(gui), "--self-test", str(report)], env)
        data = json.loads(report.read_text(encoding="utf-8"))
        require(data.get("frozen") and data["status"] == "passed", f"GUI 自检报告无效：{name}")
        if name == "gui-native":
            require(data["display"]["platform"] not in {"offscreen", "minimal"}, "原生 GUI 被离屏验证替代")
        cases.append(dict(name=name, cases=data["cases"], display=data["display"]))
    if sys.platform == "darwin":
        execute("codesign-integrity", ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
        cases.append("ad-hoc-codesign-integrity-not-notarization")
        # 另外经 LaunchServices 验证 .app 主入口，不能只运行 Contents/MacOS 内的文件。
        report = root / "gui-launchservices.json"
        execute("gui-launchservices", ["/usr/bin/open", "-n", "-W", str(bundle), "--args", "--self-test", str(report)])
        data = json.loads(report.read_text(encoding="utf-8"))
        require(data.get("status") == "passed" and data["display"]["platform"] == "cocoa", "应用包原生首启验收失败")
        cases.append(dict(name="gui-launchservices", cases=data["cases"], display=data["display"]))
    return dict(status="passed", target=config.target, cases=cases,
                untested=["物理串口/RS485 与真实设备", "长期压力", "外部网络", "第三方电脑的系统安全策略"],
                serial_validation="isolated-pty" if os.name == "posix" else "pending-virtual-or-physical-COM")
