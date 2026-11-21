"""Deterministic case brief. It does not choose a different decision.

The brief restates the engine's comparison in analyst language and copies
the underlying records so a person can read them. Nothing here is a model.
"""

from __future__ import annotations


def build_brief(world, hit_id: str) -> dict:
    hit = world.hits.get(hit_id)
    if hit is None:
        return {"error": f"Unknown hit {hit_id}"}
    alert = world.alerts.get(hit.get("alert_id"))
    if alert is None:
        return {"error": f"Hit {hit_id} has no alert"}
    outcome = world.decide_hit(alert, hit)
    parties = world.resolve(alert)
    compared = [world._compare(alert, hit, party) for party in parties]
    facts = world.hit_facts(hit)
    records = _record_index(world)
    supporting = []
    contradicting = []
    missing = []
    for party, item in zip(parties, compared):
        who = party.seed_id
        if item["name_agrees"]:
            supporting.append(_claim(
                f"{who}: the name agrees with {item['hit_name']}.",
                _name_ids(party),
            ))
        elif item["name_mismatch"]:
            contradicting.append(_claim(
                f"{who}: the verified legal name agrees with none of the hit's names.",
                _name_ids(party),
            ))
        elif not item["usable"]:
            missing.append(f"{who}: the customer does not have two real name parts, so the name cannot confirm or clear.")
        if item["dob_agrees"]:
            supporting.append(_claim(
                f"{who}: the date of birth agrees with the hit.",
                [party.dob_source_id] if party.dob_source_id else [],
            ))
        elif item["dob_mismatch"]:
            contradicting.append(_claim(
                f"{who}: every date of birth on the hit contradicts the customer's date.",
                [party.dob_source_id] if party.dob_source_id else [],
            ))
        elif party.dob is None or not facts.births:
            missing.append(f"{who}: there is no reliable date of birth on both sides to compare.")
        elif not item["dob_agrees"] and not item["dob_mismatch"]:
            confident = [birth for birth in facts.births if birth.confident]
            uncertain = [birth for birth in facts.births if not birth.confident]
            if uncertain and not confident:
                missing.append(
                    f"{who}: the only birth year on the hit is inferred with low confidence, "
                    "so it is not used to clear or to confirm."
                )
            else:
                missing.append(f"{who}: the dates are too close to confirm and too close to clear.")
        if item["location_agree"]:
            supporting.append(_claim(
                f"{who}: a country is shared with the hit.",
                _country_ids(party, item["overlap"]),
            ))
        elif item["location_clears"]:
            contradicting.append(_claim(
                f"{who}: every country on the customer differs from every country on the hit.",
                _all_country_ids(party),
            ))
        elif item["location_mismatch"] and item["name_agrees"] and item["dob_agrees"]:
            missing.append(
                f"{who}: name and date of birth agree, so the country difference cannot clear the hit."
            )
        else:
            missing.append(f"{who}: one side has no country, so location cannot confirm or clear.")
        if item["type_mismatch"]:
            contradicting.append(_claim(
                f"{who}: a {item['customer_kind']} cannot be the same entity as a {item['hit_kind']}.",
                list(party.record_ids),
            ))
        if item["deceased"]:
            contradicting.append(_claim(
                f"{who}: the profile gives a date of death before screening.",
                list(party.record_ids),
            ))
        if item["temporal"]:
            contradicting.append(_claim(
                f"{who}: the customer could not have been the listed person at the earliest dated report.",
                [party.dob_source_id] if party.dob_source_id else list(party.record_ids),
            ))
    if not parties:
        missing.append("No customer record could be resolved, so the hit cannot be confirmed or cleared.")
    uncertain_ages = [item.label() for item in facts.births if not item.confident]
    if uncertain_ages:
        missing.append(
            "A search snippet or note suggests a birth year ("
            + ", ".join(uncertain_ages)
            + ") that is not tied tightly enough to the event to clear the hit by itself."
        )
    review_reasons = []
    if outcome.decision == "REVIEW":
        review_reasons.append(outcome.reason)
        if outcome.reason_code == "AMBIGUOUS_CUSTOMER_RECORD":
            review_reasons.append("More than one customer fits, and they do not lead to the same decision.")
        elif outcome.reason_code == "INSUFFICIENT_DATA":
            review_reasons.append("Nothing in the records both contradicts the hit and is reliable enough to clear it, and there is not enough agreement to confirm it.")
    cited = []
    for item_id in outcome.evidence:
        record = records.get(item_id)
        if record:
            cited.append(record)
        else:
            cited.append({"id": item_id, "kind": "Referenced id", "title": item_id, "text": ""})
    return {
        "hit_id": hit_id,
        "alert_id": hit.get("alert_id"),
        "source": "deterministic",
        "deterministic": {
            "decision": outcome.decision,
            "reason_code": outcome.reason_code,
            "reason": outcome.reason,
            "customer_record_id": outcome.customer_record_id,
        },
        "customer": [_customer_view(party) for party in parties],
        "watchlist": {
            "name": hit.get("name") or "",
            "aliases": hit.get("aliases") or [],
            "entity_type": hit.get("entity_type") or "",
            "dobs": [item.label() for item in facts.births] or list(hit.get("dobs") or []),
            "countries": sorted(facts.countries),
            "list_type": hit.get("primary_category") or ((hit.get("categories") or [None])[0]),
        },
        "supporting": supporting,
        "contradicting": contradicting,
        "missing": missing,
        "review_reasons": review_reasons,
        "records": cited,
    }


