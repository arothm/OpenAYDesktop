"""Isolated subtasks: a helper chat that works in its own Git worktree and branch.

start_subtask(isolated=true) in a chat with Git "commit" or more, whose shared folder is a
repository's top folder with Allow edits: the server creates a worktree under
data/worktrees/<child id> on a new branch openay/<child id>, starting from the current commit.
The helper gets that worktree as its writable folder instead of the repository, so several
helpers can change code at the same time without touching each other or your working tree.
When the helper finishes, its changes are committed on its branch (with your Git identity,
never pushed), and the parent is told the branch name. Bringing the work into your branch is
a git_merge, which you approve. Deleting the helper chat removes the worktree; the branch stays.
"""

import os
import shutil

import gittools

BRANCH_PREFIX = "openay/"


class WorktreeError(Exception):
    pass


def root(data_dir):
    path = os.path.join(data_dir, "worktrees")
    os.makedirs(path, exist_ok=True)
    return path


def create(data_dir, parent_chat, child_id):
    """Make a worktree for a child chat. Returns (repo top, worktree path, branch, base commit)."""
    tops = []
    for g in gittools.capabilities.grants(parent_chat):
        if not g["write"]:
            continue
        try:
            top = gittools.repo_root(parent_chat, g["path"], write=True)
        except gittools.GitError:
            continue
        if os.path.normcase(top) == os.path.normcase(g["path"]) and top not in tops:
            tops.append(top)
    if len(tops) != 1:
        raise WorktreeError("An isolated subtask needs exactly one shared repository (its top folder, with Allow edits); "
                            "this chat has %d." % len(tops))
    top = tops[0]
    base = gittools.run(top, "rev-parse", "--verify", "HEAD").strip()
    branch = BRANCH_PREFIX + child_id
    path = os.path.join(root(data_dir), child_id)
    gittools.run(top, "worktree", "add", "-b", branch, path, base, timeout=120)
    return top, os.path.realpath(path), branch, base


def finish(path, branch, base, title):
    """Commit whatever the helper changed in its worktree. Returns a short report for the parent."""
    if not os.path.isdir(path):
        return "The subtask's worktree is gone."
    status = gittools.run(path, "status", "--porcelain=v1", "--untracked-files=all")
    if status.strip():
        gittools.run(path, "add", "-A")
        gittools.run(path, "commit", "-m", "Subtask: %s" % title[:200], timeout=120)
    log = gittools.run(path, "log", "--oneline", "-n", "20", "%s..HEAD" % base, check=False)
    stat = gittools.run(path, "diff", "--stat", base, "HEAD", check=False)
    if not status.strip() and not log.strip():
        return "The subtask changed no files in its worktree (branch %s)." % branch
    return ("The subtask's changes are committed on branch %s (not merged, not pushed).\nCommits:\n%s\nFiles:\n%s\n"
            "Use git_merge with branch %s to bring them into the current branch (the user approves it)."
            % (branch, log.strip()[:2000], stat.strip()[:3000], branch))


def remove(top, path):
    """Remove a helper's worktree (its branch is kept)."""
    if top and os.path.isdir(top):
        gittools.run(top, "worktree", "remove", "--force", path, check=False, timeout=120)
        gittools.run(top, "worktree", "prune", check=False)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
