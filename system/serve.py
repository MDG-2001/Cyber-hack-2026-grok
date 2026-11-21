#!/usr/bin/env python3
"""Local demo of the decisions. No network, no extra packages.

    python3 system/serve.py <data folder> <output file>

Then open the printed address. The page shows each alert, the decision on
every hit, and the records the decision cites.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen.engine import World

PORT = 8766


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
  .brand { display: flex; gap: 14px; align-items: center; }
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
  #list { margin-top: 8px; max-height: calc(100vh - 230px); overflow: auto; }
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
  .reason { margin: 8px 0 12px; }
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
  <div class="brand">
    <div class="mark">D</div>
    <div>
      <h1>Doppelgänger</h1>
      <p>Is this hit the customer, or just the same name?</p>
    </div>
  </div>
  <div class="counts" id="counts"></div>
</header>
<div class="workload" id="workload"></div>
<div class="toolbar">
  <div class="modes">
    <button type="button" id="mode-screen">Screen a new customer</button>
    <button type="button" id="mode-alerts" class="active">Alerts</button>
    <button type="button" id="mode-answers">Test against answers</button>
    <button type="button" id="mode-try">Try your own</button>
  </div>
  <div class="demos" id="demos"><span class="meta">Demo picks</span></div>
</div>
<main id="layout">
  <aside id="browser">
    <input id="q" placeholder="Search a name, alert or hit" autocomplete="off">
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
const LABELS = {
  DIFFERENT_ENTITY: "Cleared",
  SAME_ENTITY: "Same person",
  REVIEW: "Review"
};
let alerts = [];
let current = null;
let filter = "ALL";
let alertFilter = "ALL";

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
    if (!query) return true;
    const blob = [alert.alert_id, alert.name, alert.link, ...alert.hits.flatMap(hit => [hit.hit_id, hit.name, hit.decision, hit.reason_code])].join(" ").toLowerCase();
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

function show(alert) {
  current = alert.alert_id;
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
  title.append(el("span", "pill alert-status " + (status === "CLEARED" ? "DIFFERENT_ENTITY" : status === "ESCALATED" ? "SAME_ENTITY" : "REVIEW"), statusLabel));
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
    who.append(el("div", "meta", [hit.hit_id, hit.entity_type, (hit.categories || []).join(", ")].filter(Boolean).join(" · ")));
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
    const listed = [hit.entity_type, (hit.categories || []).join(", "), (hit.dobs || []).join(", "), (hit.countries || []).join(", "), alias].filter(Boolean).join(" · ");
    right.append(el("div", null, listed || "No further list details"));
    compare.append(left, right);
    card.append(compare);
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
    root.append(card);
  }
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
  for (const alert of rows.slice(0, 120)) {
    const btn = el("button", "alert-btn" + (alert.alert_id === current ? " active" : ""));
    btn.dataset.id = alert.alert_id;
    const text = el("div");
    text.append(el("strong", null, alert.name));
    const extra = alert.hits.length > 1 ? " · " + alert.hits.length + " hits" : "";
    text.append(el("span", null, alert.alert_id + extra));
    btn.append(text);
    const kind = headline(alert);
    btn.append(el("span", "mini " + kind, LABELS[kind]));
    btn.onclick = () => show(alert);
    list.append(btn);
  }
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
  document.getElementById("layout").classList.toggle("single", mode !== "alerts");
  document.getElementById("browser").style.display = mode === "alerts" ? "" : "none";
  ["alerts", "answers", "try", "screen"].forEach(name => {
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
    root.append(table);
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
    host.replaceChildren(el("div", "meta", "Searching every watchlist profile…"));
    fetch("/api/screen", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(response => response.json())
      .then(payload => showScreenResult(host, payload))
      .catch(() => host.replaceChildren(el("div", "cross", "Screening failed.")));
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
      .then(payload => renderSteps(host, payload));
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
    button.append(el("b", null, String(number)));
    button.append(document.createTextNode(label));
    button.onclick = () => {
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
  alerts.forEach(alert => alert.hits.forEach(hit => { codes[hit.reason_code] = (codes[hit.reason_code] || 0) + 1; }));
  function stat(number, label, key) {
    const node = el("button", "count" + (alertFilter === key ? " active" : ""));
    node.type = "button";
    node.append(el("strong", null, String(number)));
    node.append(document.createTextNode(label));
    node.onclick = () => {
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
  workload.append(saved);
  document.getElementById("q").addEventListener("input", renderList);
  document.getElementById("prev").onclick = () => step(-1);
  document.getElementById("next").onclick = () => step(1);
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
  const params = new URLSearchParams(location.search);
  if (params.get("q")) document.getElementById("q").value = params.get("q");
  renderList();
  const preset = params.get("alert");
  const start = (preset && alerts.find(alert => alert.alert_id === preset))
    || alerts.find(alert => alert.hits.some(hit => hit.decision === "SAME_ENTITY"))
    || alerts[0];
  if (start) show(start);
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
        if parsed.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json({"error": "Body was not JSON"}, 400)
            return
        if parsed.path == "/api/try":
            self._json(self._trace(self.world.explain_entered(payload)))
            return
        if parsed.path == "/api/screen":
            self._json(self.world.screen_customer(payload))
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
    if len(sys.argv) != 3:
        print("usage: python3 system/serve.py <data folder> <output file>", file=sys.stderr)
        sys.exit(2)
    folder, output = sys.argv[1], sys.argv[2]
    Handler.bundle = load_bundle(folder, output)
    Handler.saved = {row["hit_id"]: row for row in read_jsonl(output)}
    practice_path = os.path.join(folder, "practice_labels.jsonl")
    Handler.labels = read_jsonl(practice_path) if os.path.exists(practice_path) else []
    print(f"Loading the decision engine for live traces ({Handler.bundle['hits']} hits)...")
    Handler.world = World(folder)
    Handler.bundle["screen_examples"] = Handler.world.suggest_screen_examples()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Demo at http://127.0.0.1:{PORT}  ({Handler.bundle['hits']} hits)")
    server.serve_forever()


if __name__ == "__main__":
    main()
