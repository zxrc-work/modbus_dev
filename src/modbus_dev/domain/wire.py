"""Wire-time estimates. Pure calculation, independent of transport libraries."""


def minimum_cycle_ms(config):
    """All-good write groups plus readback, excluding device processing.

    Runtime must recalculate if source quality changes the write groups.
    Readback configs reserve their busiest slot, not an average over slots.
    """
    groups = []
    for address in sorted(config.addresses):
        if groups and groups[-1][-1] + 2 == address:
            groups[-1].append(address)
        else:
            groups.append([address])
    wire_bytes, transactions = 0, 0
    for group in groups:
        wire_bytes += 9 + 4 * len(group) + 8
        transactions += 1
        if config.readback_every:
            wire_bytes += 8 + 5 + 4 * len(group)
            transactions += 1
    character_ms = 1000 * config.serial.bits_per_character / config.serial.baud
    return wire_bytes * character_ms + (2 * transactions - 1) * config.serial.gap_ms
