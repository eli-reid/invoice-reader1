from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from invoice_reader import APP_ID


def data_dir() -> Path:
    override = os.environ.get("INVOICE_READER_DATA")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InvoiceReader"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "invoices.db"


DEFAULT_DB_CONFIG = {
    "engine": "sqlite",  # "sqlite" (local file) or "mysql" (remote server)
    "host": "",
    "port": 3306,
    "database": "invoice_reader",
    "user": "",
    "password": "",
}


def db_config_path() -> Path:
    return data_dir() / "database.json"


def load_db_config() -> dict:
    """Connection settings for the database. Lives in a local file (not the database
    itself) because it is what tells the app where the database is."""
    cfg = dict(DEFAULT_DB_CONFIG)
    path = db_config_path()
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_db_config(cfg: dict) -> None:
    merged = dict(DEFAULT_DB_CONFIG)
    merged.update({k: cfg[k] for k in DEFAULT_DB_CONFIG if k in cfg})
    db_config_path().write_text(json.dumps(merged, indent=2), encoding="utf-8")


def archive_dir() -> Path:
    p = data_dir() / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def downloads_dir() -> Path:
    """Resolve the real Downloads folder (it can be redirected, e.g. to OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
            guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:  # noqa: BLE001 - fall back to the conventional location
            pass
    return Path.home() / "Downloads"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    script = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{exe}" "{script}" --minimized'


def set_start_with_windows(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


def starts_with_windows() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_ID)
            return True
    except FileNotFoundError:
        return False
