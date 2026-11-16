"""Background folder scanning with settling checks for incomplete downloads."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QRunnable, QThreadPool, QTimer, Signal

from invoice_reader.core.importer import QUEUED, ImportManager
from invoice_reader.db import Database, now_iso
from invoice_reader.extraction.pipeline import is_supported

log = logging.getLogger(__name__)

TEMP_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut")
SETTLE_SECONDS = 2.0


class _ScanSignals(QObject):
    finished = Signal(object)


class _ScanJob(QRunnable):
    def __init__(self, folder: Path, recursive: bool, since: float, include_existing: bool,
                 candidates: dict, handled: dict):
        super().__init__()
        self.folder, self.recursive = folder, recursive
        self.since, self.include_existing = since, include_existing
        self.candidates, self.handled = candidates.copy(), handled.copy()
        self.paths: list[str] = []
        self.waiting = False
        self.error = ""
        self.signals = _ScanSignals()

    def run(self) -> None:
        try:
            now = time.time()
            if not self.folder.is_dir():
                raise FileNotFoundError(f"Watch folder not found: {self.folder}")
            pattern = self.folder.rglob("*") if self.recursive else self.folder.iterdir()
            for path in pattern:
                name = path.name.lower()
                if name.startswith((".", "~$")) or name.endswith(TEMP_SUFFIXES) or not is_supported(path):
                    continue
                try:
                    st = path.stat()
                except OSError as exc:
                    log.warning("Cannot inspect watched file %s: %s", path, exc)
                    continue
                key, p = (st.st_size, st.st_mtime), str(path)
                if self.handled.get(p) == key and not self.include_existing:
                    continue
                arrived = max(st.st_mtime, getattr(st, "st_birthtime", st.st_mtime))
                if arrived < self.since:
                    self.handled[p] = key
                    continue
                if st.st_size == 0:
                    continue
                stable = self.include_existing or (
                    self.candidates.get(p) == key and now - st.st_mtime >= SETTLE_SECONDS)
                if not stable:
                    self.candidates[p] = key
                    self.waiting = True
                    continue
                try:
                    with open(path, "rb") as fh:
                        fh.read(1)
                except OSError:
                    self.waiting = True
                    continue
                self.candidates.pop(p, None)
                self.handled[p] = key
                self.paths.append(p)
        except Exception as exc:  # background failures must reach the status bar
            log.exception("Watch folder scan failed")
            self.error = str(exc)
        self.signals.finished.emit(self)


class FolderWatcher(QObject):
    status_changed = Signal(str)
    scan_finished = Signal(int)

    def __init__(self, db: Database, importer: ImportManager, parent: QObject | None = None):
        super().__init__(parent)
        self.db = db
        self.importer = importer
        self._timer = QTimer(self, timeout=self.scan)
        self._soon = QTimer(self, singleShot=True, interval=1500, timeout=self.scan)
        self._fs = QFileSystemWatcher(self)
        self._fs.directoryChanged.connect(lambda _p: self._soon.start())
        self._candidates: dict[str, tuple[int, float]] = {}
        self._handled: dict[str, tuple[int, float]] = {}
        self.status = ""
        self.last_scan: datetime | None = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._job: _ScanJob | None = None
        self._generation = 0
        self._scan_generation = 0
        self._include_existing = False
        self._stopping = False

    @property
    def folder(self) -> Path | None:
        value = self.db.get_setting("watch_folder").strip()
        return Path(value) if value else None

    @property
    def enabled(self) -> bool:
        return self.db.get_bool("watch_enabled") and self.folder is not None

    def configure(self) -> None:
        self._generation += 1
        self._timer.stop()
        self._soon.stop()
        if self._fs.directories():
            self._fs.removePaths(self._fs.directories())
        self._candidates.clear()
        self._include_existing = False
        if not self.enabled:
            self._set_status("Folder watching is off")
            return
        if not self.db.get_setting("watch_since"):
            self.db.set_setting("watch_since", now_iso())
        interval = max(3, int(self.db.get_setting("watch_interval") or 10))
        self._timer.start(interval * 1000)
        self.scan()

    def change_folder(self, folder: str) -> None:
        """Only files arriving after the folder change are imported automatically."""
        self.db.set_setting("watch_folder", folder)
        self.db.set_setting("watch_since", now_iso())
        self._handled.clear()
        self.configure()

    def _set_status(self, text: str) -> None:
        self.status = text
        self.status_changed.emit(text)

    def _since(self) -> float:
        value = self.db.get_setting("watch_since")
        try:
            return datetime.fromisoformat(value).timestamp() if value else 0.0
        except ValueError:
            return 0.0

    def scan(self, include_existing: bool = False) -> None:
        """Start a background scan; scan_finished reports the submitted file count."""
        folder = self.folder
        if self._stopping or not self.enabled or folder is None:
            return
        if self._job is not None:
            self._include_existing |= include_existing
            return
        job = _ScanJob(folder, self.db.get_bool("watch_recursive"),
                       0.0 if include_existing else self._since(), include_existing,
                       self._candidates, self._handled)
        job.setAutoDelete(False)
        job.signals.finished.connect(self._on_scan_finished)
        self._job, self._scan_generation = job, self._generation
        self._set_status(f"Scanning {folder}...")
        self.pool.start(job)

    def _on_scan_finished(self, job: _ScanJob) -> None:
        self._job = None
        if self._stopping:
            return
        if self._scan_generation != self._generation:
            include_existing, self._include_existing = self._include_existing, False
            self.scan(include_existing)
            return
        self._candidates, self._handled = job.candidates, job.handled
        queued = sum(self.importer.enqueue(p, source="watch") == QUEUED for p in job.paths)
        if job.error:
            self._set_status(f"Cannot read watch folder: {job.error}")
        else:
            if str(job.folder) not in self._fs.directories():
                self._fs.addPath(str(job.folder))
            self.last_scan = datetime.now()
            self._set_status(f"Watching {job.folder} - last checked {self.last_scan:%I:%M:%S %p}")
        self.scan_finished.emit(queued)
        if job.waiting:
            self._soon.start()
        if self._include_existing:
            self._include_existing = False
            self.scan(include_existing=True)

    def stop(self) -> None:
        self._stopping = True
        self._timer.stop()
        self._soon.stop()
        if self._fs.directories():
            self._fs.removePaths(self._fs.directories())
