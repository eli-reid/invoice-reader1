"""Database backends: a thin driver layer so the app can run on the local
SQLite file (default) or a shared server database such as MySQL/MariaDB.

Application SQL is written once, in the SQLite dialect, and each backend
adapts what it needs. A backend only has to provide `execute()` returning a
DB-API cursor whose rows are addressable by column name, transaction control,
and schema/versioning - so adding another engine (e.g. PostgreSQL) means
writing one more subclass.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

try:
    import pymysql
    import pymysql.cursors
    _MYSQL_ERRORS: tuple[type[Exception], ...] = (pymysql.MySQLError,)
except ImportError:  # driver is optional; only needed when a MySQL server is configured
    pymysql = None
    _MYSQL_ERRORS = ()

# Every exception a backend driver can raise; UI code catches these instead of sqlite3.Error.
DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error,) + _MYSQL_ERRORS

# A row from any backend: sqlite3.Row or a dict, both addressable by column name.
Row = Any


class Cursor(Protocol):
    lastrowid: int | None

    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> list[Row]: ...


class Backend:
    """Interface every database driver implements."""

    engine = ""
    supports_file_backup = False

    def describe(self) -> str:
        """Human-readable connection target for error messages."""
        raise NotImplementedError

    def execute(self, sql: str, params: tuple | dict = ()) -> Cursor:
        raise NotImplementedError

    def begin(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def migrations(self) -> list[str]:
        """Ordered migration scripts for this engine; position = schema version."""
        raise NotImplementedError

    def schema_version(self) -> int:
        raise NotImplementedError

    def set_schema_version(self, version: int) -> None:
        raise NotImplementedError

    def pre_migrate(self) -> None:
        pass

    def post_migrate(self) -> None:
        pass

    def check_migration_integrity(self) -> None:
        pass

    def backup(self, dest: str | Path) -> None:
        raise NotImplementedError(
            "File backup is only available for the local SQLite database. "
            "Back up the server database with your server's tools (e.g. mysqldump).")


class SQLiteBackend(Backend):
    engine = "sqlite"
    supports_file_backup = True

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    def describe(self) -> str:
        return self.path

    def execute(self, sql: str, params: tuple | dict = ()) -> Cursor:
        return self.conn.execute(sql, params)

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def close(self) -> None:
        self.conn.close()

    def migrations(self) -> list[str]:
        from invoice_reader.db import MIGRATIONS
        return MIGRATIONS

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def set_schema_version(self, version: int) -> None:
        self.conn.execute(f"PRAGMA user_version = {version}")

    def pre_migrate(self) -> None:
        # Table rebuilds must not cascade-delete child rows; foreign keys can only be
        # toggled outside a transaction.
        self.conn.execute("PRAGMA foreign_keys = OFF")

    def post_migrate(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = ON")

    def check_migration_integrity(self) -> None:
        if self.conn.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Migration broke foreign keys")

    def backup(self, dest: str | Path) -> None:
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()


# ---- SQLite -> MySQL dialect translation ---------------------------------
# `key` (settings table) is a reserved word in MySQL; quote it before the ON
# CONFLICT rewrite introduces the "ON DUPLICATE KEY" keyword sequence.
_KEY_IDENT_RE = re.compile(r"(?<![\w`.])key(?![\w`])")
_INSERT_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
_ON_CONFLICT_RE = re.compile(r"\bON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\b", re.I)
_EXCLUDED_RE = re.compile(r"\bexcluded\.(\w+)", re.I)
_COLLATE_NOCASE_RE = re.compile(r"\s+COLLATE\s+NOCASE\b", re.I)


def translate_sql_to_mysql(sql: str) -> str:
    """Rewrite the app's SQLite-dialect SQL for MySQL/MariaDB.

    Only runtime statements go through here (never DDL, which each backend
    owns), so a bare lowercase `key` is always the settings column.
    """
    sql = _KEY_IDENT_RE.sub("`key`", sql)
    sql = _INSERT_IGNORE_RE.sub("INSERT IGNORE", sql)
    sql = _ON_CONFLICT_RE.sub("ON DUPLICATE KEY UPDATE", sql)
    sql = _EXCLUDED_RE.sub(r"VALUES(\1)", sql)
    sql = _COLLATE_NOCASE_RE.sub("", sql)
    return sql.replace("?", "%s")


# Current schema, created in one step on an empty server database. Dates are ISO-8601
# strings (as in SQLite); case-insensitive matching relies on the default *_ci collation.
MYSQL_MIGRATIONS = [
    """
    CREATE TABLE settings (
        `key` VARCHAR(100) NOT NULL PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE vendors (
        id   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(255) NOT NULL UNIQUE
    );
    CREATE TABLE products (
        id           INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        item_number  VARCHAR(255) NOT NULL,
        item_key     VARCHAR(255) NOT NULL,
        manufacturer VARCHAR(255) NOT NULL DEFAULT '',
        mfr_key      VARCHAR(255) NOT NULL DEFAULT '',
        name         VARCHAR(2000) NOT NULL DEFAULT '',
        category     VARCHAR(255) NOT NULL DEFAULT '',
        notes        VARCHAR(4000) NOT NULL DEFAULT '',
        created_at   VARCHAR(32) NOT NULL,
        updated_at   VARCHAR(32) NOT NULL,
        UNIQUE KEY uq_products_item_mfr (item_key, mfr_key),
        KEY ix_products_item_key (item_key)
    );
    CREATE TABLE invoices (
        id             INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        vendor_id      INT NULL,
        invoice_number VARCHAR(100) NOT NULL DEFAULT '',
        invoice_date   VARCHAR(32) NULL,
        total          DOUBLE NULL,
        file_path      TEXT NULL,
        original_name  VARCHAR(512) NULL,
        file_hash      VARCHAR(64) NULL UNIQUE,
        method         VARCHAR(20) NULL,
        imported_at    VARCHAR(32) NOT NULL,
        KEY ix_invoices_vendor_number (vendor_id, invoice_number),
        KEY ix_invoices_date (invoice_date),
        CONSTRAINT fk_invoices_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(id)
    );
    CREATE TABLE price_history (
        id          INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        product_id  INT NOT NULL,
        invoice_id  INT NOT NULL,
        vendor_id   INT NULL,
        price_date  VARCHAR(32) NOT NULL,
        description VARCHAR(4000) NOT NULL DEFAULT '',
        quantity    DOUBLE NOT NULL DEFAULT 0,
        uom         VARCHAR(20) NOT NULL DEFAULT 'EA',
        unit_price  DOUBLE NOT NULL,
        price_per   INT NOT NULL DEFAULT 1,
        unit_cost   DOUBLE NOT NULL,
        extended    DOUBLE NULL,
        KEY ix_price_product_date (product_id, price_date),
        KEY ix_price_invoice (invoice_id),
        CONSTRAINT fk_price_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
        CONSTRAINT fk_price_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        CONSTRAINT fk_price_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(id)
    );
    CREATE TABLE inbox (
        id          INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        file_path   TEXT NOT NULL,
        file_hash   VARCHAR(64) NOT NULL UNIQUE,
        file_size   BIGINT NULL,
        source      VARCHAR(20) NOT NULL DEFAULT 'manual',
        status      VARCHAR(20) NOT NULL,
        parsed_json MEDIUMTEXT NULL,
        error       TEXT NULL,
        invoice_id  INT NULL,
        detected_at VARCHAR(32) NOT NULL,
        updated_at  VARCHAR(32) NOT NULL,
        KEY ix_inbox_status (status),
        CONSTRAINT fk_inbox_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE SET NULL
    );
    CREATE TABLE vendor_aliases (
        alias  VARCHAR(255) NOT NULL PRIMARY KEY,
        vendor VARCHAR(255) NOT NULL
    )
    """,
    # v2: global invoice identity + deletion tombstones for two-way sync with local databases.
    """
    ALTER TABLE invoices ADD COLUMN sync_uid VARCHAR(64) NULL;
    ALTER TABLE invoices ADD UNIQUE KEY ix_invoices_sync_uid (sync_uid);
    CREATE TABLE sync_deletions (
        uid        VARCHAR(64) NOT NULL PRIMARY KEY,
        deleted_at VARCHAR(32) NOT NULL
    )
    """,
]


class MySQLBackend(Backend):
    engine = "mysql"

    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        if pymysql is None:
            raise RuntimeError("The MySQL driver is not installed. Run: pip install PyMySQL")
        self.host, self.port, self.database = host, int(port or 3306), database
        self._credentials = {"user": user, "password": password}
        self.conn = None
        self._in_transaction = False
        self._connect()

    def _connect(self) -> None:
        self.conn = pymysql.connect(
            host=self.host, port=self.port, database=self.database,
            user=self._credentials["user"], password=self._credentials["password"],
            charset="utf8mb4", autocommit=True, connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor)
        with self.conn.cursor() as cur:
            # The app's aggregate queries select columns functionally dependent on the
            # grouped primary key; don't rely on the server detecting that.
            cur.execute("SET SESSION sql_mode = (SELECT REPLACE(@@sql_mode, 'ONLY_FULL_GROUP_BY', ''))")
            cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INT NOT NULL)")

    def describe(self) -> str:
        return f"MySQL database '{self.database}' on {self.host}:{self.port}"

    def execute(self, sql: str, params: tuple | dict = ()) -> Cursor:
        sql = translate_sql_to_mysql(sql)
        try:
            return self._run(sql, params)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            if self._in_transaction:
                raise  # cannot transparently resume a broken transaction
            log.warning("MySQL connection lost; reconnecting")
            self.conn.ping(reconnect=True)
            return self._run(sql, params)

    def _run(self, sql: str, params: tuple | dict) -> Cursor:
        cur = self.conn.cursor()
        cur.execute(sql, params or None)
        return cur

    def begin(self) -> None:
        self.conn.ping(reconnect=True)
        self.conn.begin()
        self._in_transaction = True

    def commit(self) -> None:
        self.conn.commit()
        self._in_transaction = False

    def rollback(self) -> None:
        self.conn.rollback()
        self._in_transaction = False

    def close(self) -> None:
        self.conn.close()

    def migrations(self) -> list[str]:
        return MYSQL_MIGRATIONS

    def schema_version(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
            return int(cur.fetchone()["v"])

    def set_schema_version(self, version: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM schema_version")
            cur.execute("INSERT INTO schema_version(version) VALUES(%s)", (version,))


def create_backend(cfg: dict) -> Backend:
    """Build the backend described by a connection config (see config.load_db_config)."""
    if cfg.get("engine") == "mysql":
        return MySQLBackend(host=cfg.get("host", "localhost"), port=cfg.get("port", 3306),
                            database=cfg.get("database", "invoice_reader"),
                            user=cfg.get("user", ""), password=cfg.get("password", ""))
    from invoice_reader import config
    return SQLiteBackend(config.db_path())


def test_mysql_connection(cfg: dict) -> str:
    """Try to connect with the given settings; returns the server version."""
    if pymysql is None:
        raise RuntimeError("The MySQL driver is not installed. Run: pip install PyMySQL")
    conn = pymysql.connect(
        host=cfg.get("host", "localhost"), port=int(cfg.get("port") or 3306),
        database=cfg.get("database", ""), user=cfg.get("user", ""),
        password=cfg.get("password", ""), connect_timeout=8)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            return str(cur.fetchone()[0])
    finally:
        conn.close()
