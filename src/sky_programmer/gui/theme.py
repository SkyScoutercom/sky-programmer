"""Day and night colours, the stylesheet, icons and the logo. The blues come from the SkyScouter logo."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).parent / "assets"
MONO = "'DejaVu Sans Mono', 'Ubuntu Mono', monospace"


@dataclass(frozen=True)
class Palette:
    name: str
    brand: str  # the chart line; validated against `surface` with the dataviz palette checker
    primary: str
    primary_hover: str
    on_primary: str
    background: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    grid: str
    ink: str
    muted: str
    disabled: str
    accent_bg: str
    accent_ink: str
    changed: str
    invalid: str
    locked: str
    good: str
    critical: str
    warning_ink: str
    warning_bg: str
    warning_border: str
    info_bg: str
    info_border: str
    demo_ink: str
    demo_bg: str
    demo_border: str


DAY = Palette(
    name="day", brand="#0295E0", primary="#00558E", primary_hover="#00436F", on_primary="#FFFFFF",
    background="#F3F5F8", surface="#FFFFFF", surface_alt="#F8FAFB", border="#E1E6EB", border_strong="#C9D1D8",
    grid="#EDF0F3", ink="#14202B", muted="#56636F", disabled="#8E99A3", accent_bg="#E5F3FC", accent_ink="#00558E",
    changed="#E3F2FB", invalid="#FBE4E4", locked="#F3F5F7", good="#1B7F3B", critical="#B3261E", warning_ink="#6B4500",
    warning_bg="#FFF4D6", warning_border="#EFD98F", info_bg="#E5F3FC", info_border="#BDDFF5", demo_ink="#4A2C8F",
    demo_bg="#EFE9F9", demo_border="#D4C8EC",
)  # fmt: skip
NIGHT = Palette(
    name="night", brand="#0295E0", primary="#2AA9EC", primary_hover="#58BDF2", on_primary="#06202F",
    background="#12181E", surface="#1B232B", surface_alt="#222C35", border="#2E3A45", border_strong="#44525F",
    grid="#27323C", ink="#E8EDF2", muted="#A3B0BC", disabled="#75818C", accent_bg="#16354A", accent_ink="#8AD0F6",
    changed="#16354A", invalid="#4A2326", locked="#232C34", good="#5FCF85", critical="#FF8A80", warning_ink="#F2CE73",
    warning_bg="#3B2F12", warning_border="#5C4A1C", info_bg="#16354A", info_border="#245070", demo_ink="#C9B8F5",
    demo_bg="#2A2240", demo_border="#43366A",
)  # fmt: skip
PALETTES = {"day": DAY, "night": NIGHT}

current: Palette = DAY  # widgets that paint themselves read this at paint time


def _asset(name: str) -> str:
    return (ASSETS / name).as_posix()


def qss(c: Palette) -> str:
    chevron, check = _asset(f"chevron-{c.name}.svg"), _asset(f"check-{c.name}.svg")
    return f"""
QWidget {{ color: {c.ink}; font-size: 13px; }}
QWidget:disabled {{ color: {c.disabled}; }}
QMainWindow, QDialog, QMessageBox, #Pages, #Pages > QWidget {{ background: {c.background}; }}
QLabel {{ background: transparent; }}
QToolTip {{ background: {c.surface}; color: {c.ink}; border: 1px solid {c.border_strong}; padding: 4px 6px; }}

#Header {{ background: {c.surface}; border-bottom: 1px solid {c.border}; }}
#AppTitle {{ font-size: 16px; font-weight: 700; color: {c.accent_ink}; }}
#PageTitle {{ font-size: 20px; font-weight: 700; }}
#Muted {{ color: {c.muted}; }}
#LogoChip {{ background: #FFFFFF; border-radius: 10px; padding: 3px 7px; }}
#LogoChipLarge {{ background: #FFFFFF; border-radius: 18px; padding: 16px 22px; }}

#Pill {{ border-radius: 12px; padding: 3px 11px; font-weight: 600; background: {c.surface_alt};
         color: {c.muted}; border: 1px solid {c.border}; }}
#Pill[state="ok"] {{ color: {c.good}; }}
#Pill[state="busy"] {{ color: {c.accent_ink}; }}

