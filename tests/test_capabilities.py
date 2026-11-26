"""Phase 1 regression tests: session policy, file management, trash, rename and model control.

Run with:  python -m unittest discover -s tests -v
Everything runs inside temporary folders; the app's real chats and settings are never touched.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import server  # noqa: E402
import session_actions  # noqa: E402

FULL = {"editMode": "review", "fileManage": True, "deleteFiles": True, "modelControl": "ask"}


class TempChatsMixin:
    """Points the server at a temporary chats folder with one shared (writable) folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self._old_chats = server.CHATS_DIR
        server.CHATS_DIR = str(self.base / "data" / "chats")
        os.makedirs(server.CHATS_DIR)
        self.shared = self.base / "shared"; self.shared.mkdir()
        self.readonly = self.base / "readonly"; self.readonly.mkdir()
        self.outside = self.base / "outside"; self.outside.mkdir()
        server._approvals.clear(); server._model_approvals.clear()

    def tearDown(self):
        server._approvals.clear(); server._model_approvals.clear()
        server.CHATS_DIR = self._old_chats
        self.temp.cleanup()

    def make_chat(self, chat_id="c1", caps=None, write=True, **extra):
        chat = {"id": chat_id, "title": "Test", "model": "model-a", "messages": [],
                "folders": [{"path": str(self.shared), "write": write},
                            {"path": str(self.readonly), "write": False}],
                "capabilities": dict(FULL if caps is None else caps)}
        chat.update(extra)
        server.save_chat(chat_id, chat)
        return server.load_chat(chat_id)


class PolicyTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        p = capabilities.policy({"folders": []})
        self.assertEqual({"editMode": "review", "fileManage": False, "deleteFiles": False, "modelControl": "ask",
                          "webSearch": False, "untrusted": False}, p)

    def test_invalid_saved_values_fall_back_to_safe_defaults(self):
        p = capabilities.policy({"capabilities": {"editMode": "yolo", "modelControl": "whatever"}})
        self.assertEqual("review", p["editMode"]); self.assertEqual("ask", p["modelControl"])

    def test_file_management_needs_a_writable_folder(self):
        chat = {"folders": [{"path": tempfile.gettempdir(), "write": False}],
                "capabilities": {"fileManage": True, "deleteFiles": True}}
        p = capabilities.policy(chat)
        self.assertFalse(p["fileManage"]); self.assertFalse(p["deleteFiles"])


