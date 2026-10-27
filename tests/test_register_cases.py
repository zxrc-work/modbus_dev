"""验证外部固定报文诊断工具及分片交付的 FC06/FC16 写入回执。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import verify_register_cases as checker
from verify_backend_communications import SerialWire
from verify_virtual_serial import frame
sys.path.remove(str(TOOLS))


class RegisterCaseTests(unittest.TestCase):
    def test_case_validation_finishes_before_opening_any_peer(self):
        cases = [[], {}, [dict(request="")], [dict(request="zz")], [dict(request="03", unit=True)],
                 [dict(request="03", response=123)], [dict(request="03", response="gg")]]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cases.json"
            for data in cases:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        checker.load_cases(path)

    def test_fixed_response_check_records_success_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            path, output = Path(root) / "cases.json", Path(root) / "evidence"
            path.write_text(json.dumps([dict(request="0300000001", response="0302ffff")]))
            with patch.object(checker, "TcpWire") as transport, redirect_stdout(io.StringIO()):
                transport.return_value.exchange.return_value = bytes.fromhex("0302ffff")
                args = ["--host", "unused", "--cases", str(path), "--output", str(output)]
                self.assertEqual(checker.main(args), 0)
                transport.return_value.exchange.assert_called_once_with(bytes.fromhex("0300000001"), unit=1, silence=False)
                self.assertEqual(json.loads((output / "report.json").read_text())["status"], "passed")
                with self.assertRaises(FileExistsError):
                    checker.main(args)

    def test_failed_comparison_keeps_evidence_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path, output = Path(root) / "cases.json", Path(root) / "failure"
            path.write_text(json.dumps([dict(request="0300000001", response="0302ffff")]))
            with patch.object(checker, "TcpWire") as transport, redirect_stdout(io.StringIO()):
                transport.return_value.exchange.return_value = bytes.fromhex("03020000")
                self.assertEqual(checker.main(["--host", "unused", "--cases", str(path), "--output", str(output)]), 1)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["cases"][0]["passed"])

    def test_fragmented_rtu_write_ack_uses_fixed_length(self):
        class Peer:
            def __init__(self, data):
                self.reply = bytearray(frame(data))

            @property
            def in_waiting(self):
                return 1

            def write(self, packet):
                return len(packet)

            def read(self, count):
                result = bytes(self.reply[:1])
                del self.reply[:1]
                return result

        for pdu in ("060000ffff", "1000000006"):
            with self.subTest(pdu=pdu):
                trace = []
                response = bytes.fromhex(pdu)
                wire = SerialWire(Peer(b"\x01" + response), trace)
                self.assertEqual(wire.exchange(response), response)
                self.assertEqual(len(bytes.fromhex(trace[0]["response"])), 8)


if __name__ == "__main__":
    unittest.main()
