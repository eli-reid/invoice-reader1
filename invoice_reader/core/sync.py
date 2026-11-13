"""Two-way synchronization between the local working database and a shared
server database (MySQL/MariaDB).

The local SQLite file is always what the app reads and writes; when a server is
configured it acts as a hub: each PC pushes invoices and deletions the server
doesn't know about and pulls the ones it is missing. Invoices carry a global
`sync_uid`, deletions leave tombstones in `sync_deletions`, and product details
(name / category / notes) follow the most recently edited copy.
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from invoice_reader import config
from invoice_reader.db import Database, DuplicateInvoiceError, now_iso
from invoice_reader.models import LineItem, ParsedInvoice

log = logging.getLogger(__name__)


# ---- pure sync algorithm (works on any two Database objects) --------------

def sync_databases(local: Database, remote: Database) -> dict[str, int]:
    """Merge two databases; returns counts of what changed."""
    counts = {"pushed": 0, "pulled": 0, "deleted_local": 0, "deleted_remote": 0,
              "adopted": 0, "products": 0, "aliases": 0}

    # 1. Deletions win over existence: propagate tombstones both ways first.
    local_dels = {r["uid"] for r in local.query("SELECT uid FROM sync_deletions")}
    remote_dels = {r["uid"] for r in remote.query("SELECT uid FROM sync_deletions")}
    for uid in remote_dels - local_dels:
        counts["deleted_local"] += _apply_deletion(local, uid)
    for uid in local_dels - remote_dels:
        counts["deleted_remote"] += _apply_deletion(remote, uid)
    deleted = local_dels | remote_dels

    # 2. Invoices: push what the server is missing, pull what we are missing.
    local_uids = {r["sync_uid"]: r["id"] for r in
                  local.query("SELECT id, sync_uid FROM invoices WHERE sync_uid IS NOT NULL")}
    remote_uids = {r["sync_uid"]: r["id"] for r in
                   remote.query("SELECT id, sync_uid FROM invoices WHERE sync_uid IS NOT NULL")}
    for uid in set(local_uids) - set(remote_uids) - deleted:
        counts["pushed" if _transfer_invoice(local, remote, local_uids[uid], uid) else "adopted"] += 1
    for uid in set(remote_uids) - set(local_uids) - deleted:
        counts["pulled" if _transfer_invoice(remote, local, remote_uids[uid], uid) else "adopted"] += 1

    # 3. Product details (name / category / notes): most recent edit wins.
    counts["products"] = _sync_product_details(local, remote)

    # 4. Vendor aliases: union of both sides.
    local_aliases, remote_aliases = local.vendor_aliases(), remote.vendor_aliases()
    for alias, vendor in remote_aliases.items():
        if alias not in local_aliases:
            local.add_vendor_alias(alias, vendor)
            counts["aliases"] += 1
    for alias, vendor in local_aliases.items():
        if alias not in remote_aliases:
            remote.add_vendor_alias(alias, vendor)
            counts["aliases"] += 1
    return counts


def _apply_deletion(db: Database, uid: str) -> int:
    row = db.query_one("SELECT id FROM invoices WHERE sync_uid = ?", (uid,))
    if row is not None:
        db.delete_invoice(row["id"])  # also records the tombstone locally
        return 1
    db.execute("INSERT OR IGNORE INTO sync_deletions(uid, deleted_at) VALUES(?, ?)", (uid, now_iso()))
    return 0


def _transfer_invoice(src: Database, dst: Database, src_id: int, uid: str) -> bool:
    """Copy one invoice with its lines. Returns True if copied; False if the target
    already had the same invoice (saved independently) and simply adopted the uid."""
    inv = src.query_one(
        "SELECT i.*, v.name AS vendor FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id "
        "WHERE i.id = ?", (src_id,))
    lines = [LineItem(
        item_number=l["item_number"], description=l["description"], quantity=l["quantity"],
        uom=l["uom"], unit_price=l["unit_price"], price_per=l["price_per"], extended=l["extended"],
        manufacturer=l["manufacturer"] or "") for l in src.invoice_lines(src_id)]
    parsed = ParsedInvoice(
        source_path=inv["file_path"] or "", file_hash=inv["file_hash"] or "",
        vendor=inv["vendor"] or "", invoice_number=inv["invoice_number"] or "",
        invoice_date=inv["invoice_date"], total=inv["total"], method=inv["method"] or "",
        lines=lines)
    try:
        dst.save_invoice(parsed, archived_path=inv["file_path"], sync_uid=uid)
        return True
    except DuplicateInvoiceError as exc:
        # The same invoice was imported independently on both sides under different
        # uids; both rows converge on the deterministically smaller uid.
        existing = dst.query_one("SELECT sync_uid FROM invoices WHERE id = ?", (exc.invoice_id,))
        winner = min(uid, existing["sync_uid"] or uid)
        dst.execute("UPDATE invoices SET sync_uid = ? WHERE id = ?", (winner, exc.invoice_id))
        src.execute("UPDATE invoices SET sync_uid = ? WHERE id = ?", (winner, src_id))
        return False


def _sync_product_details(a: Database, b: Database) -> int:
    changed = 0
    products_a = _product_map(a)
    products_b = _product_map(b)
    for key, pa in products_a.items():
        pb = products_b.get(key)
        if pb is None:
            continue
        ua, ub = pa["updated_at"] or "", pb["updated_at"] or ""
        if ua == ub:
            continue  # no reliable winner
        newer, older, older_db = (pa, pb, b) if ua > ub else (pb, pa, a)
        fields = ("name", "category", "notes")
        if any(newer[f] != older[f] for f in fields):
            older_db.execute(
                "UPDATE products SET name = ?, category = ?, notes = ?, updated_at = ? WHERE id = ?",
                (newer["name"], newer["category"], newer["notes"], newer["updated_at"], older["id"]))
            changed += 1
    return changed


def _product_map(db: Database) -> dict[tuple[str, str], dict]:
    return {(r["item_key"], r["mfr_key"]): dict(r) for r in db.query(
        "SELECT id, item_key, mfr_key, name, category, notes, updated_at FROM products")}


# ---- background manager ----------------------------------------------------

class SyncManager(QObject):
    """Runs syncs off the UI thread: at startup, after local changes (debounced),
    periodically, and on demand."""

    PERIODIC_MS = 5 * 60 * 1000
    DEBOUNCE_MS = 4000

    status_changed = Signal(str)   # short status for the settings page
    finished = Signal(bool, str)   # ok, summary/error
    pulled_changes = Signal()      # remote changes landed locally; refresh the UI

    def __init__(self, db: Database, parent: QObject | None = None):
        super().__init__(parent)
        self.db = db
        self.enabled = False
        self.status = "Server sync is off"
        self._thread: threading.Thread | None = None
        self._rerun = False
        self._periodic = QTimer(self, timeout=self.request_sync)
        self._debounce = QTimer(self, singleShot=True, interval=self.DEBOUNCE_MS, timeout=self._start)
        self.finished.connect(self._on_finished)

    def configure(self) -> None:
        """(Re)read database.json; starts syncing immediately when enabled."""
        cfg = config.load_db_config()
        self.enabled = cfg.get("engine") == "mysql" and bool(cfg.get("host") and cfg.get("database"))
        self._periodic.stop()
        if self.enabled:
            self._periodic.start(self.PERIODIC_MS)
            self.request_sync()
        else:
            self._set_status("Server sync is off")

    def request_sync(self) -> None:
        """Debounced trigger; safe to call after every local change."""
        if self.enabled:
            self._debounce.start()

    def _set_status(self, text: str) -> None:
        self.status = text
        self.status_changed.emit(text)

    def _start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            self._rerun = True
            return
        self._set_status("Syncing...")
        self._thread = threading.Thread(target=self._run, name="db-sync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from invoice_reader.backends import create_backend
        try:
            remote = Database(create_backend(config.load_db_config()))
            try:
                counts = sync_databases(self.db, remote)
            finally:
                remote.close()
        except Exception as exc:  # noqa: BLE001 - sync must never take the app down
            log.warning("Sync failed: %s", exc)
            self.finished.emit(False, str(exc))
            return
        log.info("Sync finished: %s", counts)
        parts = [f"{counts[k]} {label}" for k, label in
                 (("pushed", "sent"), ("pulled", "received"), ("adopted", "matched"),
                  ("deleted_local", "removed here"), ("deleted_remote", "removed on server"),
                  ("products", "product details"), ("aliases", "aliases")) if counts[k]]
        if counts["pulled"] or counts["deleted_local"] or counts["products"] or counts["aliases"]:
            self.pulled_changes.emit()
        self.finished.emit(True, ", ".join(parts) if parts else "already up to date")

    def _on_finished(self, ok: bool, summary: str) -> None:
        when = now_iso()[11:16]
        self._set_status(f"Last sync {when}: {summary}" if ok else f"Sync failed: {summary}")
        if self._rerun:
            self._rerun = False
            self.request_sync()
