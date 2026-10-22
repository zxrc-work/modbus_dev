from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Event:
    code: str
    message: str
    level: str = "INFO"
    phase: str = "runtime"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Invalid event level: " + self.level)
        if not self.code or not self.message:
            raise ValueError("Events require code and message")


class ConfigurationError(ValueError):
    code = "configuration_invalid"

    def __init__(self, path, message):
        self.path = path
        super().__init__(f"{path}: {message}")
