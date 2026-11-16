import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from invoice_reader.core import importer as imports
from invoice_reader.core import watcher as watching
from invoice_reader.db import INBOX_FAILED, INBOX_IGNORED, INBOX_IMPORTED, INBOX_PENDING, Database
from invoice_reader.models import LineItem, ParsedInvoice
from invoice_reader.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert predicate(), "Timed out waiting for background work"


def heartbeat(app):
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    loop = QEventLoop()
    QTimer.singleShot(60, loop.quit)
    timer.start()
    loop.exec()
    timer.stop()
    assert len(ticks) >= 3, "The UI event loop stopped responding"


def parsed_file(path, vendors, file_hash, exclusions, aliases):
    return ParsedInvoice(
        source_path=str(path), file_hash=file_hash, vendor="Test Vendor",
        invoice_number=os.path.basename(path), invoice_date="2026-01-01",
        lines=[LineItem("PART1", "Test part", 2, "FT", 3, 1, 6)])


@pytest.fixture
def queue(app, tmp_path, monkeypatch):
    db = Database(tmp_path / "invoices.db")
    manager = imports.ImportManager(db)
    monkeypatch.setattr(imports, "process_file", parsed_file)
    monkeypatch.setattr(imports.config, "archive_dir", lambda: tmp_path / "archive")
    yield db, manager
    wait_for(app, lambda: manager.busy == 0)
    assert manager.pool.waitForDone(5000)
    manager._changes.stop()
    manager._flush.stop()
    manager.deleteLater()
    app.processEvents()
    db.close()


def test_hashing_and_auto_archive_do_not_block_ui(app, queue, tmp_path, monkeypatch):
    db, manager = queue
    db.set_setting("auto_import", True)
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice bytes")
    main_thread = threading.get_ident()
    hashing, archiving, release_hash, release_archive = (threading.Event() for _ in range(4))
    original_hash, original_copy = imports.file_sha256, imports.shutil.copy2

    def blocked_hash(path):
        assert threading.get_ident() != main_thread
        hashing.set()
        assert release_hash.wait(5)
        return original_hash(path)

    def blocked_copy(src, dest):
        assert threading.get_ident() != main_thread
        archiving.set()
        assert release_archive.wait(5)
        return original_copy(src, dest)

    monkeypatch.setattr(imports, "file_sha256", blocked_hash)
    monkeypatch.setattr(imports.shutil, "copy2", blocked_copy)
    try:
        started = time.monotonic()
        assert manager.enqueue(source) == imports.QUEUED
        assert time.monotonic() - started < 0.1
        wait_for(app, hashing.is_set)
        heartbeat(app)
        release_hash.set()
        wait_for(app, archiving.is_set)
        heartbeat(app)
        assert db.query_one("SELECT COUNT(*) AS n FROM invoices")["n"] == 0
    finally:
        release_hash.set()
        release_archive.set()
    wait_for(app, lambda: manager.busy == 0)
    assert db.inbox_list(False)[0]["status"] == INBOX_IMPORTED
    invoice = db.query_one("SELECT * FROM invoices")
    assert db.invoice_lines(invoice["id"])[0]["extended"] == 6
    assert os.path.isfile(invoice["file_path"])


def test_batch_deduplicates_records_failures_and_batches_refreshes(app, queue, tmp_path, monkeypatch):
    db, manager = queue
    changed, progress = [], []
    manager.inbox_changed.connect(lambda: changed.append(1))
    manager.busy_changed.connect(progress.append)
    real_parse = imports.process_file

    def parse(path, *args):
        if str(path).endswith("bad.pdf"):
            raise ValueError("Broken document")
        return real_parse(path, *args)

    monkeypatch.setattr(imports, "process_file", parse)
    for i in range(80):
        path = tmp_path / f"invoice-{i}.pdf"
        path.write_bytes(str(i).encode())
        assert manager.enqueue(path) == imports.QUEUED
    duplicate = tmp_path / "copy.pdf"
    duplicate.write_bytes(b"0")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"unreadable pdf")
    manager.enqueue(duplicate)
    manager.enqueue(bad)
    assert manager.enqueue(bad) == imports.KNOWN
    assert manager.busy == 82
    wait_for(app, lambda: manager.busy == 0, timeout=10)
    rows = db.inbox_list(False)
    assert len(rows) == 81
    assert sum(r["status"] == INBOX_PENDING for r in rows) == 80
    failure = next(r for r in rows if r["status"] == INBOX_FAILED)
    assert "Broken document" in failure["error"]
    assert progress[-1] == 0
    assert len(changed) < 10
    assert len(changed) < len(rows)


@pytest.mark.parametrize("stage", ["parse", "archive"])
@pytest.mark.parametrize("action", ["ignore", "remove"])
def test_inflight_ignore_or_remove_is_not_undone(app, queue, tmp_path, monkeypatch, stage, action):
    db, manager = queue
    db.set_setting("auto_import", True)
    source = tmp_path / "cancel.pdf"
    source.write_bytes(b"cancel import")
    entered, release = threading.Event(), threading.Event()
    original = imports.process_file if stage == "parse" else imports.shutil.copy2

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    if stage == "parse":
        monkeypatch.setattr(imports, "process_file", blocked)
    else:
        monkeypatch.setattr(imports.shutil, "copy2", blocked)
    try:
        manager.enqueue(source)
        wait_for(app, entered.is_set)
        inbox_id = db.inbox_list()[0]["id"]
        if action == "ignore":
            manager.ignore(inbox_id)
        else:
            db.inbox_delete(inbox_id)
    finally:
        release.set()
    wait_for(app, lambda: manager.busy == 0)
    assert db.query_one("SELECT COUNT(*) AS n FROM invoices")["n"] == 0
    row = db.inbox_get(inbox_id)
    if action == "ignore":
        assert row["status"] == INBOX_IGNORED
    else:
        assert row is None


