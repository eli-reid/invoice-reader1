from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from invoice_reader import APP_NAME
from invoice_reader.core.importer import DUPLICATE, ERROR, QUEUED, UNSUPPORTED, ImportManager
from invoice_reader.core.watcher import FolderWatcher
from invoice_reader.db import INBOX_FAILED, INBOX_PENDING, INBOX_PROCESSING, Database
from invoice_reader.models import ParsedInvoice
from invoice_reader.ui.pages.dashboard import DashboardPage
from invoice_reader.ui.pages.inbox import InboxPage
from invoice_reader.ui.pages.invoices import InvoicesPage
from invoice_reader.ui.pages.products import ProductsPage
from invoice_reader.ui.pages.settings import SettingsPage
from invoice_reader.ui.review_dialog import ReviewDialog
from invoice_reader.ui.theme import Glyph, app_icon, glyph_icon
from invoice_reader.ui.widgets import FILE_FILTER, open_path

log = logging.getLogger(__name__)

NAV = [
    ("dashboard", "Dashboard", Glyph.HOME),
    ("inbox", "Import queue", Glyph.INBOX),
    ("products", "Products", Glyph.TAG),
    ("invoices", "Invoices", Glyph.DOCUMENT),
    ("settings", "Settings", Glyph.SETTINGS),
]


