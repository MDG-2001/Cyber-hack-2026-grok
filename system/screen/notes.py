"""Read identity checks and 'places lived' out of free-text notes.

The wording varies, but every approved identity note names a person in capitals,
a date of birth, and a document country. Residence notes say the customer lived
somewhere. Anything else is support chatter and is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .countries import canon, mentioned
from .dates import When, find_dates, parse_when

# A note describes somewhere the customer lived, not a passing country word.
_LIVED = re.compile(
    r"\b(?:liv(?:e|ed|ing)|resid\w*|household|dwelling|dwell(?:ed|ing)?|home|domicil\w*|settled|inhabitant)\b"
    r"|\bbased in\b|\brelocated\b|\bpersonal life\b|\bplace of residence\b|\bstayed in\b|\bmoved to\b",
    re.I,
)
_DOB_HINT = re.compile(r"\b(born|dob|birth)\b", re.I)
_SKIP_CAPS = {"DOB", "ID", "KYC", "USA", "UK", "UAE", "PEP"}


@dataclass
class ParsedIdentity:
    name: str | None
    dob: When | None
    document_country: str | None


def _caps_sequences(text: str) -> list[list[str]]:
    sequences: list[list[str]] = []
    current: list[str] = []
    for token in re.findall(r"\S+", text):
        core = token.strip(".,;:()\"'“”")
        letters = [char for char in core if char.isalpha()]
        if core.upper() in _SKIP_CAPS or not letters:
            if current:
                sequences.append(current)
                current = []
            continue
        if len(letters) >= 2 and all(char.isupper() for char in letters):
            current.append(core)
        elif current:
            sequences.append(current)
            current = []
    if current:
        sequences.append(current)
    return sequences


def _best_name(text: str) -> str | None:
    sequences = _caps_sequences(text)
    sequences = [seq for seq in sequences if seq]
    if not sequences:
        return None
    sequences.sort(key=lambda seq: (len(seq) >= 2, len(seq)), reverse=True)
    return " ".join(sequences[0])


def _document_country(text: str) -> str | None:
    """Prefer the country attached to the document, not a passing mention."""
    patterns = [
        r"(?:from|issued in|issued by|originating from|ctry)\s+(?:the\s+)?([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,3})",
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:passport|national ID|driving licence|driving license|ID)\b",
        r"\b((?:[A-Z][a-z]+){0,2}[a-z]+)\s+passport\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            country = canon(match.group(1))
            if country:
                return country
    found = mentioned(text)
    if len(found) == 1:
        return next(iter(found))
    return None


def parse_identity(text: str, created_at: str | None = None) -> ParsedIdentity | None:
    if not text:
        return None
    name = _best_name(text)
    dates = find_dates(text)
    check_day = parse_when(created_at) if created_at else None
    dob = None
    for index, when in dates:
        window = text[max(0, index - 40) : index + 5]
        if _DOB_HINT.search(window):
            dob = when
            break
    if dob is None:
        for _index, when in dates:
            if check_day and when.year == check_day.year and when.month == check_day.month and when.day == check_day.day:
                continue
            # A parenthetical or second date is the birth date; the check date matches created_at.
            dob = when
            break
    country = _document_country(text)
    if not name and dob is None:
        return None
    return ParsedIdentity(name=name, dob=dob, document_country=country)


def lived_countries(text: str) -> set[str]:
    if not text or not _LIVED.search(text):
        return set()
    return mentioned(text)
