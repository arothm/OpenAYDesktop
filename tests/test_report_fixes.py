"""1.17.1: fixes from the first diagnostics report: a cap on tool calls per step, lighter history (old write
contents and superseded reads), a Large context budget, short whole-repository diffs, and tokens per second.

Run with:  python -m unittest discover -s tests -v
"""

import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_budget  # noqa: E402
import diagnostics  # noqa: E402
import runs  # noqa: E402
import server  # noqa: E402
from test_phase4 import stream_calls  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402


def write_step(path, text):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": {"path": path, "content": text}}}]}


class LightenTests(unittest.TestCase):
    def test_old_write_contents_become_a_note_and_recent_ones_stay(self):
        big = "x" * 5_000
        history = [{"role": "user", "content": "go"}]
        for n in range(5):
            history += [write_step("f%d.txt" % n, big), {"role": "tool", "tool_name": "write_file", "content": "Wrote f%d.txt" % n}]
        out = runs.lighten(json.loads(json.dumps(history)))
        args = [m["tool_calls"][0]["function"]["arguments"]["content"] for m in out if m.get("tool_calls")]
        self.assertTrue(all("5000 characters" in a for a in args[:2])); self.assertEqual([big] * 3, args[2:])
        self.assertEqual(big, history[1]["tool_calls"][0]["function"]["arguments"]["content"])  # the chat itself keeps everything

    def test_small_edits_are_kept(self):
        step = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "a", "old_text": "x", "new_text": "y"}}}]}
        history = [json.loads(json.dumps(step)) for _ in range(5)]
        self.assertEqual(history, runs.lighten(json.loads(json.dumps(history))))


class ToolCapTests(RunTestCase):
    def test_a_runaway_step_runs_only_the_first_calls(self):
        cid = self.chat()
        many = stream_calls(*[("list_directory", {"path": str(self.shared)})] * 40)
        self.script = [many, say("done")]
        chat = self.run_to_end(cid)
        self.assertEqual(runs.MAX_TOOL_CALLS_PER_STEP, sum(1 for m in chat["messages"] if m.get("role") == "tool"))
        step = next(m for m in chat["messages"] if m.get("tool_calls"))
        self.assertEqual(runs.MAX_TOOL_CALLS_PER_STEP, len(step["tool_calls"]))
        note = next(m for m in chat["messages"] if m.get("auto") and "skipped" in m["content"])
        self.assertIn("15 were skipped", note["content"])
        self.assertIn("15 were skipped", json.dumps(self.requests[-1]["messages"]))  # the model is told
        cap = next(e for e in diagnostics.events(server.data_root()) if e["kind"] == "tool_cap")
        self.assertEqual((40, 25), (cap["requested"], cap["ran"]))


class StaleReadTests(RunTestCase):
    def test_a_read_replaced_by_a_later_change_is_left_out_of_requests(self):
        cid = self.chat(mode="auto")
        f = self.shared / "notes.txt"; f.write_text("OLD-CONTENT " * 200, encoding="utf-8")
        self.script = [call("read_file", path=str(f)), call("write_file", path=str(f), content="new"),
                       call("read_file", path=str(f)), say("done")]
        with mock.patch.object(runs, "STALE_READ_BATCH", 1):
            chat = self.run_to_end(cid)
        sent = json.dumps(self.requests[-1]["messages"])
        self.assertNotIn("OLD-CONTENT", sent); self.assertIn("An older read of", sent)
        first = next(m for m in chat["messages"] if m.get("tool_name") == "read_file")
        self.assertTrue(first.get("stale")); self.assertIn("OLD-CONTENT", first["content"])  # the chat keeps it

    def test_small_amounts_wait_for_a_batch(self):
        cid = self.chat(mode="auto")
        f = self.shared / "n.txt"; f.write_text("OLD-CONTENT", encoding="utf-8")
        self.script = [call("read_file", path=str(f)), call("read_file", path=str(f)), say("done")]
        self.run_to_end(cid)
        self.assertIn("OLD-CONTENT", json.dumps(self.requests[-1]["messages"]))  # under STALE_READ_BATCH: unchanged


class BudgetTests(unittest.TestCase):
    def test_large_caps_a_million_token_window_near_200k(self):
        budget, ctx = context_budget.budget_for(1_048_576, "large")
        self.assertEqual(1_048_576, ctx); self.assertTrue(150_000 < budget <= 200_000)
        self.assertGreater(context_budget.budget_for(1_048_576, "full")[0], 900_000)


@unittest.skipUnless(shutil.which("git"), "Git isn't installed")
class DiffSummaryTests(RunTestCase):
    def test_a_big_diff_comes_back_as_a_summary_and_one_path_in_full(self):
        repo = self.shared / "repo"; repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)  # noqa: E731
        run("init", "-q"); run("config", "user.name", "T"); run("config", "user.email", "t@example.invalid"); run("config", "commit.gpgsign", "false")
        for n in range(3):
            (repo / ("f%d.txt" % n)).write_text("line\n" * 10, encoding="utf-8")
        run("add", "-A"); run("commit", "-qm", "base")
        for n in range(3):
            (repo / ("f%d.txt" % n)).write_text("".join("changed line %d\n" % i for i in range(400)), encoding="utf-8")
        c = server.create_chat({"model": "m1", "folders": [{"path": str(repo), "write": True}]})
        chat = server.saved_tool_config(c["id"])
        whole = server.run_tool(chat, "git_diff", {"repo": str(repo)})
        self.assertIn("Call git_diff with path", whole); self.assertNotIn("+changed line 399", whole); self.assertIn("f1.txt", whole)
        one = server.run_tool(chat, "git_diff", {"repo": str(repo), "path": "f1.txt"})
        self.assertIn("+changed line 399", one)


class SpeedTests(RunTestCase):
    def test_steps_without_timings_still_get_a_generation_time(self):
        cid = self.chat()
        slow = io.BytesIO((json.dumps({"message": {"content": "hi"}}) + "\n" + json.dumps({"done": True, "prompt_eval_count": 5, "eval_count": 30}) + "\n").encode())
        real = slow.readline
        def readline():  # a little time passes between the first token and the end
            line = real()
            if line.startswith(b'{"done"'):
                import time; time.sleep(0.05)
            return line
        slow.readline = readline
        self.script = [slow]; self.run_to_end(cid)
        step = next(e for e in diagnostics.events(server.data_root()) if e["kind"] == "step")
        self.assertGreater(step["evalMs"], 0)


if __name__ == "__main__":
    unittest.main()
