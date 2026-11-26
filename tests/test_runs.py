"""Phase 2 tests: server-side runs, live events, approvals, stop, context budget and usage.

Ollama Cloud is replaced by a scripted fake; everything runs in temporary folders.
Run with:  python -m unittest discover -s tests -v
"""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import context_budget  # noqa: E402
import runs  # noqa: E402
import server  # noqa: E402


def stream(*chunks):
    """An Ollama streaming response made of the given JSON chunks."""
    return io.BytesIO(("\n".join(json.dumps(c) for c in chunks) + "\n").encode())


def say(text, prompt=50, output=5):
    return stream({"message": {"content": text}}, {"done": True, "prompt_eval_count": prompt, "eval_count": output, "eval_duration": 1e9})


def call(name, **arguments):
    return stream({"message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}},
                  {"done": True, "prompt_eval_count": 40, "eval_count": 3, "eval_duration": 1e9})


class Blocking:
    """A response that streams nothing until closed (what Stop does)."""
    def __init__(self): self.closed = threading.Event()
    def readline(self): self.closed.wait(10); return b""
    def close(self): self.closed.set()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); base = Path(self.temp.name)
        self._old = server.CHATS_DIR
        server.CHATS_DIR = str(base / "data" / "chats"); os.makedirs(server.CHATS_DIR)
        self.shared = base / "shared"; self.shared.mkdir(); self.outside = base / "outside"; self.outside.mkdir()
        (self.outside / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
        self.requests, self.script = [], []
        def fake_upstream(path, body, timeout=600):
            self.requests.append(json.loads(json.dumps(body)))
            item = self.script.pop(0) if self.script else say("(no more script)")
            if isinstance(item, Exception): raise item
            return item
        self.patches = [mock.patch.object(server, "upstream", side_effect=fake_upstream),
                        mock.patch.object(server, "load_config", return_value={"apiKey": "k"}),
                        mock.patch.object(server, "model_info", return_value={"capabilities": ["completion", "tools", "vision"], "context_length": 32768}),
                        mock.patch.object(server, "get_models", return_value=([{"name": "m1"}, {"name": "m2"}], []))]
        for p in self.patches: p.start()
        server.ENGINE = runs.Engine(server)
        server._approvals.clear(); server._model_approvals.clear()

    def tearDown(self):
        for cid in list(server.ENGINE.runs): server.ENGINE.stop(cid, wait=5)
        for p in self.patches: p.stop()
        server.CHATS_DIR = self._old; self.temp.cleanup()

    def chat(self, write=True, **caps):
        c = server.create_chat({"model": "m1", "title": "t", "folders": [{"path": str(self.shared), "write": write}]})
        if caps: server.update_permissions(c["id"], {"capabilities": caps})
        return c["id"]

    def wait(self, cid, until=lambda run: not run.alive, timeout=5):
        run = server.ENGINE.runs[cid]; end = time.time() + timeout
        while time.time() < end:
            if until(run): return run
            time.sleep(0.02)
        self.fail("run did not reach the expected state")

    def run_to_end(self, cid, approve=None, **options):
        options.setdefault("text", "go")
        server.ENGINE.start(cid, options)
        while True:
            run = self.wait(cid, lambda r: not r.alive or r.approval)
            if not run.alive: return server.load_chat(cid)
            self.assertIsNotNone(approve, "unexpected approval request: %r" % run.approval["kind"])
            server.ENGINE.decide(cid, run.approval["id"], approve)
            self.wait(cid, lambda r: not r.alive or not r.approval)


class LoopTests(RunTestCase):
    def test_tool_loop_saves_messages_usage_and_no_live_flags(self):
        (self.shared / "a.txt").write_text("hello", encoding="utf-8"); cid = self.chat(write=False)
        self.script = [call("read_file", path=str(self.shared / "a.txt")), say("It says hello.", prompt=120, output=6)]
        chat = self.run_to_end(cid)
        self.assertEqual(["user", "assistant", "tool", "assistant"], [m["role"] for m in chat["messages"]])
        self.assertEqual("hello", chat["messages"][2]["content"]); self.assertEqual("It says hello.", chat["messages"][3]["content"])
        self.assertEqual({"promptTokens": 160, "outputTokens": 9, "requests": 2}, chat["usage"])
        self.assertEqual(126, chat["context"]["used"]); self.assertEqual(32768, chat["context"]["limit"])
        self.assertFalse(any(k.startswith("_") for m in chat["messages"] for k in m))
        self.assertEqual(2, server.usage_summary()["today"]["requests"])
        self.assertEqual("tool", self.requests[1]["messages"][-1]["role"])  # the tool result went back to the model

    def test_model_without_tools_is_retried_with_no_tools(self):
        cid = self.chat()
        refusal = urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"error": "registry.ollama.ai/x does not support tools"}'))
        self.script = [refusal, say("plain answer")]
        chat = self.run_to_end(cid)
        self.assertIn("tools", self.requests[0]); self.assertNotIn("tools", self.requests[1])
        self.assertEqual("plain answer", chat["messages"][-1]["content"]); self.assertIn("doesn't support tools", chat["messages"][-1]["notice"])

    def test_tools_and_note_follow_the_session_policy(self):
        c = server.create_chat({"model": "m1", "folders": []}); server.update_permissions(c["id"], {"capabilities": {"modelControl": "fixed"}})
        self.script = [say("hi")]; self.run_to_end(c["id"])
        self.assertNotIn("tools", self.requests[-1])
        c = server.create_chat({"model": "m1", "folders": []})
        self.script = [say("hi")]; self.run_to_end(c["id"])
        self.assertEqual(["set_session_model"], [t["function"]["name"] for t in self.requests[-1]["tools"]])
        self.assertIn("explicitly asks", self.requests[-1]["messages"][0]["content"])

    def test_model_cannot_reach_outside_shared_folders(self):
        cid = self.chat()
        self.script = [call("read_file", path=str(self.outside / "secret.txt")), say("done")]
        chat = self.run_to_end(cid)
        tool = chat["messages"][2]
        self.assertTrue(tool["isError"]); self.assertNotIn("TOP-SECRET", tool["content"])

    def test_upstream_error_and_missing_key(self):
        cid = self.chat(); self.script = [urllib.error.HTTPError("u", 401, "No", {}, io.BytesIO(b'{"error": "unauthorized"}'))]
        chat = self.run_to_end(cid); self.assertIn("unauthorized", chat["messages"][-1]["error"])
        with mock.patch.object(server, "load_config", return_value={"apiKey": ""}):
            chat = self.run_to_end(cid); self.assertIn("API key", chat["messages"][-1]["error"])


