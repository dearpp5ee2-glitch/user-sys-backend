"""SQLite storage for the team task board: tasks, users and sessions."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta

PRIORITIES = ("low", "medium", "high")
STATUSES = ("todo", "doing", "done")
ROLES = ("member", "admin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    note       TEXT,
    assignee   TEXT NOT NULL,
    priority   TEXT NOT NULL DEFAULT 'medium',
    status     TEXT NOT NULL DEFAULT 'todo',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(lower(username));

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""

# Tasks gained an owner when logins arrived. Rows created before that keep a
# NULL created_by and stay readable by the whole team.
TASK_OWNER_COLUMN = "created_by"
TASK_NOTE_COLUMN = "note"


def now_iso():
    """Current UTC time as 2026-01-31T09:30:00.123Z (millisecond precision)."""
    now = datetime.utcnow()
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def iso_in(seconds):
    """UTC timestamp `seconds` from now, same format as now_iso()."""
    now = datetime.utcnow() + timedelta(seconds=seconds)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


class Store(object):
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
                self._migrate_tasks(conn)
                # Expired sessions are useless; drop them on every start.
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))
                conn.commit()
            finally:
                conn.close()

    def _migrate_tasks(self, conn):
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)")]
        if TASK_OWNER_COLUMN not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        if TASK_NOTE_COLUMN not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN note TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_created_by ON tasks(created_by)"
        )

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ users
    def count_users(self):
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _insert_user(conn, username, password_hash, display_name, role, stamp):
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, display_name, role, stamp),
        )
        return dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        )

    def create_user(self, username, password_hash, display_name, role="member"):
        """Insert a user with an explicit role. None when the username is taken."""
        stamp = now_iso()
        with self._write_lock:
            conn = self._connect()
            try:
                user = self._insert_user(
                    conn, username, password_hash, display_name, role, stamp
                )
                conn.commit()
                return user
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

    def register_user(self, username, password_hash, display_name):
        """Create a self-registered account.

        The first account on a board becomes the admin. Counting and inserting
        under the same write lock keeps two simultaneous first registrations
        from both becoming admin. Returns None when the username is taken.
        """
        stamp = now_iso()
        with self._write_lock:
            conn = self._connect()
            try:
                first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                user = self._insert_user(
                    conn,
                    username,
                    password_hash,
                    display_name,
                    "admin" if first else "member",
                    stamp,
                )
                conn.commit()
                return user
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

    def get_user(self, user_id):
        conn = self._connect()
        try:
            return self._row(
                conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            )
        finally:
            conn.close()

    def get_user_by_username(self, username):
        """Case-insensitive lookup, including the password hash."""
        conn = self._connect()
        try:
            return self._row(
                conn.execute(
                    "SELECT * FROM users WHERE lower(username) = lower(?)", (username,)
                ).fetchone()
            )
        finally:
            conn.close()

    def list_users(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    # --------------------------------------------------------------- sessions
    def create_session(self, token_hash, user_id, ttl_seconds):
        stamp = now_iso()
        expires_at = iso_in(ttl_seconds)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (token_hash, user_id, stamp, expires_at),
                )
                conn.commit()
            finally:
                conn.close()
        return expires_at

    def user_for_token(self, token_hash):
        """The user behind a live session, or None when unknown/expired."""
        conn = self._connect()
        try:
            return self._row(
                conn.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
                    " WHERE s.token_hash = ? AND s.expires_at > ?",
                    (token_hash, now_iso()),
                ).fetchone()
            )
        finally:
            conn.close()

    def delete_session(self, token_hash):
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def purge_expired_sessions(self):
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    # ------------------------------------------------------------------ tasks
    @staticmethod
    def _task_select():
        return (
            "SELECT t.*, u.username AS creator FROM tasks t"
            " LEFT JOIN users u ON u.id = t.created_by"
        )

    def list(self, status=None, assignee=None, mine=None, q=None):
        sql = self._task_select()
        clauses = []
        params = []
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        if assignee:
            clauses.append("t.assignee = ?")
            params.append(assignee)
        if mine is not None:
            clauses.append("t.created_by = ?")
            params.append(mine)
        if q:
            clauses.append(
                "(lower(t.title) LIKE ? OR lower(COALESCE(t.note, '')) LIKE ?"
                " OR lower(t.assignee) LIKE ? OR lower(COALESCE(u.username, '')) LIKE ?)"
            )
            match = "%%%s%%" % q.lower()
            params.extend([match, match, match, match])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.created_at DESC, t.id DESC"
        conn = self._connect()
        try:
            return [self._row(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def get(self, task_id):
        conn = self._connect()
        try:
            return self._row(
                conn.execute(
                    self._task_select() + " WHERE t.id = ?", (task_id,)
                ).fetchone()
            )
        finally:
            conn.close()

    def create(self, title, assignee, priority="medium", status="todo", created_by=None, note=None):
        stamp = now_iso()
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO tasks"
                    " (title, note, assignee, priority, status, created_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, note, assignee, priority, status, created_by, stamp, stamp),
                )
                conn.commit()
                return self._row(
                    conn.execute(
                        self._task_select() + " WHERE t.id = ?", (cur.lastrowid,)
                    ).fetchone()
                )
            finally:
                conn.close()

    def update(self, task_id, fields):
        """Patch the given columns. Returns the row, or None when it does not exist."""
        allowed = ("title", "note", "assignee", "priority", "status")
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
                    return self._row(
                        conn.execute(
                            self._task_select() + " WHERE t.id = ?", (task_id,)
                        ).fetchone()
                    )
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
                        self._task_select() + " WHERE t.id = ?", (task_id,)
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


# Older code and tests import TaskStore; the store now covers users too.
TaskStore = Store
