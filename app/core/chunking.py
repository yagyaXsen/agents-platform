"""Split knowledge documents into overlapping passages for retrieval.

A markdown heading starts a new passage, so one passage covers one topic
(prices, timings, address…) and keeps its heading as context.
"""
from __future__ import annotations

from .text import sentences


def chunk_text(text: str, max_words: int = 110, overlap_words: int = 25) -> list[str]:
    headings = {line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip().startswith("#")}
    sents = sentences(text)
    chunks: list[str] = []
    current: list[str] = []
    count = 0

    for s in sents:
        # new section: close the previous passage (no overlap), unless it holds only headings
        if s in headings and any(c not in headings for c in current):
            chunks.append("\n".join(current))
            current, count = [], 0
        n = len(s.split())
        # A single very long sentence: split it by words
        if n > max_words:
            if current:
                chunks.append("\n".join(current))
                current, count = [], 0
            words = s.split()
            step = max(1, max_words - overlap_words)
            for i in range(0, len(words), step):
                chunks.append(" ".join(words[i : i + max_words]))
            continue

        if count + n > max_words and current:
            chunks.append("\n".join(current))
            # carry the last sentences forward as overlap
            carry: list[str] = []
            carried = 0
            for prev in reversed(current):
                w = len(prev.split())
                if carried + w > overlap_words:
                    break
                carry.insert(0, prev)
                carried += w
            current, count = carry, carried

        current.append(s)
        count += n

    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]
