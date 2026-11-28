"""1.16: Settings > Session defaults (what new chats start with) and the step limit per reply.

Run with:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import projects  # noqa: E402
import server  # noqa: E402
import skills  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402


class SessionDefaultsTests(RunTestCase):
    def use_defaults(self, value):
        server.load_config.return_value = {"apiKey": "k", "sessionDefaults": value}

    def test_new_chats_start_with_the_defaults(self):
        self.use_defaults({"capabilities": {"webSearch": True, "mode": "bypass"}})
        c = server.load_chat(self.chat())
        self.assertEqual({"mode": "bypass", "webSearch": True}, c["capabilities"])

    def test_without_defaults_nothing_changes(self):
        c = server.load_chat(self.chat())
        self.assertEqual(capabilities.DEFAULT_POLICY, {k: c["capabilities"][k] for k in capabilities.DEFAULT_POLICY})

    def test_a_chat_can_still_be_changed_on_its_own(self):
        self.use_defaults({"capabilities": {"webSearch": True}})
        cid = self.chat(); other = self.chat()
        server.update_permissions(cid, {"capabilities": {"webSearch": False}})
        self.assertFalse(capabilities.policy(server.load_chat(cid))["webSearch"])
        self.assertTrue(capabilities.policy(server.load_chat(other))["webSearch"])

    def test_bad_saved_values_are_ignored_one_by_one(self):
        self.use_defaults({"capabilities": {"mode": "everything", "webSearch": True}, "skills": ["gone"]})
        d = server.session_defaults()
        self.assertEqual({"mode": "manual", "webSearch": True}, d["capabilities"]); self.assertEqual([], d["skills"])

    def test_default_skills_are_turned_on(self):
        skills.save(server.skills_root(), "Tone", "Short answers.", "Be brief.")
        sid = skills.list_all(server.skills_root())[0]["id"]
        self.use_defaults({"skills": [sid]})
        self.assertEqual([sid], server.load_chat(self.chat())["skills"])

    def test_a_projects_permissions_win(self):
        self.use_defaults({"capabilities": {"webSearch": True, "mode": "bypass"}})
        project = projects.save_project(server.data_root(), {"name": "P", "capabilities": {"mode": "auto", "webSearch": False}},
                                        capabilities.clean_folders, capabilities.clean_capabilities, lambda ids: ids)
        c = server.create_chat({"model": "m1", "projectId": project["id"]})
        self.assertEqual({"mode": "auto", "webSearch": True}, c["capabilities"])  # the project sets the mode; web search follows the defaults

    def test_subtasks_still_get_at_most_the_parents_permissions(self):
        self.use_defaults({"capabilities": {"webSearch": True, "mode": "auto"}})
        kid = server.load_chat(server.create_subtask_chat(self.chat(), "Helper"))
        p = capabilities.policy(kid)
        self.assertEqual(("auto", True, "read", False, False), (p["mode"], p["webSearch"], p["git"], p["commands"], p["subtasks"]))

    def test_saving_rejects_unknown_values(self):
        with self.assertRaises(ValueError): server.clean_session_defaults({"capabilities": {"mode": "everything"}})
        with self.assertRaises(ValueError): server.clean_session_defaults({"skills": ["no-such-skill"]})
        saved = server.clean_session_defaults({"capabilities": {"webSearch": True}})
        self.assertEqual({"mode": "manual", "webSearch": True}, saved["capabilities"])


class StepLimitTests(RunTestCase):
    def test_a_reply_pauses_at_the_step_limit_with_continue(self):
        server.load_config.return_value = {"apiKey": "k", "maxSteps": 3}
        cid = self.chat()
        self.script = [call("list_directory", path=str(self.shared)) for _ in range(5)]
        chat = self.run_to_end(cid)
        last = chat["messages"][-1]
        self.assertTrue(last.get("stepLimit")); self.assertIn("3 steps", last["error"]); self.assertEqual(3, len(self.requests))

    def test_zero_means_no_limit(self):
        server.load_config.return_value = {"apiKey": "k", "maxSteps": 0}
        cid = self.chat()
        self.script = [call("list_directory", path=str(self.shared)) for _ in range(30)] + [say("done")]
        chat = self.run_to_end(cid)
        self.assertEqual("done", chat["messages"][-1]["content"]); self.assertEqual(31, len(self.requests))

    def test_the_default_is_100(self):
        self.assertEqual(100, server.DEFAULT_CONFIG["maxSteps"])
        with mock.patch.object(server, "load_config", return_value={"apiKey": "k"}):
            cid = self.chat()
            self.script = [call("list_directory", path=str(self.shared)) for _ in range(30)] + [say("done")]
            self.assertEqual("done", self.run_to_end(cid)["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
