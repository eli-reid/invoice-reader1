from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget

from invoice_reader.ui.theme import Glyph
from invoice_reader.ui.pages.products import reassign_purchase
from invoice_reader.ui.widgets import (
    DataTable, PageHeader, SearchBox, fmt_money, fmt_price, fmt_qty, make_item, open_path, reveal_in_explorer,
)

METHODS = {"text": "PDF text", "ocr": "OCR", "mixed": "PDF + OCR"}


class InvoicesPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._line_products: dict[int, int] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(12)

        self.header = PageHeader("Invoices", "Every imported invoice and its line items")
        self.header.add_action("Open original", Glyph.OPEN_FILE).clicked.connect(self.open_original)
        self.header.add_action("Show in folder", Glyph.FOLDER_OPEN).clicked.connect(self.reveal)
        self.header.add_action("Delete", Glyph.DELETE).clicked.connect(self.delete)
        lay.addWidget(self.header)

        self.search = SearchBox("Search by invoice #, vendor, file, or item")
        self._debounce = QTimer(self, singleShot=True, interval=250, timeout=self.refresh)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        lay.addWidget(self.search, 0, Qt.AlignLeft)

        splitter = QSplitter(Qt.Vertical)
        self.table = DataTable(["Date", "Vendor", "Invoice #", "Lines", "Lines Total", "Invoice Total", "Read Using",
                                "File", "Imported"])
        self.table.selection_id.connect(self.show_lines)
        self.table.activated_id.connect(lambda _i: self.open_original())
        splitter.addWidget(self.table)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 8, 0, 0)
        self.title = QLabel("Line items")
        self.title.setObjectName("SectionTitle")
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        self.move_btn = QPushButton("Save selected line as...")
        self.move_btn.setEnabled(False)
        self.move_btn.clicked.connect(self.move_line)
        top.addWidget(self.move_btn)
        dl.addLayout(top)
        self.lines = DataTable(["Item #", "Mfr", "Description", "Qty", "UOM", "Price", "Unit Cost", "Extended"])
        self.lines.activated_id.connect(self.open_line_product)
        self.lines.selection_id.connect(lambda hid: self.move_btn.setEnabled(hid is not None))
        self.lines.setToolTip("Double-click to open the product's price history")
        dl.addWidget(self.lines, 1)
        splitter.addWidget(detail)
        splitter.setSizes([420, 320])
        lay.addWidget(splitter, 1)

    def refresh(self) -> None:
        rows = []
        invoices = self.win.db.list_invoices(self.search.text())
        for r in invoices:
            rows.append([
                make_item(r["invoice_date"] or "", r["invoice_date"] or "", item_id=r["id"]),
                make_item(r["vendor"] or ""),
                make_item(r["invoice_number"]),
                make_item(str(r["line_count"]), r["line_count"], align_right=True),
                make_item(fmt_money(r["lines_total"], False), r["lines_total"] or 0, align_right=True),
                make_item(fmt_money(r["total"], False), r["total"] or 0, align_right=True),
                make_item(METHODS.get(r["method"], r["method"] or "")),
                make_item(r["original_name"] or ""),
                make_item((r["imported_at"] or "").replace("T", " "), r["imported_at"] or ""),
            ])
        self.table.set_rows(rows)
        self.table.fit_columns({7: 240})
        self.header.set_subtitle(f"{len(invoices):,} invoice(s)" + (" matching search" if self.search.text() else ""))
        current = self.table.current_id()
        self.show_lines(current)

    def select_invoice(self, invoice_id: int) -> None:
        if self.search.text():
            self.search.clear()
            self.refresh()
        self.table.select_id(invoice_id)
        self.show_lines(invoice_id)

    def show_lines(self, invoice_id) -> None:
        self._line_products = {}
        self.move_btn.setEnabled(False)
        if invoice_id is None:
            self.title.setText("Line items")
            self.lines.set_rows([])
            return
        rows = []
        lines = self.win.db.invoice_lines(invoice_id)
        self._line_products = {line["id"]: line["product_id"] for line in lines}
        for l in lines:
            rows.append([
                make_item(l["item_number"], item_id=l["id"]),
                make_item(l["manufacturer"]),
                make_item(l["description"]),
                make_item(fmt_qty(l["quantity"]), l["quantity"], align_right=True),
                make_item(l["uom"]),
                make_item(fmt_price(l["unit_price"], l["price_per"]), l["unit_cost"], align_right=True),
                make_item(fmt_money(l["unit_cost"]), l["unit_cost"], align_right=True),
                make_item(fmt_money(l["extended"], False), l["extended"] or 0, align_right=True),
            ])
        self.lines.set_rows(rows, keep_selection=False)
        self.lines.fit_columns({2: 420})
        self.title.setText(f"Line items ({len(lines)})")

    def open_line_product(self, history_id) -> None:
        if (product_id := self._line_products.get(history_id)) is not None:
            self.win.show_product(product_id)

    def move_line(self) -> None:
        if (history_id := self.lines.current_id()) is None:
            return
        if reassign_purchase(self.win, history_id, self) is not None:
            self.win.data_changed()

    def _current_file(self) -> str | None:
        inv_id = self.table.current_id()
        if inv_id is None:
            return None
        row = self.win.db.query_one("SELECT file_path FROM invoices WHERE id = ?", (inv_id,))
        path = row["file_path"] if row else None
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "File not found", "The original invoice file could not be found.")
            return None
        return path

    def open_original(self) -> None:
        if path := self._current_file():
            open_path(path)

    def reveal(self) -> None:
        if path := self._current_file():
            reveal_in_explorer(path)

    def delete(self) -> None:
        ids = [i for i in self.table.selected_ids() if i is not None]
        if not ids:
            return
        if QMessageBox.warning(
                self, "Delete invoices",
                f"Delete {len(ids)} invoice(s) and their price history?\nThis cannot be undone.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        for i in ids:
            self.win.db.delete_invoice(i)
        self.win.data_changed()
        self.win.importer.inbox_changed.emit()
