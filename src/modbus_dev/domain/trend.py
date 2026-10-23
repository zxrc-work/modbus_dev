"""趋势数据只保留最近十秒，按界面节奏抽取最新快照，不代表原始波形采样。"""
from collections import deque
import math

from .live_policy import CURVE_MAX_POINTS, CURVE_WINDOW_S


class TrendHistory:
    def __init__(self):
        self.series = {}

    def clear(self):
        self.series.clear()

    def sample(self, snapshot, now):
        rows = {row["key"]: row for row in snapshot.get("rows", [])}
        active = snapshot.get("state") == "running"
        for key in set(self.series) | set(rows):
            # 输入只来自固定测点快照，不能由原始地址无界创建曲线。
            if key not in self.series:
                self.series[key] = deque(maxlen=CURVE_MAX_POINTS)
            row = rows.get(key)
            valid = (row and active and row["quality"] in ("received", "submitted", "confirmed") and
                     now - row["sampled_s"] <= snapshot.get("stale_after_s", 1.) and
                     row["value"] is not None and math.isfinite(row["value"]))
            value = row["value"] if valid else None
            points = self.series[key]
            if points and now - points[-1][0] > 1.:
                # 隐藏曲线或通信中断后留断点，不能跨过缺失区间画连续线。
                points.append((now - .001, None))
            points.append((now, value))
            while points and points[0][0] < now - CURVE_WINDOW_S:
                points.popleft()
