"""Scheduled tasks: a prompt that starts a new chat on a schedule.

Stored in data/schedules.json. A background thread checks every 30 seconds while the app runs
(the window may be closed: the server keeps running in the tray). When a task is due, it creates a
chat (in its project, if it has one: the project's folders, starting permissions, skills,
instructions and memory) and starts a reply with the task's prompt. Anything that needs approval
waits in that chat and in Activity, like any other reply. If the app was closed at the due time,
the task runs once when the app next starts (at most one catch-up run per task).

Kinds: "daily" at HH:MM, "weekdays" (Mon-Fri) at HH:MM, "weekly" on a day at HH:MM,
"hourly" every N hours, "once" at a local date and time (then it turns itself off).
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

KINDS = ("daily", "weekdays", "weekly", "hourly", "once")
CHECK_EVERY = 30
MAX_TASKS = 50
_lock = threading.Lock()


class ScheduleError(Exception):
    pass


class Busy(Exception):
    """The task couldn't start yet (the most replies are already running): it is tried again shortly."""


BUSY_RETRY_FOR = 3600  # seconds a due task keeps waiting for a free slot before it counts as failed


def path(data_dir):
    return os.path.join(data_dir, "schedules.json")


def load(data_dir):
    try:
        with open(path(data_dir), encoding="utf-8") as f:
            items = json.load(f)
        return [t for t in items if isinstance(t, dict) and t.get("id")] if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _save(data_dir, items):
    os.makedirs(data_dir, exist_ok=True)
    tmp = path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(data_dir))


