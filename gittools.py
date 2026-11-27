"""Git and GitHub tools for chats, limited to repositories inside the chat's shared folders.

Permission levels (session setting "git"):
  off     no Git tools
  read    git_status, git_diff, git_log, git_show, git_branches, gh_pr_list
  commit  + git_commit            (the user reviews every commit: message, files, full diff)
  push    + git_push, gh_pr_create (the user approves every push and pull request)

Safety:
  - A repository is usable only if its top folder is inside a shared folder (sharing a
    subfolder doesn't open the whole repository); committing needs "Allow edits" on it.
  - Commands run without a shell. Branch, remote and revision names are checked so they can't
    be read as options, and paths are passed after "--".
  - External diff/textconv programs and core.fsmonitor are disabled; terminal prompts are off.
  - Git and gh get no stdin, and a timeout stops the whole process tree (see call()).
  - Commits and pushes are fingerprinted when reviewed and refused if anything changed since.
  - Force-push is not offered. Credentials stay with Git / the GitHub CLI.
"""

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time

import capabilities
import commands

MODES = ["off", "read", "commit", "push"]
READ_TOOLS = {"git_status", "git_diff", "git_log", "git_show", "git_branches", "gh_pr_list"}
REVIEWED = {"git_commit": "commit", "git_merge": "commit", "git_push": "push", "gh_pr_create": "push"}
MAX_OUT = 30_000  # one Git result; diffs longer than this are cut (the model can ask for one path)
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}$")            # remote / branch names
REV = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^@{}-]{0,99}$")       # commit-ish
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(Exception):
    pass


class Stopped(GitError):
    """The user pressed Stop while Git was running."""


_context = threading.local()  # the Stop signal of the reply running on this thread


def set_cancel(event):
    """Make Git calls on this thread end as soon as `event` is set (None: no Stop signal)."""
    _context.cancel = event


def level(pol):
    return MODES.index(pol.get("git", "off")) if pol.get("git") in MODES else 0


def require(chat, needed):
    pol = capabilities.policy(chat)
    if level(pol) < MODES.index(needed):
        what = {"read": "Git tools", "commit": "committing", "push": "pushing and pull requests"}[needed]
        raise GitError("This session doesn't allow %s. Change Git in Session settings." % what)


def _env():
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", GH_PROMPT_DISABLED="1",
                GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")


def _exe(name):
    path = shutil.which(name)
    if not path:
        raise GitError({"git": "Git isn't installed (git-scm.com).", "gh": "The GitHub CLI isn't installed (cli.github.com), or isn't on the PATH."}[name])
    return path


def call(cmd, timeout, cwd=None):
    """Run a program and return a CompletedProcess, without ever hanging the caller.

    stdin is DEVNULL: the installed app's server keeps a thread reading its own stdin (to notice when the window
    closes), and on Windows a child that inherits that pipe can block forever on it (Git for Windows does, at
    start-up). On timeout the whole process tree is stopped: Git's launcher (cmd/git.exe) starts the real git.exe,
    and killing only the launcher left the real one holding the output pipes, so the wait never ended.
    Pressing Stop (see set_cancel) ends it the same way, within a fraction of a second, and raises Stopped."""
    cancel = getattr(_context, "cancel", None)
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=_env(), creationflags=NO_WINDOW)
    deadline = time.time() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)  # retrying communicate() after a timeout loses no output
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            stopped = cancel is not None and cancel.is_set()
            if not stopped and time.time() < deadline:
                continue
        commands._kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # something outside the tree still holds the pipes; don't wait for it
        if stopped:
            raise Stopped("Stopped: you pressed Stop while %s was running." % os.path.basename(cmd[0]).split(".")[0])
        raise subprocess.TimeoutExpired(cmd, timeout)


def run(repo, *args, timeout=60, check=True):
    cmd = [_exe("git"), "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "color.ui=false", "-C", repo, *args]
    try:
        r = call(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise GitError("git %s took too long and was stopped." % args[0])
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip()[:2000] or "git %s failed (exit code %d)." % (args[0], r.returncode))
    return r.stdout


def gh(repo, *args, timeout=120):
    try:
        r = call([_exe("gh"), *args], timeout, cwd=repo)
    except subprocess.TimeoutExpired:
        raise GitError("gh %s took too long and was stopped." % args[0])
    if r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip()[:2000] or "gh failed (exit code %d)." % r.returncode)
    return r.stdout


