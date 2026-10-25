"""Coordinate immediate stopped-state persistence for every configuration change."""
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from .preferences import write_json


class SettingsController(QObject):
    failed = Signal(object)
    saved = Signal()

    def __init__(self, store, language_manager, *, busy, capture, restore, remember=True, parent=None):
        super().__init__(parent)
        self.store = store
        self.language_manager = language_manager
        self._busy = busy
        self._capture = capture
        self._restore = restore
        self.remember = remember
        self._depth = 0
        self.record = {}
        self.last_error = None

    @contextmanager
    def suspended(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def restore(self):
        if not self.remember:
            return
        self.record = self.store.load()
        with self.suspended():
            session = self.record.get("session")
            if session is not None:
                self._restore(session)

    def save(self, *, language=None):
        if self._busy() or self._depth:
            return False
        try:
            record = dict(self.record, language=language or self.language_manager.mode, session=self._capture())
            if self.remember:
                self.store.save(record)
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = exc
            self.failed.emit(exc)
            return False
        self.record = record
        self.last_error = None
        if self.remember:
            self.saved.emit()
        return True

    def change_language(self, mode):
        if self._busy() or self._depth:
            return False
        if mode == self.language_manager.mode:
            return True
        try:
            prepared = self.language_manager.prepare(mode)
        except (OSError, ValueError) as exc:
            self.failed.emit(exc)
            return False
        if not self.save(language=mode):
            return False
        with self.suspended():
            self.language_manager.apply(mode, prepared)
        return True

    def export_configuration(self, path, config):
        if self._busy():
            return False
        write_json(path, config.to_mapping())
        return True
