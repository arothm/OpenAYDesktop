"""Skill folders (the Agent Skills format): importing from a folder or a .skill/.zip file, the read_skill_file
tool, the prompt text, editing, updating from the source folder and deleting.

Run with:  python -m unittest discover -s tests -v
"""

import base64
import io
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import skills  # noqa: E402
import test_phase3  # noqa: E402
from test_runs import RunTestCase, call, say  # noqa: E402

SKILL_MD = """---
name: house-style
description: >
  The team's coding rules.
  Use for any code change.
license: MIT
---
# House style

Before editing CSS, read `references/css.md`.
Check a project with `python <skill-dir>/scripts/check.py <project>`.
"""


def write_skill(base, name="house-style", body=SKILL_MD):
    """A repository with the skill in skills/<name>, like the AYS repository."""
    top = Path(base) / "repo" / "skills" / name
    (top / "references").mkdir(parents=True)
    (top / "scripts").mkdir()
    (top / "SKILL.md").write_text(body, encoding="utf-8")
    (top / "references" / "css.md").write_text("CSS-001: never use !important.\n", encoding="utf-8")
    (top / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")
    (top / "~$lock.docx").write_bytes(b"lock")  # an Office lock file is left out
    (Path(base) / "repo" / ".git").mkdir(exist_ok=True)
    (Path(base) / "repo" / ".git" / "config").write_text("x", encoding="utf-8")
    return top


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class SkillFolderTests(RunTestCase):
    def root(self):
        return server.skills_root()

    def test_import_folder_finds_the_skill_in_a_repository(self):
        top = write_skill(self.temp.name)
        s = skills.import_folder(self.root(), str(top.parents[1]))
        self.assertEqual(("house-style", "house-style", True), (s["id"], s["name"], s["folder"]))
        self.assertEqual("The team's coding rules. Use for any code change.", s["description"])  # folded YAML
        self.assertEqual(["references/css.md", "scripts/check.py"], [f["path"] for f in s["files"]])
        self.assertEqual(os.path.realpath(top), s["source"])
        listed = skills.list_all(self.root())
        self.assertEqual([("house-style", True, 2)], [(x["id"], x["folder"], x["files"]) for x in listed])

    def test_importing_again_asks_before_replacing(self):
        top = write_skill(self.temp.name)
        skills.import_folder(self.root(), str(top))
        with self.assertRaises(skills.SkillExists):
            skills.import_folder(self.root(), str(top))
        (top / "references" / "css.md").write_text("CSS-001: changed.\n", encoding="utf-8")
        skills.import_folder(self.root(), str(top), replace=True)
        self.assertIn("changed", (Path(self.root()) / "house-style" / "references" / "css.md").read_text(encoding="utf-8"))
        self.assertEqual([], [n for n in os.listdir(self.root()) if n.startswith(".")])  # no staging folders left

    def test_folder_without_skill_or_with_several_is_refused(self):
        with self.assertRaises(skills.SkillError):
            skills.import_folder(self.root(), str(self.shared))
        write_skill(self.temp.name, "one"); write_skill(self.temp.name, "two")
        with self.assertRaises(skills.SkillError) as e:
            skills.import_folder(self.root(), os.path.join(self.temp.name, "repo"))
        self.assertIn("2 skills", str(e.exception))

    def test_import_zip_with_a_top_folder(self):
        data = zip_of({"house-style/SKILL.md": SKILL_MD, "house-style/references/css.md": "rules", "README.txt": "outside the skill"})
        s = skills.import_zip(self.root(), data, "house-style.skill")
        self.assertEqual(["references/css.md"], [f["path"] for f in s["files"]])
        self.assertEqual("", s["source"])
        with self.assertRaises(skills.SkillError):
            skills.update_from_source(self.root(), "house-style")  # nothing to update from

    def test_zip_with_unsafe_paths_is_refused(self):
        for bad in ("../evil.txt", "/abs.txt", "C:/win.txt", "a/../../evil.txt"):
            data = zip_of({"SKILL.md": SKILL_MD, bad: "x"})
            with self.assertRaises(skills.SkillError, msg=bad):
                skills.import_zip(self.root(), data, "bad.zip")
        self.assertEqual([], skills.list_all(self.root()))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root()), "evil.txt")))
        with self.assertRaises(skills.SkillError):
            skills.import_zip(self.root(), b"not a zip", "x.skill")
        with self.assertRaises(skills.SkillError):
            skills.import_zip(self.root(), zip_of({"a/SKILL.md": SKILL_MD, "b/SKILL.md": SKILL_MD}), "two.zip")

    def test_read_file_stays_inside_the_skill(self):
        skills.import_folder(self.root(), str(write_skill(self.temp.name)))
        read = lambda p: Path(p).read_text(encoding="utf-8")
        self.assertIn("CSS-001", skills.read_file(self.root(), "house-style", "references/css.md", read))
        self.assertIn("CSS-001", skills.read_file(self.root(), "house-style", "<skill-dir>/references/css.md", read))
        folder = skills.folder_path(self.root(), "house-style")
        self.assertIn("CSS-001", skills.read_file(self.root(), "house-style", os.path.join(folder, "references", "css.md"), read))
        listing = skills.read_file(self.root(), "house-style", ".", read)
        self.assertIn("SKILL.md", listing); self.assertIn("scripts/check.py", listing)
        for bad in ("../../chats", "..", str(self.outside / "secret.txt"), "references/../../x", skills.SOURCE_FILE):
            with self.assertRaises(skills.SkillError, msg=bad):
                skills.read_file(self.root(), "house-style", bad, read)

    def test_prompt_names_the_folder_and_files_and_fills_in_placeholders(self):
        skills.import_folder(self.root(), str(write_skill(self.temp.name)))
        folder = skills.folder_path(self.root(), "house-style")
        p = skills.prompt(self.root(), ["house-style"], can_read=True, can_run=True)
        self.assertIn("This skill is a folder: " + folder, p)
        self.assertIn("references/css.md (", p); self.assertIn("read_skill_file", p); self.assertIn("run_command", p)
        self.assertIn("python %s/scripts/check.py" % folder, p)
        quiet = skills.prompt(self.root(), ["house-style"])
        self.assertNotIn("read_skill_file", quiet); self.assertNotIn("run_command", quiet)  # tools the chat doesn't have

    def test_long_instructions_become_an_outline(self):
        body = "---\nname: big\ndescription: A big one\n---\n" + "Rule.\n" * 6000
        top = write_skill(self.temp.name, "big", body)
        skills.import_folder(self.root(), str(top))
        p = skills.prompt(self.root(), ["big"], can_read=True)
        self.assertLess(len(p), 2000); self.assertIn("read SKILL.md with read_skill_file", p)

    def test_editing_keeps_files_and_other_frontmatter(self):
        skills.import_folder(self.root(), str(write_skill(self.temp.name)))
        skills.save(self.root(), "house-style", "Rules: the long version", "New instructions.", "house-style")
        text = (Path(self.root()) / "house-style" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("license: MIT", text); self.assertIn('description: "Rules: the long version"', text)
        s = skills.get(self.root(), "house-style")
        self.assertEqual(("Rules: the long version", "New instructions.", 2), (s["description"], s["body"], len(s["files"])))

    def test_update_from_source_and_delete(self):
        top = write_skill(self.temp.name)
        skills.import_folder(self.root(), str(top))
        (top / "references" / "new.md").write_text("new", encoding="utf-8")
        s = skills.update_from_source(self.root(), "house-style")
        self.assertIn("references/new.md", [f["path"] for f in s["files"]])
        skills.delete(self.root(), "house-style")
        self.assertEqual([], skills.list_all(self.root())); self.assertFalse((Path(self.root()) / "house-style").exists())

    def test_a_folder_replaces_a_written_skill_with_the_same_name(self):
        skills.save(self.root(), "house-style", "", "Old text.")
        skills.import_folder(self.root(), str(write_skill(self.temp.name)), replace=True)
        self.assertEqual([("house-style", True)], [(x["id"], x["folder"]) for x in skills.list_all(self.root())])

    def test_chat_reads_skill_files_with_the_tool(self):
        skills.import_folder(self.root(), str(write_skill(self.temp.name)))
        cid = self.chat()
        server.update_permissions(cid, {"skills": ["house-style"]})
        self.script = [call("read_skill_file", skill="house-style", path="references/css.md"),
                       call("read_skill_file", skill="other", path="x"), say("done")]
        chat = self.run_to_end(cid)
        tools = [t["function"]["name"] for t in self.requests[0]["tools"]]
        self.assertIn("read_skill_file", tools)
        system = self.requests[0]["messages"][0]["content"]
        self.assertIn("House style", system); self.assertIn("read_skill_file", system)
        results = [m for m in chat["messages"] if m.get("role") == "tool"]
        self.assertIn("CSS-001", results[0]["content"]); self.assertEqual("house-style/references/css.md", results[0]["display"])
        self.assertTrue(results[1]["isError"])  # only enabled skills can be read

    def test_tool_is_offered_in_plan_mode_and_only_with_a_skill_folder(self):
        skills.save(self.root(), "Written", "", "Plain text skill.")
        cid = self.chat(); server.update_permissions(cid, {"skills": ["written"]})
        self.script = [say("ok")]; self.run_to_end(cid)
        self.assertNotIn("read_skill_file", [t["function"]["name"] for t in self.requests[-1]["tools"]])
        skills.import_folder(self.root(), str(write_skill(self.temp.name)))
        server.update_permissions(cid, {"skills": ["written", "house-style"]})
        self.script = [say("plan")]; self.run_to_end(cid, plan=True)
        self.assertIn("read_skill_file", [t["function"]["name"] for t in self.requests[-1]["tools"]])
        self.assertIn("read_skill_file", server.READ_ONLY_TOOLS)


class SkillImportHTTPTests(test_phase3.HTTPPhase3Tests):
    test_skills_endpoints = None  # inherited test, run in test_phase3

    def test_import_endpoints(self):
        top = write_skill(self.temp.name)
        status, s = self.call_http("POST", "/api/skills/import", {"path": str(top)})
        self.assertEqual((200, "house-style", 2), (status, s["id"], len(s["files"])))
        status, s = self.call_http("POST", "/api/skills/import", {"path": str(top)})
        self.assertEqual((409, True, "house-style"), (status, s["exists"], s["id"]))
        data = base64.b64encode(zip_of({"SKILL.md": SKILL_MD})).decode()
        status, s = self.call_http("POST", "/api/skills/import", {"zip": data, "name": "h.skill", "replace": True})
        self.assertEqual((200, []), (status, s["files"]))
        self.assertEqual(400, self.call_http("POST", "/api/skills/update", {"id": "house-style"})[0])
        self.assertEqual(400, self.call_http("POST", "/api/skills/import", {"zip": "@@@", "name": "x.zip"})[0])
        self.assertTrue(self.call_http("GET", "/api/skills/house-style")[1]["folder"])
        self.assertEqual(200, self.call_http("DELETE", "/api/skills/house-style")[0])
