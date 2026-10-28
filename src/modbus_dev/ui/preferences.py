"""原子保存界面设置；不兼容版本直接恢复默认，仅保留结果目录路径。"""
import json
import os
from pathlib import Path
import tempfile

from ..runtime import user_data_directory
from .defaults import SETTINGS_COMPAT_VERSION, SETTINGS_SCHEMA_VERSION, default_preferences

LANGUAGES = ("system", "zh_CN", "en")


def write_json(path, data):
    """Atomically write settings or an explicitly exported task configuration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.stem + "-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PreferenceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else user_data_directory() / "preferences.json"
        self.warning = None

    def load(self):
        self.warning = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Unsupported application settings format")
            if (type(data.get("schema_version")) is not int
                    or data["schema_version"] != SETTINGS_SCHEMA_VERSION
                    or type(data.get("config_compat_version")) is not int
                    or data["config_compat_version"] != SETTINGS_COMPAT_VERSION):
                # 在语言初始化与草稿恢复前处理旧文件，不能先尝试解释旧界面字段。
                session = data.get("session")
                output = session.get("output_directory") if isinstance(session, dict) else None
                data = default_preferences(output)
                try:
                    self.save(data)
                except OSError as error:
                    # 写入失败仍使用内存默认值，保留原文件并向界面报告原因。
                    self.warning = str(error)
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.warning = str(exc)
            return {}

    def save(self, settings):
        write_json(self.path, dict(settings, schema_version=SETTINGS_SCHEMA_VERSION,
                                  config_compat_version=SETTINGS_COMPAT_VERSION))
