"""Qt translation loading and text bindings that preserve editable widget values."""
from pathlib import Path
import weakref

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QObject, QPersistentModelIndex, QSettings, QSignalBlocker, QTranslator, Signal
from PySide6.QtWidgets import QGroupBox, QPushButton, QTableWidget, QTableWidgetItem, QWidget

from .preferences import LANGUAGES, PreferenceStore

CONTEXT = "ModbusDev"


def render(value):
    if isinstance(value, Text):
        source = QCoreApplication.translate(CONTEXT, value.source)
        return (source.format(*[render(arg) for arg in value.args],
                              **{key: render(arg) for key, arg in value.kwargs.items()})
                if value.args or value.kwargs else source)
    if isinstance(value, list):
        return [render(item) for item in value]
    return value


class Text(str):
    """A source template and its arguments; Qt receives its currently rendered string."""
    def __new__(cls, source, args=(), kwargs=None):
        translated = QCoreApplication.translate(CONTEXT, source)
        value = (translated.format(*[render(arg) for arg in args],
                                   **{key: render(arg) for key, arg in (kwargs or {}).items()})
                 if args or kwargs else translated)
        obj = str.__new__(cls, value)
        obj.source, obj.args, obj.kwargs = source, args, kwargs or {}
        return obj

    def format(self, *args, **kwargs):
        return Text(self.source, args, kwargs)

    def __str__(self):
        return render(self)

    def __format__(self, specification):
        return format(render(self), specification)

    def __add__(self, other):
        return Text("{left}{right}", kwargs=dict(left=self, right=other))

    def __radd__(self, other):
        return Text("{left}{right}", kwargs=dict(left=other, right=self))


def tr(source):
    return Text(source)


def bind(widget, method, value):
    bindings = getattr(widget, "_translation_bindings", None)
    if bindings is None:
        bindings = widget._translation_bindings = {}
    if isinstance(value, Text) or isinstance(value, list) and any(isinstance(item, Text) for item in value):
        bindings[method] = value
    else:
        bindings.pop(method, None)
    getattr(widget, method)(render(value))


def make_button(text):
    widget = QPushButton()
    bind(widget, "setText", text)
    return widget


def group_box(text):
    widget = QGroupBox()
    bind(widget, "setTitle", text)
    return widget


def add_item(combo, text, data=None):
    combo.addItem(render(text), data)
    if isinstance(text, Text):
        items = getattr(combo, "_translation_items", [])
        items = [(index, source) for index, source in items if index.isValid()]
        items.append((QPersistentModelIndex(combo.model().index(combo.count() - 1, 0)), text))
        combo._translation_items = items


def add_tab(tabs, page, text):
    tabs.addTab(page, render(text))
    bindings = getattr(tabs, "_translation_tabs", [])
    bindings.append((page, text))
    tabs._translation_tabs = bindings


def table_item(text):
    item = QTableWidgetItem(render(text))
    if isinstance(text, Text):
        item._translation_text = text
    return item


def retranslate_tree(root):
    for widget in [root, *root.findChildren(QWidget)]:
        for method, value in getattr(widget, "_translation_bindings", {}).items():
            getattr(widget, method)(render(value))
        items = getattr(widget, "_translation_items", [])
        if items:
            with QSignalBlocker(widget):
                current = widget.currentText() if widget.isEditable() else None
                for index, source in items:
                    if index.isValid():
                        widget.setItemText(index.row(), render(source))
                if current is not None:
                    widget.setEditText(current)
        for page, source in getattr(widget, "_translation_tabs", []):
            index = widget.indexOf(page)
            if index >= 0:
                widget.setTabText(index, render(source))
        if isinstance(widget, QTableWidget):
            for row in range(widget.rowCount()):
                for column in range(widget.columnCount()):
                    item = widget.item(row, column)
                    source = getattr(item, "_translation_text", None)
                    if source is not None:
                        item.setText(render(source))
                    for method, value in getattr(item, "_translation_bindings", {}).items():
                        getattr(item, method)(render(value))


def resolve_locale(mode, language_tags=None):
    if mode in ("zh_CN", "en"):
        return mode
    if language_tags is None:
        language_tags = QLocale.system().uiLanguages()
        import sys
        if sys.platform == "darwin":
            settings = QSettings(QSettings.NativeFormat, QSettings.UserScope, "Apple", "Global Domain")
            preferred = settings.value("AppleLanguages")
            if isinstance(preferred, str):
                preferred = [preferred]
            if isinstance(preferred, (list, tuple)) and preferred:
                language_tags = preferred
    for tag in language_tags:
        if not isinstance(tag, str):
            continue
        language = tag.replace("_", "-").split("-", 1)[0].split(".", 1)[0].split("@", 1)[0].lower()
        if language in ("zh", "en"):
            return "zh_CN" if language == "zh" else "en"
    return "en"


class LanguageManager(QObject):
    changed = Signal()

    def __init__(self, app, *, language=None, preferences=None):
        super().__init__(app)
        self.app = app
        self.preferences = preferences or PreferenceStore()
        self.mode = language if language in LANGUAGES else self.preferences.language()
        self.locale = "en"
        self._translators = []
        self._controllers = []
        self.warning = None
        try:
            self._install(self.mode)
        except OSError as exc:
            self.mode = "en"
            self.warning = str(exc)

    def register_controller(self, controller):
        self._controllers.append(weakref.ref(controller))

    def _prepare(self, mode):
        locale = resolve_locale(mode)
        translators = []
        if locale == "zh_CN":
            translator = QTranslator()
            catalog = Path(__file__).with_name("translations") / "modbus_dev_zh_CN.qm"
            if not translator.load(str(catalog)):
                raise OSError(f"Cannot load translation catalog: {catalog}")
            translators.append(translator)
            qt_translator = QTranslator()
            qt_directory = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
            if qt_translator.load("qtbase_zh_CN", qt_directory):
                translators.insert(0, qt_translator)
        return locale, translators

    def _replace(self, mode, locale, translators):
        for translator in self._translators:
            self.app.removeTranslator(translator)
        self._translators = translators
        for translator in translators:
            self.app.installTranslator(translator)
        self.mode, self.locale = mode, locale

    def _install(self, mode):
        locale, translators = self._prepare(mode)
        self._replace(mode, locale, translators)

    def change_language(self, mode):
        if mode not in LANGUAGES:
            raise ValueError("Unsupported UI language")
        self._controllers = [reference for reference in self._controllers if reference() is not None]
        if any(reference().busy for reference in self._controllers):
            return False
        if mode == self.mode:
            return True
        locale, translators = self._prepare(mode)
        self.preferences.save_language(mode)
        self._replace(mode, locale, translators)
        self.changed.emit()
        return True
