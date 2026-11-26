"""Fit a chat's history into the model's context window.

The full history stays saved in the chat; this only shapes what is sent with each request:
  1. Tool results from earlier turns are shortened (the model can call the tool again).
  2. If it still doesn't fit, the oldest whole turns are left out (a turn starts at a user
     message, so a tool call is never separated from its result), and a note says so.
  3. As a last resort, tool results in the current turn are shortened further.
Token counts are estimates (about 4 characters per token); Ollama reports the real
numbers after each reply, and the window shows those.
"""

import json

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 800
MESSAGE_OVERHEAD = 4
DEFAULT_CONTEXT = 65536
OUTPUT_RESERVE = 8192
OLD_TOOL_CHARS = 2_000
CURRENT_TOOL_CHARS = 60_000
TIGHT_TOOL_CHARS = 8_000


def estimate_tokens(messages):
    total = 0
    for m in messages:
        chars = len(m.get("content") or "")
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))
        total += chars // CHARS_PER_TOKEN + MESSAGE_OVERHEAD + IMAGE_TOKENS * len(m.get("images") or [])
    return total


def to_api(m):
    out = {"role": m.get("role", "user"), "content": m.get("content") or ""}
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


def build(messages, context_tokens=None):
    """Return (api_messages, info) sized for a model with `context_tokens` of context."""
    ctx = int(context_tokens or DEFAULT_CONTEXT)
    budget = max(2048, ctx - min(OUTPUT_RESERVE, ctx // 4))
    usable = [m for m in messages if _sendable(m)]
    lead = []
    while usable and usable[0].get("role") == "system":
        lead.append(to_api(usable.pop(0)))
    turns = []
    for m in usable:
        if m.get("role") == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)

    def render(kept, old_limit, current_limit):
        out, clipped = [], 0
        for ti, turn in enumerate(kept):
            limit = current_limit if ti == len(kept) - 1 else old_limit
            for m in turn:
                a = to_api(m)
                if a["role"] == "tool":
                    a["content"], was = clip(a["content"], limit)
                    clipped += was
                out.append(a)
        return out, clipped

    kept, omitted, body, clipped, total = turns, 0, [], 0, 0
    for limits in ((OLD_TOOL_CHARS, CURRENT_TOOL_CHARS), (OLD_TOOL_CHARS, TIGHT_TOOL_CHARS)):
        while True:
            body, clipped = render(kept, *limits)
            total = estimate_tokens(lead + body)
            if total <= budget or len(kept) <= 1:
                break
            omitted += len(kept[0])
            kept = kept[1:]
        if total <= budget:
            break
    note = []
    if omitted:
        what = "1 earlier message was" if omitted == 1 else "%d earlier messages were" % omitted
        note = [{"role": "system", "content": "[%s left out to fit the model's context window. If you need something "
                                              "from earlier in the conversation, ask the user.]" % what}]
    final = lead + note + body
    info = {"estimatedTokens": estimate_tokens(final), "budget": budget, "contextTokens": ctx,
            "omittedMessages": omitted, "clippedToolResults": clipped, "fits": total <= budget}
    return final, info
