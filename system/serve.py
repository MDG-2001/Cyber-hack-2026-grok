#!/usr/bin/env python3
"""Local demo of the decisions. No network, no extra packages.

    python3 system/serve.py <data folder> <output file>

Then open the printed address. The page shows each alert, the decision on
every hit, and the records the decision cites.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen.analyst_log import history as analyst_history
from screen.analyst_log import unreadable_lines
from screen.analyst_log import record as analyst_record
from screen.brief import build_brief
from screen.dashboard import name_only_decision, summarise
from screen.engine import World
from screen.intake import screen_name_error
from screen.investigate import investigate

PORT = 8766
MAX_BODY = 64 * 1024
_STATE_CHANGING = {"/api/analyst", "/api/investigate"}


def own_origin(value, port):
    """True when the header is absent or is this server on localhost."""
    if not value:
        return True
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost"):
        return False
    if parsed.port not in (None, port):
        return False
    return True


def state_change_error(path, content_type, origin, referer, token, expected, port):
    """403 reason for a state-changing POST, or None when the request may proceed."""
    if path not in _STATE_CHANGING:
        return None
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return "Content-Type must be application/json."
    if not own_origin(origin, port) or not own_origin(referer, port):
        return "This request was refused because its origin is not this server."
    presented = token or ""
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return "Missing or invalid X-Doppel-Token."
    return None


def parse_json_object(raw):
    """Return (status, error, payload). status is None when payload is an object."""
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "Body was not JSON."}, None
    if not isinstance(payload, dict):
        return 400, {"error": "JSON body must be an object."}, None
    return None, None, payload


def bind_host(host):
    """Return (host, warning). The warning is set when the host is not loopback."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return host, None
    return host, "warning: listening on " + host + ", which is not only this machine"


