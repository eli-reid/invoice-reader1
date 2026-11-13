from __future__ import annotations

import argparse
import getpass
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from invoice_reader import APP_ID, APP_NAME, __version__, config
from invoice_reader.db import Database, now_iso, open_database

log = logging.getLogger("invoice_reader")
SERVER_NAME = f"{APP_ID}-{getpass.getuser()}"


def setup_logging() -> None:
    handler = RotatingFileHandler(config.log_dir() / "invoice_reader.log", maxBytes=2_000_000, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())


def install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        if QApplication.instance():
            box = QMessageBox(QMessageBox.Critical, APP_NAME, "Something went wrong. The error was logged.")
            box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
            box.exec()

    sys.excepthook = hook


def set_app_user_model_id() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001 - cosmetic (taskbar grouping/icon) only
            pass


def notify_running_instance(files: list[str]) -> bool:
    """If another instance is running, hand it our files and return True."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    sock.write(("\n".join(["SHOW", *files]) + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def first_run_defaults(db: Database) -> None:
    if db.get_setting("initialized", "0") == "1":
        return
    downloads = config.downloads_dir()
    if not db.get_setting("watch_folder") and downloads.is_dir():
        db.set_setting("watch_folder", str(downloads))
        db.set_setting("watch_since", now_iso())
    db.set_setting("initialized", "1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoice_reader")
    parser.add_argument("--minimized", action="store_true", help="start in the system tray")
    parser.add_argument("files", nargs="*", help="invoice files to import")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    set_app_user_model_id()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Midstate")
    app.setQuitOnLastWindowClosed(False)

    if notify_running_instance(args.files):
        return 0

    setup_logging()
    install_excepthook()
    log.info("Starting %s %s (data: %s)", APP_NAME, __version__, config.data_dir())

    from invoice_reader.ui.main_window import MainWindow
    from invoice_reader.ui.theme import app_icon, apply_theme

    apply_theme(app)
    app.setWindowIcon(app_icon())

    try:
        db = open_database()
    except Exception as exc:  # noqa: BLE001
        log.exception("Cannot open database")
        QMessageBox.critical(None, APP_NAME, f"Could not open the database:\n{config.db_path()}\n\n{exc}")
        return 1
    first_run_defaults(db)

    window = MainWindow(db)

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    def on_connection():
        sock = server.nextPendingConnection()

        def read():
            lines = [l for l in bytes(sock.readAll()).decode("utf-8", "replace").splitlines() if l]
            if lines and lines[0] == "SHOW":
                window.bring_to_front()
                if lines[1:]:
                    window.import_files(lines[1:])

        sock.readyRead.connect(read)

    server.newConnection.connect(on_connection)

    if not args.minimized:
        window.show()
    if args.files:
        QTimer.singleShot(300, lambda: window.import_files(args.files))

    try:
        return app.exec()
    finally:
        server.close()
        db.close()
        log.info("Exited")
