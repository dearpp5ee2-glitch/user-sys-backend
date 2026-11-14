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
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import auth  # noqa: E402
from store import Store  # noqa: E402

ADMIN_USERNAME = "tester"
ADMIN_PASSWORD = "secret123"


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
        cls.store = Store(os.path.join(cls.tmpdir, "tasks.db"))
        app.TaskHandler.store = cls.store
        admin = cls.store.register_user(
            ADMIN_USERNAME,
            auth.hash_password(ADMIN_PASSWORD),
            "测试管理员",
        )
        assert admin["role"] == "admin"
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
        for task in self.store.list():
            self.store.delete(task["id"])
        status, _, data = self.json_request(
            "POST",
            "/api/auth/login",
            {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            auth=False,
        )
        self.assertEqual(status, 200, data)
        self.token = data["token"]
        self.user = data["user"]

    def request(self, method, path, body=None, raw=None, headers=None, auth=True):
        data = None
        send_headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        if raw is not None:
            data = raw
        if auth and getattr(self, "token", None):
            send_headers["Authorization"] = "Bearer " + self.token
        send_headers.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=send_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, resp.headers, payload
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def json_request(self, method, path, body=None, raw=None, headers=None, auth=True):
        status, response_headers, payload = self.request(
            method, path, body=body, raw=raw, headers=headers, auth=auth
        )
        parsed = json.loads(payload.decode("utf-8")) if payload else None
        return status, response_headers, parsed

    def register(self, username=None, password="secret123", display_name=None):
        username = username or "u_" + uuid.uuid4().hex[:8]
        body = {"username": username, "password": password}
        if display_name:
            body["display_name"] = display_name
        status, _, data = self.json_request("POST", "/api/auth/register", body, auth=False)
        return username, status, data

    # ---------------------------------------------------------------- tests
    def test_health(self):
        status, _, data = self.json_request("GET", "/api/health", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_tasks_require_auth(self):
        for method, path in (
            ("GET", "/api/tasks"),
            ("POST", "/api/tasks"),
            ("GET", "/api/tasks/1"),
            ("DELETE", "/api/tasks/1"),
            ("GET", "/api/users"),
            ("GET", "/api/auth/me"),
        ):
            status, headers, data = self.json_request(method, path, auth=False)
            self.assertEqual(status, 401, (method, path))
            self.assertEqual(data["error"]["code"], "unauthorized")
            self.assertIn("Bearer", headers["WWW-Authenticate"])

        status, _, data = self.json_request(
            "GET", "/api/tasks", headers={"Authorization": "Bearer nonsense"}, auth=False
        )
        self.assertEqual(status, 401)

    def test_register_signs_in(self):
        username, status, data = self.register(display_name="新同事")
        self.assertEqual(status, 201)
        self.assertEqual(data["token_type"], "Bearer")
        self.assertTrue(data["token"])
        self.assertEqual(data["user"]["username"], username)
        self.assertEqual(data["user"]["display_name"], "新同事")
        self.assertEqual(data["user"]["role"], "member")
        self.assertNotIn("password", data["user"])
        self.assertNotIn("password_hash", data["user"])
        self.assertNotIn("password_hash", json.dumps(data))

        status, _, me = self.json_request(
            "GET", "/api/auth/me", headers={"Authorization": "Bearer " + data["token"]}, auth=False
        )
        self.assertEqual(status, 200)
        self.assertEqual(me["id"], data["user"]["id"])
        self.assertNotIn("password_hash", me)

    def test_register_validation_and_duplicate(self):
        cases = [
            ({"username": "ab", "password": "secret123"}, "username"),
            ({"username": "bad name", "password": "secret123"}, "username"),
            ({"username": "valid_name", "password": "short"}, "password"),
            ({"username": "valid_name"}, "password"),
            ({"username": "valid_name", "password": "secret123", "role": "admin"}, "role"),
        ]
        for body, field in cases:
            status, _, data = self.json_request("POST", "/api/auth/register", body, auth=False)
            self.assertEqual(status, 422, body)
            self.assertEqual(data["error"]["code"], "validation_error")
            self.assertIn(field, data["error"]["fields"])

        username, status, _ = self.register("dup_user")
        self.assertEqual(status, 201)
        status, _, data = self.json_request(
            "POST", "/api/auth/register", {"username": username, "password": "secret123"}, auth=False
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "username_taken")
        # Usernames are unique regardless of case.
        status, _, data = self.json_request(
            "POST", "/api/auth/register", {"username": "DUP_USER", "password": "secret123"}, auth=False
        )
        self.assertEqual(status, 409)

    def test_first_account_is_admin(self):
        tmpdir = tempfile.mkdtemp(prefix="taskboard-fresh-")
        db_path = os.path.join(tmpdir, "fresh.db")
        app.TaskHandler.store = Store(db_path)
        httpx = app.ThreadingHTTPServer(("127.0.0.1", 0), app.TaskHandler)
        base = "http://127.0.0.1:%d" % httpx.server_address[1]
        thread = threading.Thread(target=httpx.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"username": "owner", "password": "secret123"}).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/auth/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                first = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(first["user"]["role"], "admin")

            body = json.dumps({"username": "second", "password": "secret123"}).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/auth/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                second = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(second["user"]["role"], "member")
        finally:
            httpx.shutdown()
            httpx.server_close()
            app.TaskHandler.store = self.store
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_login_and_logout(self):
        username, status, data = self.register()
        token = data["token"]

        status, _, bad = self.json_request(
            "POST", "/api/auth/login", {"username": username, "password": "wrongpass"}, auth=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(bad["error"]["code"], "unauthorized")
        self.assertEqual(bad["error"]["message"], "Wrong username or password")

        status, _, bad = self.json_request(
            "POST", "/api/auth/login", {"username": "nobody_here", "password": "secret123"}, auth=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(bad["error"]["message"], "Wrong username or password")

        status, _, logged_in = self.json_request(
            "POST", "/api/auth/login", {"username": username, "password": "secret123"}, auth=False
        )
        self.assertEqual(status, 200)
        self.assertTrue(logged_in["token"])
        self.assertNotEqual(logged_in["token"], token)

        status, _, _ = self.json_request(
            "POST",
            "/api/auth/logout",
            headers={"Authorization": "Bearer " + logged_in["token"]},
            auth=False,
        )
        self.assertEqual(status, 204)

        status, _, data = self.json_request(
            "GET",
            "/api/auth/me",
            headers={"Authorization": "Bearer " + logged_in["token"]},
            auth=False,
        )
        self.assertEqual(status, 401)

    def test_expired_token_rejected(self):
        token = auth.new_token()
        self.store.create_session(auth.token_hash(token), self.user["id"], -60)
        status, _, _ = self.json_request(
            "GET", "/api/auth/me", headers={"Authorization": "Bearer " + token}, auth=False
        )
        self.assertEqual(status, 401)

    def test_users_listing(self):
        self.register(display_name="前端同学")
        status, _, data = self.json_request("GET", "/api/users")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 2)
        usernames = [u["username"] for u in data["items"]]
        self.assertIn(ADMIN_USERNAME, usernames)
        for user in data["items"]:
            self.assertNotIn("password_hash", user)

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
        self.assertEqual(task["created_by"], self.user["id"])
        self.assertEqual(task["creator"], ADMIN_USERNAME)
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
        status, _, data = self.json_request("GET", "/api/tasks?mine=maybe")
        self.assertEqual(status, 422)

    def test_shared_board_and_mine_filter(self):
        username, _, registered = self.register()
        other_token = registered["token"]
        status, _, theirs = self.json_request(
            "POST",
            "/api/tasks",
            {"title": "别人的任务", "assignee": username},
            headers={"Authorization": "Bearer " + other_token},
            auth=False,
        )
        self.assertEqual(status, 201)
        self.assertEqual(theirs["creator"], username)

        # The team board is shared: both accounts see the task.
        _, _, data = self.json_request("GET", "/api/tasks")
        self.assertEqual(data["total"], 1)
        _, _, data = self.json_request(
            "GET", "/api/tasks", headers={"Authorization": "Bearer " + other_token}, auth=False
        )
        self.assertEqual(data["total"], 1)

        # ?mine=true narrows it to what the caller created.
        _, _, data = self.json_request("GET", "/api/tasks?mine=true")
        self.assertEqual(data["total"], 0)
        _, _, data = self.json_request(
            "GET",
            "/api/tasks?mine=1",
            headers={"Authorization": "Bearer " + other_token},
            auth=False,
        )
        self.assertEqual(data["total"], 1)

    def test_delete_permissions(self):
        username, _, registered = self.register()
        member_token = registered["token"]
        member_headers = {"Authorization": "Bearer " + member_token}

        _, _, admin_task = self.json_request(
            "POST", "/api/tasks", {"title": "管理员创建", "assignee": "张三"}
        )
        status, _, data = self.json_request(
            "DELETE", "/api/tasks/%d" % admin_task["id"], headers=member_headers, auth=False
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "forbidden")

        status, _, member_task = self.json_request(
            "POST",
            "/api/tasks",
            {"title": "成员创建", "assignee": username},
            headers=member_headers,
            auth=False,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.json_request(
            "DELETE", "/api/tasks/%d" % member_task["id"], headers=member_headers, auth=False
        )
        self.assertEqual(status, 204)

        status, _, other_task = self.json_request(
            "POST",
            "/api/tasks",
            {"title": "成员再创建", "assignee": username},
            headers=member_headers,
            auth=False,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.json_request("DELETE", "/api/tasks/%d" % other_task["id"])
        self.assertEqual(status, 204)  # admin can delete anything

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
                "Access-Control-Request-Headers": "authorization, content-type",
            },
            auth=False,
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("PATCH", headers["Access-Control-Allow-Methods"])
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])

    def test_bad_id_and_route(self):
        status, _, data = self.json_request("GET", "/api/tasks/abc")
        self.assertEqual(status, 422)
        status, _, data = self.json_request("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, _, data = self.json_request("PUT", "/api/tasks")
        self.assertEqual(status, 405)

    def test_frontend_serving(self):
        status, headers, body = self.request("GET", "/", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"taskboard", body)

        status, headers, body = self.request("GET", "/styles.css", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertIn(b"color", body)

        # An unknown extension-less route falls back to the SPA entry point.
        status, headers, body = self.request("GET", "/some/route", auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b"taskboard", body)

        status, _, data = self.json_request("GET", "/missing.png", auth=False)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

        # API paths never fall through to the SPA.
        status, _, data = self.json_request("GET", "/api/unknown", auth=False)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_swagger_assets(self):
        status, _, spec = self.json_request("GET", "/openapi.json", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/api/tasks", spec["paths"])
        self.assertIn("/api/auth/login", spec["paths"])
        self.assertEqual(
            spec["components"]["securitySchemes"]["bearerAuth"]["scheme"], "bearer"
        )
        status, headers, body = self.request("GET", "/docs", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("swagger-ui-bundle.js", body.decode("utf-8"))
        status, _, body = self.request("GET", "/static/swagger-ui/swagger-ui.css", auth=False)
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