def clip(text, limit=MAX_OUT):
    return text if len(text) <= limit else text[:limit] + "\n[... %d more characters not shown]" % (len(text) - limit)


def repo_root(chat, path, write=False):
    """Top folder of the repository at `path`, which must lie entirely inside a shared folder."""
    grants = capabilities.grants(chat)
    if not path and len(grants) == 1:
        path = grants[0]["path"]
    try:
        p = capabilities.resolve(chat, path)
    except capabilities.CapabilityError as e:
        raise GitError(str(e))
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    try:
        top = os.path.realpath(run(p, "rev-parse", "--show-toplevel").strip())
    except GitError:
        raise GitError("%s is not inside a Git repository." % p)
    matches = [g for g in grants if capabilities.inside(top, g["path"])]
    if not matches:
        raise GitError("This repository's top folder (%s) is outside the shared folders. Share the repository's top "
                       "folder with this chat to use Git on it." % top)
    if write and not max(matches, key=lambda g: len(g["path"]))["write"]:
        raise GitError("The repository's folder is shared read-only. Turn on Allow edits for it to commit.")
    return top


def rel_paths(chat, top, paths):
    """Validated pathspecs relative to the repository (all inside it and inside the shared folders)."""
    if not paths:
        return ["."]
    if isinstance(paths, str):
        paths = [paths]
    out = []
    for raw in paths[:200]:
        full = os.path.realpath(raw if os.path.isabs(str(raw)) else os.path.join(top, str(raw)))
        if not capabilities.inside(full, top):
            raise GitError("Path is outside the repository: %s" % raw)
        try:
            capabilities.resolve(chat, full)
        except capabilities.CapabilityError as e:
            raise GitError(str(e))
        out.append(os.path.relpath(full, top).replace("\\", "/") or ".")
    return out


def _name(value, what, default=None):
    value = str(value or default or "").strip()
    if not NAME.match(value) or ".." in value:
        raise GitError("Invalid %s name: %r" % (what, value))
    return value


# ---------------------------------------------------------------- read tools

def git_status(chat, repo="", **_):
    top = repo_root(chat, repo)
    return "Repository: %s\n%s" % (top, run(top, "status", "--short", "--branch") or "(clean)")


def git_diff(chat, repo="", staged=False, path="", **_):
    top = repo_root(chat, repo)
    args = ["diff", "--no-ext-diff", "--no-textconv", "--stat", "--patch"] + (["--cached"] if staged else [])
    out = run(top, *args, "--", *rel_paths(chat, top, path or None))
    return clip(out) or "No %s changes." % ("staged" if staged else "unstaged")


def git_log(chat, repo="", max_count=20, path="", **_):
    top = repo_root(chat, repo)
    try:
        n = max(1, min(int(max_count or 20), 200))
    except (TypeError, ValueError):
        n = 20
    out = run(top, "log", "-n", str(n), "--date=short", "--pretty=format:%h %ad %an  %s%d", "--", *rel_paths(chat, top, path or None), check=False)
    return out or "No commits yet."


def git_show(chat, repo="", rev="HEAD", **_):
    top = repo_root(chat, repo)
    rev = str(rev or "HEAD").strip()
    if not REV.match(rev):
        raise GitError("Invalid revision: %r" % rev)
    return clip(run(top, "show", "--no-ext-diff", "--no-textconv", "--stat", "--patch", rev, "--"))


def git_branches(chat, repo="", **_):
    top = repo_root(chat, repo)
    return run(top, "branch", "--all", "--verbose", "--no-color") or "No branches yet."


def gh_pr_list(chat, repo="", **_):
    top = repo_root(chat, repo)
    return gh(top, "pr", "list", "--limit", "20") or "No open pull requests."


