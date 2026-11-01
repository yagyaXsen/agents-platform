"""Glue between the pure agent logic (app.core) and the database.

`handle_customer_message()` is the single entry point for every channel:
web chat and WhatsApp both call it, so behaviour is identical everywhere.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import settings
from .core import llm, pii
from .core.chunking import chunk_text
from .core.dialogue import AgentConfig, Answer, Question, SlotOption, step
from .core.retrieval import Passage, extract_answer, retrieve
from .core.slots import Hours, label, not_before, upcoming
from .models import Booking, Chunk, Conversation, Document, Event, Lead, Message, Tenant, utcnow
from .security import generate_keys, hash_key

log = logging.getLogger("agentgrid")

LLM = llm.LLMConfig(settings.llm_base_url, settings.llm_api_key, settings.llm_model)

DEFAULT_QUESTIONS = [
    {"key": "name", "prompt": "May I have your name?", "kind": "name"},
    {"key": "need", "prompt": "What would you like help with?"},
    {"key": "timeline", "prompt": "When would you like to come in?"},
    {"key": "contact", "prompt": "What's the best phone number or email to reach you?", "kind": "contact"},
]


# Starting points for a new business. Everything can be changed later in settings.
TEMPLATES = {
    "general": {
        "label": "General business",
        "booking_offer": "Would you like to book an appointment?",
        "questions": DEFAULT_QUESTIONS,
    },
    "clinic": {
        "label": "Clinic or salon",
        "booking_offer": "Would you like to book a visit?",
        "questions": [
            {"key": "name", "prompt": "May I have your name?", "kind": "name"},
            {"key": "need", "prompt": "What would you like help with?"},
            {"key": "timeline", "prompt": "When would you like to come in?"},
            {"key": "contact", "prompt": "What's the best phone number or email to confirm your appointment?", "kind": "contact"},
        ],
    },
    "real_estate": {
        "label": "Real estate",
        "booking_offer": "Would you like to book a site visit?",
        "questions": [
            {"key": "name", "prompt": "May I have your name?", "kind": "name"},
            {"key": "need", "prompt": "Are you looking to buy or rent, and what type of home?"},
            {"key": "location", "prompt": "Which areas are you interested in?"},
            {"key": "budget", "prompt": "What's your budget range?"},
            {"key": "timeline", "prompt": "When are you planning to move or buy?"},
            {"key": "contact", "prompt": "What's the best phone number or email to reach you?", "kind": "contact"},
        ],
    },
}


# ---------------------------------------------------------------- config

def agent_config(tenant: Tenant) -> AgentConfig:
    s = tenant.settings or {}
    qs = s.get("questions") or DEFAULT_QUESTIONS
    return AgentConfig(
        business_name=tenant.name,
        greeting=s.get("greeting") or f"Hi! I'm the {tenant.name} assistant. How can I help you today?",
        questions=tuple(Question(q["key"], q["prompt"], q.get("kind", "text")) for q in qs),
        booking_enabled=bool(s.get("booking_enabled", True)),
        booking_offer=s.get("booking_offer") or "Would you like to book an appointment?",
        min_confidence=float(s.get("min_confidence", 0.5)),
    )


def tenant_hours(tenant: Tenant) -> Hours:
    h = (tenant.settings or {}).get("hours") or {}
    return Hours(
        timezone=h.get("timezone", "Asia/Kolkata"),
        open_hour=int(h.get("open_hour", 10)),
        close_hour=int(h.get("close_hour", 18)),
        slot_minutes=int(h.get("slot_minutes", 60)),
        workdays=tuple(h.get("workdays", (0, 1, 2, 3, 4, 5))),
    )


# ---------------------------------------------------------------- context for the engine

class TenantContext:
    """What the dialogue engine may ask of the outside world, scoped to one tenant."""

    def __init__(self, db: Session, tenant: Tenant):
        self.db, self.tenant = db, tenant

    def answer(self, question: str) -> Answer:
        rows = self.db.execute(
            select(Chunk.id, Document.title, Chunk.text)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.tenant_id == self.tenant.id)
        ).all()
        passages = [Passage(r.id, r.title, r.text) for r in rows]
        found = retrieve(question, passages, k=3)
        if not found.hits:
            return Answer("", 0.0)

        sources = tuple(dict.fromkeys(h.passage.title for h in found.hits[:2]))
        if found.confidence >= agent_config(self.tenant).min_confidence and self._llm_allowed():
            try:
                text, used = llm.grounded_answer(
                    LLM, self.tenant.name, question, [(h.passage.title, h.passage.text) for h in found.hits]
                )
                self.tenant.tokens_used += used
                if text is None:  # the model found no answer in the documents
                    return Answer("", 0.0)
                return Answer(text, found.confidence, sources)
            except llm.LLMError as e:
                log.warning("LLM failed, using extractive answer: %s", e)

        text, titles = extract_answer(question, found)
        return Answer(text, found.confidence, titles)

    def _llm_allowed(self) -> bool:
        return LLM.enabled and self.tenant.tokens_used < self.tenant.token_quota

    def slots(self, timeline: str | None = None) -> list[SlotOption]:
        now = datetime.now(timezone.utc)
        hours = tenant_hours(self.tenant)
        start = not_before(timeline, now.astimezone(ZoneInfo(hours.timezone)).date())
        taken_rows = self.db.scalars(
            select(Booking.start_at).where(
                Booking.tenant_id == self.tenant.id,
                Booking.status == "confirmed",
                Booking.start_at >= now.replace(tzinfo=None),
            )
        ).all()
        taken = {t.replace(tzinfo=timezone.utc) for t in taken_rows}
        found = upcoming(now, hours, taken, start=start)
        if not found and start:  # nothing on the asked day(s): fall back to the soonest free slots
            found = upcoming(now, hours, taken)
        return [SlotOption(d.isoformat(), label(d)) for d in found]


# ---------------------------------------------------------------- persistence helpers

def record_event(db: Session, tenant: Tenant, type_: str, conversation_id: str | None = None, **data) -> None:
    safe = {k: pii.redact(v) if isinstance(v, str) else v for k, v in data.items()}
    db.add(Event(tenant_id=tenant.id, conversation_id=conversation_id, type=type_, data=safe))


def add_message(db: Session, conv: Conversation, role: str, text: str, intent: str | None = None) -> Message:
    msg = Message(tenant_id=conv.tenant_id, conversation_id=conv.id, role=role, text=text, intent=intent)
    db.add(msg)
    conv.updated_at = utcnow()
    return msg


def start_conversation(db: Session, tenant: Tenant, channel: str, contact: str | None = None) -> Conversation:
    conv = Conversation(tenant_id=tenant.id, channel=channel, state={})
    if contact:
        conv.contact_fp = pii.fingerprint(contact)
        conv.contact_hint = pii.mask_phone(contact)
    db.add(conv)
    db.flush()
    record_event(db, tenant, "conversation_started", conv.id, channel=channel)
    return conv


def _upsert_lead(db: Session, tenant: Tenant, conv: Conversation, data: dict, score: int, status: str) -> Lead:
    lead = db.scalar(select(Lead).where(Lead.tenant_id == tenant.id, Lead.conversation_id == conv.id))
    if lead is None:
        lead = Lead(tenant_id=tenant.id, conversation_id=conv.id, data=dict(data), score=score, status=status)
        db.add(lead)
    else:
        lead.data = {**(lead.data or {}), **data}
        lead.score = max(lead.score, score)
        if lead.status != "booked":
            lead.status = status
    db.flush()
    return lead


# ---------------------------------------------------------------- the main entry point

def handle_customer_message(db: Session, tenant: Tenant, conv: Conversation, text: str) -> list[str]:
    """Store the customer's message, run the agent, apply its actions, store replies."""
    text = (text or "").strip()[:2000]
    if not text:
        return []
    add_message(db, conv, "user", text)

    if conv.status in ("handover", "closed"):
        if conv.status == "closed":  # a new message reopens a closed chat with the bot
            conv.status, conv.state = "bot", {}
        else:
            record_event(db, tenant, "message_while_handover", conv.id)
            db.commit()
            return []

    ctx = TenantContext(db, tenant)
    result = step(agent_config(tenant), dict(conv.state or {}), text, ctx)
    replies = list(result.replies)
    state = dict(result.state)

    for action in result.actions:
        if action.type == "save_lead":
            status = "needs_follow_up" if action.data.get("note") == "needs_other_time" else "qualified"
            _upsert_lead(db, tenant, conv, action.data["lead"], int(action.data.get("score", 0)), status)
            record_event(db, tenant, "lead_qualified", conv.id, score=action.data.get("score", 0))

        elif action.type == "book":
            start = datetime.fromisoformat(action.data["start"]).astimezone(timezone.utc).replace(tzinfo=None)
            existing = db.scalar(select(Booking).where(Booking.tenant_id == tenant.id, Booking.start_at == start))
            if existing and existing.status == "confirmed":
                options = ctx.slots((state.get("lead") or {}).get("timeline"))
                lines = "\n".join(f"{i}. {o.label}" for i, o in enumerate(options, 1))
                replies = [f"Sorry, that time was just taken. These are still free:\n{lines}\n\nReply with a number."]
                state.update(stage="choosing_slot", offered=[o.iso for o in options],
                             labels=[o.label for o in options], booked=False)
                continue
            lead = _upsert_lead(db, tenant, conv, state.get("lead") or {}, 0, "booked")
            lead.status = "booked"
            if existing:  # the slot was cancelled earlier: one row per slot, so reuse it
                existing.conversation_id, existing.lead_id = conv.id, lead.id
                existing.label, existing.status, existing.created_at = action.data["label"], "confirmed", utcnow()
            else:
                db.add(Booking(tenant_id=tenant.id, conversation_id=conv.id, lead_id=lead.id,
                               start_at=start, label=action.data["label"]))
            record_event(db, tenant, "booking_created", conv.id, label=action.data["label"])

        elif action.type == "handover":
            conv.status = "handover"
            conv.handover_reason = action.data.get("reason", "unknown")
            record_event(db, tenant, "handover", conv.id, reason=conv.handover_reason)

    for i, r in enumerate(replies):
        add_message(db, conv, "bot", r, intent=result.intent if i == 0 else None)
    conv.state = state
    db.commit()
    log.info("tenant=%s conv=%s intent=%s text=%r", tenant.slug, conv.id[:8], result.intent, pii.redact(text)[:120])
    return replies


