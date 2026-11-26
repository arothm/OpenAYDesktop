"""Allowed-commands tests: matching, refusals, approvals, environment, time limits and Stop.

Commands run for real (the current Python interpreter is the test program).
Run with:  python -m unittest discover -s tests -v
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import commands  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402

PY = sys.executable


def line(*args):
    import subprocess
    return subprocess.list2cmdline([PY, *args])


class MatchingTests(unittest.TestCase):
    def test_exact_and_wildcard(self):
        entries = ["npm test", "python -m pytest *"]
        self.assertEqual("npm test", commands.allowed_by(commands.split("npm test"), entries))
        self.assertEqual("npm test", commands.allowed_by(commands.split("npm.cmd test"), entries))
        self.assertIsNone(commands.allowed_by(commands.split("npm test --watch"), entries))
        self.assertEqual("python -m pytest *", commands.allowed_by(commands.split("python -m pytest -k fast"), entries))
        self.assertIsNone(commands.allowed_by(commands.split("python -m pip install x"), entries))
        self.assertIsNone(commands.allowed_by(commands.split("npm"), entries))

    def test_refusals(self):
        for bad in ("a && b", "a | b", "a; b", "a > f", "a < f", "a >> f", "a 2>&1", "a\nb", "", "   "):
            with self.assertRaises(commands.CommandError): commands.split(bad)
        self.assertEqual(["python", "-c", "import os; print(1)"], commands.split('python -c "import os; print(1)"'))  # ; inside an argument is fine
        with self.assertRaises(commands.CommandError): commands.resolve_program(["definitely-not-a-real-program-xyz"])
        self.assertEqual(["npm test", "python -m pytest *"], commands.clean_allowed(["npm  test", "", "python -m pytest *", "npm test"]))
        with self.assertRaises(commands.CommandError): commands.clean_allowed(["a && b"])

    @unittest.skipUnless(os.name == "nt", "Windows batch-file rule")
    def test_batch_files_refuse_cmd_special_characters(self):
        bat = Path(os.environ.get("TEMP", ".")) / "openay_test_tool.cmd"; bat.write_text("@echo %*\n", encoding="utf-8")
        try:
            with mock.patch.object(commands.shutil, "which", return_value=str(bat)):
                commands.resolve_program(["tool", "safe-arg"])
                for bad in ("a&calc", "x|y", "%PATH%", "q\"r"):
                    with self.assertRaises(commands.CommandError): commands.resolve_program(["tool", bad])
        finally:
            bat.unlink()


class ExecuteTests(RunTestCase):
    def chat_with(self, **caps):
        return self.chat(commands=True, **caps)

    def plan(self, cid, cmd, **extra):
        return commands.prepare(server.saved_tool_config(cid), dict(command=cmd, **extra), [])

    def test_runs_in_folder_without_the_api_key(self):
        cid = self.chat_with()
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "secret-key"}):
            out = commands.execute(self.plan(cid, line("-c", "import os; print(os.getcwd()); print(os.environ.get('OLLAMA_API_KEY'))")))
        self.assertIn("exit code 0", out); self.assertIn(os.path.realpath(self.shared), out)
        self.assertNotIn("secret-key", out); self.assertIn("None", out)
        out = commands.execute(self.plan(cid, line("-c", "import sys; print('bad'); sys.exit(3)")))
        self.assertIn("exit code 3", out); self.assertIn("bad", out)

    def test_folder_rules_and_permission(self):
        cid = self.chat_with()
        with self.assertRaises(commands.CommandError): self.plan(cid, line("-V"), cwd=str(self.outside))
        ro_dir = self.shared.parent / "ro"; ro_dir.mkdir()
        server.update_permissions(cid, {"folders": [{"path": str(self.shared), "write": True}, {"path": str(ro_dir), "write": False}]})
        with self.assertRaises(commands.CommandError): self.plan(cid, line("-V"), cwd=str(ro_dir))
        with self.assertRaises(commands.CommandError): self.plan(self.chat(), line("-V"))  # commands off
        ro = self.chat(write=False, commands=True)  # no writable folder: the permission can't be on
        self.assertFalse(capabilities.policy(server.saved_tool_config(ro))["commands"])

    def test_time_limit_and_stop_end_the_process(self):
        cid = self.chat_with()
        t = time.time(); out = commands.execute(self.plan(cid, line("-c", "import time; time.sleep(60)"), timeout_seconds=5))
        self.assertIn("timed out after 5 seconds", out); self.assertLess(time.time() - t, 20)
        import threading
        stop = threading.Event(); threading.Timer(1.0, stop.set).start()
        t = time.time(); out = commands.execute(self.plan(cid, line("-c", "import time; time.sleep(60)")), stop)
        self.assertIn("stopped", out); self.assertLess(time.time() - t, 15)

    def test_long_output_keeps_start_and_end(self):
        cid = self.chat_with()
        out = commands.execute(self.plan(cid, line("-c", "print('FIRST'); print('x' * 200000); print('LAST')")))
        self.assertIn("FIRST", out); self.assertIn("LAST", out); self.assertIn("not shown", out)


class RunCommandToolTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.allowed = [line("-c", "print(42)")]
        cfg = dict(server.DEFAULT_CONFIG, apiKey="k", allowedCommands=self.allowed, minBalanceUsd=0)
        self.cfg_patch = mock.patch.object(server, "load_config", return_value=cfg); self.cfg_patch.start()

    def tearDown(self):
        self.cfg_patch.stop(); super().tearDown()

    def test_allowed_command_runs_without_asking(self):
        cid = self.chat(commands=True); self.script = [call("run_command", command=self.allowed[0]), say("it printed 42")]
        chat = self.run_to_end(cid)  # fails if an approval is requested
        self.assertIn("42", chat["messages"][2]["content"])
        spec = next(s for s in server.tool_specs(server.saved_tool_config(cid))[0] if s["function"]["name"] == "run_command")
        self.assertIn("print(42)", spec["function"]["description"])

    def test_other_commands_need_approval(self):
        cid = self.chat(commands=True); marker = self.shared / "made.txt"
        cmd = line("-c", "open('made.txt', 'w').write('x')")
        self.script = [call("run_command", command=cmd), say("ok")]
        chat = self.run_to_end(cid, approve=False)
        self.assertFalse(marker.exists()); self.assertIn("didn't allow", chat["messages"][2]["content"])
        self.script = [call("run_command", command=cmd), say("ok")]
        self.run_to_end(cid, approve=True); self.assertTrue(marker.exists())

    def test_untrusted_chat_asks_even_for_allowed_commands(self):
        cid = self.chat(commands=True); server.mark_untrusted(cid, "page: x", [])
        self.script = [call("run_command", command=self.allowed[0]), say("ok")]
        server.ENGINE.start(cid, {"text": "go"}); run = self.wait(cid, lambda r: r.approval)
        self.assertIn("web content", run.approval["data"]["why"]); server.ENGINE.decide(cid, run.approval["id"], False); self.wait(cid)

    def test_not_offered_when_off_or_in_subtasks(self):
        names = lambda c: {s["function"]["name"] for s in server.tool_specs(c)[0]}  # noqa: E731
        self.assertNotIn("run_command", names(server.saved_tool_config(self.chat())))
        parent = server.saved_tool_config(self.chat(commands=True, subtasks=True))
        self.assertIn("run_command", names(parent))
        self.assertFalse(capabilities.child_capabilities(parent)["commands"])


if __name__ == "__main__":
    unittest.main()
