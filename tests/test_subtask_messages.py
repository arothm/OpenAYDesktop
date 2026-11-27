"""Messages between a parent chat and its subtasks: message_parent, message_subtask and wait_subtasks.

Run with:  python -m unittest discover -s tests -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runs  # noqa: E402
import server  # noqa: E402
import test_phase4  # noqa: E402
from test_runs import call, say  # noqa: E402


def seq(*items):
    it = iter(items)
    return lambda: next(it, say("(child script ended)"))


def tools(chat, name=None):
    return [m for m in chat["messages"] if m.get("role") == "tool" and (name is None or m.get("tool_name") == name)]


def kid(parent_id):
    return next(c for c in server.list_chats() if c.get("parentId") == parent_id)


class SubtaskMessageTests(test_phase4.SubtaskTestCase):
    def test_question_and_answer(self):
        cid = self.parent()
        self.child_script = {"paint the fence": seq(call("message_parent", message="Which colour?", wait_for_reply=True),
                                                    say("Painted it."))}
        self.script = [call("start_subtask", task="paint the fence", title="Painter"),
                       call("message_subtask", subtask="Painter", message="Blue, please."),
                       call("wait_subtasks"), say("All painted.")]
        chat = self.run_to_end(cid)
        start, = tools(chat, "start_subtask")
        self.assertIn("Question from subtask \"Painter\"", start["content"]); self.assertIn("Which colour?", start["content"])
        self.assertIn("waiting for your answer", start["content"])
        self.assertIn("Answered the question", tools(chat, "message_subtask")[0]["content"])
        waited = tools(chat, "wait_subtasks")[0]["content"]
        self.assertIn("finished. Its answer", waited); self.assertIn("Painted it.", waited)
        child = server.load_chat(kid(cid)["id"])
        asked, = tools(child, "message_parent")
        self.assertIn("Blue, please.", asked["content"]); self.assertNotIn("_waitingParent", asked)
        child_tools = {t["function"]["name"] for r in self.requests if "You are a subtask helper" in r["messages"][0]["content"]
                       for t in r.get("tools") or []}
        self.assertIn("message_parent", child_tools); self.assertNotIn("message_subtask", child_tools)

    def test_progress_note_arrives_with_the_result(self):
        cid = self.parent()
        self.child_script = {"count": seq(call("message_parent", message="Halfway there."), say("Counted 42."))}
        self.script = [call("start_subtask", task="count", title="Counter"), say("ok")]
        chat = self.run_to_end(cid)
        content = tools(chat, "start_subtask")[0]["content"]
        self.assertIn("Message from subtask \"Counter\"", content); self.assertIn("Halfway there.", content)
        self.assertIn("Counted 42.", content)
        self.assertIn("Sent to the parent chat", tools(server.load_chat(kid(cid)["id"]), "message_parent")[0]["content"])

    def test_follow_up_continues_a_finished_subtask(self):
        cid = self.parent()
        self.child_script = {"draft a title": seq(say("Title: Silver Lines"), say("Subtitle: Handmade in Cairo"))}
        self.script = [call("start_subtask", task="draft a title", title="Writer"),
                       call("message_subtask", subtask="Writer", message="Now a subtitle."),
                       call("wait_subtasks", subtasks=["Writer"]), say("done")]
        chat = self.run_to_end(cid)
        self.assertIn("continues with your message", tools(chat, "message_subtask")[0]["content"])
        self.assertIn("Subtitle: Handmade in Cairo", tools(chat, "wait_subtasks")[0]["content"])
        child = server.load_chat(kid(cid)["id"])
        follow = [m for m in child["messages"] if m.get("fromParent")]
        self.assertEqual(1, len(follow)); self.assertTrue(follow[0]["auto"]); self.assertIn("Now a subtitle.", follow[0]["content"])

    def test_message_to_a_running_subtask_is_read_before_its_next_step(self):
        cid = self.parent()
        seen = []

        def second():  # the child's second request: the parent's message must be in it
            seen.append(self.requests[-1]["messages"])
            return say("Used the new limit.")
        steps = iter([call("message_parent", message="Starting.", wait_for_reply=True), call("list_directory", path=str(self.shared))])

        def child():
            item = next(steps, None)
            return item if item is not None else second()
        self.child_script = {"tidy up": child}
        self.script = [call("start_subtask", task="tidy up", title="Tidy"),
                       call("message_subtask", subtask="Tidy", message="Go on."),  # answers the question
                       call("message_subtask", subtask="Tidy", message="Limit: 3 files."),  # read before its next step
                       call("wait_subtasks"), say("ok")]
        self.run_to_end(cid)
        child = server.load_chat(kid(cid)["id"])
        self.assertTrue(any(m.get("fromParent") and "Limit: 3 files." in m["content"] for m in child["messages"]))

    def test_unread_subtasks_get_a_reminder_then_are_stopped(self):
        cid = self.parent()
        self.child_script = {"wait for me": seq(call("message_parent", message="Need input", wait_for_reply=True), say("never"))}
        self.script = [call("start_subtask", task="wait for me", title="Waiter"), say("Done."), say("Done again."), say("Final.")]
        chat = self.run_to_end(cid)
        reminders = [m for m in chat["messages"] if m.get("role") == "user" and m.get("auto")]
        self.assertEqual(runs.SUBTASK_REMINDERS, len(reminders)); self.assertIn("wait_subtasks", reminders[0]["content"])
        self.assertEqual("Final.", chat["messages"][-1]["content"])
        child_id = kid(cid)["id"]
        self.wait(child_id)
        self.assertEqual("stopped", server.ENGINE.runs[child_id].outcome)

    def test_bad_names_and_limits(self):
        cid = self.parent()
        self.script = [call("message_subtask", subtask="Nobody", message="hi"),
                       call("message_subtask", subtask="", message="hi"), call("wait_subtasks"), say("ok")]
        chat = self.run_to_end(cid)
        a, b, w = tools(chat)
        self.assertTrue(a["isError"]); self.assertIn("No subtask", a["content"]); self.assertTrue(b["isError"])
        self.assertIn("None of your subtasks", w["content"])
        child = server.create_subtask_chat(cid, "Solo")  # a subtask chat the user talks to directly has no parent reply
        self.script = [say("hello")]
        self.run_to_end(child)
        self.assertNotIn("message_parent", {t["function"]["name"] for t in self.requests[-1].get("tools") or []})

    def test_messages_from_a_subtask_that_read_the_web_make_the_parent_untrusted(self):
        cid = self.parent(webSearch=True)
        self.child_script = {"look": seq(call("message_parent", message="Found a page"), say("done"))}
        self.script = [call("start_subtask", task="look", title="Looker"), say("ok")]
        server.ENGINE.start(cid, {"text": "go"})
        self.wait(cid, lambda r: any(c.get("parentId") == cid for c in server.list_chats()))
        server.mark_untrusted(kid(cid)["id"], "search: q", ["https://r.example"])
        self.wait(cid)
        self.assertIn("subtask: Looker", server.load_chat(cid)["untrusted"]["sources"])