# ---------------------------------------------------------------- businesses (tenants)

class TenantError(ValueError):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]


def create_tenant(
    db: Session,
    name: str,
    slug: str | None = None,
    template: str = "general",
    greeting: str | None = None,
    booking_enabled: bool = True,
    hours: dict | None = None,
    knowledge: str | None = None,
) -> tuple[Tenant, str]:
    """Create a business with random keys. Returns the tenant and its admin key,
    which is shown once and stored only as a hash."""
    name = name.strip()
    if not name:
        raise TenantError("Name is required")
    if template not in TEMPLATES:
        raise TenantError(f"Unknown template. Use one of: {', '.join(TEMPLATES)}")
    slug = slugify(slug or name)
    if len(slug) < 3:
        raise TenantError("Pick a longer name or slug (at least 3 letters or digits)")
    if db.scalar(select(Tenant.id).where(Tenant.slug == slug)):
        raise TenantError(f"The slug '{slug}' is taken. Choose another.")
    if (db.scalar(select(func.count()).select_from(Tenant)) or 0) >= settings.max_tenants:
        raise TenantError("This deployment has reached its business limit (MAX_TENANTS).")

    h = {"timezone": "Asia/Kolkata", "open_hour": 10, "close_hour": 18, "slot_minutes": 60, "workdays": [0, 1, 2, 3, 4, 5]}
    h.update({k: v for k, v in (hours or {}).items() if k in h and v is not None})
    try:
        ZoneInfo(h["timezone"])
    except Exception:
        raise TenantError(f"Unknown timezone '{h['timezone']}'") from None
    if not (0 <= int(h["open_hour"]) < int(h["close_hour"]) <= 24):
        raise TenantError("Opening hour must be before closing hour (0 to 24)")

    t = TEMPLATES[template]
    public_key, admin_key = generate_keys(slug)
    tenant = Tenant(
        slug=slug,
        name=name[:200],
        public_key=public_key,
        admin_key_hash=hash_key(admin_key),
        settings={
            "template": template,
            "greeting": (greeting or "").strip()[:500] or None,
            "booking_enabled": booking_enabled,
            "booking_offer": t["booking_offer"],
            "questions": t["questions"],
            "hours": h,
        },
    )
    db.add(tenant)
    db.flush()
    record_event(db, tenant, "tenant_created", None, template=template)
    db.commit()
    if knowledge and knowledge.strip():
        add_document(db, tenant, "Getting started", knowledge)
    return tenant, admin_key


