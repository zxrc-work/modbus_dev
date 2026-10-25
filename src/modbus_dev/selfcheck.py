"""发布包的内置诊断；只使用回环 TCP，不访问已有串口或真实寄存器。"""
import importlib
import importlib.machinery
import json
from pathlib import Path
import socket
import struct
import sys
import time

from .runtime import frozen, release_info, worker_command


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def native_check(output):
    """检查实际加载位置，拒绝从源码目录或全局环境借用业务模块。"""
    require(frozen(), "self-check 只用于实际发布程序")
    headless = not any(name.startswith("PySide6.Qt") for name in sys.modules)
    metadata = release_info()
    root = Path(sys._MEIPASS).resolve()
    modules = {}
    for name in metadata["compiled_modules"]:
        path = Path(importlib.import_module(name).__file__).resolve()
        require(path.is_relative_to(root), f"业务模块不在包内：{name}")
        require(any(str(path).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES),
                f"业务模块不是本机扩展：{name}")
        modules[name] = str(path.relative_to(root))
    require(headless, "CLI 在运行诊断前已主动加载 Qt")
    save(output, dict(status="passed", frozen=True, target=metadata["target"],
                      source_sha256=metadata["source_sha256"], headless_cli=True, modules=modules))
    return 0


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def gui_check(output):
    """真实 GUI 经实际 CLI 工作进程完成通信、停止、崩溃和恢复闭环。"""
    from PySide6.QtCore import QProcess
    from .domain.config import TaskConfig
    from .gui import Workbench, create_application

    require(frozen(), "GUI 自检必须运行实际发布入口")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    app = create_application(language="zh_CN")
    window = Workbench()
    window.output.setText(str(output.parent / output.stem))
    cases = []

    def wait_for(predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        raise TimeoutError("发布版 Qt 生命周期超时")

    def configure(duration=0, port=None):
        window.apply_config(TaskConfig.from_mapping(dict(mode="tcp-slave", duration_s=duration,
            port=port or available_port(), source=dict(kind="constant", value=12.5))))

    def begin():
        window.start()
        wait_for(lambda: window.status.text() == "运行中")
        require(Path(window.process.program()).name == release_info()["cli_executable"],
                "GUI 未启动包内独立 CLI 入口")

    def ended(expected):
        wait_for(lambda: window.temporary is None)
        require(window.result is not None and window.result["exit_code"] == expected,
                f"GUI 会话结果不符：{window.result}")
        summary = json.loads((window.last_output / "summary.json").read_text(encoding="utf-8"))
        require(summary["evidence_complete"], "正常 GUI 会话日志证据不完整")
        require(window.start_button.isEnabled(), "工作进程退出后未恢复编辑")

    try:
        configure()
        window.show()
        app.processEvents()
        require(window.grab().save(str(output.with_suffix(".png"))), "GUI 截图保存失败")
        begin()
        # 客户端直接构造 Modbus 字节，不调用业务编码器自证通信。
        with socket.create_connection(("127.0.0.1", int(window.text("port"))), timeout=2) as peer:
            peer.sendall(struct.pack(">HHHBBHH", 7, 0, 6, 1, 3, 0, 2))
            response = bytearray()
            while len(response) < 13:
                part = peer.recv(13 - len(response))
                require(bool(part), "GUI TCP 从站提前断开")
                response.extend(part)
            require(bytes(response) == struct.pack(">HHHBBBf", 7, 0, 7, 1, 3, 4, 12.5), "GUI TCP 读值错误")
        window.grab().save(str(output.with_name(output.stem + "-running.png")))
        window.stop()
        ended(130)
        cases.append("tcp-read-and-graceful-stop")
        port = int(window.text("port"))
        configure(.4, port)
        window.start()
        ended(0)
        cases.append("same-port-restart-and-natural-end")
        configure(port=port)
        begin()
        window.process.kill()
        wait_for(lambda: window.temporary is None)
        marker = json.loads((window.last_output / "gui-worker-error.json").read_text(encoding="utf-8"))
        require(marker["evidence_complete"] is False and window.result is None, "GUI 伪造了崩溃结果")
        require("异常结束" in window.status.text(), "GUI 未显示崩溃状态")
        configure(.4, port)
        window.start()
        ended(0)
        cases.append("worker-crash-and-recovery")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            reserved.listen()
            configure(.4, reserved.getsockname()[1])
            window.start()
            ended(1)
        configure(.4)
        window.start()
        ended(0)
        cases.append("occupied-port-and-recovery")
        configure()
        begin()
        window.close()
        ended(130)
        require(not window.isVisible(), "GUI 关闭未完成")
        cases.append("close-stops-worker")
        display = dict(platform=app.platformName(), dpr=window.devicePixelRatioF(),
                       font=window.font().family(), logical_size=[window.width(), window.height()])
        if sys.platform == "win32" and app.platformName() == "windows":
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
            user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            context = user32.GetWindowDpiAwarenessContext(int(window.winId()))
            display["per_monitor_v2"] = bool(user32.AreDpiAwarenessContextsEqual(context, ctypes.c_void_p(-4)))
            require(display["per_monitor_v2"], "Windows 实包未启用 Per-Monitor V2")
        save(output, dict(status="passed", frozen=True, cases=cases, display=display,
                          worker_executable=worker_command("config", "output")[0]))
        return 0
    finally:
        # 自检失败也要关闭子进程，不让测试服务遗留在后台。
        if window.active():
            window.process.kill()
            window.process.waitForFinished(3000)
            app.processEvents()
        window.close()
        window.deleteLater()
        app.processEvents()
