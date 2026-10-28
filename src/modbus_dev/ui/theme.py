"""统一浅色主题与逻辑像素尺寸，测点地址控件沿用原有高 DPI 设计。"""
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette


COLORS = {
    "paper": "#F6F7F9", "panel": "#FFFFFF", "ink": "#252A34",
    "muted": "#687181", "placeholder": "#9198A5", "line": "#DCE1E8",
    "line_hover": "#B8C2D0", "hover": "#F0F3F8", "pressed": "#E7ECF4",
    "accent": "#4269C7", "accent_hover": "#365BB5", "accent_pressed": "#2C4D9D",
    "accent_soft": "#EAF0FC", "focus": "#7389BC", "disabled": "#F1F3F6",
    "disabled_ink": "#A1A8B4", "success": "#338164", "success_soft": "#EAF5EF",
    "warning": "#A87426", "warning_soft": "#FFF5E6",
    "error": "#BC4D54", "error_soft": "#FBEDEF",
}
FONT_FAMILIES = ("Segoe UI", "Microsoft YaHei", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono")


@dataclass(frozen=True)
class DesignMetrics:
    font_size: int = 14
    caption_size: int = 13
    title_size: int = 24
    section_size: int = 15
    metric_size: int = 22
    control_height: int = 36
    spacing: int = 8
    section_spacing: int = 12
    page_spacing: int = 16
    page_padding: int = 24
    card_padding: int = 16
    radius: int = 6
    panel_radius: int = 8
    window_width: int = 1280
    window_height: int = 840


METRICS = DesignMetrics()


def ui_font(*, monospace=False):
    """Select installed fonts and retain explicit CJK fallback and hinting."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    font = QFont(app.font()) if app else QFont()
    available = set(QFontDatabase.families())
    candidates = (*MONO_FAMILIES, *FONT_FAMILIES) if monospace else FONT_FAMILIES
    families = [family for family in candidates if family in available]
    if families:
        font.setFamilies(families)
    font.setPixelSize(METRICS.caption_size if monospace else METRICS.font_size)
    return font


def application_palette():
    palette = QPalette()
    roles = {
        QPalette.Window: "paper", QPalette.WindowText: "ink", QPalette.Base: "panel",
        QPalette.AlternateBase: "hover", QPalette.Text: "ink", QPalette.Button: "panel",
        QPalette.ButtonText: "ink", QPalette.Highlight: "accent_soft",
        QPalette.HighlightedText: "ink", QPalette.PlaceholderText: "placeholder",
        QPalette.ToolTipBase: "panel", QPalette.ToolTipText: "ink", QPalette.Link: "accent",
        QPalette.Light: "panel", QPalette.Mid: "line", QPalette.Dark: "line_hover",
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(COLORS[color]))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(COLORS["disabled_ink"]))
    for role in (QPalette.Base, QPalette.Button):
        palette.setColor(QPalette.Disabled, role, QColor(COLORS["disabled"]))
    return palette


def application_stylesheet():
    c, m = COLORS, METRICS
    # QSS sizes the content box: padding (4 + 4) and borders (1 + 1) total 10.
    control_content = m.control_height - 10
    return f"""
    QWidget {{ color: {c['ink']}; font-size: {m.font_size}px; }}
    QMainWindow, QWidget#appRoot, QWidget#runtimeContent {{ background: {c['paper']}; }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[role="title"] {{ font-size: {m.title_size}px; font-weight: 700; }}
    QLabel[role="section"] {{ font-size: {m.section_size}px; font-weight: 600; }}
    QLabel[role="caption"] {{ color: {c['muted']}; font-size: {m.caption_size}px; }}
    QLabel[role="pointRange"] {{ color: {c['muted']}; font-size: {m.caption_size}px; }}
    QLabel[tone="warning"] {{ color: {c["warning"]}; }}
    QLabel[tone="error"] {{ color: {c['error']}; }}
    QLabel[role="metric"] {{ font-size: {m.metric_size}px; font-weight: 600; }}
    QLabel#brandMark {{ background: {c['accent']}; color: {c['panel']}; border-radius: 10px;
        font-size: {m.title_size}px; font-weight: 700; }}
    QFrame[card="true"] {{ background: {c['panel']}; border: 1px solid {c['line']};
        border-radius: {m.panel_radius}px; }}
    QLabel#sessionStatus {{ background: {c['hover']}; color: {c['muted']};
        border-radius: {m.radius}px; padding: 5px 10px; font-size: {m.caption_size}px; }}
    QLabel#sessionStatus[tone="active"] {{ background: {c['accent_soft']}; color: {c['accent']}; }}
    QLabel#sessionStatus[tone="success"] {{ background: {c['success_soft']}; color: {c['success']}; }}
    QLabel#sessionStatus[tone="warning"] {{ background: {c['warning_soft']}; color: {c['warning']}; }}
    QLabel#sessionStatus[tone="error"] {{ background: {c['error_soft']}; color: {c['error']}; }}
    QLabel#sessionHint {{ color: {c['muted']}; font-size: {m.caption_size}px; }}
    QLabel#sessionHint[writing="true"] {{ color: {c['warning']}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget#configPage {{ background: {c['panel']}; }}
    QGroupBox {{ border: none; border-top: 1px solid {c['line']};
        margin-top: 12px; padding-top: 20px; font-size: {m.section_size}px; font-weight: 600; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
        padding: 0 8px 0 0; background: {c['panel']}; }}
    QLineEdit, QComboBox, QSpinBox {{ background: {c['panel']}; border: 1px solid {c['line']};
        border-radius: {m.radius}px; padding: 4px 10px;
        min-height: {control_content}px; max-height: {control_content}px;
        selection-background-color: {c['accent_soft']}; selection-color: {c['ink']}; }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {c['line_hover']}; }}
    QLineEdit:focus, QComboBox:focus, QComboBox:on, QSpinBox:focus {{ border-color: {c['focus']}; }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ background: {c['disabled']}; color: {c['disabled_ink']}; }}
    QSpinBox {{ padding-right: 26px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 22px; border: none;
        border-left: 1px solid {c['line']}; background: {c['hover']}; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c['pressed']}; }}
    QComboBox {{ padding-right: 32px; }}
    QComboBox QLineEdit {{ border: none; border-radius: 0; padding: 0;
        min-height: 0; max-height: 16777215px; background: transparent; }}
    QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: top right;
        width: 30px; background: transparent; border: none; }}
    QComboBox::down-arrow {{ image: none; }}
    QComboBox QAbstractItemView {{ background: {c['panel']}; color: {c['ink']};
        border: none; padding: 4px; outline: none;
        selection-background-color: {c['accent_soft']}; selection-color: {c['ink']}; }}
    QComboBox QAbstractItemView::item {{ min-height: 34px; padding: 0 8px; border-radius: 4px; }}
    QComboBox QAbstractItemView::item:hover {{ background: {c['hover']}; }}
    QComboBox QAbstractItemView::item:selected {{ background: {c['accent_soft']}; color: {c['ink']}; }}
    QPushButton {{ background: {c['panel']}; border: 1px solid {c['line']};
        border-radius: {m.radius}px; padding: 4px 12px;
        min-height: {control_content}px; max-height: {control_content}px; }}
    QPushButton:hover {{ background: {c['hover']}; border-color: {c['line_hover']}; }}
    QPushButton:pressed {{ background: {c['pressed']}; }}
    QPushButton:focus {{ border-color: {c['focus']}; }}
    QPushButton[role="primary"] {{ background: {c['accent']}; border-color: {c['accent']};
        color: {c['panel']}; font-weight: 600; }}
    QPushButton[role="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton[role="primary"]:pressed {{ background: {c['accent_pressed']}; }}
    QPushButton:disabled, QPushButton[role="primary"]:disabled {{ background: {c['disabled']};
        color: {c['disabled_ink']}; border-color: {c['line']}; }}
    QCheckBox {{ background: transparent; spacing: 8px; min-height: {m.control_height}px; }}
    QCheckBox:disabled {{ color: {c['disabled_ink']}; }}
    QCheckBox:focus {{ color: {c['accent']}; }}
    QTabWidget::pane {{ border: none; background: {c['panel']}; }}
    QTabBar::tab {{ background: transparent; color: {c['muted']}; border: none;
        border-bottom: 2px solid transparent; padding: 10px 16px; }}
    QTabBar::tab:selected {{ color: {c['accent']}; border-bottom-color: {c['accent']}; font-weight: 600; }}
    QTabBar::tab:hover {{ background: {c['hover']}; }}
    QPlainTextEdit, QTableWidget {{ background: {c['panel']}; border: none;
        selection-background-color: {c['accent_soft']}; selection-color: {c['ink']}; }}
    QPlainTextEdit {{ padding: 12px; font-size: {m.caption_size}px; }}
    QHeaderView::section {{ background: {c['paper']}; border: none;
        border-bottom: 1px solid {c['line']}; padding: 8px; color: {c['muted']}; }}
    QTableWidget::item {{ padding: 7px; border-bottom: 1px solid {c['hover']}; }}
    QTableWidget::item:selected {{ background: {c['accent_soft']}; color: {c['ink']}; }}
    QSplitter::handle {{ background: transparent; width: 16px; height: 8px; }}
    QSplitter::handle:hover {{ background: {c['line']}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; border: none; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; border: none; }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {c['line_hover']};
        border-radius: 3px; min-height: 28px; min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QToolTip, QMenu {{ background: {c['panel']}; color: {c['ink']};
        border: 1px solid {c['line']}; padding: 6px; }}
    QMenu::item:selected {{ background: {c['accent_soft']}; }}
    QDialog, QMessageBox {{ background: {c['paper']}; }}
    """
