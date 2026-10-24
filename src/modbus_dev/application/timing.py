"""Absolute-slot scheduling policy for fixed-cadence communication.

No sleep, I/O, logging, GUI or implicit clock. Adapters own cancellable waiting.
"""
from dataclasses import dataclass
import math


SCHEDULE_LATE_BASIS = "cycle_first_send_interval"


@dataclass(frozen=True)
class SlotDecision:
    slot: int
    scheduled_s: float
    deadline_s: float
    skipped: int


class ScheduleOverrun(RuntimeError):
    code = "schedule_overrun"


class AbsoluteSchedule:
    def __init__(self, origin, period_s, timeout_s, policy="continuous"):
        if not all(math.isfinite(v) for v in (origin, period_s, timeout_s)):
            raise ValueError("Schedule times must be finite")
        if not 0 < timeout_s < period_s or policy not in ("continuous", "strict"):
            raise ValueError("Invalid schedule budget or policy")
        self.origin, self.period, self.timeout, self.policy = origin, period_s, timeout_s, policy

    def at(self, slot, now):
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0 or not math.isfinite(now):
            raise ValueError("Invalid slot or current time")
        scheduled = self.origin + slot * self.period
        skipped = 0
        if now - scheduled >= self.period:
            if self.policy == "strict":
                raise ScheduleOverrun("Replay fell behind one whole source period")
            next_slot = math.floor((now - self.origin) / self.period) + 1
            skipped, slot = next_slot - slot, next_slot
            scheduled = self.origin + slot * self.period
        return SlotDecision(slot, scheduled, scheduled + self.timeout, skipped)

    @staticmethod
    def can_start(now, quiet_until, deadline, minimum_transaction_s):
        return now >= quiet_until and now + minimum_transaction_s < deadline
