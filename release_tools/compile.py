"""在隔离副本编译所有应用模块，保留 Python 的边界、除法和回调语义。"""
import ast
import importlib.machinery
from pathlib import Path
import shutil
import subprocess
import sys


def discover_modules(source, package):
    protected, imports = [], set()
    for path in sorted((source / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.name == "__init__.py" and any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
                                              for node in ast.walk(tree)):
            raise RuntimeError(f"包初始化含业务定义，必须移入受保护模块：{path}")
        if path.name not in {"__init__.py", "__main__.py"}:
            protected.append(".".join(path.relative_to(source).with_suffix("").parts))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imports.add(node.module)
    imports.discard("__future__")
    # 串口后端按平台动态选择，Qt 的插件由官方 hook 收集。
    imports.update(("serial.tools.list_ports", "serial.serialutil", "pymodbus"))
    if sys.platform == "win32":
        imports.update(("serial.serialwin32", "serial.tools.list_ports_windows"))
    else:
        imports.add("serial.serialposix")
        imports.add("serial.tools.list_ports_osx" if sys.platform == "darwin" else "serial.tools.list_ports_linux")
    return protected, sorted(imports | set(protected))


def compile_package(config, stage):
    source, native = stage / "src", stage / "native"
    shutil.copytree(config.root / "src" / config.package, source / config.package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    protected, hidden = discover_modules(source, config.package)
    specs = [(name, str(source / Path(*name.split(".")).with_suffix(".py"))) for name in protected]
    setup = stage / "setup_native.py"
    setup.write_text(
        '"""发布暂存目录中的本机扩展编译入口。"""\n'
        "from setuptools import Extension, setup\nfrom Cython.Build import cythonize\n"
        "from Cython.Compiler import Options\nOptions.docstrings = False\n"
        f"specs = {specs!r}\n"
        "setup(ext_modules=cythonize([Extension(name, [path]) for name, path in specs],\n"
        f" build_dir={str(stage / 'cython')!r},\n"
        " compiler_directives={'language_level': 3, 'annotation_typing': False, 'binding': True,\n"
        " 'boundscheck': True, 'wraparound': True, 'cdivision': False}))\n",
        encoding="utf-8")
    subprocess.run([sys.executable, str(setup), "build_ext", "--build-lib", str(native),
                    "--build-temp", str(stage / "objects"), "--parallel", "4"], cwd=stage, check=True)
    # 初始化文件仅保留包说明和版本；其余源码不得成为编译失败后的后备。
    for path in source.rglob("*.py"):
        if path.name not in {"__init__.py", "__main__.py"}:
            continue
        destination = native / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    translations = native / config.package / "ui/translations"
    translations.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / config.package / "ui/translations/modbus_dev_zh_CN.qm", translations)
    expected = []
    for name in protected:
        base = native / Path(*name.split("."))
        candidates = [Path(str(base) + suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES]
        matches = [path for path in candidates if path.is_file()]
        if len(matches) != 1:
            raise RuntimeError(f"模块未生成唯一的本机扩展：{name}: {matches}")
        expected.append(matches[0].relative_to(native).as_posix())
    return native, protected, hidden, expected
