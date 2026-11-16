"""Background import queue: parse files off the UI thread, park results in the
inbox for review, and commit reviewed invoices to the database."""
from __future__ import annotations

import logging
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from invoice_reader import config
from invoice_reader.db import (
    INBOX_FAILED, INBOX_IGNORED, INBOX_IMPORTED, INBOX_PENDING, INBOX_PROCESSING, INBOX_SKIPPED,
    Database, DuplicateInvoiceError, ImportCancelledError,
)
from invoice_reader.extraction.pipeline import file_sha256, is_supported, process_file
from invoice_reader.models import ParsedInvoice

log = logging.getLogger(__name__)

QUEUED, DUPLICATE, KNOWN, UNSUPPORTED, ERROR = "queued", "duplicate", "known", "unsupported", "error"


class _JobSignals(QObject):
    finished = Signal(str, object)


@dataclass
class _ImportResult:
    outcome: str
    title: str = ""
    message: str = ""
    saved: bool = False


class _ImportJob(QRunnable):
    def __init__(self, db: Database, key: str, path: str, source: str, force: bool,
                 inbox_id: int | None = None):
        super().__init__()
        self.db, self.key, self.path = db, key, path
        self.source, self.force, self.inbox_id = source, force, inbox_id
        self.signals = _JobSignals()

    def run(self) -> None:
        db = self.db
        try:
            # WAL lets the UI keep reading while an automatic save writes many lines.
            if db.backend.engine == "sqlite" and db.path != ":memory:":
                db = Database(db.path)
            result = self._process(db)
        except Exception as exc:  # noqa: BLE001 - report every failure to the inbox
            log.exception("Failed to process %s", self.path)
            error = f"{type(exc).__name__}: {exc}"
            if self.inbox_id is not None:
                try:
                    db.inbox_update(self.inbox_id, INBOX_FAILED, error=error, expected_status=INBOX_PROCESSING)
                except Exception:
                    log.exception("Could not record import failure for %s", self.path)
            result = _ImportResult(ERROR, "Could not read invoice", f"{Path(self.path).name}: {error}")
        finally:
            if db is not self.db:
                db.close()
        self.signals.finished.emit(self.key, result)

    def _process(self, db: Database) -> _ImportResult:
        path = Path(self.path)
        name = path.name
        file_hash = file_sha256(path)
        if self.inbox_id is None:
            if not self.force and db.invoice_by_hash(file_hash):
                return _ImportResult(DUPLICATE, "Already imported", name)
            row = db.inbox_by_hash(file_hash)
            if row and not self.force:
                if self.source == "watch" or row["status"] in (
                        INBOX_PENDING, INBOX_FAILED, INBOX_PROCESSING):
                    return _ImportResult(KNOWN)
            self.inbox_id = db.inbox_upsert(str(path), file_hash, path.stat().st_size, self.source)
        parsed = process_file(
            self.path, db.vendor_names(), file_hash,
            [n.strip() for n in db.get_setting("company_names").split(";") if n.strip()],
            db.vendor_aliases())
        row = db.inbox_get(self.inbox_id)
        if row is None or row["status"] != INBOX_PROCESSING:
            return _ImportResult(KNOWN)  # removed or ignored while the worker was reading
        if not parsed.looks_like_invoice:
            db.inbox_update(self.inbox_id, INBOX_SKIPPED, parsed=parsed, error="Not recognized as an invoice.",
                            expected_status=INBOX_PROCESSING)
            return _ImportResult(QUEUED, "Not an invoice" if self.source == "manual" else "",
                                 f"{name} doesn't look like an invoice.")
        if (db.get_bool("auto_import") and parsed.is_clean
                and not db.find_invoice(parsed.vendor, parsed.invoice_number)):
            try:
                ImportManager._commit(db, self.inbox_id, parsed, automatic=True)
            except ImportCancelledError:
                return _ImportResult(KNOWN)
            except Exception as exc:  # retain the parsed invoice for manual review
                log.exception("Auto-import failed for %s", name)
                db.inbox_update(self.inbox_id, INBOX_PENDING, parsed=parsed,
                                error=f"Automatic save failed: {exc}", expected_status=INBOX_PROCESSING)
                return _ImportResult(ERROR, "Automatic save failed", f"{name}: {exc}. Ready for review.")
            else:
                return _ImportResult(QUEUED, "Invoice imported",
                                     f"{parsed.vendor} #{parsed.invoice_number} - {len(parsed.lines)} line(s)",
                                     saved=True)
        db.inbox_update(self.inbox_id, INBOX_PENDING, parsed=parsed, expected_status=INBOX_PROCESSING)
        return _ImportResult(QUEUED, "Invoice ready for review", f"{name} - {len(parsed.lines)} line(s) found")


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
        self._jobs: dict[str, _ImportJob] = {}
        self._pending: deque[tuple[str, str, str, bool, int | None]] = deque()
        self._paths: set[str] = set()
        self.accepting = True
        self._data_dirty = False
        self._changes = QTimer(self, singleShot=True, interval=500, timeout=self._emit_changes)
        self._notes: list[tuple[str, str]] = []
        self._flush = QTimer(self, singleShot=True, interval=1500, timeout=self._flush_notifications)

    @property
    def busy(self) -> int:
        return len(self._paths)

    def company_names(self) -> list[str]:
        return [n.strip() for n in self.db.get_setting("company_names").split(";") if n.strip()]

    def resume(self) -> None:
        """Requeue files that were mid-processing when the app last exited."""
        for row in self.db.query("SELECT * FROM inbox WHERE status = ?", (INBOX_PROCESSING,)):
            self._submit(row["file_path"], row["source"], False, row["id"])
        self._changed()

    def enqueue(self, path: str | Path, source: str = "manual", force: bool = False) -> str:
        """Schedule a file without reading it. Duplicate/error results arrive later."""
        path = Path(path)
        if not is_supported(path):
            return UNSUPPORTED
        if not self.accepting:
            return ERROR
        return self._submit(str(path), source, force)

    def reprocess(self, inbox_id: int) -> str:
        row = self.db.inbox_get(inbox_id)
        if not row:
            return ERROR
        return self.enqueue(row["file_path"], row["source"], force=True)

    def ignore(self, inbox_id: int) -> None:
        self.db.inbox_update(inbox_id, INBOX_IGNORED)
        self._changed()

    def _submit(self, path: str, source: str, force: bool, inbox_id: int | None = None) -> str:
        key = os.path.normcase(os.path.abspath(path))
        if key in self._paths:
            return KNOWN
        self._paths.add(key)
        self._pending.append((key, path, source, force, inbox_id))
        self._changed()
        if self.busy == 1:
            self.busy_changed.emit(self.busy)
            QTimer.singleShot(0, self._start_next)
        return QUEUED

    def _start_next(self) -> None:
        if self._jobs or not self._pending:
            return
        key, path, source, force, inbox_id = self._pending.popleft()
        job = _ImportJob(self.db, key, path, source, force, inbox_id)
        job.setAutoDelete(False)
        job.signals.finished.connect(self._on_finished)
        self._jobs[key] = job
        self.pool.start(job)
        self.busy_changed.emit(self.busy)

    def _on_finished(self, key: str, result: _ImportResult) -> None:
        self._jobs.pop(key, None)
        self._paths.discard(key)
        if result.title:
            self._queue_note(result.title, result.message)
        self._changed(result.saved)
        self.busy_changed.emit(self.busy)
        if not self.busy:
            self._changes.stop()
            self._emit_changes()
        else:
            self._start_next()

    def _changed(self, saved: bool = False) -> None:
        self._data_dirty |= saved
        if not self._changes.isActive():
            self._changes.start()

    def _emit_changes(self) -> None:
        saved, self._data_dirty = self._data_dirty, False
        self.inbox_changed.emit()
        if saved:
            self.data_changed.emit()

    def commit(self, inbox_id: int, parsed: ParsedInvoice, allow_duplicate: bool = False) -> int:
        invoice_id = self._commit(self.db, inbox_id, parsed, allow_duplicate)
        self._changed(saved=True)
        return invoice_id

    @staticmethod
    def _commit(db: Database, inbox_id: int, parsed: ParsedInvoice, allow_duplicate: bool = False,
                automatic: bool = False) -> int:
        if not allow_duplicate:
            existing = db.find_invoice(parsed.vendor, parsed.invoice_number)
            if existing:
                raise DuplicateInvoiceError(existing)
        archived = ImportManager._archive(parsed)
        if not automatic:
            ImportManager._learn_vendor(db, inbox_id, parsed.vendor)
        invoice_id = db.save_invoice(parsed, archived, allow_duplicate=True,
                                     processing_inbox_id=inbox_id if automatic else None)
        if not automatic:
            db.inbox_update(inbox_id, INBOX_IMPORTED, parsed=parsed, invoice_id=invoice_id)
        log.info("Imported invoice %s from %s (%d lines)", parsed.invoice_number, parsed.vendor, len(parsed.lines))
        return invoice_id

    @staticmethod
    def _learn_vendor(db: Database, inbox_id: int, vendor: str) -> None:
        """If the user corrected a guessed vendor, remember the guess so the same
        letterhead maps to the right vendor next time."""
        row = db.inbox_get(inbox_id)
        if not row or not row["parsed_json"]:
            return
        guess = ParsedInvoice.from_json(row["parsed_json"]).vendor
        if guess and vendor and guess.lower() != vendor.lower():
            db.add_vendor_alias(guess, vendor)
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
        if not self._flush.isActive():
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
