"""CLI 与 GUI 共用的单会话入口，按启用测点执行独立采样与分组通信。"""
from collections import Counter
from pathlib import Path
import threading
import traceback

from ..domain.events import Event
from ..domain.operations import cycle_requests
from ..domain.transactions import StopRequested
from ..sources.synthetic import SyntheticSource
from ..reporting.journal import Journal, LogWriteError
from .control import Control
from .metrics import Metric
from .timing import AbsoluteSchedule
from .live import LiveValues

_ACTIVE_SESSION = threading.Lock()


def check_capabilities(config):
    config.validate()
    if config.usage.source and any(point.source.kind not in ("constant", "sine") for point in config.enabled_points):
        raise ValueError("This build runs constant/sine sources only; offline sources are deferred until after GUI")


class SessionRunner:
    def __init__(self, config, directory, *, stop=None, control=None,
                 engine_factory=None, journal_factory=Journal):
        check_capabilities(config)
        self.config, self.directory = config, Path(directory)
        self.control = control or Control(stop)
        self.engine_factory, self.journal_factory = engine_factory, journal_factory
        self.state = "idle"
        self._used = False
        self.counters = Counter()
        self.metrics = {name: Metric() for name in ("send_interval_ms", "send_lateness_ms", "response_ms",
                                                   "cycle_interval_ms", "cycle_lateness_ms")}
        self.previous_send = None
        self.previous_cycle_send = None
        self.last_sent_slot = None
        self.journal = self.engine = None
        self.current_slot = None
        self.current_scheduled = None
        self.current_request = None
        self.live = LiveValues(config)

    def _emit(self, code, message, level="INFO", phase="runtime", **details):
        self.journal.emit(Event(code, message, level, phase, details))

    def _state(self, state):
        if state != self.state:
            previous, self.state = self.state, state
            self._emit("state_changed", f"{previous} -> {state}", previous=previous, state=state)

    def _health(self):
        if self.journal.failed.is_set():
            raise LogWriteError(self.journal.snapshot()["log_error"] or "Recording failed")

    def _engine_event(self, event):
        details = dict(event.details)
        details.update(slot=self.current_slot, port=self.config.serial.port, unit=self.config.unit)
        if event.code == "tx":
            request = self.current_request
            packet = details.get("hex", "")
            if request and request.function in (6, 16) and details.get("bytes_written") == len(packet) // 2:
                self._value_event(request, request.registers, "tx", details["sent_s"], "submitted")
            sent = details["sent_s"]
            self.counters["write_attempts"] += 1
            if self.previous_send is not None:
                self.metrics["send_interval_ms"].add((sent - self.previous_send) * 1000)
            self.previous_send = sent
            if self.current_scheduled is not None:
                self.metrics["send_lateness_ms"].add((sent - self.current_scheduled) * 1000)
            if self.current_slot != self.last_sent_slot:
                lateness_ms = (sent - self.current_scheduled) * 1000
                # Compare absolute times; 1 ns slack avoids floating point noise
                # at the inclusive tolerance boundary, not a runtime grace period.
                if sent > self.current_scheduled + self.config.schedule_tolerance_ms / 1000 + 1e-9:
                    self.counters["schedule_late"] += 1
                    self._emit("schedule_late", "Cycle first send exceeded scheduling tolerance",
                               "WARNING", "scheduling", slot=self.current_slot,
                               scheduled_s=self.current_scheduled, sent_s=sent,
                               lateness_ms=lateness_ms,
                               tolerance_ms=self.config.schedule_tolerance_ms,
                               tolerance_percent=self.config.schedule_tolerance_percent)
                if self.previous_cycle_send is not None:
                    self.metrics["cycle_interval_ms"].add((sent - self.previous_cycle_send) * 1000)
                self.metrics["cycle_lateness_ms"].add((sent - self.current_scheduled) * 1000)
                self.previous_cycle_send, self.last_sent_slot = sent, self.current_slot
        self.journal.emit(Event(event.code, event.message, event.level, event.phase, details))

    def _transaction(self, request, deadline, slot, scheduled):
        self.current_request = request
        reply = self.engine.exchange(request, deadline)
        self.current_request = None
        if reply.status == "read":
            quality = "ambiguous" if reply.ambiguous else "received"
            if request.registers and reply.registers != request.registers:
                quality = "readback-mismatch"
            self._value_event(request, reply.registers, "rx", reply.received_s,
                              quality)
        elif request.function in (6, 16):
            quality = reply.status
            if reply.status == "ack":
                quality = "ambiguous" if reply.ambiguous else "confirmed"
            event = Event("register_confirmation", "写入应答状态", "DEBUG", "communication",
                          dict(area="holding", direction="tx", address=request.address, count=request.count,
                               quality=quality))
            self.live.consume(event)
        status = reply.status + ("_ambiguous" if reply.ambiguous and reply.status in ("ack", "read", "exception") else "")
        self.counters[status] += 1
        if reply.received_s is not None and reply.sent_s is not None:
            self.metrics["response_ms"].add((reply.received_s - reply.sent_s) * 1000)
        level = "INFO" if status in ("ack", "read") else "WARNING"
        self._emit("transaction", status, level, "communication", slot=slot, status=status,
                   port=self.config.serial.port, unit=self.config.unit, function=request.function,
                   address=request.address, count=request.count, sent_s=reply.sent_s,
                   received_s=reply.received_s, deadline_s=deadline, timeout_ms=self.config.timeout_ms,
                   ambiguous=reply.ambiguous, exception_code=reply.exception_code, detail=reply.detail)
        return reply

    def _value_event(self, request, registers, direction, sampled_s, quality):
        event = Event("register_values", "实际寄存器数值", "DEBUG", "communication",
                      dict(area="input" if request.function == 4 else "holding", direction=direction,
                           address=request.address, registers=registers, function=request.function,
                           sampled_s=self.control.now() if sampled_s is None else sampled_s, quality=quality))
        self.live.consume(event)
        self.journal.emit(event)

    def run(self):
        if self.config.mode in ("tcp-slave", "rtu-slave"):
            from .slave import run_slave
            return run_slave(self)
        if self._used or not _ACTIVE_SESSION.acquire(blocking=False):
            raise RuntimeError("Only one session can run in this process; use a new runner after completion")
        self._used = True
        result = None
        old_health = self.control.health_check
        try:
            # Resolve dependencies before creating output or acquiring hardware.
            if self.engine_factory is None:
                from ..engines.rtu import RtuEngine
                self.engine_factory = RtuEngine
            from ..engines.platform import WindowsTimer
            usage = self.config.usage
            source = SyntheticSource(self.config) if usage.source else None
            self.journal = self.journal_factory(self.directory, self.config.to_mapping())
            self.control.health_check = self._health
            started = self.control.now()
            reason, error = "duration_complete", None
            try:
                self._state("starting")
                self.engine = self.engine_factory(self.config, self.control, self._engine_event)
                with WindowsTimer() as timer:
                    self._emit("session_started", "RTU master starting", timer=timer.status,
                               engine="pymodbus-codec/pyserial-deadline", policy=self.config.policy,
                               schedule_tolerance_percent=self.config.schedule_tolerance_percent,
                               schedule_tolerance_ms=self.config.schedule_tolerance_ms)
                    reconnect_at = started
                    try:
                        self.engine.connect()
                        self.counters["port_opens"] += 1
                        self._state("running")
                    except (OSError, ValueError) as exc:
                        self.counters["open_errors"] += 1
                        self._emit("port_open_failed", str(exc), "ERROR", "connect",
                                   port=self.config.serial.port, traceback=traceback.format_exc(), retry_s=1)
                        if self.config.policy == "strict":
                            raise
                        reconnect_at = self.control.now() + 1
                        self._state("reconnecting")
                    # Establish the request schedule after initial COM setup.
                    origin = self.control.now() + .1
                    schedule = AbsoluteSchedule(origin, self.config.period_ms / 1000,
                                                self.config.timeout_ms / 1000, self.config.policy)
                    slot = 0
                    while not self.config.duration_s or slot * schedule.period < self.config.duration_s:
                        decision = schedule.at(slot, self.control.now())
                        if decision.skipped:
                            self.counters["skipped_slots"] += decision.skipped
                            self._emit("schedule_skip", "Skipped expired slots; no catch-up burst", "WARNING",
                                       first_slot=slot, next_slot=decision.slot)
                            slot = decision.slot
                            continue
                        self.control.wait_until(decision.scheduled_s)
                        # Scheduling may overrun while sleeping or checking recording.
                        if self.control.now() - decision.scheduled_s >= schedule.period:
                            continue
                        if self.engine.port is None:
                            if self.control.now() < reconnect_at:
                                self.counters["port_unavailable"] += 1
                                slot += 1
                                continue
                            try:
                                self.engine.connect()
                                self.counters["port_opens"] += 1
                                self._state("running")
                            except (OSError, ValueError) as exc:
                                self.counters["open_errors"] += 1
                                self._emit("port_open_failed", str(exc), "ERROR", "connect",
                                           port=self.config.serial.port, traceback=traceback.format_exc(), retry_s=1)
                                if self.config.policy == "strict":
                                    raise
                                reconnect_at = self.control.now() + 1
                                self._state("reconnecting")
                                slot += 1
                                continue
                        self.control.check()
                        self.current_slot, self.current_scheduled = slot, decision.scheduled_s
                        values = source.sample(slot) if source is not None else ()
                        readback = usage.readback and slot % self.config.readback_every == 0
                        requests = cycle_requests(self.config, values, readback)
                        # Reserve the complete remaining group plan before the first write.
                        minimum = sum(self.engine.minimum_s(r) for r in requests)
                        minimum += (len(requests) - 1) * self.config.serial.gap_ms / 1000
                        if self.control.now() + minimum >= decision.deadline_s:
                            self.counters["schedule_missed"] += 1
                            self._emit("schedule_missed", "Insufficient budget for the complete cycle", "WARNING", slot=slot)
                            if self.config.policy == "strict":
                                raise RuntimeError("Strict replay has insufficient cycle budget")
                            slot += 1
                            continue
                        if source is not None:
                            self._emit("source_sample", "Source values prepared", "DEBUG", "source", slot=slot, values=values)
                        try:
                            for request in requests:
                                reply = self._transaction(request, decision.deadline_s, slot, decision.scheduled_s)
                                if reply.status == "read" and request.registers and reply.registers != request.registers:
                                    self.counters["readback_mismatch"] += 1
                                    self._emit("readback_mismatch", "Readback differs from source registers", "ERROR",
                                               "verification", slot=slot, address=request.address,
                                               expected=request.registers, actual=reply.registers)
                                    if self.config.policy == "strict":
                                        raise RuntimeError("Readback differs from source registers")
                                    break
                                if reply.status not in ("ack", "read") or reply.ambiguous:
                                    if self.config.policy == "strict":
                                        raise RuntimeError(f"Strict replay transaction failed: {reply.status}; ambiguous={reply.ambiguous}")
                                if reply.status not in ("ack", "read"):
                                    # Do not add optional traffic after an unresolved request.
                                    break
                        except (OSError, ValueError) as exc:
                            self.counters["io_errors"] += 1
                            self.engine.mark_uncertain("Serial I/O failed; request or reply may remain in flight")
                            self._emit("serial_io_error", str(exc), "ERROR", "communication",
                                       slot=slot, port=self.config.serial.port, traceback=traceback.format_exc())
                            self.engine.close()
                            if self.config.policy == "strict":
                                raise
                            reconnect_at = self.control.now() + 1
                            self._state("reconnecting")
                        slot += 1
                    self.control.check()
            except (StopRequested, KeyboardInterrupt):
                reason = "user_stopped"
            except Exception as exc:
                reason, error = "failed", f"{type(exc).__name__}: {exc}"
                self._emit(getattr(exc, "code", "session_failed"), str(exc), "ERROR",
                           traceback=traceback.format_exc())
            finally:
                self._state("stopping")
                if self.engine is not None:
                    try:
                        self.engine.close()
                    except Exception as exc:
                        reason, error = "failed", f"Close failed: {exc}"
                        self._emit("port_close_failed", str(exc), "ERROR")
                self._state("failed" if error else "stopped")
                result = dict(state=self.state, stop_reason=reason, error=error,
                              exit_code=1 if error else 130 if reason == "user_stopped" else 0,
                              elapsed_s=self.control.now() - started, counters=dict(self.counters),
                              ack_correlation_uncertain=bool(self.engine and self.engine.uncertain),
                              metrics={name: metric.report() for name, metric in self.metrics.items()})
                try:
                    self.journal.close(run_summary=result)
                except (OSError, LogWriteError) as exc:
                    self.state = "failed"
                    result.update(state="failed", stop_reason="log_write_failed", error=str(exc), exit_code=1)
            return result
        finally:
            self.control.health_check = old_health
            _ACTIVE_SESSION.release()
