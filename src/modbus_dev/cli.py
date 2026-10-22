"""Unified configuration, port discovery and single-session communication CLI."""
import argparse
import json
import sys
import signal
import threading

from . import __version__
from .domain.config import load_config
from .domain.events import ConfigurationError
from .domain.wire import minimum_cycle_ms


def main(argv=None):
    parser = argparse.ArgumentParser(prog="modbus-dev", description="Modbus Dev communication tools")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate task configuration without opening a device")
    validate.add_argument("config")
    run = commands.add_parser("run", help="run one RTU master or read-only RTU/TCP slave session (constant/sine)")
    run.add_argument("config")
    run.add_argument("--output", required=True, help="new result directory; existing directories are refused")
    commands.add_parser("ports", help="list serial ports without opening them")
    args = parser.parse_args(argv)
    try:
        if args.command == "ports":
            from serial.tools.list_ports import comports
            print(json.dumps([dict(port=p.device, description=p.description, hwid=p.hwid) for p in comports()],
                             ensure_ascii=False))
            return 0
        config = load_config(args.config)
        if args.command == "run":
            from .application.session import SessionRunner
            stop = threading.Event()
            previous = {}
            runner = SessionRunner(config, args.output, stop=stop)
            try:
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        previous[sig] = signal.signal(sig, lambda *_: stop.set())
                result = runner.run()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print(json.dumps(result, ensure_ascii=False))
            return result["exit_code"]
        result = dict(valid=True, schema_version=config.schema_version, mode=config.mode,
                      period_ms=config.period_ms, configuration_only=True,
                      schedule_tolerance_percent=config.schedule_tolerance_percent,
                      schedule_tolerance_ms=config.schedule_tolerance_ms)
        if config.mode == "rtu-master":
            minimum = minimum_cycle_ms(config)
            result.update(minimum_cycle_ms=minimum, remaining_processing_budget_ms=config.timeout_ms - minimum,
                          gap_ms=config.serial.gap_ms)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ConfigurationError, OSError, ValueError, ImportError) as error:
        print(json.dumps(dict(valid=False, code=getattr(error, "code", "configuration_read_failed"),
                              message=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2
