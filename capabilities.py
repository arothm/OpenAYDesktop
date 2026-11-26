"""Server-enforced session capabilities and safe file-management operations."""

import json
import os
import re
import shutil
import time
import uuid

# modelControl: "ask" = the user approves each switch (default), "allowed" = switch when the
# user asks without a confirmation step, "fixed" = the session can't change its model.
# webSearch: the session may search the web and read pages (off by default).
# subtasks: the session may start helper chats that run in parallel and report back (off by default).
DEFAULT_POLICY = {"editMode": "review", "fileManage": False, "deleteFiles": False, "modelControl": "ask", "webSearch": False,
                  "subtasks": False}
EDIT_MODES = {"review", "auto"}
# After a session has read untrusted content (web pages), these need the user's approval each time.
GATED_WHEN_UNTRUSTED = {"create_folder", "copy_path", "move_path", "delete_path", "restore_path"}
MODEL_MODES = {"allowed", "ask", "fixed"}
MAX_COPY_BYTES = 500_000_000


class CapabilityError(Exception):
    pass


def policy(chat):
    """Backward-compatible policy: existing write grants map to review editing only.

    Anything not explicitly saved falls back to DEFAULT_POLICY, so older chats never gain
    file management or deletion just because a folder allows edits.
    """
    writable = any(isinstance(f, dict) and f.get("write") for f in chat.get("folders") or [])
    p = dict(DEFAULT_POLICY)
    saved = chat.get("capabilities")
    if isinstance(saved, dict):
        p.update({k: saved[k] for k in DEFAULT_POLICY if k in saved})
    if p["editMode"] not in EDIT_MODES:
        p["editMode"] = "review"
    if p["modelControl"] not in MODEL_MODES:
        p["modelControl"] = "ask"
    p["fileManage"] = bool(p["fileManage"]) and writable
    p["deleteFiles"] = bool(p["deleteFiles"]) and writable
    p["webSearch"] = bool(p["webSearch"])
    p["subtasks"] = bool(p["subtasks"]) and not chat.get("parentId")  # one level only: subtasks can't start subtasks
    # Untrusted content (web pages) can carry instructions aimed at the model. Once a session has
    # read some, nothing that changes files or the model happens without the user's approval.
    p["untrusted"] = bool(chat.get("untrusted"))
    if p["untrusted"]:
        p["editMode"] = "review"
        if p["modelControl"] == "allowed":
            p["modelControl"] = "ask"
    return p


def child_capabilities(parent):
    """A subtask gets at most its parent's effective permissions, a fixed model, and no subtasks of its own."""
    p = policy(parent)
    return {"editMode": p["editMode"], "fileManage": p["fileManage"], "deleteFiles": p["deleteFiles"],
            "modelControl": "fixed", "webSearch": p["webSearch"], "subtasks": False}


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
    return path


def require(chat, capability):
    if not policy(chat).get(capability):
        names = {"fileManage": "file/folder creation, copy, move and rename",
                 "deleteFiles": "recoverable deletion"}
        raise CapabilityError("This session does not allow %s. Enable it in Session permissions." % names.get(capability, capability))


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
    if "editMode" in value:
        if value["editMode"] not in EDIT_MODES: raise CapabilityError("Unknown edit mode.")
        out["editMode"] = value["editMode"]
    if "modelControl" in value:
        if value["modelControl"] not in MODEL_MODES: raise CapabilityError("Unknown model control mode.")
        out["modelControl"] = value["modelControl"]
    for key in ("fileManage", "deleteFiles", "webSearch", "subtasks"):
        if key in value:
            if not isinstance(value[key], bool): raise CapabilityError("%s must be true or false." % key)
            out[key] = value[key]
    return out


OPERATIONS = {"create_folder": create_folder, "copy_path": copy_path, "move_path": move_path,
              "delete_path": delete_path, "restore_path": restore_path}
