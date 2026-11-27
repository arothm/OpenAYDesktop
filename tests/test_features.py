"""1.12 tests: undo a reply, plan mode, documents, search, terminal, preview, schedules, git_merge and
isolated subtasks in Git worktrees. Ollama Cloud is a scripted fake; Git runs on temporary repositories."""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import documents  # noqa: E402
import runs  # noqa: E402
import scheduler  # noqa: E402
import server  # noqa: E402
import undo  # noqa: E402
from test_git import HAVE_GIT, GitTestCase, git  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402


class UndoTests(RunTestCase):
    def test_undo_puts_back_every_file_the_reply_changed(self):
        (self.shared / "keep.txt").write_text("original\n", encoding="utf-8")
        cid = self.chat(editMode="auto", fileManage=True)
        self.script = [call("edit_file", path=str(self.shared / "keep.txt"), old_text="original", new_text="changed"),
                       call("write_file", path=str(self.shared / "new.txt"), content="brand new"),
                       call("create_folder", path=str(self.shared / "made")), say("done")]
        chat = self.run_to_end(cid)
        run_ids = {m.get("runId") for m in chat["messages"] if m.get("role") == "tool"}
        self.assertEqual(1, len(run_ids)); run_id = run_ids.pop(); self.assertTrue(run_id)
        steps = undo.plan(server.data_root(), cid, run_id)
        self.assertEqual(3, len(steps)); self.assertTrue(all(s["ok"] for s in steps))
        results = undo.undo(server.data_root(), cid, run_id, server.capabilities.trash_root(server.CHATS_DIR))
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual("original\n", (self.shared / "keep.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.shared / "new.txt").exists()); self.assertFalse((self.shared / "made").exists())
        self.assertEqual([], undo.plan(server.data_root(), cid, run_id))  # nothing left to undo
        trashed = [m["originalPath"] for m in server.capabilities.list_trash(server.capabilities.trash_root(server.CHATS_DIR))]
        self.assertIn(os.path.realpath(self.shared / "new.txt"), trashed)  # an undo can itself be undone from the Trash

    def test_files_changed_after_the_reply_are_skipped(self):
        cid = self.chat(editMode="auto")
        self.script = [call("write_file", path=str(self.shared / "a.txt"), content="from the reply"), say("ok")]
        chat = self.run_to_end(cid)
        run_id = next(m["runId"] for m in chat["messages"] if m.get("runId"))
        (self.shared / "a.txt").write_text("edited by hand", encoding="utf-8")
        results = undo.undo(server.data_root(), cid, run_id, server.capabilities.trash_root(server.CHATS_DIR))
        self.assertFalse(results[0]["ok"]); self.assertEqual("edited by hand", (self.shared / "a.txt").read_text(encoding="utf-8"))


class PlanModeTests(RunTestCase):
    def test_plan_mode_offers_only_reading_and_refuses_changes(self):
        cid = self.chat(editMode="auto", fileManage=True)
        self.script = [call("write_file", path=str(self.shared / "x.txt"), content="no"), say("1. Do this")]
        chat = self.run_to_end(cid, plan=True)
        names = {t["function"]["name"] for t in self.requests[0]["tools"]}
        self.assertIn("read_file", names); self.assertNotIn("write_file", names); self.assertNotIn("create_folder", names)
        self.assertIn("Plan mode", self.requests[0]["messages"][0]["content"])
        self.assertTrue(chat["messages"][0].get("plan")); self.assertFalse((self.shared / "x.txt").exists())
        self.assertIn("Plan mode", next(m["content"] for m in chat["messages"] if m.get("role") == "tool"))
        self.script = [call("write_file", path=str(self.shared / "x.txt"), content="yes"), say("done")]
        self.run_to_end(cid, text="Go ahead")
        self.assertTrue((self.shared / "x.txt").exists())  # the next, normal message can change files


class DocumentTests(RunTestCase):
    def office(self, files):
        b = io.BytesIO(); z = zipfile.ZipFile(b, "w")
        for n, t in files.items(): z.writestr(n, t)
        z.close(); return b.getvalue()

    def test_office_and_pdf_text(self):
        x = self.office({"xl/sharedStrings.xml": '<sst xmlns="x"><si><t>Item</t></si><si><t>Price</t></si></sst>',
                         "xl/workbook.xml": '<workbook xmlns="x"><sheets><sheet name="Prices"/></sheets></workbook>',
                         "xl/worksheets/sheet1.xml": '<worksheet xmlns="x"><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                                                     '<row><c t="inlineStr"><is><t>Tea</t></is></c><c><v>3.5</v></c></row></sheetData></worksheet>'})
        self.assertEqual("## Prices\nItem\tPrice\nTea\t3.5", documents.extract("p.xlsx", x)[0])
        pp = self.office({"ppt/slides/slide1.xml": '<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>Hello slide</a:t></a:r></a:p></p:sld>'})
        self.assertIn("Hello slide", documents.extract("s.pptx", pp)[0])
        cmap = b"/CIDInit begincmap 1 beginbfchar <0001> <0048> <0002> <0069> endbfchar endcmap"
        pdf = (b"%PDF-1.4\n1 0 obj << /Length 1 >>\nstream\n" + cmap + b"\nendstream\nendobj\n"
               b"2 0 obj << /Length 1 >>\nstream\nBT /F1 12 Tf <00010002> Tj ET\nendstream\nendobj\n%%EOF")
        self.assertEqual("Hi", documents.extract("a.pdf", pdf)[0])
        text, note = documents.extract("scan.pdf", b"%PDF-1.4\n%%EOF")
        self.assertIn("scanned", note)

    def test_documents_reach_the_model_with_the_message(self):
        cid = self.chat(); self.script = [say("It is about tea.")]
        chat = self.run_to_end(cid, text="What is this about?", documents=[{"name": "notes.txt", "text": "Tea is grown in Assam."}])
        self.assertEqual("notes.txt", chat["messages"][0]["attachments"][0]["name"])
        sent = self.requests[-1]["messages"][-1]["content"]
        self.assertIn('<attached_file name="notes.txt">', sent); self.assertIn("Tea is grown in Assam.", sent)


class SearchTerminalPreviewTests(RunTestCase):
    def test_search_finds_words_in_messages(self):
        cid = self.chat(); self.script = [say("The capital of Mongolia is Ulaanbaatar.")]; self.run_to_end(cid, text="capital?")
        hits = server.search_chats("ulaanbaatar mongolia")
        self.assertEqual([cid], [h["id"] for h in hits]); self.assertIn("Ulaanbaatar", hits[0]["snippet"])
        self.assertEqual([], server.search_chats("zanzibar"))

    def test_terminal_runs_only_in_writable_shared_folders(self):
        cid = self.chat(write=True)
        out = server.run_terminal({"chatId": cid, "cwd": str(self.shared), "command": '"%s" -c "print(6*7)"' % sys.executable})
        self.assertIn("42", out)
        with self.assertRaises(server.commands.CommandError):
            server.run_terminal({"chatId": cid, "cwd": str(self.outside), "command": "whoami"})

    def test_preview_serves_only_files_inside_the_shared_folder(self):
        (self.shared / "page.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        cid = self.chat(write=False)
        data, kind = server.preview_file("/preview/%s/0/page.html" % cid)
        self.assertEqual(b"<h1>Hi</h1>", data); self.assertTrue(kind.startswith("text/html"))
        for bad in ("../outside/secret.txt", "..%2Foutside%2Fsecret.txt"):
            with self.assertRaises((ValueError, OSError, server.capabilities.CapabilityError)):
                server.preview_file("/preview/%s/0/%s" % (cid, bad))
        self.assertIn("sandbox", server.PREVIEW_CSP)


class ScheduleTests(unittest.TestCase):
    def test_next_run_times(self):
        now = datetime(2026, 11, 27, 10, 0)  # a Friday
        daily = {"kind": "daily", "time": "09:00", "enabled": True}
        self.assertEqual(datetime(2026, 11, 28, 9, 0), scheduler.next_run(daily, now))
        self.assertEqual(datetime(2026, 11, 30, 9, 0), scheduler.next_run(dict(daily, kind="weekdays"), now))  # skips the weekend
        self.assertEqual(datetime(2026, 12, 1, 9, 0), scheduler.next_run(dict(daily, kind="weekly", day=1), now))  # Tuesday
        self.assertIsNone(scheduler.next_run(dict(daily, enabled=False), now))
        once = {"kind": "once", "at": "2026-11-27T12:30", "enabled": True}
        self.assertEqual(datetime(2026, 11, 27, 12, 30), scheduler.next_run(once, now))
        self.assertIsNone(scheduler.next_run(dict(once, lastRun=1), now))

    def test_due_tasks_run_once(self):
        d = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        t = scheduler.save_task(str(d), {"name": "Tests", "prompt": "run tests", "kind": "daily", "time": "09:00", "model": "m1"}, ["m1"], set())
        started = []
        s = scheduler.Scheduler(lambda: str(d), lambda task: started.append(task["id"]) or "c1")
        s.checked = datetime(2026, 11, 27, 8, 59)
        s.tick(datetime(2026, 11, 27, 9, 0, 10)); s.tick(datetime(2026, 11, 27, 9, 0, 40))
        self.assertEqual([t["id"]], started)
        self.assertEqual("c1", scheduler.load(str(d))[0]["lastChat"])
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.save_task(str(d), {"name": "x", "prompt": "y", "kind": "daily", "time": "25:00", "model": "m1"}, ["m1"], set())


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class MergeAndWorktreeTests(GitTestCase):
    def test_git_merge_needs_approval_and_aborts_on_conflict(self):
        git(self.repo, "checkout", "-q", "-b", "feature"); (self.repo / "b.txt").write_text("feature\n", encoding="utf-8")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "feature work"); git(self.repo, "checkout", "-q", "main")
        cid = self.git_chat("commit")
        self.script = [call("git_merge", repo=str(self.repo), branch="feature"), say("merged")]
        self.run_to_end(cid, approve=True)
        self.assertTrue((self.repo / "b.txt").exists()); self.assertIn("Merge", git(self.repo, "log", "-1", "--pretty=%s"))
        git(self.repo, "checkout", "-q", "-b", "clash", "HEAD~1"); (self.repo / "a.txt").write_text("clash\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "clash"); git(self.repo, "checkout", "-q", "main")
        (self.repo / "a.txt").write_text("main side\n", encoding="utf-8"); git(self.repo, "commit", "-q", "-am", "main side")
        self.script = [call("git_merge", repo=str(self.repo), branch="clash"), say("no")]
        chat = self.run_to_end(cid, approve=True)
        self.assertIn("conflict", [m for m in chat["messages"] if m.get("role") == "tool"][-1]["content"])
        self.assertEqual("", git(self.repo, "status", "--porcelain").strip())  # nothing half-merged

    def test_isolated_subtask_works_on_its_own_branch(self):
        cid = self.git_chat("commit"); server.update_permissions(cid, {"capabilities": {"subtasks": True, "editMode": "auto"}})
        state = {}
        def child_writes():
            child = next(c for c in server.list_chats() if c.get("parentId") == cid)
            path = server.load_chat(child["id"])["folders"][-1]["path"]; state["path"] = path; state["child"] = child["id"]
            return call("write_file", path=os.path.join(path, "feature.txt"), content="from the helper")
        self.script = [call("start_subtask", task="add feature.txt", title="Feature", isolated=True), "child", say("helper done"), say("all done")]
        real = server.upstream.side_effect
        def upstream(path, body, timeout=600):
            if self.script and self.script[0] == "child":
                self.script.pop(0); self.requests.append(body); return child_writes()
            return real(path, body, timeout)
        with mock.patch.object(server, "upstream", side_effect=upstream):
            chat = self.run_to_end(cid)
        branch = "openay/" + state["child"]
        self.assertIn("feature.txt", git(self.repo, "show", "--stat", branch))
        self.assertFalse((self.repo / "feature.txt").exists())  # your working tree is untouched
        result = next(m for m in chat["messages"] if m.get("tool_name") == "start_subtask")["content"]
        self.assertIn("git_merge", result); self.assertIn(branch, result)
        server.delete_chat(state["child"])
        self.assertFalse(os.path.exists(state["path"]))  # the worktree is removed; the branch stays
        self.assertIn(branch, git(self.repo, "branch", "--list", branch))


if __name__ == "__main__":
    unittest.main()
