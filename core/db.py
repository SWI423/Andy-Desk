"""
数据访问层 db.py
职责：连接管理 + 建表 + 通用查询封装
所有写操作集中在同一把锁上，避免桌面多线程写冲突
"""

import os
import sqlite3
import threading
from datetime import datetime

from config import DB_PATH, ensure_dirs

_LOCK = threading.RLock()
_CONN = None


def get_conn() -> sqlite3.Connection:
    """获取全局连接（WAL 模式 + 行工厂）"""
    global _CONN
    if _CONN is None:
        ensure_dirs()
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA foreign_keys=ON")
    return _CONN


def execute(sql: str, params: tuple = ()) -> int:
    """执行写语句，返回 lastrowid（无则返回受影响行数）"""
    with _LOCK:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or cur.rowcount


def executemany(sql: str, seq) -> int:
    with _LOCK:
        conn = get_conn()
        cur = conn.executemany(sql, seq)
        conn.commit()
        return cur.rowcount


def query(sql: str, params: tuple = ()) -> list:
    """查询，返回 list[dict]"""
    with _LOCK:
        rows = get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()):
    rows = query(sql, params)
    return rows[0] if rows else None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


SCHEMA = """
-- ================= 记忆层 L2：事件流水（append-only）=================
CREATE TABLE IF NOT EXISTS chat_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    importance  REAL    NOT NULL DEFAULT 0.0,
    summarized  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_session_ts ON chat_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_summarized ON chat_events(summarized);

-- ================= 记忆层 L3：结构化事实 =================
CREATE TABLE IF NOT EXISTS semantic_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT NOT NULL DEFAULT 'user',
    predicate    TEXT NOT NULL,
    object       TEXT NOT NULL,
    domain       TEXT,
    confidence   REAL NOT NULL DEFAULT 0.8,
    source_event_id INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_pred ON semantic_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_domain ON semantic_facts(domain);

-- ================= 摘要层（V1.0 启用）=================
CREATE TABLE IF NOT EXISTS session_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    summary     TEXT NOT NULL,
    covered_until_event_id INTEGER,
    ts          TEXT NOT NULL
);

-- ================= 任务层 =================
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    domain      TEXT NOT NULL DEFAULT 'life',
    status      TEXT NOT NULL DEFAULT 'todo',
    priority    INTEGER NOT NULL DEFAULT 2,
    due_at      TEXT,
    progress    INTEGER NOT NULL DEFAULT 0,
    parent_id   INTEGER,
    source      TEXT NOT NULL DEFAULT 'manual',
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tasks_domain_status ON tasks(domain, status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT,
    ts         TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, ts);

-- ================= 提醒层 =================
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    rule_type     TEXT NOT NULL DEFAULT 'daily',   -- once / daily / weekly
    rule_expr     TEXT NOT NULL,                   -- once: "2026-11-26 09:00"; daily: "07:30"; weekly: "1 09:00"
    next_fire_at  TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    missed_count  INTEGER NOT NULL DEFAULT 0,
    task_id       INTEGER,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_next ON reminders(next_fire_at, enabled);

-- ================= 用户画像（轻量 KV）=================
CREATE TABLE IF NOT EXISTS user_profile (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def init_db() -> bool:
    """建表（幂等）"""
    with _LOCK:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.commit()
    return True


def health_check() -> bool:
    try:
        query("SELECT 1 AS ok")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
    for t in query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(" -", t["name"])
