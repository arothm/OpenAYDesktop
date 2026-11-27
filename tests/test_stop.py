"""Stop always ends a reply, even when a step is stuck and can't be interrupted.

Run with:  python -m unittest discover -s tests -v
"""

import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gittools  # noqa: E402
import runs  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402


class StopTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        real = server.run_tool

        def stuck_run_tool(cfg, name, args):  # a read that never returns until the test lets it
            if name == "read_file":
                self.release.wait(30)
                return "LATE RESULT"
            return real(cfg, name, args)
        self.grace = mock.patch.object(runs, "STOP_GRACE", 0.5); self.grace.start()
        self.tool = mock.patch.object(server, "run_tool", side_effect=stuck_run_tool); self.tool.start()

    def tearDown(self):
        self.release.set(); self.tool.stop(); self.grace.stop(); super().tearDown()

    def test_stop_ends_a_reply_stuck_in_a_tool(self):
        cid = self.chat()
        (self.shared / "a.txt").write_text("x", encoding="utf-8")
        self.script = [call("read_file", path=str(self.shared / "a.txt")), say("should not be reached")]
        server.ENGINE.start(cid, {"text": "go"})
        run = self.wait(cid, lambda r: any(m.get("_running") for m in r.chat["messages"]))
        started = time.time()
        self.assertTrue(server.ENGINE.stop(cid))
        self.wait(cid, lambda r: not r.alive, timeout=5)
        self.assertLess(time.time() - started, 3)
        self.assertTrue(run.thread.is_alive())  # the stuck step itself is still blocked
        self.assertFalse(server.ENGINE.busy(cid)); self.assertEqual("stopped", run.outcome)
        saved = server.load_chat(cid)
        tool = [m for m in saved["messages"] if m.get("role") == "tool"][-1]
        self.assertEqual(runs.STUCK_STEP, tool["content"]); self.assertTrue(tool["isError"]); self.assertNotIn("_running", tool)

        self.script = [say("fresh reply")]  # the chat is free again
        self.run_to_end(cid, text="again")
        self.release.set(); run.thread.join(5)  # the stuck step returns late: its result must not land anywhere
        self.assertFalse(run.thread.is_alive())
        final = server.load_chat(cid)
        self.assertNotIn("LATE RESULT", str(final["messages"]))
        self.assertEqual("fresh reply", final["messages"][-1]["content"])

    def test_normal_stop_does_not_mark_anything_stuck(self):
        cid = self.chat()
        self.release.set()
        self.script = [say("done")]
        self.run_to_end(cid)
        self.assertFalse(server.ENGINE.stop(cid))  # nothing running
        self.assertNotIn(runs.STUCK_STEP, str(server.load_chat(cid)["messages"]))


class GitStopTests(unittest.TestCase):
    def test_stop_ends_a_running_git_call(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        gittools.set_cancel(cancel)
        started = time.time()
        try:
            with self.assertRaises(gittools.Stopped):
                gittools.call([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
        finally:
            gittools.set_cancel(None)
        self.assertLess(time.time() - started, 5)

    def test_without_stop_signal_a_call_finishes(self):
        r = gittools.call([sys.executable, "-c", "print('hi')"], timeout=30)
        self.assertEqual(("hi", 0), (r.stdout.strip(), r.returncode))
        with self.assertRaises(subprocess.TimeoutExpired):
            gittools.call([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


if __name__ == "__main__":
    unittest.main()
