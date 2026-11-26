"""Isolated regression tests for Open AY Desktop.

Run with Python's standard library:
    python -m unittest discover -s tests -v
"""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        server.CHATS_DIR = os.path.join(self.base, "chats")
        os.makedirs(server.CHATS_DIR)
        self.shared = self.base / "shared"
        self.outside = self.base / "outside"
        self.shared.mkdir(); self.outside.mkdir()
        (self.outside / "secret.txt").write_text("TOP-SECRET\n", encoding="utf-8")
        self.chat_id = "test-chat"
        self.chat = {"id": self.chat_id, "title": "Test", "messages": [],
                     "folders": [{"path": str(self.shared), "write": True}]}
        server.save_chat(self.chat_id, self.chat)
        server._approvals.clear()

    def tearDown(self):
        server._approvals.clear(); self.temp.cleanup()

    def test_empty_and_traversal_paths_are_denied(self):
        with self.assertRaises(server.ToolError):
            server.resolve_path({"folders": [{"path": "", "write": True}]}, "")
        with self.assertRaises(server.ToolError):
            server.resolve_path({"folders": self.chat["folders"]}, str(self.outside / "secret.txt"))

    def test_recursive_search_never_reads_outside_symlink(self):
        link = self.shared / "leak.txt"
        try:
            link.symlink_to(self.outside / "secret.txt")
        except (OSError, NotImplementedError) as e:
            self.skipTest("Symlink creation is not available: %s" % e)
        result = server.tool_search_files({"folders": self.chat["folders"]}, contains="TOP-SECRET")
        self.assertEqual("No matches.", result)

    def test_permissions_come_from_saved_chat_not_request(self):
        target = self.shared / "target.txt"; target.write_text("safe", encoding="utf-8")
        with self.assertRaises(server.ToolError):
            server.run_tool(server.saved_tool_config(self.chat_id), "read_file",
                            {"path": str(self.outside / "secret.txt")})
        self.assertEqual("safe", server.tool_read_file({"folders": self.chat["folders"]}, str(target)))

    def test_write_requires_bound_single_use_token_and_full_diff(self):
        target = self.shared / "target.txt"; target.write_text("old line\n", encoding="utf-8")
        content = "first line\n" + ("x" * 6000) + "\nlast line\n"
        prepared = server.prepare_write(self.chat_id, "write_file", {"path": str(target), "content": content})
        self.assertTrue(prepared["exists"]); self.assertEqual(content, prepared["content"])
        self.assertIn("old line", prepared["diff"]); self.assertIn("last line", prepared["diff"])
        self.assertIn("x" * 6000, prepared["diff"])
        with self.assertRaises(server.ToolError): server.run_approved_write(self.chat_id, "forged-token")
        self.assertEqual("old line\n", target.read_text(encoding="utf-8"))
        server.run_approved_write(self.chat_id, prepared["token"])
        self.assertEqual(content, target.read_text(encoding="utf-8"))
        with self.assertRaises(server.ToolError): server.run_approved_write(self.chat_id, prepared["token"])

    def test_changed_content_cannot_be_substituted_for_approved_content(self):
        target = self.shared / "target.txt"; target.write_text("old", encoding="utf-8")
        prepared = server.prepare_write(self.chat_id, "write_file", {"path": str(target), "content": "approved"})
        server.run_approved_write(self.chat_id, prepared["token"])
        self.assertEqual("approved", target.read_text(encoding="utf-8"))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.CHATS_DIR = os.path.join(self.temp.name, "chats"); os.makedirs(server.CHATS_DIR)

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_saves_are_valid_json_and_leave_no_temp_files(self):
        errors = []
        def save(n):
            try:
                for i in range(25):
                    server.save_chat("concurrent", {"title": "worker-%d" % n, "sequence": i,
                                     "messages": [{"role": "user", "content": str(n) * 1000}]})
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=save, args=(n,)) for n in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual([], errors); self.assertEqual("concurrent", server.load_chat("concurrent")["id"])
        self.assertEqual([], list(Path(server.CHATS_DIR).glob("*.tmp")))


class ConfigAndUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.CONFIG_PATH = os.path.join(self.temp.name, "config.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_config_preserves_model_and_removes_local_options(self):
        with open(server.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"lastModel": "example:cloud", "mode": "local", "localUrl": "http://localhost:11434",
                       "apiKey": "secret", "temperature": 0.5, "systemPrompt": "legacy"}, f)
        cfg = server.load_config()
        self.assertEqual("example:cloud", cfg["defaultModel"])
        self.assertNotIn("mode", cfg); self.assertNotIn("localUrl", cfg)
        self.assertNotIn("temperature", cfg); self.assertNotIn("systemPrompt", cfg)
        public = server.public_config(cfg)
        self.assertNotIn("apiKey", public); self.assertTrue(public["hasApiKey"])
        self.assertEqual("Open AY Desktop", public["appName"])

    def test_new_config_saves_only_cloud_settings(self):
        server.save_config({"apiKey": "secret", "defaultModel": "example:cloud", "mode": "local", "temperature": 1})
        with open(server.CONFIG_PATH, encoding="utf-8") as f: saved = json.load(f)
        self.assertEqual("example:cloud", saved["defaultModel"]); self.assertNotIn("mode", saved); self.assertNotIn("temperature", saved)

    @mock.patch.object(server, "APP_VERSION", "1.0.0")  # independent of the real installed version
    def test_update_numeric_comparison(self):
        self.assertLess(server.version_tuple("v1.9.0"), server.version_tuple("1.10.0"))
        release = {"tag_name": "v1.1.0", "name": "Next", "html_url": "https://github.com/arothm/OpenAYDesktop/releases/tag/v1.1.0"}
        result = server.check_for_updates(lambda *args, **kwargs: release)
        self.assertTrue(result["updateAvailable"]); self.assertEqual("1.1.0", result["latestVersion"])
        release["tag_name"] = "v1.0.0"
        result = server.check_for_updates(lambda *args, **kwargs: release)
        self.assertFalse(result["updateAvailable"]); self.assertEqual("current", result["status"])

    def test_no_release_and_bad_release(self):
        error = urllib.error.HTTPError(server.UPDATE_API_URL, 404, "not found", {}, None)
        def no_release(*args, **kwargs): raise error
        result = server.check_for_updates(no_release)
        self.assertEqual("no-release", result["status"]); self.assertFalse(result["updateAvailable"])
        with self.assertRaises(RuntimeError):
            server.check_for_updates(lambda *args, **kwargs: {"tag_name": "not-a-version"})

    def test_update_failure_is_clear(self):
        with self.assertRaises(RuntimeError):
            server.check_for_updates(lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); base = Path(cls.temp.name)
        server.APP_DIR = str(base); server.DATA_DIR = str(base / "data")
        server.CHATS_DIR = str(base / "data" / "chats"); server.VENDOR_DIR = str(base / "vendor")
        server.CONFIG_PATH = str(base / "data" / "config.json"); os.makedirs(server.CHATS_DIR)
        (base / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
        (base / "app.js").write_text("window.test = true;", encoding="utf-8")
        (base / "styles.css").write_text("body {}", encoding="utf-8"); os.makedirs(server.VENDOR_DIR)
        for name in server.VENDOR_FILES: (base / "vendor" / name).write_text("/* locally cached */", encoding="utf-8")
        cls.shared = base / "shared"; cls.shared.mkdir()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]; cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=3); cls.temp.cleanup()

    def request(self, method, path, body=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:%d" % self.port}
        if origin: headers["Origin"] = "http://127.0.0.1:%d" % self.port
        payload = None
        if body is not None: payload = json.dumps(body); headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers); response = conn.getresponse(); data = response.read()
        result = (response.status, dict(response.getheaders()), data); conn.close(); return result

    def create_chat(self, chat_id="http-chat", write=True):
        chat = {"id": chat_id, "title": "HTTP test", "messages": [], "folders": [{"path": str(self.shared), "write": write}]}
        self.request("PUT", "/api/chats/" + chat_id, chat); return chat

    def test_ui_assets_and_security_headers(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(200, status); self.assertIn(b"<!doctype html>", data); self.assertIn("script-src 'self'", headers.get("Content-Security-Policy", ""))
        for path, marker in (("/app.js", b"window.test"), ("/styles.css", b"body"), ("/vendor/marked.min.js", b"locally cached")):
            status, _, data = self.request("GET", path); self.assertEqual(200, status); self.assertIn(marker, data)
        status, _, _ = self.request("GET", "/vendor/%2e%2e/server.py"); self.assertEqual(404, status)

    def test_cross_origin_requests_are_rejected(self):
        status, _, _ = self.request("GET", "/api/chats", origin=False); self.assertEqual(200, status)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/chats", headers={"Host": "127.0.0.1:%d" % self.port, "Origin": "https://example.com"})
        response = conn.getresponse(); response.read(); conn.close(); self.assertEqual(403, response.status)

if __name__ == "__main__":
    unittest.main()
