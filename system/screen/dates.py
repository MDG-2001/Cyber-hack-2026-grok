"""Parse the dates the bank and the screening provider actually write."""

from __future__ import annotations

import re
from dataclasses import dataclass

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
MONTH_PATTERN = "|".join(MONTHS)


@dataclass(frozen=True)
class When:
    """A date, or an age-derived pair of possible birth years.

    `years` holds every year that is still possible. A listed year is one
    entry. "Aged 45 in 2019" is (1973, 1974). Month and day are set only
    when the source actually gave them.
    """

    years: tuple[int, ...]
    month: int | None = None
    day: int | None = None
    origin: str = "listed"  # listed, notes, search, customer

    @property
    def year(self) -> int:
        return self.years[0]

    def label(self) -> str:
        if len(self.years) == 2 and self.month is None:
            lo, hi = sorted(self.years)
            return f"{lo} or {hi}"
        year = self.years[0]
        if self.month and self.day:
            return f"{year:04d}-{self.month:02d}-{self.day:02d}"
        if self.month:
            return f"{year:04d}-{self.month:02d}"
        return f"{year:04d}"


def _when(year: int, month: int | None, day: int | None, origin: str = "listed") -> When | None:
    if year < 1850 or year > 2100:
        return None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    return When(years=(year,), month=month, day=day, origin=origin)


def parse_when(value, origin: str = "listed") -> When | None:
    """Parse one date field. Day/month numbers are day-first, matching the bank."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return _when(int(match.group(1)), int(match.group(2)), int(match.group(3)), origin)
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return _when(int(match.group(1)), int(match.group(2)), None, origin)
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        return _when(int(match.group(1)), None, None, origin)
    match = re.fullmatch(rf"({MONTH_PATTERN})\s+(\d{{1,2}}),\s*(\d{{4}})", text, re.I)
    if match:
        return _when(int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2)), origin)
    match = re.fullmatch(rf"(\d{{1,2}})\s+({MONTH_PATTERN})\s+(\d{{4}})", text, re.I)
    if match:
        return _when(int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1)), origin)
    match = re.fullmatch(rf"({MONTH_PATTERN})\s+(\d{{4}})", text, re.I)
    if match:
        return _when(int(match.group(2)), MONTHS[match.group(1).lower()], None, origin)
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        # The bank writes day/month/year. Values above 12 in the first slot confirm it.
        return _when(int(match.group(3)), int(match.group(2)), int(match.group(1)), origin)
    return None


_DATE_TOKEN = re.compile(
    rf"(\d{{4}}-\d{{2}}-\d{{2}})"
    rf"|(\d{{1,2}}/\d{{1,2}}/\d{{4}})"
    rf"|((?:{MONTH_PATTERN})\s+\d{{1,2}},\s*\d{{4}})"
    rf"|(\d{{1,2}}\s+(?:{MONTH_PATTERN})\s+\d{{4}})"
    rf"|((?:{MONTH_PATTERN})\s+\d{{4}})",
    re.I,
)


def find_dates(text: str) -> list[tuple[int, When]]:
    found = []
    for match in _DATE_TOKEN.finditer(text or ""):
        parsed = parse_when(match.group(0))
        if parsed is not None:
            found.append((match.start(), parsed))
    return found


def full_date(when: When | None) -> bool:
    return bool(when and when.month and when.day and len(when.years) == 1)


def iso(when: When) -> str:
    return when.label()


def max_age(born: When, on: When) -> int:
    """Oldest age `born` could be on `on`. Partial dates assume the earliest day."""
    month = born.month or 1
    day = born.day or 1
    age = on.year - born.year
    if (on.month or 1, on.day or 1) < (month, day):
        age -= 1
    return age


def certainly_before(earlier: When, later: When) -> bool:
    """True only when `earlier` is before `later` at the precision both give."""
    if earlier.year != later.year:
        return earlier.year < later.year
    if earlier.month is None or later.month is None:
        return False
    if earlier.month != later.month:
        return earlier.month < later.month
    if earlier.day is None or later.day is None:
        return False
    return earlier.day < later.day


def certainly_after(later: When, earlier: When) -> bool:
    return certainly_before(earlier, later)


def overlaps_screen(screened: When | None, record: When | None) -> bool:
    """Finding a customer: every part both dates give is equal. Missing is not a conflict."""
    if screened is None:
        return True
    if record is None:
        return False
    if screened.year not in record.years and record.year not in screened.years:
        # Both are single-year dates in this use.
        if screened.year != record.year:
            return False
    if screened.month and record.month and screened.month != record.month:
        return False
    if screened.day and record.day and screened.day != record.day:
        return False
    return screened.year == record.year or screened.year in record.years


def classify_pair(customer: When, hit: When) -> str:
    """Return agree, contradict, or neither for one customer date against one hit date."""
    distance = min(abs(customer.year - year) for year in hit.years)
    same_year = customer.year in hit.years
    month_conflict = False
    if same_year and customer.month and hit.month and customer.month != hit.month:
        month_conflict = True
    if (
        same_year
        and customer.month
        and hit.month
        and customer.month == hit.month
        and customer.day
        and hit.day
        and customer.day != hit.day
    ):
        month_conflict = True
    if distance > 1 or month_conflict:
        return "contradict"
    if same_year:
        return "agree"
    return "neither"


def compare_births(customer: When | None, hits: list[When]) -> tuple[bool, bool]:
    """(agrees, every hit date contradicted). One agreeing hit date blocks a mismatch."""
    if customer is None or not hits:
        return False, False
    flags = [classify_pair(customer, hit) for hit in hits]
    if any(flag == "agree" for flag in flags):
        return True, False
    if flags and all(flag == "contradict" for flag in flags):
        return False, True
    return False, False
