"""Persistence: vendors, products, invoices, price history and the import inbox
(which also remembers every file the watcher has seen). Runs on a local SQLite
file by default, or a shared server database via invoice_reader.backends."""
from __future__ import annotations

import csv
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from invoice_reader.backends import Backend, Row, SQLiteBackend
from invoice_reader.models import LineItem, ParsedInvoice, emt_item_number

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE vendors (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    );
    CREATE TABLE products (
        id          INTEGER PRIMARY KEY,
        item_number TEXT NOT NULL,
        item_key    TEXT NOT NULL UNIQUE,
        manufacturer TEXT NOT NULL DEFAULT '',
        name        TEXT NOT NULL DEFAULT '',
        category    TEXT NOT NULL DEFAULT '',
        notes       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE invoices (
        id             INTEGER PRIMARY KEY,
        vendor_id      INTEGER REFERENCES vendors(id),
        invoice_number TEXT NOT NULL DEFAULT '',
        invoice_date   TEXT,
        total          REAL,
        file_path      TEXT,
        original_name  TEXT,
        file_hash      TEXT UNIQUE,
        method         TEXT,
        imported_at    TEXT NOT NULL
    );
    CREATE INDEX ix_invoices_vendor_number ON invoices(vendor_id, invoice_number);
    CREATE INDEX ix_invoices_date ON invoices(invoice_date);
    CREATE TABLE price_history (
        id          INTEGER PRIMARY KEY,
        product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
        vendor_id   INTEGER REFERENCES vendors(id),
        price_date  TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        quantity    REAL NOT NULL DEFAULT 0,
        uom         TEXT NOT NULL DEFAULT 'EA',
        unit_price  REAL NOT NULL,
        price_per   INTEGER NOT NULL DEFAULT 1,
        unit_cost   REAL NOT NULL,
        extended    REAL
    );
    CREATE INDEX ix_price_product_date ON price_history(product_id, price_date);
    CREATE INDEX ix_price_invoice ON price_history(invoice_id);
    CREATE TABLE inbox (
        id          INTEGER PRIMARY KEY,
        file_path   TEXT NOT NULL,
        file_hash   TEXT NOT NULL UNIQUE,
        file_size   INTEGER,
        source      TEXT NOT NULL DEFAULT 'manual',
        status      TEXT NOT NULL,
        parsed_json TEXT,
        error       TEXT,
        invoice_id  INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
        detected_at TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX ix_inbox_status ON inbox(status);
    CREATE TABLE vendor_aliases (
        alias  TEXT PRIMARY KEY COLLATE NOCASE,
        vendor TEXT NOT NULL
    );
    """,
    # v2: products are unique per (item #, manufacturer) so the same catalog # from two
    # manufacturers can be tracked separately when manufacturer matching is on.
    """
    CREATE TABLE products_new (
        id           INTEGER PRIMARY KEY,
        item_number  TEXT NOT NULL,
        item_key     TEXT NOT NULL,
        manufacturer TEXT NOT NULL DEFAULT '',
        mfr_key      TEXT NOT NULL DEFAULT '',
        name         TEXT NOT NULL DEFAULT '',
        category     TEXT NOT NULL DEFAULT '',
        notes        TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE(item_key, mfr_key)
    );
    INSERT INTO products_new(id, item_number, item_key, manufacturer, mfr_key, name, category, notes,
                             created_at, updated_at)
        SELECT id, item_number, item_key, manufacturer, UPPER(REPLACE(TRIM(manufacturer), ' ', '')), name,
               category, notes, created_at, updated_at FROM products;
    DROP TABLE products;
    ALTER TABLE products_new RENAME TO products;
    CREATE INDEX ix_products_item_key ON products(item_key);
    """,
    # v3: repair recognizable EMT conduit history using the diameter in its description.
    "",
]

INBOX_PROCESSING = "processing"
INBOX_PENDING = "pending"      # parsed, waiting for user review
INBOX_FAILED = "failed"
INBOX_IMPORTED = "imported"
INBOX_IGNORED = "ignored"      # dismissed by the user
INBOX_SKIPPED = "skipped"      # did not look like an invoice
INBOX_ACTIVE = (INBOX_PROCESSING, INBOX_PENDING, INBOX_FAILED)

DEFAULT_SETTINGS = {
    "watch_enabled": "1",
    "watch_folder": "",
    "watch_recursive": "0",
    "watch_interval": "10",
    "watch_since": "",
    "auto_import": "0",
    "close_to_tray": "1",
    "company_names": "Midstate Electric",
    "match_manufacturer": "0",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def item_key(item_number: str) -> str:
    """Normalized catalog # used to match products across invoices: case, spacing and
    punctuation are ignored, and O/0 are treated alike because OCR confuses them."""
    fractional_emt = re.fullmatch(r'EMT\s+(\d+/\d+)"', item_number.strip(), re.I)
    if fractional_emt:
        return f"EMT:{fractional_emt[1]}"
    return re.sub(r"[^A-Z0-9]", "", item_number.upper()).replace("O", "0")


def mfr_key(manufacturer: str) -> str:
    return re.sub(r"\s+", "", (manufacturer or "").upper())


class DuplicateInvoiceError(Exception):
    def __init__(self, invoice_id: int):
        super().__init__(f"Invoice already exists (id {invoice_id})")
        self.invoice_id = invoice_id


class Database:
    def __init__(self, source: str | Path | Backend):
        self.backend = source if isinstance(source, Backend) else SQLiteBackend(source)
        self.path = getattr(self.backend, "path", self.backend.describe())
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.backend.close()

    @contextmanager
    def transaction(self) -> Iterator[Backend]:
        with self._lock:
            self.backend.begin()
            try:
                yield self.backend
            except BaseException:
                self.backend.rollback()
                raise
            self.backend.commit()

    def _migrate(self) -> None:
        scripts = self.backend.migrations()
        version = self.backend.schema_version()
        if version >= len(scripts):
            return
        self.backend.pre_migrate()
        try:
            for i, script in enumerate(scripts[version:], start=version + 1):
                with self.transaction() as c:
                    for statement in filter(str.strip, script.split(";")):
                        c.execute(statement)
                    if self.backend.engine == "sqlite" and i == 3:
                        self._repair_emt_history(c)
                    self.backend.check_migration_integrity()
                    self.backend.set_schema_version(i)
        finally:
            self.backend.post_migrate()

    def _repair_emt_history(self, c: Backend) -> None:
        rows = c.execute(
            "SELECT ph.id, ph.product_id, ph.description, p.item_number, p.manufacturer, p.category, p.notes "
            "FROM price_history ph JOIN products p ON p.id = ph.product_id "
            "WHERE UPPER(p.item_number) LIKE 'EMT%'").fetchall()
        changed = 0
        for row in rows:
            corrected = emt_item_number(row["item_number"], row["description"])
            if corrected is None or item_key(corrected) == item_key(row["item_number"]):
                continue
            target = self._product_id(c, LineItem(
                item_number=corrected, description=row["description"], manufacturer=row["manufacturer"]),
                self.get_bool("match_manufacturer"), now_iso())
            c.execute("UPDATE price_history SET product_id = ? WHERE id = ?", (target, row["id"]))
            c.execute("UPDATE products SET category = ?, notes = ? WHERE id = ? AND category = '' AND notes = ''",
                      (row["category"], row["notes"], target))
            c.execute("DELETE FROM products WHERE id = ? AND NOT EXISTS "
                      "(SELECT 1 FROM price_history WHERE product_id = ?)", (row["product_id"], row["product_id"]))
            changed += 1
        if changed:
            log.info("Separated %d saved EMT purchase(s) by conduit diameter", changed)

    def query(self, sql: str, params: tuple | dict = ()) -> list[Row]:
        with self._lock:
            return self.backend.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> Row | None:
        with self._lock:
            return self.backend.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()):
        with self._lock:
            return self.backend.execute(sql, params)

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is not None and row["value"] is not None:
            return row["value"]
        return default if default is not None else DEFAULT_SETTINGS.get(key, "")

    def get_bool(self, key: str) -> bool:
        return self.get_setting(key) == "1"

    def set_setting(self, key: str, value: str | bool | int) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        self.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    # ---- vendors --------------------------------------------------------
    def vendor_names(self) -> list[str]:
        return [r["name"] for r in self.query("SELECT name FROM vendors ORDER BY name")]

    def vendor_aliases(self) -> dict[str, str]:
        return {r["alias"]: r["vendor"] for r in self.query("SELECT alias, vendor FROM vendor_aliases")}

    def add_vendor_alias(self, alias: str, vendor: str) -> None:
        alias, vendor = alias.strip(), vendor.strip()
        if alias and vendor and alias.lower() != vendor.lower():
            self.execute("INSERT INTO vendor_aliases(alias, vendor) VALUES(?, ?) "
                         "ON CONFLICT(alias) DO UPDATE SET vendor = excluded.vendor", (alias, vendor))

    def _vendor_id(self, c: Backend, name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        c.execute("INSERT OR IGNORE INTO vendors(name) VALUES(?)", (name,))
        return c.execute("SELECT id FROM vendors WHERE name = ?", (name,)).fetchone()["id"]

    # ---- invoices -------------------------------------------------------
    def find_invoice(self, vendor: str, invoice_number: str) -> int | None:
        if not invoice_number.strip():
            return None
        row = self.query_one(
            "SELECT i.id FROM invoices i JOIN vendors v ON v.id = i.vendor_id "
            "WHERE v.name = ? COLLATE NOCASE AND i.invoice_number = ? COLLATE NOCASE",
            (vendor.strip(), invoice_number.strip()))
        return row["id"] if row else None

    def invoice_by_hash(self, file_hash: str) -> int | None:
        row = self.query_one("SELECT id FROM invoices WHERE file_hash = ?", (file_hash,))
        return row["id"] if row else None

    def save_invoice(self, inv: ParsedInvoice, archived_path: str | None = None,
                     allow_duplicate: bool = False) -> int:
        if not allow_duplicate:
            existing = self.find_invoice(inv.vendor, inv.invoice_number)
            if existing:
                raise DuplicateInvoiceError(existing)
        ts = now_iso()
        price_date = inv.invoice_date or ts[:10]
        match_mfr = self.get_bool("match_manufacturer")
        with self.transaction() as c:
            vendor_id = self._vendor_id(c, inv.vendor)
            file_hash = inv.file_hash or None
            if file_hash and c.execute("SELECT 1 FROM invoices WHERE file_hash = ?", (file_hash,)).fetchone():
                file_hash = None  # same file imported twice deliberately; keep both rows
            cur = c.execute(
                "INSERT INTO invoices(vendor_id, invoice_number, invoice_date, total, file_path, "
                "original_name, file_hash, method, imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (vendor_id, inv.invoice_number.strip(), inv.invoice_date, inv.total,
                 archived_path or inv.source_path, Path(inv.source_path).name if inv.source_path else "",
                 file_hash, inv.method, ts))
            invoice_id = cur.lastrowid
            for line in inv.lines:
                product_id = self._product_id(c, line, match_mfr, ts)
                if product_id is None:
                    continue
                c.execute(
                    "INSERT INTO price_history(product_id, invoice_id, vendor_id, price_date, description, "
                    "quantity, uom, unit_price, price_per, unit_cost, extended) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (product_id, invoice_id, vendor_id, price_date, line.description.strip(), line.quantity,
                     line.uom or "EA", line.unit_price, line.price_per or 1, line.unit_cost,
                     line.extended if line.extended is not None else line.computed_extended))
        return invoice_id

    @staticmethod
    def _product_id(c: Backend, line: LineItem, match_mfr: bool, ts: str) -> int | None:
        """Find or create the product for an invoice line.

        Item # only (default): any product with the same item # matches; one with the same
        manufacturer is preferred if several exist.
        Item # + manufacturer: the manufacturer must match too. A line without a manufacturer
        joins the item's only product, and a product with no manufacturer yet is claimed by the
        first line that has one."""
        key = item_key(line.item_number)
        if not key:
            return None
        mk = mfr_key(line.manufacturer)
        rows = c.execute("SELECT id, mfr_key, name FROM products WHERE item_key = ? ORDER BY id", (key,)).fetchall()
        exact = [r for r in rows if r["mfr_key"] == mk]
        unknown = [r for r in rows if r["mfr_key"] == ""]
        if exact:
            row = exact[0]
        elif not match_mfr and rows:
            row = rows[0]
        elif match_mfr and not mk and len(rows) == 1:
            row = rows[0]
        elif match_mfr and mk and unknown:
            row = unknown[0]
        else:
            return c.execute(
                "INSERT INTO products(item_number, item_key, manufacturer, mfr_key, name, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (line.item_number.strip(), key, line.manufacturer.strip().upper(), mk, line.description.strip(),
                 ts, ts)).lastrowid
        if not row["name"] and line.description.strip():
            c.execute("UPDATE products SET name = ? WHERE id = ?", (line.description.strip(), row["id"]))
        if mk and not row["mfr_key"]:
            c.execute("UPDATE products SET manufacturer = ?, mfr_key = ? WHERE id = ?",
                      (line.manufacturer.strip().upper(), mk, row["id"]))
        return row["id"]

    def delete_invoice(self, invoice_id: int) -> None:
        with self.transaction() as c:
            c.execute("UPDATE inbox SET status = ?, updated_at = ? WHERE invoice_id = ?",
                      (INBOX_IGNORED, now_iso(), invoice_id))
            c.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
            # Products with no remaining history are orphans; remove them.
            c.execute("DELETE FROM products WHERE id NOT IN (SELECT DISTINCT product_id FROM price_history)")

    def list_invoices(self, search: str = "") -> list[Row]:
        like = f"%{search.strip()}%"
        return self.query(
            """
            SELECT i.id, i.invoice_number, i.invoice_date, i.total, i.file_path, i.original_name,
                   i.method, i.imported_at, v.name AS vendor,
                   COUNT(ph.id) AS line_count, COALESCE(SUM(ph.extended), 0) AS lines_total
            FROM invoices i
            LEFT JOIN vendors v ON v.id = i.vendor_id
            LEFT JOIN price_history ph ON ph.invoice_id = i.id
            WHERE ? = '' OR i.invoice_number LIKE ? OR v.name LIKE ? OR i.original_name LIKE ?
               OR i.id IN (SELECT ph2.invoice_id FROM price_history ph2 JOIN products p ON p.id = ph2.product_id
                           WHERE p.item_number LIKE ? OR p.name LIKE ?)
            GROUP BY i.id
            ORDER BY i.invoice_date DESC, i.id DESC
            """, (search.strip(), like, like, like, like, like))

    def invoice_lines(self, invoice_id: int) -> list[Row]:
        return self.query(
            """
            SELECT ph.*, p.item_number, p.manufacturer, p.name AS product_name
            FROM price_history ph JOIN products p ON p.id = ph.product_id
            WHERE ph.invoice_id = ? ORDER BY ph.id
            """, (invoice_id,))

    # ---- products -------------------------------------------------------
    _PRODUCT_SQL = """
        WITH ranked AS (
            SELECT ph.*, v.name AS vendor_name,
                   ROW_NUMBER() OVER (PARTITION BY ph.product_id ORDER BY ph.price_date DESC, ph.id DESC) AS rn
            FROM price_history ph LEFT JOIN vendors v ON v.id = ph.vendor_id
        ),
        stats AS (
            SELECT product_id, MIN(unit_cost) AS min_cost, MAX(unit_cost) AS max_cost,
                   AVG(unit_cost) AS avg_cost, COUNT(*) AS purchases, SUM(quantity) AS total_qty
            FROM price_history GROUP BY product_id
        )
        SELECT p.id, p.item_number, p.manufacturer, p.name, p.category, p.notes,
               l.unit_price AS last_price, l.price_per AS last_per, l.unit_cost AS last_cost,
               l.price_date AS last_date, l.vendor_name AS last_vendor, l.uom AS uom,
               pr.unit_cost AS prev_cost, pr.price_date AS prev_date,
               s.min_cost, s.max_cost, s.avg_cost, s.purchases, s.total_qty
        FROM products p
        LEFT JOIN ranked l ON l.product_id = p.id AND l.rn = 1
        LEFT JOIN ranked pr ON pr.product_id = p.id AND pr.rn = 2
        LEFT JOIN stats s ON s.product_id = p.id
    """

    def list_products(self, search: str = "") -> list[Row]:
        like = f"%{search.strip()}%"
        return self.query(
            self._PRODUCT_SQL + " WHERE ? = '' OR p.item_number LIKE ? OR p.name LIKE ? OR p.category LIKE ? "
            "OR p.manufacturer LIKE ? ORDER BY p.item_number COLLATE NOCASE",
            (search.strip(), like, like, like, like))

    def get_product(self, product_id: int) -> Row | None:
        rows = self.query(self._PRODUCT_SQL + " WHERE p.id = ?", (product_id,))
        return rows[0] if rows else None

    def recent_price_changes(self, limit: int = 50) -> list[Row]:
        return self.query(
            self._PRODUCT_SQL + " WHERE pr.unit_cost IS NOT NULL AND ABS(l.unit_cost - pr.unit_cost) > 1e-9 "
            "ORDER BY l.price_date DESC, ABS((l.unit_cost - pr.unit_cost) / pr.unit_cost) DESC LIMIT ?",
            (limit,))

    def product_history(self, product_id: int) -> list[Row]:
        return self.query(
            """
            SELECT ph.*, v.name AS vendor, i.invoice_number, i.id AS invoice_id
            FROM price_history ph
            LEFT JOIN vendors v ON v.id = ph.vendor_id
            LEFT JOIN invoices i ON i.id = ph.invoice_id
            WHERE ph.product_id = ? ORDER BY ph.price_date, ph.id
            """, (product_id,))

    def update_product(self, product_id: int, name: str, category: str, notes: str,
                       manufacturer: str | None = None, item_number: str | None = None) -> int:
        """Updates a product. If the new item # or manufacturer matches another product
        (same item # and manufacturer), the two are merged; returns the surviving product id."""
        with self.transaction() as c:
            row = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if row is None:
                raise ValueError("Product no longer exists.")
            number = row["item_number"] if item_number is None else item_number.strip()
            if not item_key(number):
                raise ValueError("Enter a valid item #.")
            if manufacturer is not None or item_number is not None:
                mfr = row["manufacturer"] if manufacturer is None else manufacturer.strip().upper()
                key = item_key(number)
                other = c.execute("SELECT id FROM products WHERE item_key = ? AND mfr_key = ? AND id != ?",
                                  (key, mfr_key(mfr), product_id)).fetchone()
                if other:
                    c.execute("UPDATE price_history SET product_id = ? WHERE product_id = ?", (other["id"], product_id))
                    c.execute("DELETE FROM products WHERE id = ?", (product_id,))
                    product_id = other["id"]
                else:
                    c.execute("UPDATE products SET item_number = ?, item_key = ?, manufacturer = ?, mfr_key = ? "
                              "WHERE id = ?", (number, key, mfr, mfr_key(mfr), product_id))
            c.execute("UPDATE products SET name = ?, category = ?, notes = ?, updated_at = ? WHERE id = ?",
                      (name.strip(), category.strip(), notes.strip(), now_iso(), product_id))
        return product_id

    def reassign_purchase(self, history_id: int, item_number: str, manufacturer: str, name: str,
                          target_product_id: int | None = None) -> int:
        """Move one purchase without changing its invoice, description, quantities or prices."""
        if not item_key(item_number):
            raise ValueError("Enter a valid item #.")
        with self.transaction() as c:
            row = c.execute("SELECT product_id FROM price_history WHERE id = ?", (history_id,)).fetchone()
            if row is None:
                raise ValueError("Purchase no longer exists.")
            if target_product_id is not None:
                if c.execute("SELECT 1 FROM products WHERE id = ?", (target_product_id,)).fetchone() is None:
                    raise ValueError("Selected product no longer exists.")
                target = target_product_id
            else:
                target = self._product_id(c, LineItem(
                    item_number=item_number, manufacturer=manufacturer, description=name),
                    self.get_bool("match_manufacturer"), now_iso())
            c.execute("UPDATE price_history SET product_id = ? WHERE id = ?", (target, history_id))
            c.execute("DELETE FROM products WHERE id = ? AND NOT EXISTS "
                      "(SELECT 1 FROM price_history WHERE product_id = ?)", (row["product_id"], row["product_id"]))
        return target

    def stats(self) -> dict[str, int]:
        row = self.query_one(
            "SELECT (SELECT COUNT(*) FROM products) AS products, (SELECT COUNT(*) FROM invoices) AS invoices, "
            "(SELECT COUNT(*) FROM vendors) AS vendors, "
            "(SELECT COUNT(*) FROM inbox WHERE status IN ('pending','failed')) AS review")
        return dict(row)

    # ---- inbox ----------------------------------------------------------
    def inbox_by_hash(self, file_hash: str) -> Row | None:
        return self.query_one("SELECT * FROM inbox WHERE file_hash = ?", (file_hash,))

    def inbox_get(self, inbox_id: int) -> Row | None:
        return self.query_one("SELECT * FROM inbox WHERE id = ?", (inbox_id,))

    def inbox_upsert(self, file_path: str, file_hash: str, file_size: int, source: str) -> int:
        ts = now_iso()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO inbox(file_path, file_hash, file_size, source, status, detected_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_hash) DO UPDATE SET file_path = excluded.file_path, "
                "source = excluded.source, status = excluded.status, error = NULL, updated_at = excluded.updated_at",
                (file_path, file_hash, file_size, source, INBOX_PROCESSING, ts, ts))
            return c.execute("SELECT id FROM inbox WHERE file_hash = ?", (file_hash,)).fetchone()["id"]

    def inbox_update(self, inbox_id: int, status: str, parsed: ParsedInvoice | None = None,
                     error: str | None = None, invoice_id: int | None = None) -> None:
        sets = ["status = ?", "updated_at = ?", "error = ?"]
        params: list = [status, now_iso(), error]
        if parsed is not None:
            sets.append("parsed_json = ?")
            params.append(parsed.to_json())
        if invoice_id is not None:
            sets.append("invoice_id = ?")
            params.append(invoice_id)
        params.append(inbox_id)
        self.execute(f"UPDATE inbox SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def inbox_list(self, active_only: bool = True) -> list[Row]:
        where = f"WHERE status IN ({','.join('?' * len(INBOX_ACTIVE))})" if active_only else ""
        return self.query(f"SELECT * FROM inbox {where} ORDER BY detected_at DESC, id DESC",
                          INBOX_ACTIVE if active_only else ())

    def inbox_delete(self, inbox_id: int) -> None:
        self.execute("DELETE FROM inbox WHERE id = ?", (inbox_id,))

    # ---- maintenance ----------------------------------------------------
    def backup(self, dest: str | Path) -> None:
        with self._lock:
            self.backend.backup(dest)

    def export_products_csv(self, dest: str | Path) -> int:
        rows = self.list_products()
        with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Item #", "Manufacturer", "Name", "Category", "Last Price", "Price Per", "Last Unit Cost",
                        "Previous Unit Cost", "Change %", "Min Unit Cost", "Max Unit Cost", "Avg Unit Cost",
                        "Last Vendor", "Last Date", "Purchases", "Total Qty"])
            for r in rows:
                change = ((r["last_cost"] - r["prev_cost"]) / r["prev_cost"] * 100
                          if r["prev_cost"] and r["last_cost"] is not None else None)
                w.writerow([r["item_number"], r["manufacturer"], r["name"], r["category"], r["last_price"], r["last_per"],
                            r["last_cost"], r["prev_cost"], f"{change:.2f}" if change is not None else "",
                            r["min_cost"], r["max_cost"], r["avg_cost"], r["last_vendor"], r["last_date"],
                            r["purchases"], r["total_qty"]])
        return len(rows)

    def export_history_csv(self, dest: str | Path) -> int:
        rows = self.query(
            """
            SELECT ph.price_date, v.name AS vendor, i.invoice_number, p.manufacturer, p.item_number, p.name,
                   ph.description, ph.quantity, ph.uom, ph.unit_price, ph.price_per, ph.unit_cost, ph.extended
            FROM price_history ph JOIN products p ON p.id = ph.product_id
            LEFT JOIN vendors v ON v.id = ph.vendor_id LEFT JOIN invoices i ON i.id = ph.invoice_id
            ORDER BY ph.price_date, ph.id
            """)
        with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Date", "Vendor", "Invoice #", "Manufacturer", "Item #", "Product Name",
                        "Invoice Description", "Qty", "UOM", "Unit Price", "Price Per", "Unit Cost", "Extended"])
            cols = ("price_date", "vendor", "invoice_number", "manufacturer", "item_number", "name",
                    "description", "quantity", "uom", "unit_price", "price_per", "unit_cost", "extended")
            for r in rows:
                w.writerow([r[k] for k in cols])
        return len(rows)