def rotate_admin_key(db: Session, tenant: Tenant) -> str:
    """Issue a new admin key; the old one stops working immediately."""
    _, admin_key = generate_keys(tenant.slug)
    tenant.admin_key_hash = hash_key(admin_key)
    record_event(db, tenant, "admin_key_rotated", None)
    db.commit()
    return admin_key


# ---------------------------------------------------------------- bookings

def cancel_booking(db: Session, tenant: Tenant, booking: Booking) -> tuple[Conversation | None, str]:
    """Cancel a booking: the slot becomes free again, the lead needs follow-up,
    and the customer's chat gets a note. Returns the conversation and the note."""
    booking.status = "cancelled"
    notice = f"Your booking for {booking.label} has been cancelled by the team. Reply here if you'd like a new time."
    lead = db.get(Lead, booking.lead_id) if booking.lead_id else None
    if lead and lead.tenant_id == tenant.id:
        lead.status = "needs_follow_up"
    conv = db.scalar(select(Conversation).where(Conversation.id == booking.conversation_id, Conversation.tenant_id == tenant.id))
    if conv:
        st = dict(conv.state or {})
        st["booked"] = False  # the agent may offer booking again
        conv.state = st
        add_message(db, conv, "system", notice)
    record_event(db, tenant, "booking_cancelled", booking.conversation_id, label=booking.label)
    db.commit()
    return conv, notice


