"""Rule-based dialogue manager: the deterministic half of the agent.

The RFP asks for "deterministic logic plus LLM where needed". This module is
the deterministic part. It decides *what happens next* (answer, qualify,
offer slots, book, hand over) and only calls out to `ctx.answer()` (RAG, and
optionally an LLM) for free-text knowledge questions.

It is pure: `step(config, state, text, ctx)` returns replies, a new state and
a list of actions for the caller to persist. No database, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from . import pii

HANDOVER_PHRASES = (
    "human", "real person", "talk to someone", "speak to someone", "talk to a person",
    "speak to a person", "talk to the team", "speak to the team", "manager",
    "representative", "customer care", "complaint", "call me back", "not helpful",
    "useless", "frustrated", "angry", "terrible",
)
BOOK_WORDS = (
    "book", "appointment", "schedule", "reserve", "consultation", "visit", "slot",
    "meeting", "viewing", "demo", "come in", "site visit",
)
GREETINGS = ("hi", "hello", "hey", "namaste", "good morning", "good afternoon", "good evening", "hii", "helo")
THANKS = ("thanks", "thank you", "thx", "great thanks", "ok thanks", "dhanyavaad", "shukriya")
CANCEL = ("cancel", "stop", "never mind", "nevermind", "forget it", "not now", "later")
YES = ("yes", "yeah", "yep", "sure", "ok", "okay", "please", "haan", "ha", "yes please", "y", "go ahead")
NO = ("no", "nope", "nah", "not now", "no thanks", "nahi")
OTHER_TIME = ("other", "none", "different", "another time", "none of these", "not these")


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    kind: str = "text"  # "text" | "name" | "contact"


@dataclass(frozen=True)
class AgentConfig:
    business_name: str
    greeting: str
    questions: tuple[Question, ...]
    booking_enabled: bool = True
    booking_offer: str = "Would you like to book an appointment?"
    min_confidence: float = 0.5
    max_misses: int = 2  # low-confidence answers in a row before handing over


@dataclass(frozen=True)
class Answer:
    text: str
    confidence: float
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotOption:
    iso: str
    label: str


class Context(Protocol):
    def answer(self, question: str) -> Answer: ...
    def slots(self, timeline: str | None = None) -> list[SlotOption]: ...


@dataclass(frozen=True)
class Action:
    type: str  # "save_lead" | "book" | "handover"
    data: dict = field(default_factory=dict)


@dataclass
class StepResult:
    replies: list[str]
    state: dict
    actions: list[Action]
    intent: str


# ---------------------------------------------------------------- helpers

def _has(low: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", low) for p in phrases)


def _starts(low: str, words: tuple[str, ...]) -> bool:
    first = re.sub(r"[^a-z\s]", "", low).strip()
    return any(first == w or first.startswith(w + " ") for w in words)


def wants_handover(low: str) -> bool:
    return _has(low, HANDOVER_PHRASES)


def wants_booking(low: str) -> bool:
    return _has(low, BOOK_WORDS)


def is_question(text: str) -> bool:
    low = text.lower().strip()
    return low.endswith("?") or bool(
        re.match(r"(what|how|when|where|which|who|why|do you|does|is there|are there|can i|can you|is it)\b", low)
    )


def clean_name(text: str) -> str:
    name = re.sub(r"^(hi|hello|hey)[,!\s]+", "", text.strip(), flags=re.I)
    name = re.sub(r"^(my name is|my name's|i am|i'm|im|this is|it's|its|name is|call me)\s+", "", name, flags=re.I)
    name = re.split(r"[,.;!\n]| and ", name)[0].strip()
    return " ".join(w.capitalize() for w in name.split()[:4])


def lead_score(lead: dict) -> int:
    """0–100. Contact details matter most; urgency and budget add to it."""
    score = 0
    if lead.get("phone") or lead.get("email"):
        score += 40
    if lead.get("need"):
        score += 20
    timeline = str(lead.get("timeline", "")).lower()
    if timeline:
        urgent = ("today", "tomorrow", "this week", "asap", "soon", "urgent", "next week", "now")
        score += 20 if any(u in timeline for u in urgent) else 10
    if re.search(r"\d", str(lead.get("budget", ""))):
        score += 20
    elif lead.get("budget"):
        score += 10
    return min(score, 100)


def new_state() -> dict:
    return {"stage": "idle", "q_index": 0, "lead": {}, "offered": [], "labels": [], "misses": 0, "awaiting": None}


# ---------------------------------------------------------------- engine

def step(cfg: AgentConfig, state: dict | None, text: str, ctx: Context) -> StepResult:
    st = {**new_state(), **(state or {})}
    st["lead"] = dict(st.get("lead") or {})
    t = text.strip()
    low = t.lower()

    if not t:
        return StepResult([], st, [], "empty")

    # A person is handling this conversation: the bot stays quiet.
    if st["stage"] == "handover":
        return StepResult([], st, [], "handover_active")

    if wants_handover(low):
        st.update(stage="handover", awaiting=None)
        return StepResult(
            [f"Of course. I'm connecting you with the {cfg.business_name} team now. Someone will reply here shortly."],
            st,
            [Action("handover", {"reason": "customer_request"})],
            "handover",
        )

    if st["stage"] in ("qualifying", "choosing_slot") and _starts(low, CANCEL):
        st.update(stage="idle", awaiting=None, offered=[], labels=[])
        return StepResult(["No problem. Is there anything else I can help you with?"], st, [], "cancel")

    if st["stage"] == "qualifying":
        return _qualify(cfg, st, t, ctx)

    if st["stage"] == "choosing_slot":
        return _choose_slot(cfg, st, t, ctx)

    # ---- idle
    if st.get("awaiting") == "booking_yes":
        st["awaiting"] = None
        if _starts(low, YES):
            return _start_booking(cfg, st, ctx)
        if _starts(low, NO):
            return StepResult(["Sure. Feel free to ask me anything else."], st, [], "decline_booking")

    if cfg.booking_enabled and wants_booking(low) and not is_question(t):
        return _start_booking(cfg, st, ctx)

    words = re.sub(r"[^a-z\s]", "", low).split()
    if words and len(words) <= 4 and _starts(low, GREETINGS) and not is_question(t):
        return StepResult([cfg.greeting], st, [], "greeting")
    if _starts(low, THANKS) and len(words) <= 5:
        return StepResult(["You're welcome! Anything else I can help with?"], st, [], "thanks")

    return _answer(cfg, st, t, ctx, offer_booking=True)


def _answer(cfg: AgentConfig, st: dict, text: str, ctx: Context, offer_booking: bool) -> StepResult:
    ans = ctx.answer(text)
    if ans.confidence < cfg.min_confidence or not ans.text:
        st["misses"] = int(st.get("misses", 0)) + 1
        if st["misses"] >= cfg.max_misses:
            st.update(stage="handover", awaiting=None)
            return StepResult(
                [f"I don't want to give you a wrong answer, so I'm passing this to the {cfg.business_name} team. "
                 "Someone will reply here shortly."],
                st,
                [Action("handover", {"reason": "low_confidence", "question": text})],
                "handover",
            )
        return StepResult(
            ["I'm not sure about that one. Could you rephrase it? You can also type \"human\" to talk to the team."],
            st, [], "low_confidence",
        )

    st["misses"] = 0
    reply = ans.text
    if ans.sources:
        reply += "\n\nSource: " + ", ".join(dict.fromkeys(ans.sources))
    replies = [reply]
    if offer_booking and cfg.booking_enabled and st["stage"] == "idle" and not st.get("booked"):
        if wants_booking(text.lower()):
            replies.append("Would you like me to book that for you?")
        else:
            replies.append(cfg.booking_offer)
        st["awaiting"] = "booking_yes"
    return StepResult(replies, st, [], "answer")


def _start_booking(cfg: AgentConfig, st: dict, ctx: Context) -> StepResult:
    st.update(stage="qualifying", q_index=0, awaiting=None)
    nxt = _next_question(cfg, st)
    if nxt is None:
        return _finish_qualifying(cfg, st, ctx, intro="Great.")
    st["q_index"] = nxt
    return StepResult([f"Happy to help you book. {cfg.questions[nxt].prompt}"], st, [], "start_booking")


def _next_question(cfg: AgentConfig, st: dict) -> int | None:
    lead = st["lead"]
    for i in range(st.get("q_index", 0), len(cfg.questions)):
        q = cfg.questions[i]
        if q.kind == "contact":
            if not (lead.get("phone") or lead.get("email")):
                return i
        elif not lead.get(q.key):
            return i
    return None


def _qualify(cfg: AgentConfig, st: dict, text: str, ctx: Context) -> StepResult:
    q = cfg.questions[st["q_index"]]

    # A side question in the middle of the flow: answer it, then re-ask.
    if is_question(text) and q.kind != "contact":
        side = _answer(cfg, {**st, "stage": "side"}, text, ctx, offer_booking=False)
        if side.intent == "answer":
            return StepResult(side.replies + [q.prompt], st, [], "side_answer")

    lead = st["lead"]
    if q.kind == "contact":
        email, phone = pii.find_email(text), pii.find_phone(text)
        if not (email or phone):
            return StepResult(
                ["Could you share a phone number or email address so the team can confirm with you?"],
                st, [], "ask_again",
            )
        if email:
            lead["email"] = email
        if phone:
            lead["phone"] = phone
    elif q.kind == "name":
        name = clean_name(text)
        if not name:
            return StepResult([q.prompt], st, [], "ask_again")
        lead[q.key] = name
    else:
        lead[q.key] = text.strip()[:500]

    st["q_index"] += 1
    nxt = _next_question(cfg, st)
    if nxt is not None:
        st["q_index"] = nxt
        prefix = f"Thanks, {lead['name'].split()[0]}. " if q.kind == "name" and lead.get("name") else "Thanks. "
        return StepResult([prefix + cfg.questions[nxt].prompt], st, [], "qualifying")
    return _finish_qualifying(cfg, st, ctx, intro="Thank you.")


def _finish_qualifying(cfg: AgentConfig, st: dict, ctx: Context, intro: str) -> StepResult:
    lead = st["lead"]
    actions = [Action("save_lead", {"lead": dict(lead), "score": lead_score(lead)})]
    if not cfg.booking_enabled:
        st.update(stage="idle")
        return StepResult([f"{intro} The team will be in touch shortly."], st, actions, "lead_saved")

    options = ctx.slots(lead.get("timeline"))
    if not options:
        st.update(stage="idle")
        return StepResult(
            [f"{intro} I couldn't find a free slot in the next few days, so the team will contact you to arrange a time."],
            st, actions, "lead_saved",
        )
    st.update(stage="choosing_slot", offered=[o.iso for o in options], labels=[o.label for o in options])
    lines = "\n".join(f"{i}. {o.label}" for i, o in enumerate(options, 1))
    return StepResult(
        [f"{intro} These times are available:\n{lines}\n\nReply with the number that suits you."],
        st, actions, "offer_slots",
    )


def _choose_slot(cfg: AgentConfig, st: dict, text: str, ctx: Context) -> StepResult:
    from .slots import pick

    low = text.lower()
    if _has(low, OTHER_TIME):
        st.update(stage="idle", offered=[], labels=[])
        return StepResult(
            ["No problem. The team will contact you to find a time that works."],
            st, [Action("save_lead", {"lead": dict(st["lead"]), "score": lead_score(st["lead"]), "note": "needs_other_time"})],
            "other_time",
        )

    idx = pick(text, st["labels"])
    if idx is None:
        if is_question(text):
            side = _answer(cfg, {**st, "stage": "side"}, text, ctx, offer_booking=False)
            if side.intent == "answer":
                return StepResult(side.replies + ["Which of the times above suits you?"], st, [], "side_answer")
        n = len(st["labels"])
        options = ", ".join(str(i) for i in range(1, n + 1))
        return StepResult(
            [f"Please reply with {options}, or type \"other\" and the team will find another time."],
            st, [], "ask_again",
        )

    iso, lab = st["offered"][idx], st["labels"][idx]
    name = (st["lead"].get("name") or "").split(" ")[0]
    st.update(stage="idle", offered=[], labels=[], booked=True, awaiting=None)
    who = f", {name}" if name else ""
    return StepResult(
        [f"Done{who}. You're booked for {lab}. You'll get a confirmation shortly. Is there anything else I can help with?"],
        st,
        [Action("book", {"start": iso, "label": lab})],
        "booked",
    )
