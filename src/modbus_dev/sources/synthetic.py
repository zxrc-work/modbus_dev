"""按统一槽位时钟分别计算各启用测点，保持数值和地址的配置顺序一致。"""
import math


class SyntheticSource:
    def __init__(self, config):
        if any(point.source.kind not in ("constant", "sine") for point in config.enabled_points):
            raise ValueError("Offline sources are deferred until after GUI; no device has been opened")
        self.config = config

    def sample(self, slot):
        # 同一周期使用同一源时间，但每点保留独立的常量、偏置、振幅与频率。
        time_s = slot * self.config.period_ms / 1000
        values = []
        for point in self.config.enabled_points:
            source = point.source
            value = source.value
            if source.kind == "sine":
                value += source.amplitude * math.sin(2 * math.pi * source.frequency_hz * time_s)
            values.append(value)
        return tuple(values)