class FileOperationTests(TempChatsMixin, unittest.TestCase):
    def test_create_folder_never_overwrites(self):
        chat = self.make_chat(); target = self.shared / "new"
        self.assertIn("Created folder", server.run_tool(chat, "create_folder", {"path": str(target)}))
        self.assertTrue(target.is_dir())
        with self.assertRaises(server.ToolError): server.run_tool(chat, "create_folder", {"path": str(target)})

    def test_operations_refused_when_disabled_or_read_only_or_outside(self):
        chat = self.make_chat(caps=dict(FULL, fileManage=False))
        with self.assertRaises(server.ToolError): server.run_tool(chat, "create_folder", {"path": str(self.shared / "x")})
        chat = self.make_chat()
        with self.assertRaises(server.ToolError): server.run_tool(chat, "create_folder", {"path": str(self.readonly / "x")})
        with self.assertRaises(server.ToolError): server.run_tool(chat, "create_folder", {"path": str(self.outside / "x")})
        self.assertFalse((self.readonly / "x").exists()); self.assertFalse((self.outside / "x").exists())

    def test_copy_file_and_folder(self):
        chat = self.make_chat()
        (self.shared / "a.txt").write_text("hello", encoding="utf-8")
        server.run_tool(chat, "copy_path", {"source": str(self.shared / "a.txt"), "destination": str(self.shared / "sub" / "b.txt")})
        self.assertEqual("hello", (self.shared / "sub" / "b.txt").read_text(encoding="utf-8"))
        server.run_tool(chat, "copy_path", {"source": str(self.shared / "sub"), "destination": str(self.shared / "sub2")})
        self.assertTrue((self.shared / "sub2" / "b.txt").exists())
        with self.assertRaises(server.ToolError):  # destination exists
            server.run_tool(chat, "copy_path", {"source": str(self.shared / "a.txt"), "destination": str(self.shared / "sub" / "b.txt")})
        with self.assertRaises(server.ToolError):  # into itself
            server.run_tool(chat, "copy_path", {"source": str(self.shared / "sub"), "destination": str(self.shared / "sub" / "inner")})
        with self.assertRaises(server.ToolError):  # out of the sandbox
            server.run_tool(chat, "copy_path", {"source": str(self.shared / "a.txt"), "destination": str(self.outside / "a.txt")})
        self.assertFalse((self.outside / "a.txt").exists())

    def test_move_and_rename(self):
        chat = self.make_chat()
        (self.shared / "old.txt").write_text("x", encoding="utf-8")
        server.run_tool(chat, "move_path", {"source": str(self.shared / "old.txt"), "destination": str(self.shared / "new.txt")})
        self.assertFalse((self.shared / "old.txt").exists()); self.assertTrue((self.shared / "new.txt").exists())
        with self.assertRaises(server.ToolError):  # shared root itself
            server.run_tool(chat, "move_path", {"source": str(self.shared), "destination": str(self.shared / "moved")})

    def test_delete_goes_to_trash_and_restores(self):
        chat = self.make_chat(); f = self.shared / "doomed.txt"; f.write_text("keep me", encoding="utf-8")
        out = server.run_tool(chat, "delete_path", {"path": str(f)})
        self.assertFalse(f.exists()); trash_id = out.split("ID: ")[1].split()[0]
        trash = Path(server.CHATS_DIR).parent / "trash"
        self.assertTrue((trash / (trash_id + ".json")).exists())
        f.write_text("new file in the way", encoding="utf-8")
        with self.assertRaises(server.ToolError): server.run_tool(chat, "restore_path", {"trash_id": trash_id})
        f.unlink()
        self.assertIn("Restored", server.run_tool(chat, "restore_path", {"trash_id": trash_id}))
        self.assertEqual("keep me", f.read_text(encoding="utf-8"))
        with self.assertRaises(server.ToolError): server.run_tool(chat, "restore_path", {"trash_id": "../../etc"})
        with self.assertRaises(server.ToolError): server.run_tool(chat, "delete_path", {"path": str(self.shared)})

    def test_tool_specs_follow_policy(self):
        names = lambda chat: {s["function"]["name"] for s in server.tool_specs(chat)[0]}  # noqa: E731
        full = names(self.make_chat())
        self.assertTrue({"write_file", "create_folder", "delete_path", "set_session_model"} <= full)
        limited = names(self.make_chat(caps={"editMode": "review", "modelControl": "fixed"}))
        self.assertIn("read_file", limited)
        self.assertFalse({"create_folder", "delete_path", "set_session_model"} & limited)
        self.assertEqual({"set_session_model"}, names({"folders": [], "capabilities": {"modelControl": "ask"}}))


