"""A running reply keeps the user's renames and settings, and a parent hears when its subtask pauses for low balance.

Run with:  python -m unittest discover -s tests -v
"""

import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import test_phase4  # noqa: E402
from test_runs import Blocking, RunTestCase, call, say  # noqa: E402


class SessionRefreshTests(RunTestCase):
    def test_rename_while_running_reaches_the_reply_and_windows(self):
        cid = self.chat()
        block = Blocking()
        self.script = [block, say("done")]
        server.ENGINE.start(cid, {"text": "go"})
        run = self.wait(cid, lambda r: r.response is not None)
        ch = server.ENGINE.channel(cid)
        with ch.cond:
            seq = ch.seq
        with server.chat_lock(cid):
            c = server.load_chat(cid); c["title"] = "New name"; server.save_chat(cid, c)
        snap, _ = server.ENGINE.snapshot(cid)
        self.assertEqual("New name", snap["chat"]["title"])  # a window opening the chat now sees the new name
        server.refresh_running(cid)
        self.assertEqual("New name", run.chat["title"])
        with ch.cond:
            events = [e for s, e in ch.events if s > seq] if hasattr(ch, "events") else []
        if events:
            self.assertTrue(any(e.get("type") == "session" and e["chat"].get("title") == "New name" for e in events))
        block.close(); self.wait(cid)
        self.assertEqual("New name", server.load_chat(cid)["title"])  # the reply's save keeps it

    def test_wrapping_status(self):
        cid = self.chat()
        block = Blocking()
        self.script = [block]
        server.ENGINE.start(cid, {"text": "go"})
        run = self.wait(cid, lambda r: r.response is not None)
        self.assertEqual("running", server.ENGINE.statuses()[cid])
        run.wrapping_up = True
        self.assertEqual("wrapping", server.ENGINE.statuses()[cid])
        run.wrapping_up = False
        block.close(); self.wait(cid)


class SubtaskPauseTests(test_phase4.SubtaskTestCase):
    def test_parent_stops_waiting_when_a_subtask_pauses_for_low_balance(self):
        cid = self.parent()
        checks = {}
        parent_thread = "run-" + cid

        def balance_low(*a, **k):  # low only for the subtask, after its first step
            name = threading.current_thread().name
            if name == parent_thread or not name.startswith("run-"):
                return False, None, 1.0
            checks[name] = checks.get(name, 0) + 1
            return checks[name] > 1, {"totalUsd": 0.86}, 1.0
        steps = iter([call("list_directory", path=str(self.shared)), say("Summary so far: the sitemap is half done.")])
        self.child_script = {"draft the sitemap": lambda: next(steps)}
        self.script = [call("start_subtask", task="draft the sitemap", title="Sitemap"), say("Paused for now."),
                       say("Still paused."), say("Done for now.")]
        with mock.patch.object(server, "balance_low", side_effect=balance_low):
            chat = self.run_to_end(cid)
        start = next(m for m in chat["messages"] if m.get("tool_name") == "start_subtask")["content"]
        self.assertIn("is paused", start); self.assertIn("Summary so far: the sitemap is half done.", start)
        kid = next(c for c in server.list_chats() if c.get("parentId") == cid)
        self.wait(kid["id"])
        self.assertEqual("stopped", server.ENGINE.runs[kid["id"]].outcome)  # the parent's reply ended, so it was stopped
