"""AgentGrid PoC — HTTP API, channel webhooks and the demo UI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .channels import whatsapp
from .config import settings
from .core import pii
from .db import get_db, init_db
from .models import Booking, Conversation, Document, Event, Lead, Message, Tenant
from .ratelimit import LIMITS, client_ip, enforce, limiter
from .security import normalize_origin, require_platform_admin, tenant_from_admin_key, tenant_from_public_key

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).resolve().parent / "static"



@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="AgentGrid PoC",
    version="0.1.0",
    description="Multi-tenant AI agents for customer conversations: RAG answers, lead qualification, "
    "booking, human handover and analytics across web chat and WhatsApp.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Agent-Key"],
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- schemas

class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class MessageOut(BaseModel):
    id: int
    role: str
    text: str
    created_at: datetime


class ChatReply(BaseModel):
    conversation_id: str
    replies: list[str]
    status: str
    messages: list[MessageOut]


class DocumentIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=200_000)


class HoursIn(BaseModel):
    timezone: str | None = Field(None, max_length=64)
    open_hour: int | None = Field(None, ge=0, le=23)
    close_hour: int | None = Field(None, ge=1, le=24)
    slot_minutes: int | None = Field(None, ge=15, le=240)


class TenantIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(None, max_length=64)
    template: str = "general"
    greeting: str | None = Field(None, max_length=500)
    booking_enabled: bool = True
    hours: HoursIn | None = None
    knowledge: str | None = Field(None, max_length=200_000)


def _messages(db: Session, conv: Conversation, after_id: int = 0) -> list[MessageOut]:
    rows = db.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.id > after_id).order_by(Message.id)
    ).all()
    return [MessageOut(id=m.id, role=m.role, text=m.text, created_at=m.created_at) for m in rows]


def _deliver(conv: Conversation, text: str) -> tuple[bool, str | None]:
    """Push a team message to the customer. Web chat picks it up on its next poll;
    WhatsApp needs an outbound send through Twilio."""
    if conv.channel != "whatsapp":
        return True, None
    phone = ((conv.state or {}).get("lead") or {}).get("phone")
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_from and phone):
        return False, "WhatsApp sending isn't configured (TWILIO_ACCOUNT_SID, TWILIO_WHATSAPP_FROM)."
    try:
        whatsapp.send_message(settings.twilio_account_sid, settings.twilio_auth_token,
                              settings.twilio_whatsapp_from, phone, text)
    except whatsapp.SendError as e:
        logging.getLogger("agentgrid").warning("WhatsApp send failed: %s", e)
        return False, "Couldn't send on WhatsApp. Try again."
    return True, None


def _conversation(db: Session, tenant: Tenant, conversation_id: str) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == tenant.id))
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/demo", include_in_schema=False)
def demo_site() -> FileResponse:
    return FileResponse(STATIC / "demo.html")


@app.get("/admin", include_in_schema=False)
def admin_console() -> FileResponse:
    return FileResponse(STATIC / "admin.html")


@app.get("/platform", include_in_schema=False)
def platform_console() -> FileResponse:
    return FileResponse(STATIC / "platform.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm": "enabled" if services.LLM.enabled else "extractive-fallback"}


# ---------------------------------------------------------------- public chat API (web widget)

@app.get("/v1/agent")
def agent_info(tenant: Tenant = Depends(tenant_from_public_key)) -> dict:
    cfg = services.agent_config(tenant)
    return {"name": tenant.name, "greeting": cfg.greeting, "theme": (tenant.settings or {}).get("theme", {})}


@app.post("/v1/conversations", response_model=ChatReply)
def create_conversation(request: Request, tenant: Tenant = Depends(tenant_from_public_key), db: Session = Depends(get_db)):
    enforce("public", f"{tenant.id}:{client_ip(request)}")
    conv = services.start_conversation(db, tenant, "web")
    greeting = services.agent_config(tenant).greeting
    services.add_message(db, conv, "bot", greeting, intent="greeting")
    db.commit()
    return ChatReply(conversation_id=conv.id, replies=[greeting], status=conv.status, messages=_messages(db, conv))


@app.post("/v1/conversations/{conversation_id}/messages", response_model=ChatReply)
def send_message(
    conversation_id: str,
    body: MessageIn,
    after_id: int = 0,
    tenant: Tenant = Depends(tenant_from_public_key),
    db: Session = Depends(get_db),
):
    conv = _conversation(db, tenant, conversation_id)
    enforce("public", f"conv:{conv.id}")
    replies = services.handle_customer_message(db, tenant, conv, body.text)
    return ChatReply(conversation_id=conv.id, replies=replies, status=conv.status, messages=_messages(db, conv, after_id))


@app.get("/v1/conversations/{conversation_id}/messages", response_model=ChatReply)
def poll_messages(
    conversation_id: str,
    after_id: int = 0,
    tenant: Tenant = Depends(tenant_from_public_key),
    db: Session = Depends(get_db),
):
    """The widget polls this to receive replies from a human agent after handover."""
    conv = _conversation(db, tenant, conversation_id)
    return ChatReply(conversation_id=conv.id, replies=[], status=conv.status, messages=_messages(db, conv, after_id))


# ---------------------------------------------------------------- WhatsApp (Twilio) webhook

@app.post("/v1/channels/whatsapp/{tenant_slug}", include_in_schema=False)
async def whatsapp_webhook(tenant_slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")

    params = {k: str(v) for k, v in (await request.form()).items()}
    if settings.twilio_auth_token:
        url = f"{settings.public_base_url}{request.url.path}"
        if not whatsapp.verify_signature(settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad signature")

    phone = whatsapp.sender(params)
    if not phone:
        return Response(whatsapp.twiml([]), media_type="application/xml")

    fp = pii.fingerprint(phone)
    if limiter.hit(f"public:wa:{tenant.id}:{fp}", LIMITS["public"]):
        logging.getLogger("agentgrid").warning("WhatsApp rate limit hit for tenant=%s", tenant.slug)
        return Response(whatsapp.twiml([]), media_type="application/xml")  # drop quietly; Twilio needs a 200
    conv = db.scalar(
        select(Conversation)
        .where(Conversation.tenant_id == tenant.id, Conversation.channel == "whatsapp", Conversation.contact_fp == fp)
        .order_by(Conversation.created_at.desc())
    )
    if conv is None:
        conv = services.start_conversation(db, tenant, "whatsapp", contact=phone)
        conv.state = {"lead": {"phone": phone}}  # WhatsApp already tells us the number

    replies = services.handle_customer_message(db, tenant, conv, params.get("Body", ""))
    return Response(whatsapp.twiml(replies), media_type="application/xml")


# ---------------------------------------------------------------- platform API (the operator, across businesses)

@app.get("/v1/platform/templates", dependencies=[Depends(require_platform_admin)])
def platform_templates() -> list[dict]:
    return [{"id": k, "label": t["label"], "questions": [q["prompt"] for q in t["questions"]]}
            for k, t in services.TEMPLATES.items()]


@app.get("/v1/platform/tenants", dependencies=[Depends(require_platform_admin)])
def platform_tenants(db: Session = Depends(get_db)) -> list[dict]:
    counts = dict(db.execute(select(Conversation.tenant_id, func.count()).group_by(Conversation.tenant_id)).all())
    rows = db.scalars(select(Tenant).order_by(Tenant.created_at.desc())).all()
    return [{"id": t.id, "slug": t.slug, "name": t.name, "public_key": t.public_key,
             "template": (t.settings or {}).get("template"), "conversations": counts.get(t.id, 0),
             "llm_tokens_used": t.tokens_used, "llm_token_quota": t.token_quota, "created_at": t.created_at} for t in rows]


@app.post("/v1/platform/tenants", status_code=201, dependencies=[Depends(require_platform_admin)])
def platform_create_tenant(body: TenantIn, db: Session = Depends(get_db)) -> dict:
    try:
        tenant, admin_key = services.create_tenant(
            db, body.name, body.slug, body.template, body.greeting, body.booking_enabled,
            body.hours.model_dump() if body.hours else None, body.knowledge,
        )
    except services.TenantError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "public_key": tenant.public_key,
            "admin_key": admin_key, "note": "Save the admin key now. It is stored only as a hash and can't be shown again."}


@app.post("/v1/platform/tenants/{tenant_id}/rotate-admin-key", dependencies=[Depends(require_platform_admin)])
def platform_rotate_key(tenant_id: str, db: Session = Depends(get_db)) -> dict:
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return {"admin_key": services.rotate_admin_key(db, tenant),
            "note": "The old admin key no longer works. Save this one now."}


# ---------------------------------------------------------------- admin API

@app.get("/v1/admin/me")
def admin_me(tenant: Tenant = Depends(tenant_from_admin_key)) -> dict:
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "public_key": tenant.public_key,
            "settings": tenant.settings}


class OriginsIn(BaseModel):
    origins: list[str] = Field(default_factory=list, max_length=20)


@app.put("/v1/admin/settings/allowed-origins")
def admin_set_origins(body: OriginsIn, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """Websites the chat widget may run on, e.g. https://www.example.com or
    https://*.example.com. An empty list allows any website."""
    cleaned = []
    for o in body.origins:
        if not o.strip():
            continue
        n = normalize_origin(o)
        if not n:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{o}' isn't a website origin. Use a form like https://www.example.com")
        cleaned.append(n)
    tenant.settings = {**(tenant.settings or {}), "allowed_origins": list(dict.fromkeys(cleaned))}
    services.record_event(db, tenant, "settings_changed", None, field="allowed_origins", count=len(cleaned))
    db.commit()
    return {"ok": True, "allowed_origins": tenant.settings["allowed_origins"]}


