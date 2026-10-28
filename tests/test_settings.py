"""验证停止状态自动保存、真实进程重启以及写入失败处理。"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from config_factory import task_mapping
from modbus_dev.domain.config import TaskConfig
from modbus_dev.ui.preferences import PreferenceStore
from modbus_dev.ui.defaults import SETTINGS_COMPAT_VERSION, default_task_config


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Requires the optional GUI dependency")
class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from modbus_dev.gui import create_application
        cls.app = create_application(language="zh_CN")

    def setUp(self):
        from modbus_dev.gui import Workbench
        self.root = tempfile.TemporaryDirectory()
        self.store = PreferenceStore(Path(self.root.name) / "preferences.json")
        self.previous_store = self.app.preference_store
        self.app.preference_store = self.store
        self.app.language_manager._install("zh_CN")
        self.window = Workbench()
        self.window.show()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Qt/worker lifecycle timed out")

    def tearDown(self):
        if self.window.active():
            self.window.stop()
            self.wait_for(lambda: not self.window.active())
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.preference_store = self.previous_store
        self.app.language_manager._install("zh_CN")
        self.root.cleanup()

    def test_every_registered_field_is_saved_before_returning_from_its_change(self):
        from PySide6.QtWidgets import QAbstractButton, QComboBox, QSpinBox
        self.assertFalse(self.store.path.exists())
        for key, field in self.window.editor.form_state.fields.items():
            with self.subTest(field=key):
                if isinstance(field, QComboBox):
                    if field.isEditable():
                        field.setEditText("custom-" + key)
                    else:
                        index = (field.currentIndex() + 1) % field.count()
                        field.setCurrentIndex(index)
                elif isinstance(field, QAbstractButton):
                    field.setChecked(not field.isChecked())
                elif isinstance(field, QSpinBox):
                    field.setValue(field.value() + 1)
                else:
                    field.setText("draft-" + key)
                saved = self.store.load()["session"]["configuration"]["values"]
                self.assertEqual(saved, self.window.editor.form_state.capture())

    def test_all_configuration_and_language_survive_a_new_process_without_autostart(self):
        w = self.window
        config = TaskConfig.from_mapping(task_mapping(
            mode="rtu-master", access_mode="read-write", operation="write-multiple",
            serial=dict(port="COM77", baud=31250, parity="E", stop_bits=2, rts_toggle=True),
            host="127.0.0.2", port=2502, unit=17, period_ms=250, timeout_ms=150, duration_s=1.5,
            policy="strict", byte_order="B4321", readback_every=2,
            schedule_tolerance_percent=15, logging=dict(max_files=7), points=[
                dict(name="温度", address=10, source=dict(kind="constant", value=12.5)),
                dict(name="压力", address=20, source=dict(kind="sine", value=3, amplitude=2, frequency_hz=.5)),
                dict(enabled=False, name="保留", address=30, source=dict(value=-2)),
                dict(enabled=True, name="第四点", address=40, source=dict(value=4))]))
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            self.assertTrue(w.apply_config(config))
            self.assertEqual(save.call_count, 1)
        w.output.setText(str(Path(self.root.name) / "results with spaces"))
        self.assertTrue(w.set_language("en"))
        expected = json.loads(json.dumps(w.configuration().to_mapping()))
        before = self.store.path.read_bytes()
        script = '''
import json, sys
from modbus_dev.gui import Workbench, create_application
app = create_application(preferences_path=sys.argv[1])
w = Workbench()
print(json.dumps(dict(config=w.configuration().to_mapping(), output=w.output.text(),
    language=app.language_manager.mode, title=w.start_button.text(),
    active=w.active(), process=w.process is not None, result=w.result)))
w.close()
'''
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        child = subprocess.run([sys.executable, "-c", script, str(self.store.path)],
                               env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        restored = json.loads(child.stdout)
        self.assertEqual(restored["config"], expected)
        self.assertEqual(restored["output"], w.output.text())
        self.assertEqual(restored["language"], "en")
        self.assertEqual(restored["title"], "Start session")
        self.assertFalse(restored["active"])
        self.assertFalse(restored["process"])
        self.assertIsNone(restored["result"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_incomplete_drafts_restore_without_a_validation_dialog(self):
        from modbus_dev.gui import Workbench
        w = self.window
        w.fields["serial.port"].setEditText("COM-CUSTOM")
        w.fields["serial.baud"].setEditText("31250")
        w.fields["period_ms"].setText("unfinished")
        w.point_editors[0].fields["source.value"].setText("1.")
        w.point_editors[1].enabled.setChecked(True)
        w.point_editors[1].fields["name"].setText("draft name")
        expected = w.editor.form_state.capture()
        before = self.store.path.read_bytes()
        w.close()
        with patch.object(Workbench, "error") as error, patch.object(self.store, "save", wraps=self.store.save) as save:
            restored = Workbench()
            self.assertEqual(restored.editor.form_state.capture(), expected)
            self.assertFalse(restored.active())
            error.assert_not_called()
            save.assert_not_called()
            restored.close()
            restored.deleteLater()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_incompatible_settings_reset_on_cold_start_but_keep_results(self):
        # 新进程覆盖语言初始化先于主窗口恢复的真实顺序，不能仅测试内存重置。
        output = Path(self.root.name) / 'previous results'
        output.mkdir()
        evidence = output / 'history.json'
        evidence.write_text('do not delete', encoding='utf-8')
        script = '''
import json, sys
from modbus_dev.gui import Workbench, create_application
app = create_application(preferences_path=sys.argv[1])
w = Workbench()
print(json.dumps(dict(config=w.configuration().to_mapping(), output=w.output.text(),
    language=app.language_manager.mode, active=w.active(), logs=w.logs.toPlainText())))
w.close()
'''
        for version in (None, SETTINGS_COMPAT_VERSION - 1):
            data = dict(schema_version=1, language='en', session=dict(
                configuration=dict(schema_version=1, base_config={'schema_version': 3},
                                   values={'readback_every': 'unfinished'}), output_directory=str(output)))
            if version is not None:
                data['config_compat_version'] = version
            self.store.path.write_text(json.dumps(data), encoding='utf-8')
            with self.subTest(version=version):
                child = subprocess.run([sys.executable, '-c', script, str(self.store.path)],
                    env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), capture_output=True,
                    text=True, encoding='utf-8', timeout=20)
                self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                restored = json.loads(child.stdout)
                self.assertEqual(restored['config'], json.loads(json.dumps(default_task_config().to_mapping())))
                self.assertEqual(restored['language'], 'system')
                self.assertEqual(restored['output'], str(output))
                self.assertFalse(restored['active'])
                self.assertEqual(restored['logs'], '')
                self.assertEqual(evidence.read_text(), 'do not delete')
                self.assertEqual(json.loads(self.store.path.read_text())['config_compat_version'], SETTINGS_COMPAT_VERSION)

    def test_import_old_task_reports_incompatibility_without_changing_current_draft(self):
        from PySide6.QtWidgets import QFileDialog
        path = Path(self.root.name) / 'old-task.json'
        path.write_text(json.dumps(dict(schema_version=3, mode='rtu-master', master_operation='read-input')))
        before = self.window.editor.capture_draft()
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), 'JSON')), \
                patch.object(self.window, 'error') as error, patch.object(self.store, 'save') as save:
            self.window.load()
            error.assert_called_once()
            self.assertEqual(error.call_args.args[0].reason, 'config_version')
            save.assert_not_called()
        self.assertEqual(self.window.editor.capture_draft(), before)

    def test_language_and_output_changes_preserve_the_entire_saved_configuration(self):
        self.window.fields["unit"].setText("27")
        self.window.point_editors[2].fields["name"].setText("third point")
        expected = self.store.load()["session"]["configuration"]
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            self.assertTrue(self.window.set_language("en"))
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.store.load()["session"]["configuration"], expected)
        self.window.output.setText("custom-results")
        saved = self.store.load()
        self.assertEqual(saved["language"], "en")
        self.assertEqual(saved["session"]["configuration"], expected)
        self.assertEqual(saved["session"]["output_directory"], "custom-results")

    def test_manual_export_uses_task_schema_without_overwriting_autosaved_settings(self):
        from PySide6.QtWidgets import QFileDialog
        self.window.fields["unit"].setText("19")
        before = self.store.path.read_bytes()
        target = Path(self.root.name) / "exported.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "JSON")), \
                patch.object(self.store, "save", wraps=self.store.save) as save:
            self.window.save()
            save.assert_not_called()
        exported = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(exported["schema_version"], 4)
        self.assertEqual(exported["unit"], 19)
        self.assertNotIn("language", exported)
        self.assertNotIn("session", exported)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_no_writes_during_running_stopping_or_exit_and_no_exit_retry(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        w = self.window
        w.apply_config(TaskConfig(mode="tcp-slave", port=port, duration_s=0))
        w.output.setText(self.root.name)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            w.start()
            self.assertTrue(w.active())
            self.assertFalse(w.settings.save())
            self.assertFalse(w.set_language("en"))
            self.wait_for(lambda: w.status.text() == "运行中")
            w.stop()
            self.assertTrue(w.active())
            self.assertFalse(w.settings.save())
            self.wait_for(lambda: not w.active())
            w.close()
            self.app.processEvents()
            save.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_failed_write_is_reported_immediately_and_never_retried_on_exit(self):
        w = self.window
        w.fields["unit"].setText("7")
        before = self.store.path.read_bytes()
        with patch("modbus_dev.ui.preferences.os.replace", side_effect=OSError("disk unavailable")) as replace, \
                patch.object(w, "error") as error:
            w.fields["unit"].setText("8")
            self.assertEqual(error.call_count, 1)
            self.assertIn("未保存", str(error.call_args.args[0]))
            self.assertIsNotNone(w.settings.last_error)
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertFalse(w.set_language("en"))
            self.assertEqual(error.call_count, 2)
            self.assertEqual(self.app.language_manager.mode, "zh_CN")
            attempts = replace.call_count
            w.close()
            self.app.processEvents()
            self.assertEqual(replace.call_count, attempts)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.store.path.parent.glob("preferences-*.tmp")), [])

    def test_import_write_failure_is_not_reported_as_success(self):
        from PySide6.QtWidgets import QFileDialog
        w = self.window
        w.fields["unit"].setText("7")
        before = self.store.path.read_bytes()
        imported = Path(self.root.name) / "imported.json"
        imported.write_text(json.dumps(TaskConfig(mode="tcp-slave", unit=8).to_mapping()), encoding="utf-8")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(imported), "JSON")), \
                patch("modbus_dev.ui.preferences.os.replace", side_effect=OSError("disk unavailable")), \
                patch.object(w, "error") as error, patch.object(w, "set_status") as status:
            w.load()
            error.assert_called_once()
            status.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(w.fields["unit"].text(), "8")
        self.assertIsNotNone(w.settings.last_error)

    def test_current_language_settings_restore_without_rewriting_the_file(self):
        from modbus_dev.gui import Workbench
        self.store.save(dict(language="en", future_setting="keep"))
        before = self.store.path.read_bytes()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            restored = Workbench()
            self.assertEqual(restored.text("mode"), "tcp-slave")
            save.assert_not_called()
            restored.fields["unit"].setText("18")
            self.assertEqual(save.call_count, 1)
            self.assertEqual(self.store.load()["future_setting"], "keep")
            restored.close()
            restored.deleteLater()
        self.assertNotEqual(self.store.path.read_bytes(), before)

    def test_corrupt_saved_draft_keeps_defaults_and_the_original_file(self):
        from modbus_dev.gui import Workbench
        self.store.save(dict(language="zh_CN", session=dict(configuration={}, output_directory="saved")))
        before = self.store.path.read_bytes()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            restored = Workbench()
            self.assertEqual(restored.text("mode"), "tcp-slave")
            self.assertIn("无法恢复", restored.logs.toPlainText())
            save.assert_not_called()
            restored.close()
            restored.deleteLater()
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
