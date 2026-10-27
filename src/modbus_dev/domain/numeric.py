"""测点数值的公共编码契约；只处理类型和排列，不做工程量换算。"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import struct


# 排列针对测点的有效载荷；Modbus 寄存器在线上仍按高字节在前传输。
ORDERS = {
    "AB": (0, 1), "BA": (1, 0),
    "B1234": (0, 1, 2, 3), "B4321": (3, 2, 1, 0),
    "B2143": (1, 0, 3, 2), "B3412": (2, 3, 0, 1),
}


@dataclass(frozen=True)
class NumericType:
    code: str
    registers: int
    minimum: int | float
    maximum: int | float
    integer: bool = True

    @property
    def orders(self):
        return tuple(key for key, indices in ORDERS.items() if len(indices) == self.registers * 2)


NUMERIC_TYPES = {
    "int16": NumericType("h", 1, -(2 ** 15), 2 ** 15 - 1),
    "uint16": NumericType("H", 1, 0, 2 ** 16 - 1),
    "int32": NumericType("i", 2, -(2 ** 31), 2 ** 31 - 1),
    "uint32": NumericType("I", 2, 0, 2 ** 32 - 1),
    "float32": NumericType("f", 2, -3.4028234663852886e38, 3.4028234663852886e38, False),
}


def numeric_type(data_type):
    if not isinstance(data_type, str) or data_type not in NUMERIC_TYPES:
        raise ValueError(f"Unsupported numeric type: {data_type!r}")
    return NUMERIC_TYPES[data_type]


def resolve_order(data_type, order=None, default_order="B1234"):
    specification = numeric_type(data_type)
    if order is None:
        order = "AB" if specification.registers == 1 else default_order
    if order not in specification.orders:
        raise ValueError(f"Unsupported byte order {order!r} for {data_type}")
    return order


def parse_integer(text, data_type):
    """精确解析整数输入，避免先转 double 将细小小数或越界值舍入成合法整数。"""
    specification = numeric_type(data_type)
    if not specification.integer or not isinstance(text, str):
        raise ValueError("Integer type required")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError("Integral value required") from error
    if (not value.is_finite() or not specification.minimum <= value <= specification.maximum
            or value != value.to_integral_value()):
        raise ValueError(f"Integral value within {data_type} range required")
    return int(value)


def encode_value(value, data_type="float32", order=None):
    """将一个有限数值编码为完整测点；整数不隐式取整或钳位。"""
    specification = numeric_type(data_type)
    order = resolve_order(data_type, order)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Numeric value required")
    if not specification.minimum <= value <= specification.maximum:
        raise ValueError(f"Value is outside finite {data_type} range")
    if specification.integer:
        if value != int(value):
            raise ValueError(f"An integral value is required for {data_type}")
        value = int(value)
    try:
        raw = struct.pack(">" + specification.code, value)
    except (OverflowError, struct.error) as error:
        raise ValueError(f"Value is outside finite {data_type} range") from error
    payload = bytes(raw[index] for index in ORDERS[order])
    return struct.unpack(">" + "H" * specification.registers, payload)


def decode_value(words, data_type="float32", order=None):
    """解码完整测点；部分寄存器及非有限浮点数交由调用方标记为原始数据。"""
    specification = numeric_type(data_type)
    order = resolve_order(data_type, order)
    if len(words) != specification.registers:
        raise ValueError("Incomplete numeric point")
    if any(type(word) is not int or not 0 <= word <= 65535 for word in words):
        raise ValueError("Invalid register value")
    payload = struct.pack(">" + "H" * specification.registers, *words)
    indices = ORDERS[order]
    raw = bytes(payload[indices.index(index)] for index in range(len(indices)))
    value = struct.unpack(">" + specification.code, raw)[0]
    if not specification.integer and not math.isfinite(value):
        raise ValueError("Nonfinite numeric value")
    return value
