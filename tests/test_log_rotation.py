"""验证日志分片、保留上限、序号连续性及磁盘故障，使用小文件覆盖真实边界。"""
import errno
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from modbus_dev.domain.events import Event
from modbus_dev.reporting.journal import Journal, LogWriteError
from modbus_dev.reporting.segments import MAX_SEGMENT_BYTES, SegmentedEventWriter, event_paths


def read_records(directory):
    return [json.loads(line) for path in event_paths(directory)
            for line in path.read_text(encoding="utf-8").splitlines()]


def emit_samples(journal, count, limit):
    for index in range(count):
        accepted = journal.emit(Event("sample", "x" * (limit // 2), details={"index": index}))
        if not accepted:
            raise AssertionError("测试生产者意外丢失事件")


class PausedOutput:
    def __init__(self, path, entered, release):
        self.output = path.open("xb")
        self.entered, self.release = entered, release

    def write(self, data):
        self.entered.set()
        if not self.release.wait(3):
            raise OSError("测试写入等待释放超时")
        return self.output.write(data)

    def flush(self):
        self.output.flush()

    def close(self):
        self.output.close()


class FaultyOutput:
    def __init__(self, path, failure):
        self.output, self.failure = path.open("xb"), failure

    def write(self, data):
        return self.output.write(data[:-1] if self.failure == "short" else data)

    def flush(self):
        if self.failure == "flush":
            raise OSError("injected flush failure")
        self.output.flush()

    def close(self):
        self.output.close()
        if self.failure == "close":
            raise OSError("injected close failure")


class LogRotationTests(unittest.TestCase):
    def test_exact_boundary_rotates_before_next_record(self):
        with tempfile.TemporaryDirectory() as root:
            writer = SegmentedEventWriter(root, max_bytes=4, max_files=2)
            try:
                writer.write(b"a\n", sequence=1)
                writer.write(b"b\n", sequence=2)
                self.assertEqual(writer.snapshot()["rotations"], 0)
                writer.write(b"c\n", sequence=3)
            finally:
                writer.close()
            self.assertEqual([path.read_bytes() for path in event_paths(root)], [b"a\nb\n", b"c\n"])
            self.assertTrue(all(path.stat().st_size <= 4 for path in event_paths(root)))

    def test_rotations_keep_all_sequences_before_retention_limit(self):
        with tempfile.TemporaryDirectory() as root:
            limit, count = 1024, 8
            journal = Journal(Path(root) / "run", {}, max_bytes=limit)
            emit_samples(journal, count, limit)
            self.assertNotIn("files", journal.snapshot()["segments"])
            summary = journal.close()
            self.assertEqual([row["sequence"] for row in read_records(journal.directory)], list(range(1, count + 1)))
            self.assertEqual(summary["written"], count)
            self.assertEqual(summary["segments"]["rotations"], count - 1)
            self.assertTrue(summary["evidence_complete"])
            self.assertFalse(journal.failed.is_set())
            self.assertEqual([item["name"] for item in summary["segments"]["files"]],
                             [path.name for path in event_paths(journal.directory)])
            self.assertTrue(all(path.stat().st_size <= limit for path in event_paths(journal.directory)))

    def test_retention_limits_files_and_separates_deletion_from_queue_loss(self):
        for maximum in (1, 2, 100):
            with self.subTest(maximum=maximum), tempfile.TemporaryDirectory() as root:
                limit, count = 1024, maximum + 3
                journal = Journal(Path(root) / "run", dict(logging=dict(max_files=maximum)), max_bytes=limit)
                emit_samples(journal, count, limit)
                summary = journal.close(run_summary=dict(state="stopped", exit_code=0))
                paths = event_paths(journal.directory)
                self.assertEqual(len(paths), maximum)
                self.assertTrue(all(path.stat().st_size <= limit for path in paths))
                self.assertEqual([row["sequence"] for row in read_records(journal.directory)],
                                 list(range(4, count + 1)))
                self.assertEqual(summary["written"], count)
                self.assertEqual(summary["dropped"], 0)
                self.assertEqual(summary["segments"]["deleted_files"], 3)
                self.assertEqual(summary["segments"]["deleted_records"], 3)
                self.assertEqual(summary["segments"]["first_deleted_sequence"], 1)
                self.assertEqual(summary["segments"]["last_deleted_sequence"], 3)
                self.assertEqual(summary["segments"]["retained_bytes"], sum(path.stat().st_size for path in paths))
                self.assertTrue(summary["retention_truncated"])
                self.assertFalse(summary["evidence_complete"])
                self.assertFalse(journal.failed.is_set())
                self.assertEqual(summary["run"]["exit_code"], 0)
                self.assertIsNone(summary["log_error"])

    def test_queue_gap_record_can_rotate_without_stopping_writer(self):
        with tempfile.TemporaryDirectory() as root:
            entered, release = threading.Event(), threading.Event()
            def opener(path):
                return PausedOutput(path, entered, release) if path.name == "events.jsonl" else path.open("xb")
            journal = Journal(Path(root) / "run", {}, max_bytes=1024, capacity=1, opener=opener)
            try:
                journal.emit(Event("first", "x" * 512))
                self.assertTrue(entered.wait(1))
                self.assertTrue(journal.emit(Event("second", "x" * 512)))
                self.assertFalse(journal.emit(Event("failure", "queue full", "ERROR")))
            finally:
                release.set()
                summary = journal.close()
            rows = read_records(journal.directory)
            self.assertEqual(rows[-1]["code"], "log_overflow")
            self.assertEqual(rows[-1]["details"]["dropped"], 1)
            self.assertEqual(summary["dropped"], 1)
            self.assertFalse(summary["retention_truncated"])
            self.assertFalse(journal.failed.is_set())
            self.assertGreater(summary["segments"]["rotations"], 0)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in event_paths(journal.directory)))

    def test_new_segment_open_failure_is_fatal_and_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            def opener(path):
                if path.name != "events.jsonl":
                    raise OSError(errno.ENOSPC, "simulated disk full")
                return path.open("xb")
            journal = Journal(Path(root) / "run", {}, max_bytes=1024, opener=opener)
            emit_samples(journal, 2, 1024)
            self.assertTrue(journal.failed.wait(3))
            with self.assertRaisesRegex(LogWriteError, "disk full"):
                journal.close(run_summary=dict(state="stopped", exit_code=0))
            summary = json.loads((journal.directory / "summary.json").read_text())
            self.assertEqual([row["sequence"] for row in read_records(journal.directory)], [1])
            self.assertFalse(summary["evidence_complete"])
            self.assertEqual(summary["run"]["exit_code"], 1)

    def test_old_segment_delete_failure_stops_without_exceeding_file_limit(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "run", dict(logging=dict(max_files=1)), max_bytes=1024)
            with patch.object(Path, "unlink", side_effect=PermissionError("deletion blocked")):
                emit_samples(journal, 2, 1024)
                self.assertTrue(journal.failed.wait(3))
                with self.assertRaisesRegex(LogWriteError, "deletion blocked"):
                    journal.close()
            self.assertEqual(len(event_paths(journal.directory)), 1)
            self.assertEqual([row["sequence"] for row in read_records(journal.directory)], [1])

    def test_write_flush_and_close_failures_are_fatal(self):
        for failure, message in (("short", "Short"), ("flush", "flush failure"), ("close", "close failure")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                journal = Journal(Path(root) / "run", {}, max_bytes=1024, flush_interval=60,
                                  opener=lambda path: FaultyOutput(path, failure))
                emit_samples(journal, 2, 1024)
                self.assertTrue(journal.failed.wait(3))
                with self.assertRaisesRegex(LogWriteError, message):
                    journal.close(run_summary=dict(state="stopped", exit_code=0))
                summary = json.loads((journal.directory / "summary.json").read_text())
                self.assertFalse(summary["evidence_complete"])
                self.assertEqual(summary["run"]["exit_code"], 1)

    def test_oversize_record_does_not_delete_existing_segment(self):
        with tempfile.TemporaryDirectory() as root:
            writer = SegmentedEventWriter(root, max_bytes=4, max_files=1)
            try:
                writer.write(b"123\n", sequence=1)
                with self.assertRaisesRegex(OSError, "size limit"):
                    writer.write(b"1234\n", sequence=2)
            finally:
                writer.close()
            self.assertEqual(writer.snapshot()["deleted_files"], 0)
            self.assertEqual([path.read_bytes() for path in event_paths(root)], [b"123\n"])

    def test_reader_orders_numerically_without_initial_segment(self):
        with tempfile.TemporaryDirectory() as root:
            names = ["events-1000000.jsonl", "events-000002.jsonl", "events-999999.jsonl", "events-backup.jsonl"]
            for name in names:
                (Path(root) / name).touch()
            self.assertEqual([path.name for path in event_paths(root)],
                             ["events-000002.jsonl", "events-999999.jsonl", "events-1000000.jsonl"])

    def test_segment_size_cannot_exceed_production_limit(self):
        with tempfile.TemporaryDirectory() as root:
            for size in (0, True, MAX_SEGMENT_BYTES + 1):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    SegmentedEventWriter(root, max_bytes=size, max_files=1)


if __name__ == "__main__":
    unittest.main()
