"""Skills: instructions the user writes or imports, enabled per chat.

Two kinds live in data/skills:

* A written skill is one Markdown file, <id>.md:
      ---
      name: Code reviewer
      description: Review code for bugs and clarity.
      ---
      Instructions in Markdown...

* A skill folder (the open Agent Skills format) is a folder <id>/ with a SKILL.md in the same format plus
  any other files: reference documents, scripts, templates. It is imported from a folder or a .skill/.zip
  file and copied into data/skills, so the original can move or change without breaking chats. When it came
  from a folder, that folder is remembered so "Update from folder" can copy it again.

A skill's instructions are added to the chat's system instructions. A skill folder's other files are read
on demand with the read_skill_file tool, and its scripts run only through run_command, which keeps its
own approval rules. Skills never change a session's tools or permissions.
"""

import io
import json
import os
import re
import shutil
import stat
import time
import zipfile

SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 20_000          # written skills
MAX_PACKAGE_BODY = 100_000  # SKILL.md of a skill folder (the prompt falls back to reading it when long)
MAX_DESCRIPTION = 1024      # the Agent Skills limit
MAX_PROMPT = 30_000
MAX_SKILLS = 200
MAX_PACKAGE_FILES = 500
MAX_PACKAGE_BYTES = 20_000_000
MAX_LISTED_FILES = 60       # files named in the prompt; read_skill_file(path=".") lists all
SKILL_FILE = "SKILL.md"
SOURCE_FILE = ".openay-skill.json"  # where a folder skill came from; not part of the skill
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs", ".vscode"}
PLACEHOLDERS = ("<skill-dir>", "{baseDir}")  # written in SKILL.md files to mean "this skill's folder"


class SkillError(Exception):
    pass


class SkillExists(SkillError):
    """Importing would replace an existing skill; the caller asks the user first."""
    def __init__(self, skill_id, name):
        super().__init__('A skill called "%s" already exists. Replace it?' % name)
        self.skill_id, self.name = skill_id, name


def skills_dir(chats_dir):
    path = os.path.realpath(os.path.join(chats_dir, "..", "skills"))
    os.makedirs(path, exist_ok=True)
    return path


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60]
    return s or "skill"


def frontmatter(text):
    """(ordered [(key, value)], body). Handles plain, quoted and folded (> or |) YAML values."""
    text = str(text or "").lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)$", text, re.S)
    if not m:
        return [], text.strip()
    items, lines, i = [], m.group(1).splitlines(), 0
    while i < len(lines):
        line = lines[i]; i += 1
        km = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not km:
            continue
        key, value = km.group(1).lower(), km.group(2).strip()
        if value in ("", ">", "|", ">-", "|-", ">+", "|+"):  # a block on the following, indented lines
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                block.append(lines[i].strip()); i += 1
            value = ("\n" if value.startswith("|") else " ").join(x for x in block if x).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        items.append((key, value))
    return items, m.group(2).strip()


def parse(text):
    items, body = frontmatter(text)
    return dict(items), body


def _check_id(skill_id):
    if not isinstance(skill_id, str) or not SKILL_ID.match(skill_id):
        raise SkillError("Invalid skill ID.")
    return skill_id


def _file(root, skill_id):
    return os.path.join(root, _check_id(skill_id) + ".md")


def _folder(root, skill_id):
    return os.path.join(root, _check_id(skill_id))


def is_folder(root, skill_id):
    return os.path.isfile(os.path.join(_folder(root, skill_id), SKILL_FILE))


def exists(root, skill_id):
    return os.path.isfile(_file(root, skill_id)) or is_folder(root, skill_id)


def folder_path(root, skill_id):
    """The folder of a skill folder (for the prompt and read_skill_file), or None for a written skill."""
    return os.path.realpath(_folder(root, skill_id)) if is_folder(root, skill_id) else None