def serve_args(argv, environ=None):
    """Parse [--host HOST] <data> <output>. HOST env is the default."""
    environ = os.environ if environ is None else environ
    host = environ.get("HOST") or "127.0.0.1"
    args = []
    index = 1
    while index < len(argv):
        if argv[index] == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
            index += 2
            continue
        args.append(argv[index])
        index += 1
    if len(args) != 2:
        return None
    return host, args[0], args[1]


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def as_text(value):
    """Display text. A converted workbook can store one field as a string or a list."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (as_text(item) for item in value) if part)
    return str(value).strip()


def as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [as_text(item) for item in value if as_text(item)]
    return [as_text(value)]


def load_bundle(folder: str, output: str) -> dict:
    alerts = {row["alert_id"]: row for row in read_jsonl(os.path.join(folder, "alerts.jsonl"))}
    hits = {row["hit_id"]: row for row in read_jsonl(os.path.join(folder, "hits.jsonl"))}
    predictions = read_jsonl(output)

    customers = {}
    for row in read_jsonl(os.path.join(folder, "customers.jsonl")):
        customers[row["record_id"]] = {
            "kind": "Customer record",
            "title": as_text(row.get("full_name")) or row["record_id"],
            "lines": [
                as_text(row.get("entity_type")),
                ("Born " + as_text(row.get("dob"))) if as_text(row.get("dob")) else "",
                ", ".join(
                    part
                    for part in (
                        as_text(row.get("nationality")),
                        as_text(row.get("residence_country")),
                        as_text(row.get("city")),
                        as_text(row.get("address")),
                    )
                    if part
                ),
            ],
        }
    checks = {}
    for row in read_jsonl(os.path.join(folder, "kyc_checks.jsonl")):
        checked = " ".join(
            part
            for part in (as_text(row.get("result")), as_text(row.get("checked_at")))
            if part
        )
        checks[row["check_id"]] = {
            "kind": "Identity check",
            "title": as_text(row.get("verified_name")) or row["check_id"],
            "lines": [
                checked,
                ("Born " + as_text(row.get("verified_dob"))) if as_text(row.get("verified_dob")) else "",
                ("Document " + as_text(row.get("document_type"))) if as_text(row.get("document_type")) else "",
                ("Document country " + as_text(row.get("document_country"))) if as_text(row.get("document_country")) else "",
                ("Number " + as_text(row.get("document_number"))) if as_text(row.get("document_number")) else "",
            ],
        }
    notes = {}
    for row in read_jsonl(os.path.join(folder, "notes.jsonl")):
        notes[row["note_id"]] = {
            "kind": "Note · " + (as_text(row.get("channel")) or "note"),
            "title": as_text(row.get("created_at")),
            "lines": [as_text(row.get("text"))],
        }
    searches = {}
    for row in read_jsonl(os.path.join(folder, "search_results.jsonl")):
        searches[row["result_id"]] = {
            "kind": "Search result",
            "title": as_text(row.get("title")) or row["result_id"],
            "lines": [as_text(row.get("snippet")), as_text(row.get("query"))],
        }
    records = {}
    records.update(customers)
    records.update(checks)
    records.update(notes)
    records.update(searches)

    by_alert = defaultdict(list)
    for row in predictions:
        hit = hits.get(row["hit_id"], {})
        by_alert[row["alert_id"]].append(
            {
                "hit_id": row["hit_id"],
                "name": as_text(hit.get("name")) or row["hit_id"],
                "aliases": as_list(hit.get("aliases")),
                "dobs": as_list(hit.get("dobs")),
                "countries": as_list(hit.get("countries")) + as_list(hit.get("nationality")) + as_list(hit.get("citizenship")),
                "entity_type": as_text(hit.get("entity_type")),
                "categories": as_list(hit.get("categories")),
                "primary_category": as_text(hit.get("primary_category")),
                "sources": as_list(hit.get("sources")),
                "position_or_allegation": as_text(hit.get("position_or_allegation")),
                "decision": row["decision"],
                "reason_code": row["reason_code"],
                "customer_record_id": row.get("customer_record_id"),
                "reason": row["reason"],
                "evidence": [
                    {"id": item, **records.get(item, {"kind": "Record", "title": item, "lines": []})}
                    for item in row.get("evidence") or []
                ],
            }
        )

    order = {"SAME_ENTITY": 0, "REVIEW": 1, "DIFFERENT_ENTITY": 2}
    alerts_out = []
    for alert_id, alert_hits in by_alert.items():
        alert = alerts.get(alert_id, {})
        screened = alert.get("customer") or {}
        alert_hits.sort(key=lambda item: (order.get(item["decision"], 9), item["hit_id"]))
        alerts_out.append(
            {
                "alert_id": alert_id,
                "screened_at": as_text(alert.get("screened_at")),
                "name": as_text(screened.get("name")) or alert_id,
                "entity_type": as_text(screened.get("entity_type")),
                "dob": as_text(screened.get("dob")),
                "country": as_text(screened.get("country")),
                "city": as_text(screened.get("city")),
                "link": as_text(alert.get("account_ref") or alert.get("login_handle") or alert.get("email")),
                "hits": alert_hits,
            }
        )
    alerts_out.sort(key=lambda item: item["alert_id"])
    counts = {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 0, "REVIEW": 0}
    for row in predictions:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    return {
        "alerts": alerts_out,
        "counts": counts,
        "hits": len(predictions),
        "demos": demo_picks(predictions),
        "has_practice": os.path.exists(os.path.join(folder, "practice_labels.jsonl")),
    }


def demo_picks(predictions):
    """Three alerts worth showing in three minutes, chosen from the saved file."""
    picks = []
    same = next((row for row in predictions if row.get("reason_code") == "NAME_DOB_LOCATION_MATCH"), None)
    review = next((row for row in predictions if row.get("decision") == "REVIEW"), None)
    widest = None
    gap = -1
    for row in predictions:
        if row.get("reason_code") != "DOB_MISMATCH":
            continue
        years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", row.get("reason") or "")]
        if len(years) < 2:
            continue
        width = max(years) - min(years)
        if width > gap:
            gap = width
            widest = row
    if widest:
        picks.append({
            "label": "Large date gap",
            "alert_id": widest["alert_id"],
            "hit_id": widest["hit_id"],
            "blurb": "Cleared because the birth dates are " + str(gap) + " years apart",
        })
    if same:
        picks.append({
            "label": "Same person",
            "alert_id": same["alert_id"],
            "hit_id": same["hit_id"],
            "blurb": "Name, date of birth and country all agree",
        })
    if review:
        picks.append({
            "label": "Needs review",
            "alert_id": review["alert_id"],
            "hit_id": review["hit_id"],
            "blurb": "Nothing contradicts the hit, and nothing confirms it",
        })
    return picks


COST = {
    "DIFFERENT_ENTITY": {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 3, "REVIEW": 3},
    "SAME_ENTITY": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 0, "REVIEW": 2},
    "REVIEW": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 2, "REVIEW": 0},
}


def practice_report(labels, answers, live=False):
    rows = []
    for label in labels:
        answer = answers.get(label["hit_id"], {})
        expected = label.get("decision")
        got = answer.get("decision")
        codes = label.get("reason_codes") or []
        code_ok = answer.get("reason_code") in codes
        rows.append({
            "hit_id": label["hit_id"],
            "alert_id": label.get("alert_id") or answer.get("alert_id"),
            "expected": expected,
            "got": got,
            "match": got == expected,
            "expected_codes": codes,
            "got_code": answer.get("reason_code"),
            "code_ok": code_ok,
            "unsafe": expected != "DIFFERENT_ENTITY" and got == "DIFFERENT_ENTITY",
        })
    cost = sum(COST[row["expected"]][row["got"] or "REVIEW"] for row in rows if row["expected"] in COST)
    baseline = sum(COST[row["expected"]]["REVIEW"] for row in rows if row["expected"] in COST)
    right = [row for row in rows if row["match"]]
    unsafe = sum(1 for row in rows if row["unsafe"])
    score = 100 * max(0.0, 1 - cost / baseline) if baseline else 100.0
    return {
        "total": len(rows),
        "right": len(right),
        "unsafe": unsafe,
        "score": round(score, 1),
        "code_ok": sum(1 for row in right if row["code_ok"]),
        "live": live,
        "rows": rows,
    }


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doppelgänger</title>
<style>
  :root {
    --paper: #f3efe4;
    --ink: #1d1a16;
    --muted: #6d655b;
    --line: #ddd4c4;
    --card: #fffdf8;
    --cleared: #1c6b45;
    --cleared-bg: #e5f4ec;
    --same: #8e1d2b;
    --same-bg: #f8e6e8;
    --review: #8a5a10;
    --review-bg: #f8efd8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    color: var(--ink);
    background: var(--paper);
    font: 16px/1.45 "Iowan Old Style", Palatino, Georgia, serif;
  }
  header {
    padding: 18px 28px 16px;
    border-bottom: 1px solid var(--line);
    display: flex;
    justify-content: space-between;
    gap: 20px;
    flex-wrap: wrap;
    align-items: center;
    background: #faf7f1;
  }
  a.brand {
    display: flex; gap: 14px; align-items: center;
    color: inherit; text-decoration: none; cursor: pointer;
  }
  a.brand:hover h1 { text-decoration: underline; }
  .mark {
    width: 42px; height: 42px; border-radius: 12px;
    background: var(--ink); color: var(--paper);
    display: grid; place-items: center;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-weight: 750; font-size: 18px; letter-spacing: -0.04em;
  }
  h1 { font-size: 26px; font-weight: 650; margin: 0; letter-spacing: -0.03em; }
  header p { margin: 2px 0 0; color: var(--muted); }
  .counts { display: flex; gap: 8px; flex-wrap: wrap; }
  .count {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 8px 12px;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 13px;
    cursor: pointer;
    color: inherit;
    text-align: left;
    min-width: 92px;
  }
  .count b { display: block; font-size: 18px; letter-spacing: -0.03em; }
  .count.active { border-color: var(--ink); background: #fff; }
  .count.cleared b { color: var(--cleared); }
  .count.same b { color: var(--same); }
  .count.review b { color: var(--review); }
  main { display: grid; grid-template-columns: 320px 1fr; min-height: calc(100vh - 92px); }
  aside { border-right: 1px solid var(--line); padding: 14px; }
  input {
    width: 100%;
    font: 15px/1.3 ui-sans-serif, system-ui, sans-serif;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--card);
    color: var(--ink);
  }
  .nav {
    display: flex; justify-content: space-between; align-items: center;
    gap: 8px; margin-top: 10px;
    font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; color: var(--muted);
  }
  .nav button {
    border: 1px solid var(--line); background: var(--card); border-radius: 8px;
    padding: 4px 10px; cursor: pointer; font: inherit; color: var(--ink);
  }
  #list { margin-top: 8px; max-height: calc(100vh - 360px); overflow: auto; }
  .alert-btn .list-badge { font-size: 10px; font-family: ui-sans-serif, system-ui, sans-serif; }
  .alert-btn {
    width: 100%;
    text-align: left;
    border: 0;
    border-radius: 10px;
    background: transparent;
    padding: 10px 10px;
    cursor: pointer;
    color: inherit;
    font: inherit;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    align-items: center;
  }
  .alert-btn strong { display: block; font-size: 16px; }
  .alert-btn span { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }
  .alert-btn.active, .alert-btn:hover { background: #fff; }
  .mini {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 10px; font-weight: 750; letter-spacing: 0.04em;
    text-transform: uppercase; border-radius: 999px; padding: 3px 7px; white-space: nowrap;
  }
  .mini.SAME_ENTITY { background: var(--same-bg); color: var(--same); }
  .mini.REVIEW { background: var(--review-bg); color: var(--review); }
  .mini.DIFFERENT_ENTITY { background: var(--cleared-bg); color: var(--cleared); }
  .lists { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .list-badge, .list-chip, .source {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 10px; font-weight: 750; letter-spacing: 0.03em;
    text-transform: uppercase; border-radius: 999px; padding: 3px 7px;
    white-space: nowrap; border: 1px solid transparent;
  }
  .list-badge.sanctions, .list-chip.sanctions {
    background: #6e1220; color: #fff; border-color: #6e1220;
    font-size: 11px; padding: 4px 9px;
  }
  .list-badge.pep, .list-chip.pep { background: #e4e9f7; color: #24356b; }
  .list-badge.adverse, .list-chip.adverse { background: #f4e3c4; color: #7a4b08; }
  .list-badge.enforcement, .list-chip.enforcement { background: #efe4f6; color: #5b2a78; }
  .list-badge.registration, .list-chip.registration { background: #d9f0ea; color: #0e5c4d; }
  .list-badge.soe, .list-chip.soe { background: #e4e7ee; color: #243044; }
  .list-badge.other, .list-chip.other { background: #eeeae4; color: #5c564e; }
  .list-filters { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .list-chip { cursor: pointer; background: #fff; border-color: var(--line); color: var(--ink); text-transform: none; letter-spacing: 0; font-size: 12px; font-weight: 650; }
  .list-chip.sanctions { text-transform: uppercase; }
  .list-chip.active { box-shadow: 0 0 0 2px var(--ink); }
  .filter-label { margin: 12px 0 0; }
  .sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
  .source {
    text-transform: none; letter-spacing: 0; font-weight: 600; font-size: 12px;
    background: #faf7f1; color: var(--ink); border-color: var(--line); border-radius: 8px;
    white-space: normal;
  }
  .allegation { margin: 8px 0 4px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 14px; }
  .brief { margin-top: 12px; }
  .brief h3 { margin: 14px 0 6px; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }
  .fact-row, .contra-row, .missing-row { border-left: 4px solid var(--line); padding: 6px 10px; margin: 6px 0; background: #faf7f1; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 14px; }
  .fact-row { border-left-color: var(--cleared); }
  .contra-row { border-left-color: var(--same); }
  .missing-row { border-left-color: var(--review); }
  .advisory { border: 1px dashed var(--line); border-radius: 10px; padding: 10px 12px; margin-top: 10px; background: #fff; }
  .tag { font-family: ui-sans-serif, system-ui, sans-serif; font-size: 10px; font-weight: 750; letter-spacing: .04em; text-transform: uppercase; margin-right: 6px; }
  .tag.verified { color: var(--cleared); }
  .tag.interpretation { color: var(--review); }
  .tag.unsupported { color: var(--same); }
  .record-link { background: none; border: 0; padding: 0; color: inherit; text-decoration: underline; cursor: pointer; font: inherit; }
  section { padding: 22px 28px 48px; }
  .screened {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 16px;
  }
  .screened h2 { margin: 0 0 4px; font-size: 22px; }
  .meta { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }
  .hit {
    background: var(--card);
    border: 1px solid var(--line);
    border-left: 6px solid var(--line);
    border-radius: 14px;
    padding: 14px 16px 12px;
    margin: 0 0 12px;
  }
  .hit.SAME_ENTITY { border-left-color: var(--same); }
  .hit.REVIEW { border-left-color: var(--review); }
  .hit.DIFFERENT_ENTITY { border-left-color: var(--cleared); }
  .hit-top { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
  .pill {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    border-radius: 999px;
    padding: 4px 10px;
    white-space: nowrap;
  }
  .SAME_ENTITY .pill { background: var(--same-bg); color: var(--same); }
  .REVIEW .pill { background: var(--review-bg); color: var(--review); }
  .DIFFERENT_ENTITY .pill { background: var(--cleared-bg); color: var(--cleared); }
  .code { font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted); }
  .reason, .card, .audit, .allegation, .fact-row, .contra-row, .missing-row, .source, .meta, .notice {
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .reason { margin: 8px 0 12px; }
  .notice { margin: 0; padding: 8px 28px; background: var(--review-bg); color: var(--review); }
  .mini.human { background: #fff; color: var(--ink); border: 1px solid var(--ink); }
  .table-scroll { width: 100%; max-width: 100%; overflow-x: auto; }
  .evidence-label {
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted); margin: 0 0 8px;
  }
  .evidence { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .card {
    background: #faf7f1;
    border-radius: 10px;
    padding: 10px 12px;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 14px;
  }
  .card b { font-family: ui-monospace, monospace; font-size: 12px; }
  .card .kind { color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); padding: 24px 4px; }
  .workload {
    display: flex; gap: 18px; flex-wrap: wrap; align-items: end;
    padding: 12px 28px 4px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px;
  }
  .workload strong { display: block; font-size: 20px; letter-spacing: -0.03em; }
  .workload .hint { color: var(--muted); max-width: 36rem; }
  .alert-status { display: inline-block; margin-left: 8px; }
  .picture { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; margin-bottom: 14px; }
  .picture h2 { margin: 0 0 6px; font-size: 18px; }
  .toolbar {
    display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    padding: 10px 28px; border-bottom: 1px solid var(--line); background: var(--paper);
  }
  .modes, .demos { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .modes button, .demos button, .run, .try button {
    border: 1px solid var(--line); background: #fff; border-radius: 999px;
    padding: 6px 12px; cursor: pointer;
    font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; color: var(--ink);
  }
  .modes button.active, .run { background: var(--ink); color: var(--paper); border-color: var(--ink); }
  #mode-screen { background: #1d1a16; color: #f3efe4; border-color: #1d1a16; font-weight: 700; }
  .compare { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0 4px; }
  .compare > div { background: #faf7f1; border-radius: 10px; padding: 10px 12px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 14px; }
  .compare h3 { margin: 0 0 6px; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .ambiguous { background: var(--review-bg); color: var(--review); border-radius: 10px; padding: 8px 12px; margin: 8px 0; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 14px; }
  .trace { margin-top: 8px; }
  .step {
    border: 1px solid var(--line); border-left: 5px solid var(--muted); border-radius: 10px;
    padding: 8px 12px; margin: 8px 0; background: #fff;
    opacity: 0; transform: translateY(8px);
    animation: rise .4s ease forwards;
  }
  .step.pass { border-left-color: var(--cleared); }
  .step.contradiction { border-left-color: var(--same); }
  .step.unknown { border-left-color: var(--review); }
  .step b { font-family: ui-sans-serif, system-ui, sans-serif; }
  .vs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }
  .tick { color: var(--cleared); font-weight: 700; }
  .cross { color: var(--same); font-weight: 700; }
  .scorehead { font-family: ui-sans-serif, system-ui, sans-serif; }
  table.score { width: 100%; border-collapse: collapse; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 14px; background: #fff; }
  table.score th, table.score td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  form.try label { display: block; margin: 10px 0 4px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }
  form.try input, form.try select { width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font: 15px ui-sans-serif, system-ui, sans-serif; }
  main.single { grid-template-columns: 1fr; }
  @keyframes rise { to { opacity: 1; transform: none; } }
  .dash { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin: 8px 0 14px; }
  .dash .count { cursor: default; }
  .analyst { margin-top: 12px; padding-top: 8px; border-top: 1px solid var(--line); }
  .analyst textarea { min-height: 72px; }
  .audit { margin: 8px 0; }
  .audit .card { margin-top: 6px; }
  @media (max-width: 800px) {
    main { grid-template-columns: 1fr; }
    aside { border-right: 0; border-bottom: 1px solid var(--line); }
    #list { max-height: 240px; }
    .compare, .vs, .pair { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <a class="brand" id="home" href="/">
    <div class="mark">D</div>
    <div>
      <h1>Doppelgänger</h1>
      <p>Is this hit the customer, or just the same name?</p>
    </div>
  </a>
  <div class="counts" id="counts"></div>
</header>
<div class="workload" id="workload"></div>
<div class="toolbar">
  <div class="modes">
    <button type="button" id="mode-screen">Screen a new customer</button>
    <button type="button" id="mode-dashboard">Dashboard</button>
    <button type="button" id="mode-alerts" class="active">Alerts</button>
    <button type="button" id="mode-answers">Test against answers</button>
    <button type="button" id="mode-try">Try your own</button>
  </div>
  <div class="demos" id="demos"><span class="meta">Demo picks</span></div>
</div>
<p id="notice" class="notice" hidden></p>
<main id="layout">
  <aside id="browser">
    <input id="q" placeholder="Search a name, alert or hit" autocomplete="off">
    <div class="meta filter-label">List type, every list a hit is on</div>
    <div id="list-filters" class="list-filters"></div>
    <div class="nav">
      <button id="prev" type="button">Previous</button>
      <span id="pos">Alerts</span>
      <button id="next" type="button">Next</button>
    </div>
    <div id="list"></div>
  </aside>
  <section id="detail"><p class="empty">Loading the development set…</p></section>
</main>
<script>
const DOPPEL_TOKEN = "__DOPPEL_TOKEN__";
function writeHeaders() {
  return {"Content-Type": "application/json", "X-Doppel-Token": DOPPEL_TOKEN};
}
const LABELS = {
  DIFFERENT_ENTITY: "Cleared",
  SAME_ENTITY: "Same person",
  REVIEW: "Review"
};
let alerts = [];
let current = null;
let filter = "ALL";
let alertFilter = "ALL";
let listFilter = "ALL";
let listLimit = 120;
let humanLatest = {};
const HUMAN_LABELS = {
  CONFIRM_MATCH: "Human: confirmed",
  CLEAR_MATCH: "Human: cleared",
  ESCALATE: "Human: escalated",
  REOPEN: "Human: reopened"
};
let paintListFilters = () => {};
const LIST_ORDER = ["Sanctions", "PEP", "Adverse Media", "Enforcement", "Registration", "SOE", "Other Segment"];

function listClass(name) {
  const key = String(name || "").toLowerCase();
  if (key === "sanctions") return "sanctions";
  if (key === "pep") return "pep";
  if (key === "adverse media") return "adverse";
  if (key === "enforcement") return "enforcement";
  if (key === "registration") return "registration";
  if (key === "soe") return "soe";
  return "other";
}

function hitLists(hit) {
  const lists = [];
  const primary = (hit.primary_category || "").trim();
  if (primary) lists.push(primary);
  (hit.categories || []).forEach(item => {
    const name = String(item || "").trim();
    if (name && !lists.includes(name)) lists.push(name);
  });
  return lists;
}

function sortLists(names) {
  return names.slice().sort((a, b) => {
    const ia = LIST_ORDER.indexOf(a);
    const ib = LIST_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
}

function listBadges(names) {
  const row = el("div", "lists");
  sortLists(names).forEach(name => row.append(el("span", "list-badge " + listClass(name), name)));
  return row;
}

function watchlistSources(hit) {
  if (Array.isArray(hit.list_sources)) return hit.list_sources.filter(Boolean);
  const sources = hit.sources || [];
  if (sources.length && typeof sources[0] === "string") return sources.filter(Boolean);
  return [];
}

function appendListFacts(parent, hit) {
  const lists = hitLists(hit);
  if (lists.length) parent.append(listBadges(lists));
  if (hit.position_or_allegation) parent.append(el("p", "allegation", hit.position_or_allegation));
  const sources = watchlistSources(hit);
  if (!sources.length) return;
  parent.append(el("div", "evidence-label", "List sources"));
  const row = el("div", "sources");
  const paint = items => {
    row.replaceChildren();
    items.forEach(source => row.append(el("span", "source", source)));
  };
  paint(sources.length > 8 ? sources.slice(0, 8) : sources);
  parent.append(row);
  if (sources.length > 8) {
    const more = el("button", "list-chip", "Show all " + sources.length + " sources");
    more.type = "button";
    more.onclick = () => { paint(sources); more.remove(); };
    parent.append(more);
  }
}

function alertStatus(alert) {
  if (alert.hits.some(hit => hit.decision === "SAME_ENTITY")) return "ESCALATED";
  if (alert.hits.length && alert.hits.every(hit => hit.decision === "DIFFERENT_ENTITY")) return "CLEARED";
  return "ANALYST";
}

function headline(alert) {
  if (alert.hits.some(hit => hit.decision === "SAME_ENTITY")) return "SAME_ENTITY";
  if (alert.hits.some(hit => hit.decision === "REVIEW")) return "REVIEW";
  return "DIFFERENT_ENTITY";
}

function visible() {
  const query = document.getElementById("q").value.trim().toLowerCase();
  return alerts.filter(alert => {
    if (alertFilter !== "ALL" && alertStatus(alert) !== alertFilter) return false;
    if (filter !== "ALL" && !alert.hits.some(hit => hit.decision === filter)) return false;
    if (listFilter !== "ALL" && !alert.hits.some(hit => hitLists(hit).includes(listFilter))) return false;
    if (!query) return true;
    const blob = [alert.alert_id, alert.name, alert.link, ...alert.hits.flatMap(hit => [hit.hit_id, hit.name, hit.decision, hit.reason_code, hit.primary_category, ...(hit.categories || []), ...(watchlistSources(hit))])].join(" ").toLowerCase();
    return blob.includes(query);
  });
}

function step(delta) {
  const rows = visible();
  if (!rows.length) return;
  let index = rows.findIndex(alert => alert.alert_id === current);
  if (index < 0) index = 0;
  show(rows[(index + delta + rows.length) % rows.length]);
  const active = document.querySelector(".alert-btn.active");
  if (active) active.scrollIntoView({block: "nearest"});
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

let suppressHistory = false;

function remember(query) {
  if (suppressHistory) return;
  const next = location.pathname + (query ? "?" + query : "") + location.hash;
  const now = location.pathname + location.search + location.hash;
  if (next === now) return;
  history.pushState({doppel: query || ""}, "", next);
}

function homeAlert() {
  return alerts.find(alert => alert.hits.some(hit => hit.decision === "SAME_ENTITY")) || alerts[0];
}

function goHome(fromPop) {
  filter = "ALL";
  alertFilter = "ALL";
  listFilter = "ALL";
  paintListFilters();
  const search = document.getElementById("q");
  if (search) search.value = "";
  document.querySelectorAll("#counts .count").forEach(node => {
    node.classList.toggle("active", node.dataset.filter === "ALL");
  });
  document.querySelectorAll("#workload .count").forEach(node => node.classList.remove("active"));
  current = null;
  suppressHistory = true;
  setMode("alerts");
  const start = homeAlert();
  if (start) show(start);
  suppressHistory = false;
  if (!fromPop) remember("");
  window.scrollTo(0, 0);
  const list = document.getElementById("list");
  if (list) list.scrollTop = 0;
}

function applyLocation() {
  const params = new URLSearchParams(location.search);
  filter = "ALL";
  alertFilter = "ALL";
  listFilter = "ALL";
  paintListFilters();
  const search = document.getElementById("q");
  if (search) search.value = params.get("q") || "";
  document.querySelectorAll("#counts .count, #workload .count").forEach(node => node.classList.remove("active"));
  const allChip = document.querySelector('#counts .count[data-filter="ALL"]');
  if (allChip) allChip.classList.add("active");
  suppressHistory = true;
  const view = params.get("view");
  if (view === "screen" || view === "answers" || view === "try" || view === "dashboard") {
    setMode(view);
  } else {
    setMode("alerts");
    const preset = params.get("alert");
    const found = preset && alerts.find(alert => alert.alert_id === preset);
    const notice = document.getElementById("notice");
    if (preset && !found) {
      notice.hidden = false;
      notice.textContent = "Alert " + preset + " not found.";
    } else if (notice) {
      notice.hidden = true;
      notice.textContent = "";
    }
    const start = found || homeAlert();
    if (start) show(start);
  }
  suppressHistory = false;
  window.scrollTo(0, 0);
}

function show(alert) {
  current = alert.alert_id;
  if (!suppressHistory) remember("alert=" + encodeURIComponent(alert.alert_id));
  const rows = visible();
  const index = rows.findIndex(item => item.alert_id === current);
  const pos = document.getElementById("pos");
  if (pos) pos.textContent = rows.length ? ((index < 0 ? 1 : index + 1) + " of " + rows.length) : "No alerts";
  document.querySelectorAll(".alert-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.id === alert.alert_id);
  });
  const root = document.getElementById("detail");
  root.replaceChildren();
  const screened = el("div", "screened");
  screened.append(el("div", "meta", alert.alert_id + (alert.screened_at ? " · screened " + alert.screened_at : "")));
  const status = alertStatus(alert);
  const statusLabel = {CLEARED: "Cleared", ESCALATED: "Escalated to compliance", ANALYST: "Needs analyst"}[status];
  const title = el("h2", null, "");
  title.append(document.createTextNode(alert.name));
  title.append(el("span", "pill alert-status " + (status === "CLEARED" ? "DIFFERENT_ENTITY" : status === "ESCALATED" ? "SAME_ENTITY" : "REVIEW"), "Engine: " + statusLabel));
  const human = latestHuman(alert);
  if (human) title.append(el("span", "pill mini human", HUMAN_LABELS[human.action] || human.action));
  screened.append(title);
  const bits = [alert.entity_type, alert.dob, alert.country, alert.city, alert.link].filter(Boolean);
  screened.append(el("div", "meta", bits.join(" · ") || "No screened details beyond the name"));
  root.append(screened);
  const picture = el("div", "picture");
  picture.append(el("h2", null, "Customer picture"));
  picture.append(el("p", "meta", "Loading the records behind this alert…"));
  root.append(picture);
  fetch("/api/picture?alert_id=" + encodeURIComponent(alert.alert_id))
    .then(response => response.json())
    .then(data => renderPicture(picture, data))
    .catch(() => { picture.append(el("p", null, "Could not load the customer picture.")); });
  for (const hit of alert.hits) {
    const card = el("article", "hit " + hit.decision);
    const top = el("div", "hit-top");
    const who = el("div");
    who.append(el("strong", null, hit.name));
    const lists = hitLists(hit);
    if (lists.length) who.append(listBadges(lists));
    who.append(el("div", "meta", [hit.hit_id, hit.entity_type].filter(Boolean).join(" · ")));
    top.append(who);
    top.append(el("span", "pill", LABELS[hit.decision] || hit.decision));
    card.append(top);
    const record = hit.customer_record_id ? "Customer " + hit.customer_record_id : "No customer record";
    card.append(el("div", "code", hit.reason_code + " · " + record));
    const compare = el("div", "compare");
    const left = el("div");
    left.append(el("h3", null, "Customer, as screened"));
    left.append(el("div", null, alert.name));
    left.append(el("div", null, [alert.entity_type, alert.dob, alert.country, alert.city].filter(Boolean).join(" · ") || "No further screened details"));
    const right = el("div");
    right.append(el("h3", null, "Listed profile"));
    right.append(el("div", null, hit.name));
    const alias = (hit.aliases || []).length ? "Also known as " + hit.aliases.join(", ") : "";
    const listed = [hit.entity_type, (hit.dobs || []).join(", "), (hit.countries || []).join(", "), alias].filter(Boolean).join(" · ");
    right.append(el("div", null, listed || "No further list details"));
    compare.append(left, right);
    card.append(compare);
    if (hit.position_or_allegation) card.append(el("p", "allegation", hit.position_or_allegation));
    const sources = watchlistSources(hit);
    if (sources.length) {
      card.append(el("div", "evidence-label", "List sources"));
      const row = el("div", "sources");
      const paint = items => {
        row.replaceChildren();
        items.forEach(source => row.append(el("span", "source", source)));
      };
      paint(sources.length > 8 ? sources.slice(0, 8) : sources);
      card.append(row);
      if (sources.length > 8) {
        const more = el("button", "list-chip", "Show all " + sources.length + " sources");
        more.type = "button";
        more.onclick = () => { paint(sources); more.remove(); };
        card.append(more);
      }
    }
    if (hit.customer_record_id === "AMBIGUOUS") {
      card.append(el("div", "ambiguous", "More than one customer fits this alert. Run the decision to see how many, and whether they all lead to the same verdict."));
    }
    const run = el("button", "run", "Run decision");
    run.type = "button";
    run.onclick = () => runHit(hit.hit_id, trace);
    card.append(run);
    const trace = el("div", "trace");
    card.append(trace);
    card.append(el("p", "reason", hit.reason));
    card.append(el("div", "evidence-label", "Evidence"));
    const evidence = el("div", "evidence");
    for (const item of hit.evidence) {
      const box = el("div", "card");
      box.append(el("div", "kind", item.kind || "Record"));
      box.append(el("div", null, ""));
      const title = document.createElement("div");
      const id = el("b", null, item.id);
      title.append(id, document.createTextNode("  " + (item.title || "")));
      box.append(title);
      for (const line of (item.lines || []).filter(Boolean)) box.append(el("div", null, line));
      evidence.append(box);
    }
    card.append(evidence);
    const briefHost = el("div", "brief");
    briefHost.append(el("p", "meta", "Loading the case brief…"));
    card.append(briefHost);
    fetch("/api/brief?hit_id=" + encodeURIComponent(hit.hit_id))
      .then(response => response.json())
      .then(data => renderBrief(briefHost, data))
      .catch(() => { briefHost.replaceChildren(el("p", "cross", "The case brief could not be loaded.")); });
    root.append(card);
  }
}

function renderBrief(host, data) {
  host.replaceChildren();
  if (!data || data.error) {
    host.append(el("p", null, data && data.error ? data.error : "No case brief."));
    return;
  }
  const decision = data.deterministic || {};
  host.append(el("h3", null, "Screening result"));
  host.append(el("div", null, (LABELS[decision.decision] || decision.decision || "") + (decision.reason_code ? " · " + decision.reason_code : "")));
  host.append(el("p", "reason", decision.reason || ""));
  host.append(el("h3", null, "Verified customer"));
  (data.customer || []).forEach(person => {
    const line = [person.verified_name || "No verified legal name", person.date_of_birth ? "born " + person.date_of_birth : "no date of birth", (person.countries || []).join(", ")].filter(Boolean).join(" · ");
    host.append(el("div", "fact-row", line));
    if (person.verified_from) host.append(el("div", "meta", "Verified from " + person.verified_from + ". Rejected and expired checks are not used."));
  });
  if (!(data.customer || []).length) host.append(el("div", "missing-row", "No customer record was resolved."));
  const watch = data.watchlist || {};
  host.append(el("h3", null, "Watchlist candidate"));
  host.append(el("div", "fact-row", [watch.name, watch.entity_type, (watch.dobs || []).join(", "), (watch.countries || []).join(", "), watch.list_type].filter(Boolean).join(" · ")));
  host.append(el("h3", null, "Supporting evidence"));
  renderClaims(host, data.supporting, "fact-row", "Nothing in the records supports a match.");
  host.append(el("h3", null, "Contradicting evidence"));
  renderClaims(host, data.contradicting, "contra-row", "Nothing in the records contradicts the hit.");
  host.append(el("h3", null, "Missing information"));
  if ((data.missing || []).length) data.missing.forEach(line => host.append(el("div", "missing-row", line)));
  else host.append(el("div", "meta", "No gap was recorded."));
  if ((data.review_reasons || []).length) {
    host.append(el("h3", null, "Why a person still has to look"));
    data.review_reasons.forEach(line => host.append(el("div", "missing-row", line)));
  }
  renderAnalyst(host, data.hit_id, (data.records || []).map(record => record.id));
  host.append(el("h3", null, "Original records"));
  (data.records || []).forEach(record => {
    const box = el("div", "card");
    box.append(el("div", "kind", record.kind || "Record"));
    const title = el("div");
    const button = el("button", "record-link", record.id);
    button.type = "button";
    const body = el("div", null, record.text || "No text stored for this id.");
    body.hidden = true;
    button.onclick = () => { body.hidden = !body.hidden; };
    title.append(button, document.createTextNode("  " + (record.title || "")));
    box.append(title, body);
    host.append(box);
  });
  const advisory = el("div", "advisory");
  advisory.append(el("h3", null, "Grok advisory"));
  advisory.append(el("p", "meta", "This is an advisory. It does not change the screening decision. A live answer names its provider. If no key is set, nothing is invented."));
  const button = el("button", "run", "Request advisory");
  button.type = "button";
  const result = el("div");
  button.onclick = () => {
    result.replaceChildren(el("div", "meta", "Asking Grok. The screening decision will not change."));
    fetch("/api/investigate", {method: "POST", headers: writeHeaders(), body: JSON.stringify({hit_id: data.hit_id})})
      .then(response => response.json())
      .then(payload => renderAdvisory(result, payload, data.records || []))
      .catch(() => result.replaceChildren(el("div", "cross", "The advisory request failed. The screening decision is unchanged.")));
  };
  advisory.append(button, result);
  host.append(advisory);
}

function renderClaims(host, claims, className, empty) {
  if (!(claims || []).length) {
    host.append(el("div", "meta", empty));
    return;
  }
  claims.forEach(claim => {
    const row = el("div", className);
    row.append(el("span", "tag verified", "Verified fact"));
    row.append(document.createTextNode(claim.fact || ""));
    if ((claim.evidence_ids || []).length) row.append(el("div", "meta", claim.evidence_ids.join(", ")));
    host.append(row);
  });
}

function renderAnalyst(host, hitId, allowedIds) {
  const box = el("div", "analyst");
  box.append(el("h3", null, "Analyst decision"));
  box.append(el("p", "meta", "This is your decision. It is stored separately and does not change the screening result."));
  const name = document.createElement("input");
  name.placeholder = "Your name";
  name.setAttribute("aria-label", "Your name");
  name.value = localStorage.getItem("analyst-name") || "";
  const action = document.createElement("select");
  action.setAttribute("aria-label", "Decision");
  [["CONFIRM_MATCH", "Confirm match"], ["CLEAR_MATCH", "Clear match"], ["ESCALATE", "Escalate"], ["REOPEN", "Reopen"]].forEach(pair => {
    const option = el("option", null, pair[1]);
    option.value = pair[0];
    action.append(option);
  });
  const rationale = document.createElement("textarea");
  rationale.placeholder = "Why this decision (required)";
  rationale.setAttribute("aria-label", "Rationale");
  const notes = document.createElement("textarea");
  notes.placeholder = "Optional notes";
  notes.setAttribute("aria-label", "Notes");
  const button = el("button", "run", "Record decision");
  button.type = "button";
  const message = el("div");
  const audit = el("div", "audit");
  function showHistory() {
    audit.replaceChildren(el("p", "meta", "Loading the audit history…"));
    fetch("/api/analyst?hit_id=" + encodeURIComponent(hitId)).then(response => response.json()).then(payload => {
      audit.replaceChildren();
      const unread = payload.unreadable_lines || [];
      if (unread.length) {
        audit.append(el("p", "cross", unread.length + " unreadable log lines preserved at line " + unread.join(", ") + "."));
      }
      const events = payload.events || [];
      if (!events.length) {
        audit.append(el("p", "meta", "No analyst decision yet."));
        return;
      }
      events.forEach(event => {
        const card = el("div", "card");
        card.append(el("div", "kind", event.at + " · " + event.analyst));
        card.append(el("div", null, event.action + (event.previous_action ? " (was " + event.previous_action + ")" : "")));
        card.append(el("div", null, event.rationale));
        if (event.notes) card.append(el("div", "meta", event.notes));
        if ((event.cited_ids || []).length) card.append(el("div", "meta", "Cited " + event.cited_ids.join(", ")));
        audit.append(card);
      });
    }).catch(() => audit.replaceChildren(el("p", "cross", "The audit history could not be loaded.")));
  }
  button.onclick = () => {
    if (button.dataset.pending === "1") return;
    button.dataset.pending = "1";
    const key = button.dataset.key || (hitId + "-" + Date.now());
    button.dataset.key = key;
    localStorage.setItem("analyst-name", name.value);
    message.replaceChildren(el("p", "meta", "Saving…"));
    fetch("/api/analyst", {
      method: "POST",
      headers: writeHeaders(),
      body: JSON.stringify({
        hit_id: hitId,
        action: action.value,
        rationale: rationale.value,
        notes: notes.value,
        analyst: name.value,
        idempotency_key: key,
        cited_ids: allowedIds
      })
    }).then(response => response.json().then(payload => ({ok: response.ok, status: response.status, payload}))).then(result => {
      button.dataset.pending = "0";
      if (!result.ok) {
        message.replaceChildren(el("p", "cross", (result.payload && result.payload.error) || ("The decision was rejected (" + result.status + ").")));
        return;
      }
      button.dataset.key = "";
      message.replaceChildren(el("p", "tick", result.payload.status === "recorded" ? "Recorded." : "Already recorded."));
      refreshHumans().then(() => { renderList(); showHistory(); });
    }).catch(() => {
      button.dataset.pending = "0";
      message.replaceChildren(el("p", "cross", "The decision could not be saved. The screening result is unchanged."));
    });
  };
  const nameLabel = el("label", null, "Your name");
  const decisionLabel = el("label", null, "Decision");
  const rationaleLabel = el("label", null, "Rationale");
  const notesLabel = el("label", null, "Notes");
  box.append(nameLabel, name, decisionLabel, action, rationaleLabel, rationale, notesLabel, notes, button, message, audit);
  host.append(box);
  showHistory();
}

function renderAdvisory(host, payload, records) {
  host.replaceChildren();
  if (!payload) return;
  if (!payload.live) {
    host.append(el("div", "missing-row", payload.reason || payload.error || "Live Grok was not called."));
    host.append(el("div", "meta", "Source: " + (payload.source || "unavailable") + ". Screening decision: " + ((payload.deterministic || {}).decision || "unchanged") + "."));
    return;
  }
  host.append(el("div", null, ""));
  const banner = el("div", "fact-row");
  banner.append(el("span", "tag interpretation", "Grok advisory"));
  banner.append(document.createTextNode((payload.assessment || "UNCERTAIN") + " · model " + (payload.model || "")));
  host.append(banner);
  host.append(el("p", "meta", "Advisory only. The screening decision stays " + ((payload.deterministic || {}).decision || "as saved") + "."));
  (payload.supports || []).forEach(claim => host.append(advisoryClaim(claim, "fact-row", records)));
  (payload.contradicts || []).forEach(claim => host.append(advisoryClaim(claim, "contra-row", records)));
  (payload.missing || []).forEach(line => host.append(el("div", "missing-row", line)));
  (payload.next_checks || []).forEach(line => host.append(el("div", "meta", "Check next: " + line)));
  (payload.uncertain || []).forEach(line => {
    const row = el("div", "missing-row");
    row.append(el("span", "tag unsupported", "Unsupported"));
    row.append(document.createTextNode(line));
    host.append(row);
  });
}

function advisoryClaim(claim, className, records) {
  const row = el("div", className);
  const unsupported = claim.kind === "unsupported" || !(claim.evidence_ids || []).length;
  row.append(el("span", "tag " + (unsupported ? "unsupported" : "interpretation"), unsupported ? "Unsupported" : "Model reading"));
  row.append(document.createTextNode(claim.statement || ""));
  (claim.evidence_ids || []).forEach(id => {
    const record = (records || []).find(item => item.id === id);
    const button = el("button", "record-link", id);
    button.type = "button";
    const body = el("div", null, record ? (record.text || "No text stored for this id.") : "This id is not in the case brief.");
    body.hidden = true;
    button.onclick = () => { body.hidden = !body.hidden; };
    row.append(el("div", null, ""));
    row.append(button);
    row.append(body);
  });
  return row;
}

function renderList() {
  const list = document.getElementById("list");
  const rows = visible();
  list.replaceChildren();
  const index = rows.findIndex(alert => alert.alert_id === current);
  document.getElementById("pos").textContent = rows.length ? ((index < 0 ? 1 : index + 1) + " of " + rows.length) : "No alerts";
  if (!rows.length) {
    list.append(el("p", "empty", "Nothing matches that search."));
    return;
  }
  const shown = rows.slice(0, listLimit);
  for (const alert of shown) {
    const btn = el("button", "alert-btn" + (alert.alert_id === current ? " active" : ""));
    btn.dataset.id = alert.alert_id;
    const text = el("div");
    text.append(el("strong", null, alert.name));
    const extra = alert.hits.length > 1 ? " · " + alert.hits.length + " hits" : "";
    text.append(el("span", null, alert.alert_id + extra));
    const names = [];
    alert.hits.forEach(hit => hitLists(hit).forEach(name => { if (!names.includes(name)) names.push(name); }));
    if (names.length) text.append(listBadges(names));
    btn.append(text);
    const kind = headline(alert);
    const marks = el("div");
    marks.append(el("span", "mini " + kind, LABELS[kind]));
    const human = latestHuman(alert);
    if (human) marks.append(el("span", "mini human", HUMAN_LABELS[human.action] || human.action));
    btn.append(marks);
    btn.onclick = () => { clearNotice(); show(alert); };
    list.append(btn);
  }
  if (rows.length > shown.length) {
    list.append(el("p", "meta", "Showing " + shown.length + " of " + rows.length));
    const more = el("button", null, "Show more");
    more.type = "button";
    more.onclick = () => { listLimit += 120; renderList(); };
    list.append(more);
  }
}

function clearNotice() {
  const notice = document.getElementById("notice");
  if (!notice) return;
  notice.hidden = true;
  notice.textContent = "";
}

function refreshHumans() {
  return fetch("/api/analyst").then(response => response.json()).then(data => {
    humanLatest = {};
    (data.events || []).forEach(event => { humanLatest[event.hit_id] = event; });
    const unread = data.unreadable_lines || [];
    const notice = document.getElementById("notice");
    if (unread.length && notice && notice.hidden) {
      notice.hidden = false;
      notice.textContent = unread.length + " unreadable log lines preserved at line " + unread.join(", ") + ".";
    }
  }).catch(() => {});
}

function latestHuman(alert) {
  let found = null;
  (alert.hits || []).forEach(hit => {
    const event = humanLatest[hit.hit_id];
    if (event && (!found || event.at > found.at)) found = event;
  });
  return found;
}

function renderPicture(host, data) {
  host.replaceChildren();
  host.append(el("h2", null, "Customer picture"));
  if (!data || data.error) {
    host.append(el("p", null, data && data.error ? data.error : "No picture."));
    return;
  }
  host.append(el("p", null, data.link || ""));
  (data.people || []).forEach(person => {
    const block = el("div");
    block.append(el("div", null, "Person " + person.seed_id + " (" + (person.record_ids || []).join(", ") + ")"));
    (person.why || []).forEach(line => block.append(el("div", "meta", line)));
    (person.records || []).forEach(record => {
      block.append(el("div", null, record.record_id + " · " + record.name + (record.dob ? " · born " + record.dob : "") + (record.country ? " · " + record.country : "")));
    });
    (person.checks || []).forEach(check => {
      block.append(el("div", null, check.check_id + " · " + check.result + (check.checked_at ? " · " + check.checked_at : "") + (check.verified_name ? " · " + check.verified_name : "")));
      block.append(el("div", "meta", check.note || ""));
    });
    host.append(block);
  });
}

function renderSteps(host, payload) {
  host.replaceChildren();
  (payload.steps || []).forEach((step, index) => {
    const box = el("div", "step " + (step.status || "unknown"));
    box.style.animationDelay = (index * 0.12) + "s";
    const title = el("div");
    title.append(el("b", null, step.title || "Step"));
    title.append(document.createTextNode(" · " + (step.status || "")));
    box.append(title);
    if (step.customer || step.hit) {
      const vs = el("div", "vs");
      const left = el("div");
      left.append(el("div", "meta", "Customer"));
      left.append(el("div", null, step.customer || ""));
      const right = el("div");
      right.append(el("div", "meta", "Hit"));
      right.append(el("div", null, step.hit || ""));
      vs.append(left, right);
      box.append(vs);
    }
    if (step.detail) box.append(el("div", null, step.detail));
    host.append(box);
  });
  if (payload.matches_saved === true) {
    host.append(el("div", "tick", "Matches the saved output."));
  } else if (payload.matches_saved === false) {
    host.append(el("div", "cross", "Does not match the saved output."));
  }
}

function runHit(hitId, host) {
  host.replaceChildren(el("div", "meta", "Running the same rules on this hit…"));
  fetch("/api/trace?hit_id=" + encodeURIComponent(hitId))
    .then(response => response.json())
    .then(payload => renderSteps(host, payload))
    .catch(() => host.replaceChildren(el("div", "cross", "The live run failed.")));
}

function setMode(mode) {
  if (!suppressHistory && mode !== "alerts") remember("view=" + mode);
  document.getElementById("layout").classList.toggle("single", mode !== "alerts");
  document.getElementById("browser").style.display = mode === "alerts" ? "" : "none";
  ["alerts", "answers", "try", "screen", "dashboard"].forEach(name => {
    document.getElementById("mode-" + name).classList.toggle("active", name === mode);
  });
  if (mode === "alerts") {
    renderList();
    const alert = alerts.find(item => item.alert_id === current);
    if (alert) show(alert);
    return;
  }
  const root = document.getElementById("detail");
  root.replaceChildren(el("p", "empty", "Loading…"));
  if (mode === "answers") loadAnswers(false);
  if (mode === "try") showTry();
  if (mode === "screen") showScreen();
  if (mode === "dashboard") loadDashboard();
}

function loadDashboard() {
  const root = document.getElementById("detail");
  root.replaceChildren(el("p", "empty", "Loading the dashboard…"));
  fetch("/api/dashboard").then(response => {
    if (!response.ok) throw new Error("The dashboard request failed (" + response.status + ").");
    return response.json();
  }).then(report => {
    root.replaceChildren();
    root.append(el("h2", null, "Performance"));
    root.append(el("p", "meta", "Figures are computed from the saved decisions and the practice file. The 769 unlabelled hits are not an accuracy score."));
    const hits = el("div", "dash");
    [["processed", report.hits.processed], ["cleared", report.hits.cleared], ["confirmed", report.hits.confirmed], ["review", report.hits.review]].forEach(pair => {
      const card = el("div", "count");
      card.append(el("b", null, String(pair[1])));
      card.append(document.createTextNode(pair[0] + " hits"));
      hits.append(card);
    });
    root.append(el("h3", null, "Hits"));
    root.append(hits);
    const alertsBox = el("div", "dash");
    [["processed", report.alerts.processed], ["cleared", report.alerts.cleared], ["escalated", report.alerts.escalated], ["need an analyst", report.alerts.needs_analyst]].forEach(pair => {
      const card = el("div", "count");
      card.append(el("b", null, String(pair[1])));
      card.append(document.createTextNode(pair[0] + " alerts"));
      alertsBox.append(card);
    });
    root.append(el("h3", null, "Alerts"));
    root.append(alertsBox);
    const practice = report.practice || {};
    root.append(el("h3", null, "Labelled practice hits only"));
    root.append(el("p", null, practice.right + " / " + practice.labelled + " decisions. Score " + practice.score + ". Unsafe clearances: " + practice.unsafe_clearances + "."));
    root.append(el("p", "meta", "Unlabelled development hits: " + report.unlabelled_hits + ". No accuracy is claimed for them."));
    const timing = report.timing || {};
    root.append(el("h3", null, "Processing time"));
    if (!timing.measured) root.append(el("p", "missing-row", "This process has not measured a run."));
    else root.append(el("p", null, timing.elapsed_ms + " ms for " + report.hits.processed + " hits (" + timing.ms_per_hit + " ms per hit). Measured " + timing.measured_at + "."));
    root.append(el("p", "meta", timing.what || ""));
    root.append(el("h3", null, "Hits by primary list only"));
    const wrap = el("div", "table-scroll");
    const table = document.createElement("table");
    table.className = "score";
    const header = document.createElement("tr");
    ["List type", "Hits"].forEach(label => header.append(el("th", null, label)));
    table.append(header);
    (report.list_types || []).forEach(row => {
      const tr = document.createElement("tr");
      tr.append(el("td", null, row.list_type));
      tr.append(el("td", null, String(row.hits)));
      table.append(tr);
    });
    wrap.append(table);
    root.append(wrap);
    root.append(el("h3", null, "Reason codes"));
    ["clearing", "confirming", "review"].forEach(name => {
      const line = ((report.reason_codes || {})[name] || []).map(item => item.reason_code + " " + item.hits).join(" · ");
      root.append(el("p", null, name + ": " + (line || "none")));
    });
    const baseline = report.name_only_baseline || {};
    root.append(el("h3", null, "Name-only baseline, same 50 labels"));
    root.append(el("p", null, baseline.right + " / " + baseline.labelled + ". Score " + baseline.score + ". Unsafe clearances: " + baseline.unsafe_clearances + "."));
    root.append(el("p", "meta", baseline.rule || ""));
  }).catch(error => {
    root.replaceChildren(el("h2", null, "Performance"));
    root.append(el("p", "cross", error.message || "The dashboard could not be loaded."));
  });
}

function loadAnswers(live) {
  const root = document.getElementById("detail");
  const url = live ? "/api/practice/run" : "/api/practice";
  const options = live ? {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"} : {};
  fetch(url, options).then(response => response.json()).then(report => {
    root.replaceChildren();
    const head = el("div", "screened scorehead");
    head.append(el("h2", null, report.right + " / " + report.total + " practice decisions"));
    head.append(el("p", null, "Score " + report.score + ". Unsafe clearances: " + report.unsafe + ". Reason code agrees on " + report.code_ok + " of the right decisions." + (report.live ? " Recomputed live." : " Compared with the saved file.")));
    const button = el("button", "run", report.live ? "Run all again" : "Run all");
    button.type = "button";
    button.onclick = () => loadAnswers(true);
    head.append(button);
    root.append(head);
    if (!report.total) {
      root.append(el("p", "empty", "This data folder has no practice_labels.jsonl."));
      return;
    }
    const table = document.createElement("table");
    table.className = "score";
    const header = document.createElement("tr");
    ["Hit", "Expected", "Ours", "Reason code", ""].forEach(label => header.append(el("th", null, label)));
    table.append(header);
    report.rows.forEach(row => {
      const tr = document.createElement("tr");
      const link = el("button", null, row.hit_id);
      link.type = "button";
      link.onclick = () => { setMode("alerts"); const alert = alerts.find(item => item.alert_id === row.alert_id); if (alert) show(alert); };
      const cell = document.createElement("td");
      cell.append(link);
      tr.append(cell);
      tr.append(el("td", null, row.expected || ""));
      tr.append(el("td", null, row.got || "missing"));
      tr.append(el("td", null, (row.got_code || "") + (row.code_ok ? "" : " (differs)")));
      tr.append(el("td", row.match ? "tick" : "cross", row.match ? "Match" : (row.unsafe ? "Unsafe clear" : "Miss")));
      table.append(tr);
    });
    const wrap = el("div", "table-scroll");
    wrap.append(table);
    root.append(wrap);
  }).catch(() => root.replaceChildren(el("p", "cross", "Could not score the practice labels.")));
}

function field(form, label, name, value) {
  form.append(el("label", null, label));
  const input = document.createElement("input");
  input.name = name;
  input.value = value || "";
  form.append(input);
}

function fillScreen(example) {
  const form = document.getElementById("screen-form");
  if (!form || !example) return;
  ["customer_name", "aliases", "customer_type", "customer_dob", "customer_countries", "email", "phone"].forEach(name => {
    if (form.elements[name]) form.elements[name].value = example[name] || (name === "customer_type" ? "Individual" : "");
  });
}

function showScreenResult(host, payload) {
  host.replaceChildren();
  const banner = el("div", "step " + (payload.verdict === "PASS" ? "pass" : payload.verdict === "ESCALATE" ? "contradiction" : "unknown"));
  banner.style.animationDelay = "0s";
  banner.append(el("b", null, payload.verdict === "PASS" ? "Pass" : payload.verdict === "ESCALATE" ? "Escalate to compliance" : "Needs an analyst"));
  banner.append(el("div", null, payload.summary || ""));
  banner.append(el("div", "meta", "Checked " + payload.checked_hits + " hits (" + payload.checked_profiles + " profiles). " + payload.candidate_count + " shared a name."));
  host.append(banner);
  (payload.candidates || []).forEach((candidate, index) => {
    const card = el("article", "hit " + candidate.decision);
    card.append(el("strong", null, candidate.name + " · " + candidate.hit_id));
    appendListFacts(card, candidate);
    card.append(el("div", "meta", candidate.why + " · " + candidate.reason_code));
    card.append(el("p", "reason", candidate.reason));
    if ((candidate.sources || []).length) {
      card.append(el("div", "evidence-label", "External sources, not the customer's record"));
      candidate.sources.forEach(source => {
        card.append(el("div", null, source.origin + ": " + source.fact));
      });
    }
    const steps = el("div", "trace");
    renderSteps(steps, {steps: candidate.steps || []});
    card.append(steps);
    host.append(card);
    card.style.animationDelay = Math.min(index * 0.08, 0.6) + "s";
  });
}

function showScreen() {
  const root = document.getElementById("detail");
  root.replaceChildren();
  root.append(el("h2", null, "Screen a new customer"));
  root.append(el("p", "meta", "The watchlist search uses the same name rules as a real alert. Dates and countries on a match come from the profile, its notes, or web search results."));
  const examples = el("div", "demos");
  (window.screenExamples || []).forEach(example => {
    const button = el("button", null, example.label + " · " + example.verdict);
    button.type = "button";
    button.onclick = () => { fillScreen(example); document.getElementById("screen-form").requestSubmit(); };
    examples.append(button);
  });
  root.append(examples);
  const form = el("form", "try");
  form.id = "screen-form";
  const first = (window.screenExamples || [])[0] || {};
  field(form, "Full name", "customer_name", first.customer_name || "");
  field(form, "Aliases, optional", "aliases", first.aliases || "");
  form.append(el("label", null, "Person or company"));
  const kind = document.createElement("select");
  kind.name = "customer_type";
  ["Individual", "Company"].forEach(option => {
    const node = el("option", null, option);
    node.value = option;
    if ((first.customer_type || "Individual") === option) node.selected = true;
    kind.append(node);
  });
  form.append(kind);
  field(form, "Date of birth or incorporation date", "customer_dob", first.customer_dob || "");
  field(form, "Countries, nationality, or document country", "customer_countries", first.customer_countries || "");
  field(form, "Email, optional", "email", "");
  field(form, "Phone, optional", "phone", "");
  const button = el("button", "run", "Screen");
  button.type = "submit";
  form.append(button);
  const host = el("div", "trace");
  form.onsubmit = event => {
    event.preventDefault();
    const body = {};
    new FormData(form).forEach((value, key) => { body[key] = value; });
    const screenName = String(body.customer_name || "").trim();
    const screenToken = screenName.toLowerCase();
    if (!screenName || ["na", "n/a", "test", "unknown", "xxx", "none", "null", "nil"].indexOf(screenToken) >= 0 || /^[0-9]+$/.test(screenToken)) {
      host.replaceChildren(el("div", "cross", "A real customer name is required. Placeholders such as NA, TEST and UNKNOWN are not a name."));
      return;
    }
    host.replaceChildren(el("div", "meta", "Searching every watchlist profile…"));
    fetch("/api/screen", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(response => response.json().then(payload => ({ok: response.ok, payload})))
      .then(result => {
        if (!result.ok) {
          host.replaceChildren(el("div", "cross", (result.payload && result.payload.error) || "Screening was rejected."));
          return;
        }
        showScreenResult(host, result.payload);
      })
      .catch(() => host.replaceChildren(el("div", "cross", "Screening failed. The server did not answer.")));
  };
  root.append(form, host);
}

function showTry() {
  const root = document.getElementById("detail");
  root.replaceChildren();
  root.append(el("h2", null, "Try your own"));
  root.append(el("p", "meta", "Same rules as a real hit. Nothing here is written into the saved file."));
  const form = el("form", "try");
  const pair = el("div", "pair");
  const customer = el("div");
  customer.append(el("h3", null, "Customer"));
  field(customer, "Name", "customer_name", "John Doe");
  customer.append(el("label", null, "Type"));
  const customerType = document.createElement("select");
  customerType.name = "customer_type";
  ["Individual", "Company"].forEach(option => {
    const node = el("option", null, option);
    node.value = option;
    customerType.append(node);
  });
  customer.append(customerType);
  field(customer, "Date of birth or year", "customer_dob", "1990-05-12");
  field(customer, "Countries or nationalities", "customer_countries", "British");
  const listed = el("div");
  listed.append(el("h3", null, "Listed profile"));
  field(listed, "Name", "hit_name", "Jon Doe");
  field(listed, "Aliases, separated by commas", "hit_aliases", "");
  listed.append(el("label", null, "Type"));
  const hitType = document.createElement("select");
  hitType.name = "hit_type";
  ["Individual", "Company"].forEach(option => {
    const node = el("option", null, option);
    node.value = option;
    hitType.append(node);
  });
  listed.append(hitType);
  field(listed, "Dates of birth or years", "hit_dobs", "1969-05-12");
  field(listed, "Countries", "hit_countries", "Belarus");
  field(listed, "List type (optional)", "list_type", "Sanctions");
  pair.append(customer, listed);
  form.append(pair);
  const button = el("button", "run", "Run decision");
  button.type = "submit";
  form.append(button);
  const host = el("div", "trace");
  form.onsubmit = event => {
    event.preventDefault();
    const body = {};
    new FormData(form).forEach((value, key) => { body[key] = value; });
    host.replaceChildren(el("div", "meta", "Running…"));
    fetch("/api/try", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(response => response.json())
      .then(payload => renderSteps(host, payload))
      .catch(() => host.replaceChildren(el("div", "cross", "The decision could not be run. The server did not answer.")));
  };
  root.append(form, host);
}

fetch("/api/bundle").then(r => r.json()).then(data => {
  alerts = data.alerts;
  window.screenExamples = data.screen_examples || [];
  const counts = document.getElementById("counts");
  function chip(key, className, number, label) {
    const button = el("button", "count" + (className ? " " + className : "") + (filter === key ? " active" : ""));
    button.type = "button";
    button.dataset.filter = key;
    button.append(el("b", null, String(number)));
    button.append(document.createTextNode(label));
    button.onclick = () => {
      listLimit = 120;
      filter = filter === key ? "ALL" : key;
      counts.querySelectorAll(".count").forEach(node => node.classList.remove("active"));
      if (filter !== "ALL") button.classList.add("active");
      const rows = visible();
      if (rows.length) show(rows[0]);
      renderList();
    };
    counts.append(button);
  }
  chip("ALL", "", data.hits, "hits");
  chip("DIFFERENT_ENTITY", "cleared", data.counts.DIFFERENT_ENTITY, "cleared");
  chip("SAME_ENTITY", "same", data.counts.SAME_ENTITY, "same person");
  chip("REVIEW", "review", data.counts.REVIEW, "review");
  const workload = document.getElementById("workload");
  const tally = {CLEARED: 0, ESCALATED: 0, ANALYST: 0};
  const codes = {};
  alerts.forEach(alert => { tally[alertStatus(alert)] += 1; });
  const listCounts = {};
  alerts.forEach(alert => alert.hits.forEach(hit => {
    codes[hit.reason_code] = (codes[hit.reason_code] || 0) + 1;
    hitLists(hit).forEach(name => { listCounts[name] = (listCounts[name] || 0) + 1; });
  }));
  function stat(number, label, key) {
    const node = el("button", "count" + (alertFilter === key ? " active" : ""));
    node.type = "button";
    node.append(el("strong", null, String(number)));
    node.append(document.createTextNode(label));
    node.onclick = () => {
      listLimit = 120;
      alertFilter = alertFilter === key ? "ALL" : key;
      workload.querySelectorAll(".count").forEach(item => item.classList.remove("active"));
      if (alertFilter !== "ALL") node.classList.add("active");
      const rows = visible();
      if (rows.length) show(rows[0]);
      renderList();
    };
    workload.append(node);
  }
  stat(tally.CLEARED, "alerts cleared", "CLEARED");
  stat(tally.ESCALATED, "escalated to compliance", "ESCALATED");
  stat(tally.ANALYST, "need an analyst", "ANALYST");
  const saved = el("div", "hint");
  const minutes = tally.CLEARED * 8;
  const hours = Math.round(minutes / 6) / 10;
  const codeLine = Object.keys(codes).sort().map(code => code + " " + codes[code]).join(" · ");
  saved.append(el("div", null, "Estimate: " + minutes + " minutes saved (" + hours + " hours), assuming 8 minutes of analyst time for each alert that can be closed without a person."));
  saved.append(el("div", null, codeLine));
  const breakdown = el("div", "lists");
  sortLists(Object.keys(listCounts)).forEach(name => {
    breakdown.append(el("span", "list-badge " + listClass(name), name + " " + listCounts[name]));
  });
  saved.append(el("div", null, "Hits by list type. A hit on more than one list is counted in each."));
  saved.append(breakdown);
  saved.append(el("div", null, "List type is shown for context only. Per the brief, decisions are about identity, not severity, so every list is treated the same."));
  workload.append(saved);
  paintListFilters = () => {
    const host = document.getElementById("list-filters");
    if (!host) return;
    host.replaceChildren();
    const all = el("button", "list-chip" + (listFilter === "ALL" ? " active" : ""), "All lists");
    all.type = "button";
    all.onclick = () => {
      listFilter = "ALL";
      paintListFilters();
      const rows = visible();
      if (rows.length) show(rows[0]);
      renderList();
    };
    host.append(all);
    sortLists(Object.keys(listCounts)).forEach(name => {
      const button = el("button", "list-chip " + listClass(name) + (listFilter === name ? " active" : ""), name + " " + listCounts[name]);
      button.type = "button";
      button.onclick = () => {
        listFilter = listFilter === name ? "ALL" : name;
        paintListFilters();
        const rows = visible();
        if (rows.length) show(rows[0]);
        renderList();
      };
      host.append(button);
    });
  };
  paintListFilters();
  document.getElementById("q").addEventListener("input", () => { listLimit = 120; renderList(); });
  document.getElementById("prev").onclick = () => step(-1);
  document.getElementById("next").onclick = () => step(1);
  document.getElementById("mode-dashboard").onclick = () => setMode("dashboard");
  document.getElementById("mode-screen").onclick = () => setMode("screen");
  document.getElementById("mode-alerts").onclick = () => setMode("alerts");
  document.getElementById("mode-answers").onclick = () => setMode("answers");
  document.getElementById("mode-try").onclick = () => setMode("try");
  (data.demos || []).forEach(demo => {
    const button = el("button", null, demo.label);
    button.type = "button";
    button.title = demo.blurb || "";
    button.onclick = () => {
      setMode("alerts");
      const alert = alerts.find(item => item.alert_id === demo.alert_id);
      if (alert) show(alert);
    };
    document.getElementById("demos").append(button);
  });
  document.addEventListener("keydown", event => {
    if (event.target.tagName === "INPUT" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") { event.preventDefault(); step(1); }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
  });
  document.getElementById("home").addEventListener("click", event => {
    event.preventDefault();
    goHome(false);
  });
  window.addEventListener("popstate", () => applyLocation());
  refreshHumans().then(() => applyLocation());
}).catch(() => {
  document.getElementById("detail").replaceChildren(el("p", "cross", "The development set could not be loaded. The server did not answer."));
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    bundle = {}
    world = None
    saved = {}
    labels = []
    analyst_path = ""
    measured_ms = None
    measured_at = None
    csrf_token = ""
    port = PORT

    def _read_json(self):
        header = self.headers.get("Content-Length")
        if header is None or header == "":
            length = 0
        else:
            try:
                length = int(header)
            except ValueError:
                return 400, {"error": "Content-Length was not a number."}, None
        if length < 0:
            return 400, {"error": "Content-Length was not a number."}, None
        if length > MAX_BODY:
            self.close_connection = True
            return 413, {"error": "Request body is larger than 64KB."}, None
        raw = self.rfile.read(length) if length else b"{}"
        return parse_json_object(raw)

    def _forbid(self, path):
        message = state_change_error(
            path,
            self.headers.get("Content-Type"),
            self.headers.get("Origin"),
            self.headers.get("Referer"),
            self.headers.get("X-Doppel-Token"),
            self.csrf_token,
            self.port,
        )
        if message:
            self._json({"error": message}, 403)
            return True
        return False

    def _dashboard(self):
        label_ids = {row["hit_id"] for row in self.labels}
        baseline = {}
        for row in self.saved.values():
            if row["hit_id"] not in label_ids:
                continue
            hit = self.world.hits.get(row["hit_id"]) or {}
            alert = self.world.alerts.get(row["alert_id"]) or {}
            screened = (alert.get("customer") or {}).get("name")
            baseline[row["hit_id"]] = name_only_decision(self.world.names, screened, hit)
        return summarise(
            list(self.saved.values()),
            self.world.hits,
            self.labels,
            baseline,
            self.measured_ms,
            self.measured_at,
        )

    def log_message(self, fmt, *args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trace(self, explained):
        saved = self.saved.get(explained.get("hit_id") or "")
        if saved:
            explained["saved"] = {
                "decision": saved.get("decision"),
                "reason_code": saved.get("reason_code"),
                "customer_record_id": saved.get("customer_record_id"),
            }
            explained["matches_saved"] = (
                saved.get("decision") == explained.get("decision")
                and saved.get("reason_code") == explained.get("reason_code")
                and (saved.get("customer_record_id") or None) == (explained.get("customer_record_id") or None)
            )
        else:
            explained["saved"] = None
            explained["matches_saved"] = None
        return explained

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/bundle":
            self._json(self.bundle)
            return
        if parsed.path == "/api/trace":
            hit_id = (parse_qs(parsed.query).get("hit_id") or [""])[0]
            explained = self.world.explain_hit(hit_id)
            if explained.get("error"):
                self._json(explained, 404)
                return
            self._json(self._trace(explained))
            return
        if parsed.path == "/api/practice":
            self._json(practice_report(self.labels, self.saved, live=False))
            return
        if parsed.path == "/api/picture":
            alert_id = (parse_qs(parsed.query).get("alert_id") or [""])[0]
            picture = self.world.customer_picture(alert_id)
            self._json(picture, 404 if picture.get("error") else 200)
            return
        if parsed.path == "/api/brief":
            hit_id = (parse_qs(parsed.query).get("hit_id") or [""])[0]
            brief = build_brief(self.world, hit_id)
            self._json(brief, 404 if brief.get("error") else 200)
            return
        if parsed.path == "/api/dashboard":
            self._json(self._dashboard())
            return
        if parsed.path == "/api/analyst":
            hit_id = (parse_qs(parsed.query).get("hit_id") or [""])[0]
            if hit_id and hit_id not in self.world.hits:
                self._json({"error": "Unknown hit."}, 404)
                return
            self._json({
                "events": analyst_history(self.analyst_path, hit_id),
                "unreadable_lines": unreadable_lines(self.analyst_path),
            })
            return
        if parsed.path in ("/", "/index.html"):
            body = PAGE.replace("__DOPPEL_TOKEN__", self.csrf_token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        status, error, payload = self._read_json()
        if error:
            self._json(error, status)
            return
        if self._forbid(parsed.path):
            return
        if parsed.path == "/api/try":
            self._json(self._trace(self.world.explain_entered(payload)))
            return
        if parsed.path == "/api/screen":
            message = screen_name_error(payload.get("customer_name") or payload.get("name"))
            if message:
                self._json({"error": message}, 400)
                return
            result = self.world.screen_customer(payload)
            for candidate in result.get("candidates") or []:
                hit = self.world.hits.get(candidate.get("hit_id")) or {}
                candidate["categories"] = as_list(hit.get("categories"))
                candidate["primary_category"] = as_text(hit.get("primary_category"))
                candidate["list_sources"] = as_list(hit.get("sources"))
                candidate["position_or_allegation"] = as_text(hit.get("position_or_allegation"))
            self._json(result)
            return
        if parsed.path == "/api/analyst":
            hit_id = str(payload.get("hit_id") or "")
            brief = build_brief(self.world, hit_id) if hit_id in self.world.hits else {"error": "Unknown hit."}
            allowed = [record.get("id") for record in brief.get("records") or []]
            status, body = analyst_record(
                self.analyst_path,
                hit_id,
                payload.get("action"),
                payload.get("rationale") if isinstance(payload.get("rationale"), str) else "",
                payload.get("analyst") if isinstance(payload.get("analyst"), str) else "",
                notes=payload.get("notes") if isinstance(payload.get("notes"), str) else "",
                idempotency_key=payload.get("idempotency_key"),
                cited_ids=payload.get("cited_ids") or [],
                allowed_ids=allowed,
                hit_known=hit_id in self.world.hits,
            )
            self._json(body, status)
            return
        if parsed.path == "/api/investigate":
            hit_id = str(payload.get("hit_id") or "")
            self._json(investigate(self.world, hit_id))
            return
        if parsed.path == "/api/practice/run":
            live = {}
            for label in self.labels:
                hit = self.world.hits.get(label["hit_id"])
                alert = self.world.alerts.get(label.get("alert_id") or (hit or {}).get("alert_id"))
                if hit is None or alert is None:
                    continue
                outcome = self.world.decide_hit(alert, hit)
                live[label["hit_id"]] = {
                    "hit_id": label["hit_id"],
                    "alert_id": hit["alert_id"],
                    "decision": outcome.decision,
                    "reason_code": outcome.reason_code,
                    "customer_record_id": outcome.customer_record_id,
                }
            self._json(practice_report(self.labels, live, live=True))
            return
        self.send_error(404)


def main():
    parsed = serve_args(sys.argv)
    if parsed is None:
        print("usage: python3 system/serve.py [--host HOST] <data folder> <output file>", file=sys.stderr)
        sys.exit(2)
    host, folder, output = parsed
    host, warning = bind_host(host)
    if warning:
        print(warning, file=sys.stderr)
    Handler.bundle = load_bundle(folder, output)
    Handler.saved = {row["hit_id"]: row for row in read_jsonl(output)}
    practice_path = os.path.join(folder, "practice_labels.jsonl")
    Handler.labels = read_jsonl(practice_path) if os.path.exists(practice_path) else []
    print(f"Loading the decision engine for live traces ({Handler.bundle['hits']} hits)...")
    Handler.world = World(folder)
    Handler.bundle["screen_examples"] = Handler.world.suggest_screen_examples()
    Handler.analyst_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "analyst_log.jsonl")
    Handler.csrf_token = secrets.token_urlsafe(32)
    Handler.port = PORT
    started = time.perf_counter()
    Handler.world.decide_all()
    Handler.measured_ms = (time.perf_counter() - started) * 1000.0
    Handler.measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"Demo at http://127.0.0.1:{PORT}  ({Handler.bundle['hits']} hits)")
    server.serve_forever()


if __name__ == "__main__":
    main()
