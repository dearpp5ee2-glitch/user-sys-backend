"""SQLite storage for the team task board."""

import os
import sqlite3
import threading
from datetime import datetime

PRIORITIES = ("low", "medium", "high")
STATUSES = ("todo", "doing", "done")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    assignee   TEXT NOT NULL,
    priority   TEXT NOT NULL DEFAULT 'medium',
    status     TEXT NOT NULL DEFAULT 'todo',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def now_iso():
    """Current UTC time as 2026-01-31T09:30:00.123Z (millisecond precision)."""
    now = datetime.utcnow()
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


class TaskStore(object):
    def __init__(self, db_path):
        self.db_path = db_path
        self._write_lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def list(self, status=None, assignee=None):
        sql = "SELECT * FROM tasks"
        clauses = []
        params = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if assignee:
            clauses.append("assignee = ?")
            params.append(assignee)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        conn = self._connect()
        try:
            return [self._row(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def get(self, task_id):
        conn = self._connect()
        try:
            return self._row(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
        finally:
            conn.close()

    def create(self, title, assignee, priority="medium", status="todo"):
        stamp = now_iso()
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO tasks (title, assignee, priority, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (title, assignee, priority, status, stamp, stamp),
                )
                conn.commit()
                return self._row(
                    conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)
                    ).fetchone()
                )
            finally:
                conn.close()

    def update(self, task_id, fields):
        """Patch the given columns. Returns the row, or None when it does not exist."""
        allowed = ("title", "assignee", "priority", "status")
        changes = [(k, fields[k]) for k in allowed if k in fields]
        with self._write_lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if current is None:
                    return None
                if not changes:
                    return self._row(current)
                assignments = ", ".join("%s = ?" % k for k, _ in changes)
                values = [v for _, v in changes]
                values.extend([now_iso(), task_id])
                conn.execute(
                    "UPDATE tasks SET %s, updated_at = ? WHERE id = ?" % assignments,
                    values,
                )
                conn.commit()
                return self._row(
                    conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                )
            finally:
                conn.close()

    def delete(self, task_id):
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
