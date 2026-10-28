"""验证配置边界、绝对调度和日志证据的基础约束。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from modbus_dev.cli import main
from modbus_dev.domain.config import TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError, Event
from modbus_dev.domain.wire import minimum_cycle_ms
from modbus_dev.application.timing import AbsoluteSchedule, ScheduleOverrun
from modbus_dev.reporting.journal import Journal, LogWriteError


def mapping(**overrides):
    return dict(serial=dict(port="COM11"), **overrides)


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        config = TaskConfig.from_mapping(mapping())
        self.assertEqual(config, TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))))

    def test_logging_defaults_limits_and_roundtrip(self):
        self.assertEqual(TaskConfig.from_mapping(mapping()).logging.max_files, 100)
        for count in (1, 3, 100, 10000):
            config = TaskConfig.from_mapping(mapping(logging=dict(max_files=count)))
            self.assertEqual(config.logging.max_files, count)
            self.assertEqual(config, TaskConfig.from_mapping(config.to_mapping()))
        for value in (0, -1, 10001, True, 1.5, "100", None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(mapping(logging=dict(max_files=value)))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(mapping(logging=dict(max_fiels=100)))

    def test_turnaround_is_part_of_transaction_budget(self):
        for timeout in (22, 25):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(mapping(timeout_ms=timeout))
        config = TaskConfig.from_mapping(mapping(timeout_ms=26))
        self.assertAlmostEqual(minimum_cycle_ms(config), 25.5208333333)

    def test_grouping_and_readback_budget(self):
        one = TaskConfig.from_mapping(mapping())
        contiguous = TaskConfig.from_mapping(mapping(addresses=[0, 2]))
        disjoint = TaskConfig.from_mapping(mapping(addresses=[0, 4]))
        readback = TaskConfig.from_mapping(mapping(readback_every=10))
        self.assertGreater(minimum_cycle_ms(contiguous), minimum_cycle_ms(one))
        self.assertGreater(minimum_cycle_ms(disjoint), minimum_cycle_ms(contiguous))
        self.assertGreater(minimum_cycle_ms(readback), minimum_cycle_ms(one))
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(mapping(addresses=[0, 4, 8, 12]))

    def test_parity_and_stop_bits_change_wire_budget(self):
        one = TaskConfig.from_mapping(mapping())
        changed = TaskConfig.from_mapping(dict(serial=dict(port="COM11", parity="E", stop_bits=2)))
        self.assertAlmostEqual(minimum_cycle_ms(changed) / minimum_cycle_ms(one), 1.2)

    def test_unknown_fields_versions_and_bad_types_rejected(self):
        cases = [mapping(timeuot_ms=80), mapping(schema_version=5), mapping(unit=True),
                 mapping(unit="1"), mapping(addresses=[True]), mapping(addresses=[0, 1]),
                 mapping(duration_s=float("nan")), mapping(source=dict(amplitdue=1)),
                 mapping(source=dict(kind="dyn", channels="channel-1")), mapping(mode=[])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(data)

    def test_signal_bounds_and_nyquist(self):
        for source in (dict(kind="sine", frequency_hz=5),
                       dict(kind="sine", value=3e38, amplitude=3e38),
                       dict(kind="constant", value=float("inf"))):
            with self.subTest(source=source), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(mapping(source=source))

    def test_dyn_requires_explicit_channels(self):
        with self.assertRaises(ConfigurationError):
            TaskConfig.from_mapping(mapping(source=dict(kind="dyn", path="input.dyn")))
        TaskConfig.from_mapping(mapping(source=dict(kind="dyn", path="input.dyn", channels=["P"])))

    def test_duplicate_json_field_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text('{"unit":1,"unit":2}', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "duplicate field"):
                load_config(path)

    def test_cli_output_and_error_exit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps(mapping()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["validate", str(path)]), 0)
            self.assertTrue(json.loads(out.getvalue())["configuration_only"])
            self.assertEqual(json.loads(out.getvalue())["schedule_late_basis"], "cycle_first_send_interval")
            self.assertEqual(json.loads(out.getvalue())["cycle_interval_limit_ms"], 110)
            path.write_text('{"oops":1}', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["validate", str(path)]), 2)
            self.assertEqual(json.loads(err.getvalue())["code"], "configuration_invalid")


class TimingTests(unittest.TestCase):
    def test_absolute_slots_do_not_accumulate_reply_time(self):
        for period in (.1, .2):
            schedule = AbsoluteSchedule(.1, period, .08)
            times = [schedule.at(i, .1 + i * period + .035).scheduled_s for i in range(100)]
            for a, b in zip(times, times[1:]):
                self.assertAlmostEqual(b - a, period)

    def test_overrun_skips_to_future_slot(self):
        result = AbsoluteSchedule(.1, .1, .08).at(1, .45)
        self.assertAlmostEqual(result.scheduled_s, .5)
        self.assertEqual(result.skipped, 3)

    def test_strict_replay_never_silently_drops_samples(self):
        with self.assertRaises(ScheduleOverrun):
            AbsoluteSchedule(.1, .1, .08, "strict").at(1, .45)

    def test_silence_and_remaining_wire_budget_both_required(self):
        allowed = AbsoluteSchedule.can_start
        self.assertFalse(allowed(.2, .20365, .28, .02552))
        self.assertTrue(allowed(.20365, .20365, .28, .02552))
        self.assertFalse(allowed(.26, .26, .28, .02552))


class PausedFile:
    def __init__(self, fail=False):
        self.entered, self.release = threading.Event(), threading.Event()
        self.data = []
        self.fail = fail

    def write(self, data):
        self.entered.set()
        if not self.release.wait(3):
            raise OSError("Test writer release timeout")
        if self.fail:
            raise OSError("Simulated disk full")
        self.data.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class JournalTests(unittest.TestCase):
    def test_file_records_and_summary(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "run", {"schema_version": 1})
            for i in range(20):
                journal.emit(Event("transaction", "Transaction completed", details={"slot": i}))
            summary = journal.close()
            rows = [json.loads(line) for line in (journal.directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual([r["sequence"] for r in rows], list(range(1, 21)))
            self.assertEqual(summary["written"], 20)
            self.assertTrue(summary["evidence_complete"])
            self.assertEqual(summary["pending"], 0)

    def test_slow_disk_does_not_block_submit_and_loss_is_visible(self):
        with tempfile.TemporaryDirectory() as root:
            output = PausedFile()
            journal = Journal(Path(root) / "run", {}, capacity=1, opener=lambda _: output)
            try:
                journal.emit(Event("first", "First"))
                self.assertTrue(output.entered.wait(1))
                self.assertTrue(journal.emit(Event("second", "Second")))
                self.assertFalse(journal.emit(Event("failure", "Connection lost", "ERROR")))
                snapshot = journal.snapshot()
                self.assertEqual(snapshot["dropped"], 1)
                self.assertEqual(snapshot["last_error"]["code"], "failure")
                self.assertFalse(snapshot["evidence_complete"])
            finally:
                output.release.set()
                summary = journal.close()
            self.assertEqual(summary["dropped_by_level"], {"ERROR": 1})
            records = [json.loads(raw) for raw in output.data]
            self.assertEqual(records[-1]["code"], "log_overflow")
            self.assertEqual(records[-1]["details"]["dropped"], 1)

    def test_disk_error_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as root:
            output = PausedFile(fail=True)
            output.release.set()
            journal = Journal(Path(root) / "run", {}, opener=lambda _: output)
            journal.emit(Event("transaction", "Transaction"))
            self.assertTrue(journal.failed.wait(1))
            with self.assertRaisesRegex(LogWriteError, "disk full"):
                journal.close()
            summary = json.loads((journal.directory / "summary.json").read_text())
            self.assertFalse(summary["evidence_complete"])

    def test_size_limit_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "run", {}, max_bytes=1)
            journal.emit(Event("transaction", "Transaction"))
            self.assertTrue(journal.failed.wait(1))
            with self.assertRaisesRegex(LogWriteError, "size limit"):
                journal.close()
            self.assertEqual((journal.directory / "events.jsonl").read_bytes(), b"")

    def test_oversize_event_is_counted_instead_of_filling_queue(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "run", {})
            self.assertFalse(journal.emit(Event("oversize", "x" * 40000)))
            self.assertEqual(journal.close()["dropped"], 1)

    def test_details_are_snapshotted_at_submission(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "run", {})
            details = {"slot": 1}
            journal.emit(Event("transaction", "Transaction", details=details))
            details["slot"] = 2
            journal.close()
            row = json.loads((journal.directory / "events.jsonl").read_text())
            self.assertEqual(row["details"]["slot"], 1)

    def test_existing_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileExistsError):
                Journal(root, {})


if __name__ == "__main__":
    unittest.main()
