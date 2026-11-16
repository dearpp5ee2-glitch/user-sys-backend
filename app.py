#!/usr/bin/env python3
"""Team task board API.

Standard library only: http.server + sqlite3. Runs on Python 3.6+.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

import auth
from store import PRIORITIES, STATUSES, Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OPENAPI_PATH = os.path.join(BASE_DIR, "openapi.json")
# Static frontend build (index.html, styles.css, app.js). Served at "/".
FRONTEND_DIR = os.path.abspath(
    os.environ.get("FRONTEND_DIR", os.path.join(BASE_DIR, "frontend"))
)

MAX_BODY_BYTES = 64 * 1024
TITLE_MAX = 200
ASSIGNEE_MAX = 50
NOTE_MAX = 200

USERNAME_MIN = 3
USERNAME_MAX = 32
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PASSWORD_MIN = 8
PASSWORD_MAX = 128
DISPLAY_NAME_MAX = 50
SESSION_TTL_SECONDS = 7 * 24 * 3600

CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class ValidationError(Exception):
    def __init__(self, fields, message="Request validation failed"):
        Exception.__init__(self, message)
        self.fields = fields
        self.message = message


class Unauthorized(Exception):
    def __init__(self, message="Authentication required"):
        Exception.__init__(self, message)
        self.message = message


class Forbidden(Exception):
    def __init__(self, message="You are not allowed to do that"):
        Exception.__init__(self, message)
        self.message = message


def _clean_text(value, field, max_len, required=True):
    if value is None:
        if required:
            raise ValidationError({field: "is required"}, "Field is required: %s" % field)
        return None
    if not isinstance(value, str):
        raise ValidationError({field: "must be a string"}, "Field must be a string: %s" % field)
    value = value.strip()
    if required and not value:
        raise ValidationError({field: "must not be empty"}, "Field must not be empty: %s" % field)
    if len(value) > max_len:
        raise ValidationError(
            {field: "must be at most %d characters" % max_len},
            "Field is too long: %s" % field,
        )
    return value


def validate_create(payload):
    if not isinstance(payload, dict):
        raise ValidationError({}, "JSON body must be an object")
    unknown = sorted(set(payload) - {"title", "note", "assignee", "priority", "status"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    fields = {}
    fields["title"] = _clean_text(payload.get("title"), "title", TITLE_MAX)
    note = _clean_text(payload.get("note"), "note", NOTE_MAX, required=False)
    fields["note"] = note or None
    fields["assignee"] = _clean_text(payload.get("assignee"), "assignee", ASSIGNEE_MAX)
    priority = payload.get("priority", "medium")
    if priority not in PRIORITIES:
        raise ValidationError(
            {"priority": "must be one of: " + ", ".join(PRIORITIES)}, "Invalid priority"
        )
    status = payload.get("status", "todo")
    if status not in STATUSES:
        raise ValidationError(
            {"status": "must be one of: " + ", ".join(STATUSES)}, "Invalid status"
        )
    fields["priority"] = priority
    fields["status"] = status
    return fields


def validate_update(payload):
    if not isinstance(payload, dict):
        raise ValidationError({}, "JSON body must be an object")
    unknown = sorted(set(payload) - {"title", "note", "assignee", "priority", "status"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    if not payload:
        raise ValidationError({}, "Provide at least one field to update")
    fields = {}
    if "title" in payload:
        fields["title"] = _clean_text(payload["title"], "title", TITLE_MAX)
    if "note" in payload:
        note = _clean_text(payload["note"], "note", NOTE_MAX, required=False)
        fields["note"] = note or None
    if "assignee" in payload:
        fields["assignee"] = _clean_text(payload["assignee"], "assignee", ASSIGNEE_MAX)
    if "priority" in payload:
        if payload["priority"] not in PRIORITIES:
            raise ValidationError(
                {"priority": "must be one of: " + ", ".join(PRIORITIES)}, "Invalid priority"
            )
        fields["priority"] = payload["priority"]
    if "status" in payload:
        if payload["status"] not in STATUSES:
            raise ValidationError(
                {"status": "must be one of: " + ", ".join(STATUSES)}, "Invalid status"
            )
        fields["status"] = payload["status"]
    return fields


def validate_register(payload):
    if not isinstance(payload, dict):
        raise ValidationError({}, "JSON body must be an object")
    unknown = sorted(set(payload) - {"username", "password", "display_name"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    username = _clean_text(payload.get("username"), "username", USERNAME_MAX)
    if len(username) < USERNAME_MIN:
        raise ValidationError(
            {"username": "must be at least %d characters" % USERNAME_MIN}, "Username is too short"
        )
    if not USERNAME_RE.match(username):
        raise ValidationError(
            {"username": "may only contain letters, digits, dot, dash and underscore"},
            "Username has invalid characters",
        )
    password = payload.get("password")
    if not isinstance(password, str) or not password:
        raise ValidationError({"password": "is required"}, "Field is required: password")
    if len(password) < PASSWORD_MIN:
        raise ValidationError(
            {"password": "must be at least %d characters" % PASSWORD_MIN}, "Password is too short"
        )
    if len(password) > PASSWORD_MAX:
        raise ValidationError(
            {"password": "must be at most %d characters" % PASSWORD_MAX}, "Password is too long"
        )
    display_name = _clean_text(payload.get("display_name"), "display_name", DISPLAY_NAME_MAX, required=False)
    return {
        "username": username,
        "password": password,
        "display_name": display_name or username,
    }


def validate_login(payload):
    if not isinstance(payload, dict):
        raise ValidationError({}, "JSON body must be an object")
    unknown = sorted(set(payload) - {"username", "password"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    username = _clean_text(payload.get("username"), "username", USERNAME_MAX)
    password = payload.get("password")
    if not isinstance(password, str) or not password:
        raise ValidationError({"password": "is required"}, "Field is required: password")
    return {"username": username, "password": password}


def public_user(user):
    """The user fields safe to hand to a client (never the password hash)."""
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "created_at": user["created_at"],
    }


class TaskHandler(BaseHTTPRequestHandler):
    store = None
    server_version = "TeamTaskBoard/1.1"

    # ------------------------------------------------------------------ utils
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(self, status, code, message, fields=None, headers=None):
        payload = {"error": {"code": code, "message": message}}
        if fields:
            payload["error"]["fields"] = fields
        self._send_json(status, payload, headers)

    def _read_json(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise ValidationError({}, "Content-Length is required")
        try:
            length = int(raw_len)
        except ValueError:
            raise ValidationError({}, "Content-Length must be an integer")
        if length <= 0:
            raise ValidationError({}, "JSON body is required")
        if length > MAX_BODY_BYTES:
            raise PayloadTooLarge()
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError({}, "Body must be valid UTF-8 JSON")

    # ------------------------------------------------------------------ verbs
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_HEAD(self):
        self._dispatch("GET")

    # ----------------------------------------------------------------- router
    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if len(path) > 1:
            path = path.rstrip("/")
        try:
            if path == "/":
                return self._serve_frontend(path, method)
            if path == "/api/health" and method == "GET":
                return self._send_json(200, {"status": "ok"})
            if path == "/api/auth/register":
                if method == "POST":
                    return self._register()
                return self._method_not_allowed("POST")
            if path == "/api/auth/login":
                if method == "POST":
                    return self._login()
                return self._method_not_allowed("POST")
            if path == "/api/auth/logout":
                if method == "POST":
                    return self._logout()
                return self._method_not_allowed("POST")
            if path == "/api/auth/me":
                if method == "GET":
                    return self._send_json(200, public_user(self._current_user()))
                return self._method_not_allowed("GET")
            if path == "/api/users":
                if method == "GET":
                    self._current_user()
                    users = [public_user(u) for u in self.store.list_users()]
                    return self._send_json(200, {"items": users, "total": len(users)})
                return self._method_not_allowed("GET")
            if path == "/api/tasks":
                if method == "GET":
                    return self._list_tasks(parsed.query, self._current_user())
                if method == "POST":
                    return self._create_task(self._current_user())
                return self._method_not_allowed("GET, POST")
            if path.startswith("/api/tasks/"):
                task_id = self._parse_id(path)
                user = self._current_user()
                if method == "GET":
                    return self._get_task(task_id)
                if method == "PATCH":
                    return self._update_task(task_id)
                if method == "DELETE":
                    return self._delete_task(task_id, user)
                return self._method_not_allowed("GET, PATCH, DELETE")
            if path == "/openapi.json" and method == "GET":
                return self._send_file(OPENAPI_PATH, "application/json; charset=utf-8")
            if path in ("/docs", "/swagger") and method == "GET":
                return self._send_file(
                    os.path.join(BASE_DIR, "docs.html"), "text/html; charset=utf-8"
                )
            if path.startswith("/static/"):
                return self._serve_static(path, method)
            return self._serve_frontend(path, method)
        except ValidationError as exc:
            self._send_error(422, "validation_error", exc.message, exc.fields)
        except Unauthorized as exc:
            self._send_error(
                401,
                "unauthorized",
                exc.message,
                headers={"WWW-Authenticate": 'Bearer realm="team-tasks"'},
            )
        except Forbidden as exc:
            self._send_error(403, "forbidden", exc.message)
        except PayloadTooLarge:
            self._send_error(413, "payload_too_large", "JSON body must be under 64 KB")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write("Unhandled error on %s %s: %r\n" % (method, path, exc))
            self._send_error(500, "internal_error", "Unexpected server error")

    def _method_not_allowed(self, allow):
        self._send_json(
            405,
            {"error": {"code": "method_not_allowed", "message": "Allowed: " + allow}},
            {"Allow": allow},
        )

    def _parse_id(self, path):
        raw = path[len("/api/tasks/"):]
        if not raw or "/" in raw:
            raise ValidationError({}, "Task id must be a positive integer")
        try:
            task_id = int(raw)
        except ValueError:
            raise ValidationError({}, "Task id must be a positive integer")
        if task_id <= 0:
            raise ValidationError({}, "Task id must be a positive integer")
        return task_id

    # ------------------------------------------------------------------ auth
    def _bearer_token(self):
        header = self.headers.get("Authorization", "")
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            raise Unauthorized("Send Authorization: Bearer <token>")
        return parts[1]

    def _current_user(self):
        user = self.store.user_for_token(auth.token_hash(self._bearer_token()))
        if user is None:
            raise Unauthorized("Token is invalid or expired")
        return user

    def _issue_session(self, user, status=200):
        token = auth.new_token()
        expires_at = self.store.create_session(
            auth.token_hash(token), user["id"], SESSION_TTL_SECONDS
        )
        self._send_json(
            status,
            {
                "token": token,
                "token_type": "Bearer",
                "expires_at": expires_at,
                "user": public_user(user),
            },
        )

    def _register(self):
        fields = validate_register(self._read_json())
        user = self.store.register_user(
            fields["username"],
            auth.hash_password(fields["password"]),
            fields["display_name"],
        )
        if user is None:
            return self._send_error(
                409,
                "username_taken",
                "That username is already registered",
                {"username": "is already taken"},
            )
        # Registering signs you in, so the frontend does not need a second call.
        self._issue_session(user, status=201)

    def _login(self):
        fields = validate_login(self._read_json())
        user = self.store.get_user_by_username(fields["username"])
        if user is None:
            auth.verify_password(fields["password"], auth.DUMMY_HASH)
            raise Unauthorized("Wrong username or password")
        if not auth.verify_password(fields["password"], user["password_hash"]):
            raise Unauthorized("Wrong username or password")
        self._issue_session(user)

    def _logout(self):
        token = self._bearer_token()
        self._current_user()
        self.store.delete_session(auth.token_hash(token))
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------- api
    def _list_tasks(self, query, user):
        params = parse_qs(query)
        status = params.get("status", [None])[0]
        assignee = params.get("assignee", [None])[0]
        mine = params.get("mine", [None])[0]
        q = params.get("q", [None])[0]
        if status and status not in STATUSES:
            raise ValidationError(
                {"status": "must be one of: " + ", ".join(STATUSES)}, "Invalid status filter"
            )
        if mine is not None and mine.lower() not in ("1", "true", "yes"):
            raise ValidationError(
                {"mine": "must be one of: 1, true, yes"}, "Invalid mine filter"
            )
        items = self.store.list(
            status=status,
            assignee=assignee,
            mine=user["id"] if mine else None,
            q=q.strip() if q else None,
        )
        self._send_json(200, {"items": items, "total": len(items)})

    def _create_task(self, user):
        fields = validate_create(self._read_json())
        task = self.store.create(created_by=user["id"], **fields)
        self._send_json(201, task, {"Location": "/api/tasks/%d" % task["id"]})

    def _get_task(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            return self._send_error(404, "not_found", "Task %d does not exist" % task_id)
        self._send_json(200, task)

    def _update_task(self, task_id):
        fields = validate_update(self._read_json())
        task = self.store.update(task_id, fields)
        if task is None:
            return self._send_error(404, "not_found", "Task %d does not exist" % task_id)
        self._send_json(200, task)

    def _delete_task(self, task_id, user):
        task = self.store.get(task_id)
        if task is None:
            return self._send_error(404, "not_found", "Task %d does not exist" % task_id)
        # The board is shared, but a task can only be removed by its creator or
        # an admin. Tasks from before logins existed have no creator.
        if user["role"] != "admin" and task["created_by"] != user["id"]:
            raise Forbidden("Only the creator or an admin can delete this task")
        if not self.store.delete(task_id):
            return self._send_error(404, "not_found", "Task %d does not exist" % task_id)
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------------- static
    def _serve_static(self, path, method):
        if method not in ("GET", "HEAD"):
            return self._method_not_allowed("GET")
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return self._send_error(404, "not_found", "File not found")
        ext = os.path.splitext(full)[1].lower()
        self._send_file(
            full,
            CONTENT_TYPES.get(ext, "application/octet-stream"),
            cache="public, max-age=86400",
        )

    def _serve_frontend(self, path, method):
        """Serve the static frontend at "/", falling back to index.html for routes."""
        if method not in ("GET", "HEAD"):
            return self._method_not_allowed("GET")
        if path.startswith("/api/"):
            return self._send_error(404, "not_found", "No route for %s" % path)

        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if path == "/":
            if os.path.isfile(index_path):
                return self._send_file(index_path, CONTENT_TYPES[".html"], cache="no-cache")
            return self._send_json(
                200,
                {
                    "name": "Team Task Board API",
                    "version": "1.0.0",
                    "docs": "/docs",
                    "openapi": "/openapi.json",
                    "frontend": "not deployed (set FRONTEND_DIR)",
                },
            )

        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        if full.startswith(FRONTEND_DIR + os.sep) and os.path.isfile(full):
            ext = os.path.splitext(full)[1].lower()
            return self._send_file(
                full, CONTENT_TYPES.get(ext, "application/octet-stream"), cache="no-cache"
            )

        # Unknown path without a file extension: hand it to the SPA.
        if not os.path.splitext(path)[1] and os.path.isfile(index_path):
            return self._send_file(index_path, CONTENT_TYPES[".html"], cache="no-cache")
        return self._send_error(404, "not_found", "Not found: %s" % path)

    def _send_file(self, full_path, content_type, cache=None):
        if not os.path.isfile(full_path):
            return self._send_error(404, "not_found", "File not found")
        with open(full_path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "%s - %s\n" % (self.log_date_time_string(), fmt % args)
        )


class PayloadTooLarge(Exception):
    pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Team task board API")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--db", default=os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "tasks.db"))
    )
    args = parser.parse_args(argv)

    TaskHandler.store = Store(args.db)
    httpd = ThreadingHTTPServer((args.host, args.port), TaskHandler)

    def shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    sys.stderr.write(
        "Team task board API listening on http://%s:%d (db: %s)\n"
        % (args.host, args.port, os.path.abspath(args.db))
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        sys.stderr.write("Server stopped\n")


if __name__ == "__main__":
    main()
