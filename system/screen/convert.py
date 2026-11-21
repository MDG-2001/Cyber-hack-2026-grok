"""Turn the Google Sheets workbook into the JSONL folder system/run reads.

The workbook is a view of the same tables. Lists are written with semicolons
("1996; 1997", "H-1; H-2"). This script puts them back into the JSONL shape.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

LIST_FIELDS = {
    "aliases",
    "dobs",
    "nationality",
    "citizenship",
    "countries",
    "addresses",
    "identifiers",
    "categories",
    "sources",
    "source_urls",
    "relationships",
    "hit_ids",
    "reason_codes",
    "customer_record_ids",
    "evidence",
}


def _text(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _rows(sheet):
    iterator = sheet.iter_rows(values_only=True)
    header = [cell for cell in next(iterator)]
    while header and header[-1] is None:
        header.pop()
    names = [str(cell).strip() if cell is not None else "" for cell in header]
    for raw in iterator:
        if not any(cell not in (None, "") for cell in raw):
            continue
        record = {}
        for name, cell in zip(names, raw):
            if not name:
                continue
            if name in LIST_FIELDS:
                record[name] = _as_list(cell)
            else:
                value = _text(cell)
                if isinstance(value, float) and value.is_integer() and name not in {"match_score"}:
                    # Phone-like numbers should stay as written; dates already became strings.
                    pass
                record[name] = value
        yield record


def _sheet(workbook, *options):
    by_name = {name.lower(): name for name in workbook.sheetnames}
    for option in options:
        found = by_name.get(option.lower())
        if found:
            return workbook[found]
    raise SystemExit(
        "This workbook has no sheet named "
        + " or ".join(options)
        + f". It has: {', '.join(workbook.sheetnames)}"
    )


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _alert(row: dict) -> dict:
    return {
        "alert_id": row.get("alert_id"),
        "screened_at": row.get("screened_at"),
        "customer": {
            "name": row.get("customer_name") or row.get("name"),
            "entity_type": row.get("entity_type"),
            "dob": row.get("dob"),
            "country": row.get("country"),
            "city": row.get("city"),
        },
        "account_ref": row.get("account_ref"),
        "login_handle": row.get("login_handle"),
        "email": row.get("email"),
        "hit_ids": row.get("hit_ids") or [],
    }


def _hit(row: dict) -> dict:
    row = dict(row)
    row.setdefault("first_name", None)
    row.setdefault("middle_name", None)
    row.setdefault("last_name", None)
    row.setdefault("best_matching_name", None)
    row.setdefault("gender", None)
    row.setdefault("life_status", None)
    row.setdefault("deceased_date", None)
    row.setdefault("place_of_birth", None)
    row.setdefault("primary_category", None)
    row.setdefault("position_or_allegation", None)
    row.setdefault("profile_notes", None)
    for key in ("aliases", "dobs", "nationality", "citizenship", "countries", "addresses", "identifiers", "categories", "sources", "source_urls", "relationships"):
        row[key] = row.get(key) or []
    # The sheet flattens the details object. The decision does not read it.
    if not isinstance(row.get("details"), dict):
        row["details"] = {}
    if isinstance(row.get("match_score"), float):
        row["match_score"] = int(row["match_score"])
    return row


def _variants(policy_rows: list) -> dict:
    """The Policy sheet ends with the name groups, one comma-separated line each."""
    groups = []
    forms = []
    started = False
    for value in policy_rows:
        text = str(value).strip() if value is not None else ""
        if text == "name_variants.json":
            started = True
            continue
        if not started:
            continue
        if text.lower().startswith("company legal forms"):
            forms = [part.strip() for part in text.split(":", 1)[1].split(",") if part.strip()]
            break
        if "," in text and text[:1].islower():
            groups.append([part.strip() for part in text.split(",") if part.strip()])
    if not groups or not forms:
        raise SystemExit("Could not find the name variant groups on the Policy sheet.")
    return {
        "description": "Groups of given-name forms that count as the same name part, and the company legal forms that are not name parts.",
        "groups": groups,
        "company_legal_forms": forms,
    }


def convert(workbook_path: str, folder: str) -> dict:
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("Install openpyxl first: python3 -m pip install openpyxl")

    workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    os.makedirs(folder, exist_ok=True)

    alerts = [_alert(row) for row in _rows(_sheet(workbook, "Alerts"))]
    hits = [_hit(row) for row in _rows(_sheet(workbook, "Hits"))]
    customers = list(_rows(_sheet(workbook, "Customers")))
    accounts = list(_rows(_sheet(workbook, "Accounts")))
    onboarding = list(_rows(_sheet(workbook, "Onboarding")))
    checks = list(_rows(_sheet(workbook, "KYC checks", "KYC Checks")))
    notes = list(_rows(_sheet(workbook, "Notes")))
    searches = list(_rows(_sheet(workbook, "Search results", "Search Results")))

    _write_jsonl(os.path.join(folder, "alerts.jsonl"), alerts)
    _write_jsonl(os.path.join(folder, "hits.jsonl"), hits)
    _write_jsonl(os.path.join(folder, "customers.jsonl"), customers)
    _write_jsonl(os.path.join(folder, "accounts.jsonl"), accounts)
    _write_jsonl(os.path.join(folder, "onboarding.jsonl"), onboarding)
    _write_jsonl(os.path.join(folder, "kyc_checks.jsonl"), checks)
    _write_jsonl(os.path.join(folder, "notes.jsonl"), notes)
    _write_jsonl(os.path.join(folder, "search_results.jsonl"), searches)

    policy = _sheet(workbook, "Policy")
    policy_values = [row[0] for row in policy.iter_rows(values_only=True)]
    variants = _variants(policy_values)
    with open(os.path.join(folder, "name_variants.json"), "w", encoding="utf-8") as handle:
        json.dump(variants, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    if any(name.lower() == "practice labels" for name in workbook.sheetnames):
        labels = []
        for row in _rows(_sheet(workbook, "Practice labels", "Practice Labels")):
            path = row.get("path")
            if isinstance(path, str):
                row["path"] = [part.strip() for part in path.split("→") if part.strip()]
            labels.append(row)
        _write_jsonl(os.path.join(folder, "practice_labels.jsonl"), labels)

    workbook.close()
    summary = {
        "alerts": len(alerts),
        "hits": len(hits),
        "customers": len(customers),
        "accounts": len(accounts),
        "onboarding": len(onboarding),
        "kyc_checks": len(checks),
        "notes": len(notes),
        "search_results": len(searches),
    }
    return summary


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) != 3:
        print("usage: python3 system/convert_xlsx.py <sheet.xlsx> <output folder>", file=sys.stderr)
        sys.exit(2)
    summary = convert(argv[1], argv[2])
    print(f"Wrote {summary['alerts']} alerts and {summary['hits']} hits to {argv[2]}")
    for name, count in summary.items():
        if name not in ("alerts", "hits"):
            print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
