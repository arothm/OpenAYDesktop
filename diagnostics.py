"""Local, anonymous diagnostics (Settings > Diagnostics): what replies, model steps and tools cost and how long
they take, kept on this PC so the app can be improved from real evidence.

Never stored: message text, file names or paths, folder names, web addresses, search queries, command text, chat
titles, chat IDs or keys. An event holds only numbers, true/false and short names from fixed sets (tool and model
names, modes, outcomes, error types); any other string is dropped by clean(), whatever the caller passes.

Files: data/diagnostics/YYYY-MM-DD.jsonl, one event per line, kept KEEP_DAYS days.
"""

import json
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

KEEP_DAYS = 90
MAX_EVENTS_READ = 500_000  # a summary never reads more than this
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")  # "read_file", "gpt-oss:120b", "HTTPError"; no spaces, slashes or backslashes
_lock = threading.Lock()
_pruned = {}  # data folder -> the day its old files were last pruned


def folder(data_dir):
    return os.path.join(data_dir, "diagnostics")


def clean(fields):
    """Keep numbers, booleans and short safe names; drop everything else (text could carry private content)."""
    out = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not SAFE_NAME.match(k):
            continue
        if v is None or isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            if math.isfinite(v):
                out[k] = round(v, 3)
        elif isinstance(v, str) and SAFE_NAME.match(v):
            out[k] = v
    return out


def record(data_dir, kind, fields):
    """Append one event. Never raises: diagnostics must not break a reply."""
    try:
        event = dict(clean(fields), kind=kind if SAFE_NAME.match(str(kind)) else "other", t=int(time.time()))
        path = folder(data_dir)
        today = date.today()
        with _lock:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, today.isoformat() + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
            if _pruned.get(path) != today:
                _pruned[path] = today
                prune(data_dir, today)
    except Exception:  # noqa: BLE001
        pass


def prune(data_dir, today=None):
    """Delete day files older than KEEP_DAYS."""
    cutoff = (today or date.today()) - timedelta(days=KEEP_DAYS)
    for name in _day_files(data_dir):
        if _day_of(name) < cutoff:
            try: os.remove(os.path.join(folder(data_dir), name))
            except OSError: pass


def clear(data_dir):
    """Delete all diagnostics (Settings > Diagnostics > Clear)."""
    removed = 0
    with _lock:
        for name in _day_files(data_dir):
            try:
                os.remove(os.path.join(folder(data_dir), name)); removed += 1
            except OSError:
                pass
    return removed


def _day_files(data_dir):
    try:
        names = os.listdir(folder(data_dir))
    except OSError:
        return []
    return sorted(n for n in names if re.match(r"^\d{4}-\d{2}-\d{2}\.jsonl$", n))


def _day_of(name):
    return date.fromisoformat(name[:10])


