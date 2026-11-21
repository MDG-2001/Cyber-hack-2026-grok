#!/usr/bin/env python3
"""Local demo of the decisions. No network, no extra packages.

    python3 system/serve.py <data folder> <output file>

Then open the printed address. The page shows each alert, the decision on
every hit, and the records the decision cites.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8766


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_bundle(folder: str, output: str) -> dict:
    alerts = {row["alert_id"]: row for row in read_jsonl(os.path.join(folder, "alerts.jsonl"))}
    hits = {row["hit_id"]: row for row in read_jsonl(os.path.join(folder, "hits.jsonl"))}
    predictions = read_jsonl(output)

    customers = {}
    for row in read_jsonl(os.path.join(folder, "customers.jsonl")):
        customers[row["record_id"]] = {
            "kind": "Customer record",
            "title": row.get("full_name") or row["record_id"],
            "lines": [
                row.get("entity_type") or "",
                f"Born {row.get('dob')}" if row.get("dob") else "",
                ", ".join(
                    part
                    for part in (
                        row.get("nationality"),
                        row.get("residence_country"),
                        row.get("city"),
                    )
                    if part
                ),
            ],
        }
    checks = {}
    for row in read_jsonl(os.path.join(folder, "kyc_checks.jsonl")):
        checks[row["check_id"]] = {
            "kind": "Identity check",
            "title": row.get("verified_name") or row["check_id"],
            "lines": [
                f"{row.get('result')} {row.get('checked_at') or ''}".strip(),
                f"Born {row.get('verified_dob')}" if row.get("verified_dob") else "",
                f"Document country {row.get('document_country')}" if row.get("document_country") else "",
            ],
        }
    notes = {}
    for row in read_jsonl(os.path.join(folder, "notes.jsonl")):
        notes[row["note_id"]] = {
            "kind": f"Note · {row.get('channel') or 'note'}",
            "title": "",
            "lines": [row.get("text") or ""],
        }
    searches = {}
    for row in read_jsonl(os.path.join(folder, "search_results.jsonl")):
        searches[row["result_id"]] = {
            "kind": "Search result",
            "title": row.get("title") or row["result_id"],
            "lines": [row.get("snippet") or ""],
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
                "name": hit.get("name") or row["hit_id"],
                "entity_type": hit.get("entity_type") or "",
                "categories": hit.get("categories") or [],
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
                "screened_at": alert.get("screened_at"),
                "name": screened.get("name") or alert_id,
                "entity_type": screened.get("entity_type") or "",
                "dob": screened.get("dob"),
                "country": screened.get("country"),
                "city": screened.get("city"),
                "link": alert.get("account_ref") or alert.get("login_handle") or alert.get("email"),
                "hits": alert_hits,
            }
        )
    alerts_out.sort(key=lambda item: item["alert_id"])
    counts = {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 0, "REVIEW": 0}
    for row in predictions:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    return {"alerts": alerts_out, "counts": counts, "hits": len(predictions)}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Same person?</title>
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
    padding: 22px 28px 16px;
    border-bottom: 1px solid var(--line);
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    align-items: end;
  }
  h1 { font-size: 28px; font-weight: 600; margin: 0; letter-spacing: -0.03em; }
  header p { margin: 4px 0 0; color: var(--muted); max-width: 46rem; }
  .counts { display: flex; gap: 8px; flex-wrap: wrap; }
  .count {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 6px 12px;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 13px;
  }
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
  #list { margin-top: 10px; max-height: calc(100vh - 170px); overflow: auto; }
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
  }
  .alert-btn strong { display: block; font-size: 16px; }
  .alert-btn span { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }
  .alert-btn.active, .alert-btn:hover { background: #fff; }
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
  .evidence { display: grid; gap: 8px; }
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
  @media (max-width: 800px) {
    main { grid-template-columns: 1fr; }
    aside { border-right: 0; border-bottom: 1px solid var(--line); }
    #list { max-height: 240px; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>Same person?</h1>
    <p>Each screening hit is cleared, confirmed, or left for an analyst. The note under a hit is the reason, and the cards are the records that decided it.</p>
  </div>
  <div class="counts" id="counts"></div>
</header>
<main>
  <aside>
    <input id="q" placeholder="Search a name, alert or hit" autocomplete="off">
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

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function show(alert) {
  current = alert.alert_id;
  document.querySelectorAll(".alert-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.id === alert.alert_id);
  });
  const root = document.getElementById("detail");
  root.replaceChildren();
  const screened = el("div", "screened");
  screened.append(el("div", "meta", alert.alert_id + (alert.screened_at ? " · screened " + alert.screened_at : "")));
  screened.append(el("h2", null, alert.name));
  const bits = [alert.entity_type, alert.dob, alert.country, alert.city, alert.link].filter(Boolean);
  screened.append(el("div", "meta", bits.join(" · ") || "No screened details beyond the name"));
  root.append(screened);
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
    card.append(el("p", "reason", hit.reason));
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
  const query = document.getElementById("q").value.trim().toLowerCase();
  const list = document.getElementById("list");
  list.replaceChildren();
  const matches = alerts.filter(alert => {
    if (!query) return true;
    const blob = [alert.alert_id, alert.name, alert.link, ...alert.hits.flatMap(hit => [hit.hit_id, hit.name, hit.decision])].join(" ").toLowerCase();
    return blob.includes(query);
  }).slice(0, 80);
  if (!matches.length) {
    list.append(el("p", "empty", "Nothing matches that search."));
    return;
  }
  for (const alert of matches) {
    const btn = el("button", "alert-btn" + (alert.alert_id === current ? " active" : ""));
    btn.dataset.id = alert.alert_id;
    btn.append(el("strong", null, alert.name));
    const decisions = alert.hits.map(hit => LABELS[hit.decision] || hit.decision).join(", ");
    btn.append(el("span", null, alert.alert_id + " · " + decisions));
    btn.onclick = () => show(alert);
    list.append(btn);
  }
}

fetch("/api/bundle").then(r => r.json()).then(data => {
  alerts = data.alerts;
  const counts = document.getElementById("counts");
  counts.append(el("div", "count", data.hits + " hits"));
  counts.append(el("div", "count", data.counts.DIFFERENT_ENTITY + " cleared"));
  counts.append(el("div", "count", data.counts.SAME_ENTITY + " same person"));
  counts.append(el("div", "count", data.counts.REVIEW + " review"));
  document.getElementById("q").addEventListener("input", renderList);
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

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path.split("?")[0] == "/api/bundle":
            body = json.dumps(self.bundle).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def main():
    if len(sys.argv) != 3:
        print("usage: python3 system/serve.py <data folder> <output file>", file=sys.stderr)
        sys.exit(2)
    Handler.bundle = load_bundle(sys.argv[1], sys.argv[2])
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Demo at http://127.0.0.1:{PORT}  ({Handler.bundle['hits']} hits)")
    server.serve_forever()


if __name__ == "__main__":
    main()
