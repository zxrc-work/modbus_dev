"""按测点实际宽度映射、编码和分组，保持地址与配置顺序中的数值绑定。"""
from .config import PointConfig
from .numeric import ORDERS, encode_value, resolve_order
from .transactions import Request


def point_ranges(points):
    """相邻完整测点合并为读取区间，不跨未配置地址。"""
    groups = []
    for point in sorted(points, key=lambda point: point.address):
        if groups and groups[-1][0] + groups[-1][1] == point.address:
            groups[-1][1] += point.register_count
        else:
            groups.append([point.address, point.register_count])
    return groups


def point_write_requests(points, values, default_order="B1234"):
    if len(points) != len(values):
        raise ValueError("Each point requires one source value")
    groups = []
    for point, value in sorted(zip(points, values), key=lambda pair: pair[0].address):
        order = resolve_order(point.data_type, point.byte_order, default_order)
        words = encode_value(value, point.data_type, order)
        if groups and groups[-1][0] + len(groups[-1][1]) == point.address:
            groups[-1][1].extend(words)
        else:
            groups.append((point.address, list(words)))
    return [Request(16, address, len(words), tuple(words)) for address, words in groups]


def write_requests(addresses, values, order):
    """保留旧 float32 调用入口，实际编码仍只走公共类型模块。"""
    return point_write_requests(tuple(PointConfig(address=address) for address in addresses), values, order)