def _claim(fact: str, evidence_ids: list) -> dict:
    ids = [item for item in evidence_ids if item]
    return {"fact": fact, "evidence_ids": ids, "kind": "verified"}


def _name_ids(party) -> list:
    if party.identity and party.verified_name:
        return [party.identity.source_id]
    return list(party.record_ids)


def _country_ids(party, countries) -> list:
    ids = []
    for country in countries:
        ids.extend(sorted(party.country_sources.get(country, ())))
    return ids


def _all_country_ids(party) -> list:
    ids = []
    for sources in party.country_sources.values():
        ids.extend(sorted(sources))
    return ids


def _customer_view(party) -> dict:
    identity = party.identity
    return {
        "record_ids": list(party.record_ids),
        "verified_name": identity.name if identity and party.verified_name else None,
        "verified_from": identity.source_id if identity and party.verified_name else None,
        "date_of_birth": party.dob.label() if party.dob else None,
        "date_source": party.dob_source_id,
        "countries": sorted(party.countries),
        "rejected_or_expired_checks_ignored": True,
    }


def _record_index(world) -> dict:
    records = {}
    for record in world.customers.values():
        records[record["record_id"]] = {
            "id": record["record_id"],
            "kind": "Customer record",
            "title": record.get("full_name") or record["record_id"],
            "text": _join(
                record.get("entity_type"),
                record.get("dob"),
                record.get("residence_country"),
                record.get("nationality"),
                record.get("city"),
                record.get("address"),
            ),
        }
    for checks in world.checks_by_customer.values():
        for check in checks:
            records[check["check_id"]] = {
                "id": check["check_id"],
                "kind": "Identity check",
                "title": check.get("verified_name") or check["check_id"],
                "text": _join(
                    check.get("result"),
                    check.get("checked_at"),
                    check.get("verified_dob"),
                    check.get("document_country"),
                    check.get("document_number"),
                ),
            }
    for notes in world.notes_by_email.values():
        for note in notes:
            records[note["note_id"]] = {
                "id": note["note_id"],
                "kind": "Note",
                "title": note.get("created_at") or note["note_id"],
                "text": note.get("text") or "",
            }
    for results in world.search_by_name.values():
        for result in results:
            records[result["result_id"]] = {
                "id": result["result_id"],
                "kind": "Search result",
                "title": result.get("title") or result["result_id"],
                "text": _join(result.get("snippet"), result.get("query")),
            }
    return records


def _join(*parts) -> str:
    return " · ".join(str(part) for part in parts if part)
