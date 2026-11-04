"""Colours and the logo. The two blues come from the SkyScouter logo."""
from __future__ import annotations

from importlib import resources

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap

BRAND = "#0295E0"  # the chart line; passes the colour validator against SURFACE
BRAND_DARK = "#00558E"
BRAND_DARKER = "#00436F"
SURFACE = "#FFFFFF"
BACKGROUND = "#F4F6F8"
BORDER = "#D9DEE3"
GRID = "#E8EBEE"
INK = "#14202B"
INK_MUTED = "#5A6772"
CHANGED = "#E3F2FB"
INVALID = "#FBE4E4"
LOCKED = "#F1F3F5"
GOOD = "#1B7F3B"
WARNING = "#8A5A00"
WARNING_BG = "#FFF4D6"
CRITICAL = "#B3261E"
DEMO_BG = "#EDE7F6"
DEMO = "#4A2C8F"
MONO = "'DejaVu Sans Mono', 'Ubuntu Mono', monospace"

QSS = f"""
QWidget {{ color: {INK}; font-size: 13px; }}
QMainWindow, QDialog, QStackedWidget > QWidget {{ background: {BACKGROUND}; }}
QLabel {{ background: transparent; }}
#Header {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
#AppTitle {{ font-size: 16px; font-weight: 600; color: {BRAND_DARK}; }}
#PageTitle {{ font-size: 18px; font-weight: 600; }}
#Muted {{ color: {INK_MUTED}; }}
#Nav {{ background: {SURFACE}; border: none; border-right: 1px solid {BORDER}; outline: 0; padding-top: 8px; }}
#Nav::item {{ padding: 10px 16px; color: {INK}; }}
#Nav::item:selected {{ background: {CHANGED}; color: {BRAND_DARK}; font-weight: 600;
                       border-left: 3px solid {BRAND_DARK}; }}
#Nav::item:disabled {{ color: #A4ADB5; }}
#Tile, #Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
#TileLabel {{ color: {INK_MUTED}; font-size: 12px; }}
#TileValue {{ font-size: 22px; font-weight: 600; }}
QPushButton {{ background: {SURFACE}; border: 1px solid #B8C1C9; border-radius: 4px; padding: 6px 12px; }}
QPushButton:hover {{ background: {BACKGROUND}; }}
QPushButton:focus {{ border: 1px solid {BRAND_DARK}; }}
QPushButton:disabled {{ color: #A4ADB5; border-color: {BORDER}; background: {BACKGROUND}; }}
QPushButton#Primary {{ background: {BRAND_DARK}; color: white; border: 1px solid {BRAND_DARK}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {BRAND_DARKER}; }}
QPushButton#Primary:disabled {{ background: #B8C1C9; border-color: #B8C1C9; color: white; }}
QLineEdit, QComboBox, QPlainTextEdit {{ background: {SURFACE}; border: 1px solid #B8C1C9; border-radius: 4px;
                                         padding: 4px 6px; selection-background-color: {BRAND_DARK}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {BRAND_DARK}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; selection-background-color: {CHANGED};
                               selection-color: {INK}; }}
QPlainTextEdit#Output {{ font-family: {MONO}; font-size: 12px; }}
QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; gridline-color: {GRID};
                selection-background-color: {CHANGED}; selection-color: {INK}; }}
QHeaderView::section {{ background: {BACKGROUND}; border: none; border-bottom: 1px solid {BORDER};
                        padding: 6px; font-weight: 600; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 8px 16px; background: transparent; }}
QTabBar::tab:selected {{ color: {BRAND_DARK}; font-weight: 600; border-bottom: 2px solid {BRAND_DARK}; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
#Banner[kind="warning"] {{ background: {WARNING_BG}; border-bottom: 1px solid #E8D28F; }}
#Banner[kind="info"] {{ background: {CHANGED}; border-bottom: 1px solid #BBDDF2; }}
#Banner[kind="demo"] {{ background: {DEMO_BG}; border-bottom: 1px solid #CFC3E8; }}
"""


def _logo_bytes() -> bytes:
    return resources.files("sky_programmer.gui").joinpath("assets/logo.png").read_bytes()


def logo_pixmap(height: int, scale: float = 1.0) -> QPixmap:
    """The logo at this height in screen units, sharp on a high-DPI screen."""
    pix = QPixmap()
    pix.loadFromData(QByteArray(_logo_bytes()))
    pix = pix.scaledToHeight(int(height * scale), Qt.TransformationMode.SmoothTransformation)
    pix.setDevicePixelRatio(scale)
    return pix


def icon_image(size: int) -> QImage:
    """The logo centred on a transparent square, since desktop icons must be square."""
    src = QImage()
    src.loadFromData(QByteArray(_logo_bytes()))
    src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.drawImage((size - src.width()) // 2, (size - src.height()) // 2, src)
    painter.end()
    return out


def app_icon() -> QIcon:
    return QIcon(QPixmap.fromImage(icon_image(256)))
