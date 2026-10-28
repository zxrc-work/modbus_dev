"""原生构建、源码保护审计、归档解压和实包验收的顺序编排。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

from .audit import audit_bundle, write_checksums
from .bundle import make_bundle
from .compile import compile_package
from .config import ReleaseConfig, TARGETS, inspect_environment, source_identity
from .verify import verify_release
from .translations import compile_catalog

DEFAULT_TEST_TIMEOUT = 600


def positive_seconds(value):
    seconds = int(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("测试超时必须为正整数秒")
    return seconds


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_tests(config, stage, source, name, *, timeout=DEFAULT_TEST_TIMEOUT):
    environment = dict(os.environ, PYTHONPATH=str(source), PYTHONNOUSERSITE="1", QT_QPA_PLATFORM="offscreen")
    log = stage / f"{name}.log"
    print(f"执行 {name}（限时 {timeout} 秒）：{log}", flush=True)
    started = time.monotonic()
    try:
        with log.open("wb") as stream:
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(config.root / "tests"), "-v"],
                           cwd=stage, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)
    finally:
        print(f"{name} 耗时 {time.monotonic() - started:.3f} 秒", flush=True)


def archive_payload(payload, destination, windows):
    # macOS 的 Qt.framework 含符号链接；tar 保留链接及执行权限，不能用普通 ZIP 复制展开。
    if windows:
        return Path(shutil.make_archive(str(destination), "zip", root_dir=payload.parent, base_dir=payload.name))
    archive = Path(str(destination) + ".tar.gz")
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(payload, arcname=payload.name)
    return archive


def unpack_archive(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as stream:
            stream.extractall(destination)
    else:
        with tarfile.open(archive) as stream:
            stream.extractall(destination, filter="data")


def build(config, *, test_timeout=DEFAULT_TEST_TIMEOUT):
    config.require_native_target()
    dependencies = inspect_environment(config)
    compile_catalog(config.root)
    identity = source_identity(config.root)
    work = config.root / "build/releases"
    work.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=config.target + "-", dir=work)).resolve()
    print(f"构建证据目录：{stage}", flush=True)
    run_tests(config, stage, config.root / "src", "source-tests", timeout=test_timeout)
    native, protected, hidden, expected = compile_package(config, stage)
    run_tests(config, stage, native, "compiled-tests", timeout=test_timeout)
    metadata = dict(application=config.application, version=config.version, target=config.target,
        cli_executable=config.cli_executable, gui_executable=config.gui_executable,
        built_at=datetime.now(timezone.utc).isoformat(), built_on=platform.platform(),
        libc=platform.libc_ver(), python=sys.version, dependencies=dependencies, test_timeout_s=test_timeout,
        compiled_modules=protected, native_files=expected,
        protection="Cython native modules; only launchers and package initializers use bytecode",
        signing="ad-hoc; no Developer ID or notarization" if config.target.startswith("macos-") else "unsigned",
        validation="Only the adjacent validation receipt describes executed tests; no other target is implied",
        **identity)
    save(stage / "BUILD_INFO.json", metadata)
    bundle = make_bundle(config, stage, native, protected, hidden)
    audit = audit_bundle(config, bundle, protected, expected)
    name = f"{config.application}-{config.version}-{config.target}-{identity['source_sha256'][:12]}"
    payload = stage / "payload" / name
    payload.mkdir(parents=True)
    # move 保留 framework 的链接布局；资源只复制白名单，不扫描整个仓库。
    shutil.move(str(bundle), str(payload / bundle.name))
    for resource in config.resources:
        source = config.root / resource
        destination = payload / source.name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    shutil.copy2(stage / "BUILD_INFO.json", payload / "BUILD_INFO.json")
    save(payload / "AUDIT.json", audit)
    write_checksums(payload)
    archive = archive_payload(payload, stage / name, config.target.startswith("windows-"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    verification = config.root / "results" / f"release-{config.target}-{stamp}"
    extraction = verification / "解压验收 with spaces"
    unpack_archive(archive, extraction)
    unpacked = extraction / name
    unpacked_bundle = unpacked / (config.application + (".app" if config.target.startswith("macos-") else ""))
    audit_bundle(config, unpacked_bundle, protected, expected)
    receipt = verify_release(config, unpacked_bundle, verification / "checks")
    if source_identity(config.root)["source_sha256"] != identity["source_sha256"]:
        raise RuntimeError("构建期间输入发生变化，请重新构建，避免源码摘要与实际产物不一致")
    # 自检不得写回安装目录；比较包含可执行程序和依赖在内的完整交付清单。
    before = (unpacked / "SHA256SUMS").read_bytes()
    write_checksums(unpacked)
    if before != (unpacked / "SHA256SUMS").read_bytes():
        raise RuntimeError("实际运行修改了便携包内容，拒绝发布")
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt.update(archive=archive.name, archive_sha256=checksum, build=metadata,
                   audit=audit, evidence=str(verification), build_evidence=str(stage))
    save(verification / "validation.json", receipt)
    release = config.root / "dist"
    release.mkdir(exist_ok=True)
    destination = release / archive.name
    if destination.exists():
        raise FileExistsError(f"发布文件已存在，不覆盖旧证据：{destination}")
    # 只有所有验收通过后才进入 dist，失败产物留在独立暂存目录供排查。
    shutil.copy2(archive, destination)
    shutil.copy2(verification / "validation.json", release / (archive.name + ".validation.json"))
    destination.with_name(destination.name + ".sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"验收通过：{destination}\n验收记录：{verification / 'validation.json'}", flush=True)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--test-timeout", type=positive_seconds, default=DEFAULT_TEST_TIMEOUT,
                        help=f"每轮完整回归的超时秒数，必须为正整数（默认 {DEFAULT_TEST_TIMEOUT}）")
    args = parser.parse_args(argv)
    config = ReleaseConfig.load(Path(__file__).resolve().parents[1], args.target)
    try:
        build(config, test_timeout=args.test_timeout)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"发布构建失败：{error}", file=sys.stderr)
        return 1
    return 0