class ApprovalTests(RunTestCase):
    def test_review_write_waits_for_the_user(self):
        cid = self.chat(); f = self.shared / "n.txt"
        self.script = [call("write_file", path=str(f), content="new"), say("ok")]
        chat = self.run_to_end(cid, approve=False)
        self.assertFalse(f.exists()); self.assertIn("denied", chat["messages"][2]["content"])
        self.script = [call("write_file", path=str(f), content="new"), say("ok")]
        self.run_to_end(cid, approve=True); self.assertEqual("new", f.read_text(encoding="utf-8"))

    def test_approval_event_carries_the_full_diff_and_the_run_waits(self):
        cid = self.chat(); f = self.shared / "e.txt"; f.write_text("a=1\n", encoding="utf-8")
        self.script = [call("edit_file", path=str(f), old_text="a=1", new_text="a=2"), say("ok")]
        server.ENGINE.start(cid, {"text": "edit"})
        run = self.wait(cid, lambda r: r.approval)
        snap, _ = server.ENGINE.snapshot(cid)
        self.assertEqual("waiting", snap["status"]); self.assertEqual("write", snap["approval"]["kind"])
        self.assertIn("+a=2", snap["approval"]["data"]["diff"]); self.assertNotIn("token", snap["approval"]["data"])
        self.assertFalse(server.ENGINE.decide(cid, "wrong-id", True))
        self.assertTrue(server.ENGINE.decide(cid, run.approval["id"], True)); self.wait(cid)
        self.assertEqual("a=2\n", f.read_text(encoding="utf-8"))

    def test_auto_edit_writes_without_asking(self):
        cid = self.chat(editMode="auto"); f = self.shared / "auto.txt"
        self.script = [call("write_file", path=str(f), content="auto"), say("ok")]
        self.run_to_end(cid); self.assertEqual("auto", f.read_text(encoding="utf-8"))

    def test_unanswered_approval_times_out_as_denied(self):
        cid = self.chat(); f = self.shared / "late.txt"
        self.script = [call("write_file", path=str(f), content="x"), say("ok")]
        with mock.patch.object(runs, "APPROVAL_WAIT", 0.2):
            server.ENGINE.start(cid, {"text": "go"}); self.wait(cid)
        self.assertFalse(f.exists()); self.assertIn("denied", server.load_chat(cid)["messages"][2]["content"])

    def test_model_switch_inside_a_run(self):
        cid = self.chat()
        self.script = [call("set_session_model", model="m2", reason="user asked"), say("hello from m2")]
        chat = self.run_to_end(cid, approve=True)
        self.assertEqual("m2", chat["model"]); self.assertEqual("m2", self.requests[1]["model"])
        self.assertEqual("m2", chat["messages"][-1]["model"])


