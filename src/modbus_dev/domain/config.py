"""版本化配置模型；CLI、GUI 和日志保留策略共用校验。"""
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path

from .events import ConfigurationError
from .usage import ParameterUsage
from .numeric import NUMERIC_TYPES, canonical_order, encode_value, numeric_type, parse_integer, resolve_order

MAX_POINTS = 8
CONFIG_VERSION = 4
MIN_PERIOD_MS = 20
MAX_PERIOD_MS = 60000
# 仅供旧 float32 点表和预留槽位的默认间距使用，实际占用由每点类型决定。
REGISTERS_PER_POINT = NUMERIC_TYPES["float32"].registers
SOURCE_FIELDS = {"constant": ("value",), "sine": ("value", "amplitude", "frequency_hz"),
                 "triangle": ("lower", "upper")}


def fail(path, message, *, reason=None, **parameters):
    raise ConfigurationError(path, message, reason=reason, parameters=parameters)


def number(value, path, low, high, integer=False):
    expected = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        fail(path, "must be an integer" if integer else "must be a number")
    if not low <= value <= high or not math.isfinite(value):
        fail(path, f"must be finite and in {low}..{high}", reason="numeric_range", low=low, high=high)


def choice(value, path, choices):
    if value not in choices:
        fail(path, "must be one of " + ", ".join(map(str, choices)), reason="invalid_choice", choices=", ".join(map(str, choices)))


def text(value, path):
    if not isinstance(value, str) or not value.strip():
        fail(path, "must be a nonempty string")


def section(data, path, allowed):
    if not isinstance(data, dict):
        fail(path, "must be an object")
    unknown = set(data) - set(allowed)
    if unknown:
        fail(path, "unknown fields: " + ", ".join(sorted(unknown)), reason="unknown_fields", fields=", ".join(sorted(unknown)))
    return data


@dataclass(frozen=True)
class SerialConfig:
    port: str = ""
    baud: int = 9600
    parity: str = "N"
    stop_bits: int = 1
    rts_toggle: bool = False

    @property
    def bits_per_character(self):
        return 1 + 8 + int(self.parity != "N") + self.stop_bits

    @property
    def gap_ms(self):
        # Preserve the conservative existing transport policy.
        return max(1.75, 3500 * self.bits_per_character / self.baud)

    def validate(self):
        text(self.port, "serial.port")
        number(self.baud, "serial.baud", 1200, 1000000, True)
        choice(self.parity, "serial.parity", ("N", "E", "O"))
        number(self.stop_bits, "serial.stop_bits", 1, 2, True)
        if ParameterUsage("rtu-master").rts_supported and not isinstance(self.rts_toggle, bool):
            fail("serial.rts_toggle", "must be a boolean")


@dataclass(frozen=True)
class SourceConfig:
    kind: str = "constant"
    value: float = 0.0
    amplitude: float = 0.5
    frequency_hz: float = 0.8
    lower: float = 0.0
    upper: float = 10.0
    path: str = ""
    channels: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data, path="source", *, active=True):
        data = dict(section(data, path, cls.__dataclass_fields__))
        channels = data.get("channels", [])
        if isinstance(channels, (list, tuple)):
            channels = tuple(channels)
        elif active and data.get("kind") == "dyn":
            fail(path + ".channels", "must be an array of strings")
        data["channels"] = channels
        return cls(**data)

    def validate(self, period_ms, points=1, path="source", *, active=True, data_type="float32"):
        if not active:
            return
        choice(self.kind, path + ".kind", ("constant", "sine", "triangle", "dyn"))
        specification = numeric_type(data_type)
        if specification.integer and self.kind not in ("constant", "triangle"):
            fail(path + ".kind", "integer points support constant and triangle sources only", reason="integer_source")
        if self.kind == "triangle":
            # 固定步长为 1，配置只接受上下限；整数边界不能通过浮点舍入变成合法整数。
            for key in ("lower", "upper"):
                value = getattr(self, key)
                number(value, path + "." + key, specification.minimum, specification.maximum)
                try:
                    encode_value(value, data_type)
                except ValueError:
                    fail(path + "." + key, "integer points require integral values", reason="integer_value")
            if self.lower >= self.upper:
                fail(path + ".upper", "upper bound must exceed lower bound", reason="triangle_bounds")
        if self.kind in ("constant", "sine"):
            number(self.value, path + ".value", specification.minimum, specification.maximum)
        if self.kind == "sine":
            number(self.amplitude, path + ".amplitude", 0, specification.maximum)
            number(self.frequency_hz, path + ".frequency_hz", 0, 1e6)
            if not 0 < self.frequency_hz < 500 / period_ms:
                fail(path + ".frequency_hz", "must be positive and below output Nyquist frequency")
        if self.kind in ("constant", "sine"):
            amplitude = self.amplitude if self.kind == "sine" else 0
            try:
                for value in (self.value - amplitude, self.value + amplitude):
                    encode_value(value, data_type)
            except ValueError:
                if specification.integer:
                    fail(path + ".value", "integer points require integral values", reason="integer_value")
                fail(path, "signal exceeds finite float32 range")
        if self.kind == "dyn":
            text(self.path, path + ".path")
            if not isinstance(self.channels, (list, tuple)) or any(not isinstance(c, str) for c in self.channels):
                fail(path + ".channels", "must be an array of strings")
            if len(self.channels) != points or len(set(self.channels)) != points:
                fail(path + ".channels", "provide one unique channel per point")
            choice(period_ms, "period_ms for DYN", (100, 200, 250, 500, 1000))
            for channel in self.channels:
                text(channel, path + ".channels")


