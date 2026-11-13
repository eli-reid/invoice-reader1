import pytest

from invoice_reader.db import INBOX_IMPORTED, INBOX_PENDING, Database, DuplicateInvoiceError
from invoice_reader.models import LineItem, ParsedInvoice


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def make_invoice(number: str, date: str, price: float, file_hash: str) -> ParsedInvoice:
    return ParsedInvoice(
        source_path=f"C:/x/{number}.pdf", file_hash=file_hash, vendor="Graybar", invoice_number=number,
        invoice_date=date, total=100.0, method="text",
        lines=[
            LineItem("QO120", "SQUARE D BREAKER", 10, "EA", price, 1, round(price * 10, 2)),
            LineItem("THHN12", "12 THHN WIRE", 500, "FT", 150.0, 1000, 75.0),
        ])


def test_save_and_price_history(db):
    db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h1"))
    db.save_invoice(make_invoice("1002", "2026-03-10", 8.80, "h2"))
    products = {p["item_number"]: p for p in db.list_products()}
    assert set(products) == {"QO120", "THHN12"}
    qo = products["QO120"]
    assert qo["last_cost"] == pytest.approx(8.80)
    assert qo["prev_cost"] == pytest.approx(8.00)
    assert qo["purchases"] == 2
    assert products["THHN12"]["last_cost"] == pytest.approx(0.15)
    changes = db.recent_price_changes()
    assert [c["item_number"] for c in changes] == ["QO120"]
    assert len(db.product_history(qo["id"])) == 2
    assert db.stats()["invoices"] == 2


def test_duplicate_invoice_detection(db):
    db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h1"))
    with pytest.raises(DuplicateInvoiceError):
        db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h9"))
    db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h1"), allow_duplicate=True)
    assert db.stats()["invoices"] == 2


def test_delete_invoice_removes_orphans(db):
    inv_id = db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h1"))
    db.delete_invoice(inv_id)
    assert db.list_products() == []


def test_inbox_roundtrip(db):
    inbox_id = db.inbox_upsert("C:/x/a.pdf", "abc", 10, "watch")
    parsed = make_invoice("1", "2026-01-01", 1.0, "abc")
    db.inbox_update(inbox_id, INBOX_PENDING, parsed=parsed)
    row = db.inbox_get(inbox_id)
    restored = ParsedInvoice.from_json(row["parsed_json"])
    assert restored.lines[1].price_per == 1000
    assert len(db.inbox_list()) == 1
    db.inbox_update(inbox_id, INBOX_IMPORTED)
    assert db.inbox_list() == []


def test_settings_and_exports(db, tmp_path):
    assert db.get_setting("watch_interval") == "10"
    db.set_setting("auto_import", True)
    assert db.get_bool("auto_import")
    db.save_invoice(make_invoice("1001", "2026-01-10", 8.00, "h1"))
    assert db.export_products_csv(tmp_path / "p.csv") == 2
    assert db.export_history_csv(tmp_path / "h.csv") == 2
    db.backup(tmp_path / "backup.db")
    assert Database(tmp_path / "backup.db").stats()["invoices"] == 1

def test_manufacturer_and_vendor_aliases(db):
    inv = make_invoice("2001", "2026-01-10", 8.00, "h9")
    inv.lines[0].manufacturer = "SQD"
    db.save_invoice(inv)
    p = {r["item_number"]: r for r in db.list_products("SQD")}
    assert list(p) == ["QO120"] and p["QO120"]["manufacturer"] == "SQD"
    db.add_vendor_alias("Acknowledgement", "Electrical Wholesalers")
    db.add_vendor_alias("Same", "same")
    assert db.vendor_aliases() == {"Acknowledgement": "Electrical Wholesalers"}


def test_item_key_tolerates_ocr_and_punctuation():
    from invoice_reader.db import item_key
    assert item_key("Q0120") == item_key("qo-120") == item_key("QO 120")


def _one_line(number, mfr, item, price, file_hash):
    return ParsedInvoice(source_path=f"C:/x/{number}.pdf", file_hash=file_hash, vendor="EW", invoice_number=number,
                         invoice_date="2026-10-01", method="text",
                         lines=[LineItem(item, "PART", 1, "EA", price, 1, price, manufacturer=mfr)])