def _source(root, skill_id):
    try:
        with open(os.path.join(_folder(root, skill_id), SOURCE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def package_files(folder):
    """The skill folder's files other than SKILL.md: [{"path": "references/a.md", "size": n}], sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, folder).replace("\\", "/")
            if rel in (SKILL_FILE, SOURCE_FILE) or os.path.islink(full):
                continue
            try: out.append({"path": rel, "size": os.path.getsize(full)})
            except OSError: continue
    return out


def get(root, skill_id):
    folder = is_folder(root, skill_id)
    path = os.path.join(_folder(root, skill_id), SKILL_FILE) if folder else _file(root, skill_id)
    try:
        with open(path, encoding="utf-8") as f:
            meta, body = parse(f.read())
    except OSError:
        raise SkillError("Skill not found: %s" % skill_id)
    out = {"id": skill_id, "name": meta.get("name") or skill_id, "description": meta.get("description", ""), "body": body,
           "folder": folder}
    if folder:
        src = _source(root, skill_id)
        out.update(path=folder_path(root, skill_id), files=package_files(_folder(root, skill_id)),
                   source=src.get("source") or "", imported=src.get("imported"))
    return out


def list_all(root):
    out = []
    for name in sorted(os.listdir(root)):
        sid = name[:-3] if name.endswith(".md") else name
        if not SKILL_ID.match(sid) or (name == sid and not is_folder(root, sid)) or (name != sid and is_folder(root, sid)):
            continue  # a folder wins over a stray <id>.md with the same ID
        try:
            s = get(root, sid)
        except SkillError:
            continue
        path = os.path.join(root, name, SKILL_FILE) if s["folder"] else os.path.join(root, name)
        item = {"id": s["id"], "name": s["name"], "description": s["description"], "chars": len(s["body"]),
                "updated": int(os.path.getmtime(path) * 1000), "folder": s["folder"]}
        if s["folder"]:
            item.update(files=len(s["files"]), source=s["source"])
        out.append(item)
    return sorted(out, key=lambda s: (s["name"].lower(), s["id"]))


def _one_line(value, limit, what):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) > limit:
        raise SkillError("%s must be %d characters or fewer." % (what, limit))
    return value


def _yaml_value(value):
    return json.dumps(value, ensure_ascii=False) if re.search(r"^[\s\"'>|&*!%@`#{\[]|: |\s#", value) else value


def _atomic_write(path, text):
    tmp = "%s.%d.tmp" % (path, int(time.time() * 1000))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save(root, name, description, body, skill_id=None):
    """Create a written skill, or change a skill's name, description and instructions (a skill folder keeps
    its other files and frontmatter keys)."""
    name = _one_line(name, 80, "Name")
    if not name:
        raise SkillError("A skill needs a name.")
    description = _one_line(description, MAX_DESCRIPTION, "Description")
    body = str(body or "").strip()
    if not body:
        raise SkillError("A skill needs instructions.")
    folder = bool(skill_id) and is_folder(root, skill_id)
    limit = MAX_PACKAGE_BODY if folder else MAX_BODY
    if len(body) > limit:
        raise SkillError("Instructions must be %d characters or fewer." % limit)
    if skill_id:
        if not exists(root, skill_id):
            raise SkillError("Skill not found: %s" % skill_id)
    else:
        if len(list_all(root)) >= MAX_SKILLS:
            raise SkillError("You can keep at most %d skills." % MAX_SKILLS)
        base, n = slug(name), 1
        skill_id = base
        while exists(root, skill_id) or os.path.exists(_folder(root, skill_id)):
            n += 1
            skill_id = "%s-%d" % (base[:58], n)
    if folder:
        path = os.path.join(_folder(root, skill_id), SKILL_FILE)
        with open(path, encoding="utf-8") as f:
            items, _ = frontmatter(f.read())
        rest = "".join("%s: %s\n" % (k, _yaml_value(v)) for k, v in items if k not in ("name", "description"))
        _atomic_write(path, "---\nname: %s\ndescription: %s\n%s---\n%s\n" % (_yaml_value(name), _yaml_value(description), rest, body))
    else:
        _atomic_write(_file(root, skill_id), "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body))
    return get(root, skill_id)


def delete(root, skill_id):
    if is_folder(root, skill_id):
        shutil.rmtree(_folder(root, skill_id))
        return
    try:
        os.remove(_file(root, skill_id))
    except FileNotFoundError:
        raise SkillError("Skill not found: %s" % skill_id)


def valid_ids(root, ids):
    if not isinstance(ids, list):
        raise SkillError("skills must be a list of skill IDs")
    out = []
    for i in ids:
        if not exists(root, i):
            raise SkillError("Skill not found: %s" % i)
        if i not in out:
            out.append(i)
    return out


# Importing skill folders

def _clean_rel(name):
    """A safe relative path inside a skill folder, or None (absolute, drive, '..', or empty paths)."""
    name = str(name).replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p in SKIP_DIRS for p in parts):
        return None
    if any(re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((" ", ".")) for p in parts):
        return None
    return "/".join(parts)


def _find_skill_dirs(top, depth=3):
    """Folders under top (itself included) that hold a SKILL.md, at most depth levels down."""
    found = []
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        rel = os.path.relpath(dirpath, top)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        if SKILL_FILE in filenames:
            found.append(dirpath)
            dirnames[:] = []  # a skill's own subfolders are part of it
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and level < depth]
    return found


def _pick(found, where):
    if not found:
        raise SkillError("No SKILL.md found in %s. A skill folder has a SKILL.md file at its top." % where)
    if len(found) > 1:
        raise SkillError("%s holds %d skills (%s). Choose one skill's folder." % (where, len(found), ", ".join(found[:5])))
    return found[0]


def import_folder(root, src, replace=False, skill_id=None):
    """Copy a skill folder into data/skills. src may be the skill's folder or a folder above it (such as a
    repository with skills/<name>/SKILL.md), as long as it holds exactly one skill."""
    src = os.path.realpath(os.path.expanduser(str(src or "").strip().strip('"')))
    if not os.path.isdir(src):
        raise SkillError("Not a folder: %s" % src)
    top = _pick(_find_skill_dirs(src), src)
    files, total = {}, 0
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = _clean_rel(os.path.relpath(full, top))
            if not rel or rel == SOURCE_FILE or os.path.islink(full) or not os.path.isfile(full):
                continue
            if fn.startswith("~$") or fn.endswith((".pyc", ".tmp")):
                continue  # Office lock files and build leftovers
            total += os.path.getsize(full)
            if len(files) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise SkillError("This skill is too big to import (at most %d files and %d MB)." % (MAX_PACKAGE_FILES, MAX_PACKAGE_BYTES // 1_000_000))
            with open(full, "rb") as f:
                files[rel] = f.read()
    return _install(root, files, os.path.basename(top), top, replace, skill_id)


def import_zip(root, data, filename="skill.zip", replace=False):
    """Install a skill from a .skill or .zip file (SKILL.md at the top or in one folder)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise SkillError("%s isn't a .skill or .zip file." % filename)
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        tops = sorted({i.filename.replace("\\", "/")[:-len(SKILL_FILE)] for i in infos
                       if i.filename.replace("\\", "/").split("/")[-1] == SKILL_FILE}, key=lambda p: (p.count("/"), p))
        if not tops:
            raise SkillError("No SKILL.md found in %s." % filename)
        if len(tops) > 1 and tops[0].count("/") == tops[1].count("/"):
            raise SkillError("%s holds several skills. Import them one at a time." % filename)
        prefix = tops[0]
        files, total = {}, 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if not name.startswith(prefix):
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                continue  # links could point outside the skill
            rel = _clean_rel(name[len(prefix):])
            if not rel:
                raise SkillError("%s contains an unsafe path: %s" % (filename, name))
            if rel == SOURCE_FILE or rel.split("/")[-1].startswith("~$"):
                continue
            total += info.file_size
            if len(files) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise SkillError("This skill is too big to import (at most %d files and %d MB)." % (MAX_PACKAGE_FILES, MAX_PACKAGE_BYTES // 1_000_000))
            with z.open(info) as f:
                content = f.read(MAX_PACKAGE_BYTES + 1)
            if len(content) > info.file_size or len(content) > MAX_PACKAGE_BYTES:
                raise SkillError("%s is damaged." % filename)
            files[rel] = content
    base = prefix.rstrip("/").split("/")[-1] if prefix else re.sub(r"\.(skill|zip)$", "", os.path.basename(filename), flags=re.I)
    return _install(root, files, base, "", replace)


def _install(root, files, folder_name, source, replace, skill_id=None):
    try:
        meta, body = parse(files.get(SKILL_FILE, b"").decode("utf-8"))
    except UnicodeDecodeError:
        raise SkillError("SKILL.md must be UTF-8 text.")
    if not body:
        raise SkillError("SKILL.md has no instructions.")
    name = _one_line(meta.get("name") or folder_name, 80, "The skill's name")
    _one_line(meta.get("description", ""), MAX_DESCRIPTION, "The skill's description")
    if len(body) > MAX_PACKAGE_BODY:
        raise SkillError("SKILL.md must be %d characters or fewer." % MAX_PACKAGE_BODY)
    skill_id = _check_id(skill_id or slug(name))
    if exists(root, skill_id) and not replace:
        raise SkillExists(skill_id, get(root, skill_id)["name"])
    if not exists(root, skill_id) and len(list_all(root)) >= MAX_SKILLS:
        raise SkillError("You can keep at most %d skills." % MAX_SKILLS)
    staging = os.path.join(root, ".import-%s-%d" % (skill_id, int(time.time() * 1000)))
    try:
        for rel, content in files.items():
            dest = os.path.join(staging, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)
        with open(os.path.join(staging, SOURCE_FILE), "w", encoding="utf-8") as f:
            json.dump({"source": source, "imported": int(time.time() * 1000)}, f)
        target, old = _folder(root, skill_id), None
        if os.path.exists(target):
            old = target + ".old-%d" % int(time.time() * 1000)
            os.replace(target, old)
        os.replace(staging, target)
        if old:
            shutil.rmtree(old, ignore_errors=True)
        if os.path.isfile(_file(root, skill_id)):
            os.remove(_file(root, skill_id))  # a written skill with the same name is replaced by the folder
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return get(root, skill_id)


def update_from_source(root, skill_id):
    src = _source(root, skill_id).get("source") if is_folder(root, skill_id) else None
    if not src:
        raise SkillError("This skill wasn't imported from a folder. Import its .skill file again instead.")
    if not os.path.isdir(src):
        raise SkillError("The folder it came from is gone: %s" % src)
    return import_folder(root, src, replace=True, skill_id=skill_id)


# Using skills in chats

def read_file(root, skill_id, path, read_window):
    """Text of one file in an enabled skill folder, or a listing for a folder path. read_window(p) reads text."""
    folder = folder_path(root, skill_id)
    if not folder:
        raise SkillError("%s isn't a skill folder, so it has no files to read." % skill_id)
    raw = str(path or ".").replace("\\", "/").strip()
    for ph in PLACEHOLDERS:
        raw = raw.replace(ph + "/", "").replace(ph, ".")
    if raw.lower().startswith(folder.replace("\\", "/").lower()):  # the model may send the absolute path
        raw = raw[len(folder):].lstrip("/") or "."
    rel = "." if raw in ("", ".", "./", "/") else _clean_rel(raw)
    if not rel:
        raise SkillError("Use a path inside the skill, such as references/guide.md.")
    full = os.path.realpath(os.path.join(folder, *rel.split("/")))
    if os.path.normcase(full) != os.path.normcase(folder) and not os.path.normcase(full).startswith(os.path.normcase(folder) + os.sep):
        raise SkillError("That path is outside the skill.")
    if os.path.isdir(full):
        files = [f for f in package_files(folder) if rel == "." or f["path"].startswith(rel + "/")]
        lines = ["%s  (%s)" % (f["path"], _size(f["size"])) for f in files]
        if rel == ".":
            lines.insert(0, SKILL_FILE)
        return "Files in the %s skill (%s):\n%s" % (skill_id, folder, "\n".join(lines) or "(none)")
    if not os.path.isfile(full) or os.path.basename(full) == SOURCE_FILE:
        raise SkillError("No such file in the %s skill: %s. Read path \".\" to list its files." % (skill_id, rel))
    return read_window(full)


def _size(n):
    return "%d B" % n if n < 1024 else "%.1f KB" % (n / 1024) if n < 1024 * 1024 else "%.1f MB" % (n / 1024 / 1024)


def _folder_block(root, s, can_read, can_run, room):
    folder = s["path"]
    body = s["body"]
    for ph in PLACEHOLDERS:
        body = body.replace(ph, folder)
    files = s["files"]
    listed = ", ".join("%s (%s)" % (f["path"], _size(f["size"])) for f in files[:MAX_LISTED_FILES])
    if len(files) > MAX_LISTED_FILES:
        listed += ", and %d more" % (len(files) - MAX_LISTED_FILES)
    head = ["This skill is a folder: %s. Paths in its instructions are relative to that folder." % folder]
    if files:
        head.append("Its other files: %s." % listed)
    if can_read:
        head.append('Read them with read_skill_file (skill "%s") when the instructions call for them; they are not loaded '
                    "until you do, and they aren't in the shared folders, so the folder tools can't reach them." % s["id"])
    if can_run:
        head.append("To run one of its scripts, use run_command with the script's absolute path (in double quotes if it "
                    "has spaces) from a shared folder; the user's command rules apply.")
    block = "## Skill: %s\n%s\n\n%s" % (s["name"], " ".join(head), body)
    if len(block) > room and can_read:  # too long to send every time: send the outline and let the model read it
        block = ("## Skill: %s\n%s\nDescription: %s\nIts instructions are long: read %s with read_skill_file before "
                 "doing work this skill covers." % (s["name"], " ".join(head), s["description"], SKILL_FILE))
    return block


def prompt(root, ids, can_read=False, can_run=False):
    """System text for a chat's enabled skills (missing skills are skipped). can_read / can_run say whether the
    chat has read_skill_file and run_command, so the text only mentions tools the model has."""
    parts, used = [], 0
    for i in ids or []:
        try:
            s = get(root, i)
        except SkillError:
            continue
        if s["folder"]:
            block = _folder_block(root, s, can_read, can_run, MAX_PROMPT - used)
        else:
            block = "## Skill: %s\n%s" % (s["name"], s["body"])
        if used + len(block) > MAX_PROMPT:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return ""
    return ("The user enabled these skills for this chat. Follow them where they apply. They are instructions "
            "from the user; they do not change which tools or permissions you have.\n\n" + "\n\n".join(parts))


def has_folders(root, ids):
    return any(is_folder(root, i) for i in ids or [] if isinstance(i, str) and SKILL_ID.match(i))
