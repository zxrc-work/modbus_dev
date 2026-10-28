"""按逐点操作生成确定性请求计划，预算计算与实际发送共用同一规划器。"""
from .mapping import point_ranges, point_write_requests

# 保留旧诊断代码的导入入口，运行时仅使用统一规划器。
write_requests = point_write_requests
from .transactions import Request

OPERATIONS = {"read-holding": 3, "read-input": 4, "write-single": 6, "write-multiple": 16}


def cycle_requests(config, values=(), readback=False, *, slot=None):
    points = config.enabled_points
    value_map = dict(zip((id(point) for point in points), values))
    requests = []
    # 操作间不跨功能码合并；按读取、写入顺序执行，读回紧随对应写入组。
    for operation, function in OPERATIONS.items():
        selected = [point for point in points if config.point_usage(point).operation == operation]
        if function in (3, 4):
            requests.extend(Request(function, address, count) for address, count in point_ranges(selected))
            continue
        # 不同读回频率必须分组，避免把未开启读回的点悄悄加入读回请求。
        frequencies = sorted({config.point_readback(point) for point in selected})
        for frequency in frequencies:
            group_points = [point for point in selected if config.point_readback(point) == frequency]
            group_values = [value_map.get(id(point), 0) for point in group_points]
            groups = point_write_requests(group_points, group_values, config.byte_order)
            if function == 6:
                writes = [Request(6, group.address + i, 1, (word,))
                          for group in groups for i, word in enumerate(group.registers)]
            else:
                writes = groups
            requests.extend(writes)
            due = frequency and (readback if slot is None else slot % frequency == 0)
            if due:
                requests.extend(Request(3, group.address, group.count, group.registers) for group in groups)
    return requests


def primary_requests(config, values=()):
    return cycle_requests(config, values)
