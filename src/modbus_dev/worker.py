"""版本化 JSONL 工作进程；快照最多 5Hz，缓慢界面管道不阻塞通信线程。"""
from collections import deque
import json
import os
import queue
import sys
import threading
import traceback

from .application.session import SessionRunner
from .domain.config import load_config
from .reporting.journal import Journal
from .domain.live_policy import VALUE_REFRESH_HZ


class EventPreview:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque(maxlen=100)
        self.dropped = 0

    def add(self, event):
        if event.level == "DEBUG":
            return
        with self.lock:
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.events.append(dict(level=event.level, code=event.code, message=event.message[:300]))

    def drain(self):
        with self.lock:
            events = list(self.events)
            self.events.clear()
            return events, self.dropped


def main(config_path, output):
    stop = threading.Event()
    preview = EventPreview()
    outgoing = queue.Queue(maxsize=2)
    pipe_failed = threading.Event()
    output_fd = sys.stdout.fileno()

    def writer():
        try:
            while True:
                payload = outgoing.get()
                data = (json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
                # Avoid Python's buffered stdout lock: a daemon blocked on a full
                # pipe must not deadlock interpreter shutdown during final flush.
                while data:
                    count = os.write(output_fd, data)
                    if not count:
                        raise OSError("Worker output pipe closed")
                    data = data[count:]
                if payload["type"] == "result":
                    return
        except (OSError, ValueError):
            pipe_failed.set()
            stop.set()

    def commands():
        try:
            while True:
                line = sys.stdin.readline(4097)
                if not line:
                    stop.set()
                    return
                message = json.loads(line)
                if len(line) > 4096 or message != {"schema_version": 1, "command": "stop"}:
                    raise ValueError("Unsupported worker command")
                stop.set()
        except (ValueError, OSError):
            stop.set()

    def publish(payload):
        payload["schema_version"] = 1
        # Replace a stale status rather than waiting for a UI that stopped reading.
        while True:
            try:
                outgoing.put_nowait(payload)
                return
            except queue.Full:
                try:
                    discarded = outgoing.get_nowait()
                    with preview.lock:
                        preview.dropped += len(discarded.get("events", []))
                        payload["preview_dropped"] = preview.dropped
                except queue.Empty:
                    pass

    class PreviewJournal(Journal):
        def emit(self, event):
            result = super().emit(event)
            preview.add(event)
            return result

    sender = threading.Thread(target=writer, name="worker-output", daemon=True)
    sender.start()
    threading.Thread(target=commands, name="worker-control", daemon=True).start()
    result = {}
    try:
        runner = SessionRunner(load_config(config_path), output, stop=stop, journal_factory=PreviewJournal)

        def execute():
            try:
                result.update(runner.run())
            except BaseException as exc:
                traceback.print_exc(file=sys.stderr)
                result.update(state="failed", exit_code=1, error=str(exc), stop_reason="worker_error")

        task = threading.Thread(target=execute, name="session")
        task.start()
        while task.is_alive():
            events, dropped = preview.drain()
            recording = runner.journal.snapshot() if runner.journal else {}
            publish(dict(type="status", state=runner.state, counters=dict(runner.counters),
                         recording=recording, events=events, preview_dropped=dropped,
                         live=runner.live.snapshot(runner.control.now(), runner.state)))
            task.join(1 / VALUE_REFRESH_HZ)
        task.join()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        result.update(state="failed", exit_code=2, error=str(exc), stop_reason="configuration_error")
    events, dropped = preview.drain()
    publish(dict(type="result", result=result, events=events, preview_dropped=dropped,
                 live=runner.live.snapshot(runner.control.now(), runner.state) if 'runner' in locals() else {}))
    sender.join(2)
    # A broken or blocked output channel is never reported as a successful worker.
    return 1 if sender.is_alive() or pipe_failed.is_set() else result["exit_code"]
