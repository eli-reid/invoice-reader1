"""Two-way sync between a local and a 'server' database (both SQLite in tests;
the algorithm only uses the Database API, so the engine does not matter)."""
import pytest

from invoice_reader.core.sync import sync_databases
from invoice_reader.db import Database
from invoice_reader.models import LineItem, ParsedInvoice


@pytest.fixture
def local(tmp_path):
    d = Database(tmp_path / "local.db")
    yield d
    d.close()


@pytest.fixture
def remote(tmp_path):
    d = Database(tmp_path / "remote.db")
    yield d
    d.close()


def make_invoice(number: str, price: float = 8.0, vendor: str = "Graybar",
                 file_hash: str | None = None) -> ParsedInvoice:
    return ParsedInvoice(
        source_path=f"C:/x/{number}.pdf", file_hash=file_hash or f"hash-{vendor}-{number}",
        vendor=vendor, invoice_number=number, invoice_date="2026-01-10", total=price * 10,
        method="text",
        lines=[LineItem("QO120", "SQUARE D BREAKER", 10, "EA", price, 1, round(price * 10, 2))])


def test_push_and_pull(local, remote):
    local.save_invoice(make_invoice("L-1"))
    remote.save_invoice(make_invoice("R-1", vendor="Rexel"))

    counts = sync_databases(local, remote)
    assert counts["pushed"] == 1 and counts["pulled"] == 1
    for db in (local, remote):
        assert {r["invoice_number"] for r in db.list_invoices()} == {"L-1", "R-1"}
        assert db.stats()["invoices"] == 2
    # uids line up so nothing transfers twice
    uids = lambda db: {r["sync_uid"] for r in db.query("SELECT sync_uid FROM invoices")}  # noqa: E731
    assert uids(local) == uids(remote)
    assert sync_databases(local, remote) == {
        "pushed": 0, "pulled": 0, "deleted_local": 0, "deleted_remote": 0,
        "adopted": 0, "products": 0, "aliases": 0}

    # the pulled invoice's lines made it into price history
    rexel = next(r for r in local.list_invoices() if r["invoice_number"] == "R-1")
    lines = local.invoice_lines(rexel["id"])
    assert len(lines) == 1 and lines[0]["item_number"] == "QO120" and lines[0]["unit_price"] == 8.0


def test_deletion_propagates_and_stays_dead(local, remote):
    inv_id = local.save_invoice(make_invoice("L-1"))
    sync_databases(local, remote)

    local.delete_invoice(inv_id)
    counts = sync_databases(local, remote)
    assert counts["deleted_remote"] == 1
    assert remote.stats()["invoices"] == 0

    # the tombstone prevents the other side from resurrecting it
    counts = sync_databases(local, remote)
    assert counts["pulled"] == 0 and counts["pushed"] == 0
    assert local.stats()["invoices"] == 0


def test_same_invoice_imported_on_both_sides_adopts_uid(local, remote):
    local.save_invoice(make_invoice("1001", file_hash="h-local"))
    remote.save_invoice(make_invoice("1001", file_hash="h-remote"))

    counts = sync_databases(local, remote)
    assert counts["adopted"] >= 1
    assert local.stats()["invoices"] == 1 and remote.stats()["invoices"] == 1
    # both sides converge on one uid eventually
    sync_databases(local, remote)
    uid = local.query_one("SELECT sync_uid FROM invoices")["sync_uid"]
    assert remote.query_one("SELECT sync_uid FROM invoices")["sync_uid"] == uid


def test_product_details_follow_latest_edit(local, remote):
    local.save_invoice(make_invoice("L-1"))
    sync_databases(local, remote)
    # The remote copy is older than the local edit that follows.
    remote.execute("UPDATE products SET updated_at = '2020-01-01T00:00:00'")
    product = local.list_products()[0]
    local.update_product(product["id"], "20A 1-pole breaker", "Breakers", "stock item")

    counts = sync_databases(local, remote)
    assert counts["products"] == 1
    p = remote.list_products()[0]
    assert (p["name"], p["category"], p["notes"]) == ("20A 1-pole breaker", "Breakers", "stock item")


def test_vendor_aliases_merge(local, remote):
    local.add_vendor_alias("EW -", "Electrical Wholesalers")
    remote.add_vendor_alias("GB", "Graybar")
    counts = sync_databases(local, remote)
    assert counts["aliases"] == 2
    expected = {"EW -": "Electrical Wholesalers", "GB": "Graybar"}
    assert local.vendor_aliases() == expected
    assert remote.vendor_aliases() == expected
