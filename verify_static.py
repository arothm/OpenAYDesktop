"""Local static consistency checks; does not alter application data."""
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

root = Path(__file__).resolve().parent
ast.parse((root / "server.py").read_text(encoding="utf-8"))
ast.parse((root / "tests" / "test_server.py").read_text(encoding="utf-8"))
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
assert "body.get(\"folders\")" not in source
assert 'out = run_tool({"folders": body.get' not in source
assert re.search(r'chatId\s*:\s*chat\.id\b', js), "Frontend must send the chat ID for saved folder grants"
assert re.search(r'chat_id\s*=\s*req\.pop\("chatId",\s*""\)', source), "Server must remove the chat ID unconditionally"
print("Server-owned folder grants and internal-field removal: OK")
print("Static checks passed. Run the regression suite separately; this script does not launch a browser or test GitHub.")
