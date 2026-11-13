"""Background import queue: parse files off the UI thread, park results in the
inbox for review, and commit reviewed invoices to the database."""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from invoice_reader import config
from invoice_reader.db import (
    INBOX_FAILED, INBOX_IGNORED, INBOX_IMPORTED, INBOX_PENDING, INBOX_PROCESSING, INBOX_SKIPPED,
    Database, DuplicateInvoiceError,
)
from invoice_reader.extraction.pipeline import file_sha256, is_supported, process_file
from invoice_reader.models import ParsedInvoice

log = logging.getLogger(__name__)

QUEUED, DUPLICATE, KNOWN, UNSUPPORTED, ERROR = "queued", "duplicate", "known", "unsupported", "error"


class _JobSignals(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)


class _ParseJob(QRunnable):
    def __init__(self, inbox_id: int, path: str, file_hash: str, vendors: list[str], exclude: list[str],
                 aliases: dict[str, str]):
        super().__init__()
        self.inbox_id, self.path, self.file_hash = inbox_id, path, file_hash
        self.vendors, self.exclude, self.aliases = vendors, exclude, aliases
        self.signals = _JobSignals()

    def run(self) -> None:
        try:
            parsed = process_file(self.path, self.vendors, self.file_hash, self.exclude, self.aliases)
            self.signals.finished.emit(self.inbox_id, parsed)
        except Exception as exc:  # noqa: BLE001 - report every failure to the inbox
            log.exception("Failed to process %s", self.path)
            self.signals.failed.emit(self.inbox_id, f"{type(exc).__name__}: {exc}")