@dataclass(frozen=True)
class PointConfig:
    """逐点保存类型、排列、地址和数据源；禁用点不占用通信地址。"""
    enabled: bool = True
    name: str = ""
    address: int = 0
    source: SourceConfig = field(default_factory=SourceConfig)
    data_type: str = "float32"
    byte_order: str | None = None
    area: str = "input"
    operation: str | None = None
    readback_every: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "byte_order", canonical_order(self.byte_order))

    @property
    def register_count(self):
        return numeric_type(self.data_type).registers

    def encoding_order(self, default_order="B1234"):
        return resolve_order(self.data_type, self.byte_order, default_order)

    @classmethod
    def from_mapping(cls, data, path, *, source_used=True):
        data = dict(section(data, path, cls.__dataclass_fields__))
        data["source"] = SourceConfig.from_mapping(data.get("source", {}), path + ".source",
                                                 active=source_used and data.get("enabled", True) is True)
        return cls(**data)

    def validate(self, period_ms, path, *, source_used=True, default_order="B1234"):
        if not isinstance(self.enabled, bool):
            fail(path + ".enabled", "must be a boolean")
        if not self.enabled:
            return
        if not isinstance(self.name, str):
            fail(path + ".name", "must be a string")
        choice(self.data_type, path + ".data_type", tuple(NUMERIC_TYPES))
        if self.byte_order is not None:
            choice(self.byte_order, path + ".byte_order", numeric_type(self.data_type).orders)
        self.encoding_order(default_order)
        number(self.address, path + ".address", 0, 65536 - self.register_count, True)
        self.source.validate(period_ms, path=path + ".source", active=source_used, data_type=self.data_type)


def default_point(index):
    return PointConfig(enabled=index == 0, address=index * REGISTERS_PER_POINT,
                       byte_order="B1234", operation="write-multiple", readback_every=0)


def point_conflicts(points, area_of=lambda point: point.area):
    """返回启用点中重叠的槽位编号，供配置校验和界面即时提示共用。"""
    active = [(i, point) for i, point in enumerate(points) if point.enabled]
    return [(i, j) for n, (i, first) in enumerate(active) for j, second in active[n + 1:]
            if area_of(first) == area_of(second)
            and first.address < second.address + second.register_count
            and second.address < first.address + first.register_count]


@dataclass(frozen=True)
class LoggingConfig:
    max_files: int = 100

    def validate(self):
        # 数量包含当前写入分片，限制元数据规模，避免配置导致无界内存增长。
        number(self.max_files, "logging.max_files", 1, 10000, True)


