"""统一生成 RTU 四种寄存器操作，供调度与线速预算共用。"""
from .mapping import write_requests
from .transactions import Request

OPERATIONS = {
    "read-holding": 3, "read-input": 4,
    "write-single": 6, "write-multiple": 16,
}


def primary_requests(config, values):
    groups = write_requests(config.addresses, values, config.byte_order)
    function = OPERATIONS[config.master_operation]
    if function in (3, 4):
        return [Request(function, group.address, group.count) for group in groups]
    if function == 6:
        # 一个 float32 占两个寄存器，FC06 必须拆成两次独立写入，不宣称原子性。
        return [Request(6, group.address + offset, 1, (word,))
                for group in groups for offset, word in enumerate(group.registers)]
    return groups


def cycle_requests(config, values, readback=False):
    requests = primary_requests(config, values)
    if readback and config.master_function in (6, 16):
        groups = write_requests(config.addresses, values, config.byte_order)
        requests += [Request(config.read_function, group.address, group.count, group.registers)
                     for group in groups]
    return requests
