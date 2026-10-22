"""Library-independent transaction contracts."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    function: int
    address: int
    count: int
    registers: tuple[int, ...] = ()


@dataclass(frozen=True)
class Reply:
    status: str
    registers: tuple[int, ...] = ()
    sent_s: float | None = None
    received_s: float | None = None
    ambiguous: bool = False
    detail: str = ""
    exception_code: int | None = None


class StopRequested(Exception):
    """Cooperative stop; never a protocol failure."""
