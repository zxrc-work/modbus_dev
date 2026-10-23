"""有界实时快照：只记录实际提交或成功解析的寄存器，不记录发送计划。"""
import math
import struct
import threading

from ..domain.mapping import ORDERS
from ..domain.live_policy import VALUE_REFRESH_HZ


def decode_float(words, order):
    raw = struct.pack(">HH", *words)
    original = bytes(raw[ORDERS[order].index(index)] for index in range(4))
    value = struct.unpack(">f", original)[0]
    return value if math.isfinite(value) else None


class LiveValues:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.rows = {}

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
                    continue
                complete = start <= point.address and stop >= point.address + 2
                raw = words[point.address - start:point.address - start + 2] if complete else words
                value = decode_float(raw, self.config.byte_order) if complete else None
                quality = data.get("quality", "received") if complete else "partial"
                if complete and value is None:
                    quality = "nonfinite"
                self.rows[key] = dict(key=key, point=index, name=point.name or f"测点 {index + 1}",
                                     area=data["area"], address=point.address, direction=data["direction"],
                                     value=value, registers=list(raw), function=data["function"],
                                     quality=quality,
                                     sampled_s=data["sampled_s"])

    def snapshot(self, now, state):
        # 每个配置点最多保留两个寄存器区、两个方向，数量不随报文累计。
        with self.lock:
            return dict(sampled_s=now, state=state,
                        stale_after_s=max(1., 3 * self.config.period_ms / 1000),
                        rows=[dict(row, registers=list(row["registers"])) for row in self.rows.values()])
