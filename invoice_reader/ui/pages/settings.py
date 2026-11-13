from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from invoice_reader import APP_NAME, __version__, config
from invoice_reader.extraction.ocr import available_engines
from invoice_reader.ui import update
from invoice_reader.ui.update import CommandDialog
from invoice_reader.ui.widgets import PageHeader, open_path


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(True)
    return label


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        db = win.db
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(16)
        lay.addWidget(PageHeader("Settings"))

        # Watch folder
        g = QGroupBox("Watch folder")
        f = QFormLayout(g)
        self.watch_enabled = QCheckBox("Automatically import new invoices saved to this folder")
        self.watch_enabled.setChecked(db.get_bool("watch_enabled"))
        self.watch_enabled.toggled.connect(lambda v: self._set_and_reconfigure("watch_enabled", v))
        f.addRow(self.watch_enabled)
        row = QHBoxLayout()
        self.folder = QLineEdit(db.get_setting("watch_folder"))
        self.folder.setReadOnly(True)
        browse = QPushButton("Browse...")
        browse.clicked.connect(win.choose_watch_folder)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        f.addRow("Folder", row)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(db.get_bool("watch_recursive"))
        self.recursive.toggled.connect(lambda v: self._set_and_reconfigure("watch_recursive", v))
        f.addRow("", self.recursive)
        self.interval = QSpinBox()
        self.interval.setRange(3, 3600)
        self.interval.setSuffix(" seconds")
        self.interval.setValue(int(db.get_setting("watch_interval") or 10))
        self.interval.editingFinished.connect(lambda: self._set_and_reconfigure("watch_interval",
                                                                              self.interval.value()))
        f.addRow("Check every", self.interval)
        self.since = _hint("")
        f.addRow("", self.since)
        scan_existing = QPushButton("Scan existing files in folder now")
        scan_existing.clicked.connect(self._scan_existing)
        f.addRow("", scan_existing)
        lay.addWidget(g)

        # Importing
        g = QGroupBox("Importing")
        f = QFormLayout(g)
        self.auto_import = QCheckBox("Save invoices automatically when every line checks out")
        self.auto_import.setChecked(db.get_bool("auto_import"))
        self.auto_import.toggled.connect(lambda v: db.set_setting("auto_import", v))
        f.addRow(self.auto_import)
        f.addRow(_hint("When off, every invoice waits in the Import queue for you to review. When on, invoices "
                       "where vendor, invoice #, date and every line's math (qty \u00d7 price = extended) are "
                       "verified are saved without review; everything else still waits for you."))
        self.company = QLineEdit(db.get_setting("company_names"))
        self.company.setPlaceholderText("e.g. Midstate Electric; Midstate Electrical Contractors")
        self.company.editingFinished.connect(lambda: db.set_setting("company_names", self.company.text()))
        f.addRow("Your company name(s)", self.company)
        f.addRow("", _hint("Separate names with ';'. Used so your Bill To / Ship To name is never taken as the "
                           "vendor."))
        self.match_mfr = QCheckBox("Match products by item # and manufacturer")
        self.match_mfr.setChecked(db.get_bool("match_manufacturer"))
        self.match_mfr.toggled.connect(lambda v: db.set_setting("match_manufacturer", v))
        f.addRow(self.match_mfr)
        f.addRow(_hint("When off, the same item # is always one product. When on, the same item # from different "
                       "manufacturers (e.g. CRS 11 vs. another brand's 11) gets its own price history. Applies to "
                       "invoices saved from now on; lines without a manufacturer still join the existing product."))
        lay.addWidget(g)

        # Application
        g = QGroupBox("Application")
        f = QFormLayout(g)
        self.autostart = QCheckBox("Start with Windows (runs in the system tray so the watch folder is always "
                                   "monitored)")
        self.autostart.setChecked(config.starts_with_windows())
        self.autostart.toggled.connect(self._toggle_autostart)
        f.addRow(self.autostart)
        self.tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray.setChecked(db.get_bool("close_to_tray"))
        self.tray.toggled.connect(lambda v: db.set_setting("close_to_tray", v))
        f.addRow(self.tray)
        lay.addWidget(g)

        # Data
        g = QGroupBox("Data")
        f = QFormLayout(g)
        row = QHBoxLayout()
        path = QLineEdit(str(config.data_dir()))
        path.setReadOnly(True)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: open_path(config.data_dir()))
        row.addWidget(path, 1)
        row.addWidget(open_btn)
        f.addRow("Data folder", row)
        f.addRow("", _hint("Contains the database (invoices.db), an archive copy of every imported invoice, "
                           "and logs."))
        buttons = QHBoxLayout()
        for text, slot in (("Back up database...", self._backup), ("Export price history (CSV)...",
                                                                    self._export_history)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        f.addRow("", buttons)
        lay.addWidget(g)

        # Database connection
        g = QGroupBox("Database")
        f = QFormLayout(g)
        f.addRow("In use now", QLabel(self.win.db.backend.describe()))
        cfg = config.load_db_config()
        self.db_engine = QComboBox()
        self.db_engine.addItem("Local file (SQLite)", "sqlite")
        self.db_engine.addItem("MySQL / MariaDB server", "mysql")
        self.db_engine.setCurrentIndex(max(0, self.db_engine.findData(cfg["engine"])))
        self.db_engine.currentIndexChanged.connect(self._db_engine_changed)
        f.addRow("Store data in", self.db_engine)
        self.db_host = QLineEdit(cfg["host"])
        self.db_host.setPlaceholderText("e.g. 192.168.1.10 or db.mycompany.com")
        f.addRow("Host", self.db_host)
        self.db_port = QSpinBox()
        self.db_port.setRange(1, 65535)
        self.db_port.setValue(int(cfg["port"] or 3306))
        f.addRow("Port", self.db_port)
        self.db_name = QLineEdit(cfg["database"])
        f.addRow("Database name", self.db_name)
        self.db_user = QLineEdit(cfg["user"])
        f.addRow("User", self.db_user)
        self.db_password = QLineEdit(cfg["password"])
        self.db_password.setEchoMode(QLineEdit.Password)
        f.addRow("Password", self.db_password)
        buttons = QHBoxLayout()
        for text, slot in (("Test connection", self._db_test), ("Save database settings", self._db_save),
                           ("Copy local data to server...", self._db_copy_to_server)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        f.addRow("", buttons)
        f.addRow("", _hint("A server database lets several PCs share the same invoices and products, and other "
                           "apps can read it (give them a read-only account). The database and user must already "
                           "exist on the server; tables are created automatically. Settings are stored in "
                           "database.json in the data folder and take effect the next time the app starts."))
        self._db_engine_changed()
        lay.addWidget(g)

        # Updates
        g = QGroupBox("Updates")
        f = QFormLayout(g)
        if update.is_source_checkout() and update.git_exe():
            self.src_label = QLabel(update.git_summary())
            self.src_label.setWordWrap(True)
            f.addRow("Source code", self.src_label)
            buttons = QHBoxLayout()
            for text, slot in (("Update from Git...", self._update_from_git),
                               ("Rebuild installer (single EXE)...", self._rebuild_installer)):
                b = QPushButton(text)
                b.clicked.connect(slot)
                buttons.addWidget(b)
            buttons.addStretch(1)
            f.addRow("", buttons)
            f.addRow("", _hint("Update pulls the latest code from the Git remote and refreshes the Python "
                               "packages; close and reopen the app afterward. Rebuilding the installer creates "
                               "dist\\InvoiceReaderSetup.exe for installing on other PCs (needs Inno Setup 6 and "
                               "internet access on this PC, and takes a few minutes)."))
        else:
            f.addRow(_hint("Updating is available when the app runs from its source folder with Git installed. "
                           "On installed copies, update by running the newest InvoiceReaderSetup.exe."))
        lay.addWidget(g)

        g = QGroupBox("About")
        f = QFormLayout(g)
        f.addRow("Version", QLabel(f"{APP_NAME} {__version__}"))
        engines = available_engines()
        self.ocr = QLabel(", ".join(engines) if engines else
                          "No OCR engine available - scanned invoices can't be read. "
                          "Install an English language pack with OCR in Windows Settings.")
        self.ocr.setWordWrap(True)
        f.addRow("OCR engine", self.ocr)
        lay.addWidget(g)
        lay.addStretch(1)

    def refresh(self) -> None:
        db = self.win.db
        self.folder.setText(db.get_setting("watch_folder"))
        since = db.get_setting("watch_since")
        if since:
            when = datetime.fromisoformat(since)
            self.since.setText(f"Files added to this folder after {when:%b %d, %Y %I:%M %p} are imported "
                               "automatically. Use the button below to also import files that were already there.")

    def _set_and_reconfigure(self, key: str, value) -> None:
        self.win.db.set_setting(key, value)
        self.win.watcher.configure()

    def _scan_existing(self) -> None:
        if not self.win.watcher.enabled:
            QMessageBox.information(self, "Watch folder off", "Turn on the watch folder first.")
            return
        if QMessageBox.question(self, "Scan existing files",
                                "Read every invoice file currently in the watch folder?\n\n"
                                "Files already imported are skipped automatically.") == QMessageBox.Yes:
            n = self.win.watcher.scan(include_existing=True)
            self.win.notify("Scan started", f"{n} file(s) queued for reading.")

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            config.set_start_with_windows(enabled)
        except OSError as exc:
            QMessageBox.warning(self, "Start with Windows", f"Could not change the startup setting:\n{exc}")

    def _backup(self) -> None:
        if not self.win.db.backend.supports_file_backup:
            QMessageBox.information(self, "Back up database",
                                    "You are using a server database. Back it up on the server "
                                    "(e.g. with mysqldump) instead.")
            return
        default = f"invoice-reader-backup-{datetime.now():%Y%m%d-%H%M}.db"
        path, _ = QFileDialog.getSaveFileName(self, "Back up database", default, "SQLite database (*.db)")
        if path:
            self.win.db.backup(path)
            QMessageBox.information(self, "Backup complete", f"Database backed up to\n{path}")

    # ---- database connection --------------------------------------------
    def _db_engine_changed(self) -> None:
        remote = self.db_engine.currentData() == "mysql"
        for w in (self.db_host, self.db_port, self.db_name, self.db_user, self.db_password):
            w.setEnabled(remote)

    def _db_config(self) -> dict:
        return {
            "engine": self.db_engine.currentData(),
            "host": self.db_host.text().strip(),
            "port": self.db_port.value(),
            "database": self.db_name.text().strip(),
            "user": self.db_user.text().strip(),
            "password": self.db_password.text(),
        }

    def _db_test(self) -> None:
        cfg = self._db_config()
        if cfg["engine"] != "mysql":
            QMessageBox.information(self, "Test connection", "The local SQLite file needs no connection.")
            return
        from invoice_reader.backends import test_mysql_connection
        try:
            version = test_mysql_connection(cfg)
        except Exception as exc:  # noqa: BLE001 - show any driver/network error to the user
            QMessageBox.warning(self, "Connection failed", str(exc))
            return
        QMessageBox.information(self, "Connection OK", f"Connected. Server version: {version}")

    def _db_save(self) -> None:
        cfg = self._db_config()
        if cfg["engine"] == "mysql" and not (cfg["host"] and cfg["database"] and cfg["user"]):
            QMessageBox.warning(self, "Database settings", "Enter the host, database name and user first.")
            return
        config.save_db_config(cfg)
        QMessageBox.information(self, "Database settings saved",
                                "The new database will be used the next time the app starts.")

    def _db_copy_to_server(self) -> None:
        cfg = self._db_config()
        if cfg["engine"] != "mysql":
            QMessageBox.information(self, "Copy data", "Select the MySQL / MariaDB server engine first.")
            return
        if self.win.db.backend.engine != "sqlite":
            QMessageBox.information(self, "Copy data", "You are already using the server database.")
            return
        if QMessageBox.question(
                self, "Copy local data to server",
                "Copy all local invoices, products, price history and settings to the server database?\n\n"
                "The server database must be empty. Your local file is not changed.") != QMessageBox.Yes:
            return
        from invoice_reader.backends import create_backend
        from invoice_reader.db import Database, copy_database
        try:
            remote = Database(create_backend(cfg))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Connection failed", str(exc))
            return
        try:
            counts = copy_database(self.win.db, remote)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Copy failed", str(exc))
            return
        finally:
            remote.close()
        summary = "\n".join(f"  {table}: {n:,}" for table, n in counts.items() if n)
        QMessageBox.information(self, "Copy complete",
                                f"Copied to the server:\n{summary}\n\nSave the database settings and restart "
                                "the app to start using it.")

    # ---- updates ----------------------------------------------------------
    def _update_from_git(self) -> None:
        root = update.project_root()
        if not update.remote_url():
            url, ok = QInputDialog.getText(
                self, "Git remote", "No Git remote is configured yet.\n"
                "Enter the repository URL to update from (e.g. https://github.com/you/invoice-reader.git):")
            if not ok or not url.strip():
                return
            code, out = update.set_remote(url.strip())
            if code != 0:
                QMessageBox.warning(self, "Git remote", f"Could not set the remote:\n{out}")
                return
        changed = update.run_git("status", "--porcelain")[1]
        if changed and QMessageBox.question(
                self, "Local changes",
                "There are uncommitted local changes in the source folder. Updating only fast-forwards and "
                "will fail if they conflict with the update.\n\nTry to update anyway?") != QMessageBox.Yes:
            return
        steps = [
            ("Pull latest code", update.git_exe(),
             ["-C", str(root), "pull", "--ff-only", "origin", update.current_branch()]),
            ("Update Python packages", sys.executable,
             ["-m", "pip", "install", "-r", str(root / "requirements.txt")]),
        ]
        dlg = CommandDialog("Update from Git", steps, root, self)
        dlg.exec()
        self.src_label.setText(update.git_summary())
        if dlg.succeeded:
            QMessageBox.information(self, "Update complete",
                                    "The source code and packages are up to date.\n"
                                    "Close and reopen the app to run the updated version.")

    def _rebuild_installer(self) -> None:
        root = update.project_root()
        script = root / "installer" / "build-installer.ps1"
        if not script.is_file():
            QMessageBox.warning(self, "Rebuild installer", f"Build script not found:\n{script}")
            return
        if QMessageBox.question(
                self, "Rebuild installer",
                "Build dist\\InvoiceReaderSetup.exe now?\n\nThis downloads the bundled Python runtime and "
                "packages, so it needs internet access and a few minutes.") != QMessageBox.Yes:
            return
        steps = [("Build the offline installer", "powershell",
                  ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])]
        dlg = CommandDialog("Rebuild installer", steps, root, self)
        dlg.exec()
        if dlg.succeeded:
            open_path(root / "dist")

    def _export_history(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export price history", "price-history.csv",
                                              "CSV files (*.csv)")
        if path:
            n = self.win.db.export_history_csv(path)
            QMessageBox.information(self, "Export complete", f"Exported {n:,} price record(s) to\n{path}")
