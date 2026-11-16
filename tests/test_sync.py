"""Two-way sync between a local and a 'server' database (both SQLite in tests;
the algorithm only uses the Database API, so the engine does not matter)."""
import pytest

from invoice_reader import config
from invoice_reader.core.sync import sync_databases
from invoice_reader.db import Database
from invoice_reader.models import LineItem, ParsedInvoice


@pytest.fixture(autouse=True)
def pc_identity(monkeypatch):
    monkeypatch.setattr(config, "pc_id", lambda: "a" * 64)


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
        "adopted": 0, "products": 0, "aliases": 0,
        "settings_backed_up": 0, "settings_restored": 0}

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


def test_settings_backups_are_scoped_to_pc_and_exclude_credentials(local, remote, tmp_path):
    pc_a, pc_b = "a" * 64, "b" * 64
    local.set_setting("watch_folder", r"C:\InvoicesA")
    local.set_setting("auto_import", True)
    local.set_setting("password", "do-not-back-up")
    local.set_setting("initialized", "1")
    remote.set_setting("watch_folder", r"C:\LegacyGlobalFolder")
    assert sync_databases(local, remote, pc_a)["settings_backed_up"] == 2
    with_second = Database(tmp_path / "second.db")
    try:
        with_second.set_setting("watch_folder", r"D:\InvoicesB")
        with_second.set_setting("auto_import", False)
        assert sync_databases(with_second, remote, pc_b)["settings_backed_up"] == 2
        assert with_second.get_setting("watch_folder") == r"D:\InvoicesB"
        local.set_setting("watch_folder", r"C:\UpdatedA")
        assert sync_databases(local, remote, pc_a)["settings_backed_up"] == 1
        sync_databases(with_second, remote, pc_b)
        rows = {(r["pc_id"], r["key"]): r["value"] for r in remote.query("SELECT * FROM pc_settings")}
        assert rows == {
            (pc_a, "watch_folder"): r"C:\UpdatedA", (pc_a, "auto_import"): "1",
            (pc_b, "watch_folder"): r"D:\InvoicesB", (pc_b, "auto_import"): "0"}
        assert remote.get_setting("watch_folder") == r"C:\LegacyGlobalFolder"
    finally:
        with_second.close()


def test_restores_only_same_pcs_missing_settings_and_preserves_empty_values(local, remote, tmp_path):
    local.set_setting("watch_folder", r"C:\SavedFolder")
    local.set_setting("auto_import", True)
    local.set_setting("company_names", "Test Company")
    local.set_setting("watch_interval", 25)
    sync_databases(local, remote, "a" * 64)
    restored = Database(tmp_path / "reinstalled.db")
    try:
        # A new PC must not inherit anything, including legacy unscoped server settings.
        remote.set_setting("company_names", "Wrong Company")
        assert sync_databases(restored, remote, "b" * 64)["settings_restored"] == 0
        assert restored.query("SELECT * FROM settings") == []
        # An empty path and an explicit "off" are existing preferences, not missing ones.
        restored.set_setting("watch_folder", "")
        restored.set_setting("auto_import", False)
        counts = sync_databases(restored, remote, "a" * 64)
        assert counts["settings_restored"] == 2
        assert restored.get_setting("company_names") == "Test Company"
        assert restored.get_setting("watch_interval") == "25"
        assert restored.get_setting("watch_folder") == ""
        assert not restored.get_bool("auto_import")
        counts = sync_databases(restored, remote, "a" * 64)
        assert counts["settings_backed_up"] == counts["settings_restored"] == 0
    finally:
        restored.close()


def test_copied_local_database_uses_new_pcs_identity(local, remote, tmp_path, monkeypatch):
    local.set_setting("company_names", "Original PC")
    sync_databases(local, remote)
    path = tmp_path / "copied.db"
    local.backup(path)
    copied = Database(path)
    try:
        monkeypatch.setattr(config, "pc_id", lambda: "b" * 64)
        copied.set_setting("company_names", "Copied PC")
        sync_databases(copied, remote)
        saved = {r["pc_id"]: r["value"] for r in remote.query(
            "SELECT pc_id, value FROM pc_settings WHERE key = ?", ("company_names",))}
        assert saved == {"a" * 64: "Original PC", "b" * 64: "Copied PC"}
    finally:
        copied.close()