def open_database() -> Database:
    """Open the database configured in database.json (local SQLite file by default)."""
    from invoice_reader import config
    from invoice_reader.backends import create_backend

    return Database(create_backend(config.load_db_config()))


# Insert order respects foreign keys; ids are preserved so references stay intact.
_COPY_TABLES = ["settings", "vendors", "products", "invoices", "price_history", "inbox", "vendor_aliases"]


def copy_database(src: Database, dst: Database) -> dict[str, int]:
    """Copy all data from one database to an empty one (e.g. local file -> new server
    database). Returns row counts per table."""
    for table in ("invoices", "products", "price_history"):
        if dst.query_one(f"SELECT 1 AS x FROM {table} LIMIT 1") is not None:
            raise ValueError("The destination database already contains data. "
                             "Copying is only supported into an empty database.")
    counts: dict[str, int] = {}
    with dst.transaction() as c:
        for table in _COPY_TABLES:
            rows = [dict(r) for r in src.query(f"SELECT * FROM {table}")]
            counts[table] = len(rows)
            if not rows:
                continue
            cols = list(rows[0])
            sql = (f"INSERT INTO {table}({', '.join(cols)}) "
                   f"VALUES({', '.join('?' * len(cols))})")
            for row in rows:
                c.execute(sql, tuple(row[col] for col in cols))
    return counts
