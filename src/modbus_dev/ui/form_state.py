"""Shared field codecs for configuration drafts; no disk access or business validation."""
from contextlib import contextmanager, ExitStack
from functools import wraps

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QSpinBox


class FormState:
    def __init__(self, fields, edited):
        self.fields = fields
        self._depth = 0
        self._edited = edited
        for field in fields.values():
            if isinstance(field, QComboBox):
                signal = field.currentTextChanged if field.isEditable() else field.currentIndexChanged
            elif isinstance(field, QAbstractButton):
                signal = field.toggled
            elif isinstance(field, QSpinBox):
                signal = field.valueChanged
            else:
                signal = field.textChanged
            signal.connect(self._notify)

    def _notify(self, *_):
        if not self._depth:
            self._edited()

    @contextmanager
    def suspended(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def capture(self):
        values = {}
        for key, field in self.fields.items():
            if isinstance(field, QComboBox):
                values[key] = field.currentText() if field.isEditable() else field.currentData()
            elif isinstance(field, QAbstractButton):
                values[key] = field.isChecked()
            elif isinstance(field, QSpinBox):
                values[key] = field.value()
            else:
                values[key] = field.text()
        return values

    def validate(self, values):
        if not isinstance(values, dict):
            raise ValueError("Invalid saved configuration fields")
        for key, value in values.items():
            field = self.fields.get(key)
            if field is None:
                continue
            if isinstance(field, QComboBox) and not field.isEditable():
                valid = value is None or field.findData(value) >= 0
            elif isinstance(field, QAbstractButton):
                valid = type(value) is bool
            elif isinstance(field, QSpinBox):
                valid = type(value) is int and field.minimum() <= value <= field.maximum()
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError(f"Invalid saved configuration field: {key}")

    def restore(self, values):
        self.validate(values)
        with self.suspended(), ExitStack() as stack:
            for field in self.fields.values():
                stack.enter_context(QSignalBlocker(field))
            for key, value in values.items():
                field = self.fields.get(key)
                if field is None:
                    continue
                if isinstance(field, QComboBox):
                    field.setCurrentIndex(field.findText(value) if field.isEditable() else field.findData(value))
                    if field.isEditable():
                        field.setEditText(value)
                elif isinstance(field, QAbstractButton):
                    field.setChecked(value)
                elif isinstance(field, QSpinBox):
                    field.setValue(value)
                else:
                    field.setText(value)


def programmatic_changes(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.form_state.suspended():
            return method(self, *args, **kwargs)
    return wrapped
