"""Side-by-side review of a parsed invoice: original document on the left,
editable header fields and line items on the right."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from invoice_reader.db import DuplicateInvoiceError
from invoice_reader.extraction.parser import parse_number
from invoice_reader.models import LineItem, ParsedInvoice
from invoice_reader.ui.preview import DocumentPreview
from invoice_reader.ui.theme import Glyph, glyph_icon
from invoice_reader.ui.widgets import Banner, fmt_money

C_MFR, C_ITEM, C_DESC, C_QTY, C_UOM, C_PRICE, C_PER, C_EXT, C_COST, C_CHECK = range(10)
HEADERS = ["Mfr", "Item #", "Description", "Qty", "UOM", "Unit Price", "Per", "Extended", "Unit Cost", ""]
PER_OPTIONS = [("E", 1), ("FT", 1), ("C", 100), ("M", 1000)]
WARN_BRUSH = QBrush(QColor(255, 185, 0, 55))
IGNORED_RESULT = 2


def _num(text: str) -> float | None:
    return parse_number(text.replace(" ", "")) if text.strip() else None


class ReviewDialog(QDialog):
    def __init__(self, parsed: ParsedInvoice, inbox_id: int, importer, preview_path: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.parsed = copy.deepcopy(parsed)
        self.inbox_id = inbox_id
        self.importer = importer
        self.invoice_id: int | None = None
        self._updating = False

        name = Path(parsed.source_path).name if parsed.source_path else "invoice"
        self.setWindowTitle(f"Review invoice - {name}")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1440, 880)

        splitter = QSplitter(Qt.Horizontal)
        self.preview = DocumentPreview()
        splitter.addWidget(self.preview)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        self.banner = Banner()
        rl.addWidget(self.banner)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.vendor = QComboBox()
        self.vendor.setEditable(True)
        self.vendor.addItems(importer.db.vendor_names())
        self.vendor.setCurrentText(parsed.vendor)
        self.vendor.lineEdit().setPlaceholderText("Vendor name")
        self.number = QLineEdit(parsed.invoice_number)
        self.number.setPlaceholderText("Invoice number")
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("MMM d, yyyy")
        d = QDate.fromString(parsed.invoice_date or "", "yyyy-MM-dd")
        self.date.setDate(d if d.isValid() else QDate.currentDate())
        self.total = QDoubleSpinBox()
        self.total.setRange(-1e9, 1e9)
        self.total.setDecimals(2)
        self.total.setPrefix("$ ")
        self.total.setGroupSeparatorShown(True)
        self.total.setValue(parsed.total or 0.0)
        self.total.valueChanged.connect(self._update_summary)
        method = {"text": "Digital PDF text", "ocr": "OCR (scanned)", "mixed": "PDF text + OCR"}.get(parsed.method,
                                                                                                parsed.method)
        src = QLabel(method)
        src.setObjectName("Muted")
        form.addRow("Vendor", self.vendor)
        form.addRow("Invoice #", self.number)
        form.addRow("Invoice date", self.date)
        form.addRow("Invoice total", self.total)
        form.addRow("Read using", src)
        rl.addLayout(form)

        tabs = QTabWidget()
        lines_tab = QWidget()
        ll = QVBoxLayout(lines_tab)
        ll.setContentsMargins(0, 8, 0, 0)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(C_DESC, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        ll.addWidget(self.table, 1)

        row_btns = QHBoxLayout()
        add = QPushButton("Add line")
        add.setIcon(glyph_icon(Glyph.ADD, size=16))
        add.clicked.connect(lambda: self._add_row(LineItem(confidence="low"), edit=True))
        remove = QPushButton("Remove selected")
        remove.setIcon(glyph_icon(Glyph.DELETE, size=16))
        remove.clicked.connect(self._remove_rows)
        QShortcut(QKeySequence.Delete, self.table, activated=self._remove_rows)
        row_btns.addWidget(add)
        row_btns.addWidget(remove)
        row_btns.addStretch(1)
        self.summary = QLabel()
        row_btns.addWidget(self.summary)
        ll.addLayout(row_btns)
        tabs.addTab(lines_tab, "Line items")

        raw = QPlainTextEdit(parsed.raw_text)
        raw.setReadOnly(True)
        raw.setLineWrapMode(QPlainTextEdit.NoWrap)
        raw.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace;")
        tabs.addTab(raw, "Extracted text")
        rl.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        ignore = QPushButton("Not an invoice / ignore")
        ignore.setIcon(glyph_icon(Glyph.CANCEL, size=16))
        ignore.clicked.connect(self._ignore)
        cancel = QPushButton("Review later")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save to database")
        save.setIcon(glyph_icon(Glyph.ACCEPT, size=16))
        save.setDefault(True)
        save.clicked.connect(self._save)
        bottom.addWidget(ignore)
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        rl.addLayout(bottom)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)
        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        for line in self.parsed.lines:
            self._add_row(line)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(C_ITEM, max(self.table.columnWidth(C_ITEM), 140))
        self.table.setColumnWidth(C_PER, 70)
        self._update_summary()
        if parsed.warnings:
            self.banner.show_message("Please check: " + " ".join(parsed.warnings), "warning")
        else:
            self.banner.show_message("Everything checks out. Review and save.", "success")
        self.preview.load(preview_path or parsed.source_path)

    # ---- table ----------------------------------------------------------
    def _add_row(self, line: LineItem, edit: bool = False) -> None:
        self._updating = True
        r = self.table.rowCount()
        self.table.insertRow(r)
        values = {
            C_MFR: line.manufacturer, C_ITEM: line.item_number, C_DESC: line.description,
            C_QTY: f"{line.quantity:g}" if line.quantity else "",
            C_UOM: line.uom, C_PRICE: f"{line.unit_price:g}" if line.unit_price else "",
            C_EXT: f"{line.extended:.2f}" if line.extended is not None else "",
        }
        for col in range(len(HEADERS)):
            item = QTableWidgetItem(values.get(col, ""))
            if col in (C_QTY, C_PRICE, C_EXT, C_COST):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if col in (C_COST, C_CHECK):
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if col == C_ITEM:
                item.setToolTip(line.raw or "")
            self.table.setItem(r, col, item)
        combo = QComboBox()
        for label, per in PER_OPTIONS:
            combo.addItem(label, per)
        combo.setCurrentIndex(max(0, combo.findData(line.price_per or 1)))
        if (line.price_per or 1) == 1 and line.uom.upper() in ("FT", "LF"):
            combo.setCurrentIndex(combo.findText("FT"))
        combo.setToolTip("Pricing basis: E = each, FT = per foot, C = per 100, M = per 1000")
        combo.currentIndexChanged.connect(lambda _i, c=combo: self._recalc_row(self._row_of(c)))
        self.table.setCellWidget(r, C_PER, combo)
        self._updating = False
        self._recalc_row(r)
        if edit:
            self.table.setCurrentCell(r, C_ITEM)
            self.table.editItem(self.table.item(r, C_ITEM))

    def _row_of(self, widget: QWidget) -> int:
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, C_PER) is widget:
                return r
        return -1

    def _remove_rows(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._update_summary()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if not self._updating and item.column() in (C_QTY, C_PRICE, C_EXT):
            self._recalc_row(item.row())

    def _row_line(self, r: int) -> LineItem:
        def text(c: int) -> str:
            it = self.table.item(r, c)
            return it.text().strip() if it else ""

        combo = self.table.cellWidget(r, C_PER)
        return LineItem(
            item_number=text(C_ITEM).upper(), description=text(C_DESC),
            quantity=_num(text(C_QTY)) or 0.0, uom=(text(C_UOM) or "EA").upper(),
            unit_price=_num(text(C_PRICE)) or 0.0, price_per=combo.currentData() if combo else 1,
            extended=_num(text(C_EXT)),
            raw=(self.table.item(r, C_ITEM).toolTip() if self.table.item(r, C_ITEM) else ""),
            manufacturer=text(C_MFR).upper(),
        )

    def _recalc_row(self, r: int) -> None:
        if r < 0 or self._updating:
            return
        self._updating = True
        line = self._row_line(r)
        self.table.item(r, C_COST).setText(fmt_money(line.unit_cost) if line.unit_price else "")
        ok = bool(line.item_number and line.unit_price > 0 and line.quantity)
        check = self.table.item(r, C_CHECK)
        if ok and line.extended is not None and abs(line.computed_extended - line.extended) > max(
                0.02, abs(line.extended) * 0.005):
            ok = False
            check.setToolTip(f"Qty x price = {fmt_money(line.computed_extended, False)}, "
                             f"but extended is {fmt_money(line.extended, False)}")
        elif not ok:
            check.setToolTip("Item #, quantity and unit price are required")
        else:
            check.setToolTip("Verified")
        check.setText("\u2713" if ok else "\u26a0")
        brush = QBrush() if ok else WARN_BRUSH
        for c in range(len(HEADERS)):
            self.table.item(r, c).setBackground(brush)
        self._updating = False
        self._update_summary()

    def _lines(self) -> list[LineItem]:
        lines = []
        for r in range(self.table.rowCount()):
            line = self._row_line(r)
            line.confidence = "high" if self.table.item(r, C_CHECK).text() == "\u2713" else "low"
            lines.append(line)
        return lines

    def _update_summary(self) -> None:
        if self._updating:
            return
        lines = self._lines()
        total = round(sum(l.extended if l.extended is not None else l.computed_extended for l in lines), 2)
        flagged = sum(1 for l in lines if l.confidence != "high")
        parts = [f"{len(lines)} line(s)", f"lines total {fmt_money(total, False)}"]
        if self.total.value():
            parts.append(f"invoice total {fmt_money(self.total.value(), False)}")
        if flagged:
            parts.append(f"<span style='color:#c42b1c'>{flagged} need attention</span>")
        self.summary.setText(" \u2022 ".join(parts))

    # ---- actions --------------------------------------------------------
    def result_invoice(self) -> ParsedInvoice:
        inv = copy.deepcopy(self.parsed)
        inv.vendor = self.vendor.currentText().strip()
        inv.invoice_number = self.number.text().strip()
        inv.invoice_date = self.date.date().toString("yyyy-MM-dd")
        inv.total = self.total.value() or None
        inv.lines = self._lines()
        return inv

    def _ignore(self) -> None:
        self.importer.ignore(self.inbox_id)
        self.done(IGNORED_RESULT)

    def _save(self) -> None:
        inv = self.result_invoice()
        if not inv.vendor:
            QMessageBox.warning(self, "Vendor required", "Enter the vendor name before saving.")
            self.vendor.setFocus()
            return
        valid = [l for l in inv.lines if l.item_number and l.unit_price > 0]
        if not valid:
            QMessageBox.warning(self, "No line items", "Add at least one line with an item # and unit price.")
            return
        if len(valid) < len(inv.lines):
            skipped = len(inv.lines) - len(valid)
            if QMessageBox.question(self, "Incomplete lines",
                                    f"{skipped} line(s) are missing an item # or price and will be skipped. "
                                    "Continue?") != QMessageBox.Yes:
                return
        inv.lines = valid
        if not inv.invoice_number and QMessageBox.question(
                self, "No invoice number",
                "This invoice has no invoice number, so duplicates can't be detected. Save anyway?") != QMessageBox.Yes:
            self.number.setFocus()
            return
        try:
            self.invoice_id = self.importer.commit(self.inbox_id, inv)
        except DuplicateInvoiceError:
            answer = QMessageBox.question(
                self, "Duplicate invoice",
                f"Invoice #{inv.invoice_number} from {inv.vendor} is already in the database.\n\n"
                "Import it again anyway? (Choose No to keep the existing one.)")
            if answer != QMessageBox.Yes:
                return
            self.invoice_id = self.importer.commit(self.inbox_id, inv, allow_duplicate=True)
        except Exception as exc:  # noqa: BLE001 - surface any save problem to the user
            QMessageBox.critical(self, "Could not save", f"The invoice could not be saved:\n\n{exc}")
            return
        self.accept()
