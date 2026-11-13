from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from invoice_reader.backends import DB_ERRORS
from invoice_reader.ui.chart import PriceChart
from invoice_reader.db import item_key
from invoice_reader.models import emt_item_number
from invoice_reader.ui.theme import Glyph, glyph_icon
from invoice_reader.ui.widgets import (
    DataTable, PageHeader, SearchBox, fmt_money, fmt_pct, fmt_price, fmt_qty, make_item, pct_change,
)

UP = QColor("#c42b1c")
DOWN = QColor("#107c10")
log = logging.getLogger(__name__)


class ProductDialog(QDialog):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit product - {row['item_number']}")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        self.item_number = QLineEdit(row["item_number"])
        self.manufacturer = QLineEdit(row["manufacturer"])
        self.manufacturer.setPlaceholderText("e.g. LEV, RAB, SQD")
        self.name = QLineEdit(row["name"])
        self.category = QLineEdit(row["category"])
        self.category.setPlaceholderText("e.g. Breakers, Wire, Conduit, Boxes")
        self.notes = QPlainTextEdit(row["notes"])
        self.notes.setFixedHeight(90)
        form.addRow("Item #", self.item_number)
        form.addRow("Manufacturer", self.manufacturer)
        form.addRow("Name", self.name)
        form.addRow("Category", self.category)
        form.addRow("Notes", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        if not item_key(self.item_number.text()):
            QMessageBox.warning(self, "Invalid item #", "Enter a valid item #.")
            return
        super().accept()


class PurchaseDialog(QDialog):
    def __init__(self, win, row, parent=None):
        super().__init__(parent)
        self.win = win
        self.row = row
        self.product_id: int | None = None
        self.setWindowTitle("Save purchase as a different product")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        description = QLabel(row["description"])
        description.setWordWrap(True)
        form.addRow("Invoice description", description)
        self.products = win.db.list_products()
        self.existing = QComboBox()
        self.existing.addItem("New product / enter item # below", None)
        for product in self.products:
            self.existing.addItem(
                f"{product['manufacturer']} {product['item_number']} - {product['name']}".strip(), product["id"])
        form.addRow("Save as", self.existing)
        self.item_number = QLineEdit(emt_item_number(row["item_number"], row["description"]) or row["item_number"])
        self.manufacturer = QLineEdit(row["manufacturer"])
        self.name = QLineEdit(row["description"] or row["product_name"])
        form.addRow("Item #", self.item_number)
        form.addRow("Manufacturer", self.manufacturer)
        form.addRow("Product name", self.name)
        hint = QLabel("Only this purchase moves. Invoice descriptions and prices stay unchanged.\n"
                      "Existing products keep their current name and details.")
        hint.setWordWrap(True)
        form.addRow(hint)
        self.existing.currentIndexChanged.connect(self._choose_product)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _choose_product(self, index: int) -> None:
        if index > 0:
            product = self.products[index - 1]
            self.item_number.setText(product["item_number"])
            self.manufacturer.setText(product["manufacturer"])
            self.name.setText(product["name"])
        for field in (self.item_number, self.manufacturer, self.name):
            field.setReadOnly(index > 0)

    def accept(self) -> None:
        try:
            self.product_id = self.win.db.reassign_purchase(
                self.row["id"], self.item_number.text(), self.manufacturer.text(), self.name.text(),
                target_product_id=self.existing.currentData())
        except (ValueError, *DB_ERRORS) as exc:
            log.exception("Could not reassign purchase %s", self.row["id"])
            QMessageBox.warning(self, "Could not save purchase", str(exc))
            return
        super().accept()


def reassign_purchase(win, history_id: int, parent) -> int | None:
    row = win.db.query_one(
        "SELECT ph.*, p.item_number, p.manufacturer, p.name AS product_name "
        "FROM price_history ph JOIN products p ON p.id = ph.product_id WHERE ph.id = ?", (history_id,))
    if row is None:
        QMessageBox.warning(parent, "Purchase not found", "This purchase no longer exists.")
        return None
    dlg = PurchaseDialog(win, row, parent)
    return dlg.product_id if dlg.exec() == QDialog.Accepted else None


class ProductsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._current: int | None = None
        self._history_invoices: dict[int, int] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(12)

        self.header = PageHeader("Products", "Every item # seen on an invoice with its price history")
        self.header.add_action("Export CSV", Glyph.EXPORT).clicked.connect(self.export)
        lay.addWidget(self.header)

        self.search = SearchBox("Search by item #, name, manufacturer or category")
        self._debounce = QTimer(self, singleShot=True, interval=250, timeout=self.refresh)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        lay.addWidget(self.search, 0, Qt.AlignLeft)

        splitter = QSplitter(Qt.Vertical)
        self.table = DataTable(["Item #", "Mfr", "Name", "Category", "Last Price", "Unit Cost", "Change", "Low", "High",
                                "Last Vendor", "Last Purchased", "Purchases"])
        self.table.selection_id.connect(self.show_detail)
        self.table.activated_id.connect(lambda _i: self.edit_product())
        splitter.addWidget(self.table)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 8, 0, 0)
        top = QHBoxLayout()
        self.title = QLabel("Select a product")
        self.title.setObjectName("SectionTitle")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.title, 1)
        self.edit_btn = QPushButton("Edit details")
        self.edit_btn.setIcon(glyph_icon(Glyph.EDIT, size=16))
        self.edit_btn.clicked.connect(self.edit_product)
        self.edit_btn.setEnabled(False)
        top.addWidget(self.edit_btn)
        dl.addLayout(top)
        self.stats = QLabel()
        self.stats.setObjectName("Muted")
        dl.addWidget(self.stats)
        inner = QSplitter(Qt.Horizontal)
        self.chart = PriceChart()
        inner.addWidget(self.chart)
        self.history = DataTable(["Date", "Vendor", "Invoice #", "Qty", "UOM", "Price", "Unit Cost", "Extended",
                                  "Description"])
        self.history.activated_id.connect(self.open_history_invoice)
        self.history.selection_id.connect(lambda hid: self.move_btn.setEnabled(hid is not None))
        self.history.setToolTip("Double-click to open the invoice")
        inner.addWidget(self.history)
        inner.setSizes([500, 600])
        dl.addWidget(inner, 1)
        self.move_btn = QPushButton("Save selected purchase as...")
        self.move_btn.setIcon(glyph_icon(Glyph.EDIT, size=16))
        self.move_btn.setEnabled(False)
        self.move_btn.clicked.connect(self.move_purchase)
        dl.addWidget(self.move_btn, 0, Qt.AlignRight)
        splitter.addWidget(detail)
        splitter.setSizes([420, 380])
        lay.addWidget(splitter, 1)

    def refresh(self) -> None:
        rows = []
        products = self.win.db.list_products(self.search.text())
        for r in products:
            change = pct_change(r["last_cost"], r["prev_cost"])
            change_item = make_item(fmt_pct(change), change if change is not None else -1e9, align_right=True)
            if change:
                change_item.setForeground(UP if change > 0 else DOWN)
            per = r["last_per"] or 1
            rows.append([
                make_item(r["item_number"], item_id=r["id"]),
                make_item(r["manufacturer"]),
                make_item(r["name"]),
                make_item(r["category"]),
                make_item(fmt_price(r["last_price"], per), r["last_cost"] or 0, align_right=True),
                make_item(fmt_money(r["last_cost"]), r["last_cost"] or 0, align_right=True),
                change_item,
                make_item(fmt_money(r["min_cost"]), r["min_cost"] or 0, align_right=True),
                make_item(fmt_money(r["max_cost"]), r["max_cost"] or 0, align_right=True),
                make_item(r["last_vendor"] or ""),
                make_item(r["last_date"] or "", r["last_date"] or ""),
                make_item(str(r["purchases"] or 0), r["purchases"] or 0, align_right=True),
            ])
        self.table.set_rows(rows)
        self.table.fit_columns({2: 340})
        self.header.set_subtitle(f"{len(products):,} product(s)" + (" matching search" if self.search.text() else ""))
        if self._current is not None and not self.table.select_id(self._current):
            self.show_detail(None)
        elif self._current is not None:
            self.show_detail(self._current)

    def select_product(self, product_id: int) -> None:
        if self.search.text():
            self.search.clear()
            self.refresh()
        self.table.select_id(product_id)
        self.show_detail(product_id)

    def show_detail(self, product_id) -> None:
        self._current = product_id
        self._history_invoices = {}
        self.move_btn.setEnabled(False)
        self.edit_btn.setEnabled(product_id is not None)
        if product_id is None or (p := self.win.db.get_product(product_id)) is None:
            self.title.setText("Select a product")
            self.stats.setText("")
            self.chart.set_history([])
            self.history.set_rows([])
            return
        mfr = f"{p['manufacturer']} " if p["manufacturer"] else ""
        self.title.setText(f"{mfr}{p['item_number']}  \u2014  {p['name']}")
        per = p["last_per"] or 1
        self.stats.setText(
            f"Last {fmt_price(p['last_price'], per)} from {p['last_vendor'] or 'unknown'} on {p['last_date']}   "
            f"\u2022   Low {fmt_money(p['min_cost'])}   \u2022   High {fmt_money(p['max_cost'])}   "
            f"\u2022   Avg {fmt_money(p['avg_cost'])} per unit   \u2022   {p['purchases']} purchase(s), "
            f"{fmt_qty(p['total_qty'])} {p['uom'] or ''} total")
        history = self.win.db.product_history(product_id)
        self._history_invoices = {h["id"]: h["invoice_id"] for h in history}
        self.chart.set_history(history)
        rows = []
        for h in reversed(history):
            rows.append([
                make_item(h["price_date"], h["price_date"], item_id=h["id"]),
                make_item(h["vendor"] or ""),
                make_item(h["invoice_number"] or ""),
                make_item(fmt_qty(h["quantity"]), h["quantity"], align_right=True),
                make_item(h["uom"]),
                make_item(fmt_price(h["unit_price"], h["price_per"]), h["unit_cost"], align_right=True),
                make_item(fmt_money(h["unit_cost"]), h["unit_cost"], align_right=True),
                make_item(fmt_money(h["extended"], False), h["extended"] or 0, align_right=True),
                make_item(h["description"]),
            ])
        self.history.set_rows(rows, keep_selection=False)
        self.history.fit_columns()

    def edit_product(self) -> None:
        if self._current is None or (row := self.win.db.get_product(self._current)) is None:
            return
        dlg = ProductDialog(row, self)
        if dlg.exec() == QDialog.Accepted:
            try:
                new_id = self.win.db.update_product(
                    self._current, dlg.name.text(), dlg.category.text(), dlg.notes.toPlainText(),
                    dlg.manufacturer.text(), dlg.item_number.text())
            except (ValueError, *DB_ERRORS) as exc:
                log.exception("Could not update product %s", self._current)
                QMessageBox.warning(self, "Could not save product", str(exc))
                return
            if new_id != self._current:
                QMessageBox.information(self, "Products merged",
                                        "Another product already has this item # and manufacturer, so their "
                                        "price histories were combined.")
                self._current = new_id
            self.win.data_changed()
            self.select_product(new_id)

    def open_history_invoice(self, history_id) -> None:
        if (invoice_id := self._history_invoices.get(history_id)) is not None:
            self.win.show_invoice(invoice_id)

    def move_purchase(self) -> None:
        if (history_id := self.history.current_id()) is None:
            return
        product_id = reassign_purchase(self.win, history_id, self)
        if product_id is not None:
            self._current = product_id
            self.win.data_changed()
            self.select_product(product_id)

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export products", "products.csv", "CSV files (*.csv)")
        if path:
            n = self.win.db.export_products_csv(path)
            QMessageBox.information(self, "Export complete", f"Exported {n:,} product(s) to\n{path}")
