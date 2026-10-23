"""源码与便携版共用的路径和子进程入口，不依赖 Qt 或开发机 Python。"""
import os
from pathlib import Path
import sys


def frozen():
    return bool(getattr(sys, "frozen", False))


def release_info():
    """资源只从包内读取，不使用当前工作目录寻找可执行文件。"""
    import json
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return json.loads((root / "release/BUILD_INFO.json").read_text(encoding="utf-8"))


def worker_command(config, output):
    arguments = ["worker", str(config), "--output", str(output)]
    if not frozen():
        return sys.executable, ["-m", "modbus_dev", *arguments]
    # GUI 和 CLI 是相邻的两个入口；只有 CLI 使用控制台引导器，保留标准管道。
    executable = adjacent_executable("cli_executable")
    return str(executable), arguments


def adjacent_executable(key):
    """冻结版只从同一构建的入口目录选择程序，不回退到 PATH 或其他安装。"""
    name = release_info()[key]
    executable = Path(sys.executable).resolve().parent / name
    if not executable.is_file():
        raise FileNotFoundError(f"便携包缺少发布入口：{executable}")
    return executable


def user_data_directory():
    """配置和诊断写入用户目录，不能修改应用包或依赖当前目录可写。"""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/ModbusDev"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    if not base.is_absolute():
        base = home / ".local/share"
    return base / "ModbusDev"


def default_output_directory():
    return (user_data_directory() if frozen() else Path.cwd()) / "results"
