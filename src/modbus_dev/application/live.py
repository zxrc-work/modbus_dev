"""最新数值与有界通信点缓冲，不记录发送计划或源计划值。"""
from collections import deque
from itertools import islice
import math
import struct
import threading

from ..domain.mapping import ORDERS
from ..domain.live_policy import CURVE_WINDOW_S, LIVE_BATCH_MAX_SAMPLES, LIVE_SAMPLE_CAPACITY


def decode_float(words, order):
    raw = struct.pack(">HH", *words)
    original = bytes(raw[ORDERS[order].index(index)] for index in range(4))
    value = struct.unpack(">f", original)[0]
    return value if math.isfinite(value) else None


class LiveValues:
    def __init__(self, config, *, capacity=LIVE_SAMPLE_CAPACITY):
        if capacity < 1:
            raise ValueError("Live sample capacity must be positive")
        self.config = config
        self.lock = threading.Lock()
        self.rows = {}
        self.samples = deque()
        self.capacity = capacity
        self.dropped = 0
        self.sequence = 0

    def _enqueue(self, row):
        if len(self.samples) == self.capacity:
            self.samples.popleft()
            self.dropped += 1
        self.samples.append([row["sample_id"], row["key"], row["sampled_s"], row["value"], row["quality"]])

    def consume(self, event):
        if event.code not in ("register_values", "register_confirmation"):
            return
        data = event.details
        start = data["address"]
        words = tuple(data.get("registers", ()))
        stop = start + (len(words) or data.get("count", 0))
        with self.lock:
            for index, point in enumerate(self.config.points):
                if not point.enabled or stop <= point.address or start >= point.address + 2:
                    continue
                key = f"{data['area']}:{index}:{data['direction']}"
                if event.code == "register_confirmation":
                    if key in self.rows:
                        self.rows[key]["quality"] = data["quality"]
                        # 同一采样点的确认状态更新，不制造新的测量点。
                        self._enqueue(self.rows[key])
                    continue
                complete = start <= point.address and stop >= point.address + 2
                raw = words[max(start, point.address) - start:min(stop, point.address + 2) - start]
                value = decode_float(raw, self.config.byte_order) if complete else None
                quality = data.get("quality", "received") if complete else "partial"
                if complete and value is None:
                    quality = "nonfinite"
                self.sequence += 1
                self.rows[key] = dict(key=key, sample_id=self.sequence, point=index, name=point.name or f"测点 {index + 1}",
                                     area=data["area"], address=point.address, direction=data["direction"],
                                     value=value, registers=list(raw), function=data["function"],
                                     quality=quality,
                                     sampled_s=data["sampled_s"])
                self._enqueue(self.rows[key])

    def snapshot(self, now, state, *, drain=False):
        with self.lock:
            while self.samples and self.samples[0][2] < now - CURVE_WINDOW_S:
                self.samples.popleft()
                self.dropped += 1
            samples = list(islice(self.samples, LIVE_BATCH_MAX_SAMPLES))
            if drain:
                for _ in samples:
                    self.samples.popleft()
            return dict(sampled_s=now, state=state,
                        stale_after_s=max(1., 3 * self.config.period_ms / 1000),
                        rows=[dict(row, registers=list(row["registers"])) for row in self.rows.values()],
                        samples=samples, samples_dropped=self.dropped,
                        samples_pending=len(self.samples) if drain else max(0, len(self.samples) - len(samples)))