#Nav {{ background: {c.surface}; border: none; border-right: 1px solid {c.border}; outline: 0; padding: 10px 8px; }}
#Nav::item {{ padding: 9px 10px; margin: 2px 0; border-radius: 10px; color: {c.ink}; }}
#Nav::item:hover {{ background: {c.surface_alt}; }}
#Nav::item:selected {{ background: {c.accent_bg}; color: {c.accent_ink}; font-weight: 600; }}
#Nav::item:disabled {{ color: {c.disabled}; }}

#Tile, #Card {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 14px; }}
#TileLabel {{ color: {c.muted}; font-size: 12px; }}
#TileValue {{ font-size: 21px; font-weight: 700; }}
#TileValue[status="good"] {{ color: {c.good}; }}
#TileValue[status="critical"] {{ color: {c.critical}; }}

QPushButton {{ background: {c.surface}; border: 1px solid {c.border_strong}; border-radius: 10px;
               padding: 7px 15px; font-weight: 600; }}
QPushButton:hover {{ background: {c.surface_alt}; border-color: {c.primary}; }}
QPushButton:pressed {{ background: {c.accent_bg}; }}
QPushButton:focus {{ border: 1px solid {c.primary}; }}
QPushButton:disabled {{ color: {c.disabled}; border-color: {c.border}; background: {c.surface_alt}; }}
QPushButton#Primary {{ background: {c.primary}; color: {c.on_primary}; border: 1px solid {c.primary}; }}
QPushButton#Primary:hover {{ background: {c.primary_hover}; border-color: {c.primary_hover}; }}
QPushButton#Primary:disabled {{ background: {c.surface_alt}; border-color: {c.border}; color: {c.disabled}; }}
QPushButton#Danger {{ color: {c.critical}; border-color: {c.critical}; }}
QPushButton#Danger:hover {{ background: {c.invalid}; border-color: {c.critical}; }}
QPushButton#Danger:disabled {{ color: {c.disabled}; border-color: {c.border}; }}
QPushButton#ThemeToggle {{ border-radius: 16px; padding: 6px 14px; }}

QLineEdit, QPlainTextEdit {{ background: {c.surface}; border: 1px solid {c.border_strong}; border-radius: 10px;
                             padding: 6px 10px; selection-background-color: {c.primary};
                             selection-color: {c.on_primary}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {c.primary}; }}
QLineEdit:disabled {{ color: {c.disabled}; background: {c.surface_alt}; }}
QPlainTextEdit#Output {{ font-family: {MONO}; font-size: 12px; border-radius: 14px; padding: 10px;
                         border: 1px solid {c.border}; }}

QComboBox {{ background: {c.surface}; border: 1px solid {c.border_strong}; border-radius: 10px;
             padding: 6px 30px 6px 12px; min-height: 20px; }}
QComboBox:hover, QComboBox:focus {{ border-color: {c.primary}; }}
QComboBox:disabled {{ color: {c.disabled}; background: {c.surface_alt}; border-color: {c.border}; }}
QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: center right; width: 30px; border: none;
                        background: transparent; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 13px; height: 13px; }}
QComboBox QLineEdit {{ border: none; background: transparent; padding: 0; border-radius: 0; }}
QComboBox QAbstractItemView {{ background: {c.surface}; border: 1px solid {c.border_strong}; border-radius: 10px;
                               padding: 4px; outline: 0; selection-background-color: {c.accent_bg};
                               selection-color: {c.accent_ink}; }}
QTableWidget QComboBox {{ background: transparent; border: none; border-radius: 0; padding: 0 28px 0 6px; }}

QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 6px; border: 1px solid {c.border_strong};
                        background: {c.surface}; }}
QCheckBox::indicator:hover {{ border-color: {c.primary}; }}
QCheckBox::indicator:checked {{ background: {c.primary}; border-color: {c.primary}; image: url("{check}"); }}

QTableWidget {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 14px; gridline-color: {c.grid};
                selection-background-color: {c.grid}; selection-color: {c.ink}; outline: 0; }}
QTableWidget::item {{ padding: 2px 6px; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {c.surface_alt}; color: {c.muted}; border: none;
                        border-bottom: 1px solid {c.border}; padding: 8px; font-weight: 600; }}
