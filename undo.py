"""Undo a reply: put back every file change one reply made.

While a reply runs, each change it makes to files is written to the chat's journal
(data/undo/<chat id>.jsonl, one JSON object per line):
  write    a file was created or replaced    {"path", "trash": ID of the earlier version or null if new, "after": sha256}
  create   a folder was created              {"path"}
  copy     a file/folder was copied          {"path": destination}
  move     a file/folder was moved/renamed   {"source", "path": destination}
  delete   a file/folder went to the trash   {"path", "trash"}
  restore  a trash item was put back         {"path"}
Undo works through that reply's entries newest first. It never overwrites anything it didn't
write: a file edited again after the reply (by you or a later reply) is skipped and listed, and
whatever undo replaces is kept in the trash first, so an undo can be undone from the Trash screen.
Commands and Git actions can't be undone and are listed as such.
"""

import hashlib
import json
import os
import shutil
import threading
import time

import capabilities

_lock = threading.Lock()
_context = threading.local()   # the reply running on this thread: (chat id, run id)


def journal_path(data_dir, chat_id):
    folder = os.path.join(data_dir, "undo")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, chat_id + ".jsonl")


def set_context(chat_id, run_id):
    _context.value = (chat_id, run_id) if chat_id and run_id else None


def current():
    return getattr(_context, "value", None)


def fingerprint(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def record(data_dir, kind, **fields):
    """Add an entry for the reply running on this thread (no-op outside a reply)."""
    ctx = current()
    if not ctx:
        return
    entry = dict(fields, kind=kind, run=ctx[1], at=int(time.time()))
    with _lock, open(journal_path(data_dir, ctx[0]), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def entries(data_dir, chat_id, run_id):
    try:
        with open(journal_path(data_dir, chat_id), encoding="utf-8") as f:
            items = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return [e for e in items if e.get("run") == run_id and not e.get("undone")]


def runs_with_changes(data_dir, chat_id):
    """Run IDs that have something left to undo."""
    try:
        with open(journal_path(data_dir, chat_id), encoding="utf-8") as f:
            return sorted({e["run"] for e in (json.loads(l) for l in f if l.strip())
                           if not e.get("undone") and e.get("kind") not in ("command", "git")})
    except (OSError, ValueError, KeyError):
        return []


def plan(data_dir, chat_id, run_id):
    """What undo would do: [{"text", "ok"}], newest change first."""
    out = []
    for e in reversed(entries(data_dir, chat_id, run_id)):
        out.append(_describe(e))
    return out


def _describe(e):
    k, p = e["kind"], e.get("path", "")
    if k == "write":
        changed = fingerprint(p) != e.get("after")
        if changed:
            return {"text": "Skip %s: it changed after this reply" % p, "ok": False}
        return {"text": ("Restore the earlier version of %s" if e.get("trash") else "Move %s (created by this reply) to the trash") % p, "ok": True}
    if k == "create":
        return {"text": "Move the new folder %s to the trash" % p, "ok": os.path.isdir(p)}
    if k == "copy":
        return {"text": "Move the copy %s to the trash" % p, "ok": os.path.lexists(p)}
    if k == "move":
        ok = os.path.lexists(p) and not os.path.lexists(e.get("source", ""))
        return {"text": "Move %s back to %s" % (p, e.get("source")), "ok": ok}
    if k == "delete":
        return {"text": "Restore %s from the trash" % p, "ok": not os.path.lexists(p)}
    if k == "restore":
        return {"text": "Move %s back to the trash" % p, "ok": os.path.lexists(p)}
    if k == "command":
        return {"text": "Not undone: the command %s (commands can't be undone)" % p, "ok": False}
    if k == "git":
        return {"text": "Not undone: %s (Git actions can't be undone here)" % p, "ok": False}
    return {"text": "Can't undo: %s" % k, "ok": False}


def undo(data_dir, chat_id, run_id, trash):
    """Undo one reply's file changes. Returns [{"text", "ok"}] describing what happened."""
    results = []
    with _lock:
        path = journal_path(data_dir, chat_id)
        try:
            with open(path, encoding="utf-8") as f:
                items = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            items = []
        for e in reversed(items):
            if e.get("run") != run_id or e.get("undone"):
                continue
            d = _describe(e)
            if not d["ok"]:
                results.append(d)
                continue
            try:
                _apply(e, trash, chat_id)
                e["undone"] = True
                results.append({"text": d["text"].replace("Restore the earlier", "Restored the earlier").replace("Move ", "Moved ", 1)
                                .replace("Restore ", "Restored ", 1), "ok": True})
            except (OSError, capabilities.CapabilityError) as err:
                results.append({"text": "Couldn't undo %s: %s" % (e.get("path"), err), "ok": False})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
        os.replace(tmp, path)
    return results


def _apply(e, trash, chat_id):
    k, p = e["kind"], e.get("path", "")
    if k == "write":
        if e.get("trash"):
            capabilities.restore_item(trash, e["trash"])  # keeps the reply's version in the trash too
        else:
            capabilities._store(trash, p, "deleted", chat_id)
    elif k in ("create", "copy", "restore"):
        capabilities._store(trash, p, "deleted", chat_id)
    elif k == "move":
        os.makedirs(os.path.dirname(e["source"]), exist_ok=True)
        shutil.move(p, e["source"])
    elif k == "delete":
        capabilities.restore_item(trash, e["trash"], p)


def forget(data_dir, chat_id):
    try:
        os.remove(journal_path(data_dir, chat_id))
    except FileNotFoundError:
        pass
