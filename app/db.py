"""SQLite 登记层：包登记、逐日修订与校验信息。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    stored_path TEXT NOT NULL DEFAULT '',
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'registered'
                CHECK (status IN ('registered', 'missing', 'corrupt')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS package_days (
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,               -- ISO YYYY-MM-DD
    revision   INTEGER NOT NULL,
    feed_kg    REAL NOT NULL,
    row_sha256 TEXT NOT NULL,               -- 行级校验: sha256("date|revision|feed_kg")
    PRIMARY KEY (package_id, date, revision)
);

CREATE INDEX IF NOT EXISTS idx_package_days_date ON package_days(date);
"""


class Database:
    """极简 SQLite 访问层。每次操作开新连接，with 块结束自动提交/回滚。"""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            con.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