class ControlTests(RunTestCase):
    def test_stop_ends_a_streaming_reply(self):
        cid = self.chat(); blocking = Blocking(); self.script = [blocking]
        server.ENGINE.start(cid, {"text": "go"})
        self.wait(cid, lambda r: r.response is not None)
        self.assertTrue(server.ENGINE.stop(cid)); run = self.wait(cid)
        self.assertEqual("idle", run.status); self.assertIn("stopped", server.load_chat(cid)["messages"][-1].get("stats", ""))

    def test_stop_while_waiting_for_approval(self):
        cid = self.chat(); f = self.shared / "s.txt"; self.script = [call("write_file", path=str(f), content="x")]
        server.ENGINE.start(cid, {"text": "go"}); self.wait(cid, lambda r: r.approval)
        server.ENGINE.stop(cid); self.wait(cid)
        self.assertFalse(f.exists()); self.assertIn("cancelled", server.load_chat(cid)["messages"][2]["content"])

    def test_one_run_per_chat_and_two_chats_at_once(self):
        a, b = self.chat(), self.chat(); self.script = [Blocking(), Blocking()]
        server.ENGINE.start(a, {"text": "go"}); server.ENGINE.start(b, {"text": "go"})
        self.assertTrue(server.ENGINE.busy(a) and server.ENGINE.busy(b))
        with self.assertRaises(runs.RunError): server.ENGINE.start(a, {"text": "again"})
        self.assertEqual({a: "running", b: "running"}, server.ENGINE.statuses())

    def test_edit_and_regenerate(self):
        cid = self.chat(); self.script = [say("first"), say("second"), say("edited")]
        self.run_to_end(cid, text="one"); chat = self.run_to_end(cid, regenerate=True, text=None)
        self.assertEqual(["user", "assistant"], [m["role"] for m in chat["messages"]]); self.assertEqual("second", chat["messages"][1]["content"])
        chat = self.run_to_end(cid, text="one, edited", truncateAt=0)
        self.assertEqual("one, edited", chat["messages"][0]["content"]); self.assertEqual(2, len(chat["messages"]))
        with self.assertRaises(runs.RunError): server.ENGINE.start(cid, {"text": "x", "truncateAt": 1})  # not a user message
        with self.assertRaises(runs.RunError): server.ENGINE.start(cid, {"text": "  "})


class HTTPRunTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); super().tearDown()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, None if body is None else json.dumps(body), {"Host": "127.0.0.1:%d" % self.port,
                     "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.port})
        r = conn.getresponse(); data = r.read(); conn.close(); return r.status, json.loads(data or b"{}")