def events(data_dir, days=30):
    """The events of the last `days` days, oldest first."""
    since = date.today() - timedelta(days=max(1, int(days)) - 1)
    out = []
    for name in _day_files(data_dir):
        if _day_of(name) < since:
            continue
        try:
            with open(os.path.join(folder(data_dir), name), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        out.append(e)
                    if len(out) >= MAX_EVENTS_READ:
                        return out
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- summary

def _pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def _avg(values):
    return round(sum(values) / len(values), 1) if values else 0


def summary(data_dir, days=30):
    """Totals for Settings > Diagnostics and the exported report."""
    evs = events(data_dir, days)
    by = defaultdict(list)
    for e in evs:
        by[e.get("kind")].append(e)
    replies, steps, tools = by["reply"], by["step"], by["tool"]

    def n(e, k):
        v = e.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    prompt = sum(n(s, "promptTokens") for s in steps); output = sum(n(s, "outputTokens") for s in steps)
    cached = sum(n(s, "cachedTokens") for s in steps)
    top = [r for r in replies if not r.get("subtask")]
    totals = {
        "replies": len(top), "subtaskReplies": len(replies) - len(top), "steps": len(steps),
        "stepsPerReply": _avg([n(r, "steps") for r in top]),
        "replySecondsMedian": round(_pct([n(r, "ms") for r in top], 50) / 1000, 1),
        "replySecondsP90": round(_pct([n(r, "ms") for r in top], 90) / 1000, 1),
        "firstTokenSecondsMedian": round(_pct([n(s, "ttftMs") for s in steps if n(s, "ttftMs")], 50) / 1000, 2),
        "promptTokens": prompt, "outputTokens": output, "cachedTokens": cached,
        "cachedShare": round(cached / prompt, 3) if prompt else 0,
        "tokensPerReply": round((prompt + output) / len(top)) if top else 0,
        "outcomes": dict(Counter(r.get("outcome", "?") for r in replies)),
        "stepLimitHits": sum(1 for r in replies if r.get("outcome") == "stepLimit"),
        "retries": dict(Counter(str(e.get("code", "?")) for e in by["retry"])),
        "messagesWhileRunning": sum(n(r, "steered") for r in replies),
        "errors": dict(Counter(e.get("type", "?") for e in by["error"])),
    }

    models = {}
    for name, group in _group(steps, "model").items():
        evals = [(n(s, "outputTokens"), n(s, "evalMs")) for s in group if n(s, "evalMs")]
        rs = [r for r in replies if r.get("model") == name]
        models[name] = {
            "steps": len(group), "replies": len(rs),
            "promptTokens": sum(n(s, "promptTokens") for s in group), "outputTokens": sum(n(s, "outputTokens") for s in group),
            "thinkingTokens": sum(n(s, "thinkingTokens") for s in group),
            "tokensPerSecond": round(sum(o for o, _ in evals) / (sum(ms for _, ms in evals) / 1000), 1) if evals and sum(ms for _, ms in evals) else 0,
            "firstTokenSecondsMedian": round(_pct([n(s, "ttftMs") for s in group if n(s, "ttftMs")], 50) / 1000, 2),
            "toolCallSteps": sum(1 for s in group if n(s, "toolCalls")),
            "errorReplies": sum(1 for r in rs if r.get("outcome") == "error"),
        }

    tool_rows = {}
    for name, group in _group(tools, "name").items():
        ms = [n(t, "ms") for t in group]
        tool_rows[name] = {
            "calls": len(group), "msMedian": _pct(ms, 50), "msP90": _pct(ms, 90),
            "resultTokens": sum(n(t, "resultTokens") for t in group), "resultTokensAvg": _avg([n(t, "resultTokens") for t in group]),
            "errors": sum(1 for t in group if t.get("error")), "denied": sum(1 for t in group if t.get("denied")),
        }

    parts = {k: sum(n(s, k) for s in steps) for k in ("systemTokens", "toolSpecTokens", "userTokens", "assistantTokens", "toolResultTokens", "summaryTokens")}
    approvals = by["approval"]
    approval_rows = {}
    for name, group in _group(approvals, "what").items():
        approval_rows[name] = {"asked": len(group), "allowed": sum(1 for a in group if a.get("allowed")),
                               "waitSecondsMedian": round(_pct([n(a, "waitMs") for a in group], 50) / 1000, 1)}
    comp = by["compaction"]
    compaction = {"count": len(comp), "manual": sum(1 for c in comp if c.get("manual")), "failed": sum(1 for c in comp if not c.get("ok")),
                  "tokensIn": sum(n(c, "inTokens") for c in comp), "tokensOut": sum(n(c, "outTokens") for c in comp)}
    ui = by["ui"]
    window = {"renders": sum(n(u, "renders") for u in ui), "renderMsAvg": round(sum(n(u, "renderMs") for u in ui) / max(1, sum(n(u, "renders") for u in ui)), 1),
              "renderMsMax": max([n(u, "maxMs") for u in ui] or [0]), "longFrames": sum(n(u, "longFrames") for u in ui)}
    daily = defaultdict(lambda: {"replies": 0, "promptTokens": 0, "outputTokens": 0})
    for r in top:
        daily[datetime.fromtimestamp(n(r, "t")).date().isoformat()]["replies"] += 1
    for s in steps:
        d = daily[datetime.fromtimestamp(n(s, "t")).date().isoformat()]
        d["promptTokens"] += n(s, "promptTokens"); d["outputTokens"] += n(s, "outputTokens")
    apps = by["app"]
    return {"days": int(days), "events": len(evs), "since": evs[0]["t"] if evs else None, "totals": totals, "models": models,
            "tools": tool_rows, "requestParts": parts, "approvals": approval_rows, "compaction": compaction, "window": window,
            "daily": dict(sorted(daily.items())), "versions": dict(Counter(a.get("version", "?") for a in apps))}


def _group(items, key):
    out = defaultdict(list)
    for e in items:
        out[str(e.get(key, "?"))].append(e)
    return dict(out)


def report(data_dir, days, app_info):
    """The exported file: the summary plus the raw (already anonymous) events."""
    return {"report": "Open AY Desktop diagnostics", "created": datetime.now().isoformat(timespec="seconds"), "app": clean(app_info),
            "summary": summary(data_dir, days), "events": events(data_dir, days)}
