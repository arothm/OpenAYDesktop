"""Server-enforced session capabilities and safe file-management operations."""

import json
import os
import re
import shutil
import time
import uuid

# A chat stores two settings (Session settings); everything else follows from them:
#   mode: what needs the user's approval
#     "manual" - every file write, file operation (create, copy, move, delete, restore), command, commit, push
#                and model change asks first
#     "auto"   - file writes and file operations happen without asking; commands not on the allowed list,
#                commits, pushes and model changes ask. After the chat reads web content, every command asks.
#     "bypass" - nothing asks, web content or not
#   webSearch: the chat may search the web and open pages (any address, without asking)
# Every top-level chat can use files (in folders with Allow edits), Git up to push, commands (in folders with
# Allow edits), subtasks, instruction files and memory. A subtask gets read-only Git, no commands, no subtasks,
# no memory and a fixed model.
DEFAULT_POLICY = {"mode": "manual", "webSearch": False}
MODES = ("manual", "auto", "bypass")
GIT_MODES = ("off", "read", "commit", "push")
EDIT_MODES = {"review", "auto"}
# File operations that ask first in Manual mode (file writes have their own review).
GATED_FILE_OPS = {"create_folder", "copy_path", "move_path", "delete_path", "restore_path"}
MODEL_MODES = {"allowed", "ask", "fixed"}
MAX_COPY_BYTES = 500_000_000


class CapabilityError(Exception):
    pass


def mode_of(saved):
    """The chat's mode. Chats saved before modes existed: auto-edit maps to Auto, anything else to Manual."""
    saved = saved if isinstance(saved, dict) else {}
    if saved.get("mode") in MODES:
        return saved["mode"]
    return "auto" if saved.get("editMode") == "auto" else "manual"


def policy(chat):
    """What this chat may do and what asks first, from its mode and web search setting (see the top of this file).

    Keys: mode, webSearch, editMode ("review" = every write asks), fileManage, deleteFiles, git, commands,
    subtasks, instructions, memory, modelControl ("ask" | "allowed" | "fixed"), untrusted (read web content),
    askFileOps (file operations ask), askEveryCommand (even allowed-list commands ask), askGit (commits and pushes
    ask), askMemory (saving memory asks).
    """
    writable = any(isinstance(f, dict) and f.get("write") for f in chat.get("folders") or [])
    saved = chat.get("capabilities") if isinstance(chat.get("capabilities"), dict) else {}
    child = bool(chat.get("parentId"))
    mode = mode_of(saved)
    untrusted = bool(chat.get("untrusted"))
    bypass = mode == "bypass"
    return {
        "mode": mode, "webSearch": bool(saved.get("webSearch")), "untrusted": untrusted,
        "editMode": "review" if mode == "manual" else "auto",
        "fileManage": writable, "deleteFiles": writable,
        "git": "read" if child else "push",  # subtasks never commit or push
        "commands": writable and not child,  # needs a writable folder; never in subtasks
        "subtasks": not child,  # one level only
        "instructions": True,
        "memory": not child,  # helpers neither read nor save memory
        "modelControl": "fixed" if child else ("allowed" if bypass else "ask"),
        "askFileOps": mode == "manual",
        # Manual: every command. Auto: commands off the allowed list, and all of them once web content was read.
        "askEveryCommand": mode == "manual" or (mode == "auto" and untrusted),
        "askGit": not bypass,
        "askMemory": untrusted and not bypass,  # a web page must not plant lasting instructions
    }


def stored(saved, changes=None):
    """The two settings a chat stores, from what it had (any older format) and validated changes."""
    saved = saved if isinstance(saved, dict) else {}
    changes = changes or {}
    mode = mode_of(changes) if ("mode" in changes or "editMode" in changes) else mode_of(saved)
    return {"mode": mode, "webSearch": bool(changes.get("webSearch", saved.get("webSearch", False)))}


def child_capabilities(parent):
    """A subtask starts with its parent's mode and web search (its other limits come from policy)."""
    p = policy(parent)
    return {"mode": p["mode"], "webSearch": p["webSearch"]}


def grants(chat):
    result = []
    for folder in chat.get("folders") or []:
        if not isinstance(folder, dict):
            continue
        raw = str(folder.get("path", "")).strip().strip('"')
        if raw:
            path = os.path.realpath(os.path.expanduser(raw))
            if os.path.isdir(path):
                result.append({"path": path, "write": bool(folder.get("write"))})
    return result


def inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def resolve(chat, raw, write=False):
    shared = grants(chat)
    if not shared:
        raise CapabilityError("No folders are shared with this session.")
    raw = str(raw or "").strip().strip('"')
    if not raw:
        raise CapabilityError("path is required")
    if not os.path.isabs(raw):
        if len(shared) != 1:
            raise CapabilityError("Use an absolute path inside a shared folder.")
        raw = os.path.join(shared[0]["path"], raw)
    path = os.path.realpath(raw)
    matches = [g for g in shared if inside(path, g["path"])]
    if not matches:
        raise CapabilityError("Access denied: path is outside the shared folders.")
    if write and not max(matches, key=lambda g: len(g["path"]))["write"]:
        raise CapabilityError("The target folder is read-only.")
    if write and any(part.lower() == ".git" for part in path.split(os.sep)):
        # Git's own files (config, hooks) can make Git run programs: changing them needs a command, not a file tool.
        raise CapabilityError("Files inside a .git folder can't be changed with file tools.")
    return path


