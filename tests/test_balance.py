"""Balance guard tests: reading the Ollama balance, wrap-up, pause and resume.

Ollama (chat and balance) is replaced by fakes; everything runs in temporary folders.
Run with:  python -m unittest discover -s tests -v
"""

import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runs  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402


class BalanceReadTests(unittest.TestCase):
    def setUp(self):
        server._balance.update(at=0.0, info=None)

    def tearDown(self):
        server._balance.update(at=0.0, info=None)

    def fake(self, payload, calls):
        def up(path, body=None, timeout=600):
            calls.append(path)
            if isinstance(payload, Exception): raise payload
            return io.BytesIO(json.dumps(payload).encode())
        return up

    def test_parses_and_caches(self):
        calls = []
        payload = {"included": {"balance_usd": 2, "allowance_usd": 20, "period": {"until": "2026-12-26T00:00:00Z"}},
                   "purchased": {"balance_usd": 1.41983}}
        with mock.patch.object(server, "upstream", side_effect=self.fake(payload, calls)), \
             mock.patch.object(server, "load_config", return_value={"apiKey": "k", "minBalanceUsd": 1.0}):
            info = server.balance()
            self.assertEqual((3.4198, 2.0, 1.4198, "2026-12-26T00:00:00Z"), (info["totalUsd"], info["includedUsd"], info["purchasedUsd"], info["periodUntil"]))
            server.balance(); self.assertEqual(["/api/balance"], calls)  # cached
            server.balance(force=True); self.assertEqual(2, len(calls))
            self.assertEqual(False, server.balance_low()[0])

    def test_unreadable_balance_never_blocks(self):
        calls = []
        with mock.patch.object(server, "upstream", side_effect=self.fake(OSError("offline"), calls)), \
             mock.patch.object(server, "load_config", return_value={"apiKey": "k", "minBalanceUsd": 1.0}):
            low, info, minimum = server.balance_low()
        self.assertFalse(low); self.assertIn("offline", info["error"]); self.assertEqual(1.0, minimum)

    def test_threshold_and_off(self):
        payload = {"included": {"balance_usd": 0}, "purchased": {"balance_usd": 1.0}}
        with mock.patch.object(server, "upstream", side_effect=self.fake(payload, [])):
            with mock.patch.object(server, "load_config", return_value={"apiKey": "k", "minBalanceUsd": 1.0}):
                self.assertTrue(server.balance_low()[0])  # at the minimum counts as low
            with mock.patch.object(server, "load_config", return_value={"apiKey": "k", "minBalanceUsd": 0}):
                self.assertEqual((False, None, 0.0), server.balance_low())


class BalanceGuardTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.low = False
        self.guard = mock.patch.object(server, "balance_low", side_effect=lambda max_age=30, force=False:
                                       (self.low, {"totalUsd": 0.8 if self.low else 5.0}, 1.0))
        self.guard.start()

    def tearDown(self):
        self.guard.stop(); super().tearDown()

    def wait_status(self, cid, status):
        return self.wait(cid, lambda r: r.status == status or not r.alive)

    def test_low_balance_before_any_work_pauses_without_spending(self):
        cid = self.chat(); self.low = True; self.script = [say("hello")]
        server.ENGINE.start(cid, {"text": "hi"})
        run = self.wait_status(cid, "paused")
        self.assertEqual("paused", run.status); self.assertEqual([], self.requests)  # nothing was sent
        self.assertEqual(0.8, server.ENGINE.snapshot(cid)[0]["pause"]["balance"]["totalUsd"])
        self.low = False; server.ENGINE.balance_changed(); self.wait(cid)
        chat = server.load_chat(cid)
        self.assertEqual("hello", chat["messages"][-1]["content"])
        self.assertFalse(any(m.get("auto") for m in chat["messages"]))  # no work to resume, so no note

    def test_running_reply_wraps_up_pauses_and_resumes(self):
        (self.shared / "a.txt").write_text("x", encoding="utf-8"); cid = self.chat()
        def first():
            self.low = True  # the balance drops while the first step runs
            return call("read_file", path=str(self.shared / "a.txt"))
        queue = [first, lambda: say("Summary: read a.txt; next, edit it."), lambda: say("All done.")]
        def up(path, body=None, timeout=600):
            self.requests.append(json.loads(json.dumps(body))); return queue.pop(0)()
        with mock.patch.object(server, "upstream", side_effect=up):
            server.ENGINE.start(cid, {"text": "work"})
            run = self.wait_status(cid, "paused")
            self.assertEqual(2, len(self.requests))
            wrap = self.requests[1]
            self.assertNotIn("tools", wrap); self.assertIn("balance is nearly used up", wrap["messages"][-1]["content"])  # last, so the cached start stays the same
            saved = server.load_chat(cid)
            self.assertTrue(saved["messages"][-1].get("wrapUp")); self.assertIn("Summary", saved["messages"][-1]["content"])
            self.low = False; server.ENGINE.balance_changed(); self.wait(cid)
        chat = server.load_chat(cid)
        self.assertTrue(chat["messages"][-2].get("auto")); self.assertIn("Continue the work", chat["messages"][-2]["content"])
        self.assertEqual("All done.", chat["messages"][-1]["content"]); self.assertEqual("done", run.outcome)

    def test_stop_while_paused(self):
        cid = self.chat(); self.low = True
        server.ENGINE.start(cid, {"text": "hi"}); self.wait_status(cid, "paused")
        t = time.time(); server.ENGINE.stop(cid); run = self.wait(cid)
        self.assertLess(time.time() - t, 3); self.assertEqual("stopped", run.outcome)
        self.assertEqual({}, server.ENGINE.statuses())

    def test_every_running_reply_pauses(self):
        a, b = self.chat(), self.chat(); self.low = True
        server.ENGINE.start(a, {"text": "1"}); server.ENGINE.start(b, {"text": "2"})
        self.wait_status(a, "paused"); self.wait_status(b, "paused")
        self.assertEqual({a: "paused", b: "paused"}, server.ENGINE.statuses())
        self.script = [say("one"), say("two")]; self.low = False; server.ENGINE.balance_changed()
        self.wait(a); self.wait(b); self.assertEqual({}, server.ENGINE.statuses())


if __name__ == "__main__":
    unittest.main()