@dataclass(frozen=True)
class TaskConfig:
    schema_version: int = CONFIG_VERSION
    mode: str = "rtu-master"
    policy: str = "continuous"
    unit: int = 1
    points: tuple[PointConfig, ...] = field(default_factory=lambda: tuple(
        default_point(i) for i in range(MAX_POINTS)))
    byte_order: str = field(default="B1234", compare=False, repr=False)
    access_mode: str = "read-write"
    master_operation: str = field(default="write-multiple", compare=False, repr=False)
    read_function: int = field(default=3, compare=False, repr=False)
    readback_every: int = field(default=0, compare=False, repr=False)
    period_ms: int = 100
    schedule_tolerance_percent: float = 10.0
    timeout_ms: int = 80
    duration_s: float = 120.0
    serial: SerialConfig = field(default_factory=SerialConfig)
    host: str = "127.0.0.1"
    port: int = 1502
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def __post_init__(self):
        # 主站区域由逐点功能码唯一确定，避免导出的区域与实际请求相矛盾。
        if self.mode == "rtu-master":
            object.__setattr__(self, "points", tuple(replace(point, area=(
                "input" if (point.operation or self.master_operation) == "read-input" else "holding"))
                for point in self.points))

    @classmethod
    def from_mapping(cls, data, *, validate=True):
        data = dict(section(data, "config", (*cls.__dataclass_fields__, "addresses", "source")))
        # 旧从站配置继续保持只读权限，不因升级悄悄开放写入。
        if data.get("mode") in ("rtu-slave", "tcp-slave"):
            data.setdefault("access_mode", "read-only")
        if data.get("access_mode") == "read-only":
            data.setdefault("master_operation", "read-input")
        usage = ParameterUsage(data.get("mode", "rtu-master"), data.get("master_operation", "write-multiple"))
        number(data.get("period_ms", 100), "period_ms", MIN_PERIOD_MS, MAX_PERIOD_MS, True)
        from .migration import migrate_points
        points = migrate_points(data, usage)
        points.extend(default_point(i) for i in range(len(points), MAX_POINTS))
        data["points"], data["schema_version"] = tuple(points), CONFIG_VERSION
        serial = section(data.pop("serial", {}), "serial", SerialConfig.__dataclass_fields__)
        logging = section(data.pop("logging", {}), "logging", LoggingConfig.__dataclass_fields__)
        result = cls(**data, serial=SerialConfig(**serial), logging=LoggingConfig(**logging))
        if validate:
            result.validate()
        return result

    def validate(self):
        number(self.schema_version, "schema_version", CONFIG_VERSION, CONFIG_VERSION, True)
        choice(self.mode, "mode", ("rtu-master", "rtu-slave", "tcp-slave"))
        choice(self.policy, "policy", ("continuous", "strict"))
        usage = self.usage
        if usage.serial or self.mode == "tcp-slave":
            choice(self.access_mode, "access_mode", ("read-only", "read-write"))
        number(self.unit, "unit", 1, 247, True)
        number(self.period_ms, "period_ms", MIN_PERIOD_MS, MAX_PERIOD_MS, True)
        if usage.master:
            number(self.schedule_tolerance_percent, "schedule_tolerance_percent", 0, 100)
        number(self.timeout_ms, "timeout_ms", 1, 60000, True)
        number(self.duration_s, "duration_s", 0, 86400 * 365)
        if usage.readback:
            choice(self.read_function, "read_function", (3,))
        if not 1 <= len(self.points) <= MAX_POINTS:
            fail("points", f"provide 1..{MAX_POINTS} point configurations", reason="point_count", maximum=MAX_POINTS)
        for i, point in enumerate(self.points):
            point_usage = self.point_usage(point)
            point.validate(self.period_ms, f"points[{i}]", source_used=point_usage.source, default_order=self.byte_order)
            if not point.enabled:
                continue
            choice(point.area, f"points[{i}].area", ("holding", "input"))
            if usage.master:
                from .operations import OPERATIONS
                choice(point_usage.operation, f"points[{i}].operation", tuple(OPERATIONS))
                if self.access_mode == "read-only" and point_usage.writing:
                    fail(f"points[{i}].operation", "只读主站不能执行写入操作", reason="readonly_write")
                if point_usage.writing:
                    number(self.point_readback(point), f"points[{i}].readback_every", 0, 1000000, True)
                    if point_usage.operation == "write-single" and point.register_count != 1:
                        fail(f"points[{i}].operation", "FC06 requires a 16-bit point; use FC16 for 32-bit values", reason="fc06_width")
            elif point.area == "holding" and point.source.kind != "constant":
                fail(f"points[{i}].source.kind", "holding registers accept an initial constant only", reason="holding_initial")
        if not self.enabled_points:
            fail("points", "请至少启用一个测点", reason="no_enabled_points")
        conflicts = point_conflicts(self.points, self.point_area)
        if conflicts:
            first, second = conflicts[0]
            fail("points", f"测点 {first + 1} 与测点 {second + 1} 的寄存器范围重叠", reason="point_overlap", first=first + 1, second=second + 1)
        if usage.serial:
            self.serial.validate()
        else:
            text(self.host, "host")
            number(self.port, "port", 1, 65535, True)
        self.logging.validate()
        if self.mode == "rtu-master":
            from .wire import cycle_budget
            budget = cycle_budget(self)
            if self.period_ms < budget.minimum_period_ms:
                fail("period_ms", f"minimum cycle is {budget.minimum_period_ms} ms for {budget.transactions} requests "
                     f"including {budget.reserve_ms} ms workflow reserve", reason="cycle_minimum",
                     minimum=budget.minimum_period_ms, requests=budget.transactions, wire=budget.line_ms)
            if not budget.transaction_ms < self.timeout_ms <= self.period_ms - self.serial.gap_ms:
                fail("timeout_ms", f"cycle budget must exceed {budget.transaction_ms:.3f} ms and leave "
                     f"{self.serial.gap_ms:.3f} ms final silence",
                     reason="transaction_budget", minimum=budget.transaction_ms,
                     gap=self.serial.gap_ms)

    def to_mapping(self):
        # 新文件只保存每点的有效设置，不再导出全局操作和继承编码。
        data = asdict(self)
        for key in ("byte_order", "master_operation", "read_function", "readback_every"):
            data.pop(key)
        data["schema_version"] = CONFIG_VERSION
        for point, value in zip(self.points, data["points"]):
            value["byte_order"] = point.encoding_order(self.byte_order) if point.enabled else point.byte_order
            value["operation"] = self.point_usage(point).operation
            value["readback_every"] = self.point_readback(point)
        return data

    def point_usage(self, point):
        return ParameterUsage(self.mode, point.operation or self.master_operation, self.point_readback(point))

    def point_readback(self, point):
        return self.readback_every if point.readback_every is None else point.readback_every

    def point_area(self, point):
        if self.mode == "rtu-master":
            return "input" if self.point_usage(point).operation == "read-input" else "holding"
        return point.area

    @property
    def usage(self):
        usages = [self.point_usage(point) for point in self.enabled_points]
        writing = any(item.writing for item in usages)
        return ParameterUsage(self.mode, "write-multiple" if writing else "read-input",
                              int(any(item.readback for item in usages)))

    @property
    def enabled_points(self):
        return tuple(point for point in self.points if point.enabled)

    @property
    def master_function(self):
        from .operations import OPERATIONS
        return OPERATIONS[self.master_operation]

    @property
    def addresses(self):
        return tuple(point.address for point in self.enabled_points)

    @property
    def schedule_tolerance_ms(self):
        return self.period_ms * self.schedule_tolerance_percent / 100

    @property
    def cycle_interval_limit_ms(self):
        return self.period_ms + self.schedule_tolerance_ms


