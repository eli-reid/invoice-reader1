from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

# Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) glyphs.
class Glyph:
    HOME = "\ue80f"
    INBOX = "\ue896"
    TAG = "\ue8ec"
    DOCUMENT = "\ue8a5"
    SETTINGS = "\ue713"
    REFRESH = "\ue72c"
    ADD = "\ue710"
    DELETE = "\ue74d"
    SAVE = "\ue74e"
    OPEN_FILE = "\ue8e5"
    FOLDER = "\ue8b7"
    FOLDER_OPEN = "\ue838"
    EXPORT = "\uede1"
    ACCEPT = "\ue8fb"
    CANCEL = "\ue711"
    EDIT = "\ue70f"
    SEARCH = "\ue721"
    ZOOM_IN = "\ue8a3"
    ZOOM_OUT = "\ue71f"
    FIT = "\ue9a6"
    EYE = "\ue890"
    SYNC = "\ue895"
    BACKUP = "\uea35"
    RECEIPT = "\ue8a5"
    WARNING = "\ue7ba"


def icon_font_family() -> str:
    families = set(QFontDatabase.families())
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in families:
            return name
    return ""


def is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorScheme"):
        return hints.colorScheme() == Qt.ColorScheme.Dark
    return QApplication.palette().color(QPalette.Window).lightness() < 128


def accent_color() -> QColor:
    return QApplication.palette().color(QPalette.Highlight)


def glyph_icon(glyph: str, color: QColor | None = None, size: int = 32) -> QIcon:
    family = icon_font_family()
    if not family:
        return QIcon()
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    font = QFont(family)
    font.setPixelSize(int(size * 0.75))
    painter.setFont(font)
    painter.setPen(color or QApplication.palette().color(QPalette.WindowText))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, glyph)
    painter.end()
    return QIcon(pix)


def app_icon() -> QIcon:
    """Rounded accent tile with a receipt glyph."""
    icon = QIcon()
    family = icon_font_family()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0F6CBD"))
        p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        if family:
            f = QFont(family)
            f.setPixelSize(int(size * 0.6))
            p.setFont(f)
            p.setPen(QColor("white"))
            p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, Glyph.RECEIPT)
        p.end()
        icon.addPixmap(pix)
    return icon


STYLESHEET = """
QWidget#Sidebar { background: transparent; }
QListWidget#NavList { background: transparent; border: none; outline: 0; }
QListWidget#NavList::item { padding: 8px 10px; margin: 2px 6px; border-radius: 6px; }
QListWidget#NavList::item:hover { background: rgba(128, 128, 128, 0.12); }
QListWidget#NavList::item:selected { background: rgba(128, 128, 128, 0.22); color: palette(text); }
QLabel#AppTitle { font-size: 11pt; font-weight: 600; padding: 4px 4px 12px 12px; }
QLabel#PageTitle { font-size: 20pt; font-weight: 600; }
QLabel#PageSubtitle { color: #7a7a7a; }
QLabel#SectionTitle { font-size: 12pt; font-weight: 600; }
QFrame#Card { background: palette(base); border: 1px solid rgba(128, 128, 128, 0.25); border-radius: 8px; }
QLabel#CardValue { font-size: 22pt; font-weight: 600; }
QLabel#CardCaption { color: #7a7a7a; }
QLabel#Muted { color: #7a7a7a; }
QFrame#DropZone { border: 2px dashed rgba(128, 128, 128, 0.45); border-radius: 10px; background: transparent; }
QFrame#DropZone[active="true"] { border-color: palette(highlight); background: rgba(15, 108, 189, 0.08); }
QFrame#Banner { border-radius: 6px; padding: 2px; }
QFrame#Banner[kind="warning"] { background: rgba(255, 185, 0, 0.18); border: 1px solid rgba(255, 185, 0, 0.5); }
QFrame#Banner[kind="info"] { background: rgba(15, 108, 189, 0.12); border: 1px solid rgba(15, 108, 189, 0.4); }
QFrame#Banner[kind="success"] { background: rgba(16, 124, 16, 0.14); border: 1px solid rgba(16, 124, 16, 0.45); }
QFrame#Banner[kind="error"] { background: rgba(196, 43, 28, 0.14); border: 1px solid rgba(196, 43, 28, 0.45); }
QTableView { gridline-color: rgba(128, 128, 128, 0.18); }
"""


def apply_theme(app: QApplication) -> None:
    if "windows11" in [k.lower() for k in QStyleFactory.keys()]:
        app.setStyle("windows11")
    elif "windowsvista" in [k.lower() for k in QStyleFactory.keys()]:
        app.setStyle("windowsvista")
    else:
        app.setStyle("Fusion")
    families = set(QFontDatabase.families())
    family = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
    font = QFont(family, 10)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)
