"""按外部固定 PDU 样例验证 TCP/RTU 对端，不复用生产数值编解码器。"""
import argparse
import json
from pathlib import Path

import serial

from verify_backend_communications import SerialWire, TcpWire, save


def load_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases must be a nonempty JSON array")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("request"), str):
            raise ValueError("Each case requires a hexadecimal request PDU")
        if not bytes.fromhex(case["request"]):
            raise ValueError("Request PDU cannot be empty")
        response = case.get("response")
        if response is not None and not isinstance(response, str):
            raise ValueError("Response must be a hexadecimal PDU or null for silence")
        if response is not None:
            bytes.fromhex(response)
        unit = case.get("unit", 1)
        if type(unit) is not int or not 0 <= unit <= 247:
            raise ValueError("Unit must be an integer in 0..247")
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--host", help="TCP peer host")
    target.add_argument("--serial-port", help="RTU peer serial port; requests may write actual registers")
    parser.add_argument("--port", type=int, default=1502)
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--parity", choices=("N", "E", "O"), default="N")
    parser.add_argument("--stop-bits", type=int, choices=(1, 2), default=1)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new evidence directory")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    args.output.mkdir(parents=True, exist_ok=False)
    trace, outcomes = [], []
    wire = peer = None
    report = dict(status="failed", cases=outcomes)
    try:
        if args.host:
            wire = TcpWire(args.host, args.port, trace)
        else:
            peer = serial.Serial(args.serial_port, baudrate=args.baud, parity=args.parity,
                                 stopbits=args.stop_bits, timeout=.01, write_timeout=.5)
            wire = SerialWire(peer, trace)
        for index, case in enumerate(cases):
            expected = case.get("response")
            actual = wire.exchange(bytes.fromhex(case["request"]), unit=case.get("unit", 1), silence=expected is None)
            passed = actual == (b"" if expected is None else bytes.fromhex(expected))
            outcomes.append(dict(name=case.get("name", str(index + 1)), passed=passed,
                                 expected=expected, actual=actual.hex()))
            if not passed:
                raise AssertionError(f"Case {index + 1} response mismatch: {actual.hex()}")
        report["status"] = "passed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if args.host and wire is not None:
            wire.close()
        if peer is not None:
            peer.close()
        # 失败现场与复测使用不同目录，完整报文不进入维护文档。
        save(args.output / "trace.json", trace)
        save(args.output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