def load_config(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("config", "duplicate field: " + key, reason="duplicate_field", field=key)
            result[key] = value
        return result
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(raw, object_pairs_hook=unique_pairs, parse_float=Decimal)
    except json.JSONDecodeError as error:
        fail("config", f"invalid JSON at line {error.lineno}, column {error.colno}", reason="invalid_json", line=error.lineno, column=error.colno)
    except InvalidOperation:
        fail("config", "invalid JSON number")

    # 整数源值先按 JSON 中的十进制精确检查；其他字段仍按原来的 float 语义处理。
    if isinstance(data, dict) and isinstance(data.get("points"), list):
        operation = data.get("master_operation", "read-input" if data.get("access_mode") == "read-only" else "write-multiple")
        usage = ParameterUsage(data.get("mode", "rtu-master"), operation)
        for index, point in enumerate(data["points"]):
            if not isinstance(point, dict) or point.get("enabled", True) is not True or not ParameterUsage(data.get("mode", "rtu-master"), point.get("operation") or operation).source:
                continue
            data_type = point.get("data_type", "float32")
            source = point.get("source")
            if (not isinstance(data_type, str) or data_type not in NUMERIC_TYPES
                    or not NUMERIC_TYPES[data_type].integer or not isinstance(source, dict)
                    or source.get("kind", "constant") not in ("constant", "triangle")):
                continue
            keys = ("lower", "upper") if source.get("kind") == "triangle" else ("value",)
            for key in keys:
                if not isinstance(source.get(key), Decimal):
                    continue
                try:
                    source[key] = parse_integer(str(source[key]), data_type)
                except ValueError:
                    fail(f"points[{index}].source.{key}", "integer points require integral values within range", reason="integer_value")

    def plain_numbers(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, list):
            return [plain_numbers(item) for item in value]
        if isinstance(value, dict):
            return {key: plain_numbers(item) for key, item in value.items()}
        return value

    data = plain_numbers(data)
    return TaskConfig.from_mapping(data)