def test_item_only_matching_ignores_manufacturer(db):
    db.save_invoice(_one_line("1", "CRS", "11", 0.24, "a"))
    db.save_invoice(_one_line("2", "BRG", "11", 0.30, "b"))
    products = db.list_products()
    assert len(products) == 1 and products[0]["manufacturer"] == "CRS" and products[0]["purchases"] == 2


def test_manufacturer_matching_splits_products(db):
    db.set_setting("match_manufacturer", True)
    db.save_invoice(_one_line("1", "CRS", "11", 0.24, "a"))
    db.save_invoice(_one_line("2", "BRG", "11", 0.30, "b"))
    db.save_invoice(_one_line("3", "crs", "11", 0.26, "c"))
    products = {p["manufacturer"]: p for p in db.list_products()}
    assert set(products) == {"CRS", "BRG"}
    assert products["CRS"]["purchases"] == 2 and products["BRG"]["purchases"] == 1
    # A line with no manufacturer is ambiguous here, so it becomes its own product.
    db.save_invoice(_one_line("4", "", "11", 0.25, "d"))
    assert len(db.list_products()) == 3


def test_manufacturer_matching_joins_or_claims_unknown(db):
    db.set_setting("match_manufacturer", True)
    db.save_invoice(_one_line("1", "", "QO120", 8.0, "a"))
    db.save_invoice(_one_line("2", "SQD", "QO120", 8.5, "b"))  # claims the product with no mfr
    db.save_invoice(_one_line("3", "", "QO120", 8.7, "c"))     # only one candidate -> joins it
    products = db.list_products()
    assert len(products) == 1 and products[0]["manufacturer"] == "SQD" and products[0]["purchases"] == 3


def test_editing_manufacturer_merges_duplicates(db):
    db.set_setting("match_manufacturer", True)
    db.save_invoice(_one_line("1", "CRS", "11", 0.24, "a"))
    db.save_invoice(_one_line("2", "CR", "11", 0.26, "b"))
    ids = {p["manufacturer"]: p["id"] for p in db.list_products()}
    survivor = db.update_product(ids["CR"], "LOCKNUT", "", "", "CRS")
    assert survivor == ids["CRS"]
    products = db.list_products()
    assert len(products) == 1 and products[0]["purchases"] == 2


def test_migrates_v1_database_keeping_history(tmp_path):
    import sqlite3
    from invoice_reader.db import MIGRATIONS
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(MIGRATIONS[0])
    con.executescript("""
        INSERT INTO vendors(id, name) VALUES(1, 'EW');
        INSERT INTO products(id, item_number, item_key, manufacturer, name, created_at, updated_at)
            VALUES(7, 'QO120', 'Q0120', 'SQD', 'BREAKER', 'x', 'x');
        INSERT INTO invoices(id, vendor_id, invoice_number, imported_at) VALUES(3, 1, 'A1', 'x');
        INSERT INTO price_history(product_id, invoice_id, vendor_id, price_date, unit_price, unit_cost)
            VALUES(7, 3, 1, '2026-01-01', 8.0, 8.0);
        PRAGMA user_version = 1;
    """)
    con.close()
    d = Database(path)
    try:
        p = d.list_products()
        assert len(p) == 1 and p[0]["id"] == 7 and p[0]["purchases"] == 1
        assert d.query_one("SELECT mfr_key FROM products")["mfr_key"] == "SQD"
        assert d.query_one("PRAGMA foreign_keys")[0] == 1
    finally:
        d.close()


def test_edit_item_number_and_merge_history(db):
    db.save_invoice(_one_line("1", "", "WRONG", 2.0, "a"))
    original = db.list_products()[0]["id"]
    assert db.update_product(original, "Corrected", "", "", item_number="EMT1") == original
    assert db.get_product(original)["item_number"] == "EMT1"
    db.save_invoice(_one_line("2", "", "EMT1", 3.0, "b"))
    assert db.get_product(original)["purchases"] == 2
    db.save_invoice(_one_line("3", "", "OTHER", 4.0, "c"))
    other = next(p for p in db.list_products() if p["item_number"] == "OTHER")
    assert db.update_product(other["id"], "Merged", "", "", item_number="emt-1") == original
    assert db.get_product(original)["purchases"] == 3
    assert db.get_product(other["id"]) is None