def test_resume_missing_file_and_reprocess(app, queue, tmp_path):
    db, manager = queue
    source = tmp_path / "missing.pdf"
    inbox_id = db.inbox_upsert(str(source), "old-hash", 12, "manual")
    manager.resume()
    wait_for(app, lambda: manager.busy == 0)
    assert db.inbox_get(inbox_id)["status"] == INBOX_FAILED
    source.write_bytes(b"now readable")
    assert manager.reprocess(inbox_id) == imports.QUEUED
    wait_for(app, lambda: manager.busy == 0)
    assert next(r for r in db.inbox_list() if r["status"] == INBOX_PENDING)


def test_folder_scan_is_async_and_discards_old_configuration(app, queue, tmp_path, monkeypatch):
    db, manager = queue
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "old.pdf").write_bytes(b"old")
    (new / "new.pdf").write_bytes(b"new")
    db.set_setting("watch_folder", str(old))
    watcher = watching.FolderWatcher(db, manager)
    entered, release = threading.Event(), threading.Event()
    original = watching.Path.iterdir

    def blocked(path):
        if path == old:
            entered.set()
            assert release.wait(5)
        return original(path)

    monkeypatch.setattr(watching.Path, "iterdir", blocked)
    try:
        watcher.scan(include_existing=True)
        wait_for(app, entered.is_set)
        heartbeat(app)
        watcher.change_folder(str(new))
        watcher.scan(include_existing=True)
    finally:
        release.set()
    try:
        wait_for(app, lambda: watcher._job is None and manager.busy == 0)
        rows = db.inbox_list(False)
        assert len(rows) == 1
        assert os.path.basename(rows[0]["file_path"]) == "new.pdf"
    finally:
        watcher.stop()
        assert watcher.pool.waitForDone(5000)
        watcher.deleteLater()


def test_quit_waits_without_blocking_and_drains_submitted_files(app, queue, tmp_path, monkeypatch):
    db, manager = queue
    entered, release = threading.Event(), threading.Event()
    original = imports.file_sha256

    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(imports, "file_sha256", blocked)
    for i in range(2):
        path = tmp_path / f"quit-{i}.pdf"
        path.write_bytes(str(i).encode())
        manager.enqueue(path)
    window = Mock()
    window._quitting = True
    window.importer = manager
    window.watcher.pool.activeThreadCount.return_value = 0
    monkeypatch.setattr("invoice_reader.ui.main_window.QApplication.quit", Mock())
    try:
        wait_for(app, entered.is_set)
        event = QCloseEvent()
        MainWindow.closeEvent(window, event)
        assert not event.isAccepted()
        assert not manager.accepting
        window.watcher.stop.assert_called_once()
        heartbeat(app)
    finally:
        release.set()
    wait_for(app, lambda: manager.busy == 0)
    assert manager.pool.waitForDone(5000)
    assert len(db.inbox_list()) == 2
    MainWindow.closeEvent(window, event)
    assert event.isAccepted()


def test_auto_save_transaction_does_not_lock_ui_connection(app, queue, tmp_path, monkeypatch):
    db, manager = queue
    db.set_setting("auto_import", True)
    source = tmp_path / "many-lines.pdf"
    source.write_bytes(b"many lines")
    entered, release = threading.Event(), threading.Event()
    original = Database._product_id

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(Database, "_product_id", staticmethod(blocked))
    try:
        manager.enqueue(source)
        wait_for(app, entered.is_set)
        assert db.query_one("SELECT COUNT(*) AS n FROM invoices")["n"] == 0
        heartbeat(app)
    finally:
        release.set()
    wait_for(app, lambda: manager.busy == 0)
    assert db.query_one("SELECT COUNT(*) AS n FROM invoices")["n"] == 1


def test_window_starts_and_displays_large_background_batch(app, queue, tmp_path, monkeypatch):
    db, _manager = queue
    monkeypatch.setenv("INVOICE_READER_DATA", str(tmp_path))
    db.set_setting("watch_enabled", False)
    window = MainWindow(db)
    entered, release = threading.Event(), threading.Event()
    original = imports.process_file

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(imports, "process_file", blocked)
    files = []
    for i in range(300):
        path = tmp_path / f"batch-{i}.pdf"
        path.write_bytes(str(i).encode())
        files.append(str(path))
    page = window.pages["inbox"]
    refresh = Mock(wraps=page.refresh)
    monkeypatch.setattr(page, "refresh", refresh)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    try:
        window.show()
        started = time.monotonic()
        window.import_files(files)
        assert time.monotonic() - started < 0.5
        wait_for(app, entered.is_set)
        heartbeat(app)
        release.set()
        timer.start()
        wait_for(app, lambda: window.importer.busy == 0, timeout=15)
        wait_for(app, lambda: page.table.model_.rowCount() == 300)
        assert not window.busy_label.text()
        assert refresh.call_count < 10
        assert len(ticks) > 5
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.75
    finally:
        release.set()
        timer.stop()
        wait_for(app, lambda: window.importer.busy == 0, timeout=15)
        assert window.importer.pool.waitForDone(5000)
        window.watcher.stop()
        window._refresh_timer.stop()
        window.importer._changes.stop()
        window.importer._flush.stop()
        window.tray.hide()
        window.hide()
        window.deleteLater()
        app.processEvents()
