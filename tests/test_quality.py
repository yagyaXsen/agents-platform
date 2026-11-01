"""Answer-quality evaluation on the two demo businesses. Standard library only:

    python -m unittest tests.test_quality -v      # pass/fail per question
    python -m tests.test_quality                  # printed report with scores

Each case is a question a real customer might type, plus words the answer must
contain. Out-of-scope cases must stay below the confidence threshold, so the
agent says it isn't sure instead of guessing. Answers come from the offline,
extractive path (no LLM), so this measures retrieval and answer selection.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from app.core.chunking import chunk_text
from app.core.retrieval import Passage, extract_answer, retrieve

SEED = Path(__file__).resolve().parent.parent / "app" / "seed_data"
MIN_CONFIDENCE = 0.5  # same default as services.agent_config

# (question, words that must all appear in the answer). None = must NOT be answered.
CLINIC = [
    ("How much is teeth whitening?", ["6,500"]),
    ("What does a check-up cost?", ["800"]),
    ("How long does a cleaning take?", ["40 minutes"]),
    ("Price of a filling?", ["1,500"]),
    ("How much does a root canal cost?", ["6,000"]),
    ("Do you do braces?", ["consultation"]),
    ("How much are clear aligners?", ["consultation"]),
    ("Is the consultation free?", ["free"]),
    ("What are your timings?", ["10:00", "18:00"]),
    ("Are you open on Sunday?", ["closed"]),
    ("Where is the clinic?", ["Galaxy Plaza"]),
    ("Is there parking?", ["basement"]),
    ("Do you accept UPI?", ["UPI"]),
    ("Do you take cashless insurance?", ["cashless"]),
    ("What is the cancellation policy?", ["4 hours"]),
    ("Is there a cancellation fee?", ["no cancellation fee"]),
    ("Who are the dentists?", ["Meera", "Arjun"]),
    ("Who does implants?", ["Arjun"]),
    ("How many whitening sessions do I need?", ["one or two"]),
    ("What should I do in a dental emergency at night?", ["emergency"]),
    # out of scope: must not be answered with false confidence
    ("Do you sell car insurance?", None),
    ("What is the capital of France?", None),
    ("Can I adopt a puppy here?", None),
]

SKYLINE = [
    ("Which areas do you cover?", ["Golf Course Road"]),
    ("Do you have properties in Saket?", ["Saket"]),
    ("What kind of homes do you list?", ["BHK"]),
    ("Do you have villas?", ["villas"]),
    ("How much is a 2 BHK rent on Sohna Road?", ["30,000"]),
    ("Price of a 3 BHK on Golf Course Road?", ["2.5 crore"]),
    ("Builder floor rent in South Delhi?", ["55,000"]),
    ("What is your brokerage for rentals?", ["15 days"]),
    ("What is the brokerage fee for buying?", ["1%"]),
    ("Are site visits free?", ["free"]),
    ("When can I do a site visit?", ["10:00"]),
    ("Does an agent come with me on the visit?", ["agent accompanies"]),
    ("What documents do I need to rent?", ["photo ID"]),
    ("How much security deposit?", ["two months"]),
    ("Where is your office?", ["Cyber Park"]),
    ("What are your office hours?", ["10:00", "18:00"]),
    ("Do I need passport photos?", ["passport photos"]),
    ("Where are your homes?", ["Gurugram"]),
    ("Do prices change?", ["change often"]),
    ("Do you cover DLF Phase 3?", ["DLF Phase"]),
    # out of scope
    ("Do you sell cars?", None),
    ("What is the weather today?", None),
    ("Can you fix my teeth?", None),
]

# Known gaps of word-based retrieval: the question's key word never appears in
# the document ("work" vs "cover", "fixed" vs "change often"). Today the agent
# says it isn't sure (safe, never a wrong answer). Dense embeddings (roadmap)
# should close these; when one starts passing, move it into the suite above.
KNOWN_GAPS = {
    "Skyline Homes (realestate.md)": [
        ("Do you work in Gurugram?", ["Gurugram"]),
        ("Are prices fixed?", ["change often"]),
    ],
}

SUITES = {"Smile Dental (clinic.md)": ("clinic.md", "Clinic guide", CLINIC),
          "Skyline Homes (realestate.md)": ("realestate.md", "Skyline Homes guide", SKYLINE)}


def passages(filename: str, title: str) -> list[Passage]:
    text = (SEED / filename).read_text()
    return [Passage(i, title, c) for i, c in enumerate(chunk_text(text))]


def ask(question: str, ps: list[Passage]) -> tuple[str, float]:
    """Same path as services.TenantContext.answer without an LLM."""
    found = retrieve(question, ps, k=3)
    if not found.hits or found.confidence < MIN_CONFIDENCE:
        return "", found.confidence
    return extract_answer(question, found)[0], found.confidence


def check(question: str, expect: list[str] | None, ps: list[Passage]) -> tuple[bool, str, float]:
    answer, conf = ask(question, ps)
    if expect is None:
        return answer == "", answer, conf
    low = answer.lower()
    return bool(answer) and all(w.lower() in low for w in expect), answer, conf


class AnswerQuality(unittest.TestCase):
    def _run(self, suite: str) -> None:
        filename, title, cases = SUITES[suite]
        ps = passages(filename, title)
        for question, expect in cases:
            with self.subTest(question=question):
                ok, answer, conf = check(question, expect, ps)
                self.assertTrue(ok, f"confidence={conf:.2f} answer={answer!r} expected={expect}")

    def test_clinic(self):
        self._run("Smile Dental (clinic.md)")

    def test_skyline(self):
        self._run("Skyline Homes (realestate.md)")

    def test_known_gaps_are_never_answered_wrongly(self):
        for suite, cases in KNOWN_GAPS.items():
            filename, title, _ = SUITES[suite]
            ps = passages(filename, title)
            for question, expect in cases:
                with self.subTest(question=question):
                    ok, answer, conf = check(question, expect, ps)
                    self.assertTrue(ok or answer == "", f"wrong answer with confidence={conf:.2f}: {answer!r}")


def report() -> int:
    failed = 0
    for suite, (filename, title, cases) in SUITES.items():
        ps = passages(filename, title)
        results = [(q, e, *check(q, e, ps)) for q, e in cases]
        answerable = [r for r in results if r[1] is not None]
        oos = [r for r in results if r[1] is None]
        print(f"\n{suite}")
        print(f"  answered correctly:     {sum(r[2] for r in answerable)}/{len(answerable)}")
        print(f"  out-of-scope refused:   {sum(r[2] for r in oos)}/{len(oos)}")
        for q, e, ok, answer, conf in results:
            if not ok:
                failed += 1
                print(f"  FAIL  {q!r} (confidence {conf:.2f}) -> {answer!r}, expected {e}")
        for q, e in KNOWN_GAPS.get(suite, []):
            ok, answer, conf = check(q, e, ps)
            outcome = "now answered" if ok else ("not sure (safe)" if not answer else "WRONG ANSWER")
            failed += outcome == "WRONG ANSWER"
            print(f"  known gap  {q!r} -> {outcome}")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if report() else 0)
