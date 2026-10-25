"""The single atomic JSON store for all application settings."""
import json
import os
from pathlib import Path
import tempfile

from ..runtime import user_data_directory

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
            if not isinstance(data, dict) or data.get("schema_version", 1) != 1:
                raise ValueError("Unsupported application settings format")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.warning = str(exc)
            return {}

    def save(self, settings):
        write_json(self.path, dict(settings, schema_version=1))
