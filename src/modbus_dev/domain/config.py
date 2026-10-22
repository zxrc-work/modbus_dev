"""Versioned configuration. CLI and future GUI use the same validation."""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import struct

from .events import ConfigurationError


def fail(path, message):
    raise ConfigurationError(path, message)


def number(value, path, low, high, integer=False):
    expected = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        fail(path, "must be an integer" if integer else "must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        fail(path, f"must be finite and in {low}..{high}")


def choice(value, path, choices):
    if value not in choices:
        fail(path, "must be one of " + ", ".join(map(str, choices)))


def text(value, path):
    if not isinstance(value, str) or not value.strip():
        fail(path, "must be a nonempty string")


def section(data, path, allowed):
    if not isinstance(data, dict):
        fail(path, "must be an object")
    unknown = set(data) - set(allowed)
    if unknown:
        fail(path, "unknown fields: " + ", ".join(sorted(unknown)))
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
        if not isinstance(self.rts_toggle, bool):
            fail("serial.rts_toggle", "must be a boolean")


@dataclass(frozen=True)
class SourceConfig:
    kind: str = "constant"
    value: float = 0.0
    amplitude: float = 0.5
    frequency_hz: float = 0.8
    path: str = ""
    channels: tuple[str, ...] = ()

    def validate(self, period_ms, points):
        choice(self.kind, "source.kind", ("constant", "sine", "dyn"))
        number(self.value, "source.value", -3.4028234e38, 3.4028234e38)
        number(self.amplitude, "source.amplitude", 0, 3.4028234e38)
        number(self.frequency_hz, "source.frequency_hz", 0, 1e6)
        if self.kind == "sine" and not 0 < self.frequency_hz < 500 / period_ms:
            fail("source.frequency_hz", "must be positive and below output Nyquist frequency")
        if self.kind in ("constant", "sine"):
            amplitude = self.amplitude if self.kind == "sine" else 0
            try:
                for value in (self.value - amplitude, self.value + amplitude):
                    if not math.isfinite(struct.unpack(">f", struct.pack(">f", value))[0]):
                        raise OverflowError()
            except (OverflowError, struct.error):
                fail("source", "signal exceeds finite float32 range")
        if self.kind == "dyn":
            text(self.path, "source.path")
            if len(self.channels) != points or len(set(self.channels)) != points:
                fail("source.channels", "provide one unique channel per point")
            choice(period_ms, "period_ms for DYN", (100, 200, 250, 500, 1000))
        for channel in self.channels:
            text(channel, "source.channels")


@dataclass(frozen=True)
class TaskConfig:
    schema_version: int = 1
    mode: str = "rtu-master"
    policy: str = "continuous"
    unit: int = 1
    addresses: tuple[int, ...] = (0,)
    byte_order: str = "B1234"
    read_function: int = 3
    readback_every: int = 0
    period_ms: int = 100
    schedule_tolerance_percent: float = 10.0
    timeout_ms: int = 80
    duration_s: float = 120.0
    serial: SerialConfig = field(default_factory=SerialConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    host: str = "127.0.0.1"
    port: int = 1502

    @classmethod
    def from_mapping(cls, data):
        data = dict(section(data, "config", cls.__dataclass_fields__))
        serial = section(data.pop("serial", {}), "serial", SerialConfig.__dataclass_fields__)
        source = dict(section(data.pop("source", {}), "source", SourceConfig.__dataclass_fields__))
        channels = source.get("channels", [])
        if not isinstance(channels, (list, tuple)) or any(not isinstance(c, str) for c in channels):
            fail("source.channels", "must be an array of strings")
        source["channels"] = tuple(channels)
        addresses = data.get("addresses", [0])
        if not isinstance(addresses, (list, tuple)):
            fail("addresses", "must be an array")
        data["addresses"] = tuple(addresses)
        result = cls(**data, serial=SerialConfig(**serial), source=SourceConfig(**source))
        result.validate()
        return result

    def validate(self):
        number(self.schema_version, "schema_version", 1, 1, True)
        choice(self.mode, "mode", ("rtu-master", "rtu-slave", "tcp-slave"))
        choice(self.policy, "policy", ("continuous", "strict"))
        number(self.unit, "unit", 1, 247, True)
        number(self.period_ms, "period_ms", 1, 60000, True)
        number(self.schedule_tolerance_percent, "schedule_tolerance_percent", 0, 100)
        number(self.timeout_ms, "timeout_ms", 1, 60000, True)
        number(self.duration_s, "duration_s", 0, 86400 * 365)
        number(self.readback_every, "readback_every", 0, 1000000, True)
        number(self.read_function, "read_function", 3, 4, True)
        choice(self.byte_order, "byte_order", ("B1234", "B4321", "B2143", "B3412"))
        if not 1 <= len(self.addresses) <= 4:
            fail("addresses", "first release supports 1..4 float32 points")
        for address in self.addresses:
            number(address, "addresses", 0, 65534, True)
        ordered = sorted(self.addresses)
        if any(b - a < 2 for a, b in zip(ordered, ordered[1:])):
            fail("addresses", "float32 points must not overlap")
        if self.mode.startswith("rtu"):
            self.serial.validate()
        text(self.host, "host")
        number(self.port, "port", 1, 65535, True)
        self.source.validate(self.period_ms, len(self.addresses))
        if self.mode != "rtu-master" and self.readback_every:
            fail("readback_every", "readback is only supported in RTU master mode")
        if self.mode == "rtu-master":
            from .wire import minimum_cycle_ms
            minimum = minimum_cycle_ms(self)
            if not minimum < self.timeout_ms < self.period_ms - self.serial.gap_ms:
                fail("timeout_ms", f"must exceed {minimum:.3f} ms transaction budget and leave "
                     f"{self.serial.gap_ms:.3f} ms silence before the next slot")

    def to_mapping(self):
        return asdict(self)

    @property
    def schedule_tolerance_ms(self):
        return self.period_ms * self.schedule_tolerance_percent / 100


def load_config(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("config", "duplicate field: " + key)
            result[key] = value
        return result
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(raw, object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as error:
        fail("config", f"invalid JSON at line {error.lineno}, column {error.colno}")
    return TaskConfig.from_mapping(data)