class ModelSwitchTests(TempChatsMixin, unittest.TestCase):
    MODELS = ([{"name": "model-a"}, {"name": "model-b"}, {"name": "text-only"}], [])
    CAPS = {"model-a": ["completion", "tools", "vision"], "model-b": ["completion", "tools", "vision"],
            "text-only": ["completion"]}

    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(server, "get_models", return_value=self.MODELS),
                        mock.patch.object(server, "model_capabilities", side_effect=lambda n: self.CAPS.get(n))]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        super().tearDown()

    def switch(self, model, token="", chat_id="c1"):
        return server.change_session_model(chat_id, {"model": model, "reason": "user asked"}, token)

    def test_ask_mode_needs_a_matching_single_use_token(self):
        self.make_chat()
        r = self.switch("model-b"); self.assertTrue(r["needsApproval"])
        self.assertEqual("model-a", server.load_chat("c1")["model"])
        self.assertTrue(self.switch("text-only", r["approvalToken"])["needsApproval"])  # token is bound to model-b
        r = self.switch("model-b"); done = self.switch("model-b", r["approvalToken"])
        self.assertFalse(done["needsApproval"]); self.assertEqual("model-b", server.load_chat("c1")["model"])
        self.assertTrue(self.switch("model-a", r["approvalToken"])["needsApproval"])  # already used

    def test_cancelled_approval_cannot_be_used(self):
        self.make_chat(); r = self.switch("model-b")
        server.cancel_write("c1", r["approvalToken"])
        self.assertTrue(self.switch("model-b", r["approvalToken"])["needsApproval"])
        self.assertEqual("model-a", server.load_chat("c1")["model"])

    def test_allowed_mode_switches_immediately_and_fixed_refuses(self):
        self.make_chat(caps=dict(FULL, modelControl="allowed"))
        self.assertFalse(self.switch("model-b")["needsApproval"]); self.assertEqual("model-b", server.load_chat("c1")["model"])
        self.make_chat(caps=dict(FULL, modelControl="fixed"))
        with self.assertRaises(server.ToolError): self.switch("model-b")

    def test_unknown_model_and_same_model(self):
        self.make_chat(caps=dict(FULL, modelControl="allowed"))
        with self.assertRaises(server.ToolError): self.switch("does-not-exist")
        self.assertIn("already uses", self.switch("model-a")["content"])

    def test_refuses_non_vision_model_when_chat_has_images_and_warns_without_tools(self):
        self.make_chat(caps=dict(FULL, modelControl="allowed"),
                       messages=[{"role": "user", "content": "look", "images": ["aGk="]}])
        with self.assertRaises(server.ToolError): self.switch("text-only")
        self.make_chat(caps=dict(FULL, modelControl="allowed"))
        done = self.switch("text-only")
        self.assertIn("doesn't support tools", done["content"]); self.assertEqual("text-only", server.load_chat("c1")["model"])


class SessionRenameTests(unittest.TestCase):
    def test_title_validation(self):
        self.assertEqual("My session", session_actions.clean_title("  My   session "))
        for bad in ("", "   ", "x" * 101, "bad\x07title"):
            with self.assertRaises(ValueError): session_actions.clean_title(bad)


class HTTPPhase1Tests(TempChatsMixin, unittest.TestCase):
    """End-to-end requests against a real local server (rename, auto-edit, tool injection)."""

    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(timeout=3)
        super().tearDown()

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, json.dumps(body), {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json",
                                                      "Origin": "http://127.0.0.1:%d" % self.port})
        r = conn.getresponse(); data = r.read(); conn.close(); return r.status, json.loads(data or b"{}")

    def test_rename_does_not_deadlock_and_chat_stays_usable(self):
        self.make_chat()
        status, body = self.post("/api/session/rename", {"chatId": "c1", "title": "Renamed"})  # used to hang forever
        self.assertEqual(200, status); self.assertEqual("Renamed", body["title"])
        self.assertEqual("Renamed", server.load_chat("c1")["title"])
        status, body = self.post("/api/session/rename", {"chatId": "c1", "title": "   "})
        self.assertEqual(400, status)

