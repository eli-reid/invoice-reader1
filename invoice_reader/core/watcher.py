"""Watches a folder (e.g. Downloads) and queues new invoice files for import.

Polling is the source of truth (robust across network drives / OneDrive);
QFileSystemWatcher only triggers an early scan. A file is imported only once
its size and timestamp are unchanged across two scans, so partially
downloaded files are never read.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from invoice_reader.core.importer import QUEUED, ImportManager
from invoice_reader.db import Database, now_iso
from invoice_reader.extraction.pipeline import is_supported

log = logging.getLogger(__name__)

TEMP_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut")
SETTLE_SECONDS = 2.0


class FolderWatcher(QObject):
    status_changed = Signal(str)

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

    @property
    def folder(self) -> Path | None:
        value = self.db.get_setting("watch_folder").strip()
        return Path(value) if value else None

    @property
    def enabled(self) -> bool:
        return self.db.get_bool("watch_enabled") and self.folder is not None

    def configure(self) -> None:
        self._timer.stop()
        if self._fs.directories():
            self._fs.removePaths(self._fs.directories())
        self._candidates.clear()
        folder = self.folder
        if not self.enabled:
            self._set_status("Folder watching is off")
            return
        if not folder.is_dir():
            self._set_status(f"Watch folder not found: {folder}")
            self._timer.start(30_000)  # it may be a drive that reconnects later
            return
        if not self.db.get_setting("watch_since"):
            self.db.set_setting("watch_since", now_iso())
        self._fs.addPath(str(folder))
        interval = max(3, int(self.db.get_setting("watch_interval") or 10))
        self._timer.start(interval * 1000)
        self._set_status(f"Watching {folder}")
        self.scan()

    def change_folder(self, folder: str) -> None:
        """New folder: only files arriving from now on are imported automatically."""
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

    def _iter_files(self, folder: Path):
        pattern = folder.rglob("*") if self.db.get_bool("watch_recursive") else folder.iterdir()
        for p in pattern:
            name = p.name.lower()
            if name.startswith((".", "~$")) or name.endswith(TEMP_SUFFIXES) or not is_supported(p):
                continue
            yield p

    def scan(self, include_existing: bool = False) -> int:
        """Returns how many files were queued. include_existing ignores the
        'only new files' cutoff (used for 'Scan existing files now')."""
        folder = self.folder
        if not self.enabled or folder is None:
            return 0
        if not folder.is_dir():
            self._set_status(f"Watch folder not found: {folder}")
            return 0
        if not any(Path(d) == folder for d in self._fs.directories()):
            self._fs.addPath(str(folder))  # folder (re)appeared, e.g. reconnected drive
        since = 0.0 if include_existing else self._since()
        now = time.time()
        queued = 0
        waiting = False
        try:
            for path in self._iter_files(folder):
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = (st.st_size, st.st_mtime)
                p = str(path)
                if self._handled.get(p) == key and not include_existing:
                    continue
                arrived = max(st.st_mtime, getattr(st, "st_birthtime", st.st_mtime))
                if arrived < since:
                    self._handled[p] = key
                    continue
                if st.st_size == 0:
                    continue
                stable = include_existing or (self._candidates.get(p) == key and now - st.st_mtime >= SETTLE_SECONDS)
                if not stable:
                    self._candidates[p] = key
                    waiting = True
                    continue
                try:
                    with open(path, "rb") as fh:
                        fh.read(1)
                except OSError:
                    waiting = True  # still locked by the browser / scanner
                    continue
                self._candidates.pop(p, None)
                self._handled[p] = key
                if self.importer.enqueue(path, source="watch") == QUEUED:
                    queued += 1
                    log.info("Queued %s from watch folder", path.name)
        except OSError as exc:
            log.warning("Watch folder scan failed: %s", exc)
            self._set_status(f"Cannot read watch folder: {exc}")
            return queued
        if waiting:
            self._soon.start()
        self.last_scan = datetime.now()
        self._set_status(f"Watching {folder} - last checked {self.last_scan:%I:%M:%S %p}")
        return queued
