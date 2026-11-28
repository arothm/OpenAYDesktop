"""1.16: permission modes (Manual / Auto edits / Bypass), the main chat changing a subtask's mode, and messages
sent while a reply runs.

Run with:  python -m unittest discover -s tests -v
"""

import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import server  # noqa: E402
import test_phase4  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402

PY = sys.executable


def line(*args):
    return subprocess.list2cmdline([PY, *args])


class Gate:
    """A model response held back until open() (so a test can act while the step is running)."""
    def __init__(self, inner): self.inner = inner; self.opened = threading.Event()
    def open(self): self.opened.set()
    def readline(self): self.opened.wait(10); return self.inner.readline()
    def read(self, *a): self.opened.wait(10); return self.inner.read(*a)
    def close(self): self.opened.set()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class ModeTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.allowed = [line("-c", "print(42)")]
        server.load_config.return_value = dict(server.DEFAULT_CONFIG, apiKey="k", allowedCommands=self.allowed, minBalanceUsd=0)

    def test_manual_asks_even_for_allowed_commands(self):
        cid = self.chat(mode="manual")
        self.script = [call("run_command", command=self.allowed[0]), say("ok")]
        server.ENGINE.start(cid, {"text": "go"}); run = self.wait(cid, lambda r: r.approval)
        self.assertIn("Manual", run.approval["data"]["why"]); server.ENGINE.decide(cid, run.approval["id"], False); self.wait(cid)

    def test_auto_asks_for_commands_off_the_list(self):
        cid = self.chat(mode="auto")
        self.script = [call("run_command", command=line("-c", "print(7)")), say("ok")]
        server.ENGINE.start(cid, {"text": "go"}); run = self.wait(cid, lambda r: r.approval)
        self.assertIn("allowed list", run.approval["data"]["why"]); server.ENGINE.decide(cid, run.approval["id"], False); self.wait(cid)

    def test_bypass_asks_for_nothing(self):
        cid = self.chat(mode="bypass", webSearch=True)
        server.mark_untrusted(cid, "page: x", [])  # web content doesn't change Bypass
        (self.shared / "a.txt").write_text("a", encoding="utf-8")
        self.script = [call("run_command", command=line("-c", "print(7)")),
                       call("write_file", path=str(self.shared / "b.txt"), content="b"),
                       call("move_path", source=str(self.shared / "a.txt"), destination=str(self.shared / "c.txt")),
                       call("delete_path", path=str(self.shared / "c.txt")),
                       call("save_memory", note="likes tea", scope="global"), say("done")]
        with mock.patch.object(server, "data_root", return_value=str(Path(self.temp.name) / "data")):
            chat = self.run_to_end(cid)  # fails if anything asks
        self.assertEqual("done", chat["messages"][-1]["content"])
        self.assertIn("7", chat["messages"][2]["content"]); self.assertTrue((self.shared / "b.txt").exists()); self.assertFalse((self.shared / "c.txt").exists())

    @unittest.skipUnless(shutil.which("git"), "Git isn't installed")
    def test_bypass_commits_without_asking(self):
        repo = self.shared / "repo"; repo.mkdir()
        for args in (["init", "-q"], ["config", "user.name", "T"], ["config", "user.email", "t@example.invalid"], ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        (repo / "f.txt").write_text("x", encoding="utf-8")
        c = server.create_chat({"model": "m1", "folders": [{"path": str(repo), "write": True}], "capabilities": {"mode": "bypass"}})
        self.script = [call("git_commit", repo=str(repo), message="Add f", all=True), say("done")]
        self.run_to_end(c["id"])
        log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        self.assertIn("Add f", log)

    def test_a_new_chat_can_start_in_a_chosen_mode(self):
        c = server.create_chat({"model": "m1", "capabilities": {"mode": "auto"}})
        self.assertEqual("auto", c["capabilities"]["mode"])
        with self.assertRaises(ValueError): server.create_chat({"model": "m1", "capabilities": {"mode": "root"}})


class SubtaskModeTests(test_phase4.SubtaskTestCase):
    def test_the_main_chat_sets_a_subtasks_mode_up_to_its_own(self):
        cid = self.parent(mode="auto")
        self.script = [call("start_subtask", task="tidy", title="Helper"),
                       call("set_subtask_mode", subtask="Helper", mode="manual"),
                       call("set_subtask_mode", subtask="Helper", mode="bypass"), say("done")]
        chat = self.run_to_end(cid)
        steps = [m for m in chat["messages"] if m.get("tool_name") == "set_subtask_mode"]
        self.assertIn("Manual approve", steps[0]["content"]); self.assertTrue(steps[1].get("isError"))
        self.assertIn("more permissions than this chat", steps[1]["content"])
        child = next(c for c in server.list_chats() if c.get("parentId") == cid)
        self.assertEqual("manual", server.load_chat(child["id"])["capabilities"]["mode"])
        names = {s["function"]["name"] for s in server.tool_specs(server.saved_tool_config(child["id"]))[0]}
        self.assertNotIn("set_subtask_mode", names)  # subtasks have no subtasks


class SteerTests(RunTestCase):
    def test_a_message_sent_during_a_step_is_read_before_the_next_one(self):
        cid = self.chat()
        gate = Gate(call("list_directory", path=str(self.shared)))
        self.script = [gate, say("Checked both.")]
        server.ENGINE.start(cid, {"text": "look around"})
        self.wait(cid, lambda r: r.response is not None or len(self.requests) >= 1)
        self.assertTrue(server.ENGINE.steer(cid, "Also check the tests folder."))
        gate.open(); self.wait(cid)
        chat = server.load_chat(cid)
        sent = [m for m in chat["messages"] if m.get("sentWhileRunning")]
        self.assertEqual(["Also check the tests folder."], [m["content"] for m in sent])
        self.assertIn("Also check the tests folder.", [m.get("content") for m in self.requests[1]["messages"]])
        self.assertEqual("Checked both.", chat["messages"][-1]["content"])

    def test_a_message_sent_during_the_answer_gets_one_more_step(self):
        cid = self.chat()
        gate = Gate(say("First answer."))
        self.script = [gate, say("Answered the follow-up too.")]
        server.ENGINE.start(cid, {"text": "hi"})
        self.wait(cid, lambda r: len(self.requests) >= 1)
        server.ENGINE.steer(cid, "And one more thing.")
        gate.open(); self.wait(cid)
        self.assertEqual(2, len(self.requests)); self.assertEqual("Answered the follow-up too.", server.load_chat(cid)["messages"][-1]["content"])

    def test_no_running_reply_means_not_queued(self):
        cid = self.chat()
        self.assertFalse(server.ENGINE.steer(cid, "hello"))
        with self.assertRaises(Exception): server.ENGINE.steer(cid, "   ")


class SteerWhileWaitingTests(test_phase4.SubtaskTestCase):
    def test_a_message_wakes_a_reply_waiting_for_its_subtasks(self):
        release = threading.Event()

        def slow():
            release.wait(10); return say("helper done")
        self.child_script = {"slow job": slow}
        cid = self.parent()
        self.script = [call("start_subtask", task="slow job", title="Slow"), say("OK, pausing as you asked.")]
        try:
            server.ENGINE.start(cid, {"text": "go"})
            end = time.time() + 5
            while not any(c.get("parentId") == cid for c in server.list_chats()) and time.time() < end: time.sleep(0.05)
            time.sleep(0.3)  # the parent is now waiting for its subtask
            self.assertTrue(server.ENGINE.steer(cid, "Pause for now."))
            end = time.time() + 5
            seen = lambda: any("Pause for now." in str(m.get("content")) for r in self.requests for m in r.get("messages", []))  # noqa: E731
            while not seen() and time.time() < end: time.sleep(0.05)
            self.assertTrue(seen(), "the parent didn't read the message while its subtask was still running")
        finally:
            release.set(); server.ENGINE.stop(cid, wait=5)


if __name__ == "__main__":
    unittest.main()
