"""Fit a chat's history into the context budget, and keep requests cache-friendly.

The full history stays saved in the chat; this only shapes what is sent with each request.

Budget: the smaller of the model's context window and the user's "Context budget" setting
(Settings: Economy 32k, Balanced 64k, or Full = the model's whole window). Everything sent
counts: the messages, plus the tool definitions and system instructions (`overhead_tokens`).

Order of savings, cheapest first:
  1. Tool results from earlier turns are shortened (the model can call the tool again).
  2. Over budget, older turns are replaced by a summary (runs.py writes it with the chat's
     model and stores it as chat["compaction"] = {"upTo": n, "summary": text}). The summary
     changes only when the next compaction happens, so the start of every request stays
     identical between compactions and Ollama's cheaper cached-input rate applies to it.
  3. If no summary can be made, the oldest whole turns are left out (a turn starts at a user
     message, so a tool call is never separated from its result), and a note says so.
  4. As a last resort, tool results in the current turn are shortened further.
Token counts are estimates (about 4 characters per token); Ollama reports the real numbers
after each reply, and the window shows those.
"""

import json

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1_000          # a picture after the window shrinks it to at most 1600 px
MESSAGE_OVERHEAD = 4
DEFAULT_CONTEXT = 65536
OUTPUT_RESERVE = 8192
OLD_TOOL_CHARS = 2_000
CURRENT_TOOL_CHARS = 30_000   # one tool result in the reply being written (about 7,500 tokens)
TIGHT_TOOL_CHARS = 8_000
BUDGETS = {"economy": 32_768, "balanced": 65_536, "full": None}
DEFAULT_BUDGET = "full"       # summarize only near the model's window: Ollama bills an unchanged start at the cheaper cached rate
KEEP_RECENT = 0.4             # after a compaction, recent turns fill at most this share of the budget
ATTACHMENT_CHARS = 60_000     # text of one attached document sent to the model
SUMMARY_CHARS = 8_000         # longest summary kept (about 2,000 tokens)
USER_NOTE_CHARS = 700         # each of the user's summarized messages, kept next to the summary
USER_NOTES_TOTAL = 24_000     # all of them together (about 6,000 tokens)
SUMMARY_PREFIX ="Summary of the earlier part of this conversation (older messages were summarized to save context):\n\n"


def estimate_tokens(messages):
    total = 0
    for m in messages:
        chars = len(m.get("content") or "")
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))
        total += chars // CHARS_PER_TOKEN + MESSAGE_OVERHEAD + IMAGE_TOKENS * len(m.get("images") or [])
    return total


def attachments_text(m):
    """Attached documents (their extracted text) as they are given to the model."""
    parts = []
    for a in m.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        text = str(a.get("text") or "")[:ATTACHMENT_CHARS]
        more = " The text was cut short; ask the user for the rest if you need it." if a.get("truncated") or len(str(a.get("text") or "")) > ATTACHMENT_CHARS else ""
        parts.append('<attached_file name="%s">\n%s\n</attached_file>%s' % (str(a.get("name") or "file").replace('"', "'"), text, more))
    return "\n\n".join(parts)


def to_api(m):
    content = m.get("content") or ""
    if m.get("attachments"):
        content = (attachments_text(m) + ("\n\n" + content if content else "")).strip()
    out = {"role": m.get("role", "user"), "content": content}
    for key in ("images", "tool_calls", "tool_name"):
        if m.get(key):
            out[key] = m[key]
    return out


def clip(text, limit):
    if len(text) <= limit:
        return text, False
    return (text[:limit] + "\n[... %d more characters left out to save context. Call the tool again, for example "
            "reading a smaller range, if you need them.]" % (len(text) - limit)), True


def _sendable(m):
    """Skip failed assistant placeholders and anything that isn't a chat message."""
    if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant", "tool"):
        return False
    return not (m.get("role") == "assistant" and m.get("error") and not m.get("content") and not m.get("tool_calls"))


