"""验证配置拒绝旧版本、自动设置重置、语言默认值及结果目录保留。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modbus_dev.cli import main
from modbus_dev.domain.config import CONFIG_VERSION, TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.ui.defaults import SETTINGS_COMPAT_VERSION, default_preferences
from modbus_dev.ui.preferences import PreferenceStore


class TaskCompatibilityTests(unittest.TestCase):
    def test_old_and_future_versions_are_rejected_before_point_parsing(self):
        for version in (*range(1, CONFIG_VERSION), CONFIG_VERSION + 1, None, True, str(CONFIG_VERSION)):
            for mode in ('rtu-master', 'rtu-slave', 'tcp-slave'):
                with self.subTest(version=version, mode=mode), self.assertRaises(ConfigurationError) as error:
                    TaskConfig.from_mapping(dict(schema_version=version, mode=mode, points='old draft'))
                self.assertEqual(error.exception.reason, 'config_version')

    def test_current_version_rejects_shared_fields_instead_of_migrating_them(self):
        for field, value in dict(addresses=[0], source={}, master_operation='read-input',
                                 byte_order='B4321', readback_every=1, read_function=3).items():
            with self.subTest(field=field), self.assertRaises(ConfigurationError) as error:
                TaskConfig.from_mapping(dict(schema_version=CONFIG_VERSION, mode='tcp-slave', **{field: value}))
            self.assertEqual(error.exception.reason, 'unknown_fields')

    def test_file_requires_version_and_cli_never_rewrites_rejected_input(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'old.json'
            for version in (None, *range(1, CONFIG_VERSION), CONFIG_VERSION + 1):
                data = dict(mode='tcp-slave', points=[{}])
                if version is not None:
                    data['schema_version'] = version
                path.write_text(json.dumps(data), encoding='utf-8')
                before = path.read_bytes()
                with self.subTest(version=version):
                    with self.assertRaises(ConfigurationError) as error:
                        load_config(path)
                    self.assertEqual(error.exception.reason, 'config_version')
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(main(['validate', str(path)]), 2)
                    self.assertIn('Incompatible configuration version', json.loads(stderr.getvalue())['message'])
                    self.assertEqual(path.read_bytes(), before)

    def test_current_file_roundtrips_without_software_version_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'current.json'
            config = TaskConfig(mode='tcp-slave')
            path.write_text(json.dumps(config.to_mapping()), encoding='utf-8')
            with patch('modbus_dev.__version__', 'different-release'):
                self.assertEqual(load_config(path), config)


class SettingsCompatibilityTests(unittest.TestCase):
    def test_missing_or_mismatched_compatibility_resets_before_using_old_draft(self):
        with tempfile.TemporaryDirectory() as root:
            store = PreferenceStore(Path(root) / 'preferences.json')
            output = Path(root) / 'existing results'
            output.mkdir()
            evidence = output / 'history.json'
            evidence.write_text('keep this evidence', encoding='utf-8')
            for version in (None, SETTINGS_COMPAT_VERSION - 1, SETTINGS_COMPAT_VERSION + 1,
                            True, str(SETTINGS_COMPAT_VERSION)):
                data = dict(schema_version=1, language='en', extra='discard',
                            session=dict(configuration='incompatible old draft', output_directory=str(output)))
                if version is not None:
                    data['config_compat_version'] = version
                store.path.write_text(json.dumps(data), encoding='utf-8')
                with self.subTest(version=version):
                    restored = store.load()
                    self.assertEqual(restored, default_preferences(str(output)))
                    self.assertEqual(json.loads(store.path.read_text()), restored)
                    self.assertEqual(evidence.read_text(), 'keep this evidence')
                    self.assertEqual(restored['language'], 'system')
                    self.assertIsNone(store.warning)
                    before = store.path.read_bytes()
                    self.assertEqual(store.load(), restored)
                    self.assertEqual(store.path.read_bytes(), before)

    def test_current_version_preserves_incomplete_drafts_and_language_without_rewrite(self):
        with tempfile.TemporaryDirectory() as root:
            store = PreferenceStore(Path(root) / 'preferences.json')
            data = default_preferences('saved-output')
            data['language'] = 'en'
            data['session']['configuration']['values'] = {'period_ms': 'unfinished'}
            store.save(data)
            before = store.path.read_bytes()
            with patch('modbus_dev.__version__', 'another-release'), patch.object(store, 'save') as save:
                self.assertEqual(store.load(), data)
                save.assert_not_called()
            self.assertEqual(store.path.read_bytes(), before)

    def test_reset_write_failure_keeps_original_file_and_returns_safe_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            store = PreferenceStore(Path(root) / 'preferences.json')
            store.path.write_text(json.dumps(dict(language='en', session=dict(output_directory='keep-path'))))
            before = store.path.read_bytes()
            with patch('modbus_dev.ui.preferences.os.replace', side_effect=OSError('disk unavailable')):
                self.assertEqual(store.load(), default_preferences('keep-path'))
            self.assertIn('disk unavailable', store.warning)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(list(store.path.parent.glob('preferences-*.tmp')), [])

    def test_reset_does_not_require_a_valid_or_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as root:
            store = PreferenceStore(Path(root) / 'preferences.json')
            for session in (None, {}, {'output_directory': 17}, {'output_directory': ''},
                            {'output_directory': str(Path(root) / 'not-created')}):
                with self.subTest(session=session):
                    store.path.write_text(json.dumps(dict(session=session)))
                    data = store.load()
                    output = session.get('output_directory') if isinstance(session, dict) else None
                    self.assertEqual(data, default_preferences(output))
                    self.assertFalse((Path(root) / 'not-created').exists())

    def test_first_run_does_not_write_or_create_an_output_directory(self):
        with tempfile.TemporaryDirectory() as root:
            store = PreferenceStore(Path(root) / 'preferences.json')
            self.assertEqual(store.load(), {})
            self.assertFalse(store.path.exists())


if __name__ == '__main__':
    unittest.main()
