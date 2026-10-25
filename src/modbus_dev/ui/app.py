"""Qt application bootstrap; imported only by GUI entry points."""
import os
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from .theme import application_palette, application_stylesheet, ui_font
from .widgets import WorkbenchStyle
from .i18n import LanguageManager
from .preferences import PreferenceStore


def create_application(*, language=None, preferences_path=None):
    existing = QApplication.instance()
    if existing is not None:
        if not hasattr(existing, "preference_store"):
            existing.preference_store = PreferenceStore(preferences_path)
        if not hasattr(existing, "language_manager"):
            mode = language or existing.preference_store.load().get("language", "system")
            existing.language_manager = LanguageManager(existing, language=mode)
        return existing
    # Use pixel-aligned Windows text rendering. Respect an
    # explicit platform (e.g. offscreen) and never disable Qt's high-DPI scaling.
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")
    if "QT_SCALE_FACTOR_ROUNDING_POLICY" not in os.environ:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ModbusDev")
    app.setApplicationName("Modbus Dev")
    app.setStyle(WorkbenchStyle())
    app.setPalette(application_palette())
    font = ui_font()
    if sys.platform == "win32":
        font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)
    app.setStyleSheet(application_stylesheet())
    app.preference_store = PreferenceStore(preferences_path)
    mode = language or app.preference_store.load().get("language", "system")
    app.language_manager = LanguageManager(app, language=mode)
    return app



def main():
    from .main_window import Workbench
    app = create_application()
    window = Workbench()
    window.show()
    return app.exec()