@app.get("/v1/admin/analytics")
def admin_analytics(tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    return services.analytics(db, tenant)


@app.get("/v1/admin/conversations")
def admin_conversations(
    status_filter: str | None = None,
    tenant: Tenant = Depends(tenant_from_admin_key),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = select(Conversation).where(Conversation.tenant_id == tenant.id)
    if status_filter:
        q = q.where(Conversation.status == status_filter)
    rows = db.scalars(q.order_by(Conversation.updated_at.desc()).limit(200)).all()
    out = []
    for c in rows:
        last = db.scalar(select(Message).where(Message.conversation_id == c.id).order_by(Message.id.desc()).limit(1))
        lead_name = ((c.state or {}).get("lead") or {}).get("name")
        out.append({
            "id": c.id, "channel": c.channel, "status": c.status, "handover_reason": c.handover_reason,
            "customer": lead_name or c.contact_hint or "Website visitor",
            "last_message": last.text[:140] if last else "", "updated_at": c.updated_at,
        })
    return out


@app.get("/v1/admin/conversations/{conversation_id}")
def admin_conversation(conversation_id: str, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    conv = _conversation(db, tenant, conversation_id)
    lead = db.scalar(select(Lead).where(Lead.conversation_id == conv.id, Lead.tenant_id == tenant.id))
    return {
        "id": conv.id, "channel": conv.channel, "status": conv.status, "handover_reason": conv.handover_reason,
        "lead": {"data": lead.data, "score": lead.score, "status": lead.status} if lead else None,
        "messages": [m.model_dump() for m in _messages(db, conv)],
    }


@app.post("/v1/admin/conversations/{conversation_id}/reply")
def admin_reply(conversation_id: str, body: MessageIn, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """A team member replies. Web chat shows it on the widget's next poll;
    WhatsApp replies are sent through the Twilio API."""
    conv = _conversation(db, tenant, conversation_id)
    if conv.status != "handover":
        conv.status, conv.handover_reason = "handover", "agent_joined"
    delivered, warning = _deliver(conv, body.text)
    services.add_message(db, conv, "agent", body.text)
    services.record_event(db, tenant, "agent_reply", conv.id, channel=conv.channel, delivered=delivered)
    db.commit()
    return {"ok": True, "delivered": delivered, "warning": warning}


@app.post("/v1/admin/conversations/{conversation_id}/resolve")
def admin_resolve(conversation_id: str, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """Hand the conversation back to the bot."""
    conv = _conversation(db, tenant, conversation_id)
    conv.status, conv.handover_reason = "bot", None
    st = dict(conv.state or {})
    st.update(stage="idle", misses=0, awaiting=None)
    conv.state = st
    services.add_message(db, conv, "system", "The team has handed this chat back to the assistant.")
    services.record_event(db, tenant, "handover_resolved", conv.id)
    db.commit()
    return {"ok": True}


@app.delete("/v1/admin/conversations/{conversation_id}")
def admin_delete_conversation(conversation_id: str, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """Delete one conversation with its messages, lead and bookings."""
    conv = _conversation(db, tenant, conversation_id)
    services.delete_conversations(db, tenant, [conv.id], reason="admin_request")
    return {"ok": True, "deleted": 1}


class EraseIn(BaseModel):
    phone: str | None = Field(None, max_length=40)
    email: str | None = Field(None, max_length=200)


@app.post("/v1/admin/privacy/erase")
def admin_erase_customer(body: EraseIn, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """A customer asked for their data to be deleted: remove every conversation
    linked to their phone number or email (chats, leads, bookings)."""
    if not ((body.phone and pii.find_phone(body.phone)) or (body.email and pii.find_email(body.email))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give a phone number or email address")
    ids = services.conversations_for_contact(db, tenant, body.phone, body.email)
    return {"ok": True, "deleted": services.delete_conversations(db, tenant, ids, reason="customer_erasure")}


@app.post("/v1/admin/privacy/purge")
def admin_purge(older_than_days: int, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """Retention: delete conversations with no activity for this many days."""
    if older_than_days < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "older_than_days must be at least 1")
    return {"ok": True, "deleted": services.purge_older_than(db, tenant, older_than_days)}


@app.get("/v1/admin/leads")
def admin_leads(tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Lead).where(Lead.tenant_id == tenant.id).order_by(Lead.created_at.desc())).all()
    return [{"id": l.id, "conversation_id": l.conversation_id, "data": l.data, "score": l.score,
             "status": l.status, "created_at": l.created_at} for l in rows]


@app.get("/v1/admin/bookings")
def admin_bookings(tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Booking).where(Booking.tenant_id == tenant.id).order_by(Booking.start_at)).all()
    leads = {l.id: l for l in db.scalars(select(Lead).where(Lead.tenant_id == tenant.id)).all()}
    out = []
    for b in rows:
        lead = leads.get(b.lead_id) if b.lead_id else None
        out.append({"id": b.id, "label": b.label, "start_at_utc": b.start_at, "status": b.status,
                    "conversation_id": b.conversation_id, "customer": (lead.data or {}).get("name") if lead else None})
    return out


@app.post("/v1/admin/bookings/{booking_id}/cancel")
def admin_cancel_booking(booking_id: str, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    """Cancel a booking: frees the slot and tells the customer in their chat."""
    booking = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.tenant_id == tenant.id))
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    if booking.status != "confirmed":
        raise HTTPException(status.HTTP_409_CONFLICT, "This booking is already cancelled")
    conv, notice = services.cancel_booking(db, tenant, booking)
    delivered, warning = _deliver(conv, notice) if conv else (True, None)
    return {"ok": True, "delivered": delivered, "warning": warning}


@app.get("/v1/admin/events")
def admin_events(
    limit: int = 100,
    type: str | None = None,
    tenant: Tenant = Depends(tenant_from_admin_key),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Audit log, newest first. Event data is PII-redacted when it is written."""
    q = select(Event).where(Event.tenant_id == tenant.id)
    if type:
        q = q.where(Event.type == type)
    rows = db.scalars(q.order_by(Event.id.desc()).limit(max(1, min(limit, 500)))).all()
    return [{"id": e.id, "type": e.type, "conversation_id": e.conversation_id, "data": e.data, "created_at": e.created_at}
            for e in rows]


@app.get("/v1/admin/documents")
def admin_documents(tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Document).where(Document.tenant_id == tenant.id).order_by(Document.created_at.desc())).all()
    return [{"id": d.id, "title": d.title, "source": d.source, "chunks": len(d.chunks), "created_at": d.created_at} for d in rows]


@app.post("/v1/admin/documents", status_code=201)
def admin_add_document(body: DocumentIn, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    doc = services.add_document(db, tenant, body.title, body.text)
    return {"id": doc.id, "title": doc.title}


@app.post("/v1/admin/documents/upload", status_code=201)
async def admin_upload_document(
    file: UploadFile = File(...),
    title: str = Form(""),
    tenant: Tenant = Depends(tenant_from_admin_key),
    db: Session = Depends(get_db),
) -> dict:
    name = file.filename or "upload.txt"
    if not name.lower().endswith((".txt", ".md", ".pdf")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload a .txt, .md or .pdf file")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is larger than 5 MB")
    text = services.extract_text(name, raw)
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No text found in that file")
    doc = services.add_document(db, tenant, title or name, text, source=name.rsplit(".", 1)[-1])
    return {"id": doc.id, "title": doc.title}


@app.delete("/v1/admin/documents/{document_id}")
def admin_delete_document(document_id: str, tenant: Tenant = Depends(tenant_from_admin_key), db: Session = Depends(get_db)) -> dict:
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == tenant.id))
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    db.delete(doc)
    db.commit()
    return {"ok": True}
