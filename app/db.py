"""SQLite 数据层：连接管理、建表、默认管理员初始化"""
import sqlite3
import threading
from pathlib import Path

from .config import config
from .security import hash_password

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    keyword TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    platforms TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id INTEGER NOT NULL,
    title TEXT,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    snippet TEXT,
    source_platform TEXT DEFAULT '网页',
    engine TEXT,
    sentiment TEXT DEFAULT '中性',
    score REAL DEFAULT 0,
    reason TEXT,
    level TEXT DEFAULT '一般',
    level_note TEXT,
    publish_time TEXT,
    found_at TEXT DEFAULT (datetime('now','localtime')),
    pushed INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_dedup ON results(keyword_id, url_hash);
CREATE INDEX IF NOT EXISTS idx_results_sentiment ON results(sentiment);
CREATE INDEX IF NOT EXISTS idx_results_found ON results(found_at);

CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id INTEGER,
    result_ids TEXT,
    channel TEXT DEFAULT 'wecom',
    status TEXT,
    message TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS push_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    min_level TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def query(sql: str, params=()):
    cur = get_db().execute(sql, params)
    return cur.fetchall()


def query_one(sql: str, params=()):
    cur = get_db().execute(sql, params)
    return cur.fetchone()


def execute(sql: str, params=()) -> int:
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def _migrate(db: sqlite3.Connection) -> None:
    """增量迁移：为旧库补充新字段。"""
    cols = {r[1] for r in db.execute("PRAGMA table_info(results)").fetchall()}
    if "level" not in cols:
        db.execute("ALTER TABLE results ADD COLUMN level TEXT DEFAULT '一般'")
    if "level_note" not in cols:
        db.execute("ALTER TABLE results ADD COLUMN level_note TEXT")
    db.commit()


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.executescript(SCHEMA)
    _migrate(db)

    # 首次启动创建默认管理员
    exists = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not exists:
        db.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
            (config.DEFAULT_ADMIN, hash_password(config.DEFAULT_PASSWORD), "admin"),
        )
        db.commit()
    db.close()
