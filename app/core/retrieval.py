"""Lexical retrieval (TF-IDF cosine) with an interpretable confidence score.

`rank()` orders passages by TF-IDF cosine similarity. `confidence` is the share
of the query's important words (IDF-weighted) that the best passage contains,
so 0.0 means "nothing relevant" and 1.0 means "every key word is covered".
The dialogue engine hands over to a human when confidence is low.

Upgrade path: replace or blend with dense embeddings (pgvector), i.e. the
hybrid retrieval used in DataPilot. The interface stays the same.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .text import is_heading, sentences, tokens


@dataclass(frozen=True)
class Passage:
    id: Any
    title: str
    text: str


@dataclass(frozen=True)
class Hit:
    passage: Passage
    score: float
    coverage: float = 0.0  # IDF-weighted share of the query's words this passage contains


@dataclass(frozen=True)
class Retrieval:
    hits: list[Hit]
    confidence: float


def _idf(df: Counter, n_docs: int, term: str) -> float:
    return math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0


def retrieve(query: str, passages: list[Passage], k: int = 3) -> Retrieval:
    q_terms = tokens(query)
    if not q_terms or not passages:
        return Retrieval([], 0.0)

    docs = [tokens(f"{p.title} {p.text}") for p in passages]
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))

    def vec(terms: list[str]) -> dict[str, float]:
        tf = Counter(terms)
        return {t: (1 + math.log(c)) * _idf(df, n, t) for t, c in tf.items()}

    qv = vec(q_terms)
    q_norm = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    unique_q = set(q_terms)
    total = sum(_idf(df, n, t) for t in unique_q) or 1.0

    scored: list[Hit] = []
    for p, d in zip(passages, docs):
        dv = vec(d)
        d_norm = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        dot = sum(w * dv.get(t, 0.0) for t, w in qv.items())
        if dot > 0:
            covered = sum(_idf(df, n, t) for t in unique_q if t in dv)
            scored.append(Hit(p, dot / (q_norm * d_norm), round(covered / total, 6)))

    # The passage that covers most of the question wins; similarity breaks ties
    scored.sort(key=lambda h: (h.coverage, h.score), reverse=True)
    top = scored[:k]
    if not top:
        return Retrieval([], 0.0)
    # Confidence: IDF-weighted coverage of query terms by the best passage
    return Retrieval(top, top[0].coverage)


def extract_answer(query: str, found: Retrieval) -> tuple[str, tuple[str, ...]]:
    """Extractive answer and its source titles. Passages that cover the question
    as well as the best one are all searched for the best sentences."""
    if not found.hits:
        return "", ()
    tied = [h for h in found.hits if h.coverage >= found.hits[0].coverage]
    text = "\n".join(h.passage.text for h in tied)
    return best_sentences(query, text), tuple(dict.fromkeys(h.passage.title for h in tied))


def best_sentences(query: str, text: str, limit: int = 2) -> str:
    """Pick the sentences of a passage that best match the query (extractive answer).

    A sentence also counts the words of the heading it sits under, so
    "Which areas do you cover?" finds the sentence below "Areas we cover".
    """
    q = set(tokens(query))
    sents: list[str] = []
    context: list[set[str]] = []  # heading words per sentence
    heading: set[str] = set()
    for s in sentences(text):
        if is_heading(s):
            heading = set(tokens(s))
        else:
            sents.append(s)
            context.append(heading)
    if not sents:
        return text.strip()

    def overlap(i: int) -> tuple[int, int]:
        own = q & set(tokens(sents[i]))
        return len(own | (q & context[i])), len(own)

    ranked = sorted(range(len(sents)), key=lambda i: (*overlap(i), -i), reverse=True)
    best = overlap(ranked[0])[0]
    # keep the runner-up only if it is nearly as relevant as the best sentence
    chosen = sorted(i for i in ranked[:limit] if overlap(i)[0] and overlap(i)[0] >= best - 1)
    picked = [sents[i] for i in chosen] or [sents[ranked[0]]]
    out = " ".join(picked)
    return out if out.endswith((".", "!", "?")) else out + "."