    def test_create_run_and_busy_rules_over_http(self):
        status, chat = self.call("POST", "/api/chats", {"model": "m1", "title": "Hi", "folders": [{"path": str(self.shared), "write": True}]})
        self.assertEqual(200, status); self.assertEqual(capabilities.DEFAULT_POLICY, chat["capabilities"]); cid = chat["id"]
        self.script = [Blocking()]
        self.assertEqual(200, self.call("POST", "/api/runs", {"chatId": cid, "text": "hello"})[0])
        self.assertEqual(409, self.call("POST", "/api/runs", {"chatId": cid, "text": "again"})[0])
        self.assertEqual(409, self.call("PUT", "/api/chats/" + cid, {"messages": []})[0])
        self.assertEqual(409, self.call("POST", "/api/session/model", {"chatId": cid, "model": "m2"})[0])
        self.assertEqual({cid: "running"}, self.call("GET", "/api/runs")[1]["runs"])
        self.assertEqual(200, self.call("DELETE", "/api/chats/" + cid)[0])  # stops the run first
        self.assertFalse(server.ENGINE.busy(cid)); self.assertFalse(os.path.exists(server.chat_path(cid)))
        self.assertEqual(400, self.call("POST", "/api/chats", {"model": ""})[0])

    def test_event_stream_starts_with_a_snapshot(self):
        cid = self.chat()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/chats/%s/events" % cid, headers={"Host": "127.0.0.1:%d" % self.port})
        r = conn.getresponse(); self.assertEqual("text/event-stream; charset=utf-8", r.getheader("Content-Type"))
        lines = []
        while not any(l.startswith(b"data:") for l in lines): lines.append(r.fp.readline())
        event = json.loads([l for l in lines if l.startswith(b"data:")][0][5:])
        conn.close()
        self.assertEqual("snapshot", event["type"]); self.assertEqual(cid, event["chat"]["id"]); self.assertEqual("idle", event["status"])

    def test_approve_endpoint(self):
        cid = self.chat(); f = self.shared / "h.txt"; self.script = [call("write_file", path=str(f), content="via http"), say("ok")]
        self.call("POST", "/api/runs", {"chatId": cid, "text": "go"}); run = self.wait(cid, lambda r: r.approval)
        self.assertEqual(409, self.call("POST", "/api/runs/approve", {"chatId": cid, "approvalId": "nope", "approve": True})[0])
        self.assertEqual(200, self.call("POST", "/api/runs/approve", {"chatId": cid, "approvalId": run.approval["id"], "approve": True})[0])
        self.wait(cid); self.assertEqual("via http", f.read_text(encoding="utf-8"))


class ContextBudgetTests(unittest.TestCase):
    def turn(self, n, tool_chars=0):
        msgs = [{"role": "user", "content": "question %d" % n}]
        if tool_chars:
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]},
                     {"role": "tool", "tool_name": "read_file", "content": "x" * tool_chars}]
        return msgs + [{"role": "assistant", "content": "answer %d" % n}]

    def test_small_history_is_sent_unchanged(self):
        msgs = [{"role": "system", "content": "s"}] + self.turn(1) + self.turn(2)
        out, info = context_budget.build(msgs, 32768)
        self.assertEqual(len(msgs), len(out)); self.assertEqual(0, info["omittedMessages"]); self.assertTrue(info["fits"])

    def test_old_tool_results_are_clipped_but_current_turn_kept(self):
        msgs = self.turn(1, tool_chars=50_000) + self.turn(2, tool_chars=10_000)
        out, info = context_budget.build(msgs, 131072)
        tools = [m["content"] for m in out if m["role"] == "tool"]
        self.assertLess(len(tools[0]), 2_500); self.assertIn("left out to save context", tools[0])
        self.assertEqual("x" * 10_000, tools[1]); self.assertEqual(1, info["clippedToolResults"])

    def test_oldest_whole_turns_are_dropped_with_a_note(self):
        msgs = [{"role": "system", "content": "keep me"}]
        for n in range(40): msgs += self.turn(n) + [{"role": "user", "content": "y" * 4000}, {"role": "assistant", "content": "z" * 4000}]
        out, info = context_budget.build(msgs, 8192)
        self.assertTrue(info["fits"]); self.assertGreater(info["omittedMessages"], 0)
        self.assertEqual("keep me", out[0]["content"]); self.assertIn("left out to fit", out[1]["content"])
        self.assertEqual("user", out[2]["role"])  # history resumes at the start of a turn
        self.assertNotEqual("tool", out[2]["role"])

    def test_failed_placeholders_are_not_sent(self):
        out, _ = context_budget.build([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "", "error": "boom"}], 8192)
        self.assertEqual(1, len(out))


if __name__ == "__main__":
    unittest.main()
