"""1.17: local anonymous diagnostics (diagnostics.py, Settings > Diagnostics).

Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402

SECRET = "my-secret-plan-for-the-shop"


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.data = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_only_numbers_and_safe_names_are_kept(self):
        diagnostics.record(self.data, "tool", {"name": "read_file", "ms": 12, "ok": True, "ratio": 0.5,
                                               "path": "D:\\Work\\shop\\secret.txt", "text": "hello there", "url": "https://x.example/a",
                                               "unix": "/home/me/file", "nan": float("nan"), "nested": {"a": 1}})
        e = diagnostics.events(self.data)[0]
        self.assertEqual({"name": "read_file", "ms": 12, "ok": True, "ratio": 0.5, "kind": "tool"}, {k: v for k, v in e.items() if k != "t"})

    def test_old_days_are_pruned_and_clear_removes_everything(self):
        folder = diagnostics.folder(self.data); os.makedirs(folder)
        old = (date.today() - timedelta(days=diagnostics.KEEP_DAYS + 5)).isoformat()
        Path(folder, old + ".jsonl").write_text('{"kind":"x","t":1}\n', encoding="utf-8")
        diagnostics.record(self.data, "app", {"version": "1.17.0"})
        self.assertFalse(Path(folder, old + ".jsonl").exists())
        self.assertEqual(1, diagnostics.clear(self.data)); self.assertEqual([], diagnostics.events(self.data))

    def test_summary_totals(self):
        for ms in (100, 300):
            diagnostics.record(self.data, "tool", {"name": "read_file", "ms": ms, "resultTokens": 50, "error": False, "denied": False})
        diagnostics.record(self.data, "step", {"model": "m1", "promptTokens": 1000, "cachedTokens": 800, "outputTokens": 50, "evalMs": 500,
                                               "systemTokens": 300, "toolSpecTokens": 400, "toolResultTokens": 100})
        diagnostics.record(self.data, "reply", {"model": "m1", "steps": 1, "ms": 2000, "outcome": "stepLimit"})
        s = diagnostics.summary(self.data, 7)
        self.assertEqual(2, s["tools"]["read_file"]["calls"]); self.assertEqual(100, s["tools"]["read_file"]["resultTokens"])
        self.assertEqual(0.8, s["totals"]["cachedShare"]); self.assertEqual(1, s["totals"]["stepLimitHits"])
        self.assertEqual(100.0, s["models"]["m1"]["tokensPerSecond"]); self.assertEqual(400, s["requestParts"]["toolSpecTokens"])


class ReplyDiagnosticsTests(RunTestCase):
    def files_text(self):
        folder = diagnostics.folder(server.data_root())
        return "".join(Path(folder, n).read_text(encoding="utf-8") for n in os.listdir(folder))

    def test_a_reply_records_steps_tools_and_its_outcome_without_any_content(self):
        cid = self.chat(mode="auto")
        secret_file = self.shared / (SECRET + ".txt"); secret_file.write_text(SECRET * 20, encoding="utf-8")
        self.script = [call("read_file", path=str(secret_file)), say("The plan says " + SECRET)]
        self.run_to_end(cid, text="Read my file about " + SECRET)
        evs = diagnostics.events(server.data_root())
        kinds = [e["kind"] for e in evs]
        self.assertEqual(2, kinds.count("step")); self.assertEqual(1, kinds.count("tool")); self.assertEqual(1, kinds.count("reply"))
        tool = next(e for e in evs if e["kind"] == "tool"); reply = next(e for e in evs if e["kind"] == "reply")
        self.assertEqual("read_file", tool["name"]); self.assertGreater(tool["resultTokens"], 100); self.assertFalse(tool["error"])
        self.assertEqual(("done", 2, "auto", "m1"), (reply["outcome"], reply["steps"], reply["mode"], reply["model"]))
        step = next(e for e in evs if e["kind"] == "step")
        self.assertGreater(step["toolSpecTokens"], 0); self.assertGreater(step["systemTokens"], 0)
        text = self.files_text()
        for private in (SECRET, str(self.shared), "Read my file", "The plan says"):
            self.assertNotIn(private, text)

    def test_approvals_are_recorded(self):
        cid = self.chat(mode="manual")
        self.script = [call("create_folder", path=str(self.shared / "x")), say("ok")]
        self.run_to_end(cid, approve=False)
        a = next(e for e in diagnostics.events(server.data_root()) if e["kind"] == "approval")
        self.assertEqual(("create_folder", False), (a["what"], a["allowed"]))
        tool = next(e for e in diagnostics.events(server.data_root()) if e["kind"] == "tool")
        self.assertTrue(tool["denied"]); self.assertFalse(tool["error"])

    def test_turned_off_records_nothing(self):
        server.load_config.return_value = {"apiKey": "k", "diagnostics": False}
        cid = self.chat()
        self.script = [say("hi")]; self.run_to_end(cid)
        self.assertEqual([], diagnostics.events(server.data_root()))


if __name__ == "__main__":
    unittest.main()