# ---------------------------------------------------------------- privacy: deletion and retention

def delete_conversations(db: Session, tenant: Tenant, conv_ids: list[str], reason: str) -> int:
    """Delete conversations with their messages, leads and bookings. The audit
    trail keeps one PII-free event per deletion."""
    ids = list(db.scalars(select(Conversation.id).where(Conversation.tenant_id == tenant.id, Conversation.id.in_(conv_ids))))
    if not ids:
        return 0
    for model in (Booking, Lead, Message):
        db.execute(delete(model).where(model.tenant_id == tenant.id, model.conversation_id.in_(ids)))
    db.execute(delete(Conversation).where(Conversation.tenant_id == tenant.id, Conversation.id.in_(ids)))
    for cid in ids:
        record_event(db, tenant, "conversation_deleted", cid, reason=reason)
    db.commit()
    return len(ids)


def _phone_digits(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits  # ignore a country code prefix


def conversations_for_contact(db: Session, tenant: Tenant, phone: str | None, email: str | None) -> list[str]:
    """Conversations linked to a customer's phone number or email: their WhatsApp
    chats and any chat where they left these details as a lead."""
    want_phone, want_email = _phone_digits(phone), (email or "").strip().lower()
    if len(want_phone) < 8 and not want_email:
        return []
    found: set[str] = set()
    if phone and len(want_phone) >= 8:
        wa = db.scalars(select(Conversation).where(Conversation.tenant_id == tenant.id, Conversation.channel == "whatsapp")).all()
        found.update(c.id for c in wa if _phone_digits(((c.state or {}).get("lead") or {}).get("phone")) == want_phone)
    for lead in db.scalars(select(Lead).where(Lead.tenant_id == tenant.id)).all():
        d = lead.data or {}
        if (want_phone and len(want_phone) >= 8 and _phone_digits(d.get("phone")) == want_phone) or \
                (want_email and str(d.get("email", "")).strip().lower() == want_email):
            found.add(lead.conversation_id)
    return sorted(found)


def purge_older_than(db: Session, tenant: Tenant, days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    ids = list(db.scalars(select(Conversation.id).where(Conversation.tenant_id == tenant.id, Conversation.updated_at < cutoff)))
    return delete_conversations(db, tenant, ids, reason=f"retention_{days}d")


# ---------------------------------------------------------------- knowledge base

def add_document(db: Session, tenant: Tenant, title: str, text: str, source: str = "text") -> Document:
    doc = Document(tenant_id=tenant.id, title=title.strip()[:200] or "Untitled", source=source)
    db.add(doc)
    db.flush()
    for i, piece in enumerate(chunk_text(text)):
        db.add(Chunk(tenant_id=tenant.id, document_id=doc.id, position=i, text=piece))
    record_event(db, tenant, "document_added", None, title=doc.title)
    db.commit()
    return doc


def extract_text(filename: str, raw: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return raw.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- analytics

def analytics(db: Session, tenant: Tenant) -> dict:
    def count(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant.id, *where)) or 0

    conversations = count(Conversation)
    handovers = count(Event, Event.type == "handover")
    customer_msgs = count(Message, Message.role == "user")
    by_channel = dict(
        db.execute(
            select(Conversation.channel, func.count()).where(Conversation.tenant_id == tenant.id).group_by(Conversation.channel)
        ).all()
    )
    by_intent = dict(
        db.execute(
            select(Message.intent, func.count())
            .where(Message.tenant_id == tenant.id, Message.role == "bot", Message.intent.is_not(None))
            .group_by(Message.intent)
        ).all()
    )
    leads = count(Lead)
    bookings = count(Booking, Booking.status == "confirmed")
    return {
        "conversations": conversations,
        "customer_messages": customer_msgs,
        "leads": leads,
        "bookings": bookings,
        "handovers": handovers,
        "open_handovers": count(Conversation, Conversation.status == "handover"),
        "lead_conversion_rate": round(leads / conversations, 3) if conversations else 0.0,
        "booking_rate": round(bookings / conversations, 3) if conversations else 0.0,
        "escalation_rate": round(handovers / conversations, 3) if conversations else 0.0,
        "by_channel": by_channel,
        "by_intent": by_intent,
        "llm_tokens_used": tenant.tokens_used,
        "llm_token_quota": tenant.token_quota,
    }
