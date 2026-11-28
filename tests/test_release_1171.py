"""1.17.1: read_subtask, skills from GitHub links, and no web-content badge.

Run with:  python -m unittest discover -s tests -v
"""

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import skills  # noqa: E402
import test_phase4  # noqa: E402
from test_runs import call, say  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def skill_md(name, body="Do the thing."):
    return "---\nname: %s\ndescription: %s skill\n---\n%s\n" % (name, name, body)


def archive(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class ReadSubtaskTests(test_phase4.SubtaskTestCase):
    def test_the_main_chat_reads_a_subtasks_chat(self):
        cid = self.parent()
        self.child_script = {"count the apples": say("There are 42 apples in the basket.")}
        self.script = [call("start_subtask", task="count the apples", title="Apples"),
                       call("read_subtask", subtask="Apples", last=5), say("done")]
        chat = self.run_to_end(cid)
        read = next(m for m in chat["messages"] if m.get("tool_name") == "read_subtask")
        self.assertIn("There are 42 apples", read["content"]); self.assertIn("count the apples", read["content"])
        self.assertIn('Subtask "Apples"', read["content"]); self.assertNotIn("isError", read)

    def test_reading_is_allowed_in_plan_mode(self):
        self.assertIn("read_subtask", server.READ_ONLY_TOOLS)


class GithubSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = self.temp.name
        self.repo = archive({"skills-main/README.md": "x", "skills-main/skills/pdf/SKILL.md": skill_md("PDF tools"),
                             "skills-main/skills/pdf/ref/forms.md": "fields", "skills-main/skills/docx/SKILL.md": skill_md("Word docs")})
        self.urls = []

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, available):
        def download(url):
            self.urls.append(url)
            for ref, data in available.items():
                if url.endswith("/archive/%s.zip" % ref):
                    return data
            raise skills._NotFound(url)
        return download

    def test_a_skill_folder_link_installs_that_skill(self):
        s = skills.import_github(self.root, "https://github.com/acme/skills/tree/main/skills/pdf", download=self.fake({"main": self.repo}))
        self.assertEqual("PDF tools", s["name"]); self.assertEqual(["ref/forms.md"], [f["path"] for f in s["files"]])
        self.assertEqual("https://github.com/acme/skills/archive/main.zip", self.urls[0])
        self.assertEqual("https://github.com/acme/skills/tree/main/skills/pdf", s["source"])

    def test_a_repository_with_several_skills_names_them(self):
        with self.assertRaises(skills.SkillError) as e:
            skills.import_github(self.root, "https://github.com/acme/skills", download=self.fake({"HEAD": self.repo}))
        self.assertIn("2 skills", str(e.exception)); self.assertIn("pdf", str(e.exception)); self.assertIn("docx", str(e.exception))

    def test_a_one_skill_repository_and_a_branch_with_a_slash(self):
        one = archive({"tone-feature-x/SKILL.md": skill_md("Tone"), "tone-feature-x/notes.md": "n"})
        s = skills.import_github(self.root, "https://github.com/acme/tone/tree/feature/x", download=self.fake({"feature/x": one}))
        self.assertEqual("Tone", s["name"])
        self.assertEqual(["https://github.com/acme/tone/archive/feature.zip", "https://github.com/acme/tone/archive/feature/x.zip"], self.urls)

    def test_update_downloads_it_again(self):
        s = skills.import_github(self.root, "https://github.com/acme/skills/tree/main/skills/pdf", download=self.fake({"main": self.repo}))
        newer = archive({"skills-main/skills/pdf/SKILL.md": skill_md("PDF tools", "Newer instructions.")})
        with mock.patch.object(skills, "_download", self.fake({"main": newer})):
            s = skills.update_from_source(self.root, s["id"])
        self.assertIn("Newer instructions.", skills.get(self.root, s["id"])["body"])

    def test_bad_links_and_missing_repositories(self):
        for bad in ("https://gitlab.com/a/b", "github.com", "https://github.com/a/b/tree/main/../../x", "file:///C:/x"):
            with self.assertRaises(skills.SkillError): skills.parse_github(bad)
        with self.assertRaises(skills.SkillError) as e:
            skills.import_github(self.root, "https://github.com/acme/private", download=self.fake({}))
        self.assertIn("Private repositories", str(e.exception))
        with self.assertRaises(skills.SkillError):  # a folder that isn't in the repository
            skills.import_github(self.root, "https://github.com/acme/skills/tree/main/nope", download=self.fake({"main": self.repo}))


class WindowTests(unittest.TestCase):
    def test_no_web_content_badge_and_no_project_web_search(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("untrustedBadge", html); self.assertNotIn("projWeb", html)
        self.assertIn("skillImportGithub", html)


if __name__ == "__main__":
    unittest.main()