class MainWindow(QMainWindow):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._quitting = False
        self._tray_hint_shown = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)

        self.importer = ImportManager(db, self)
        self.watcher = FolderWatcher(db, self.importer, self)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(6, 16, 6, 12)
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        sl.addWidget(title)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setFocusPolicy(Qt.NoFocus)
        sl.addWidget(self.nav, 1)
        self.busy_label = QLabel()
        self.busy_label.setObjectName("Muted")
        self.busy_label.setWordWrap(True)
        self.busy_label.setContentsMargins(12, 0, 8, 0)
        sl.addWidget(self.busy_label)
        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.pages = {
            "dashboard": DashboardPage(self),
            "inbox": InboxPage(self),
            "products": ProductsPage(self),
            "invoices": InvoicesPage(self),
            "settings": SettingsPage(self),
        }
        self._dirty = set(self.pages)
        for key, label, glyph in NAV:
            item = QListWidgetItem(glyph_icon(glyph, size=32), label)
            item.setData(Qt.UserRole, key)
            item.setSizeHint(QSize(0, 40))
            self.nav.addItem(item)
            self.stack.addWidget(self.pages[key])
        self.nav.currentRowChanged.connect(self._on_nav)

        self.statusBar().setSizeGripEnabled(True)
        self.watch_label = QLabel()
        self.statusBar().addWidget(self.watch_label, 1)

        self._setup_tray()

        self.importer.inbox_changed.connect(self._on_inbox_changed)
        self.importer.data_changed.connect(self.data_changed)
        self.importer.busy_changed.connect(self._on_busy)
        self.importer.notify.connect(self.notify)
        self.watcher.status_changed.connect(self._on_watch_status)

        self.nav.setCurrentRow(0)
        self.importer.resume()
        self.watcher.configure()
        self._update_badge()

    # ---- navigation -----------------------------------------------------
    def navigate(self, key: str) -> None:
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.UserRole) == key:
                self.nav.setCurrentRow(i)
                return

    def _on_nav(self, row: int) -> None:
        key = self.nav.item(row).data(Qt.UserRole)
        self.stack.setCurrentWidget(self.pages[key])
        self._refresh_if_dirty(key)

    def _current_key(self) -> str:
        return self.nav.currentItem().data(Qt.UserRole) if self.nav.currentItem() else "dashboard"

    def _refresh_if_dirty(self, key: str) -> None:
        if key in self._dirty:
            self._dirty.discard(key)
            try:
                self.pages[key].refresh()
            except Exception:  # noqa: BLE001 - never let one page break the window
                log.exception("Failed to refresh page %s", key)

    def data_changed(self) -> None:
        self._dirty.update(self.pages)
        self._refresh_if_dirty(self._current_key())

    def show_product(self, product_id: int) -> None:
        self.navigate("products")
        self.pages["products"].select_product(product_id)

    def show_invoice(self, invoice_id: int) -> None:
        self.navigate("invoices")
        self.pages["invoices"].select_invoice(invoice_id)

    # ---- importing ------------------------------------------------------
    def browse_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Import invoices", self.db.get_setting("last_import_dir"),
                                                FILE_FILTER)
        if files:
            self.db.set_setting("last_import_dir", str(Path(files[0]).parent))
            self.import_files(files)

    def import_files(self, files: list[str]) -> None:
        results = {QUEUED: 0, DUPLICATE: 0, UNSUPPORTED: 0, ERROR: 0, "known": 0}
        for f in files:
            outcome = self.importer.enqueue(f, source="manual")
            results[outcome] = results.get(outcome, 0) + 1
        parts = []
        if results[QUEUED]:
            parts.append(f"{results[QUEUED]} file(s) are being read")
        if results[DUPLICATE]:
            parts.append(f"{results[DUPLICATE]} already imported")
        if results["known"]:
            parts.append(f"{results['known']} already in the queue")
        if results[UNSUPPORTED]:
            parts.append(f"{results[UNSUPPORTED]} unsupported")
        if results[ERROR]:
            parts.append(f"{results[ERROR]} could not be opened")
        self.statusBar().showMessage("; ".join(parts), 8000)
        if results[QUEUED]:
            self.navigate("inbox")

    def review_inbox(self, inbox_id: int) -> bool:
        """Opens the review dialog. Returns False if the user cancelled."""
        row = self.db.inbox_get(inbox_id)
        if row is None:
            return True
        if row["status"] == INBOX_PROCESSING:
            QMessageBox.information(self, "Still reading", "This file is still being read. Try again shortly.")
            return True
        if row["status"] == INBOX_FAILED or not row["parsed_json"]:
            answer = QMessageBox.question(
                self, "Could not read file",
                f"{Path(row['file_path']).name} could not be read:\n\n{row['error'] or 'Unknown error'}\n\n"
                "Try reading it again?")
            if answer == QMessageBox.Yes:
                self.importer.reprocess(inbox_id)
            return True
        if row["status"] != INBOX_PENDING:
            if row["invoice_id"]:
                self.show_invoice(row["invoice_id"])
                return True
            if QMessageBox.question(self, "Review again?",
                                    "This file was not imported. Review and import it now?") != QMessageBox.Yes:
                return True
        parsed = ParsedInvoice.from_json(row["parsed_json"])
        if not Path(parsed.source_path).exists():
            parsed.source_path = row["file_path"]
        dlg = ReviewDialog(parsed, inbox_id, self.importer, parent=self)
        result = dlg.exec()
        if dlg.invoice_id:
            self.statusBar().showMessage(f"Saved invoice {parsed.invoice_number or ''} to the database", 6000)
        return result != 0

    # ---- watch folder ---------------------------------------------------
    def scan_now(self) -> None:
        if not self.watcher.enabled:
            QMessageBox.information(self, "Watch folder off", "Turn on the watch folder in Settings first.")
            return
        n = self.watcher.scan()
        self.statusBar().showMessage(f"Scan complete - {n} new file(s) queued" if n else
                                     "Scan complete - no new invoices", 6000)

    def choose_watch_folder(self) -> None:
        current = self.db.get_setting("watch_folder")
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder your invoices are downloaded to",
                                                  current)
        if folder:
            self.watcher.change_folder(str(Path(folder)))
            self.db.set_setting("watch_enabled", True)
            self.watcher.configure()
            self.pages["settings"].watch_enabled.setChecked(True)
            self._dirty.update({"settings", "dashboard"})
            self._refresh_if_dirty(self._current_key())

    def open_watch_folder(self) -> None:
        folder = self.watcher.folder
        if folder and folder.is_dir():
            open_path(folder)

    def _on_watch_status(self, text: str) -> None:
        self.watch_label.setText(text)
        self.pages["dashboard"].set_watch_status(text)
        self.tray.setToolTip(f"{APP_NAME}\n{text}"[:127])

    def _on_busy(self, count: int) -> None:
        self.busy_label.setText(f"\u23f3 Reading {count} file(s)..." if count else "")
        if count == 0:
            self._on_inbox_changed()

    def _on_inbox_changed(self) -> None:
        self._update_badge()
        self._dirty.update({"inbox", "dashboard", "invoices"})
        self._refresh_if_dirty(self._current_key())

    def _update_badge(self) -> None:
        pending = self.db.query_one(
            "SELECT COUNT(*) AS n FROM inbox WHERE status IN (?, ?)", (INBOX_PENDING, INBOX_FAILED))["n"]
        item = self.nav.item(1)
        item.setText(f"Import queue  ({pending})" if pending else "Import queue")

    # ---- tray / lifecycle -----------------------------------------------
    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Open Invoice Reader", self.bring_to_front)
        menu.addAction("Review queue", lambda: (self.bring_to_front(), self.navigate("inbox")))
        menu.addAction("Scan watch folder now", self.scan_now)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.bring_to_front() if reason in (QSystemTrayIcon.Trigger,
                                                               QSystemTrayIcon.DoubleClick) else None)
        self.tray.messageClicked.connect(lambda: (self.bring_to_front(), self.navigate("inbox")))
        self.tray.show()

    def notify(self, title: str, message: str) -> None:
        if self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, message, app_icon(), 6000)
        else:
            self.statusBar().showMessage(f"{title}: {message}", 8000)

    def bring_to_front(self) -> None:
        self.showNormal() if self.isMinimized() or not self.isVisible() else self.show()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event):  # noqa: N802
        if not self._quitting and self.db.get_bool("close_to_tray") and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.notify(APP_NAME, "Still watching for new invoices in the background. "
                                      "Right-click the tray icon to quit.")
            return
        self.tray.hide()
        self.importer.pool.clear()
        self.importer.pool.waitForDone(5000)
        event.accept()
        QApplication.quit()
