"""Application preferences, separate from versioned communication configurations."""
import json
import os
from pathlib import Path
import tempfile

from ..runtime import user_data_directory

LANGUAGES = ("system", "zh_CN", "en")


class PreferenceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else user_data_directory() / "preferences.json"
        self.warning = None

    def language(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            mode = data.get("language") if isinstance(data, dict) else None
            return mode if mode in LANGUAGES else "system"
        except FileNotFoundError:
            return "system"
        except (OSError, ValueError) as exc:
            self.warning = str(exc)
            return "system"

    def save_language(self, mode):
        if mode not in LANGUAGES:
            raise ValueError("Unsupported UI language")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix="preferences-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(dict(schema_version=1, language=mode), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
