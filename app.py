#!/usr/bin/env python3
"""Team task board API.

Standard library only: http.server + sqlite3. Runs on Python 3.6+.
"""

import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

from store import PRIORITIES, STATUSES, TaskStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OPENAPI_PATH = os.path.join(BASE_DIR, "openapi.json")

MAX_BODY_BYTES = 64 * 1024
TITLE_MAX = 200
ASSIGNEE_MAX = 50

CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class ValidationError(Exception):
    def __init__(self, fields, message="Request validation failed"):
        Exception.__init__(self, message)
        self.fields = fields
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
    unknown = sorted(set(payload) - {"title", "assignee", "priority", "status"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    fields = {}
    fields["title"] = _clean_text(payload.get("title"), "title", TITLE_MAX)
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
    unknown = sorted(set(payload) - {"title", "assignee", "priority", "status"})
    if unknown:
        raise ValidationError(
            {k: "is not a known field" for k in unknown}, "Unknown fields: " + ", ".join(unknown)
        )
    if not payload:
        raise ValidationError({}, "Provide at least one field to update")
    fields = {}
    if "title" in payload:
        fields["title"] = _clean_text(payload["title"], "title", TITLE_MAX)
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


class TaskHandler(BaseHTTPRequestHandler):
    store = None
    server_version = "TeamTaskBoard/1.0"

    # ------------------------------------------------------------------ utils
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

    def _send_error(self, status, code, message, fields=None):
        payload = {"error": {"code": code, "message": message}}
        if fields:
            payload["error"]["fields"] = fields
        self._send_json(status, payload)

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
                return self._send_json(
                    200,
                    {
                        "name": "Team Task Board API",
                        "version": "1.0.0",
                        "docs": "/docs",
                        "openapi": "/openapi.json",
                    },
                )
            if path == "/api/health" and method == "GET":
                return self._send_json(200, {"status": "ok"})
            if path == "/api/tasks":
                if method == "GET":
                    return self._list_tasks(parsed.query)
                if method == "POST":
                    return self._create_task()
                return self._method_not_allowed("GET, POST")
            if path.startswith("/api/tasks/"):
                task_id = self._parse_id(path)
                if method == "GET":
                    return self._get_task(task_id)
                if method == "PATCH":
                    return self._update_task(task_id)
                if method == "DELETE":
                    return self._delete_task(task_id)
                return self._method_not_allowed("GET, PATCH, DELETE")
            if path == "/openapi.json" and method == "GET":
                return self._send_file(OPENAPI_PATH, "application/json; charset=utf-8")
            if path in ("/docs", "/swagger") and method == "GET":
                return self._send_file(
                    os.path.join(BASE_DIR, "docs.html"), "text/html; charset=utf-8"
                )
            if path.startswith("/static/"):
                return self._serve_static(path, method)
            return self._send_error(404, "not_found", "No route for %s" % path)
        except ValidationError as exc:
            self._send_error(422, "validation_error", exc.message, exc.fields)
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

    # ------------------------------------------------------------------- api
    def _list_tasks(self, query):
        params = parse_qs(query)
        status = params.get("status", [None])[0]
        assignee = params.get("assignee", [None])[0]
        if status and status not in STATUSES:
            raise ValidationError(
                {"status": "must be one of: " + ", ".join(STATUSES)}, "Invalid status filter"
            )
        items = self.store.list(status=status, assignee=assignee)
        self._send_json(200, {"items": items, "total": len(items)})

    def _create_task(self):
        fields = validate_create(self._read_json())
        task = self.store.create(**fields)
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

    def _delete_task(self, task_id):
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

    TaskHandler.store = TaskStore(args.db)
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
