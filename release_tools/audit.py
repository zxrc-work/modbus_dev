"""审计最终交付物及两个入口内的字节码归档，不能只检查磁盘散落文件。"""
import hashlib
from pathlib import Path


FORBIDDEN_SUFFIXES = {".py", ".pyc", ".pyo", ".c", ".cpp", ".h", ".his", ".idx", ".dyn"}


def audit_bundle(config, bundle, protected, expected):
    from PyInstaller.archive.readers import CArchiveReader
    cli, gui = config.executables(bundle)
    for executable in (cli, gui):
        if not executable.is_file():
            raise RuntimeError(f"发布入口缺失：{executable}")
        reader = CArchiveReader(str(executable))
        archives = 0
        for name in reader.toc:
            if name.endswith(".pyz"):
                archives += 1
                modules = reader.open_embedded_archive(name).toc
                leaked = set(protected).intersection(modules)
                if leaked:
                    raise RuntimeError(f"归档泄漏受保护模块：{sorted(leaked)}")
        if not archives:
            raise RuntimeError(f"无法检查入口的字节码归档：{executable}")
    files = [path for path in bundle.rglob("*") if path.is_file()]
    for path in files:
        relative = path.relative_to(bundle)
        if config.package in relative.parts and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise RuntimeError(f"应用包含源码或字节码：{relative}")
        if path.name.lower() in {"config.toml", ".env"} or path.suffix.lower() in {".his", ".idx", ".dyn"}:
            raise RuntimeError(f"应用包含非公开配置或历史数据：{relative}")
    for extension in expected:
        suffix = "/" + extension
        if not any(path.as_posix().endswith(suffix) for path in files):
            raise RuntimeError(f"应用缺少编译模块：{extension}")
    return {"entrypoints": [str(path.relative_to(bundle)) for path in (cli, gui)],
            "protected_modules": len(protected), "native_files": len(expected), "status": "passed"}


def write_checksums(root):
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
