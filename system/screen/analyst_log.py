"""Append-only analyst decisions. This file is not the screening output.

Each event is one JSON line. Reopening a case adds a line. Nothing is deleted
or rewritten. The log lives outside output/ so it cannot be mistaken for the
submission.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

ACTIONS = ("CONFIRM_MATCH", "CLEAR_MATCH", "ESCALATE", "REOPEN")
_LOCK = threading.Lock()


def record(path, hit_id, action, rationale, analyst, notes="", idempotency_key=None,
           cited_ids=None, allowed_ids=None, hit_known=True):
    """Return (status_code, body). 400 means nothing was written."""
    error = _validate(hit_id, action, rationale, analyst, notes, idempotency_key, cited_ids, allowed_ids, hit_known)
    if error:
        return 400, {"error": error}
    cited = [str(item) for item in (cited_ids or [])]
    with _LOCK:
        events = _read(path)
        if idempotency_key:
            for event in events:
                if event.get("idempotency_key") == idempotency_key:
                    return 200, {"status": "duplicate", "event": event}
        prior = [event for event in events if event.get("hit_id") == hit_id]
        previous = prior[-1]["action"] if prior else None
        if action == "REOPEN" and previous is None:
            return 400, {"error": "There is no review to reopen."}
        if (
            action != "REOPEN"
            and prior
            and previous == action
            and prior[-1].get("rationale") == rationale
            and list(prior[-1].get("cited_ids") or []) == cited
        ):
            return 200, {"status": "unchanged", "event": prior[-1]}
        event = {
            "event_id": uuid.uuid4().hex,
            "hit_id": hit_id,
            "action": action,
            "rationale": rationale,
            "notes": notes or "",
            "analyst": analyst,
            "cited_ids": cited,
            "idempotency_key": idempotency_key,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "previous_action": previous,
        }
        _append(path, event)
    return 201, {"status": "recorded", "event": event}


def history(path, hit_id):
    return [event for event in _read(path) if event.get("hit_id") == hit_id]


def _validate(hit_id, action, rationale, analyst, notes, idempotency_key, cited_ids, allowed_ids, hit_known):
    if not hit_known:
        return "Unknown hit."
    if not isinstance(hit_id, str) or not hit_id.strip():
        return "A hit id is required."
    if action not in ACTIONS:
        return "Action must be Confirm match, Clear match, Escalate, or Reopen."
    if not isinstance(rationale, str) or len(rationale.strip()) < 10 or len(rationale) > 2000:
        return "Rationale must be between 10 and 2000 characters."
    if not isinstance(analyst, str) or not analyst.strip() or len(analyst.strip()) > 80:
        return "Analyst name must be between 1 and 80 characters."
    if notes is None:
        notes = ""
    if not isinstance(notes, str) or len(notes) > 2000:
        return "Notes must be at most 2000 characters."
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 80:
            return "Idempotency key must be between 1 and 80 characters."
    if cited_ids is None:
        cited_ids = []
    if not isinstance(cited_ids, list) or len(cited_ids) > 20:
        return "Cited ids must be a list of at most 20."
    if cited_ids:
        if not isinstance(allowed_ids, (set, list, tuple)):
            return "Cited ids were not checked against the case."
        allowed = set(allowed_ids)
        unknown = [str(item) for item in cited_ids if str(item) not in allowed]
        if unknown:
            return "These evidence ids are not in the case: " + ", ".join(unknown)
    return None


def _read(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _append(path, event):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
