"""Text from attached documents: PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx) and text files.

Standard library only. The window sends the file; the server returns its text, which is stored with
the message and given to the model (context_budget.attachments_text). Scanned PDFs (pictures of pages)
have no text to extract: the result says so, so the user can attach page images instead.

The PDF reader handles the common cases: Flate-compressed content streams, literal and hex strings,
the Tj/TJ/'/" text operators, and ToUnicode maps (bfchar/bfrange) for embedded fonts.
"""

import io
import re
import zipfile
import zlib
from xml.etree import ElementTree

MAX_FILE_BYTES = 25_000_000
MAX_TEXT = 300_000          # characters kept per document (the model gets context_budget.ATTACHMENT_CHARS of it)
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".log", ".py", ".js",
            ".ts", ".tsx", ".jsx", ".css", ".java", ".cs", ".cpp", ".c", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sql", ".sh",
            ".ps1", ".bat", ".ini", ".toml", ".cfg", ".conf", ".env.example", ".rst", ".tex", ".kt", ".swift", ".scala", ".lua", ".r"}


class DocumentError(Exception):
    pass


def extract(name, data):
    """(text, note) for a file's bytes. note explains anything the user should know (or is "")."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("%s is larger than %d MB." % (name, MAX_FILE_BYTES // 1_000_000))
    lower = name.lower()
    ext = lower[lower.rfind("."):] if "." in lower else ""
    if ext == ".pdf" or data[:5] == b"%PDF-":
        text = pdf_text(data)
        note = "" if len(text.strip()) > 40 else "Little or no text was found: this PDF may be scanned pages. Attach page images instead."
    elif ext == ".docx":
        text, note = docx_text(data), ""
    elif ext == ".xlsx":
        text, note = xlsx_text(data), ""
    elif ext == ".pptx":
        text, note = pptx_text(data), ""
    elif ext in TEXT_EXT or _looks_like_text(data):
        text, note = _decode(data), ""
    else:
        raise DocumentError("%s isn't a supported document. Attach PDF, Word, Excel, PowerPoint or text files." % name)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    if len(text) > MAX_TEXT:
        text, note = text[:MAX_TEXT], (note + " " if note else "") + "Only the first %d characters were kept." % MAX_TEXT
    return text, note


def _looks_like_text(data):
    head = data[:4096]
    return b"\x00" not in head or head[:2] in (b"\xff\xfe", b"\xfe\xff")


def _decode(data):
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


# ---------------------------------------------------------------- Office files (zip + XML)

def _zip(data):
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocumentError("The file is damaged or isn't an Office document.")


def _xml(z, name):
    try:
        with z.open(name) as f:
            return ElementTree.parse(f).getroot()
    except (KeyError, ElementTree.ParseError):
        return None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def docx_text(data):
    z = _zip(data)
    root = _xml(z, "word/document.xml")
    if root is None:
        raise DocumentError("This Word file has no document text.")
    out = []
    for block in root.iter():
        tag = _local(block.tag)
        if tag == "p":
            text = "".join((t.text or "") if _local(t.tag) == "t" else "\t" if _local(t.tag) == "tab" else "" for t in block.iter())
            out.append(text)
        elif tag == "tr":
            out.append("")  # rows of a table end up as their paragraphs, separated by a blank line
    return "\n".join(out)


def xlsx_text(data):
    z = _zip(data)
    shared = []
    root = _xml(z, "xl/sharedStrings.xml")
    if root is not None:
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    names = {}
    wb = _xml(z, "xl/workbook.xml")
    if wb is not None:
        for i, s in enumerate(x for x in wb.iter() if _local(x.tag) == "sheet"):
            names["xl/worksheets/sheet%d.xml" % (i + 1)] = s.get("name") or "Sheet %d" % (i + 1)
    out = []
    for name in sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[0])):
        sheet = _xml(z, name)
        if sheet is None:
            continue
        out.append("## %s" % names.get(name, name.rsplit("/", 1)[-1]))
        for row in (r for r in sheet.iter() if _local(r.tag) == "row"):
            cells = []
            for c in (x for x in row if _local(x.tag) == "c"):
                v = next((x.text for x in c.iter() if _local(x.tag) in ("v", "t") and x.text is not None), "")
                if c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                    v = shared[int(v)]
                cells.append(v or "")
            if any(cells):
                out.append("\t".join(cells))
    return "\n".join(out)


def pptx_text(data):
    z = _zip(data)
    out = []
    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    for i, name in enumerate(slides, 1):
        root = _xml(z, name)
        if root is None:
            continue
        paras = []
        for p in (x for x in root.iter() if _local(x.tag) == "p"):
            text = "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t")
            if text.strip():
                paras.append(text)
        out.append("## Slide %d\n%s" % (i, "\n".join(paras)))
    return "\n\n".join(out)


# ---------------------------------------------------------------- PDF

STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n", re.S)


def _streams(data):
    """Decoded content of every stream in the file (compressed ones inflated)."""
    for m in STREAM.finditer(data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw, head = data[start:end], m.group(1)
        if b"/FlateDecode" in head or b"/Fl " in head or b"/Fl]" in head:
            try:
                raw = zlib.decompressobj().decompress(raw)
            except zlib.error:
                continue
        elif b"/Filter" in head:
            continue  # images and other encodings carry no text we can read
        yield raw


def _cmap(stream):
    """code (bytes) -> text from a ToUnicode map."""
    out = {}
    def hexes(s): return [bytes.fromhex(h.decode()) for h in re.findall(rb"<([0-9A-Fa-f]+)>", s)]
    def text(b):
        try: return b.decode("utf-16-be")
        except UnicodeDecodeError: return ""
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.S):
        h = hexes(block)
        for k, v in zip(h[0::2], h[1::2]):
            out[k] = text(v)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.S):
        for line in block.splitlines():
            parts = re.findall(rb"<([0-9A-Fa-f]+)>|\[(.*?)\]", line)
            if len(parts) < 3 or not parts[0][0] or not parts[1][0]:
                continue
            lo, hi, width = int(parts[0][0], 16), int(parts[1][0], 16), len(parts[0][0]) // 2
            if hi - lo > 5000:
                continue
            if parts[2][0]:
                base = bytes.fromhex(parts[2][0].decode())
                for i, code in enumerate(range(lo, hi + 1)):
                    dst = base[:-2] + (int.from_bytes(base[-2:], "big") + i).to_bytes(2, "big") if len(base) >= 2 else base
                    out[code.to_bytes(width, "big")] = text(dst)
            else:
                for i, h in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", parts[2][1])):
                    out[(lo + i).to_bytes(width, "big")] = text(bytes.fromhex(h.decode()))
    return out


TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>]+|[-+]?\d*\.?\d+|[A-Za-z'\"*]+", re.S)


def _literal(tok):
    """Bytes of a PDF literal string (handles escapes and balanced parentheses)."""
    s, out, i = tok[1:-1], bytearray(), 0
    esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}
    while i < len(s):
        c = s[i]
        if c == 92 and i + 1 < len(s):
            n = s[i + 1]
            if n in esc:
                out.append(esc[n]); i += 2; continue
            m = re.match(rb"[0-7]{1,3}", s[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8) & 255); i += 1 + len(m.group(0)); continue
            i += 2; continue  # line continuation or unknown escape
        out.append(c); i += 1
    return bytes(out)


def _show(raw, cmap):
    if cmap:
        for width in (2, 1):
            if len(raw) % width:
                continue
            codes = [raw[i:i + width] for i in range(0, len(raw), width)]
            if all(c in cmap for c in codes):
                return "".join(cmap[c] for c in codes)
    return raw.decode("latin-1")


def pdf_text(data):
    if not data.startswith(b"%PDF"):
        raise DocumentError("This isn't a PDF file.")
    if b"/Encrypt" in data[:200_000] or b"/Encrypt" in data[-50_000:]:
        raise DocumentError("This PDF is encrypted, so its text can't be read.")
    streams = list(_streams(data))
    cmap = {}
    for s in streams:
        if b"begincmap" in s:
            cmap.update(_cmap(s))
    pages = []
    for s in streams:
        if b"BT" not in s or b"begincmap" in s:
            continue
        out, operands = [], []
        for tok in TOKEN.findall(s):
            if tok[:1] == b"(":
                operands.append(_literal(tok))
            elif tok[:1] == b"<":
                h = re.sub(rb"\s", b"", tok[1:-1])
                operands.append(bytes.fromhex((h + b"0" if len(h) % 2 else h).decode()))
            elif tok in (b"[", b"]") or tok[:1] == b"/" or re.match(rb"[-+]?\d*\.?\d+$", tok):
                operands.append(tok)
            else:  # an operator
                if tok in (b"Tj", b"'", b'"'):
                    strings = [o for o in operands if isinstance(o, bytes) and o[:1] not in (b"[", b"]", b"/") and not re.match(rb"[-+]?\d*\.?\d+$", o)]
                    if tok != b"Tj":
                        out.append("\n")
                    if strings:
                        out.append(_show(strings[-1], cmap))
                elif tok == b"TJ":
                    for o in operands:
                        if o in (b"[", b"]") or o[:1] == b"/":
                            continue
                        if re.match(rb"[-+]?\d*\.?\d+$", o):
                            if float(o) < -200:
                                out.append(" ")
                        else:
                            out.append(_show(o, cmap))
                elif tok in (b"Td", b"TD"):
                    nums = [o for o in operands if re.match(rb"[-+]?\d*\.?\d+$", o or b"")]
                    if len(nums) >= 2 and abs(float(nums[-1])) > 0.5:
                        out.append("\n")
                    elif out and not out[-1].endswith((" ", "\n")):
                        out.append(" ")
                elif tok in (b"T*", b"ET"):
                    out.append("\n")
                elif tok == b"Tm":
                    out.append("\n")
                operands = []
        text = re.sub(r"\n{3,}", "\n\n", "".join(out))
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)
