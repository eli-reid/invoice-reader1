"""Backend layer: SQLite -> MySQL dialect translation and database copying."""
from invoice_reader.backends import translate_sql_to_mysql
from invoice_reader.db import Database, copy_database
from invoice_reader.models import LineItem, ParsedInvoice


def test_placeholders_and_insert_ignore():
    assert (translate_sql_to_mysql("INSERT OR IGNORE INTO vendors(name) VALUES(?)")
            == "INSERT IGNORE INTO vendors(name) VALUES(%s)")


def test_on_conflict_upsert():
    sql = ("INSERT INTO settings(key, value) VALUES(?, ?) "
           "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    assert (translate_sql_to_mysql(sql)
            == "INSERT INTO settings(`key`, value) VALUES(%s, %s) "
               "ON DUPLICATE KEY UPDATE value = VALUES(value)")


def test_multi_column_upsert_with_literal():
    sql = ("INSERT INTO inbox(file_path, file_hash) VALUES(?,?) ON CONFLICT(file_hash) DO UPDATE SET "
           "file_path = excluded.file_path, error = NULL, updated_at = excluded.updated_at")
    out = translate_sql_to_mysql(sql)
    assert "ON DUPLICATE KEY UPDATE file_path = VALUES(file_path), error = NULL, " \
           "updated_at = VALUES(updated_at)" in out


def test_pc_settings_composite_upsert_translation():
    sql = ("INSERT INTO pc_settings(pc_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
           "ON CONFLICT(pc_id, key) DO UPDATE SET value = excluded.value, "
           "updated_at = excluded.updated_at")
    assert translate_sql_to_mysql(sql) == (
        "INSERT INTO pc_settings(pc_id, `key`, value, updated_at) VALUES(%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)")


def test_collate_nocase_stripped_and_key_untouched_in_names():
    sql = "SELECT id FROM invoices WHERE invoice_number = ? COLLATE NOCASE ORDER BY item_key COLLATE NOCASE"
    assert (translate_sql_to_mysql(sql)
            == "SELECT id FROM invoices WHERE invoice_number = %s ORDER BY item_key")


def test_key_column_quoted_only_as_bare_identifier():
    assert translate_sql_to_mysql("SELECT value FROM settings WHERE key = ?") \
        == "SELECT value FROM settings WHERE `key` = %s"
    # item_key / mfr_key and already-quoted `key` must not be touched
    assert translate_sql_to_mysql("SELECT item_key, mfr_key, `key` FROM t") \
        == "SELECT item_key, mfr_key, `key` FROM t"


def test_copy_database(tmp_path):
    invoice = ParsedInvoice(
        source_path="C:/x/1001.pdf", file_hash="h1", vendor="Graybar", invoice_number="1001",
        invoice_date="2026-01-10", total=100.0, method="text",
        lines=[
            LineItem("QO120", "SQUARE D BREAKER", 10, "EA", 8.0, 1, 80.0),
            LineItem("THHN12", "12 THHN WIRE", 500, "FT", 150.0, 1000, 75.0),
        ])
    src = Database(tmp_path / "src.db")
    src.set_setting("company_names", "Midstate Electric")
    src.add_vendor_alias("EW -", "Electrical Wholesalers")
    invoice_id = src.save_invoice(invoice)

    dst = Database(tmp_path / "dst.db")
    counts = copy_database(src, dst)
    assert counts["invoices"] == 1 and counts["price_history"] == len(invoice.lines)
    assert dst.stats() == src.stats()
    assert dst.get_setting("company_names") == "Midstate Electric"
    assert dst.vendor_aliases() == {"EW -": "Electrical Wholesalers"}
    assert [dict(r) for r in dst.invoice_lines(invoice_id)] == [dict(r) for r in src.invoice_lines(invoice_id)]

    # A destination that already holds data is refused.
    try:
        copy_database(src, dst)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pc_id_is_stable_across_rename_but_differs_on_another_pc(monkeypatch):
    from invoice_reader import config

    guid = "11111111-abcd-1234-5678-000000000001"
    monkeypatch.setattr(config, "_windows_machine_guid", lambda: guid)
    original = config.pc_id()
    monkeypatch.setenv("COMPUTERNAME", "Renamed-PC")
    monkeypatch.setattr(config, "_windows_machine_guid", lambda: guid.upper())
    assert config.pc_id() == original
    assert len(original) == 64 and int(original, 16) >= 0
    assert guid not in original
    monkeypatch.setattr(config, "_windows_machine_guid",
                        lambda: "22222222-abcd-1234-5678-000000000002")
    assert config.pc_id() != original


def test_unreadable_pc_identity_fails_instead_of_using_shared_fallback(monkeypatch):
    import pytest
    from invoice_reader import config

    def denied():
        raise PermissionError("MachineGuid unavailable")

    monkeypatch.setattr(config, "_windows_machine_guid", denied)
    with pytest.raises(RuntimeError, match="Cannot read the Windows machine identity"):
        config.pc_id()
