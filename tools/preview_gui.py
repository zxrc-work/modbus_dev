"""渲染三类通信模式的设置页与四测点编辑区，不打开通信端口。"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("results/gui-preview"))
    parser.add_argument("--native", action="store_true", help="use the real Windows font and DPI backend")
    parser.add_argument("--scale", type=float, help="Qt scale multiplier; native display scale still applies")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--language", choices=("system", "zh_CN", "en"), default="zh_CN")
    args = parser.parse_args()
    if args.native:
        if sys.platform != "win32":
            parser.error("--native requires Windows")
        os.environ["QT_QPA_PLATFORM"] = "windows:fontengine=gdi"
    else:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.scale is not None:
        if args.scale <= 0:
            parser.error("--scale must be positive")
        os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFontDatabase
    from modbus_dev.domain.config import TaskConfig
    from modbus_dev.gui import Workbench, create_application
    from modbus_dev.gui_theme import ui_font

    app = create_application(language=args.language)
    # The offscreen plugin on Windows does not enumerate the installed fonts.
    if not args.native and sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "msyh.ttc"):
            path = fonts / filename
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        app.setFont(ui_font())
    window = Workbench()
    window.setAttribute(Qt.WA_DontShowOnScreen)
    if args.width or args.height:
        window.resize(args.width or window.width(), args.height or window.height())
    window.show()
    app.processEvents()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(platform=app.platformName(), platform_setting=os.environ["QT_QPA_PLATFORM"],
                  dpr=window.devicePixelRatioF(), language=app.language_manager.locale,
                  logical_size=[window.width(), window.height()],
                  available_size=[window.screen().availableGeometry().width(),
                                  window.screen().availableGeometry().height()],
                  fonts=app.font().families(), hinting=app.font().hintingPreference().name,
                  log_hinting=window.logs.font().hintingPreference().name, screenshots=[])
    if args.native:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        report["per_monitor_v2"] = bool(user32.AreDpiAwarenessContextsEqual(
            user32.GetWindowDpiAwarenessContext(int(window.winId())), ctypes.c_void_p(-4)))
    for mode in ("tcp-slave", "rtu-slave", "rtu-master"):
        window.apply_config(TaskConfig.from_mapping(dict(
            mode=mode, serial=dict(port="COM11"), duration_s=0,
            points=[dict(address=0, name="温度", source=dict(value=12.5)),
                    dict(address=2, name="压力", source=dict(kind="sine", value=10, amplitude=2, frequency_hz=.5)),
                    dict(address=4, name="流量", source=dict(value=-3.5)),
                    dict(enabled=False, address=6, name="备用")])))
        for tab in range(window.config_tabs.count()):
            window.config_tabs.setCurrentIndex(tab)
            app.processEvents()
            rendered = window.grab()
            path = args.output_dir / f"{mode}-{tab}.png"
            if not rendered.save(str(path)):
                raise RuntimeError(f"Cannot save {path}")
            report["screenshots"].append(dict(file=path.name, pixel_size=[rendered.width(), rendered.height()],
                                               dpr=rendered.devicePixelRatio()))
            if tab == 1:
                # 逐个滚动到每个配置区，额外保留下半部证据，避免只检查首屏。
                scroll = window.config_tabs.widget(tab)
                for editor in window.point_editors:
                    scroll.ensureWidgetVisible(editor)
                    app.processEvents()
                    window.grab().save(str(args.output_dir / f"{mode}-point-{editor.index + 1}.png"))
                scroll.verticalScrollBar().setValue(0)
    window.config_tabs.setCurrentIndex(0)
    window.fields["mode"].showPopup()
    app.processEvents()
    window.fields["mode"].view().window().grab().save(str(args.output_dir / "mode-popup.png"))
    window.fields["mode"].hidePopup()
    report["control_heights"] = {name: widget.height() for name, widget in {
        "start": window.start_button, "stop": window.stop_button, "output": window.output,
        "mode": window.fields["mode"], "unit": window.fields["unit"],
    }.items()}
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
