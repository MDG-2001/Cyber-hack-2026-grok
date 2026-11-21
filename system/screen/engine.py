"""Decide SAME_ENTITY, DIFFERENT_ENTITY or REVIEW for every hit.

The pipeline follows the bank's identity SOP, in order:

1. Find the customer record the alert points at, then bring in other records
   of the same person (shared phone and full date of birth, or the same
   document number on an approved check).
2. Take the legal name, date of birth and document country from the latest
   approved identity check. A rejected or expired check is ignored.
3. Read the hit's dates from its fields, then the first paragraph of its
   profile, then search results that describe the same kind of listing in a
   country tied to the hit.
4. Clear only on a contradiction. Confirm only on agreement. Otherwise review.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from .countries import canon, from_fields, joined, mentioned
from .dates import (
    When,
    certainly_after,
    certainly_before,
    compare_births,
    max_age,
    overlaps_screen,
    parse_when,
)
from .names import NameIndex, key_tokens
from .notes import lived_countries, parse_identity

MONTH = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
_DATED_REPORT = re.compile(rf"According to [^;\n]+;\s*((?:{MONTH}) \d{{1,2}}, \d{{4}}):")
_AGE_PATTERNS = [
    re.compile(r"\b(\d{1,3})\s*-\s*year\s*-\s*old\b", re.I),
    re.compile(r"\b(\d{1,3})\s+years?\s+old\b", re.I),
    re.compile(r"\bat the age of\s+(\d{1,3})\b", re.I),
    re.compile(r"\breached the age of\s+(\d{1,3})\b", re.I),
    re.compile(r"\bthe age of\b[^.]{0,60}?\bwas\s+(\d{1,3})\b", re.I),
    re.compile(r"\baged\s+(\d{1,3})\b", re.I),
    re.compile(r"\bat age\s+(\d{1,3})\b", re.I),
    re.compile(r"\bwas\s+(\d{1,3})\b", re.I),
    re.compile(r",\s*(\d{1,3})\s*,"),
]
_YEAR_IN_TEXT = re.compile(rf"\b(?:{MONTH})\s+(?:\d{{1,2}},?\s+)?(\d{{4}})\b", re.I)

PHRASES = {
    "sanctions": "a sanctioned individual",
    "pep": "a politically exposed person",
    "enforcement": "the subject of enforcement action",
    "adverse media": "named in adverse media",
    "soe": "a state-owned enterprise official",
}


def is_company(entity_type: str | None) -> bool:
    if not entity_type:
        return False
    return entity_type.strip().lower() in {
        "company",
        "organisation",
        "organization",
        "business",
        "entity",
    }


def kind_of(entity_type: str | None) -> str | None:
    if not entity_type:
        return None
    return "company" if is_company(entity_type) else "person"


def listing_phrase(hit: dict) -> str:
    category = hit.get("primary_category") or ""
    if not category:
        categories = hit.get("categories") or []
        category = categories[0] if categories else ""
    return PHRASES.get(str(category).strip().lower(), "named on a watchlist")


def first_paragraph(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    return re.split(r"\n\s*\n", text, maxsplit=1)[0]


def age_births(text: str, origin: str) -> list[When]:
    """An age at a dated event is two possible birth years, one year apart."""
    if not text:
        return []
    year_match = _YEAR_IN_TEXT.search(text)
    if not year_match:
        return []
    event_year = int(year_match.group(1))
    for pattern in _AGE_PATTERNS:
        age_match = pattern.search(text)
        if not age_match:
            continue
        age = int(age_match.group(1))
        if not 1 <= age <= 110:
            continue
        lo = event_year - age - 1
        hi = event_year - age
        return [When(years=(lo, hi), origin=origin)]
    return []


def dated_reports(text: str) -> list[When]:
    found = []
    for match in _DATED_REPORT.finditer(text or ""):
        parsed = parse_when(match.group(1))
        if parsed is not None:
            found.append(parsed)
    return found


def hit_countries(hit: dict) -> set[str]:
    found: set[str] = set()
    found |= from_fields(hit.get("countries"))
    found |= from_fields(hit.get("nationality"))
    found |= from_fields(hit.get("citizenship"))
    found |= from_fields(hit.get("place_of_birth"))
    found |= from_fields(hit.get("addresses"))
    return found


def digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


@dataclass
class Identity:
    source_id: str
    name: str | None
    dob: When | None
    document_country: str | None
    when: str


@dataclass
class Party:
    """One resolved customer: every record that belongs to the same person."""

    record_ids: list[str]
    seed_id: str
    company: bool
    identity: Identity | None
    names: list[tuple[str, str]]  # (display name, source id) used for comparison
    verified_name: bool
    dob: When | None
    dob_source_id: str | None
    countries: set[str]
    country_sources: dict[str, set[str]]  # country → source ids
    lived_note_ids: list[str] = field(default_factory=list)


@dataclass
class HitFacts:
    names: list[str]
    births: list[When]
    birth_origin: str
    search_ids: list[str]
    countries: set[str]
    company: bool
    reports: list[When]
    death: When | None


@dataclass
class Outcome:
    decision: str
    reason_code: str
    reason: str
    evidence: list[str]
    customer_record_id: str | None


class World:
    def __init__(self, folder: str):
        self.folder = folder
        variants = json.load(open(os.path.join(folder, "name_variants.json"), encoding="utf-8"))
        self.names = NameIndex(variants["groups"], variants["company_legal_forms"])
        self.alerts = {row["alert_id"]: row for row in load_jsonl(os.path.join(folder, "alerts.jsonl"))}
        self.hits = {row["hit_id"]: row for row in load_jsonl(os.path.join(folder, "hits.jsonl"))}
        self.customers = {row["record_id"]: row for row in load_jsonl(os.path.join(folder, "customers.jsonl"))}
        accounts = load_jsonl(os.path.join(folder, "accounts.jsonl"))
        onboarding = load_jsonl(os.path.join(folder, "onboarding.jsonl"))
        checks = load_jsonl(os.path.join(folder, "kyc_checks.jsonl"))
        notes = load_jsonl(os.path.join(folder, "notes.jsonl"))
        searches = load_jsonl(os.path.join(folder, "search_results.jsonl"))

        self.accounts_by_id: dict[str, list[dict]] = defaultdict(list)
        self.accounts_by_customer: dict[str, list[dict]] = defaultdict(list)
        self.login_to_customer: dict[str, str] = {}
        for account in accounts:
            self.accounts_by_id[account["account_id"]].append(account)
            self.accounts_by_customer[account["customer_id"]].append(account)
            if account.get("login_handle"):
                self.login_to_customer[account["login_handle"]] = account["customer_id"]

        self.applicant_to_customers: dict[str, set[str]] = defaultdict(set)
        for row in onboarding:
            self.applicant_to_customers[row["kyc_applicant_id"]].add(row["customer_id"])

        self.checks_by_customer: dict[str, list[dict]] = defaultdict(list)
        for check in checks:
            for customer_id in self.applicant_to_customers.get(check["applicant_id"], ()):
                self.checks_by_customer[customer_id].append(check)

        self.notes_by_email: dict[str, list[dict]] = defaultdict(list)
        for note in notes:
            email = (note.get("requester_email") or "").lower()
            if email:
                self.notes_by_email[email].append(note)

        self.search_by_name: dict[frozenset[str], list[dict]] = defaultdict(list)
        for result in searches:
            self.search_by_name[key_tokens(result.get("query") or "")].append(result)

        self._link_people()
        self._index_customers()
        self._party_cache: dict[tuple[str, ...], Party] = {}

    def _link_people(self):
        self.people = UnionFind(self.customers)
        phone_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for record in self.customers.values():
            phone = digits(record.get("phone"))
            dob = parse_when(record.get("dob"))
            if len(phone) < 6 or dob is None or not (dob.month and dob.day):
                continue
            phone_groups[(phone, dob.label())].append(record["record_id"])
        for group in phone_groups.values():
            for other in group[1:]:
                self.people.union(group[0], other)

        document_groups: dict[str, set[str]] = defaultdict(set)
        for customer_id, checks in self.checks_by_customer.items():
            for check in checks:
                if check.get("result") != "approved":
                    continue
                number = (check.get("document_number") or "").strip()
                if number:
                    document_groups[number].add(customer_id)
        for group in document_groups.values():
            group = list(group)
            for other in group[1:]:
                self.people.union(group[0], other)

        self.members: dict[str, list[str]] = defaultdict(list)
        for record_id in self.customers:
            self.members[self.people.find(record_id)].append(record_id)
        for root in self.members:
            self.members[root].sort()

    def _index_customers(self):
        self.by_email: dict[str, list[str]] = defaultdict(list)
        self.by_country_year: dict[tuple[str, int], list[str]] = defaultdict(list)
        self.by_year: dict[int, list[str]] = defaultdict(list)
        self.by_country: dict[str, list[str]] = defaultdict(list)
        for record in self.customers.values():
            record_id = record["record_id"]
            email = (record.get("email") or "").lower()
            if email:
                self.by_email[email].append(record_id)
            dob = parse_when(record.get("dob"))
            countries = set()
            residence = canon(record.get("residence_country"))
            nationality = canon(record.get("nationality"))
            if residence:
                countries.add(residence)
            if nationality:
                countries.add(nationality)
            for country in countries:
                self.by_country[country].append(record_id)
                if dob is not None:
                    self.by_country_year[(country, dob.year)].append(record_id)
            if dob is not None:
                self.by_year[dob.year].append(record_id)

    def person_ids(self, record_id: str) -> list[str]:
        return self.members[self.people.find(record_id)]

    def party_for(self, seed_id: str) -> Party:
        record_ids = tuple(self.person_ids(seed_id))
        cached = self._party_cache.get(record_ids)
        if cached is not None:
            return cached
        party = self._build_party(list(record_ids), seed_id)
        self._party_cache[record_ids] = party
        return party

    def _build_party(self, record_ids: list[str], seed_id: str) -> Party:
        records = [self.customers[record_id] for record_id in record_ids]
        company = any(is_company(record.get("entity_type")) for record in records)
        emails = {(record.get("email") or "").lower() for record in records if record.get("email")}

        identities: list[Identity] = []
        for record_id in record_ids:
            for check in self.checks_by_customer.get(record_id, []):
                if check.get("result") != "approved":
                    continue
                identities.append(
                    Identity(
                        source_id=check["check_id"],
                        name=(check.get("verified_name") or None),
                        dob=parse_when(check.get("verified_dob")),
                        document_country=canon(check.get("document_country")),
                        when=check.get("checked_at") or "",
                    )
                )
        for email in emails:
            for note in self.notes_by_email.get(email, []):
                if note.get("channel") != "kyc":
                    continue
                parsed = parse_identity(note.get("text") or "", note.get("created_at"))
                if parsed is None:
                    continue
                identities.append(
                    Identity(
                        source_id=note["note_id"],
                        name=parsed.name,
                        dob=parsed.dob,
                        document_country=parsed.document_country,
                        when=note.get("created_at") or "",
                    )
                )
        identities.sort(key=lambda item: (item.when, item.source_id))
        identity = identities[-1] if identities else None

        country_sources: dict[str, set[str]] = defaultdict(set)
        for record in records:
            for country in (canon(record.get("residence_country")), canon(record.get("nationality"))):
                if country:
                    country_sources[country].add(record["record_id"])
        lived_notes = []
        for email in emails:
            for note in self.notes_by_email.get(email, []):
                countries = lived_countries(note.get("text") or "")
                if not countries:
                    continue
                lived_notes.append(note["note_id"])
                for country in countries:
                    country_sources[country].add(note["note_id"])
        if identity and identity.document_country:
            country_sources[identity.document_country].add(identity.source_id)

        declared_names = []
        for record in records:
            for key in ("full_name", "preferred_name", "login_name"):
                if record.get(key):
                    declared_names.append((record[key], record["record_id"]))

        if identity and identity.name:
            compare_names = [(identity.name, identity.source_id)]
            verified = True
        else:
            compare_names = declared_names
            verified = False

        dob = identity.dob if identity and identity.dob else None
        dob_source = identity.source_id if identity and identity.dob else None
        if dob is None:
            for record in records:
                parsed = parse_when(record.get("dob"))
                if parsed is not None:
                    dob = parsed
                    dob_source = record["record_id"]
                    break

        return Party(
            record_ids=record_ids,
            seed_id=seed_id if seed_id in record_ids else record_ids[0],
            company=company,
            identity=identity,
            names=compare_names,
            verified_name=verified,
            dob=dob,
            dob_source_id=dob_source,
            countries=set(country_sources),
            country_sources=dict(country_sources),
            lived_note_ids=lived_notes,
        )

    def _fits(self, record: dict, alert: dict) -> bool:
        screened = alert["customer"]
        company = is_company(screened.get("entity_type")) or is_company(record.get("entity_type"))
        screened_name = self.names.tokens(screened.get("name"), company=company)
        matched = False
        for key in ("full_name", "preferred_name", "login_name"):
            if self.names.agree(screened_name, self.names.tokens(record.get(key), company=company)):
                matched = True
                break
        if not matched:
            return False
        screened_dob = parse_when(screened.get("dob"))
        if screened_dob is not None and not overlaps_screen(screened_dob, parse_when(record.get("dob"))):
            return False
        screened_country = canon(screened.get("country"))
        if screened_country:
            residence = canon(record.get("residence_country"))
            nationality = canon(record.get("nationality"))
            if screened_country not in {residence, nationality}:
                return False
        city = (screened.get("city") or "").strip()
        if city:
            from .names import fold_key

            wanted = fold_key(city)
            blob = fold_key(f"{record.get('city') or ''} {record.get('address') or ''}")
            if wanted not in blob:
                return False
        return True

    def resolve(self, alert: dict) -> list[Party]:
        """Customer records the alert may be about. One party per real person."""
        seeds: list[str] = []
        linked = False
        if alert.get("account_ref"):
            linked = True
            seeds = [
                row["customer_id"]
                for row in self.accounts_by_id.get(alert["account_ref"], [])
                if row["customer_id"] in self.customers
            ]
        elif alert.get("login_handle"):
            linked = True
            customer_id = self.login_to_customer.get(alert["login_handle"])
            if customer_id in self.customers:
                seeds = [customer_id]
        elif alert.get("email"):
            linked = True
            seeds = list(self.by_email.get(alert["email"].lower(), []))

        if linked:
            persons: dict[str, str] = {}
            for seed in seeds:
                persons.setdefault(self.people.find(seed), seed)
            if len(persons) <= 1:
                return [self.party_for(seed) for seed in persons.values()]
            matched = []
            for seed in persons.values():
                # The screened details pick among the people a shared account can reach.
                if any(self._fits(self.customers[record_id], alert) for record_id in self.person_ids(seed)):
                    matched.append(seed)
            chosen = matched or []
            if not chosen:
                # None of the linked people fit what was screened. Do not guess.
                return []
            return [self.party_for(seed) for seed in chosen]

        screened = alert["customer"]
        screened_dob = parse_when(screened.get("dob"))
        screened_country = canon(screened.get("country"))
        if screened_country and screened_dob is not None:
            pool = self.by_country_year.get((screened_country, screened_dob.year), [])
        elif screened_dob is not None:
            pool = self.by_year.get(screened_dob.year, [])
        elif screened_country:
            pool = self.by_country.get(screened_country, [])
        else:
            pool = []
        matched_people: dict[str, str] = {}
        for record_id in pool:
            if self._fits(self.customers[record_id], alert):
                matched_people.setdefault(self.people.find(record_id), record_id)
        return [self.party_for(seed) for seed in matched_people.values()]

    def hit_facts(self, hit: dict) -> HitFacts:
        company = is_company(hit.get("entity_type"))
        names = []
        for value in [hit.get("name"), hit.get("best_matching_name"), *(hit.get("aliases") or [])]:
            if value:
                names.append(value)
        built = " ".join(part for part in (hit.get("first_name"), hit.get("middle_name"), hit.get("last_name")) if part)
        if built:
            names.append(built)

        listed = []
        for value in hit.get("dobs") or []:
            parsed = parse_when(value)
            if parsed is not None:
                listed.append(parsed)
        search_ids: list[str] = []
        if company:
            births, origin = listed, "listed"
        elif listed:
            births, origin = listed, "listed"
        else:
            from_notes = age_births(first_paragraph(hit.get("profile_notes") or ""), "notes")
            if from_notes:
                births, origin = from_notes, "notes"
            else:
                births = []
                origin = "search"
                phrase = listing_phrase(hit)
                countries = hit_countries(hit)
                results = self.search_by_name.get(key_tokens(hit.get("name") or ""), [])
                for result in sorted(results, key=lambda row: row["result_id"]):
                    blob = f"{result.get('title') or ''} {result.get('snippet') or ''}"
                    if phrase not in blob.lower():
                        continue
                    if not (mentioned(blob) & countries):
                        continue
                    found = age_births(blob, "search")
                    if not found:
                        continue
                    births.extend(found)
                    search_ids.append(result["result_id"])
        return HitFacts(
            names=names,
            births=births,
            birth_origin=origin if births else "",
            search_ids=search_ids,
            countries=hit_countries(hit),
            company=company,
            reports=dated_reports(hit.get("profile_notes") or ""),
            death=parse_when(hit.get("deceased_date")),
        )

    def _compare(self, alert: dict, hit: dict, party: Party) -> dict:
        """The checks judge() uses, in one place, so a trace cannot drift from the verdict."""
        facts = self.hit_facts(hit)
        customer_kind = "company" if party.company else "person"
        hit_kind = "company" if facts.company else "person"
        type_mismatch = customer_kind != hit_kind

        screened_at = parse_when(alert.get("screened_at"))
        deceased = bool(facts.death and screened_at and certainly_before(facts.death, screened_at))

        temporal = False
        temporal_age = None
        temporal_on = None
        if facts.reports and party.dob is not None:
            earliest = min(facts.reports, key=lambda item: (item.year, item.month or 0, item.day or 0))
            if customer_kind == "person" and max_age(party.dob, earliest) < 16:
                temporal = True
                temporal_age = max_age(party.dob, earliest)
                temporal_on = earliest
            if customer_kind == "company" and certainly_after(party.dob, earliest):
                temporal = True
                temporal_on = earliest

        name_agrees = False
        name_source = None
        verified_display = None
        customer_tokens = []
        for display, source in party.names:
            tokens = self.names.tokens(display, company=party.company)
            customer_tokens.append(tokens)
            if name_source is None and tokens:
                name_source = source
                verified_display = display
        usable = True
        if customer_kind == "person":
            usable = any(len(tokens) >= 2 for tokens in customer_tokens)
        hit_token_sets = [self.names.tokens(name, company=facts.company) for name in facts.names]
        if usable:
            for tokens in customer_tokens:
                if any(self.names.agree(tokens, hit_tokens) for hit_tokens in hit_token_sets):
                    name_agrees = True
                    break
        name_mismatch = bool(party.verified_name and usable and not name_agrees and customer_tokens)

        dob_agrees, dob_mismatch = compare_births(party.dob, facts.births)

        overlap = party.countries & facts.countries
        location_agree = bool(overlap)
        location_mismatch = bool(party.countries and facts.countries and not overlap)
        # A country difference does not clear a hit whose name and date of birth agree.
        location_clears = location_mismatch and not (name_agrees and dob_agrees)
        return {
            "facts": facts,
            "customer_kind": customer_kind,
            "hit_kind": hit_kind,
            "type_mismatch": type_mismatch,
            "screened_at": screened_at,
            "deceased": deceased,
            "temporal": temporal,
            "temporal_age": temporal_age,
            "temporal_on": temporal_on,
            "name_agrees": name_agrees,
            "name_source": name_source,
            "verified_display": verified_display,
            "usable": usable,
            "name_mismatch": name_mismatch,
            "dob_agrees": dob_agrees,
            "dob_mismatch": dob_mismatch,
            "overlap": overlap,
            "location_agree": location_agree,
            "location_mismatch": location_mismatch,
            "location_clears": location_clears,
            "hit_name": hit.get("name") or "the listed name",
        }

    def judge(self, alert: dict, hit: dict, party: Party) -> Outcome:
        compared = self._compare(alert, hit, party)
        facts = compared["facts"]
        customer_kind = compared["customer_kind"]
        hit_kind = compared["hit_kind"]
        type_mismatch = compared["type_mismatch"]
        screened_at = compared["screened_at"]
        deceased = compared["deceased"]
        temporal = compared["temporal"]
        temporal_age = compared["temporal_age"]
        temporal_on = compared["temporal_on"]
        name_agrees = compared["name_agrees"]
        name_source = compared["name_source"]
        verified_display = compared["verified_display"]
        name_mismatch = compared["name_mismatch"]
        dob_agrees = compared["dob_agrees"]
        dob_mismatch = compared["dob_mismatch"]
        overlap = compared["overlap"]
        location_agree = compared["location_agree"]
        location_mismatch = compared["location_mismatch"]
        location_clears = compared["location_clears"]
        hit_name = compared["hit_name"]
        if type_mismatch:
            code = "ENTITY_TYPE_MISMATCH"
            decision = "DIFFERENT_ENTITY"
        elif deceased:
            code = "DECEASED"
            decision = "DIFFERENT_ENTITY"
        elif temporal:
            code = "TEMPORAL_IMPOSSIBILITY"
            decision = "DIFFERENT_ENTITY"
        elif dob_mismatch:
            code = "DOB_MISMATCH"
            decision = "DIFFERENT_ENTITY"
        elif name_mismatch:
            code = "NAME_MISMATCH_VERIFIED"
            decision = "DIFFERENT_ENTITY"
        elif location_clears:
            code = "LOCATION_MISMATCH"
            decision = "DIFFERENT_ENTITY"
        elif customer_kind == "person" and name_agrees and dob_agrees and not location_mismatch:
            decision = "SAME_ENTITY"
            code = "NAME_DOB_LOCATION_MATCH" if location_agree else "NAME_DOB_MATCH"
        elif customer_kind == "company" and name_agrees and location_agree:
            decision = "SAME_ENTITY"
            code = "NAME_LOCATION_MATCH"
        else:
            decision = "REVIEW"
            code = "INSUFFICIENT_DATA"

        evidence = self._evidence(
            decision,
            party,
            facts,
            name_source if party.verified_name else party.dob_source_id,
            location_clears,
            name_mismatch,
            temporal or deceased or type_mismatch,
            overlap,
        )
        reason = self._reason(
            decision,
            code,
            party,
            facts,
            hit_name,
            verified_display,
            name_agrees,
            name_mismatch,
            dob_agrees,
            dob_mismatch,
            location_agree,
            location_mismatch,
            overlap,
            temporal,
            temporal_age,
            temporal_on,
            deceased,
            screened_at,
            type_mismatch,
            customer_kind,
            hit_kind,
        )
        return Outcome(decision, code, reason, evidence, party.seed_id)

    def _evidence(
        self,
        decision,
        party: Party,
        facts: HitFacts,
        name_source,
        location_clears,
        name_mismatch,
        hard_clear,
        overlap,
    ) -> list[str]:
        evidence: set[str] = set()
        all_country_ids = {source for sources in party.country_sources.values() for source in sources}
        overlap_ids = {source for country in overlap for source in party.country_sources.get(country, ())}

        if decision == "SAME_ENTITY":
            evidence.update(party.record_ids)
            if party.identity:
                evidence.add(party.identity.source_id)
            if party.dob_source_id:
                evidence.add(party.dob_source_id)
            evidence.update(facts.search_ids)
        elif decision == "DIFFERENT_ENTITY":
            # Country clearance cites every record that put a country on the customer.
            # A date or type clearance cites the customer and the check the date came from.
            # A pure date or name clearance does not also cite countries that agreed.
            if location_clears:
                evidence.update(all_country_ids)
            if hard_clear:
                evidence.update(party.record_ids)
                if party.identity:
                    evidence.add(party.identity.source_id)
                if party.dob_source_id:
                    evidence.add(party.dob_source_id)
            if not location_clears and party.dob_source_id:
                evidence.add(party.dob_source_id)
                evidence.update(facts.search_ids)
            if name_mismatch and name_source:
                evidence.add(name_source)
        else:
            evidence.update(overlap_ids)
            if not evidence:
                if party.identity:
                    evidence.add(party.identity.source_id)
                elif party.dob_source_id:
                    evidence.add(party.dob_source_id)
                elif party.record_ids:
                    evidence.add(party.seed_id)
        return sorted(evidence, key=_evidence_key)

    def _reason(self, decision, code, party, facts, hit_name, verified_display, name_agrees, name_mismatch,
                dob_agrees, dob_mismatch, location_agree, location_mismatch, overlap, temporal, temporal_age,
                temporal_on, deceased, screened_at, type_mismatch, customer_kind, hit_kind) -> str:
        sentences = []
        if type_mismatch:
            sentences.append(
                f"The customer is a {customer_kind} and the hit is a {hit_kind}, so they cannot be the same entity."
            )
        if deceased and facts.death and screened_at:
            sentences.append(
                f"The hit's profile gives a date of death of {facts.death.label()}, "
                f"which is before the customer was screened on {screened_at.label()}."
            )
        if temporal and party.dob is not None and temporal_on is not None:
            source = party.dob_source_id or party.seed_id
            if customer_kind == "company":
                sentences.append(
                    f"The customer was incorporated on {party.dob.label()} ({source}), "
                    f"which is after the hit's earliest dated report on {temporal_on.label()}."
                )
            else:
                # "Born in 2007" is read as a year. The oldest that year allows is
                # report year minus birth year. Use the exact age only when that
                # upper bound is no longer under 16 but the real age still is.
                year_gap = temporal_on.year - party.dob.year
                if temporal_age is not None and temporal_age < year_gap and year_gap >= 16:
                    age_words = f"they were {temporal_age}"
                else:
                    age_words = f"they were at most {year_gap}"
                sentences.append(
                    f"The customer was born in {party.dob.year} ({source}), so {age_words} "
                    f"when the hit's profile reports activity on {temporal_on.label()}."
                )
        if location_mismatch and decision == "DIFFERENT_ENTITY":
            sentences.append(
                f"The customer is linked only to {joined(party.countries)} "
                f"but the hit is linked only to {joined(facts.countries)}."
            )
        if dob_mismatch and party.dob is not None:
            sentences.append(
                f"The customer's date of birth is {party.dob.label()} ({party.dob_source_id}) "
                f"but {self._hit_birth_clause(facts)}."
            )
        if name_mismatch and verified_display:
            source = party.identity.source_id if party.identity else party.seed_id
            sentences.append(
                f"The customer's verified name is {verified_display} ({source}), "
                f"which does not match the hit's name {hit_name}."
            )
        if decision == "SAME_ENTITY":
            if customer_kind == "person" and party.dob is not None:
                sentences.append(
                    f"The customer's date of birth {party.dob.label()} ({party.dob_source_id}) "
                    f"agrees with the hit's {self._hit_birth_label(facts)}."
                )
            if location_agree:
                sentences.append(f"Both the customer and the hit are linked to {joined(overlap)}.")
            if name_agrees:
                sentences.append(f"The customer's name agrees with the hit's name {hit_name}.")
        if decision == "REVIEW":
            if location_agree:
                sentences.append(f"Both the customer and the hit are linked to {joined(overlap)}.")
            elif location_mismatch and name_agrees and dob_agrees:
                sentences.append(
                    f"The customer's name and date of birth agree with the hit, "
                    f"but the customer is linked only to {joined(party.countries)} "
                    f"and the hit is linked only to {joined(facts.countries)}."
                )
            if name_agrees:
                sentences.append(f"The customer's name agrees with the hit's name {hit_name}.")
            elif not name_mismatch:
                sentences.append(
                    f"The name on the customer's records does not agree closely enough with the hit's name {hit_name} to confirm it."
                )
            if dob_agrees and location_mismatch:
                sentences.append(
                    "A country difference does not clear a hit whose name and date of birth agree, "
                    "and it is not enough to confirm the hit either, so it stays in review."
                )
            elif not dob_agrees and not dob_mismatch:
                sentences.append(
                    "Nothing in the customer's records contradicts the hit, and the date of birth "
                    "cannot be compared with it, so the hit can be neither cleared nor confirmed."
                )
            elif dob_mismatch is False and party.dob is not None and facts.births and not dob_agrees:
                sentences.append(
                    f"The customer's date of birth {party.dob.label()} ({party.dob_source_id}) "
                    f"is close to {self._hit_birth_clause(facts)}, but not close enough to confirm or to clear."
                )
        # Keep the reason to a few sentences and drop accidental duplicates.
        unique = []
        for sentence in sentences:
            if sentence and sentence not in unique:
                unique.append(sentence)
        if not unique:
            unique.append("The evidence does not confirm the hit and does not contradict it, so it needs review.")
        return " ".join(unique[:4])

    def _hit_birth_label(self, facts: HitFacts) -> str:
        if not facts.births:
            return "date of birth"
        if len(facts.births) == 1:
            return facts.births[0].label()
        if facts.birth_origin == "listed":
            return ", ".join(item.label() for item in facts.births)
        return facts.births[0].label()

    def _hit_birth_clause(self, facts: HitFacts) -> str:
        label = self._hit_birth_label(facts)
        if facts.birth_origin == "notes":
            return f"the hit's profile notes put its birth year at {label}"
        if facts.birth_origin == "search":
            return f"the hit's search results put its birth year at {label}"
        if len(facts.births) == 1:
            return f"the hit's listed date of birth is {label}"
        return f"the hit's listed dates of birth are {label}"

    def describe_link(self, alert: dict, parties: list) -> str:
        """How the alert was tied to customer records. Does not choose the records."""
        if alert.get("_entered"):
            return "These details were typed into Try your own. They are not a customer in the bank's files."
        if alert.get("account_ref"):
            holders = [
                row["customer_id"]
                for row in self.accounts_by_id.get(alert["account_ref"], [])
                if row.get("customer_id")
            ]
            opener = f"Account {alert['account_ref']} is held by {', '.join(holders) or 'no one in the file'}."
        elif alert.get("login_handle"):
            owner = self.login_to_customer.get(alert["login_handle"])
            opener = f"Login {alert['login_handle']} belongs to {owner or 'no account'}."
        elif alert.get("email"):
            found = self.by_email.get(str(alert["email"]).lower(), [])
            opener = f"Email {alert['email']} matches {len(found)} customer record(s)."
        else:
            screened = alert.get("customer") or {}
            opener = (
                "The alert has no account, login or email, so the screened name, date of birth, "
                f"country and city were searched: {screened.get('name') or 'unknown name'}"
                f", born {screened.get('dob') or 'unknown'}"
                f", {screened.get('country') or 'unknown country'}"
                + (f", {screened.get('city')}" if screened.get("city") else "")
                + "."
            )
        if not parties:
            return opener + " No customer record fits, so the hit cannot be confirmed or cleared."
        if len(parties) == 1:
            party = parties[0]
            extra = ""
            if len(party.record_ids) > 1:
                extra = (
                    " Same person on more than one record ("
                    + ", ".join(party.record_ids)
                    + "), linked by a shared phone and full date of birth or by the same document number."
                )
            return opener + f" Resolved to {party.seed_id}." + extra
        return (
            opener
            + f" {len(parties)} possible customers fit: "
            + ", ".join(party.seed_id for party in parties)
            + "."
        )

    def _steps(self, alert: dict, hit: dict, parties: list, outcome: Outcome) -> list:
        compared = [self._compare(alert, hit, party) for party in parties] if parties else []

        def values(key, empty="not on file"):
            bits = []
            for party, item in zip(parties, compared):
                bits.append(f"{party.seed_id}: {key(party, item)}")
            return "; ".join(bits) if bits else empty

        steps = []
        link = self.describe_link(alert, parties)
        if len(parties) > 1 and outcome.decision == "DIFFERENT_ENTITY":
            link += f" {len(parties)} possible customers, all contradicted, so cleared."
        elif len(parties) > 1 and outcome.customer_record_id == "AMBIGUOUS":
            link += " Each possible customer is decided on its own."
        steps.append({"title": "Customer link", "status": "pass" if parties else "unknown", "detail": link})

        if parties:
            identity_bits = []
            for party in parties:
                if party.identity and (party.identity.name or party.identity.dob):
                    born = party.identity.dob.label() if party.identity.dob else "no date"
                    country = party.identity.document_country or "no document country"
                    identity_bits.append(
                        f"{party.seed_id}: {party.identity.name or 'no verified name'}, born {born}, "
                        f"document {country} ({party.identity.source_id})"
                    )
                else:
                    identity_bits.append(f"{party.seed_id}: no approved identity check, so the customer record is used")
            steps.append({
                "title": "Verified identity",
                "status": "pass",
                "detail": " ".join(identity_bits),
            })

        def check(title, status, customer, hit_value, detail):
            steps.append({
                "title": title,
                "status": status,
                "customer": customer,
                "hit": hit_value,
                "detail": detail,
            })

        if compared:
            name_status = "unknown"
            if any(item["name_mismatch"] for item in compared):
                name_status = "contradiction"
            elif any(item["name_agrees"] for item in compared) and not any(item["name_mismatch"] for item in compared):
                name_status = "pass"
            names = values(lambda party, item: item["verified_display"] or "no usable name")
            hit_names = ", ".join(compared[0]["facts"].names) or "no name"
            if name_status == "pass":
                name_detail = "The names agree under the bank's name rules."
            elif name_status == "contradiction":
                name_detail = "The verified legal name agrees with none of the hit's names."
            else:
                name_detail = "Not enough of a name to confirm or to clear."
            check("Name", name_status, names, hit_names, name_detail)

            dob_status = "unknown"
            if any(item["dob_mismatch"] for item in compared) and not any(item["dob_agrees"] for item in compared):
                dob_status = "contradiction"
            elif any(item["dob_agrees"] for item in compared) and not any(item["dob_mismatch"] for item in compared):
                dob_status = "pass"
            elif any(item["dob_agrees"] for item in compared) and any(item["dob_mismatch"] for item in compared):
                dob_status = "unknown"
            dob_customer = values(
                lambda party, item: party.dob.label() if party.dob else "no date of birth"
            )
            hit_dob = self._hit_birth_label(compared[0]["facts"]) if compared[0]["facts"].births else "no date of birth"
            origin = compared[0]["facts"].birth_origin
            if dob_status == "contradiction":
                dob_detail = "Every date the hit gives is contradicted."
            elif dob_status == "pass":
                dob_detail = "A date of birth agrees, including a year on its own with a full date."
            else:
                dob_detail = "The dates cannot be compared, or they are only a year apart."
            if origin:
                dob_detail += f" Hit date source: {origin}."
            check("Date of birth", dob_status, dob_customer, hit_dob, dob_detail)

            time_status = "unknown"
            time_detail = "The hit has no dated report in the form the policy counts, and no date of death before screening."
            if any(item["deceased"] or item["temporal"] for item in compared):
                time_status = "contradiction"
                time_detail = "The customer could not have been the listed person at the date in the profile."
            elif compared[0]["facts"].reports and any(party.dob is not None for party in parties):
                time_status = "pass"
                time_detail = "Dated reports exist, and the customer is old enough (or the company already existed)."
            check(
                "Time",
                time_status,
                values(lambda party, item: party.dob.label() if party.dob else "no date"),
                ", ".join(item.label() for item in compared[0]["facts"].reports) or (
                    compared[0]["facts"].death.label() if compared[0]["facts"].death else "no dated report"
                ),
                time_detail,
            )

            if any(item["location_mismatch"] for item in compared) and not any(item["location_agree"] for item in compared):
                loc_status = "contradiction"
                loc_detail = "Every country on the customer differs from every country on the hit."
                if any(item["name_agrees"] and item["dob_agrees"] for item in compared):
                    loc_detail += " Name and date of birth agree, so this difference does not clear the hit."
            elif any(item["location_agree"] for item in compared):
                loc_status = "pass"
                loc_detail = "At least one country is shared."
            else:
                loc_status = "unknown"
                loc_detail = "One side has no country, so location cannot confirm or clear."
            check(
                "Location",
                loc_status,
                values(lambda party, item: joined(party.countries) or "no country"),
                joined(compared[0]["facts"].countries) or "no country",
                loc_detail,
            )

            if any(item["type_mismatch"] for item in compared):
                type_status, type_detail = "contradiction", "One side is a person and the other is a company."
            else:
                type_status, type_detail = "pass", "Both sides are the same kind of entity."
            check(
                "Type",
                type_status,
                values(lambda party, item: item["customer_kind"]),
                compared[0]["hit_kind"],
                type_detail,
            )

        verdict = {
            "DIFFERENT_ENTITY": "Cleared",
            "SAME_ENTITY": "Same person",
            "REVIEW": "Review",
        }.get(outcome.decision, outcome.decision)
        steps.append({
            "title": "Verdict",
            "status": "contradiction" if outcome.decision == "DIFFERENT_ENTITY" else (
                "pass" if outcome.decision == "SAME_ENTITY" else "unknown"
            ),
            "detail": f"{verdict}. {outcome.reason_code}. {outcome.reason}",
            "customer": outcome.customer_record_id or "no customer record",
            "hit": hit.get("name") or "",
        })
        return steps

    def explain_hit(self, hit_id: str) -> dict:
        hit = self.hits.get(hit_id)
        if hit is None:
            return {"error": f"Unknown hit {hit_id}"}
        alert = self.alerts.get(hit["alert_id"])
        if alert is None:
            outcome = Outcome("REVIEW", "INSUFFICIENT_DATA", "This hit has no alert.", [hit_id], None)
            parties = []
        else:
            parties = self.resolve(alert)
            outcome = self.decide_hit(alert, hit)
        return self._explain_payload(alert or {"customer": {}}, hit, parties, outcome)

    def explain_entered(self, payload: dict) -> dict:
        """A customer and a list profile typed into the form, judged by the same rules."""
        customer_name = (payload.get("customer_name") or "").strip()
        customer_type = payload.get("customer_type") or "Individual"
        customer_dob = parse_when(payload.get("customer_dob"))
        customer_countries = set()
        for part in str(payload.get("customer_countries") or "").replace(";", ",").split(","):
            country = canon(part.strip())
            if country:
                customer_countries.add(country)
        hit_name = (payload.get("hit_name") or "").strip() or "Unknown"
        aliases = [
            part.strip()
            for part in str(payload.get("hit_aliases") or "").replace(";", ",").split(",")
            if part.strip()
        ]
        hit_dobs = [
            part.strip()
            for part in str(payload.get("hit_dobs") or "").replace(";", ",").split(",")
            if part.strip()
        ]
        hit_countries = []
        for part in str(payload.get("hit_countries") or "").replace(";", ",").split(","):
            country = canon(part.strip())
            if country:
                hit_countries.append(country)
        hit_type = payload.get("hit_type") or "Individual"
        company = is_company(customer_type)
        party = Party(
            record_ids=["ENTERED"],
            seed_id="ENTERED",
            company=company,
            identity=Identity(
                source_id="entered",
                name=customer_name or None,
                dob=customer_dob,
                document_country=next(iter(sorted(customer_countries)), None),
                when="",
            ),
            names=[(customer_name, "entered")] if customer_name else [],
            verified_name=bool(customer_name),
            dob=customer_dob,
            dob_source_id="entered" if customer_dob else None,
            countries=customer_countries,
            country_sources={country: {"entered"} for country in customer_countries},
        )
        alert = {
            "_entered": True,
            "alert_id": "ENTERED",
            "screened_at": "2026-11-21",
            "customer": {
                "name": customer_name,
                "entity_type": "Company" if company else "Individual",
                "dob": payload.get("customer_dob") or None,
                "country": next(iter(sorted(customer_countries)), None),
                "city": None,
            },
            "account_ref": None,
            "login_handle": None,
            "email": None,
            "hit_ids": ["ENTERED"],
        }
        hit = {
            "hit_id": "ENTERED",
            "alert_id": "ENTERED",
            "entity_type": "Company" if is_company(hit_type) else "Individual",
            "name": hit_name,
            "aliases": aliases,
            "dobs": hit_dobs,
            "countries": hit_countries,
            "nationality": [],
            "citizenship": [],
            "addresses": [],
            "place_of_birth": None,
            "categories": [payload.get("list_type")] if payload.get("list_type") else [],
            "primary_category": payload.get("list_type") or None,
            "profile_notes": None,
            "deceased_date": None,
            "best_matching_name": None,
            "first_name": None,
            "middle_name": None,
            "last_name": None,
        }
        outcome = self.judge(alert, hit, party)
        return self._explain_payload(alert, hit, [party], outcome)

    def _explain_payload(self, alert, hit, parties, outcome: Outcome) -> dict:
        return {
            "hit_id": hit.get("hit_id"),
            "alert_id": hit.get("alert_id"),
            "decision": outcome.decision,
            "reason_code": outcome.reason_code,
            "customer_record_id": outcome.customer_record_id,
            "reason": outcome.reason,
            "evidence": outcome.evidence,
            "steps": self._steps(alert, hit, parties, outcome),
            "hit_profile": {
                "name": hit.get("name") or "",
                "aliases": hit.get("aliases") or [],
                "dobs": hit.get("dobs") or [],
                "countries": sorted(hit_countries(hit)),
                "entity_type": hit.get("entity_type") or "",
                "categories": hit.get("categories") or [],
            },
        }

    def decide_hit(self, alert: dict, hit: dict) -> Outcome:
        parties = self.resolve(alert)
        if not parties:
            return Outcome(
                decision="REVIEW",
                reason_code="INSUFFICIENT_DATA",
                reason=(
                    f"No customer record could be resolved for the screened name "
                    f"{alert['customer'].get('name')}, so the hit {hit.get('name')} "
                    f"cannot be confirmed or cleared."
                ),
                evidence=[alert["alert_id"]],
                customer_record_id=None,
            )
        outcomes = [self.judge(alert, hit, party) for party in parties]
        if len(outcomes) == 1:
            return outcomes[0]
        decisions = {outcome.decision for outcome in outcomes}
        evidence: set[str] = set()
        for outcome in outcomes:
            evidence.update(outcome.evidence)
        record_ids = ", ".join(party.seed_id for party in parties)
        if len(decisions) == 1:
            chosen = outcomes[0]
            return Outcome(
                decision=chosen.decision,
                reason_code=chosen.reason_code,
                reason=chosen.reason,
                evidence=sorted(evidence, key=_evidence_key),
                customer_record_id="AMBIGUOUS",
            )
        summary = "; ".join(
            f"{party.seed_id} would be {outcome.decision}"
            for party, outcome in zip(parties, outcomes)
        )
        return Outcome(
            decision="REVIEW",
            reason_code="AMBIGUOUS_CUSTOMER_RECORD",
            reason=(
                f"More than one customer fits the screened details ({record_ids}) "
                f"and they do not lead to the same decision: {summary}. "
                f"The hit stays in review."
            ),
            evidence=sorted(evidence, key=_evidence_key),
            customer_record_id="AMBIGUOUS",
        )

    def decide_all(self) -> list[dict]:
        rows = []
        for hit in self.hits.values():
            alert = self.alerts.get(hit["alert_id"])
            if alert is None:
                outcome = Outcome(
                    "REVIEW",
                    "INSUFFICIENT_DATA",
                    f"Hit {hit['hit_id']} has no alert in the data folder, so it stays in review.",
                    [hit["hit_id"]],
                    None,
                )
            else:
                outcome = self.decide_hit(alert, hit)
            rows.append(
                {
                    "hit_id": hit["hit_id"],
                    "alert_id": hit["alert_id"],
                    "decision": outcome.decision,
                    "reason_code": outcome.reason_code,
                    "customer_record_id": outcome.customer_record_id,
                    "reason": outcome.reason,
                    "evidence": outcome.evidence,
                }
            )
        rows.sort(key=lambda row: row["hit_id"])
        return rows


def _evidence_key(item: str) -> tuple:
    prefix = item.split("-", 1)[0]
    order = {"CUS": 0, "KYC": 1, "NOTE": 2, "SR": 3, "ACC": 4, "APP": 5, "A": 6, "H": 7}
    return (order.get(prefix, 9), item)


def write_output(rows: list[dict], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3:
        print("usage: system/run <data folder or sheet.xlsx> <output file>", file=sys.stderr)
        sys.exit(2)
    source, output = argv[1], argv[2]
    folder = source
    if os.path.isfile(source) and source.lower().endswith((".xlsx", ".xlsm")):
        import tempfile

        from .convert import convert

        folder = tempfile.mkdtemp(prefix="screening-")
        summary = convert(source, folder)
        print(
            f"Converted {source}: {summary['alerts']} alerts, {summary['hits']} hits.",
            file=sys.stderr,
        )
    world = World(folder)
    rows = world.decide_all()
    write_output(rows, output)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["decision"]] += 1
    print(
        f"Wrote {len(rows)} decisions to {output} "
        f"({counts['DIFFERENT_ENTITY']} cleared, {counts['SAME_ENTITY']} matches, {counts['REVIEW']} review)."
    )


if __name__ == "__main__":
    main()
