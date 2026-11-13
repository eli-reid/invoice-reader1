from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QTableView, QToolButton, QVBoxLayout, QWidget,
)

from invoice_reader.extraction.pipeline import SUPPORTED_EXTENSIONS
from invoice_reader.models import PRICE_PER_SUFFIX
from invoice_reader.ui.theme import Glyph, glyph_icon

SORT_ROLE = Qt.UserRole + 1
ID_ROLE = Qt.UserRole + 2


def fmt_money(value: float | None, small_precision: bool = True) -> str:
    if value is None:
        return ""
    if small_precision and abs(value) < 10 and round(value, 2) != round(value, 4):
        return f"${value:,.4f}"
    return f"${value:,.2f}"


def fmt_price(price: float | None, per: int | None) -> str:
    if price is None:
        return ""
    return f"{fmt_money(price)}{PRICE_PER_SUFFIX.get(per or 1, f'/{per}')}"


def fmt_qty(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.3f}".rstrip("0").rstrip(".")


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or not old:
        return None
    return (new - old) / old * 100


def fmt_pct(value: float | None) -> str:
    if value is None:
        return ""
    arrow = "\u25b2" if value > 0 else ("\u25bc" if value < 0 else "")
    return f"{arrow} {value:+.1f}%"


def open_path(path: str | Path) -> None:
    os.startfile(str(path))  # noqa: S606 - opening a user's own file with its default app


def reveal_in_explorer(path: str | Path) -> None:
    subprocess.Popen(["explorer", "/select,", str(Path(path))])  # noqa: S603,S607


def make_item(text: str, sort_value=None, item_id=None, align_right: bool = False) -> QStandardItem:
    item = QStandardItem(text)
    item.setEditable(False)
    item.setData(sort_value if sort_value is not None else text.lower(), SORT_ROLE)
    if item_id is not None:
        item.setData(item_id, ID_ROLE)
    if align_right:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


class DataTable(QTableView):
    """Read-only, sortable table backed by a QStandardItemModel."""
    activated_id = Signal(object)
    selection_id = Signal(object)

    def __init__(self, headers: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.model_ = QStandardItemModel(0, len(headers), self)
        self.model_.setHorizontalHeaderLabels(headers)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(SORT_ROLE)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(32)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.doubleClicked.connect(lambda idx: self.activated_id.emit(self._id_at(idx.row())))
        self.selectionModel().currentRowChanged.connect(lambda cur, _p: self.selection_id.emit(self._id_at(cur.row())))

    def _id_at(self, proxy_row: int):
        if proxy_row < 0:
            return None
        src = self.proxy.mapToSource(self.proxy.index(proxy_row, 0))
        return self.model_.item(src.row(), 0).data(ID_ROLE) if src.isValid() else None

    def current_id(self):
        return self._id_at(self.currentIndex().row())

    def selected_ids(self) -> list:
        rows = {i.row() for i in self.selectionModel().selectedRows()}
        return [self._id_at(r) for r in sorted(rows)]

    def set_rows(self, rows: list[list[QStandardItem]], keep_selection: bool = True) -> None:
        current = self.current_id() if keep_selection else None
        header = self.horizontalHeader()
        sort_col, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.setSortingEnabled(False)
        self.model_.removeRows(0, self.model_.rowCount())
        for row in rows:
            self.model_.appendRow(row)
        self.setSortingEnabled(True)
        if 0 <= sort_col < self.model_.columnCount():
            self.sortByColumn(sort_col, sort_order)
        if current is not None:
            self.select_id(current)

    def select_id(self, item_id) -> bool:
        for r in range(self.proxy.rowCount()):
            if self._id_at(r) == item_id:
                self.selectRow(r)
                self.scrollTo(self.proxy.index(r, 0))
                return True
        return False

    def fit_columns(self, widths: dict[int, int] | None = None) -> None:
        self.resizeColumnsToContents()
        for col, w in (widths or {}).items():
            self.setColumnWidth(col, w)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 14, 16, 14)
        self.layout_.setSpacing(6)


class StatCard(Card):
    def __init__(self, caption: str, glyph: str, parent: QWidget | None = None):
        super().__init__(parent)
        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(glyph_icon(glyph, size=20).pixmap(20, 20))
        cap = QLabel(caption)
        cap.setObjectName("CardCaption")
        top.addWidget(icon)
        top.addWidget(cap, 1)
        self.value = QLabel("0")
        self.value.setObjectName("CardValue")
        self.layout_.addLayout(top)
        self.layout_.addWidget(self.value)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, v) -> None:
        self.value.setText(f"{v:,}" if isinstance(v, int) else str(v))


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        text = QVBoxLayout()
        text.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        text.addWidget(t)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setVisible(bool(subtitle))
        text.addWidget(self.subtitle)
        lay.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        lay.addLayout(self.actions)

    def add_action(self, text: str, glyph: str | None = None, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        if glyph:
            btn.setIcon(glyph_icon(glyph, size=16))
        if primary:
            btn.setDefault(True)
            btn.setAutoDefault(False)
        self.actions.addWidget(btn)
        return btn

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


class SearchBox(QLineEdit):
    def __init__(self, placeholder: str = "Search", parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(glyph_icon(Glyph.SEARCH, size=16), QLineEdit.LeadingPosition)
        self.setMinimumWidth(280)


class Banner(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.label, 1)
        close = QToolButton()
        close.setIcon(glyph_icon(Glyph.CANCEL, size=12))
        close.setAutoRaise(True)
        close.clicked.connect(self.hide)
        lay.addWidget(close, 0, Qt.AlignTop)
        self.hide()

    def show_message(self, text: str, kind: str = "info") -> None:
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)
        self.label.setText(text)
        self.show()


class DropZone(QFrame):
    files_dropped = Signal(list)
    browse_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        icon = QLabel()
        icon.setPixmap(glyph_icon(Glyph.INBOX, size=28).pixmap(28, 28))
        icon.setAlignment(Qt.AlignCenter)
        text = QLabel("Drop invoice PDFs or images here")
        text.setAlignment(Qt.AlignCenter)
        btn = QPushButton("Browse files...")
        btn.clicked.connect(self.browse_clicked)
        lay.addWidget(icon)
        lay.addWidget(text)
        lay.addWidget(btn, 0, Qt.AlignCenter)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event):  # noqa: N802
        self._set_active(False)

    def dropEvent(self, event):  # noqa: N802
        self._set_active(False)
        files = collect_files([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        if files:
            self.files_dropped.emit(files)


def collect_files(paths: list[str]) -> list[str]:
    """Expand dropped folders and keep supported invoice files."""
    out: list[str] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(str(f) for f in sorted(p.rglob("*")) if f.suffix.lower() in SUPPORTED_EXTENSIONS)
        elif p.suffix.lower() in SUPPORTED_EXTENSIONS:
            out.append(str(p))
    return out


FILE_FILTER = "Invoices (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp);;All files (*.*)"
