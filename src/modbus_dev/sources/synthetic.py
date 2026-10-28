"""按统一槽位时钟分别计算各启用测点，保持数值和地址的配置顺序一致。"""
import math
from ..domain.config import SOURCE_FIELDS
from ..domain.numeric import numeric_type

SYNTHETIC_KINDS = tuple(SOURCE_FIELDS)


def triangle_value(slot, lower, upper):
    """按逻辑更新槽位加减 1；触及边界后下一槽反向，不积累可变状态。"""
    half_cycle = math.ceil(upper - lower)
    phase = slot % (2 * half_cycle)
    if phase <= half_cycle:
        return min(lower + phase, upper)
    return max(upper - (phase - half_cycle), lower)


class SyntheticSource:
    def __init__(self, config):
        if any(point.source.kind not in SYNTHETIC_KINDS for point in config.enabled_points if config.point_usage(point).source):
            raise ValueError("Offline sources are deferred until after GUI; no device has been opened")
        self.config = config

    def sample(self, slot):
        # 同一周期使用同一源时间，但每点保留独立的常量、偏置、振幅与频率。
        time_s = slot * self.config.period_ms / 1000
        values = []
        for point in self.config.enabled_points:
            if not self.config.point_usage(point).source:
                values.append(None)
                continue
            source = point.source
            value = source.value
            if source.kind == "sine":
                value += source.amplitude * math.sin(2 * math.pi * source.frequency_hz * time_s)
            elif source.kind == "triangle":
                value = triangle_value(slot, source.lower, source.upper)
                if numeric_type(point.data_type).integer:
                    value = int(value)
            values.append(value)
        return tuple(values)
