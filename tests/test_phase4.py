"""Phase 4 tests: subtasks (child chats), limits, and the activity view.

Ollama is replaced by a scripted fake that answers per chat; everything runs in temporary folders.
Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import runs  # noqa: E402
import server  # noqa: E402
from test_runs import Blocking, RunTestCase, call, say  # noqa: E402


class SubtaskTestCase(RunTestCase):
    """Routes the fake model by chat: the parent gets self.script, subtask chats get self.child_script."""

    def setUp(self):
        super().setUp()
        self.child_script = {}
        original = server.upstream.side_effect

        def route(path, body, timeout=600):
            system = (body.get("messages") or [{}])[0].get("content", "")
            if "You are a subtask helper" in system:
                task = next(m["content"] for m in body["messages"] if m["role"] == "user")
                self.requests.append(json.loads(json.dumps(body)))
                item = self.child_script.get(task, say("child answer for: " + task))
                return item() if callable(item) else item
            return original(path, body, timeout)
        server.upstream.side_effect = route

    def parent(self, **caps):
        return self.chat(subtasks=True, **caps)


class SubtaskTests(SubtaskTestCase):
    def test_subtasks_run_and_return_their_answers(self):
        cid = self.parent()
        self.script = [stream_calls(("start_subtask", {"task": "count apples", "title": "Apples"}),
                                    ("start_subtask", {"task": "count pears", "title": "Pears"})),
                       say("Apples and pears counted.")]
        chat = self.run_to_end(cid)
        tools = [m for m in chat["messages"] if m["role"] == "tool"]
        self.assertEqual(2, len(tools)); self.assertIn("child answer for: count apples", tools[0]["content"])
        self.assertIn("child answer for: count pears", tools[1]["content"])
        kids = [c for c in server.list_chats() if c["parentId"] == cid]
        self.assertEqual({"Apples", "Pears"}, {k["title"] for k in kids})
        self.assertEqual(set(t["subtaskId"] for t in tools), {k["id"] for k in kids})

    def test_subtasks_run_at_the_same_time(self):
        cid = self.parent(); gates = []
        def slow():
            b = Blocking(); gates.append(b); return b
        self.child_script = {"a": slow, "b": slow}
        self.script = [stream_calls(("start_subtask", {"task": "a", "title": "A"}), ("start_subtask", {"task": "b", "title": "B"})), say("done")]
        server.ENGINE.start(cid, {"text": "go"})
        end = time.time() + 5
        while len(gates) < 2 and time.time() < end: time.sleep(0.02)
        self.assertEqual(2, len(gates), "both subtasks should be streaming at once")
        self.assertEqual(3, len(server.ENGINE.statuses()))
        server.ENGINE.stop(cid); self.wait(cid)  # stopping the parent stops both children
        self.assertEqual({}, server.ENGINE.statuses())
        self.assertIn("stopped", " ".join(m.get("content", "") for m in server.load_chat(cid)["messages"] if m["role"] == "tool"))

    def test_child_gets_at_most_the_parents_permissions(self):
        cid = self.parent(mode="auto", webSearch=True)
        server.update_permissions(cid, {"folders": [{"path": str(self.shared), "write": True}]})
        server.mark_untrusted(cid, "search: x", ["https://x.example"])
        child = server.load_chat(server.create_subtask_chat(cid, "Helper"))
        self.assertEqual(cid, child["parentId"]); self.assertEqual(server.load_chat(cid)["folders"], child["folders"])
        pol = capabilities.policy(child)
        self.assertEqual(("auto", "fixed", False, True), (pol["editMode"], pol["modelControl"], pol["subtasks"], pol["untrusted"]))
        with self.assertRaises(server.ToolError): server.create_subtask_chat(child["id"], "Grandchild")
        names = {s["function"]["name"] for s in server.tool_specs(child)[0]}
        self.assertNotIn("start_subtask", names); self.assertNotIn("set_session_model", names)

    def test_untrusted_child_makes_parent_untrusted(self):
        cid = self.parent(webSearch=True)
        with mock.patch.object(server, "cloud_post", return_value={"results": [{"title": "t", "url": "https://r.example", "content": "c"}]}):
            self.child_script = {"look it up": None}
            seq = iter([call("web_search", query="q"), say("found it")])
            self.child_script["look it up"] = lambda: next(seq)
            self.script = [call("start_subtask", task="look it up", title="Lookup"), say("ok")]
            chat = self.run_to_end(cid)
        self.assertIn("subtask: Lookup", chat["untrusted"]["sources"])

    def test_subtasks_off_and_limits(self):
        cid = self.parent()
        with mock.patch.object(runs, "MAX_SUBTASKS_PER_REPLY", 1):
            self.script = [stream_calls(("start_subtask", {"task": "one", "title": "1"}), ("start_subtask", {"task": "two", "title": "2"})), say("ok")]
            chat = self.run_to_end(cid)
        tools = [m for m in chat["messages"] if m["role"] == "tool"]
        self.assertFalse(tools[0].get("isError")); self.assertIn("most allowed", tools[1]["content"])

    def test_deleting_parent_deletes_children(self):
        cid = self.parent(); child = server.create_subtask_chat(cid, "Kid")
        server.delete_chat(cid)
        self.assertFalse(os.path.exists(server.chat_path(cid))); self.assertFalse(os.path.exists(server.chat_path(child)))


class LimitTests(RunTestCase):
    def config(self, **limits):
        cfg = dict(server.DEFAULT_CONFIG, apiKey="k", **limits)
        return mock.patch.object(server, "load_config", return_value=cfg)

    def test_daily_limit_stops_before_the_request(self):
        cid = self.chat(); server.record_daily_usage(900, 100)
        with self.config(dailyTokenLimit=1000):
            self.script = [say("should not be sent")]; chat = self.run_to_end(cid)
        self.assertIn("daily token limit", chat["messages"][-1]["error"]); self.assertEqual([], self.requests)

    def test_chat_limit(self):
        cid = self.chat()
        self.script = [say("first", prompt=600, output=500)]; self.run_to_end(cid)
        with self.config(chatTokenLimit=1000):
            self.script = [say("second")]; chat = self.run_to_end(cid)
        self.assertIn("This chat reached its token limit", chat["messages"][-1]["error"])

    def test_concurrent_reply_limit(self):
        a, b = self.chat(), self.chat(); self.script = [Blocking()]
        with self.config(maxConcurrentRuns=1):
            server.ENGINE.start(a, {"text": "go"})
            with self.assertRaises(runs.RunError) as e: server.ENGINE.start(b, {"text": "go"})
        self.assertIn("limit", str(e.exception))


class ActivityTests(RunTestCase):
    def test_activity_lists_running_and_finished(self):
        cid = self.chat(); self.script = [Blocking()]
        server.ENGINE.start(cid, {"text": "go"}); self.wait(cid, lambda r: r.response is not None)
        act = server.ENGINE.activity()
        self.assertEqual([cid], [a["chatId"] for a in act["active"]]); self.assertEqual("running", act["active"][0]["status"])
        server.ENGINE.stop(cid); self.wait(cid)
        act = server.ENGINE.activity()
        self.assertEqual([], act["active"]); self.assertEqual("stopped", act["recent"][0]["status"])
        self.script = [say("hi", prompt=10, output=5)]; self.run_to_end(cid)
        recent = server.ENGINE.activity()["recent"][0]
        self.assertEqual(("done", 1, 15), (recent["status"], recent["steps"], recent["tokens"]))


def stream_calls(*calls):
    """One model response that asks for several tools at once."""
    import io
    chunk = {"message": {"tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]}}
    done = {"done": True, "prompt_eval_count": 40, "eval_count": 3, "eval_duration": 1e9}
    return io.BytesIO((json.dumps(chunk) + "\n" + json.dumps(done) + "\n").encode())


if __name__ == "__main__":
    unittest.main()
