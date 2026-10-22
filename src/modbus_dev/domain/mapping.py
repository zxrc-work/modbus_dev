"""Float point grouping extracted from the existing RTU simulator.

Uses the standard struct codec, not a second Modbus implementation.
"""
import math
import struct

from .transactions import Request

ORDERS = {"B1234": (0, 1, 2, 3), "B4321": (3, 2, 1, 0),
          "B2143": (1, 0, 3, 2), "B3412": (2, 3, 0, 1)}


def write_requests(addresses, values, order):
    if len(addresses) != len(values):
        raise ValueError("Each address requires one source value")
    groups = []
    for address, value in sorted(zip(addresses, values)):
        if not math.isfinite(value):
            raise ValueError("Nonfinite source value")
        raw = struct.pack(">f", value)
        words = struct.unpack(">HH", bytes(raw[i] for i in ORDERS[order]))
        if groups and groups[-1][0] + len(groups[-1][1]) == address:
            groups[-1][1].extend(words)
        else:
            groups.append((address, list(words)))
    return [Request(16, address, len(words), tuple(words)) for address, words in groups]
