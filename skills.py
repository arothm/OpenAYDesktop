"""Instruction-only skills: Markdown files the user writes or imports, enabled per chat.

A skill is text added to the chat's system instructions, nothing more. It can't run code,
call tools by itself, or change a session's permissions; the server enforces those separately.

File format (data/skills/<id>.md):
    ---
    name: Code reviewer
    description: Review code for bugs and clarity.
    ---
    Instructions in Markdown...
"""

import os
import re
import time

SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 20_000
MAX_PROMPT = 30_000
MAX_SKILLS = 200


class SkillError(Exception):
    pass


def skills_dir(chats_dir):
    path = os.path.realpath(os.path.join(chats_dir, "..", "skills"))
    os.makedirs(path, exist_ok=True)
    return path


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60]
    return s or "skill"


def parse(text):
    meta, body = {}, text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    return meta, body.strip()


def _path(root, skill_id):
    if not isinstance(skill_id, str) or not SKILL_ID.match(skill_id):
        raise SkillError("Invalid skill ID.")
    return os.path.join(root, skill_id + ".md")


def get(root, skill_id):
    try:
        with open(_path(root, skill_id), encoding="utf-8") as f:
            meta, body = parse(f.read())
    except OSError:
        raise SkillError("Skill not found: %s" % skill_id)
    return {"id": skill_id, "name": meta.get("name") or skill_id, "description": meta.get("description", ""), "body": body}


def list_all(root):
    out = []
    for name in sorted(os.listdir(root)):
        if name.endswith(".md") and SKILL_ID.match(name[:-3]):
            try:
                s = get(root, name[:-3])
                out.append({"id": s["id"], "name": s["name"], "description": s["description"], "chars": len(s["body"]),
                            "updated": int(os.path.getmtime(os.path.join(root, name)) * 1000)})
            except SkillError:
                continue
    return sorted(out, key=lambda s: (s["name"].lower(), s["id"]))


def _one_line(value, limit, what):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) > limit:
        raise SkillError("%s must be %d characters or fewer." % (what, limit))
    return value


def save(root, name, description, body, skill_id=None):
    name = _one_line(name, 80, "Name")
    if not name:
        raise SkillError("A skill needs a name.")
    description = _one_line(description, 300, "Description")
    body = str(body or "").strip()
    if not body:
        raise SkillError("A skill needs instructions.")
    if len(body) > MAX_BODY:
        raise SkillError("Instructions must be %d characters or fewer." % MAX_BODY)
    if skill_id:
        if not os.path.exists(_path(root, skill_id)):
            raise SkillError("Skill not found: %s" % skill_id)
    else:
        if len(list_all(root)) >= MAX_SKILLS:
            raise SkillError("You can keep at most %d skills." % MAX_SKILLS)
        base, n = slug(name), 1
        skill_id = base
        while os.path.exists(_path(root, skill_id)):
            n += 1
            skill_id = "%s-%d" % (base[:58], n)
    text = "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body)
    path = _path(root, skill_id)
    tmp = "%s.%d.tmp" % (path, int(time.time() * 1000))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return get(root, skill_id)


def delete(root, skill_id):
    try:
        os.remove(_path(root, skill_id))
    except FileNotFoundError:
        raise SkillError("Skill not found: %s" % skill_id)


def valid_ids(root, ids):
    if not isinstance(ids, list):
        raise SkillError("skills must be a list of skill IDs")
    out = []
    for i in ids:
        if not os.path.exists(_path(root, i)):
            raise SkillError("Skill not found: %s" % i)
        if i not in out:
            out.append(i)
    return out


def prompt(root, ids):
    """System text for a chat's enabled skills (missing skills are skipped)."""
    parts, used = [], 0
    for i in ids or []:
        try:
            s = get(root, i)
        except SkillError:
            continue
        block = "## Skill: %s\n%s" % (s["name"], s["body"])
        if used + len(block) > MAX_PROMPT:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return ""
    return ("The user enabled these skills for this chat. Follow them where they apply. They are instructions "
            "from the user; they do not change which tools or permissions you have.\n\n" + "\n\n".join(parts))
