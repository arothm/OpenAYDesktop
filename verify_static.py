"""Local static consistency checks; does not alter application data."""
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

root = Path(__file__).resolve().parent
for name in ("server.py", "runs.py", "context_budget.py", "capabilities.py", "session_actions.py", "skills.py", "web.py"):
    ast.parse((root / name).read_text(encoding="utf-8"))
for test in (root / "tests").glob("test_*.py"):
    ast.parse(test.read_text(encoding="utf-8"))
print("Python syntax: OK")

html = (root / "index.html").read_text(encoding="utf-8")
js = (root / "app.js").read_text(encoding="utf-8")
source = (root / "server.py").read_text(encoding="utf-8")
assert "<title>Open AY Desktop</title>" in html
assert 'APP_NAME = "Open AY Desktop"' in source
assert 'REPOSITORY = "arothm/OpenAYDesktop"' in source
assert "cdnjs.cloudflare.com" not in html
assert 'src="/app.js"' in html and 'href="/styles.css"' in html
assert "4000" not in js
print("Application identity and local-only assets: OK")

for required in ('id="apiKey"', 'id="defaultModel"', 'id="checkUpdates"'):
    assert required in html, required
for removed in ('id="localField"', 'id="sysPrompt"', 'id="temp"', 'name="mode"'):
    assert removed not in html, removed
assert "systemPrompt" not in js and "temperature" not in js and "lastModel" not in js
assert '"/api/updates/check"' in js and 'p == "/api/updates/check"' in source
assert re.search(r'defaultModel\s*:\s*\$\("#defaultModel"\)\.value', js), "Default model is not saved"
print("Requested Settings controls and cloud-only model behavior: OK")

class IDs(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id": self.ids.append(value)
p = IDs(); p.feed(html)
assert len(p.ids) == len(set(p.ids)), "Duplicate HTML IDs"
used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
missing = used - set(p.ids)
assert not missing, "JS references missing IDs: %r" % missing
print("JavaScript selector IDs: OK (%d checked)" % len(used))
runs_src = (root / "runs.py").read_text(encoding="utf-8")
# Replies run on the server; the window has no way to call tools or the model directly.
for gone in ('"/api/tool"', '"/api/tool/prepare"', '"/api/chat"', "_proxy_chat", "_proxy_stream"):
    assert gone not in source, gone
for gone in ('"/api/tool', '"/api/chat"', "runAssistant", "streamReply"):
    assert gone not in js, gone
# Every tool call re-reads permissions from the saved chat, never from a request.
assert runs_src.count("saved_tool_config(self.chat_id)") >= 3, "Tool calls must load the saved chat's permissions"
assert "body.get(\"folders\")" not in runs_src
assert re.search(r'chatId\s*:\s*chat\.id\b', js), "Frontend must send the chat ID"
print("Server-side runs and server-owned folder grants: OK")
import json
pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
version = re.search(r'APP_VERSION = "([^"]+)"', source).group(1)
assert pkg["version"] == version, "package.json version %s != APP_VERSION %s" % (pkg["version"], version)
main_js = (root / "electron" / "main.js").read_text(encoding="utf-8")
for flag in ("contextIsolation: true", "sandbox: true", "nodeIntegration: false", "setWindowOpenHandler", "will-navigate", "--exit-with-stdin"):
    assert flag in main_js, "Electron window is missing: " + flag
for name in pkg["build"]["extraResources"][0]["filter"]:
    assert name.endswith("/**") or (root / name).exists(), "Packaged file is missing: " + name
assert set(pkg["build"]["extraResources"][0]["filter"]) >= {"server.py", "runs.py", "context_budget.py", "capabilities.py", "session_actions.py", "skills.py", "web.py"}
fetch = (root / "scripts" / "fetch-python.js").read_text(encoding="utf-8")
assert re.search(r'const SHA256 = "[0-9a-f]{64}"', fetch), "The bundled Python download must be pinned to a SHA-256"
assert any(e.get("to") == "python" for e in pkg["build"]["extraResources"]), "The installer must bundle Python"
assert "fetch-python.js" in pkg["scripts"]["dist"]
print("Electron shell: version %s, locked-down window, packaged server files and pinned Python: OK" % version)
print("Static checks passed. Run the regression suite separately; this script does not launch a browser or test GitHub.")