def require(chat, capability):
    if not policy(chat).get(capability):
        names = {"fileManage": "file/folder creation, copy, move and rename",
                 "deleteFiles": "recoverable deletion"}
        raise CapabilityError("This session can't do %s: share a folder with Allow edits first." % names.get(capability, capability))


def destination(chat, raw):
    path = resolve(chat, raw, write=True)
    if os.path.lexists(path):
        raise CapabilityError("Destination already exists; refusing to overwrite: " + path)
    return path


def tree_size(path):
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
            if total > MAX_COPY_BYTES:
                raise CapabilityError("Copy exceeds the 500 MB safety limit.")
    return total


def create_folder(chat, path="", **_):
    require(chat, "fileManage")
    target = destination(chat, path)
    os.makedirs(target)
    return "Created folder: " + target


def copy_path(chat, source="", destination="", **_):
    require(chat, "fileManage")
    src = resolve(chat, source, write=True)
    if not os.path.lexists(src):
        raise CapabilityError("Source does not exist: " + src)
    dst = globals()["destination"](chat, destination)
    if os.path.isdir(src) and not os.path.islink(src):
        if inside(dst, src):
            raise CapabilityError("Cannot copy a folder into itself.")
        tree_size(src)
        shutil.copytree(src, dst, symlinks=True)
    else:
        if not os.path.islink(src) and os.path.getsize(src) > MAX_COPY_BYTES:
            raise CapabilityError("Copy exceeds the 500 MB safety limit.")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
    return "Copied %s to %s" % (src, dst)


def move_path(chat, source="", destination="", **_):
    require(chat, "fileManage")
    src = resolve(chat, source, write=True)
    if not os.path.lexists(src):
        raise CapabilityError("Source does not exist: " + src)
    if any(os.path.normcase(src) == os.path.normcase(g["path"]) for g in grants(chat)):
        raise CapabilityError("Cannot move a shared root itself.")
    dst = globals()["destination"](chat, destination)
    if os.path.isdir(src) and inside(dst, src):
        raise CapabilityError("Cannot move a folder into itself.")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return "Moved/renamed %s to %s" % (src, dst)


# ---------------------------------------------------------------- app trash
# data/trash holds two kinds of items, each as "<id>.item" plus a "<id>.json" manifest:
#   "deleted" - something delete_path moved out of a shared folder
#   "version" - the previous content of a file, kept before every overwrite or edit
# Nothing here is permanent until the user empties it from the Trash screen.

TRASH_ID = re.compile(r"^\d{1,12}-[0-9a-f]{10}$")
MAX_VERSION_BYTES = 20_000_000


def trash_root(chats_dir):
    path = os.path.realpath(os.path.join(chats_dir, "..", "trash"))
    os.makedirs(path, exist_ok=True)
    return path


def trash_dir(chat):
    """The app trash for a saved chat, or None for an unsaved/plain policy dict."""
    return trash_root(os.path.dirname(chat["_path"])) if chat.get("_path") else None


def _size(path):
    if os.path.isdir(path) and not os.path.islink(path):
        return sum(os.path.getsize(os.path.join(b, n)) for b, _, files in os.walk(path) for n in files)
    return os.path.getsize(path) if os.path.exists(path) else 0


