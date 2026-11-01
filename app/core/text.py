"""Small, dependency-free text helpers: tokenising, light stemming and synonyms.

Kept deliberately simple so retrieval works offline and is easy to reason
about. The production path swaps this for dense embeddings (see docs).
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9ऀ-ॿ]+")

STOPWORDS = frozenset(
    """
    a an the and or but if of to in on at for with by from is are was were be been being am
    it its this that these those i you we they he she me my your our their them us his her
    do does did done can could will would should may might must shall what which who whom
    how when where why there here as not no so than too very just about into over under
    again also any all some more most such only own same other then once both each few
    have has had having get got please want would like need much many tell know let ok okay
    hi hello hey thanks thank yes yeah
    """.split()
)

# Words customers use for the same idea. Mapped before stemming.
SYNONYMS = {
    "cost": "price", "costs": "price", "fee": "price", "fees": "price", "charge": "price",
    "charges": "price", "pricing": "price", "rate": "price", "rates": "price", "prices": "price",
    "rent": "rent", "rental": "rent",
    "timing": "hour", "timings": "hour", "hours": "hour", "open": "hour", "opening": "hour",
    "close": "hour", "closing": "hour",
    "location": "address", "located": "address", "where": "address", "directions": "address",
    "appointment": "book", "appointments": "book", "booking": "book", "schedule": "book",
    "doctor": "dentist", "doctors": "dentist", "dr": "dentist",
    "flat": "apartment", "flats": "apartment", "bhk": "apartment",
    "refund": "cancel", "refunds": "cancel", "cancellation": "cancel",
}


def stem(word: str) -> str:
    """Very light suffix stripping, enough to match plurals and -ing forms."""
    if len(word) > 6 and word.endswith("ings"):  # fillings -> filling -> fill
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text.lower()):
        if w in STOPWORDS:
            continue
        w = SYNONYMS.get(w, w)
        out.append(stem(w))
    return out


# "Dr. Meera" is one sentence, not two
_ABBREV = re.compile(r"(?:^|\s)(?:dr|mr|mrs|ms|prof|st|no|vs|approx|e\.g|i\.e)\.$", re.I)


def _split_line(line: str) -> list[str]:
    parts: list[str] = []
    for p in re.split(r"(?<=[.!?])\s+", line):
        if parts and _ABBREV.search(parts[-1]):
            parts[-1] += " " + p
        else:
            parts.append(p)
    return [p.strip(" -*•\t") for p in parts if p.strip(" -*•\t")]


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):  # markdown heading: keep as its own short sentence
            out.append(line.lstrip("#").strip())
            continue
        out.extend(_split_line(line))
    return out


def is_heading(sentence: str) -> bool:
    """Short line with no sentence punctuation, e.g. 'Brokerage fee'."""
    return not sentence.rstrip().endswith((".", "!", "?", ":")) and len(sentence.split()) <= 6
