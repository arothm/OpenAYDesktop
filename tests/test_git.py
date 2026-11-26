"""Phase 5 tests: Git/GitHub tools on real temporary repositories (a local bare repo stands in for GitHub).

Run with:  python -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities  # noqa: E402
import gittools  # noqa: E402
import server  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitTestCase(RunTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.shared / "proj"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Test User"); git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "first")
        self.remote = Path(self.temp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))

    def git_chat(self, mode, write=True):
        c = server.create_chat({"model": "m1", "folders": [{"path": str(self.repo), "write": write}]})
        server.update_permissions(c["id"], {"capabilities": {"git": mode}})
        return c["id"]

    def tool(self, cid, name, **args):
        return server.run_tool(server.saved_tool_config(cid), name, args)


class ReadToolTests(GitTestCase):
    def test_read_tools(self):
        cid = self.git_chat("read"); (self.repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        self.assertIn("M a.txt", self.tool(cid, "git_status", repo=str(self.repo)))
        self.assertIn("+two", self.tool(cid, "git_diff", repo=str(self.repo)))
        self.assertIn("first", self.tool(cid, "git_log", repo=str(self.repo)))
        self.assertIn("first", self.tool(cid, "git_show", repo=str(self.repo), rev="HEAD"))
        self.assertIn("main", self.tool(cid, "git_branches", repo=str(self.repo)))
        for bad in ("--output=x.txt", "-p", "a b", "HEAD;rm"):
            with self.assertRaises(server.ToolError): self.tool(cid, "git_show", repo=str(self.repo), rev=bad)

    def test_off_and_levels(self):
        cid = self.git_chat("off")
        with self.assertRaises(server.ToolError): self.tool(cid, "git_status", repo=str(self.repo))
        names = lambda c: {s["function"]["name"] for s in server.tool_specs(server.saved_tool_config(c))[0]}  # noqa: E731
        self.assertFalse({n for n in names(cid) if n.startswith(("git_", "gh_"))})
        read = names(self.git_chat("read")); self.assertIn("git_status", read); self.assertNotIn("git_commit", read)
        commit = names(self.git_chat("commit")); self.assertIn("git_commit", commit); self.assertNotIn("git_push", commit)
        self.assertTrue({"git_push", "gh_pr_create"} <= names(self.git_chat("push")))
        with self.assertRaises(gittools.GitError): gittools.prepare(server.saved_tool_config(self.git_chat("read")), "git_commit", {"repo": str(self.repo), "message": "x"})

    def test_repository_top_must_be_shared(self):
        sub = self.repo / "sub"; sub.mkdir(); (sub / "f.txt").write_text("x", encoding="utf-8")
        c = server.create_chat({"model": "m1", "folders": [{"path": str(sub), "write": True}]})
        server.update_permissions(c["id"], {"capabilities": {"git": "push"}})
        with self.assertRaises(server.ToolError) as e: self.tool(c["id"], "git_status", repo=str(sub))
        self.assertIn("outside the shared folders", str(e.exception))
        with self.assertRaises(server.ToolError): self.tool(c["id"], "git_status", repo=str(self.outside))

    def test_subtasks_get_read_only_git(self):
        parent = server.load_chat(self.git_chat("push"))
        self.assertEqual("read", capabilities.child_capabilities(parent)["git"])


class CommitTests(GitTestCase):
    def test_commit_after_approval_and_not_after_denial(self):
        cid = self.git_chat("commit"); (self.repo / "b.txt").write_text("new file\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("one\nchanged\n", encoding="utf-8")
        self.script = [call("git_commit", repo=str(self.repo), message="Add b", all=True), say("done")]
        self.run_to_end(cid, approve=False)
        self.assertEqual(1, len(git(self.repo, "log", "--oneline").splitlines()))
        self.script = [call("git_commit", repo=str(self.repo), message="Add b and change a", all=True), say("done")]
        server.ENGINE.start(cid, {"text": "commit"}); run = self.wait(cid, lambda r: r.approval)
        detail = run.approval["data"]["detail"]
        self.assertIn("Add b and change a", detail); self.assertIn("+changed", detail); self.assertIn("b.txt", detail)
        server.ENGINE.decide(cid, run.approval["id"], True); self.wait(cid)
        self.assertEqual("Add b and change a", git(self.repo, "log", "-1", "--pretty=%s").strip())
        self.assertEqual("", git(self.repo, "status", "--porcelain"))

    def test_only_listed_paths_are_committed(self):
        cid = self.git_chat("commit")
        (self.repo / "keep.txt").write_text("k", encoding="utf-8"); (self.repo / "skip.txt").write_text("s", encoding="utf-8")
        chat = server.saved_tool_config(cid)
        data, record = gittools.prepare(chat, "git_commit", {"repo": str(self.repo), "message": "keep", "paths": ["keep.txt"]})
        self.assertNotIn("skip.txt", data["detail"])
        gittools.execute(chat, "git_commit", {}, record)
        self.assertEqual(["keep.txt"], git(self.repo, "show", "--name-only", "--pretty=", "HEAD").split())
        self.assertIn("?? skip.txt", git(self.repo, "status", "--porcelain"))
        with self.assertRaises(gittools.GitError):
            gittools.prepare(chat, "git_commit", {"repo": str(self.repo), "message": "x", "paths": [str(self.outside / "secret.txt")]})

    def test_changes_after_review_are_refused(self):
        cid = self.git_chat("commit"); chat = server.saved_tool_config(cid)
        (self.repo / "a.txt").write_text("reviewed\n", encoding="utf-8")
        _, record = gittools.prepare(chat, "git_commit", {"repo": str(self.repo), "message": "m", "all": True})
        (self.repo / "a.txt").write_text("sneaky\n", encoding="utf-8")
        with self.assertRaises(gittools.GitError): gittools.execute(chat, "git_commit", {}, record)
        self.assertEqual(1, len(git(self.repo, "log", "--oneline").splitlines()))

    def test_read_only_folder_cannot_commit(self):
        cid = self.git_chat("commit", write=False); (self.repo / "a.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(gittools.GitError) as e:
            gittools.prepare(server.saved_tool_config(cid), "git_commit", {"repo": str(self.repo), "message": "m", "all": True})
        self.assertIn("read-only", str(e.exception))


class PushTests(GitTestCase):
    def test_push_with_approval(self):
        cid = self.git_chat("push")
        self.script = [call("git_push", repo=str(self.repo)), say("pushed")]
        server.ENGINE.start(cid, {"text": "push"}); run = self.wait(cid, lambda r: r.approval)
        self.assertIn(str(self.remote).replace("\\", "/").split("/")[-1], run.approval["data"]["detail"].replace("\\", "/"))
        self.assertIn("first", run.approval["data"]["detail"]); self.assertIn("new on the remote", run.approval["data"]["detail"])
        server.ENGINE.decide(cid, run.approval["id"], True); self.wait(cid)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"],
                                                                              capture_output=True, text=True).stdout)
        git(self.repo, "fetch", "-q", "origin")
        with self.assertRaises(gittools.GitError):  # nothing new to push
            gittools.prepare(server.saved_tool_config(cid), "git_push", {"repo": str(self.repo)})

    def test_moved_branch_and_bad_names_are_refused(self):
        cid = self.git_chat("push"); chat = server.saved_tool_config(cid)
        _, record = gittools.prepare(chat, "git_push", {"repo": str(self.repo)})
        (self.repo / "a.txt").write_text("later\n", encoding="utf-8"); git(self.repo, "commit", "-qam", "later")
        with self.assertRaises(gittools.GitError): gittools.execute(chat, "git_push", {}, record)
        for bad in ({"remote": "--receive-pack=evil"}, {"branch": "-D"}, {"remote": "../x"}, {"branch": "a b"}):
            with self.assertRaises(gittools.GitError): gittools.prepare(chat, "git_push", dict(repo=str(self.repo), **bad))
        with self.assertRaises(gittools.GitError):  # pushing needs the push level
            gittools.prepare(server.saved_tool_config(self.git_chat("commit")), "git_push", {"repo": str(self.repo)})

    def test_pull_request_needs_github_cli(self):
        cid = self.git_chat("push"); real_which = shutil.which
        with mock.patch.object(gittools.shutil, "which", side_effect=lambda n: None if n == "gh" else real_which(n)):
            data, record = gittools.prepare(server.saved_tool_config(cid), "gh_pr_create", {"repo": str(self.repo), "title": "T", "body": "B"})
            self.assertIn("Title: T", data["detail"])
            with self.assertRaises(gittools.GitError) as e: gittools.execute(server.saved_tool_config(cid), "gh_pr_create", {}, record)
        self.assertIn("GitHub CLI", str(e.exception))


if __name__ == "__main__":
    unittest.main()
