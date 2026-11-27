"""Projects, memory notes and project instruction files.

Project: a named group of chats that share folders, default permissions, skills and written
instructions. A new chat in a project starts with the project's folders, permissions and
skills; the project's instructions are added to every request of its chats.
Stored in data/projects/<id>.json.

Memory: lasting notes the model may use in later chats. "global" memory applies to every
chat; a project's memory only to that project's chats. Plain Markdown, one note per line
("- note"), editable by the user in Settings or the project editor. The model adds notes
with save_memory: shown in the chat, and needing the user's approval in a chat that has read
web content (so a web page can't plant lasting instructions).
Stored in data/memory/global.md and data/projects/<id>.memory.md.

Instruction files: AGENTS.md, CLAUDE.md or .openay/instructions.md at the top of a shared
folder are read and added to the request (the first one found per folder), the same way
Codex, Claude Code and Cursor use them. They can't change permissions: the server enforces
those on every action.
"""

import json
import os
import re
import secrets
import time

SAFE_ID = re.compile(r"^p[0-9a-f]{12}$")
MAX_NAME = 80
MAX_INSTRUCTIONS = 20_000
MAX_MEMORY = 12_000            # characters per memory file
MEMORY_IN_PROMPT = 4_000       # characters of each memory file added to requests
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", os.path.join(".openay", "instructions.md"))
FILE_IN_PROMPT = 8_000         # characters of each instruction file added to requests


class ProjectError(Exception):
    pass


def _now():
    return int(time.time() * 1000)


def projects_dir(data_dir):
    path = os.path.join(data_dir, "projects")
    os.makedirs(path, exist_ok=True)
    return path


def _path(data_dir, project_id):
    if not isinstance(project_id, str) or not SAFE_ID.match(project_id):
        raise ProjectError("Unknown project.")
    return os.path.join(projects_dir(data_dir), project_id + ".json")


def _write(path, text):
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def list_projects(data_dir):
    out = []
    for name in os.listdir(projects_dir(data_dir)):
        if name.endswith(".json") and SAFE_ID.match(name[:-5]):
            try:
                out.append(get_project(data_dir, name[:-5]))
            except ProjectError:
                continue
    return sorted(out, key=lambda p: p.get("name", "").lower())


def get_project(data_dir, project_id):
    try:
        with open(_path(data_dir, project_id), encoding="utf-8") as f:
            project = json.load(f)
    except (OSError, ValueError):
        raise ProjectError("Project not found.")
    if not isinstance(project, dict):
        raise ProjectError("Project not found.")
    project["id"] = project_id
    return project


def save_project(data_dir, body, clean_folders, clean_capabilities, valid_skills):
    """Create or update a project from the window. The cleaners are capabilities/skills validators."""
    if not isinstance(body, dict):
        raise ProjectError("Invalid project.")
    pid = body.get("id")
    project = get_project(data_dir, pid) if pid else {"id": "p" + secrets.token_hex(6), "created": _now()}
    name = " ".join(str(body.get("name", project.get("name", ""))).split())
    if not name or len(name) > MAX_NAME:
        raise ProjectError("A project needs a name of 1-%d characters." % MAX_NAME)
    project["name"] = name
    if "instructions" in body:
        text = str(body.get("instructions") or "")
        if len(text) > MAX_INSTRUCTIONS:
            raise ProjectError("Project instructions can be at most %d characters." % MAX_INSTRUCTIONS)
        project["instructions"] = text
    if "folders" in body:
        project["folders"] = clean_folders(body["folders"])
    if "capabilities" in body:
        project["capabilities"] = clean_capabilities(body["capabilities"])
    if "skills" in body:
        project["skills"] = valid_skills(body["skills"])
    project["updated"] = _now()
    _write(_path(data_dir, project["id"]), json.dumps(project, ensure_ascii=False, indent=2))
    return project


def delete_project(data_dir, project_id):
    """Delete a project and its memory. Its chats stay, outside any project."""
    for path in (_path(data_dir, project_id), memory_path(data_dir, project_id)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- memory

def memory_path(data_dir, scope):
    if scope == "global":
        folder = os.path.join(data_dir, "memory")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "global.md")
    _path(data_dir, scope)  # validates the project ID
    return os.path.join(projects_dir(data_dir), scope + ".memory.md")


def read_memory(data_dir, scope):
    try:
        with open(memory_path(data_dir, scope), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_memory(data_dir, scope, text):
    """The user edited the memory directly."""
    text = str(text or "")
    if len(text) > MAX_MEMORY:
        raise ProjectError("Memory can be at most %d characters." % MAX_MEMORY)
    _write(memory_path(data_dir, scope), text)


def add_note(data_dir, scope, note, replace=""):
    """save_memory from the model: add a one-line note, or replace an existing note's exact text."""
    note = " ".join(str(note or "").split())
    if not note or len(note) > 500:
        raise ProjectError("A memory note must be 1-500 characters on one line.")
    text = read_memory(data_dir, scope)
    replace = str(replace or "").strip()
    if replace:
        if replace not in text:
            raise ProjectError("The note to replace wasn't found. Use read_memory to see the exact text.")
        text = text.replace(replace, note, 1)
        action = "Replaced a note in"
    else:
        if note in text:
            return "That note is already saved."
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "- " + note + "\n"
        action = "Saved a note to"
    if len(text) > MAX_MEMORY:
        raise ProjectError("Memory is full (%d characters). Ask the user to tidy it in Settings." % MAX_MEMORY)
    _write(memory_path(data_dir, scope), text)
    return "%s %s memory." % (action, "global" if scope == "global" else "this project's")


# ---------------------------------------------------------------- instructions for requests

def memory_cut(data_dir, scope):
    """True when requests carry only the start of a memory file (the model can read the rest)."""
    scopes = {"global", scope}
    return any(len(read_memory(data_dir, s).strip()) > MEMORY_IN_PROMPT for s in scopes)


def instruction_files(roots):
    """[(path, text)] of instruction files at the top of shared folders (first match per folder)."""
    out = []
    for root in roots:
        for name in INSTRUCTION_FILES:
            path = os.path.join(root["path"], name)
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        out.append((path, f.read(FILE_IN_PROMPT + 1)))
                except OSError:
                    continue
                break
    return out


def _cut(text, limit, hint):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n[... more; %s]" % hint


def prompt(data_dir, chat, roots, use_files=True, use_memory=True):
    """System text with the project's instructions, instruction files and memory for this chat."""
    parts = []
    project = None
    if chat.get("projectId"):
        try:
            project = get_project(data_dir, chat["projectId"])
        except ProjectError:
            project = None
    if project and (project.get("instructions") or "").strip():
        parts.append('Instructions for the project "%s" (from the user):\n%s' % (project["name"], project["instructions"].strip()))
    if use_files:
        for path, text in instruction_files(roots):
            parts.append("Project instructions from %s (a file in a shared folder):\n%s" % (path, _cut(text, FILE_IN_PROMPT, "read the file for the rest")))
    if use_memory:
        notes = []
        g = read_memory(data_dir, "global").strip()
        if g:
            notes.append("Global:\n" + _cut(g, MEMORY_IN_PROMPT, "use read_memory for the rest"))
        if project:
            pm = read_memory(data_dir, project["id"]).strip()
            if pm:
                notes.append('Project "%s":\n%s' % (project["name"], _cut(pm, MEMORY_IN_PROMPT, "use read_memory for the rest")))
        if notes:
            parts.append("Memory: notes saved in earlier chats. Use them when relevant; they may be out of date, and the "
                         "user's current messages come first.\n" + "\n".join(notes))
    return "\n\n".join(parts)