class EditAndVersionTests(TempChatsMixin, unittest.TestCase):
    """Phase 1.6: edit_file, kept versions before overwrites, and the trash screen's operations."""

    def trash(self):
        return capabilities.trash_root(server.CHATS_DIR)

    def test_edit_file_exact_unique_replace(self):
        chat = self.make_chat(caps=dict(FULL, editMode="auto")); f = self.shared / "a.py"
        f.write_bytes(b"x = 1\r\ny = 2\r\nx = 1\r\n")  # CRLF file
        with self.assertRaises(server.ToolError): server.run_tool(chat, "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 9"})
        with self.assertRaises(server.ToolError): server.run_tool(chat, "edit_file", {"path": str(f), "old_text": "nope", "new_text": "z"})
        server.run_tool(chat, "edit_file", {"path": str(f), "old_text": "y = 2\nx = 1", "new_text": "y = 3\nx = 1"})  # LF from the model
        self.assertEqual(b"x = 1\r\ny = 3\r\nx = 1\r\n", f.read_bytes())
        server.run_tool(chat, "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 0", "replace_all": True})
        self.assertEqual(b"x = 0\r\ny = 3\r\nx = 0\r\n", f.read_bytes())
        (self.shared / "bin.dat").write_bytes(b"\x00\x01abc")
        with self.assertRaises(server.ToolError): server.run_tool(chat, "edit_file", {"path": str(self.shared / "bin.dat"), "old_text": "abc", "new_text": "d"})
        with self.assertRaises(server.ToolError): server.run_tool(chat, "edit_file", {"path": str(self.readonly / "x.txt"), "old_text": "a", "new_text": "b"})

    def test_overwrites_keep_previous_version_and_restore_is_undoable(self):
        chat = self.make_chat(); f = self.shared / "notes.txt"; f.write_text("v1", encoding="utf-8")
        out = server.run_tool(chat, "write_file", {"path": str(f), "content": "v2"})
        self.assertIn("Previous version kept in trash", out)
        items = capabilities.list_trash(self.trash()); self.assertEqual(1, len(items))
        self.assertEqual(("version", os.path.realpath(f), "c1"), (items[0]["kind"], os.path.realpath(items[0]["originalPath"]), items[0]["chatId"]))
        capabilities.restore_item(self.trash(), items[0]["id"])  # user restores v1 from the Trash screen
        self.assertEqual("v1", f.read_text(encoding="utf-8"))
        items = capabilities.list_trash(self.trash()); self.assertEqual(1, len(items))  # v2 is now the kept version
        capabilities.restore_item(self.trash(), items[0]["id"]); self.assertEqual("v2", f.read_text(encoding="utf-8"))
        server.run_tool(chat, "write_file", {"path": str(self.shared / "new.txt"), "content": "fresh"})
        self.assertEqual(1, len(capabilities.list_trash(self.trash())))  # creating a new file keeps nothing

    def test_purge_is_permanent_and_validates_ids(self):
        chat = self.make_chat(); f = self.shared / "gone.txt"; f.write_text("bye", encoding="utf-8")
        server.run_tool(chat, "delete_path", {"path": str(f)})
        item = capabilities.list_trash(self.trash())[0]
        with self.assertRaises(capabilities.CapabilityError): capabilities.purge_trash(self.trash(), ["../../server"])
        self.assertEqual(1, capabilities.purge_trash(self.trash(), [item["id"]]))
        self.assertEqual([], capabilities.list_trash(self.trash())); self.assertEqual(["." * 0] * 0, [p.name for p in Path(self.trash()).iterdir()])

    def test_reviewed_edit_shows_diff_and_refuses_if_file_changed_after_review(self):
        self.make_chat(); f = self.shared / "cfg.ini"; f.write_text("debug=false\nport=80\n", encoding="utf-8")
        prepared = server.prepare_write("c1", "edit_file", {"path": str(f), "old_text": "port=80", "new_text": "port=8080"})
        self.assertEqual("edit_file", prepared["tool"]); self.assertIn("-port=80", prepared["diff"]); self.assertIn("+port=8080", prepared["diff"])
        self.assertEqual("debug=false\nport=80\n", f.read_text(encoding="utf-8"))  # nothing written before approval
        self.assertIn("Edited", server.run_approved_write("c1", prepared["token"]))
        self.assertEqual("debug=false\nport=8080\n", f.read_text(encoding="utf-8"))
        prepared = server.prepare_write("c1", "edit_file", {"path": str(f), "old_text": "debug=false", "new_text": "debug=true"})
        f.write_text("debug=false\nport=9999\n", encoding="utf-8")  # someone else changes the file meanwhile
        with self.assertRaises(server.ToolError): server.run_approved_write("c1", prepared["token"])
        self.assertEqual("debug=false\nport=9999\n", f.read_text(encoding="utf-8"))
        with self.assertRaises(server.ToolError):  # edit_file can't create files
            server.prepare_write("c1", "edit_file", {"path": str(self.shared / "missing.txt"), "old_text": "a", "new_text": "b"})


