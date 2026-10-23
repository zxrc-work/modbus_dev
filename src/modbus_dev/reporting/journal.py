"""非阻塞事件队列与独立日志线程；按文件大小分片、有界保留并显式记录历史淘汰。"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from ..domain.events import Event
from ..domain.config import LoggingConfig
from .segments import MAX_SEGMENT_BYTES, SegmentedEventWriter


class LogWriteError(RuntimeError):
    code = "log_write_failed"


class Journal:
    MAX_EVENT_BYTES = 32768

    def __init__(self, directory, config, *, capacity=8192, flush_interval=.2,
                 max_bytes=MAX_SEGMENT_BYTES, opener=None):
        if capacity < 1 or flush_interval <= 0 or max_bytes < 1:
            raise ValueError("Invalid journal bounds")
        logging = LoggingConfig(**config.get("logging", {}))
        logging.validate()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        self.session_id = uuid.uuid4().hex
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self.failed = threading.Event()
        self._error = None
        self._closed = False
        self._sequence = self._written = 0
        self._dropped = Counter()
        self._first_dropped = self._last_dropped = None
        self._last_error = None
        self._reported_drops = 0
        self._flush_interval = flush_interval
        self._segments = SegmentedEventWriter(self.directory, max_bytes=max_bytes,
                                              max_files=logging.max_files, opener=opener)
        self._thread = threading.Thread(target=self._write, name="modbus-journal", daemon=True)
        self._thread.start()

    def emit(self, event: Event):
        """Never wait for disk or queue capacity; returns whether queued.

        JSON conversion captures mutable details at submission time. Context
        must be compact; raw I/O should already be split into bounded chunks.
        """
        with self._lock:
            if self._closed or self._closing.is_set():
                raise RuntimeError("Journal is closing or closed")
            self._sequence += 1
            sequence = self._sequence
            record = dict(schema_version=1, session_id=self.session_id, sequence=sequence,
                          utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                          monotonic_ns=time.perf_counter_ns(), code=event.code, level=event.level,
                          phase=event.phase, message=event.message, details=event.details)
            encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            if event.level == "ERROR":
                # Separate channel: a full queue must not hide the last error.
                self._last_error = dict(code=event.code, message=event.message[:2000], sequence=sequence)
            if self.failed.is_set() or len(encoded) > self.MAX_EVENT_BYTES:
                self._drop(event.level, sequence)
                return False
            try:
                self._queue.put_nowait((sequence, encoded))
                return True
            except queue.Full:
                self._drop(event.level, sequence)
                return False

    def _drop(self, level, sequence):
        self._dropped[level] += 1
        if self._first_dropped is None:
            self._first_dropped = sequence
        self._last_dropped = sequence

    def snapshot(self, *, include_files=False):
        with self._lock:
            result = dict(session_id=self.session_id, submitted=self._sequence, written=self._written,
                        dropped=sum(self._dropped.values()), dropped_by_level=dict(self._dropped),
                        first_dropped_sequence=self._first_dropped, last_dropped_sequence=self._last_dropped,
                        last_error=self._last_error, log_error=self._error,
                        evidence_complete=not self._dropped and not self.failed.is_set(),
                        pending=self._queue.qsize())
        # 分片锁不与生产者锁嵌套；任何文件操作都不能反向阻塞通信线程入队。
        result["segments"] = self._segments.snapshot(include_files=include_files)
        result["retention_truncated"] = bool(result["segments"]["deleted_files"])
        result["evidence_complete"] &= not result["retention_truncated"]
        return result

    def _write(self):
        flushed = time.perf_counter()
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=min(.05, self._flush_interval))
                except queue.Empty:
                    item = None
                if item is not None:
                    sequence, encoded = item
                    self._segments.write(encoded, sequence=sequence)
                    with self._lock:
                        self._written += 1
                now = time.perf_counter()
                if now - flushed >= self._flush_interval:
                    self._write_gap()
                    self._segments.flush()
                    flushed = now
            self._write_gap()
            self._segments.flush()
        except Exception as error:
            with self._lock:
                self._error = f"{type(error).__name__}: {error}"
                self.failed.set()
        finally:
            try:
                self._segments.close()
            except Exception as error:
                with self._lock:
                    self._error = f"{type(error).__name__}: {error}"
                    self.failed.set()

    def _write_gap(self):
        snapshot = self.snapshot()
        if snapshot["dropped"] == self._reported_drops:
            return
        # Writer-generated control records have no producer sequence; bounds
        # describe a range containing losses, not a contiguous missing range.
        record = dict(schema_version=1, session_id=self.session_id, sequence=None,
                      utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      monotonic_ns=time.perf_counter_ns(), code="log_overflow", level="WARNING",
                      phase="recording", message="Event evidence is incomplete",
                      details={key: snapshot[key] for key in ("dropped", "dropped_by_level",
                               "first_dropped_sequence", "last_dropped_sequence")})
        encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        self._segments.write(encoded)
        self._reported_drops = snapshot["dropped"]

    def close(self, timeout=3, run_summary=None):
        self._closing.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.failed.set()
            raise LogWriteError("Log writer did not finish before shutdown deadline")
        self._closed = True
        summary = self.snapshot(include_files=True)
        if run_summary is not None:
            summary["run"] = dict(run_summary)
            if self.failed.is_set():
                summary["run"].update(state="failed", stop_reason="log_write_failed",
                                      error=self._error, exit_code=1)
        path = self.directory / "summary.json"
        if not path.exists():
            path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.failed.is_set():
            raise LogWriteError(self._error)
        return summary
