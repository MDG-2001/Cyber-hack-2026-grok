"""Reject a screening request that has no real name.

This does not change a decision. It stops the request before the engine runs.
Placeholders follow principle 8: NA, N/A, TEST, UNKNOWN, XXX, NONE, and digits
are not name parts.
"""

from __future__ import annotations

import re

_PLACEHOLDERS = {"na", "n/a", "test", "unknown", "xxx", "none", "null", "nil"}


def screen_name_error(name) -> str | None:
    text = str(name or "").strip()
    if not text:
        return "A real customer name is required."
    parts = [part for part in re.split(r"[\s,;]+", text) if part]
    real = []
    for part in parts:
        token = re.sub(r"[^0-9a-z/]", "", part.casefold())
        if not token or token in _PLACEHOLDERS or token.isdigit():
            continue
        real.append(part)
    if not real:
        return "A real customer name is required. Placeholders such as NA, TEST and UNKNOWN are not a name."
    return None
