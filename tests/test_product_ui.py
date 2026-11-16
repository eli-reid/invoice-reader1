import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from invoice_reader.db import Database
from invoice_reader.models import LineItem, ParsedInvoice
from invoice_reader.ui.pages.invoices import InvoicesPage
from invoice_reader.ui.pages.products import ProductDialog, ProductsPage, PurchaseDialog
from invoice_reader.ui.pages.settings import SettingsPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    db = Database(":memory:")
    invoice_id = db.save_invoice(ParsedInvoice(
        invoice_number="TEST", vendor="EW", invoice_date="2026-10-15",
        lines=[LineItem("EMT2", '1" X 10', 40, "FT", 202.64, 100, 81.06),
               LineItem("EMT2", '2" X 10', 10, "FT", 470.42, 100, 47.04)]))
    yield SimpleNamespace(db=db, data_changed=Mock(), show_invoice=Mock(), show_product=Mock(),
                          invoice_id=invoice_id)
    db.close()


def test_product_dialog_edits_item_number(win):
    product = win.db.list_products()[0]
    dlg = ProductDialog(product)
    dlg.item_number.setText("EMT1")
    dlg.name.setText('1" EMT')
    dlg.accept()
    assert dlg.result() == QDialog.Accepted
    win.db.update_product(product["id"], dlg.name.text(), "", "", item_number=dlg.item_number.text())
    assert win.db.get_product(product["id"])["item_number"] == "EMT1"


def test_settings_edits_request_backup_and_restored_controls_do_not_resave(win, monkeypatch, tmp_path):
    from invoice_reader.ui.main_window import MainWindow

    monkeypatch.setenv("INVOICE_READER_DATA", str(tmp_path))
    monkeypatch.setattr("invoice_reader.ui.pages.settings.update.is_source_checkout", lambda: False)
    window = Mock(db=win.db, sync=Mock(status="Server sync is off"))
    page = SettingsPage(window)
    page.auto_import.setChecked(True)
    assert win.db.get_bool("auto_import")
    window.sync.request_sync.assert_called_once()
    window.sync.request_sync.reset_mock()
    win.db.set_setting("watch_enabled", False)
    win.db.set_setting("watch_interval", 25)
    win.db.set_setting("company_names", "Restored Company")
    window.pages = {"settings": page}
    window._dirty = set()
    MainWindow._on_settings_restored(window)
    assert not page.watch_enabled.isChecked()
    assert page.auto_import.isChecked()
    assert page.interval.value() == 25
    assert page.company.text() == "Restored Company"
    window.watcher.configure.assert_called_once()
    window.sync.request_sync.assert_not_called()
    page.deleteLater()


def test_products_history_selects_individual_purchase_and_opens_invoice(win, monkeypatch):
    page = ProductsPage(win)
    win.data_changed.side_effect = page.refresh
    page.refresh()
    page.select_product(win.db.list_products()[0]["id"])
    history_id = win.db.invoice_lines(win.invoice_id)[0]["id"]
    assert page.history.select_id(history_id)
    assert page.move_btn.isEnabled()
    page.open_history_invoice(history_id)
    win.show_invoice.assert_called_once_with(win.invoice_id)

    def save_dialog(dlg):
        dlg.item_number.setText("EMT1")
        dlg.accept()
        return dlg.result()

    monkeypatch.setattr(PurchaseDialog, "exec", save_dialog)
    page.move_btn.click()
    win.data_changed.assert_called_once()
    assert win.db.invoice_lines(win.invoice_id)[0]["item_number"] == "EMT1"
    assert page._current == win.db.invoice_lines(win.invoice_id)[0]["product_id"]
    page.deleteLater()


def test_invoice_lines_select_individual_purchase(win, monkeypatch):
    page = InvoicesPage(win)
    win.data_changed.side_effect = page.refresh
    page.refresh()
    page.select_invoice(win.invoice_id)
    rows = win.db.invoice_lines(win.invoice_id)
    assert page.lines.select_id(rows[1]["id"])
    assert page.move_btn.isEnabled()
    page.open_line_product(rows[1]["id"])
    win.show_product.assert_called_once_with(rows[1]["product_id"])

    def save_dialog(dlg):
        dlg.item_number.setText("EMT1")
        dlg.accept()
        return dlg.result()

    monkeypatch.setattr(PurchaseDialog, "exec", save_dialog)
    page.move_btn.click()
    assert win.db.invoice_lines(win.invoice_id)[0]["item_number"] == "EMT2"
    assert win.db.invoice_lines(win.invoice_id)[1]["item_number"] == "EMT1"
    win.data_changed.assert_called_once()
    page.deleteLater()


def test_purchase_dialog_selects_existing_product(win):
    win.db.save_invoice(ParsedInvoice(
        invoice_number="OTHER", vendor="EW",
        lines=[LineItem("EMT1", '1" EMT', 1, "FT", 2.03, 1, 2.03)]))
    row = win.db.invoice_lines(win.invoice_id)[0]
    dlg = PurchaseDialog(win, row)
    target = next(p["id"] for p in win.db.list_products() if p["item_number"] == "EMT1")
    dlg.existing.setCurrentIndex(dlg.existing.findData(target))
    assert dlg.item_number.isReadOnly()
    dlg.accept()
    assert dlg.product_id == target
    assert win.db.invoice_lines(win.invoice_id)[0]["product_id"] == target


def test_product_edit_keeps_survivor_selected_after_merge(win, monkeypatch):
    win.db.save_invoice(ParsedInvoice(
        invoice_number="OTHER", vendor="EW",
        lines=[LineItem("EMT1", '1" EMT', 1, "FT", 2.03, 1, 2.03)]))
    page = ProductsPage(win)
    win.data_changed.side_effect = page.refresh
    page.refresh()
    original = next(p["id"] for p in win.db.list_products() if p["item_number"] == "EMT2")
    target = next(p["id"] for p in win.db.list_products() if p["item_number"] == "EMT1")
    page.select_product(original)

    def save_dialog(dlg):
        dlg.item_number.setText("EMT1")
        return QDialog.Accepted

    monkeypatch.setattr(ProductDialog, "exec", save_dialog)
    monkeypatch.setattr("invoice_reader.ui.pages.products.QMessageBox.information", lambda *args: None)
    page.edit_product()
    assert page._current == target
    assert page.table.current_id() == target
    assert win.db.get_product(target)["purchases"] == 3
    page.deleteLater()