QHeaderView::section:first {{ border-top-left-radius: 13px; }}
QHeaderView::section:last {{ border-top-right-radius: 13px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 7px 14px; margin-right: 4px; border-radius: 10px; background: transparent; color: {c.muted}; }}
QTabBar::tab:hover {{ background: {c.surface_alt}; }}
QTabBar::tab:selected {{ background: {c.accent_bg}; color: {c.accent_ink}; font-weight: 600; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle {{ background: {c.border_strong}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: {c.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0; }}

QStatusBar {{ background: {c.surface}; border-top: 1px solid {c.border}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {c.muted}; }}
QProgressBar {{ background: {c.grid}; border: none; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: {c.brand}; border-radius: 4px; }}

#Banner {{ border-radius: 12px; margin: 10px 14px 0 14px; }}
#Banner[kind="warning"] {{ background: {c.warning_bg}; border: 1px solid {c.warning_border}; }}
#Banner[kind="warning"] QLabel {{ color: {c.warning_ink}; }}
#Banner[kind="info"] {{ background: {c.info_bg}; border: 1px solid {c.info_border}; }}
#Banner[kind="demo"] {{ background: {c.demo_bg}; border: 1px solid {c.demo_border}; }}
#Banner[kind="demo"] QLabel, #Banner[kind="demo"] QCheckBox {{ color: {c.demo_ink}; }}
"""


def _qt_palette(c: Palette) -> QPalette:
    """For what a stylesheet doesn't reach, such as message boxes and file dialogs."""
    pal = QPalette()
    roles = {QPalette.ColorRole.Window: c.background, QPalette.ColorRole.WindowText: c.ink,
             QPalette.ColorRole.Base: c.surface, QPalette.ColorRole.AlternateBase: c.surface_alt,
             QPalette.ColorRole.Text: c.ink, QPalette.ColorRole.Button: c.surface,
             QPalette.ColorRole.ButtonText: c.ink, QPalette.ColorRole.ToolTipBase: c.surface,
             QPalette.ColorRole.ToolTipText: c.ink, QPalette.ColorRole.Highlight: c.primary,
             QPalette.ColorRole.HighlightedText: c.on_primary, QPalette.ColorRole.PlaceholderText: c.muted,
             QPalette.ColorRole.Link: c.accent_ink}  # fmt: skip
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.disabled))
    return pal


def apply(name: str) -> Palette:
    """Switch the whole app to "day" or "night"."""
    global current
    current = PALETTES.get(name, DAY)
    app = QApplication.instance()
    if app is not None:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if current is NIGHT else Qt.ColorScheme.Light)
        app.setPalette(_qt_palette(current))
        sheet = qss(current)
        if app.styleSheet() != sheet:  # re-applying restyles every widget, which is slow and changes nothing
            app.setStyleSheet(sheet)
    return current


def system_theme() -> str:
    app = QApplication.instance()
    if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
        return "night"
    return "day"


# ---------------------------------------------------------------------- icons
_NAV_ICONS = {
    "Dashboard": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l4.5-5.5"/><circle cx="12" cy="16" r="1.2"/>',
    "Configuration": '<path d="M4 7h9M18 7h2M4 17h2M11 17h9"/><circle cx="15.5" cy="7" r="2.3"/>'
                     '<circle cx="8.5" cy="17" r="2.3"/>',
    "Backups": '<rect x="3" y="4" width="18" height="5" rx="1.6"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/>'
               '<path d="M10 13.5h4"/>',
    "Console": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9.5l3 2.5-3 2.5M13 15h4"/>',
    "Updates": '<circle cx="6" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="7.5" r="2.2"/>'
               '<path d="M6 7.7v8.6M18 9.7c0 4.3-6.5 3-10.2 6.6"/>',
}  # fmt: skip


def _svg_pixmap(body: str, color: str, size: int = 40) -> QPixmap:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')  # fmt: skip
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pix


def nav_icon(name: str) -> QIcon:
    icon = QIcon()
    body = _NAV_ICONS[name]
    icon.addPixmap(_svg_pixmap(body, current.muted), QIcon.Mode.Normal)
    icon.addPixmap(_svg_pixmap(body, current.accent_ink), QIcon.Mode.Selected)
    icon.addPixmap(_svg_pixmap(body, current.disabled), QIcon.Mode.Disabled)
    return icon


# ---------------------------------------------------------------------- the logo
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