class ServerOwnedSettingsHTTPTests(TempChatsMixin, unittest.TestCase):
    """Phase 1.6: browser saves can't change folders/permissions/title; the dedicated endpoints validate."""

    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(timeout=3)
        super().tearDown()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, None if body is None else json.dumps(body),
                     {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.port})
        r = conn.getresponse(); data = r.read(); conn.close(); return r.status, json.loads(data or b"{}")

    def test_new_chat_gets_default_permissions_and_keeps_chosen_folders(self):
        status, _ = self.call("PUT", "/api/chats/n1", {"title": "New", "messages": [], "folders": [{"path": str(self.shared), "write": True}],
                                                         "capabilities": {"editMode": "auto", "deleteFiles": True, "modelControl": "allowed"}})
        self.assertEqual(200, status); saved = server.load_chat("n1")
        self.assertEqual(capabilities.DEFAULT_POLICY, saved["capabilities"])
        self.assertEqual([{"path": str(self.shared), "write": True}], saved["folders"])

    def test_browser_save_cannot_change_server_owned_fields(self):
        self.make_chat(caps=dict(FULL, editMode="review"))
        stale = server.load_chat("c1"); stale.pop("_path")
        stale.update(title="Hacked", capabilities=dict(FULL, editMode="auto"), folders=[{"path": str(self.outside), "write": True}],
                     messages=[{"role": "user", "content": "hello"}], model="model-b")
        self.assertEqual(200, self.call("PUT", "/api/chats/c1", stale)[0])
        saved = server.load_chat("c1")
        self.assertEqual("Test", saved["title"]); self.assertEqual("review", saved["capabilities"]["editMode"])
        self.assertNotIn(str(self.outside), [f["path"] for f in saved["folders"]])
        self.assertEqual("hello", saved["messages"][0]["content"]); self.assertEqual("model-b", saved["model"])  # browser-owned fields do save

    def test_permissions_endpoint_validates_and_updates(self):
        self.make_chat(caps={"editMode": "review", "modelControl": "ask"})
        status, body = self.call("POST", "/api/session/permissions", {"chatId": "c1", "capabilities": {"editMode": "yolo"}})
        self.assertEqual(400, status)
        status, body = self.call("POST", "/api/session/permissions", {"chatId": "c1", "folders": [{"path": "bad\x01path"}]})
        self.assertEqual(400, status)
        status, body = self.call("POST", "/api/session/permissions", {"chatId": "c1", "capabilities": {"editMode": "auto", "fileManage": True},
                                                                       "folders": [{"path": str(self.shared), "write": True}, {"path": str(self.shared), "write": False}]})
        self.assertEqual(200, status); self.assertEqual(1, len(body["folders"]))
        self.assertTrue(body["policy"]["fileManage"]); self.assertEqual("auto", server.load_chat("c1")["capabilities"]["editMode"])
        self.assertEqual("ask", server.load_chat("c1")["capabilities"]["modelControl"])  # untouched keys are kept
        self.assertEqual(400, self.call("POST", "/api/session/permissions", {"chatId": "missing", "folders": []})[0])

    def test_unexpected_errors_return_json_500(self):
        with mock.patch.object(server, "list_chats", side_effect=RuntimeError("boom")):
            status, body = self.call("GET", "/api/chats")
        self.assertEqual(500, status); self.assertIn("boom", body["error"])

    def test_trash_endpoints(self):
        chat = self.make_chat(); f = self.shared / "t.txt"; f.write_text("one", encoding="utf-8")
        server.run_tool(chat, "write_file", {"path": str(f), "content": "two"})
        status, body = self.call("GET", "/api/trash"); self.assertEqual(200, status)
        self.assertEqual(1, len(body["items"])); self.assertEqual(3, body["totalBytes"]); self.assertNotIn("storedName", body["items"][0])
        status, body = self.call("POST", "/api/trash/restore", {"id": body["items"][0]["id"]})
        self.assertEqual(200, status); self.assertEqual("one", f.read_text(encoding="utf-8"))
        self.assertEqual(400, self.call("POST", "/api/trash/restore", {"id": "..\\..\\x"})[0])
        status, body = self.call("POST", "/api/trash/purge", {"all": True}); self.assertEqual(1, body["removed"])
        self.assertEqual([], self.call("GET", "/api/trash")[1]["items"])


if __name__ == "__main__":
    unittest.main()
