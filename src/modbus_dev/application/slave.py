"""从站共用独立数据时钟；TCP 与 RTU 的保持区和输入区分别发布。"""
import traceback

from ..domain.mapping import point_write_requests
from ..domain.transactions import StopRequested
from ..sources.synthetic import SyntheticSource
from ..reporting.journal import LogWriteError


def run_slave(runner):
    from .session import _ACTIVE_SESSION
    if runner.config.mode == "rtu-slave":
        from ..engines.rtu_slave import RtuSlaveEngine as DefaultEngine
    else:
        from ..engines.tcp_slave import TcpSlaveEngine as DefaultEngine

    if runner._used or not _ACTIVE_SESSION.acquire(blocking=False):
        raise RuntimeError("Only one session can run in this process; use a new runner after completion")
    runner._used = True
    old_health = runner.control.health_check
    try:
        runner.journal = runner.journal_factory(runner.directory, runner.config.to_mapping())
        runner.control.health_check = runner._health
        started = runner.control.now()
        reason, error = "duration_complete", None
        runner.metrics = {}
        try:
            runner._state("starting")
            source = SyntheticSource(runner.config)
            def emit(event):
                runner.live.consume(event)
                runner.counters[event.code] += 1
                runner.journal.emit(event)
            factory = runner.engine_factory or DefaultEngine
            runner.engine = factory(runner.config, runner.control, emit)
            runner.control.check()
            is_serial = runner.config.mode == "rtu-slave"
            reconnect_at = None

            def connect():
                try:
                    runner.engine.connect()
                    runner.counters["port_opens" if is_serial else "listener_opens"] += 1
                    runner._state("running")
                    return None
                except OSError as exc:
                    if not is_serial or runner.config.policy == "strict":
                        raise
                    runner.engine.close()
                    runner.counters["open_errors"] += 1
                    runner._emit("port_open_failed", str(exc), "ERROR", port=runner.config.serial.port, retry_s=1)
                    runner._state("reconnecting")
                    return runner.control.now() + 1

            reconnect_at = connect()
            origin = runner.control.now()
            period = runner.config.period_ms / 1000
            end = origin + runner.config.duration_s if runner.config.duration_s else float("inf")
            slot = -1
            runner._emit("session_started", "Server session started", mode=runner.config.mode,
                         address=runner.engine.bound_address)
            while runner.control.now() < end:
                runner.control.check()
                current = int((runner.control.now() - origin) / period)
                if current != slot:
                    if current > slot + 1:
                        missed = current - slot - 1
                        runner.counters["skipped_slots"] += missed
                        if runner.config.policy == "strict":
                            raise RuntimeError("Strict source update missed a complete period")
                        runner._emit("schedule_skip", "Skipped expired source updates", "WARNING", skipped=missed)
                    values = source.sample(current)
                    registers = {}
                    pairs = list(zip(runner.config.enabled_points, values))
                    for area in ("holding", "input"):
                        selected = [(point, value) for point, value in pairs if point.area == area]
                        registers[area] = {request.address + i: word
                            for request in point_write_requests([point for point, _ in selected],
                                [value for _, value in selected], runner.config.byte_order)
                            for i, word in enumerate(request.registers)}
                    runner.engine.publish(registers)
                    runner.counters["source_updates"] += 1
                    runner._emit("source_sample", "Register snapshot published", "DEBUG", "source",
                                 slot=current, source_time_s=current * period, values=values)
                    slot = current
                deadline = min(end, origin + (slot + 1) * period)
                if reconnect_at is not None:
                    if runner.control.now() >= reconnect_at:
                        reconnect_at = connect()
                    else:
                        runner.control.wait_until(min(deadline, reconnect_at))
                    continue
                try:
                    runner.engine.poll(deadline)
                except OSError as exc:
                    if not is_serial or runner.config.policy == "strict":
                        raise
                    runner.counters["io_errors"] += 1
                    runner._emit("serial_io_error", str(exc), "ERROR", port=runner.config.serial.port,
                                 traceback=traceback.format_exc())
                    runner.engine.close()
                    runner._state("reconnecting")
                    reconnect_at = runner.control.now() + 1
            runner.control.check()
        except (StopRequested, KeyboardInterrupt):
            reason = "user_stopped"
        except Exception as exc:
            reason, error = "failed", f"{type(exc).__name__}: {exc}"
            runner._emit(getattr(exc, "code", "session_failed"), str(exc), "ERROR", traceback=traceback.format_exc())
        finally:
            runner._state("stopping")
            if runner.engine is not None:
                try:
                    runner.engine.close()
                except Exception as exc:
                    reason, error = "failed", f"Close failed: {exc}"
            runner._state("failed" if error else "stopped")
            result = dict(state=runner.state, stop_reason=reason, error=error,
                          exit_code=1 if error else 130 if reason == "user_stopped" else 0,
                          elapsed_s=runner.control.now() - started, counters=dict(runner.counters),
                          metrics={})
            try:
                runner.journal.close(run_summary=result)
            except (OSError, LogWriteError) as exc:
                runner.state = "failed"
                result.update(state="failed", stop_reason="log_write_failed", error=str(exc), exit_code=1)
        return result
    finally:
        runner.control.health_check = old_health
        _ACTIVE_SESSION.release()
