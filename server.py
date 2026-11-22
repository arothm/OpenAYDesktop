"""Open AY Desktop - lightweight Ollama Cloud desktop chat."""

import argparse
import difflib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import capabilities
import session_actions

APP_NAME = "Open AY Desktop"
APP_VERSION = "1.1.0"
APP_SLUG = "open-ay-desktop"
REPOSITORY = "arothm/OpenAYDesktop"
RELEASES_URL = "https://github.com/%s/releases/latest" % REPOSITORY
UPDATE_API_URL = "https://api.github.com/repos/%s/releases/latest" % REPOSITORY
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
CHATS_DIR = os.path.join(DATA_DIR, "chats")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
VENDOR_DIR = os.path.join(APP_DIR, "vendor")
CLOUD_URL = "https://ollama.com"
DEFAULT_CONFIG = {"apiKey": "", "defaultModel": "", "think": "default"}
VENDOR_FILES = {
    "marked.min.js": {"url": "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js", "type": "application/javascript; charset=utf-8"},
    "purify.min.js": {"url": "https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js", "type": "application/javascript; charset=utf-8"},
    "highlight.min.js": {"url": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js", "type": "application/javascript; charset=utf-8"},
    "github-dark.min.css": {"url": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css", "type": "text/css; charset=utf-8"},
    "github.min.css": {"url": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css", "type": "text/css; charset=utf-8"},
}
LOCAL_FILES = {"/app.js": ("app.js", "application/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
MAX_VENDOR_BYTES = 2_000_000
MAX_REQUEST_BYTES = 100_000_000
APPROVAL_TTL = 300
_last_ping = time.time()
_lock = threading.Lock()
_chat_locks = {}
_chat_locks_guard = threading.Lock()
_approvals = {}
_model_approvals = {}
_approval_lock = threading.Lock()
_vendor_lock = threading.Lock()


# Storage

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            if "defaultModel" not in saved and saved.get("lastModel"):
                saved["defaultModel"] = saved["lastModel"]
            cfg.update(saved)
    except (OSError, ValueError):
        pass
    for legacy in ("mode", "localUrl", "lastModel", "temperature", "systemPrompt"):
        cfg.pop(legacy, None)
    if not cfg.get("apiKey"):
        cfg["apiKeyFromEnv"] = bool(os.environ.get("OLLAMA_API_KEY"))
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    atomic_write_text(CONFIG_PATH, json.dumps({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}, indent=2))


def atomic_write_text(path, content):
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.remove(tmp)
        except OSError: pass


def api_key(cfg):
    return cfg.get("apiKey") or os.environ.get("OLLAMA_API_KEY", "")


def public_config(cfg):
    out = {k: v for k, v in cfg.items() if k != "apiKey"}
    key = cfg.get("apiKey", "")
    out["hasApiKey"] = bool(key) or bool(os.environ.get("OLLAMA_API_KEY"))
    out["apiKeyHint"] = ("…" + key[-4:]) if len(key) > 8 else ""
    out.update({"appName": APP_NAME, "version": APP_VERSION, "repository": REPOSITORY})
    return out


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def chat_path(chat_id):
    if not SAFE_ID.match(chat_id or ""):
        raise ValueError("bad id")
    return os.path.join(CHATS_DIR, chat_id + ".json")


def chat_lock(chat_id):
    chat_path(chat_id)
    with _chat_locks_guard:
        # Reentrant: callers hold the lock across load_chat()/save_chat(), which also take it.
        return _chat_locks.setdefault(chat_id, threading.RLock())


def load_chat(chat_id):
    with chat_lock(chat_id):
        with open(chat_path(chat_id), "r", encoding="utf-8") as f:
            chat = json.load(f)
    if not isinstance(chat, dict):
        raise ValueError("bad chat")
    chat["_path"] = chat_path(chat_id)
    return chat


def save_chat(chat_id, chat):
    if not isinstance(chat, dict):
        raise ValueError("bad chat")
    chat = {k: v for k, v in chat.items() if not k.startswith("_")}
    chat["id"] = chat_id
    chat.setdefault("capabilities", dict(capabilities.DEFAULT_POLICY))
    os.makedirs(CHATS_DIR, exist_ok=True)
    with chat_lock(chat_id):
        atomic_write_text(chat_path(chat_id), json.dumps(chat, ensure_ascii=False))


def list_chats():
    os.makedirs(CHATS_DIR, exist_ok=True)
    out = []
    for name in os.listdir(CHATS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, name), "r", encoding="utf-8") as f:
                c = json.load(f)
            out.append({"id": c["id"], "title": c.get("title", "Untitled"), "updated": c.get("updated", 0), "model": c.get("model", "")})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


# Folder tools

MAX_READ = 200_000
MAX_LIST = 500
MAX_HITS = 200
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs"}


class ToolError(Exception):
    pass


def folder_roots(cfg):
    return capabilities.grants(cfg)


def _inside(path, root):
    return capabilities.inside(path, root)


def resolve_path(cfg, raw, need_write=False):
    try:
        return capabilities.resolve(cfg, raw, write=need_write)
    except capabilities.CapabilityError as e:
        raise ToolError(str(e))


def saved_tool_config(chat_id):
    try:
        return load_chat(chat_id)
    except (OSError, ValueError):
        raise ToolError("Save the chat before using folder tools, then try again.")


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f TB" % n


def tool_list_directory(cfg, path="", **_):
    roots = folder_roots(cfg)
    if not path:
        if len(roots) != 1:
            return "Shared folders:\n" + "\n".join("%s/  (%s)" % (r["path"], "read & write" if r["write"] else "read-only") for r in roots)
        path = roots[0]["path"]
    p = resolve_path(cfg, path)
    if not os.path.isdir(p):
        raise ToolError("Not a directory: " + p)
    entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for e in entries[:MAX_LIST]:
        try: lines.append(e.name + "/" if e.is_dir() else "%s  (%s)" % (e.name, _human(e.stat().st_size)))
        except OSError: lines.append(e.name)
    more = "\n... %d more entries" % (len(entries) - MAX_LIST) if len(entries) > MAX_LIST else ""
    return "Contents of %s:\n%s%s" % (p, "\n".join(lines) or "(empty)", more)


def tool_read_file(cfg, path="", start_line=1, max_lines=2000, **_):
    p = resolve_path(cfg, path)
    if not os.path.isfile(p):
        raise ToolError("Not a file: " + p)
    with open(p, "rb") as f:
        head = f.read(8192)
    if b"\x00" in head:
        return "%s is a binary file (%s); it can't be read as text." % (p, _human(os.path.getsize(p)))
    start = max(1, int(start_line or 1)); count = max(1, min(int(max_lines or 2000), 5000))
    out, size, total = [], 0, 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            total = i
            if i < start or len(out) >= count or size > MAX_READ: continue
            out.append(line); size += len(line)
    text = "".join(out); end = start + len(out) - 1
    note = "" if (start == 1 and end >= total) else "\n[lines %d-%d of %d]" % (start, end, total)
    return text + note


def tool_search_files(cfg, path="", pattern="*", contains="", **_):
    import fnmatch
    roots = [resolve_path(cfg, path)] if path else [r["path"] for r in folder_roots(cfg)]
    if not roots:
        raise ToolError("No folders are shared.")
    pat = (pattern or "*").lower(); needle = (contains or "").lower(); hits = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            safe_dirs = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if d in SKIP_DIRS or os.path.islink(full): continue
                try:
                    if _inside(resolve_path(cfg, full), root): safe_dirs.append(d)
                except (OSError, ToolError): continue
            dirnames[:] = safe_dirs
            for fn in filenames:
                full = os.path.join(dirpath, fn); rel = os.path.relpath(full, root).replace("\\", "/").lower()
                if not (fnmatch.fnmatch(fn.lower(), pat) or fnmatch.fnmatch(rel, pat)): continue
                try:
                    full = resolve_path(cfg, full)
                    if not os.path.isfile(full): continue
                    if needle:
                        if os.path.getsize(full) > 5_000_000: continue
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            for n, line in enumerate(f, 1):
                                if needle in line.lower():
                                    hits.append("%s:%d: %s" % (full, n, line.strip()[:200]))
                                    if len(hits) >= MAX_HITS: break
                    else: hits.append(full)
                except (OSError, ToolError): continue
                if len(hits) >= MAX_HITS: return "\n".join(hits) + "\n... (stopped at %d results)" % MAX_HITS
    return "\n".join(hits) or "No matches."


def tool_write_file(cfg, path="", content="", **_):
    p = resolve_path(cfg, path, need_write=True)
    if not isinstance(content, str): raise ToolError("content must be a string")
    existed = os.path.exists(p)
    if existed and not os.path.isfile(p): raise ToolError("Not a file: " + p)
    os.makedirs(os.path.dirname(p), exist_ok=True); atomic_write_text(p, content)
    return "%s %s (%s)." % ("Overwrote" if existed else "Created", p, _human(len(content.encode("utf-8"))))


TOOLS = {"list_directory": tool_list_directory, "read_file": tool_read_file, "search_files": tool_search_files, "write_file": tool_write_file}


def tool_specs(cfg):
    roots = folder_roots(cfg); pol = capabilities.policy(cfg)
    where = "; ".join("%s (%s)" % (r["path"], "read & write" if r["write"] else "read-only") for r in roots)
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": required}}}
    path = {"type": "string", "description": "Absolute path inside a shared folder"}
    specs = []
    if roots:
        specs = [
            fn("list_directory", "List files and subfolders of a folder. Shared folders: " + where, {"path": path}, ["path"]),
            fn("read_file", "Read a text file from a shared folder.", {"path": path, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]),
            fn("search_files", "Find files by name glob and optionally by text. Omit path to search all shared folders.", {"path": path, "pattern": {"type": "string"}, "contains": {"type": "string"}}, []),
        ]
    if any(r["write"] for r in roots):
        specs.append(fn("write_file", "Create or overwrite a text file in a writable shared folder. User review depends on session edit mode.", {"path": path, "content": {"type": "string"}}, ["path", "content"]))
    if pol["fileManage"]:
        specs.extend([
            fn("create_folder", "Create a new folder in a writable shared folder. Never overwrites.", {"path": path}, ["path"]),
            fn("copy_path", "Copy a file/folder to a new path. Both paths must be in writable shared folders.", {"source": path, "destination": path}, ["source", "destination"]),
            fn("move_path", "Move or rename a file/folder. Both paths must be in writable shared folders.", {"source": path, "destination": path}, ["source", "destination"]),
        ])
    if pol["deleteFiles"]:
        specs.extend([
            fn("delete_path", "Move a file/folder to recoverable application trash; never permanently deletes. Returns a trash ID.", {"path": path}, ["path"]),
            fn("restore_path", "Restore an app-trash item by its exact returned trash ID if the original location is free.", {"trash_id": {"type": "string"}}, ["trash_id"]),
        ])
    if pol["modelControl"] != "fixed":
        specs.append(fn("set_session_model",
                        "Switch this session to another Ollama Cloud model. Only call this when the user explicitly asks to change "
                        "or switch the model. Use the exact model name. After calling it, stop: the new model continues the reply."
                        + (" The user must approve the change." if pol["modelControl"] == "ask" else ""),
                        {"model": {"type": "string", "description": "Exact Ollama Cloud model name, e.g. gpt-oss:120b"},
                         "reason": {"type": "string", "description": "Short reason, quoting what the user asked for"}},
                        ["model", "reason"]))
    return specs, where


def tool_arguments(args):
    if not isinstance(args, dict):
        try: args = json.loads(args) if args else {}
        except ValueError: raise ToolError("Arguments must be a JSON object")
    if not isinstance(args, dict): raise ToolError("Arguments must be a JSON object")
    return args


def run_tool(cfg, name, args):
    args = tool_arguments(args)
    if name in capabilities.OPERATIONS:
        try: return capabilities.OPERATIONS[name](cfg, **args)
        except (capabilities.CapabilityError, OSError) as e: raise ToolError(str(e))
    fn = TOOLS.get(name)
    if not fn: raise ToolError("Unknown tool: %s" % name)
    try: return fn(cfg, **args)
    except TypeError as e: raise ToolError("Bad arguments: %s" % e)
    except OSError as e: raise ToolError(str(e))


def prepare_write(chat_id, name, args):
    if name != "write_file": raise ToolError("Only write_file needs approval.")
    args = tool_arguments(args); cfg = saved_tool_config(chat_id); content = args.get("content", "")
    if not isinstance(content, str): raise ToolError("content must be a string")
    try:
        path = resolve_path(cfg, args.get("path"), need_write=True); exists = os.path.exists(path)
        if exists and not os.path.isfile(path): raise ToolError("Not a file: " + path)
        old = ""
        if exists:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f: old = f.read()
    except OSError as e: raise ToolError(str(e))
    diff = ""
    if exists:
        diff = "".join(difflib.unified_diff(old.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=path + " (current)", tofile=path + " (replacement)"))
        if not diff: diff = "No textual difference; approving will still replace the file."
    token = secrets.token_urlsafe(32); now = time.monotonic()
    with _approval_lock:
        for key, record in list(_approvals.items()):
            if record["expires"] <= now or record["chat_id"] == chat_id: _approvals.pop(key, None)
        _approvals[token] = {"chat_id": chat_id, "path": path, "content": content, "expires": now + APPROVAL_TTL}
    return {"token": token, "path": path, "content": content, "exists": exists, "diff": diff,
            "currentBytes": len(old.encode("utf-8")) if exists else 0, "newBytes": len(content.encode("utf-8")), "expiresIn": APPROVAL_TTL}


def cancel_write(chat_id, token):
    """Withdraw a pending write or model-change approval belonging to this chat."""
    with _approval_lock:
        for pending in (_approvals, _model_approvals):
            record = pending.get(token or "")
            if record and record["chat_id"] == chat_id: pending.pop(token, None)


def run_approved_write(chat_id, token):
    with _approval_lock: record = _approvals.pop(token or "", None)
    if not record or record["chat_id"] != chat_id or record["expires"] <= time.monotonic():
        raise ToolError("Write approval is missing, expired, or already used. Please review the write again.")
    cfg = saved_tool_config(chat_id); path = resolve_path(cfg, record["path"], need_write=True)
    if os.path.normcase(path) != os.path.normcase(record["path"]): raise ToolError("The approved path changed. Please review it again.")
    return run_tool(cfg, "write_file", {"path": path, "content": record["content"]})


PICK_FOLDER_SCRIPT = ("import tkinter as t, tkinter.filedialog as d\nr=t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                      "print(d.askdirectory(title='Choose a folder to share with Open AY Desktop', mustexist=True) or '')\n")


# Ollama Cloud

def upstream(path, body=None, timeout=600):
    headers = {}; key = api_key(load_config())
    if key: headers["Authorization"] = "Bearer " + key
    data = None
    if body is not None: data = json.dumps(body).encode("utf-8"); headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(CLOUD_URL + path, data=data, headers=headers, method="POST" if data is not None else "GET"), timeout=timeout)


def fetch_json(url, headers=None, timeout=10):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_models():
    models, errors = [], []
    try:
        key = api_key(load_config()); hdrs = {"Authorization": "Bearer " + key} if key else {}
        for m in fetch_json(CLOUD_URL + "/api/tags", hdrs).get("models", []): models.append({"name": m["name"], "cloud": True, "installed": True})
    except Exception as e: errors.append("Ollama Cloud: %s" % e)
    return models, errors


def model_capabilities(name):
    """Capabilities Ollama Cloud reports for a model (e.g. tools, vision), or None if unknown."""
    try:
        with upstream("/api/show", {"model": name}, timeout=20) as r:
            caps = json.loads(r.read().decode("utf-8")).get("capabilities")
        return [str(c) for c in caps] if isinstance(caps, list) else None
    except Exception:  # noqa: BLE001 - capability lookup is advisory when the service can't answer
        return None


def check_model_switch(chat, requested, lookup=None):
    """Refuse switches that would break the session; return (capabilities, warnings)."""
    caps = (lookup or model_capabilities)(requested); warnings = []
    if caps is None:
        warnings.append("Couldn't confirm what %s supports." % requested)
        return [], warnings
    if "completion" not in caps:
        raise ToolError("%s can't be used for chat." % requested)
    if any(m.get("images") for m in chat.get("messages") or [] if isinstance(m, dict)) and "vision" not in caps:
        raise ToolError("This session contains images, and %s doesn't support images." % requested)
    if "tools" not in caps:
        warnings.append("%s doesn't support tools, so folder access and other session tools are unavailable while it is selected." % requested)
    return caps, warnings


def change_session_model(chat_id, args, approval_token=""):
    """set_session_model: validate, ask for approval when the session policy says so, then switch.

    Network checks run before the chat lock is taken; the policy is re-checked under the lock.
    """
    args = tool_arguments(args)
    requested = str(args.get("model", "")).strip()
    if not requested or len(requested) > 200: raise ToolError("An exact cloud model name is required.")
    reason = str(args.get("reason", "")).strip()[:300]
    chat = saved_tool_config(chat_id); mode = capabilities.policy(chat)["modelControl"]
    if mode == "fixed": raise ToolError("Model control is fixed for this session.")
    previous = chat.get("model", "")
    if requested == previous:
        return {"content": "This session already uses %s." % requested, "model": requested, "previous": previous, "needsApproval": False}
    models, errors = get_models()
    if requested not in [m["name"] for m in models]:
        raise ToolError("Model is not currently available in Ollama Cloud: %s. %s" % (requested, "; ".join(errors)))
    caps, warnings = check_model_switch(chat, requested)
    if mode == "ask":
        now = time.monotonic()
        with _approval_lock:
            record = _model_approvals.pop(approval_token or "", None)
            if not record or record["chat_id"] != chat_id or record["model"] != requested or record["expires"] <= now:
                for key, old in list(_model_approvals.items()):
                    if old["expires"] <= now or old["chat_id"] == chat_id: _model_approvals.pop(key, None)
                token = secrets.token_urlsafe(32)
                _model_approvals[token] = {"chat_id": chat_id, "model": requested, "expires": now + APPROVAL_TTL}
                return {"needsApproval": True, "approvalToken": token, "model": requested, "previous": previous,
                        "reason": reason, "capabilities": caps, "warnings": warnings}
    with chat_lock(chat_id):
        chat = load_chat(chat_id)
        if capabilities.policy(chat)["modelControl"] == "fixed": raise ToolError("Model control is fixed for this session.")
        previous = chat.get("model", ""); chat["model"] = requested; chat["updated"] = int(time.time() * 1000); save_chat(chat_id, chat)
    content = "Model changed from %s to %s. It answers from the next step of this reply onward." % (previous or "none", requested)
    if reason: content += " Reason: %s" % reason
    if warnings: content += " Note: " + " ".join(warnings)
    return {"content": content, "model": requested, "previous": previous, "needsApproval": False, "warnings": warnings}


def http_error_text(e):
    try:
        body = e.read().decode("utf-8", "replace"); msg = json.loads(body).get("error", body)
    except Exception: msg = str(e)
    if e.code == 401: msg += "  - Check your Ollama API key in Settings (ollama.com/settings/keys)."
    return msg


# Updates

def version_tuple(value):
    match = re.fullmatch(r"[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", str(value or "").strip())
    if not match: raise ValueError("Release tag must contain a semantic version such as v1.2.0")
    return tuple(int(x) for x in match.groups())


def check_for_updates(fetcher=fetch_json):
    try:
        release = fetcher(UPDATE_API_URL, {"Accept": "application/vnd.github+json", "User-Agent": "Open-AY-Desktop/%s" % APP_VERSION, "X-GitHub-Api-Version": "2022-11-28"}, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": True, "status": "no-release", "currentVersion": APP_VERSION, "updateAvailable": False, "releaseUrl": RELEASES_URL, "message": "No published release yet. This installation is version %s." % APP_VERSION}
        raise RuntimeError("GitHub update check failed (HTTP %d)." % e.code)
    except Exception as e: raise RuntimeError("Could not check GitHub for updates: %s" % e)
    if not isinstance(release, dict): raise RuntimeError("GitHub returned an invalid release response.")
    latest = str(release.get("tag_name") or release.get("name") or "").strip()
    try: available = version_tuple(latest) > version_tuple(APP_VERSION)
    except ValueError: raise RuntimeError("The latest release does not have a valid vX.Y.Z version tag.")
    url = release.get("html_url") or RELEASES_URL
    if not isinstance(url, str) or not url.startswith("https://github.com/%s/releases/" % REPOSITORY): url = RELEASES_URL
    return {"ok": True, "status": "available" if available else "current", "currentVersion": APP_VERSION, "latestVersion": latest.lstrip("vV"), "releaseName": str(release.get("name") or latest), "releaseUrl": url, "updateAvailable": available, "message": ("Version %s is available." % latest.lstrip("vV")) if available else "Open AY Desktop is up to date."}


# Local assets

def vendor_file(name):
    info = VENDOR_FILES[name]; path = os.path.join(VENDOR_DIR, name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f: return f.read(), info["type"]
    with _vendor_lock:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as f: return f.read(), info["type"]
        req = urllib.request.Request(info["url"], headers={"User-Agent": "Open-AY-Desktop/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if not r.geturl().lower().startswith("https://"): raise ValueError("Refusing a non-HTTPS asset redirect")
            data = r.read(MAX_VENDOR_BYTES + 1)
        if not data or len(data) > MAX_VENDOR_BYTES: raise ValueError("Downloaded asset has an unexpected size")
        os.makedirs(VENDOR_DIR, exist_ok=True); tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
        try:
            with open(tmp, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try: os.remove(tmp)
            except OSError: pass
        return data, info["type"]


# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenAYDesktop/1.1"
    def log_message(self, fmt, *args): pass
    def _json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8"); self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(data)
    def _body(self):
        try: n = int(self.headers.get("Content-Length") or 0)
        except ValueError: raise ValueError("bad content length")
        if n < 0 or n > MAX_REQUEST_BYTES: raise ValueError("request is too large")
        obj = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if not isinstance(obj, dict): raise ValueError("JSON body must be an object")
        return obj
    def _same_origin(self):
        host = self.headers.get("Host", "")
        if not re.match(r"^(127\.0\.0\.1|localhost)(:\d+)?$", host): return False
        origin = self.headers.get("Origin"); return origin is None or origin == "http://" + host
    def _bytes(self, data, content_type, cache="no-store", csp=False):
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache); self.send_header("X-Content-Type-Options", "nosniff")
        if csp:
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        global _last_ping
        if not self._same_origin(): return self._json({"error": "forbidden"}, 403)
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f: return self._bytes(f.read(), "text/html; charset=utf-8", csp=True)
        if p in LOCAL_FILES:
            name, content_type = LOCAL_FILES[p]
            with open(os.path.join(APP_DIR, name), "rb") as f: return self._bytes(f.read(), content_type)
        if p.startswith("/vendor/"):
            name = p.rsplit("/", 1)[1]
            if name not in VENDOR_FILES: return self._json({"error": "not found"}, 404)
            try: data, content_type = vendor_file(name)
            except Exception as e: return self._json({"error": "Local library could not be loaded: %s" % e}, 503)
            return self._bytes(data, content_type, "public, max-age=31536000, immutable")
        if p == "/api/ping": _last_ping = time.time(); self._json({"ok": True})
        elif p == "/api/info": self._json({"app": APP_SLUG, "name": APP_NAME, "version": APP_VERSION, "dir": APP_DIR})
        elif p == "/api/config": self._json(public_config(load_config()))
        elif p == "/api/models":
            models, errors = get_models(); self._json({"models": models, "errors": errors})
        elif p == "/api/chats": self._json(list_chats())
        elif p.startswith("/api/chats/"):
            try: self._json(load_chat(p.rsplit("/", 1)[1]))
            except (OSError, ValueError): self._json({"error": "not found"}, 404)
        else: self._json({"error": "not found"}, 404)
    def do_PUT(self):
        if not self._same_origin(): return self._json({"error": "forbidden"}, 403)
        p = self.path.split("?")[0]
        if p.startswith("/api/chats/"):
            try: save_chat(p.rsplit("/", 1)[1], self._body()); self._json({"ok": True})
            except ValueError as e: self._json({"error": str(e) or "bad request"}, 400)
            except OSError as e: self._json({"error": "Could not save chat: %s" % e}, 500)
        else: self._json({"error": "not found"}, 404)
    def do_DELETE(self):
        if not self._same_origin(): return self._json({"error": "forbidden"}, 403)
        p = self.path.split("?")[0]
        if p.startswith("/api/chats/"):
            try:
                chat_id = p.rsplit("/", 1)[1]
                with chat_lock(chat_id):
                    try: os.remove(chat_path(chat_id))
                    except FileNotFoundError: pass
            except (OSError, ValueError): pass
            self._json({"ok": True})
        else: self._json({"error": "not found"}, 404)
    def do_POST(self):
        if not self._same_origin(): return self._json({"error": "forbidden"}, 403)
        p = self.path.split("?")[0]
        try: body = self._body()
        except ValueError as e: return self._json({"error": str(e) or "bad request"}, 400)
        if p == "/api/config":
            with _lock:
                cfg = load_config()
                for k in ("apiKey", "defaultModel", "think"):
                    if k in body: cfg[k] = body[k]
                if body.get("clearApiKey"): cfg["apiKey"] = ""
                save_config(cfg)
            self._json(public_config(load_config()))
        elif p == "/api/chat": self._proxy_chat(body)
        elif p == "/api/updates/check":
            try: self._json(check_for_updates())
            except RuntimeError as e: self._json({"error": str(e)}, 502)
        elif p == "/api/tool/prepare":
            try: self._json({"approval": prepare_write(body.get("chatId", ""), body.get("name"), body.get("arguments") or {})})
            except ToolError as e: self._json({"error": str(e)}, 400)
        elif p == "/api/tool/cancel":
            cancel_write(body.get("chatId", ""), body.get("approvalToken", "")); self._json({"ok": True})
        elif p == "/api/tool": self._tool(body)
        elif p == "/api/session/rename":
            try:
                chat_id = body.get("chatId", "")
                with chat_lock(chat_id):
                    chat = load_chat(chat_id); result = session_actions.rename(chat, body.get("title")); save_chat(chat_id, chat)
                self._json({"content": result, "title": chat["title"]})
            except (OSError, ValueError) as e: self._json({"error": str(e)}, 400)
        elif p == "/api/pick-folder":
            try:
                exe = sys.executable.replace("python.exe", "pythonw.exe")
                r = subprocess.run([exe if os.path.exists(exe) else sys.executable, "-c", PICK_FOLDER_SCRIPT], capture_output=True, text=True, timeout=600)
                self._json({"path": os.path.normpath(r.stdout.strip()) if r.stdout.strip() else ""})
            except Exception as e: self._json({"error": "Folder picker failed: %s" % e}, 500)
        else: self._json({"error": "not found"}, 404)

    def _tool(self, body):
        chat_id = body.get("chatId", ""); name = body.get("name"); args = body.get("arguments") or {}
        try:
            if name == "set_session_model": return self._json(self.change_model(chat_id, args, body.get("approvalToken", "")))
            if name == "write_file":
                chat = saved_tool_config(chat_id)
                out = run_tool(chat, name, args) if capabilities.policy(chat)["editMode"] == "auto" else run_approved_write(chat_id, body.get("approvalToken", ""))
            else: out = run_tool(saved_tool_config(chat_id), name, args)
            self._json({"content": out})
        except (ToolError, ValueError) as e: self._json({"content": "Error: %s" % e, "error": True})

    def change_model(self, chat_id, args, approval_token=""):
        return change_session_model(chat_id, args, approval_token)

    def _proxy_chat(self, req):
        chat_id = req.pop("chatId", ""); use_tools = req.pop("toolsEnabled", True); req.pop("folders", None); req["stream"] = True
        if not SAFE_ID.match(chat_id): return self._json({"error": "A valid saved chat ID is required."}, 400)
        if not use_tools:  # model without tool support (or tools switched off): send no tools at all
            return self._proxy_stream("/api/chat", req)
        try: fcfg = saved_tool_config(chat_id)
        except ToolError as e: return self._json({"error": str(e)}, 400)
        specs, where = tool_specs(fcfg)
        if specs:
            req["tools"] = specs
            note = "You may use only the tools supplied for this session. Use absolute paths and never guess file contents."
            if folder_roots(fcfg): note += " Shared folders: %s. File operations are limited by the saved session policy. Deletion is recoverable app trash, not permanent deletion." % where
            if any(s["function"]["name"] == "set_session_model" for s in specs):
                note += (" Call set_session_model only when the user explicitly asks to change or switch this session's model;"
                         " never switch on your own initiative. After calling it, stop and write nothing more:"
                         " the new model will continue this reply and handle the rest of the user's request.")
            msgs = req.get("messages") or []
            if msgs and msgs[0].get("role") == "system": msgs[0] = dict(msgs[0], content=msgs[0]["content"] + "\n\n" + note)
            else: msgs.insert(0, {"role": "system", "content": note})
            req["messages"] = msgs
        self._proxy_stream("/api/chat", req)

    def _proxy_stream(self, path, body):
        if not api_key(load_config()): return self._json({"error": "Add your Ollama API key in Settings to use Ollama Cloud."}, 400)
        try: r = upstream(path, body)
        except urllib.error.HTTPError as e: return self._json({"error": http_error_text(e)}, e.code)
        except Exception as e: return self._json({"error": "Could not reach Ollama Cloud: %s" % e}, 502)
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "close"); self.end_headers()
        try:
            with r:
                while True:
                    line = r.readline()
                    if not line: break
                    self.wfile.write(line); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        except Exception as e:
            try: self.wfile.write((json.dumps({"error": str(e)}) + "\n").encode())
            except OSError: pass
        self.close_connection = True


# Launcher

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3); return s.connect_ex(("127.0.0.1", port)) == 0


def running_dir(port):
    try:
        info = fetch_json("http://127.0.0.1:%d/api/info" % port, timeout=2)
        return os.path.normcase(info.get("dir", "")) if info.get("app") in ("ollama-desk", APP_SLUG) else None
    except Exception: return None


def open_window(url):
    if sys.platform == "win32":
        candidates = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"), os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"), os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"), os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")]
        profile = os.path.join(DATA_DIR, "window-profile")
        for exe in candidates:
            if os.path.isfile(exe):
                subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + profile, "--window-size=1200,820", "--no-first-run", "--no-default-browser-check"]); return
    webbrowser.open(url)


def watchdog(httpd, idle_seconds):
    while True:
        time.sleep(5)
        if time.time() - _last_ping > idle_seconds: httpd.shutdown(); return


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8765); ap.add_argument("--no-window", action="store_true"); ap.add_argument("--stay", action="store_true"); args = ap.parse_args()
    port = args.port
    while port_in_use(port):
        if running_dir(port) == os.path.normcase(APP_DIR):
            if not args.no_window: open_window("http://127.0.0.1:%d/" % port)
            return
        port += 1
    url = "http://127.0.0.1:%d/" % port; os.makedirs(CHATS_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler); httpd.daemon_threads = True
    if not args.no_window:
        open_window(url)
        if not args.stay:
            global _last_ping
            _last_ping = time.time() + 30; threading.Thread(target=watchdog, args=(httpd, 180), daemon=True).start()
    if sys.stdout: print("%s %s running at %s" % (APP_NAME, APP_VERSION, url))
    httpd.serve_forever()


if __name__ == "__main__": main()