def _hhmm(value):
    try:
        h, m = (int(x) for x in str(value).split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return "%02d:%02d" % (h, m)
    except ValueError:
        pass
    raise ScheduleError("Choose a time like 09:00.")


def clean(body, models, project_ids):
    """Validate a task from the window."""
    name = " ".join(str(body.get("name") or "").split())
    prompt = str(body.get("prompt") or "").strip()
    if not name or len(name) > 80:
        raise ScheduleError("A task needs a name of 1-80 characters.")
    if not prompt or len(prompt) > 20_000:
        raise ScheduleError("Write what the task should do (up to 20,000 characters).")
    kind = body.get("kind")
    if kind not in KINDS:
        raise ScheduleError("Choose when the task runs.")
    model = str(body.get("model") or "").strip()
    if not model or (models and model not in models):
        raise ScheduleError("Choose an available model.")
    project = body.get("projectId") or None
    if project and project not in project_ids:
        raise ScheduleError("That project no longer exists.")
    task = {"name": name, "prompt": prompt, "kind": kind, "model": model, "projectId": project, "enabled": bool(body.get("enabled", True))}
    if kind in ("daily", "weekdays", "weekly"):
        task["time"] = _hhmm(body.get("time"))
    if kind == "weekly":
        day = int(body.get("day") or 0)
        if not 0 <= day <= 6:
            raise ScheduleError("Choose a day of the week.")
        task["day"] = day
    if kind == "hourly":
        every = int(body.get("every") or 0)
        if not 1 <= every <= 24:
            raise ScheduleError("Run every 1 to 24 hours.")
        task["every"] = every
    if kind == "once":
        try:
            task["at"] = datetime.fromisoformat(str(body.get("at"))).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            raise ScheduleError("Choose a date and time.")
    return task


def next_run(task, after):
    """The first due time strictly after `after` (a local datetime), or None."""
    if not task.get("enabled"):
        return None
    kind = task["kind"]
    if kind == "once":
        at = datetime.fromisoformat(task["at"])
        return at if at > after and not task.get("lastRun") else None
    if kind == "hourly":
        last = datetime.fromtimestamp(task["lastRun"] / 1000) if task.get("lastRun") else None
        step = timedelta(hours=task["every"])
        if not last:
            return after + timedelta(minutes=1)
        due = last + step
        return due if due > after else after + timedelta(seconds=1)
    h, m = (int(x) for x in task["time"].split(":"))
    day = after.replace(hour=h, minute=m, second=0, microsecond=0)
    for i in range(0, 8):
        cand = day + timedelta(days=i)
        if cand <= after:
            continue
        if kind == "weekdays" and cand.weekday() > 4:
            continue
        if kind == "weekly" and cand.weekday() != task["day"]:
            continue
        return cand
    return None


def save_task(data_dir, body, models, project_ids):
    task = clean(body, models, project_ids)
    with _lock:
        items = load(data_dir)
        existing = next((t for t in items if t["id"] == body.get("id")), None) if body.get("id") else None
        if existing:
            keep = {k: existing[k] for k in ("id", "created", "lastRun", "lastChat", "lastError") if k in existing}
            if existing.get("kind") != task["kind"] or existing.get("at") != task.get("at"):
                keep.pop("lastRun", None) if task["kind"] == "once" else None
            existing.clear(); existing.update(task, **keep)
            task = existing
        else:
            if len(items) >= MAX_TASKS:
                raise ScheduleError("At most %d scheduled tasks." % MAX_TASKS)
            task.update(id="s" + secrets.token_hex(6), created=int(time.time() * 1000))
            items.append(task)
        task["nextRun"] = _stamp(next_run(task, datetime.now()))
        _save(data_dir, items)
    return task


def delete_task(data_dir, task_id):
    with _lock:
        _save(data_dir, [t for t in load(data_dir) if t["id"] != task_id])


def _stamp(dt):
    return int(dt.timestamp() * 1000) if dt else None


def listing(data_dir):
    now = datetime.now()
    out = load(data_dir)
    for t in out:
        t["nextRun"] = _stamp(next_run(t, now))
    return out


class Scheduler:
    """Runs due tasks. start(chat) is given by the server: it creates the chat and starts the reply."""

    def __init__(self, data_dir_fn, start_fn):
        self.data_dir_fn, self.start_fn = data_dir_fn, start_fn
        self.checked = datetime.now() - timedelta(hours=12)  # one catch-up for tasks missed while the app was closed
        self.thread = None
        self.waiting = {}  # task id -> (task, first try): due tasks waiting for a free reply slot

    def run_in_background(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self.thread.start()

    def _loop(self):
        while True:
            try:
                self.tick(datetime.now())
            except Exception:  # noqa: BLE001 - a bad task must never stop the scheduler
                pass
            time.sleep(CHECK_EVERY)

    def tick(self, now):
        data_dir = self.data_dir_fn()
        due = []
        with _lock:
            items = load(data_dir)
            for t in items:
                last = datetime.fromtimestamp(t["lastRun"] / 1000) if t.get("lastRun") else None
                when = next_run(t, max(self.checked, last) if last else self.checked)
                if when and when <= now:
                    t["lastRun"] = int(now.timestamp() * 1000)
                    if t["kind"] == "once":
                        t["enabled"] = False
                    due.append(t)
            if due:
                _save(data_dir, items)
        self.checked = now
        for tid, (t, since) in list(self.waiting.items()):  # tasks that were due while too many replies ran
            if not any(x["id"] == tid and x.get("enabled", True) for x in load(data_dir)):
                self.waiting.pop(tid, None)
            elif tid not in {x["id"] for x in due}:
                due.append(t)
        for t in due:
            self.run_task(t)

    def run_task(self, task):
        data_dir = self.data_dir_fn()
        try:
            chat_id = self.start_fn(task)
            result = {"lastChat": chat_id, "lastError": ""}
            self.waiting.pop(task["id"], None)
        except Busy as e:
            first = self.waiting.get(task["id"], (task, time.time()))[1]
            if time.time() - first < BUSY_RETRY_FOR:
                self.waiting[task["id"]] = (task, first)
                result = {"lastError": "Waiting to start: %s" % e}
            else:
                self.waiting.pop(task["id"], None)
                result = {"lastError": "Didn't start: %s for an hour." % str(e).rstrip(".")}
        except Exception as e:  # noqa: BLE001
            self.waiting.pop(task["id"], None)
            result = {"lastError": str(e)[:300]}
        with _lock:
            items = load(data_dir)
            for t in items:
                if t["id"] == task["id"]:
                    t.update(result)
            _save(data_dir, items)
        return result