class ImportManager(QObject):
    inbox_changed = Signal()
    data_changed = Signal()
    busy_changed = Signal(int)
    notify = Signal(str, str)

    def __init__(self, db: Database, parent: QObject | None = None):
        super().__init__(parent)
        self.db = db
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)  # OCR is CPU heavy; keep the UI responsive
        self._jobs: dict[int, _ParseJob] = {}
        self._notes: list[tuple[str, str]] = []
        self._flush = QTimer(self, singleShot=True, interval=1500, timeout=self._flush_notifications)

    @property
    def busy(self) -> int:
        return len(self._jobs)

    def company_names(self) -> list[str]:
        return [n.strip() for n in self.db.get_setting("company_names").split(";") if n.strip()]

    def resume(self) -> None:
        """Requeue files that were mid-processing when the app last exited."""
        for row in self.db.query("SELECT * FROM inbox WHERE status = ?", (INBOX_PROCESSING,)):
            if Path(row["file_path"]).exists():
                self._start(row["id"], row["file_path"], row["file_hash"])
            else:
                self.db.inbox_update(row["id"], INBOX_FAILED, error="File no longer exists.")
        self.inbox_changed.emit()

    def enqueue(self, path: str | Path, source: str = "manual", force: bool = False) -> str:
        path = Path(path)
        if not is_supported(path):
            return UNSUPPORTED
        try:
            file_hash = file_sha256(path)
            size = path.stat().st_size
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)
            return ERROR
        if not force and self.db.invoice_by_hash(file_hash):
            return DUPLICATE
        row = self.db.inbox_by_hash(file_hash)
        if row and not force:
            if row["id"] in self._jobs or source == "watch":
                return KNOWN
            if row["status"] in (INBOX_PENDING, INBOX_FAILED, INBOX_PROCESSING):
                return KNOWN
        inbox_id = self.db.inbox_upsert(str(path), file_hash, size, source)
        self._start(inbox_id, str(path), file_hash)
        self.inbox_changed.emit()
        return QUEUED

    def reprocess(self, inbox_id: int) -> str:
        row = self.db.inbox_get(inbox_id)
        if not row:
            return ERROR
        if not Path(row["file_path"]).exists():
            self.db.inbox_update(inbox_id, INBOX_FAILED, error="File no longer exists.")
            self.inbox_changed.emit()
            return ERROR
        return self.enqueue(row["file_path"], row["source"], force=True)

    def ignore(self, inbox_id: int) -> None:
        self.db.inbox_update(inbox_id, INBOX_IGNORED)
        self.inbox_changed.emit()

    def _start(self, inbox_id: int, path: str, file_hash: str) -> None:
        if inbox_id in self._jobs:
            return
        job = _ParseJob(inbox_id, path, file_hash, self.db.vendor_names(), self.company_names(),
                        self.db.vendor_aliases())
        job.setAutoDelete(False)
        job.signals.finished.connect(self._on_finished)
        job.signals.failed.connect(self._on_failed)
        self._jobs[inbox_id] = job
        self.pool.start(job)
        self.busy_changed.emit(self.busy)

    def _done(self, inbox_id: int) -> str:
        job = self._jobs.pop(inbox_id, None)
        self.busy_changed.emit(self.busy)
        return Path(job.path).name if job else ""

    def _on_failed(self, inbox_id: int, error: str) -> None:
        name = self._done(inbox_id)
        self.db.inbox_update(inbox_id, INBOX_FAILED, error=error)
        self._queue_note("Could not read invoice", f"{name}: {error}")
        self.inbox_changed.emit()

    def _on_finished(self, inbox_id: int, parsed: ParsedInvoice) -> None:
        name = self._done(inbox_id)
        row = self.db.inbox_get(inbox_id)
        if row is None:
            return
        if not parsed.looks_like_invoice:
            self.db.inbox_update(inbox_id, INBOX_SKIPPED, parsed=parsed, error="Not recognized as an invoice.")
            if row["source"] == "manual":
                self._queue_note("Not an invoice", f"{name} doesn't look like an invoice.")
            self.inbox_changed.emit()
            return
        self.db.inbox_update(inbox_id, INBOX_PENDING, parsed=parsed)
        if (self.db.get_bool("auto_import") and parsed.is_clean
                and not self.db.find_invoice(parsed.vendor, parsed.invoice_number)):
            try:
                self.commit(inbox_id, parsed)
                self._queue_note("Invoice imported",
                                 f"{parsed.vendor} #{parsed.invoice_number} - {len(parsed.lines)} line(s)")
                return
            except Exception:  # noqa: BLE001 - fall back to manual review
                log.exception("Auto-import failed for %s", name)
        self._queue_note("Invoice ready for review", f"{name} - {len(parsed.lines)} line(s) found")
        self.inbox_changed.emit()

    def commit(self, inbox_id: int, parsed: ParsedInvoice, allow_duplicate: bool = False) -> int:
        if not allow_duplicate:
            existing = self.db.find_invoice(parsed.vendor, parsed.invoice_number)
            if existing:
                raise DuplicateInvoiceError(existing)
        archived = self._archive(parsed)
        self._learn_vendor(inbox_id, parsed.vendor)
        invoice_id = self.db.save_invoice(parsed, archived, allow_duplicate=True)
        self.db.inbox_update(inbox_id, INBOX_IMPORTED, parsed=parsed, invoice_id=invoice_id)
        log.info("Imported invoice %s from %s (%d lines)", parsed.invoice_number, parsed.vendor, len(parsed.lines))
        self.inbox_changed.emit()
        self.data_changed.emit()
        return invoice_id

    def _learn_vendor(self, inbox_id: int, vendor: str) -> None:
        """If the user corrected a guessed vendor, remember the guess so the same
        letterhead maps to the right vendor next time."""
        row = self.db.inbox_get(inbox_id)
        if not row or not row["parsed_json"]:
            return
        guess = ParsedInvoice.from_json(row["parsed_json"]).vendor
        if guess and vendor and guess.lower() != vendor.lower():
            self.db.add_vendor_alias(guess, vendor)
            log.info("Learned vendor alias %r -> %r", guess, vendor)

    @staticmethod
    def _archive(parsed: ParsedInvoice) -> str | None:
        src = Path(parsed.source_path)
        if not src.exists():
            return None
        year = (parsed.invoice_date or "unknown")[:4]
        vendor = re.sub(r"[^\w\- ]", "_", parsed.vendor or "Unknown Vendor").strip() or "Unknown Vendor"
        dest_dir = config.archive_dir() / year / vendor
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{parsed.file_hash[:10]}_{src.name}"
        if not dest.exists():
            shutil.copy2(src, dest)
        return str(dest)

    def _queue_note(self, title: str, message: str) -> None:
        self._notes.append((title, message))
        self._flush.start()

    def _flush_notifications(self) -> None:
        notes, self._notes = self._notes, []
        if not notes:
            return
        if len(notes) == 1:
            self.notify.emit(*notes[0])
            return
        titles = {t for t, _ in notes}
        title = titles.pop() if len(titles) == 1 else "Invoice Reader"
        self.notify.emit(f"{title} ({len(notes)})", "\n".join(m for _, m in notes[:4])
                         + ("\n..." if len(notes) > 4 else ""))
