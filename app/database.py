"""SQLite persistence for composition tasks.

任务状态机：pending -> running -> done（结果写入 result 列）。
后台线程与请求线程各自建立连接，配合进程内互斥锁串行化写操作。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

DB_PATH = os.environ.get("EXAM_DB_PATH", "/data/exam.db")

_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock, get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                request     TEXT NOT NULL,
                result      TEXT,
                seed        INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def create_task(name: Optional[str], request_json: str, seed: int) -> int:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, request, seed) VALUES (?, 'pending', ?, ?)",
            (name, request_json, seed),
        )
        return int(cur.lastrowid)


def update_status(task_id: int, status: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def finish_task(task_id: int, result_json: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (result_json, task_id),
        )


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
