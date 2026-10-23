"""无控制台桌面入口：正常启动与内置自检分流，启动失败保留诊断。"""
import argparse
import json
from pathlib import Path
import traceback

from .runtime import user_data_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Modbus Dev 通信工作台")
    parser.add_argument("--self-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            from .selfcheck import gui_check
            return gui_check(args.self_test.resolve())
        from .gui import main as run_gui
        return run_gui()
    except Exception:
        detail = traceback.format_exc()
        log = user_data_directory() / "logs/startup-error.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        if args.self_test:
            args.self_test.parent.mkdir(parents=True, exist_ok=True)
            args.self_test.write_text(json.dumps({"status": "failed", "error": detail}), encoding="utf-8")
        else:
            from .gui import create_application
            from PySide6.QtWidgets import QMessageBox
            app = create_application()
            QMessageBox.critical(None, "启动失败", f"启动诊断：{log}\n{detail.splitlines()[-1]}")
        return 1