def _store(trash, src, kind, chat_id, copy=False):
    ident = "%s-%s" % (int(time.time()), uuid.uuid4().hex[:10])
    stored_name = ident + ".item"
    manifest = {"id": ident, "kind": kind, "originalPath": src, "storedName": stored_name,
                "deletedAt": int(time.time()), "chatId": chat_id or "", "size": _size(src),
                "isDir": os.path.isdir(src) and not os.path.islink(src)}
    target = os.path.join(trash, stored_name)
    shutil.copy2(src, target) if copy else shutil.move(src, target)
    with open(os.path.join(trash, ident + ".json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return ident


def keep_version(chat, path):
    """Copy a file's current content into the trash before it is replaced. Returns the trash ID or None."""
    trash = trash_dir(chat)
    if not trash or not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > MAX_VERSION_BYTES:
        return None
    return _store(trash, path, "version", chat.get("id"), copy=True)


def read_manifest(trash, trash_id):
    if not isinstance(trash_id, str) or not TRASH_ID.match(trash_id):
        raise CapabilityError("Invalid trash ID")
    try:
        with open(os.path.join(trash, trash_id + ".json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise CapabilityError("Trash item not found: %s" % trash_id) from e
    stored = os.path.realpath(os.path.join(trash, str(manifest.get("storedName", ""))))
    if os.path.dirname(stored) != trash or not os.path.lexists(stored):
        raise CapabilityError("Invalid trash manifest")
    manifest["_stored"] = stored
    manifest.setdefault("kind", "deleted")
    return manifest


def restore_item(trash, trash_id, destination=None):
    """Put a trash item back. A kept version replaces the current file, whose content is kept in turn."""
    manifest = read_manifest(trash, trash_id)
    dst = destination or manifest["originalPath"]
    try:
        if manifest["kind"] == "version" and os.path.lexists(dst):
            if not os.path.isfile(dst):
                raise CapabilityError("Restore refused: %s is no longer a file." % dst)
            _store(trash, dst, "version", manifest.get("chatId"), copy=True)
            os.replace(manifest["_stored"], dst)
        else:
            if os.path.lexists(dst):
                raise CapabilityError("Original destination now exists; restore refused.")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(manifest["_stored"], dst)
        os.remove(os.path.join(trash, trash_id + ".json"))
    except OSError as e:
        raise CapabilityError("Could not restore trash item: %s" % e)
    return "Restored to " + dst


def list_trash(trash):
    items = []
    for name in os.listdir(trash):
        if name.endswith(".json"):
            try:
                m = read_manifest(trash, name[:-5])
                items.append({k: v for k, v in m.items() if not k.startswith("_") and k != "storedName"})
            except CapabilityError:
                continue
    return sorted(items, key=lambda m: m.get("deletedAt", 0), reverse=True)


def purge_trash(trash, ids):
    """Permanently delete trash items (user action from the Trash screen only)."""
    removed = 0
    for trash_id in ids:
        m = read_manifest(trash, trash_id)
        if os.path.isdir(m["_stored"]) and not os.path.islink(m["_stored"]):
            shutil.rmtree(m["_stored"])
        else:
            os.remove(m["_stored"])
        os.remove(os.path.join(trash, trash_id + ".json"))
        removed += 1
    return removed


def delete_path(chat, path="", **_):
    require(chat, "deleteFiles")
    src = resolve(chat, path, write=True)
    if not os.path.lexists(src):
        raise CapabilityError("Path does not exist: " + src)
    if any(os.path.normcase(src) == os.path.normcase(g["path"]) for g in grants(chat)):
        raise CapabilityError("Cannot delete a shared root itself.")
    trash = trash_dir(chat)
    if not trash:
        raise CapabilityError("Save the chat before deleting files.")
    ident = _store(trash, src, "deleted", chat.get("id"))
    return "Moved to recoverable app trash. ID: %s\nOriginal: %s" % (ident, src)


def restore_path(chat, trash_id="", **_):
    require(chat, "deleteFiles")
    trash = trash_dir(chat)
    if not trash:
        raise CapabilityError("Save the chat before restoring files.")
    manifest = read_manifest(trash, trash_id)
    dst = resolve(chat, manifest["originalPath"], write=True)  # only back into this session's writable folders
    return restore_item(trash, trash_id, dst)


# ---------------------------------------------------------------- server-owned session settings

MAX_FOLDERS = 50


def clean_folders(value):
    """Validate a folder list coming from the browser: [{"path": str, "write": bool}], no duplicates."""
    if not isinstance(value, list):
        raise CapabilityError("folders must be a list")
    out, seen = [], set()
    for f in value:
        if not isinstance(f, dict):
            raise CapabilityError("Each folder must be an object with a path.")
        path = str(f.get("path", "")).strip().strip('"')
        if not path or len(path) > 1024 or any(ord(c) < 32 for c in path):
            raise CapabilityError("Invalid folder path.")
        if os.path.normcase(path) in seen:
            continue
        seen.add(os.path.normcase(path))
        out.append({"path": path, "write": bool(f.get("write"))})
    if len(out) > MAX_FOLDERS:
        raise CapabilityError("A session can share at most %d folders." % MAX_FOLDERS)
    return out


def clean_capabilities(value):
    if not isinstance(value, dict):
        raise CapabilityError("capabilities must be an object")
    out = {}
    if "mode" in value:
        if value["mode"] not in MODES: raise CapabilityError("Unknown mode.")
        out["mode"] = value["mode"]
    if "editMode" in value:  # older chats, projects and settings; see mode_of
        if value["editMode"] not in EDIT_MODES: raise CapabilityError("Unknown edit mode.")
        out["editMode"] = value["editMode"]
    if "modelControl" in value:
        if value["modelControl"] not in MODEL_MODES: raise CapabilityError("Unknown model control mode.")
        out["modelControl"] = value["modelControl"]
    if "git" in value:
        if value["git"] not in GIT_MODES: raise CapabilityError("Unknown Git mode.")
        out["git"] = value["git"]
    for key in ("fileManage", "deleteFiles", "webSearch", "subtasks", "commands", "instructions", "memory"):
        if key in value:
            if not isinstance(value[key], bool): raise CapabilityError("%s must be true or false." % key)
            out[key] = value[key]
    return out


OPERATIONS = {"create_folder": create_folder, "copy_path": copy_path, "move_path": move_path,
              "delete_path": delete_path, "restore_path": restore_path}
