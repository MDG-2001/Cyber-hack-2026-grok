"""Numbers for the performance dashboard. Nothing here is typed in by hand.

The name-only baseline is not the screening engine. It predicts SAME_ENTITY
when the screened name agrees with the hit name or an alias under the same
name rules as the engine, and REVIEW otherwise. It never predicts
DIFFERENT_ENTITY, so it cannot make an unsafe clearance. Dates, countries,
entity type and list type are ignored.
"""

from __future__ import annotations

from collections import Counter

# The same cost table as validate.py. A wrong clearance of a true match or a
# true review costs 60. Predicting REVIEW for everything scores 0.
COST = {
    "DIFFERENT_ENTITY": {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 3, "REVIEW": 3},
    "SAME_ENTITY": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 0, "REVIEW": 2},
    "REVIEW": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 2, "REVIEW": 0},
}

LIST_ORDER = (
    "Sanctions",
    "PEP",
    "Adverse Media",
    "Enforcement",
    "Registration",
    "SOE",
    "Other Segment",
)


def list_type(hit: dict) -> str:
    primary = (hit.get("primary_category") or "").strip()
    if primary:
        return primary
    categories = hit.get("categories") or []
    for item in categories:
        text = str(item or "").strip()
        if text:
            return text
    return "Unspecified"


def alert_outcome(decisions) -> str:
    if any(item == "SAME_ENTITY" for item in decisions):
        return "ESCALATED"
    if decisions and all(item == "DIFFERENT_ENTITY" for item in decisions):
        return "CLEARED"
    return "NEEDS_ANALYST"


def practice_metrics(labels, answers) -> dict:
    """answers maps hit_id to a decision string. Missing answers count as REVIEW."""
    right = 0
    unsafe = 0
    cost = 0
    baseline_cost = 0
    for row in labels:
        expected = row["decision"]
        got = answers.get(row["hit_id"], "REVIEW")
        if got == expected:
            right += 1
        if expected != "DIFFERENT_ENTITY" and got == "DIFFERENT_ENTITY":
            unsafe += 1
        cost += COST[expected][got]
        baseline_cost += COST[expected]["REVIEW"]
    if baseline_cost:
        score = 100.0 * max(0.0, 1.0 - (cost / float(baseline_cost)))
    else:
        score = 0.0
    return {
        "labelled": len(labels),
        "right": right,
        "unsafe_clearances": unsafe,
        "score": round(score, 1),
        "scope": "labelled practice hits only",
    }


def name_only_decision(name_index, screened_name, hit) -> str:
    company = str(hit.get("entity_type") or "").strip().lower() in {
        "company", "organisation", "organization", "business", "entity",
    }
    left = name_index.tokens(screened_name, company=company)
    candidates = [hit.get("name")]
    candidates.extend(hit.get("aliases") or [])
    for value in candidates:
        if name_index.agree(left, name_index.tokens(value, company=company)):
            return "SAME_ENTITY"
    return "REVIEW"


def summarise(predictions, hits_by_id, labels, baseline_answers, elapsed_ms, measured_at) -> dict:
    by_alert = {}
    hit_counts = Counter()
    codes = {"DIFFERENT_ENTITY": Counter(), "SAME_ENTITY": Counter(), "REVIEW": Counter()}
    lists = Counter()
    for row in predictions:
        hit_counts[row["decision"]] += 1
        by_alert.setdefault(row["alert_id"], []).append(row["decision"])
        codes[row["decision"]][row["reason_code"]] += 1
        hit = hits_by_id.get(row["hit_id"]) or {}
        lists[list_type(hit)] += 1
    alert_counts = Counter(alert_outcome(items) for items in by_alert.values())
    labelled_ids = {row["hit_id"] for row in labels}
    engine_answers = {row["hit_id"]: row["decision"] for row in predictions}
    ordered_lists = []
    seen = set()
    for name in LIST_ORDER:
        if lists.get(name):
            ordered_lists.append({"list_type": name, "hits": lists[name]})
            seen.add(name)
    for name, count in sorted(lists.items()):
        if name not in seen:
            ordered_lists.append({"list_type": name, "hits": count})
    def top(counter):
        return [{"reason_code": code, "hits": count} for code, count in counter.most_common()]
    labelled = practice_metrics(labels, engine_answers)
    baseline = practice_metrics(labels, baseline_answers)
    return {
        "hits": {
            "processed": len(predictions),
            "cleared": hit_counts["DIFFERENT_ENTITY"],
            "confirmed": hit_counts["SAME_ENTITY"],
            "review": hit_counts["REVIEW"],
        },
        "alerts": {
            "processed": len(by_alert),
            "cleared": alert_counts["CLEARED"],
            "escalated": alert_counts["ESCALATED"],
            "needs_analyst": alert_counts["NEEDS_ANALYST"],
        },
        "practice": labelled,
        "unlabelled_hits": len(predictions) - len(labelled_ids & engine_answers.keys()),
        "timing": {
            "measured": elapsed_ms is not None,
            "elapsed_ms": None if elapsed_ms is None else round(elapsed_ms, 1),
            "ms_per_hit": None if not elapsed_ms or not predictions else round(elapsed_ms / len(predictions), 2),
            "measured_at": measured_at,
            "what": "One decide_all run after the data files were already loaded. It does not include reading the JSONL files from disk.",
        },
        "list_types": ordered_lists,
        "reason_codes": {
            "clearing": top(codes["DIFFERENT_ENTITY"]),
            "confirming": top(codes["SAME_ENTITY"]),
            "review": top(codes["REVIEW"]),
        },
        "name_only_baseline": {
            "rule": (
                "SAME_ENTITY when the screened name agrees with the hit name or an alias "
                "under the engine's name rules; otherwise REVIEW. Never DIFFERENT_ENTITY."
            ),
            "labelled": baseline["labelled"],
            "right": baseline["right"],
            "unsafe_clearances": baseline["unsafe_clearances"],
            "score": baseline["score"],
            "scope": "the same labelled practice hits, not the unlabelled development hits",
        },
    }
