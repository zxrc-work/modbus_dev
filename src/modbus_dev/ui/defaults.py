"""集中定义界面初始配置及自动保存兼容版本，不依赖 Qt 或历史迁移。"""
import json

from ..domain.config import TaskConfig
from ..runtime import default_output_directory

# 只有自动保存内容无法继续兼容时才递增，不随软件补丁版本变化。
SETTINGS_COMPAT_VERSION = 1
SETTINGS_SCHEMA_VERSION = 1


def default_task_config():
    return TaskConfig(mode="tcp-slave", access_mode="read-only", duration_s=0)


def default_preferences(output_directory=None):
    """重置通信和语言；结果目录只保留路径，从不操作已有结果文件。"""
    config = json.loads(json.dumps(default_task_config().to_mapping()))
    return dict(schema_version=SETTINGS_SCHEMA_VERSION,
                config_compat_version=SETTINGS_COMPAT_VERSION, language="system",
                session=dict(configuration=dict(schema_version=1,
                    base_config=config, values={}),
                    output_directory=(output_directory if isinstance(output_directory, str)
                                      else str(default_output_directory()))))
