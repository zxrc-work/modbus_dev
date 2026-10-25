"""发布入口、源码保护和归档约束；不把脚本测试冒充其他平台实包验收。"""
import ast
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modbus_dev import runtime
BUILD_SUPPORTED = sys.version_info >= (3, 11)
if BUILD_SUPPORTED:
    from release_tools.build import archive_payload, unpack_archive
    from release_tools.bundle import render_spec
    from release_tools.compile import discover_modules
    from release_tools.config import ReleaseConfig
    from release_tools.verify import clean_environment, crc_frame
    from release_tools.translations import validate_catalog


class RuntimeTests(unittest.TestCase):
    def test_source_worker_keeps_python_module_entry(self):
        with patch.object(sys, "frozen", False, create=True):
            command, arguments = runtime.worker_command("input.json", "result")
        self.assertEqual(command, sys.executable)
        self.assertEqual(arguments, ["-m", "modbus_dev", "worker", "input.json", "--output", "result"])

    def test_frozen_gui_uses_adjacent_console_entry_not_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "modbus-dev").touch()
            metadata = root / "release/BUILD_INFO.json"
            metadata.parent.mkdir()
            metadata.write_text(json.dumps(dict(cli_executable="modbus-dev")))
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "_MEIPASS", str(root), create=True), \
                    patch.object(sys, "executable", str(root / "ModbusDev")):
                command, arguments = runtime.worker_command("input", "output")
                self.assertEqual(Path(command), root.resolve() / "modbus-dev")
                self.assertEqual(arguments, ["worker", "input", "--output", "output"])
                (root / "modbus-dev").unlink()
                with self.assertRaises(FileNotFoundError):
                    runtime.worker_command("input", "output")

    def test_frozen_output_does_not_use_installation_or_callers_directory(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertTrue(runtime.default_output_directory().is_absolute())
            self.assertNotEqual(runtime.default_output_directory(), Path.cwd() / "results")

    def test_headless_runtime_does_not_import_qt(self):
        import subprocess
        result = subprocess.run([sys.executable, "-c", "import sys; from modbus_dev.runtime import worker_command; "
            "assert not any(n.startswith('PySide6.Qt') for n in sys.modules)"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(BUILD_SUPPORTED, "发布构建工具需要 Python 3.11+；应用运行基线不变")
class BuildTests(unittest.TestCase):
    def test_translation_catalog_is_complete_and_bundled(self):
        self.assertGreater(validate_catalog(ROOT), 0)
        for target in ("macos-arm64", "windows-x64", "kylin-x86_64"):
            config = ReleaseConfig.load(ROOT, target)
            spec = render_spec(config, Path("/stage"), Path("/native"), [], [])
            self.assertIn("ui/translations", spec.replace("\\\\", "/"))

    def test_every_business_module_is_native_and_dynamic_backends_are_declared(self):
        protected, hidden = discover_modules(ROOT / "src", "modbus_dev")
        self.assertTrue({"modbus_dev.gui", "modbus_dev.cli", "modbus_dev.worker", "modbus_dev.selfcheck",
                         "modbus_dev.domain.config"}.issubset(protected))
        self.assertNotIn("modbus_dev.__main__", protected)
        self.assertIn("PySide6.QtWidgets", hidden)
        self.assertIn("serial.tools.list_ports", hidden)

    def test_business_definitions_in_initializers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo").mkdir()
            (root / "demo/__init__.py").write_text("def unprotected(): return 1\n")
            with self.assertRaises(RuntimeError):
                discover_modules(root, "demo")

    def test_all_targets_render_separate_windowed_and_console_entries(self):
        for target in ("macos-arm64", "windows-x64", "kylin-x86_64"):
            config = ReleaseConfig.load(ROOT, target)
            tree = ast.parse(render_spec(config, Path("/stage"), Path("/native"), [], []))
            calls = {node.targets[0].id: node.value for node in tree.body if isinstance(node, ast.Assign)
                     and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)}
            for name, expected in (("cli", True), ("gui", False)):
                console = next(item.value for item in calls[name].keywords if item.arg == "console")
                self.assertEqual(ast.literal_eval(console), expected)
            self.assertEqual("app" in calls, target == "macos-arm64")

    def test_target_mismatch_is_refused(self):
        config = ReleaseConfig.load(ROOT, "windows-x64")
        with patch("release_tools.config.sys.platform", "darwin"), patch("release_tools.config.architecture", return_value="arm64"):
            with self.assertRaises(RuntimeError):
                config.require_native_target()

    @unittest.skipUnless(os.name == "posix", "需要 POSIX 符号链接和执行权限")
    def test_archive_roundtrip_preserves_links_modes_and_versioned_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "App-0.1.0-macos-arm64"
            payload.mkdir()
            executable = payload / "command"
            executable.write_text("launcher")
            executable.chmod(0o755)
            (payload / "alias").symlink_to("command")
            archive = archive_payload(payload, root / payload.name, False)
            self.assertEqual(archive.name, payload.name + ".tar.gz")
            unpack_archive(archive, root / "解压 with spaces")
            extracted = root / "解压 with spaces" / payload.name
            self.assertTrue((extracted / "alias").is_symlink())
            self.assertTrue((extracted / "command").stat().st_mode & stat.S_IXUSR)

    def test_sanitized_environment_excludes_development_runtime(self):
        with patch.dict(os.environ, PYTHONPATH="/source", QT_PLUGIN_PATH="/qt", VIRTUAL_ENV="/venv"):
            environment = clean_environment()
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("QT_PLUGIN_PATH", environment)
        self.assertNotIn("VIRTUAL_ENV", environment)

    def test_independent_crc_matches_standard_fixed_frame(self):
        self.assertEqual(crc_frame(bytes.fromhex("010300000002")).hex(), "010300000002c40b")


if __name__ == "__main__":
    unittest.main()
