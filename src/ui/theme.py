from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


COLORS = {
    "window": "#0B0F17",
    "sidebar": "#0F1520",
    "surface": "#141C29",
    "surface_alt": "#192333",
    "border": "#263349",
    "text": "#F3F6FC",
    "muted": "#93A0B4",
    "accent": "#7392FF",
    "accent_hover": "#86A1FF",
    "success": "#45D69B",
    "warning": "#F4BC5E",
    "danger": "#FF7185",
}


STYLESHEET = f"""
* {{
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
    color: {COLORS['text']};
}}
QMainWindow, QWidget#AppRoot, QStackedWidget#PageStack {{
    background: {COLORS['window']};
}}
QWidget#Sidebar {{
    background: {COLORS['sidebar']};
    border-right: 1px solid {COLORS['border']};
}}
QLabel#BrandTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#BrandCaption, QLabel#Muted, QLabel#PageSubtitle, QLabel#CardDescription {{
    color: {COLORS['muted']};
}}
QLabel#PageSubtitle {{ font-size: 13px; }}
QLabel#PageTitle {{ font-size: 27px; font-weight: 700; }}
QLabel#SectionTitle {{ font-size: 17px; font-weight: 650; }}
QLabel#CardTitle {{ font-size: 16px; font-weight: 650; }}
QLabel#LaunchIcon {{ font-size: 27px; font-weight: 500; color: #DCE5FF; }}
QLabel#MetricValue {{ font-size: 18px; font-weight: 700; }}
QLabel#StatusPill {{
    border-radius: 10px; padding: 4px 10px; font-size: 12px; font-weight: 600;
}}
QLabel#StatusPill[tone="success"] {{ background: #143A32; color: {COLORS['success']}; border: 1px solid {COLORS['success']}; }}
QLabel#StatusPill[tone="warning"] {{ background: #3B2D17; color: {COLORS['warning']}; border: 1px solid {COLORS['warning']}; }}
QLabel#StatusPill[tone="danger"] {{ background: #3A1D27; color: {COLORS['danger']}; border: 1px solid {COLORS['danger']}; }}
QLabel#StatusPill[tone="neutral"] {{ background: #202B3E; color: #AAB5C6; border: 1px solid #AAB5C6; }}
QLabel#StatusPill[tone="accent"] {{ background: #24345B; color: #AFC0FF; border: 1px solid #AFC0FF; }}
QLabel#NavSection {{
    color: #66748A;
    font-size: 11px;
    font-weight: 700;
    padding: 14px 12px 4px 12px;
}}
QPushButton {{
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    background: {COLORS['surface_alt']};
    padding: 8px 15px;
    min-height: 22px;
}}
QPushButton:hover {{ border-color: #3A4B68; background: #202C40; }}
QPushButton:pressed {{ background: #111927; }}
QPushButton:disabled {{ color: #5E697A; background: #111722; border-color: #202B3D; }}
QPushButton#PrimaryButton {{
    background: {COLORS['accent']}; color: #08101F; border: none; font-weight: 700; padding: 10px 18px;
}}
QPushButton#PrimaryButton:hover {{ background: {COLORS['accent_hover']}; }}
QPushButton#PrimaryButton:disabled {{ background: #26334B; color: #68758A; border: none; }}
QPushButton#DangerButton {{ background: #3A1822; color: #FF9AAA; border-color: #673040; font-weight: 650; }}
QPushButton#DangerButton:disabled {{ background: #211821; color: #6F5962; border-color: #34232C; }}
QPushButton#NavButton {{
    text-align: left; border: none; border-radius: 7px; background: transparent;
    color: #AAB5C6; padding: 9px 13px; min-height: 22px;
}}
QPushButton#NavButton:hover {{ background: #172131; color: {COLORS['text']}; }}
QPushButton#NavButton:checked {{ background: #203052; color: #DCE5FF; font-weight: 650; }}
QFrame#Card, QFrame#MetricCard, QFrame#RunPanel {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 11px;
}}
QFrame#AccentCard {{
    background: #17223A; border: 1px solid #344D82; border-radius: 11px;
}}
QFrame#Divider {{ background: {COLORS['border']}; min-height: 1px; max-height: 1px; border: none; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: #0F1622; border: 1px solid {COLORS['border']}; border-radius: 7px;
    padding: 7px 10px; min-height: 22px; selection-background-color: {COLORS['accent']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {COLORS['accent']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid #40506B; border-radius: 4px; background: #0F1622; }}
QCheckBox::indicator:checked {{ background: {COLORS['accent']}; border-color: {COLORS['accent']}; }}
QPlainTextEdit, QListWidget, QTableWidget {{
    background: #0D131D; border: 1px solid {COLORS['border']}; border-radius: 8px;
    alternate-background-color: #111927; selection-background-color: #253A67;
}}
QListWidget::item {{ padding: 6px 8px; }}
QHeaderView::section {{
    background: #172131; color: #AEB9CA; border: none; border-bottom: 1px solid {COLORS['border']};
    padding: 9px 8px; font-weight: 650;
}}
QTableWidget {{ gridline-color: #1F2A3B; padding: 0; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #334158; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #334158; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QProgressBar {{ border: none; border-radius: 4px; background: #0E1520; height: 7px; text-align: center; }}
QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 4px; }}
QToolTip {{ background: #202B3E; border: 1px solid #40506B; color: white; padding: 5px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0D131D"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
