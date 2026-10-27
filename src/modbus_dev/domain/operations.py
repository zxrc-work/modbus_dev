"""统一生成 RTU 四种寄存器操作，供调度与线速预算共用。"""
from .mapping import point_ranges, point_write_requests as write_requests
from .transactions import Request

OPERATIONS = {
    "read-holding": 3, "read-input": 4,
    "write-single": 6, "write-multiple": 16,
}


def primary_requests(config, values=()):
    function = OPERATIONS[config.master_operation]
    if function in (3, 4):
        groups = point_ranges(config.enabled_points)
        return [Request(function, address, count) for address, count in groups]
    groups = write_requests(config.enabled_points, values, config.byte_order)
    if function == 6:
        # 保留原始寄存器诊断能力：多寄存器点的 FC06 分笔发送，不宣称原子性。
        return [Request(6, group.address + offset, 1, (word,))
                for group in groups for offset, word in enumerate(group.registers)]
    return groups


def cycle_requests(config, values=(), readback=False):
    requests = primary_requests(config, values)
    if readback and config.master_function in (6, 16):
        groups = write_requests(config.enabled_points, values, config.byte_order)
        requests += [Request(config.read_function, group.address, group.count, group.registers)
                     for group in groups]
    return requests
