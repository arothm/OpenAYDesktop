"""1.15: long one-line files, Git hooks and .git writes, retries, Compact, the chat list cache, scheduled tasks
waiting for a free slot, terminal jobs, forgetting idle chats, and where copied files are shown.

Run with:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import commands  # noqa: E402
import gittools  # noqa: E402
import runs  # noqa: E402
import scheduler  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say, stream  # noqa: E402


class ReadAndGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_one_line_file_is_cut_to_the_read_limit(self):
        p = self.root / "bundle.min.js"; p.write_text("x" * 2_000_000, encoding="utf-8")
        out = server.read_window(str(p))
        self.assertLess(len(out), server.READ_CHARS + 300)
        self.assertIn("2,000,000 characters long", out)
        short = self.root / "a.txt"; short.write_text("one\ntwo\n", encoding="utf-8")
        self.assertEqual("one\ntwo\n", server.read_window(str(short)))  # ordinary files are unchanged

    def test_file_tools_cant_change_files_inside_git(self):
        repo = self.root / "repo"; (repo / ".git" / "hooks").mkdir(parents=True)
        chat = {"folders": [{"path": str(repo), "write": True}]}
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.resolve(chat, str(repo / ".git" / "hooks" / "pre-commit"), write=True)
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.resolve(chat, str(repo / ".git" / "config"), write=True)
        capabilities.resolve(chat, str(repo / ".git" / "config"))  # reading is fine
        capabilities.resolve(chat, str(repo / "src" / "app.py"), write=True)

    @unittest.skipUnless(shutil.which("git"), "Git isn't installed")
    def test_git_commands_run_without_hooks(self):
        repo = self.root / "hooked"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
        marker = repo / "hook-ran.txt"
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho ran > '%s'\n" % marker.as_posix(), encoding="utf-8"); os.chmod(hook, 0o755)
        (repo / "f.txt").write_text("hi", encoding="utf-8")
        gittools.run(str(repo), "add", "-A")
        gittools.run(str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
        self.assertFalse(marker.exists(), "a pre-commit hook ran")
        self.assertIn("x", gittools.run(str(repo), "log", "--oneline"))


class RetryAndCompactTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.delays = mock.patch.object(runs, "RETRY_DELAYS", (0.01, 0.01)); self.delays.start()

    def tearDown(self):
        self.delays.stop(); super().tearDown()

    def busy(self):
        return urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b'{"error": "overloaded"}'))

    def test_a_temporary_error_is_tried_again(self):
        cid = self.chat()
        self.script = [self.busy(), say("worked the second time")]
        chat = self.run_to_end(cid)
        last = chat["messages"][-1]
        self.assertEqual("worked the second time", last["content"]); self.assertNotIn("error", last); self.assertNotIn("notice", last)
        self.assertEqual(2, len(self.requests))

    def test_retries_end_with_the_error(self):
        cid = self.chat()
        self.script = [self.busy(), self.busy(), self.busy()]
        chat = self.run_to_end(cid)
        self.assertIn("overloaded", chat["messages"][-1]["error"]); self.assertEqual(3, len(self.requests))

    def test_a_cut_off_stream_starts_over(self):
        cid = self.chat()
        self.script = [stream({"message": {"content": "half an ans"}}), say("the whole answer")]  # no "done": cut off
        chat = self.run_to_end(cid)
        self.assertEqual("the whole answer", chat["messages"][-1]["content"])

    def test_compact_summarizes_older_turns_without_a_reply(self):
        cid = self.chat()
        self.script = [say("first answer " + "x" * 30_000), say("second answer " + "y" * 30_000)]
        self.run_to_end(cid, text="first"); self.run_to_end(cid, text="second")
        before = len(server.load_chat(cid)["messages"])
        summary = io.BytesIO(json.dumps({"message": {"content": "- Goal: test"}, "prompt_eval_count": 10, "eval_count": 3}).encode())
        self.script = [summary]
        ch = server.ENGINE.channel(cid)
        chat = self.run_to_end(cid, compact=True)
        self.assertEqual(before, len(chat["messages"]))  # no new message
        self.assertEqual("- Goal: test", chat["compaction"]["summary"]); self.assertGreater(chat["compaction"]["upTo"], 0)
        done = [e for _, e in ch.events if e.get("type") == "compact_done"]
        self.assertTrue(done and done[-1]["ok"])

    def test_compact_needs_two_messages_from_you(self):
        cid = self.chat()
        self.script = [say("only answer")]
        self.run_to_end(cid)
        with self.assertRaises(runs.RunError):
            server.ENGINE.start(cid, {"compact": True})

    def test_copy_and_move_steps_name_where_the_file_is_now(self):
        cid = self.chat(fileManage=True)
        (self.shared / "a.txt").write_text("a", encoding="utf-8")
        self.script = [call("copy_path", source=str(self.shared / "a.txt"), destination=str(self.shared / "b.txt")), say("done")]
        chat = self.run_to_end(cid)
        step = next(m for m in chat["messages"] if m.get("tool_name") == "copy_path")
        self.assertEqual(str(self.shared / "b.txt"), step["target"]); self.assertIn("→", step["display"])

    def test_snapshot_and_status_carry_the_start_time(self):
        cid = self.chat()
        self.script = [say("hi")]
        ch = server.ENGINE.channel(cid)
        self.run_to_end(cid)
        snap = next(e for _, e in ch.events if e.get("type") == "snapshot")
        self.assertGreater(snap["started"], 0)


class EngineMemoryTests(RunTestCase):
    def test_idle_chats_are_forgotten_unless_a_window_follows_them(self):
        cid = self.chat(); other = self.chat()
        self.script = [say("a"), say("b")]
        self.run_to_end(cid); self.run_to_end(other)
        server.ENGINE.channel(other).subscribers = 1  # a window is open on this one
        with mock.patch.object(runs, "IDLE_FORGET", 0):
            for ch in server.ENGINE.channels.values(): ch.last = 0
            for r in server.ENGINE.runs.values(): r.ended = 1
            server.ENGINE.prune()
        self.assertNotIn(cid, server.ENGINE.runs); self.assertNotIn(cid, server.ENGINE.channels)
        self.assertIn(other, server.ENGINE.channels)

    def test_stop_all_ends_every_reply_together(self):
        from test_runs import Blocking
        a, b = self.chat(), self.chat()
        self.script = [Blocking(), Blocking()]
        server.ENGINE.start(a, {"text": "go"}); server.ENGINE.start(b, {"text": "go"})
        self.wait(a, lambda r: r.response is not None); self.wait(b, lambda r: r.response is not None)
        started = time.time()
        self.assertEqual(2, server.ENGINE.stop_all(wait=5))
        self.assertLess(time.time() - started, 4)
        self.assertFalse(server.ENGINE.busy(a) or server.ENGINE.busy(b))


class ChatListCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self._old = server.CHATS_DIR
        server.CHATS_DIR = os.path.join(self.temp.name, "chats"); os.makedirs(server.CHATS_DIR)

    def tearDown(self):
        server.CHATS_DIR = self._old; self.temp.cleanup()

    def test_the_list_follows_changes_and_reads_only_changed_files(self):
        a = server.create_chat({"model": "m", "title": "First"}); server.create_chat({"model": "m", "title": "Second"})
        self.assertEqual({"First", "Second"}, {c["title"] for c in server.list_chats()})
        real_open = open
        with mock.patch("builtins.open", side_effect=lambda *x, **k: real_open(*x, **k)) as opened:
            server.list_chats()
            self.assertEqual(0, opened.call_count)  # nothing changed: nothing re-read
        with server.chat_lock(a["id"]):
            c = server.load_chat(a["id"]); c["title"] = "Renamed"; server.save_chat(a["id"], c)
        self.assertIn("Renamed", {c["title"] for c in server.list_chats()})
        os.remove(server.chat_path(a["id"]))
        self.assertEqual(["Second"], [c["title"] for c in server.list_chats()])


class ScheduleWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.data = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_a_due_task_waits_for_a_free_slot_and_then_starts(self):
        task = scheduler.save_task(self.data, {"name": "Nightly", "prompt": "check", "kind": "daily", "time": "00:00", "model": "m"}, ["m"], set())
        calls = []
        def start(t):
            calls.append(t["id"])
            if len(calls) == 1: raise scheduler.Busy("4 replies are already running.")
            return "c123"
        s = scheduler.Scheduler(lambda: self.data, start)
        s.tick(datetime.now())
        self.assertIn(task["id"], s.waiting)
        self.assertIn("Waiting to start", scheduler.load(self.data)[0]["lastError"])
        s.tick(datetime.now())  # not due again, but it was waiting
        self.assertEqual(2, len(calls)); self.assertNotIn(task["id"], s.waiting)
        saved = scheduler.load(self.data)[0]
        self.assertEqual("c123", saved["lastChat"]); self.assertEqual("", saved["lastError"])


class TerminalJobTests(unittest.TestCase):
    def test_output_arrives_while_it_runs_and_stop_ends_it(self):
        code = "import time\nfor i in range(50):\n    print('tick', i, flush=True); time.sleep(0.1)"
        plan = {"argv": [sys.executable, "-c", code], "exe": sys.executable, "cwd": tempfile.gettempdir(), "line": "python -c ...", "timeout": 60}
        job = commands.Job(plan)
        end = time.time() + 10
        while "tick 2" not in job.poll()["output"] and time.time() < end: time.sleep(0.05)
        first = job.poll(); self.assertFalse(first["done"]); self.assertIn("tick 0", first["output"])
        later = job.poll(first["next"]); self.assertNotIn("tick 0", later["output"])  # only what's new
        job.cancel.set()
        while not job.done and time.time() < end: time.sleep(0.05)
        self.assertTrue(job.done); self.assertIn("stopped", job.poll()["result"])

    def test_long_output_keeps_its_start_and_end(self):
        code = "print('A' * 200000)"
        plan = {"argv": [sys.executable, "-c", code], "exe": sys.executable, "cwd": tempfile.gettempdir(), "line": "x", "timeout": 60}
        out = commands.execute(plan)
        self.assertIn("bytes of output not shown", out)
        self.assertLess(len(out), commands.HEAD + commands.TAIL + 500)


if __name__ == "__main__":
    unittest.main()
