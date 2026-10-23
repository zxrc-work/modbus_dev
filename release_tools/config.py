"""发布配置、原生目标和构建环境校验。"""
from dataclasses import dataclass
import ast
import hashlib
import importlib.metadata
import platform
from pathlib import Path
import struct
import subprocess
import sys
import tomllib


TARGETS = {
    "macos-arm64": ("darwin", "arm64"),
    "windows-x64": ("win32", "x86_64"),
    "kylin-x86_64": ("linux", "x86_64"),
}


def architecture():
    name = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(name, name)


@dataclass(frozen=True)
class ReleaseConfig:
    root: Path
    target: str
    application: str
    package: str
    version: str
    cli_executable: str
    gui_executable: str
    bundle_identifier: str
    resources: tuple[str, ...]

    @classmethod
    def load(cls, root, target):
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        values = dict(metadata["tool"]["modbus-dev"]["release"])
        tree = ast.parse((root / "src" / values["package"] / "__init__.py").read_text(encoding="utf-8"))
        versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(name, ast.Name) and name.id == "__version__" for name in node.targets)]
        if versions != [metadata["project"]["version"]]:
            raise RuntimeError("pyproject.toml 与应用 __version__ 版本号不一致")
        extension = ".exe" if TARGETS[target][0] == "win32" else ""
        for name in ("cli_executable", "gui_executable"):
            values[name] += extension
        values["resources"] = tuple(values["resources"])
        return cls(root.resolve(), target, version=metadata["project"]["version"], **values)

    def require_native_target(self):
        actual = sys.platform, architecture()
        if actual != TARGETS[self.target] or struct.calcsize("P") != 8:
            raise RuntimeError(f"必须在对应的 64 位原生环境构建：目标 {TARGETS[self.target]}，当前 {actual}")

    def executables(self, bundle):
        directory = bundle / "Contents/MacOS" if self.target.startswith("macos-") else bundle
        return directory / self.cli_executable, directory / self.gui_executable


def inspect_environment(config):
    """构建依赖由项目声明；实际解析结果额外完整记录，不借用全局环境。"""
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("请使用独立构建虚拟环境，不要在全局 Python 中构建")
    from packaging.requirements import Requirement
    metadata = tomllib.loads((config.root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = metadata["project"]["dependencies"] + metadata["project"]["optional-dependencies"]["desktop-build"]
    for requirement in requirements:
        name, version = requirement.split("==")
        actual = importlib.metadata.version(name)
        if actual != version:
            raise RuntimeError(f"依赖版本不匹配：{name}=={version}，当前 {actual}")
    for line in (config.root / "requirements/release.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        actual = importlib.metadata.version(requirement.name)
        if actual not in requirement.specifier:
            raise RuntimeError(f"构建锁定版本不匹配：{requirement}，当前 {actual}")
    return {item.metadata["Name"]: item.version for item in importlib.metadata.distributions()}


def source_identity(root):
    """未提交的发布实现同样参与摘要，避免仅靠旧提交号混淆实际构建输入。"""
    paths = [root / "pyproject.toml", root / "build_release.py"]
    for directory in ("src/modbus_dev", "release_tools", "requirements", "examples", "tests"):
        paths.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    paths.extend([root / "docs/portable-quickstart.md"])
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0" + path.read_bytes())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
    return dict(commit=commit, dirty=dirty, source_sha256=digest.hexdigest())