def budget_for(context_tokens=None, budget_name=None, overhead_tokens=0):
    """Tokens available for messages: the model's window or the user's budget, minus reply room and overhead."""
    ctx = int(context_tokens or DEFAULT_CONTEXT)
    cap = BUDGETS.get(budget_name or DEFAULT_BUDGET, BUDGETS[DEFAULT_BUDGET])
    window = min(ctx, cap) if cap else ctx
    return max(2048, window - min(OUTPUT_RESERVE, window // 4) - int(overhead_tokens or 0)), ctx


def valid_compaction(messages, compaction):
    """The stored summary, if it still matches this history (an edit before its end makes it stale)."""
    if not isinstance(compaction, dict) or not compaction.get("summary"):
        return None
    up_to = compaction.get("upTo")
    if not isinstance(up_to, int) or not 0 < up_to <= len(messages):
        return None
    return compaction


def _turns(messages, start):
    """[(absolute index of the turn's first message, [messages])] from `start` on."""
    turns = []
    for i in range(start, len(messages)):
        m = messages[i]
        if not _sendable(m) or m.get("role") == "system":
            continue
        if m.get("role") == "user" or not turns:
            turns.append((i, [m]))
        else:
            turns[-1][1].append(m)
    return turns


def _render(turns, old_limit, current_limit):
    out, clipped = [], 0
    for ti, (_, turn) in enumerate(turns):
        limit = current_limit if ti == len(turns) - 1 else old_limit
        for m in turn:
            a = to_api(m)
            if a["role"] == "tool":
                a["content"], was = clip(a["content"], limit)
                clipped += was
            out.append(a)
    return out, clipped


def summary_message(messages, compaction):
    """What replaces the summarized messages: the summary, plus the user's own earlier messages,
    shortened, in order. A summary can miss a detail the user gave in passing; their words can't.
    It depends only on the stored summary and the history, so it stays the same until the next one."""
    notes, used = [], 0
    for m in messages[:compaction["upTo"]]:
        if isinstance(m, dict) and m.get("role") == "user" and not m.get("auto") and (m.get("content") or "").strip():
            text = " ".join(m["content"].split())
            if len(text) > USER_NOTE_CHARS:
                text = text[:USER_NOTE_CHARS] + " [...]"
            notes.append(text)
    keep = []
    for text in reversed(notes):  # over the cap, the most recent ones are kept
        if used + len(text) > USER_NOTES_TOTAL:
            break
        keep.append(text); used += len(text)
    keep.reverse()
    out = SUMMARY_PREFIX + compaction["summary"]
    if keep:
        skipped = len(notes) - len(keep)
        out += ("\n\nThe user's own messages from that part, in order (shortened%s):\n" % (", the first %d left out" % skipped if skipped else "")
                + "\n".join("%d. %s" % (i + 1 + skipped, t) for i, t in enumerate(keep)))
    return out


def build(messages, context_tokens=None, budget_name=None, overhead_tokens=0, compaction=None):
    """Return (api_messages, info) for one request. info["needsCompaction"] asks runs.py to summarize."""
    budget, ctx = budget_for(context_tokens, budget_name, overhead_tokens)
    lead = [to_api(m) for m in messages if isinstance(m, dict) and m.get("role") == "system" and _sendable(m)][:1]
    compaction = valid_compaction(messages, compaction)
    start = compaction["upTo"] if compaction else 0
    summary = [{"role": "system", "content": summary_message(messages, compaction)}] if compaction else []
    turns = _turns(messages, start)
    body, clipped = _render(turns, OLD_TOOL_CHARS, CURRENT_TOOL_CHARS)
    total = estimate_tokens(lead + summary + body)
    over = total > budget
    kept, omitted = turns, 0
    if over:  # fallback until a summary exists: leave out the oldest turns, then shorten the current turn's results
        for limits in ((OLD_TOOL_CHARS, CURRENT_TOOL_CHARS), (OLD_TOOL_CHARS, TIGHT_TOOL_CHARS)):
            while True:
                body, clipped = _render(kept, *limits)
                total = estimate_tokens(lead + summary + body)
                if total <= budget or len(kept) <= 1:
                    break
                omitted += len(kept[0][1])
                kept = kept[1:]
            if total <= budget:
                break
    note = []
    if omitted:
        what = "1 earlier message was" if omitted == 1 else "%d earlier messages were" % omitted
        note = [{"role": "system", "content": "[%s left out to fit the context budget. If you need something "
                                              "from earlier in the conversation, ask the user.]" % what}]
    final = lead + summary + note + body
    info = {"estimatedTokens": estimate_tokens(final) + int(overhead_tokens or 0), "budget": budget, "contextTokens": ctx,
            "omittedMessages": omitted, "clippedToolResults": clipped, "fits": total <= budget,
            "needsCompaction": over and len(turns) > 1, "summarizedMessages": start}
    return final, info


def plan_compaction(messages, compaction=None, context_tokens=None, budget_name=None, overhead_tokens=0):
    """(start, split): summarize messages[start:split] (plus any earlier summary), keep messages[split:].

    Recent turns are kept up to KEEP_RECENT of the budget (always at least the current turn).
    Returns None when there is nothing older than the current turn to summarize.
    """
    budget, _ = budget_for(context_tokens, budget_name, overhead_tokens)
    compaction = valid_compaction(messages, compaction)
    start = compaction["upTo"] if compaction else 0
    turns = _turns(messages, start)
    if len(turns) < 2:
        return None
    keep_from, used = len(turns) - 1, estimate_tokens(_render(turns[-1:], OLD_TOOL_CHARS, CURRENT_TOOL_CHARS)[0])
    while keep_from > 1:
        size = estimate_tokens(_render([turns[keep_from - 1]], OLD_TOOL_CHARS, OLD_TOOL_CHARS)[0])
        if used + size > budget * KEEP_RECENT:
            break
        used += size
        keep_from -= 1
    split = turns[keep_from][0]
    return (start, split) if split > start else None


def transcript(messages, tool_chars=1_500, message_chars=6_000):
    """Plain-text rendering of messages for the summarizer (tool output shortened)."""
    lines = []
    for m in messages:
        if not _sendable(m) or m.get("role") == "system":
            continue
        role = m.get("role")
        text = to_api(m)["content"]
        if role == "tool":
            text, _ = clip(text, tool_chars)
            lines.append("[tool result: %s]\n%s" % (m.get("tool_name") or "tool", text))
            continue
        if len(text) > message_chars:
            text = text[:message_chars] + " [...]"
        calls = ""
        if m.get("tool_calls"):
            names = [str(((c or {}).get("function") or {}).get("name") or "?") for c in m["tool_calls"]]
            calls = "\n[called tools: %s]" % ", ".join(names)
        if m.get("images"):
            text += "\n[%d image(s) attached]" % len(m["images"])
        lines.append("%s: %s%s" % ("User" if role == "user" else "Assistant", text, calls))
    return "\n\n".join(lines)