READ = {"git_status": git_status, "git_diff": git_diff, "git_log": git_log, "git_show": git_show,
        "git_branches": git_branches, "gh_pr_list": gh_pr_list}


def run_read(chat, name, args):
    require(chat, "read")
    try:
        return READ[name](chat, **args)
    except TypeError as e:
        raise GitError("Bad arguments: %s" % e)


# ---------------------------------------------------------------- reviewed tools: prepare -> user approves -> execute

def _commit_preview(chat, top, paths):
    status = run(top, "status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
    if not status.strip():
        raise GitError("There is nothing to commit in those paths.")
    has_head = run(top, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    diff = run(top, "diff", "--no-ext-diff", "--no-textconv", "--stat", "--patch", *(["HEAD"] if has_head else ["--cached"]), "--", *paths, check=False)
    untracked = [l[3:] for l in status.splitlines() if l.startswith("??")]
    stamp = []
    for u in untracked:  # new files aren't in the diff, so their size and time go into the fingerprint
        try:
            st = os.stat(os.path.join(top, u)); stamp.append("%s:%d:%d" % (u, st.st_size, st.st_mtime_ns))
        except OSError:
            stamp.append(u)
    fingerprint = hashlib.sha256((status + "\0" + diff + "\0" + "\n".join(stamp)).encode("utf-8", "replace")).hexdigest()
    return status, diff, untracked, fingerprint


def prepare(chat, name, args):
    """Returns (approval data for the user, record for execute)."""
    require(chat, REVIEWED[name])
    if name == "git_commit":
        top = repo_root(chat, args.get("repo"), write=True)
        message = str(args.get("message") or "").strip()
        if not message or len(message) > 5000:
            raise GitError("A commit message of 1-5000 characters is required.")
        paths = rel_paths(chat, top, None if args.get("all") or not args.get("paths") else args.get("paths"))
        status, diff, untracked, fp = _commit_preview(chat, top, paths)
        detail = "Repository: %s\n\nMessage:\n%s\n\nFiles:\n%s\n%s\n%s" % (
            top, message, status.rstrip(), ("\nNew files: " + ", ".join(untracked)) if untracked else "", clip(diff, 40_000))
        return ({"title": "Make this commit?", "detail": detail,
                 "why": "The commit is made with your Git identity. Nothing is pushed."},
                {"top": top, "paths": paths, "message": message, "fingerprint": fp})
    if name == "git_merge":
        top = repo_root(chat, args.get("repo"), write=True)
        current = run(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current == "HEAD":
            raise GitError("The repository isn't on a branch (detached HEAD).")
        other = _name(args.get("branch"), "branch")
        tip = run(top, "rev-parse", "--verify", "-q", "refs/heads/%s" % other, check=False).strip()
        if not tip:
            raise GitError("There is no local branch named %s." % other)
        if run(top, "status", "--porcelain=v1", "--untracked-files=no").strip():
            raise GitError("The repository has uncommitted changes. Commit them first, then merge.")
        commits = run(top, "log", "--oneline", "-n", "50", "HEAD..%s" % tip).strip()
        if not commits:
            raise GitError("Nothing to merge: %s has no commits that %s lacks." % (other, current))
        stat = run(top, "diff", "--stat", "HEAD...%s" % tip, check=False)
        detail = "Repository: %s\nMerge %s into %s\n\nCommits:\n%s\n\nFiles:\n%s" % (top, other, current, commits, clip(stat, 20_000))
        return ({"title": "Merge %s into %s?" % (other, current), "detail": detail,
                 "why": "This changes your working branch. If the changes conflict, nothing is merged."},
                {"top": top, "branch": current, "head": run(top, "rev-parse", "HEAD").strip(), "other": other, "tip": tip})
    top = repo_root(chat, args.get("repo"))
    branch = run(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        raise GitError("The repository isn't on a branch (detached HEAD).")
    branch = _name(args.get("branch"), "branch", branch)
    head = run(top, "rev-parse", "--verify", branch).strip()
    if name == "git_push":
        remote = _name(args.get("remote"), "remote", "origin")
        url = run(top, "remote", "get-url", "--push", remote).strip()
        known = run(top, "rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (remote, branch), check=False).strip()
        commits = run(top, "log", "--oneline", "-n", "50", ("%s..%s" % (known, branch)) if known else branch)
        if known and not commits.strip():
            raise GitError("Nothing to push: %s/%s already has these commits." % (remote, branch))
        detail = "Remote: %s (%s)\nBranch: %s%s\n\nCommits:\n%s" % (
            remote, url, branch, "" if known else " (new on the remote)", commits.strip() or "(none)")
        return ({"title": "Push to %s?" % remote, "detail": detail,
                 "why": "Pushing sends these commits to %s with your Git credentials. Force-push is never used." % url},
                {"top": top, "remote": remote, "branch": branch, "head": head})
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    if not title or len(title) > 256 or len(body) > 60_000:
        raise GitError("A pull request needs a title (up to 256 characters) and a body up to 60,000 characters.")
    base = _name(args.get("base"), "base branch") if args.get("base") else None
    detail = "Repository: %s\nFrom branch: %s%s\n\nTitle: %s\n\n%s" % (top, branch, ("\nInto: " + base) if base else "", title, body or "(no description)")
    return ({"title": "Open this pull request on GitHub?", "detail": detail,
             "why": "This creates a public pull request with your GitHub account (via the GitHub CLI). The branch must already be pushed."},
            {"top": top, "branch": branch, "head": head, "title": title, "body": body, "base": base})


def execute(chat, name, args, record):
    """Run an approved commit/push/PR after re-checking that nothing changed since the review."""
    require(chat, REVIEWED[name])
    top = record["top"]
    if name == "git_commit":
        if repo_root(chat, top, write=True) != top:
            raise GitError("The repository changed.")
        if _commit_preview(chat, top, record["paths"])[3] != record["fingerprint"]:
            raise GitError("The files changed after you reviewed the commit, so nothing was committed. Review it again.")
        run(top, "add", "-A", "--", *record["paths"])
        commit = ["commit", "-m", record["message"]] + ([] if record["paths"] == ["."] else ["--", *record["paths"]])
        run(top, *commit, timeout=120)
        return "Committed:\n" + run(top, "log", "-1", "--stat", "--pretty=format:%h %s")
    if name == "git_merge":
        if run(top, "rev-parse", "HEAD").strip() != record["head"] or run(top, "rev-parse", "--verify", "refs/heads/%s" % record["other"]).strip() != record["tip"]:
            raise GitError("A branch moved after you reviewed the merge, so nothing was merged. Review it again.")
        r = run(top, "merge", "--no-edit", "--no-ff", record["tip"], check=False, timeout=120)
        if run(top, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip():
            run(top, "merge", "--abort", check=False)
            raise GitError("The branches conflict, so the merge was cancelled and nothing changed.\n" + r[-1500:])
        if run(top, "rev-parse", "HEAD").strip() == record["head"]:
            raise GitError("git merge failed:\n" + r[-1500:])
        return "Merged %s into %s.\n%s" % (record["other"], record["branch"], run(top, "log", "-1", "--stat", "--pretty=format:%h %s"))
    if run(top, "rev-parse", "--verify", record["branch"]).strip() != record["head"]:
        raise GitError("The branch moved after you reviewed it, so nothing was sent. Review it again.")
    if name == "git_push":
        try:
            r = call([_exe("git"), "-c", "core.fsmonitor=false", "-C", top, "push", "--", record["remote"],
                      "refs/heads/%s:refs/heads/%s" % (record["branch"], record["branch"])], 300)
        except subprocess.TimeoutExpired:
            raise GitError("git push took more than 5 minutes and was stopped.")
        if r.returncode != 0:
            raise GitError((r.stderr or r.stdout).strip()[:2000] or "git push failed.")
        return "Pushed %s to %s.\n%s" % (record["branch"], record["remote"], (r.stderr or r.stdout).strip()[:2000])
    args = ["pr", "create", "--title", record["title"], "--body", record["body"] or " ", "--head", record["branch"]]
    if record["base"]:
        args += ["--base", record["base"]]
    return "Pull request created: " + gh(top, *args).strip()