def test_reassign_only_one_purchase_preserving_invoice(db):
    inv = _one_line("1", "", "EMT2", 202.64, "a")
    inv.lines[0] = LineItem("EMT2", '1" X 10', 40, "FT", 202.64, 100, 81.06)
    invoice_id = db.save_invoice(inv)
    db.save_invoice(_one_line("2", "", "EMT2", 470.42, "b"))
    before = dict(db.invoice_lines(invoice_id)[0])
    target = db.reassign_purchase(before["id"], "EMT1", "", '1" EMT')
    after = dict(db.invoice_lines(invoice_id)[0])
    for key in ("id", "invoice_id", "vendor_id", "price_date", "description", "quantity", "uom",
                "unit_price", "price_per", "unit_cost", "extended"):
        assert after[key] == before[key]
    assert after["product_id"] == target
    assert db.get_product(target)["name"] == '1" EMT'
    assert {p["item_number"]: p["purchases"] for p in db.list_products()} == {"EMT1": 1, "EMT2": 1}
    # User-selected targets take priority even when manufacturer matching is disabled.
    db.update_product(target, '1" EMT', "", "", manufacturer="SOURCE")
    db.set_setting("match_manufacturer", True)
    db.save_invoice(_one_line("3", "ABC", "EMT1", 4, "c"))
    selected = next(p["id"] for p in db.list_products() if p["manufacturer"] == "ABC")
    db.set_setting("match_manufacturer", False)
    assert db.reassign_purchase(before["id"], "EMT1", "ABC", "ignored",
                                target_product_id=selected) == selected
    assert db.get_product(target) is None
    assert db.get_product(selected)["name"] == "PART"


def test_invalid_edits_do_not_change_history(db):
    db.save_invoice(_one_line("1", "", "ONE", 2, "a"))
    product = db.list_products()[0]
    history_id = db.product_history(product["id"])[0]["id"]
    with pytest.raises(ValueError, match="valid item"):
        db.update_product(product["id"], "No", "", "", item_number=" ")
    with pytest.raises(ValueError, match="valid item"):
        db.reassign_purchase(history_id, "***", "", "No")
    with pytest.raises(ValueError, match="no longer"):
        db.reassign_purchase(history_id, "NEW", "", "No", target_product_id=999)
    assert db.get_product(product["id"])["name"] == "PART"
    assert len(db.list_products()) == 1


def test_emt_fraction_keys_do_not_collide():
    from invoice_reader.db import item_key
    assert item_key('EMT 1/2"') != item_key("EMT12")
    assert item_key('EMT 3/2"') != item_key("EMT32")


def test_migrates_mixed_emt_history_once(tmp_path):
    path = tmp_path / "mixed.db"
    old = Database(path)
    one = _one_line("1", "", "EMT2", 202.64, "a")
    one.lines[0] = LineItem("EMT2", '1" X 10', 40, "FT", 202.64, 100, 81.06)
    old.save_invoice(one)
    two = _one_line("2", "", "EMT2", 470.42, "b")
    two.lines[0] = LineItem("EMT2", '2" X 10FT', 10, "FT", 470.42, 100, 47.04)
    old.save_invoice(two)
    old.save_invoice(_one_line("3", "ARL", "EMT150", 1.77, "c"))
    old.execute("PRAGMA user_version = 2")
    old.close()
    repaired = Database(path)
    products = {p["item_number"]: dict(p) for p in repaired.list_products()}
    assert set(products) == {"EMT1", "EMT2", "EMT150"}
    assert products["EMT1"]["last_cost"] == pytest.approx(2.0264)
    assert products["EMT2"]["last_cost"] == pytest.approx(4.7042)
    assert products["EMT1"]["purchases"] == products["EMT2"]["purchases"] == 1
    assert repaired.stats()["invoices"] == 3
    assert repaired.query("PRAGMA foreign_key_check") == []
    repaired.close()
    reopened = Database(path)
    assert {p["item_number"]: dict(p) for p in reopened.list_products()} == products
    reopened.close()
