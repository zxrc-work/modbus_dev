"""按实际操作计划计算串口线速预算，不依赖通信驱动。"""


def minimum_cycle_ms(config):
    """All-good write groups plus readback, excluding device processing.

    Runtime must recalculate if source quality changes the write groups.
    Readback configs reserve their busiest slot, not an average over slots.
    """
    from .operations import cycle_requests
    requests = cycle_requests(config, [0.] * len(config.addresses), bool(config.readback_every))
    wire_bytes, transactions = 0, 0
    for request in requests:
        request_bytes = 9 + 2 * request.count if request.function == 16 else 8
        response_bytes = 8 if request.function in (6, 16) else 5 + 2 * request.count
        wire_bytes += request_bytes + response_bytes
        transactions += 1
    character_ms = 1000 * config.serial.bits_per_character / config.serial.baud
    return wire_bytes * character_ms + (2 * transactions - 1) * config.serial.gap_ms
