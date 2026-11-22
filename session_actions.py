"""Validated, server-mediated session actions."""

import re
import time


def clean_title(value):
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title:
        raise ValueError("Session name cannot be empty.")
    if len(title) > 100:
        raise ValueError("Session name must be 100 characters or fewer.")
    if any(ord(c) < 32 for c in title):
        raise ValueError("Session name contains invalid control characters.")
    return title


def rename(chat, title):
    title = clean_title(title)
    old = chat.get("title", "Untitled")
    chat["title"] = title
    chat["updated"] = int(time.time() * 1000)
    return "Session renamed from %s to %s." % (old, title)
