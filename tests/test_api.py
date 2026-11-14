"""End-to-end tests for the task board API.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from store import TaskStore  # noqa: E402


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="taskboard-test-")
        cls.frontend_dir = os.path.join(cls.tmpdir, "frontend")
        os.makedirs(cls.frontend_dir)
        with open(os.path.join(cls.frontend_dir, "index.html"), "w") as handle:
            handle.write("<!doctype html><title>taskboard</title>")
        with open(os.path.join(cls.frontend_dir, "styles.css"), "w") as handle:
            handle.write("body { color: #333; }")
        app.FRONTEND_DIR = cls.frontend_dir
        app.TaskHandler.store = TaskStore(os.path.join(cls.tmpdir, "tasks.db"))
        cls.httpd = app.ThreadingHTTPServer(("127.0.0.1", 0), app.TaskHandler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        for task in app.TaskHandler.store.list():
            app.TaskHandler.store.delete(task["id"])

    def request(self, method, path, body=None, raw=None, headers=None):
        data = None
        send_headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        if raw is not None:
            data = raw
        send_headers.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=send_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, resp.headers, payload
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def json_request(self, method, path, body=None, raw=None):
        status, headers, payload = self.request(method, path, body=body, raw=raw)
        parsed = json.loads(payload.decode("utf-8")) if payload else None
        return status, headers, parsed

    # ---------------------------------------------------------------- tests
    def test_health(self):
        status, _, data = self.json_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_list_empty(self):
        status, _, data = self.json_request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"items": [], "total": 0})

    def test_create_with_defaults(self):
        status, headers, task = self.json_request(
            "POST", "/api/tasks", {"title": "写联调文档", "assignee": "李四"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["priority"], "medium")
        self.assertEqual(task["title"], "写联调文档")
        self.assertEqual(headers["Location"], "/api/tasks/%d" % task["id"])
        self.assertRegex(task["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertTrue(headers["Access-Control-Allow-Origin"] == "*")

    def test_create_trims_and_accepts_all_enums(self):
        status, _, task = self.json_request(
            "POST",
            "/api/tasks",
            {"title": "  联调  ", "assignee": " 王五 ", "priority": "high", "status": "doing"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(task["title"], "联调")
        self.assertEqual(task["assignee"], "王五")
        self.assertEqual(task["priority"], "high")
        self.assertEqual(task["status"], "doing")

    def test_create_validation(self):
        cases = [
            ({"assignee": "张三"}, "title"),
            ({"title": "   ", "assignee": "张三"}, "title"),
            ({"title": "x", "assignee": ""}, "assignee"),
            ({"title": "x", "assignee": "y", "priority": "urgent"}, "priority"),
            ({"title": "x", "assignee": "y", "status": "blocked"}, "status"),
            ({"title": "x", "assignee": "y", "extra": 1}, "extra"),
            ({"title": "x" * 201, "assignee": "y"}, "title"),
        ]
        for body, field in cases:
            status, _, data = self.json_request("POST", "/api/tasks", body)
            self.assertEqual(status, 422, body)
            self.assertEqual(data["error"]["code"], "validation_error")
            self.assertIn(field, data["error"]["fields"])

    def test_create_rejects_bad_json(self):
        status, _, data = self.json_request("POST", "/api/tasks", raw=b"{not json")
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "validation_error")

    def test_status_flow(self):
        _, _, task = self.json_request(
            "POST", "/api/tasks", {"title": "改状态", "assignee": "张三"}
        )
        for expected in ("doing", "done"):
            status, _, updated = self.json_request(
                "PATCH", "/api/tasks/%d" % task["id"], {"status": expected}
            )
            self.assertEqual(status, 200)
            self.assertEqual(updated["status"], expected)
            self.assertEqual(updated["id"], task["id"])
        status, _, data = self.json_request("GET", "/api/tasks/%d" % task["id"])
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "done")
        self.assertNotEqual(data["updated_at"], task["updated_at"])

    def test_update_only_given_field(self):
        _, _, task = self.json_request(
            "POST", "/api/tasks", {"title": "原标题", "assignee": "张三", "priority": "low"}
        )
        _, _, updated = self.json_request("PATCH", "/api/tasks/%d" % task["id"], {"status": "done"})
        self.assertEqual(updated["title"], "原标题")
        self.assertEqual(updated["priority"], "low")
        self.assertEqual(updated["status"], "done")

    def test_update_validation_and_missing(self):
        _, _, task = self.json_request(
            "POST", "/api/tasks", {"title": "x", "assignee": "y"}
        )
        status, _, data = self.json_request("PATCH", "/api/tasks/%d" % task["id"], {})
        self.assertEqual(status, 422)
        status, _, data = self.json_request(
            "PATCH", "/api/tasks/%d" % task["id"], {"status": "nope"}
        )
        self.assertEqual(status, 422)
        status, _, data = self.json_request(
            "PATCH", "/api/tasks/%d" % task["id"], {"title": "  "}
        )
        self.assertEqual(status, 422)
        status, _, data = self.json_request("PATCH", "/api/tasks/9999", {"status": "done"})
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_filters(self):
        self.json_request("POST", "/api/tasks", {"title": "a", "assignee": "张三"})
        self.json_request("POST", "/api/tasks", {"title": "b", "assignee": "李四", "status": "done"})
        self.json_request("POST", "/api/tasks", {"title": "c", "assignee": "张三", "status": "done"})
        _, _, data = self.json_request("GET", "/api/tasks?status=done")
        self.assertEqual(data["total"], 2)
        _, _, data = self.json_request("GET", "/api/tasks?assignee=%E5%BC%A0%E4%B8%89")
        self.assertEqual(data["total"], 2)
        _, _, data = self.json_request("GET", "/api/tasks?status=doing")
        self.assertEqual(data["total"], 0)
        status, _, data = self.json_request("GET", "/api/tasks?status=bad")
        self.assertEqual(status, 422)

    def test_delete(self):
        _, _, task = self.json_request(
            "POST", "/api/tasks", {"title": "删我", "assignee": "张三"}
        )
        status, _, _ = self.json_request("DELETE", "/api/tasks/%d" % task["id"])
        self.assertEqual(status, 204)
        status, _, _ = self.json_request("GET", "/api/tasks/%d" % task["id"])
        self.assertEqual(status, 404)
        status, _, _ = self.json_request("DELETE", "/api/tasks/%d" % task["id"])
        self.assertEqual(status, 404)

    def test_cors_preflight(self):
        status, headers, _ = self.request(
            "OPTIONS",
            "/api/tasks",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("PATCH", headers["Access-Control-Allow-Methods"])

    def test_bad_id_and_route(self):
        status, _, data = self.json_request("GET", "/api/tasks/abc")
        self.assertEqual(status, 422)
        status, _, data = self.json_request("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, _, data = self.json_request("PUT", "/api/tasks")
        self.assertEqual(status, 405)

    def test_frontend_serving(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"taskboard", body)

        status, headers, body = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertIn(b"color", body)

        # An unknown extension-less route falls back to the SPA entry point.
        status, headers, body = self.request("GET", "/some/route")
        self.assertEqual(status, 200)
        self.assertIn(b"taskboard", body)

        status, _, data = self.json_request("GET", "/missing.png")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

        # API paths never fall through to the SPA.
        status, _, data = self.json_request("GET", "/api/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_swagger_assets(self):
        status, _, spec = self.json_request("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/api/tasks", spec["paths"])
        status, headers, body = self.request("GET", "/docs")
        self.assertEqual(status, 200)
        self.assertIn("swagger-ui-bundle.js", body.decode("utf-8"))
        status, _, body = self.request("GET", "/static/swagger-ui/swagger-ui.css")
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
