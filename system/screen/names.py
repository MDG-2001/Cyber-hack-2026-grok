"""Name agreement, as the screening SOP defines it (principle 8)."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

PLACEHOLDERS = {
    "na",
    "test",
    "unknown",
    "xxx",
    "none",
    "null",
    "nil",
}

_EXTRA = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ð": "d",
        "Ð": "D",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ß": "ss",
        "þ": "th",
        "Þ": "th",
        "ı": "i",
        "İ": "i",
        "å": "a",
        "Å": "A",
        "ń": "n",
        "Ń": "N",
        "ş": "s",
        "Ş": "S",
        "ğ": "g",
        "Ğ": "G",
    }
)


def strip_accents(text: str) -> str:
    text = text.translate(_EXTRA)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def fold_key(text: str) -> str:
    """Loose fold used to line a hit's name up with a search query."""
    text = strip_accents(text or "").lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def key_tokens(text: str) -> frozenset[str]:
    return frozenset(fold_key(text).split())


class NameIndex:
    def __init__(self, groups: list[list[str]], legal_forms: list[str]):
        self.legal_forms = {fold_key(form) for form in legal_forms}
        self._group_of: dict[str, int] = {}
        for index, group in enumerate(groups):
            for form in group:
                self._group_of[fold_key(form)] = index

    def tokens(self, name: str | None, company: bool = False) -> list[str]:
        if not name:
            return []
        text = strip_accents(str(name))
        text = re.sub(r"\([^)]*\)", " ", text)
        text = re.sub(r"\[[^\]]*\]", " ", text)
        text = text.lower().replace("'", "").replace("’", "")
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        parts: list[str] = []
        seen: set[str] = set()
        for part in text.split():
            if len(part) <= 1:
                continue
            if part in PLACEHOLDERS or part.isdigit():
                continue
            if company and part in self.legal_forms:
                continue
            if part in seen:
                continue
            seen.add(part)
            parts.append(part)
        return parts

    def agree(self, left: list[str], right: list[str]) -> bool:
        if not left or not right:
            return False
        pairs = self._matching_pairs(left, right)
        if not pairs:
            return False
        shorter_is_left = len(left) <= len(right)
        need = (1 << len(left)) - 1 if shorter_is_left else (1 << len(right)) - 1
        for index, (mask_left, mask_right) in enumerate(pairs):
            covered = mask_left if shorter_is_left else mask_right
            if covered & need == need:
                return True
            for other_left, other_right in pairs[index + 1 :]:
                if mask_left & other_left or mask_right & other_right:
                    continue
                return True
        return False

    def _matching_pairs(self, left: list[str], right: list[str]) -> list[tuple[int, int]]:
        expanded_left = _expand(left)
        expanded_right = _expand(right)
        pairs = []
        for mask_left, token_left in expanded_left:
            for mask_right, token_right in expanded_right:
                if self.parts_match(token_left, token_right):
                    pairs.append((mask_left, mask_right))
        return pairs

    def parts_match(self, left: str, right: str) -> bool:
        return _parts_match(left, right, self._group_of.get(left, -1), self._group_of.get(right, -1))

    def matched_labels(self, left: list[str], right: list[str]) -> list[str]:
        """Parts that agree, using the same part rules as agree(). Does not decide a hit."""
        labels = []
        used_left = 0
        used_right = 0
        for index, part in enumerate(left):
            for other, candidate in enumerate(right):
                if used_left & (1 << index) or used_right & (1 << other):
                    continue
                if not self.parts_match(part, candidate):
                    continue
                used_left |= 1 << index
                used_right |= 1 << other
                labels.append(part if part == candidate else part + "~" + candidate)
                break
        return labels


def _expand(parts: list[str]) -> list[tuple[int, str]]:
    expanded: list[tuple[int, str]] = []
    count = len(parts)
    for index, part in enumerate(parts):
        expanded.append((1 << index, part))
    for index in range(count - 1):
        mask = (1 << index) | (1 << (index + 1))
        expanded.append((mask, parts[index] + parts[index + 1]))
    for index in range(count - 2):
        mask = (1 << index) | (1 << (index + 1)) | (1 << (index + 2))
        expanded.append((mask, parts[index] + parts[index + 1] + parts[index + 2]))
    return expanded


@lru_cache(maxsize=200_000)
def _parts_match(left: str, right: str, left_group: int, right_group: int) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if left_group != -1 and left_group == right_group:
        return True
    shorter = min(len(left), len(right))
    if shorter < 4:
        return False
    limit = 1 if shorter <= 6 else 2
    return _edit_distance(left, right, limit) <= limit


def _edit_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        best = current[0]
        for j, right_char in enumerate(right, 1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace = previous[j - 1] + (left_char != right_char)
            value = insert if insert < delete else delete
            if replace < value:
                value = replace
            current.append(value)
            if value < best:
                best = value
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]
