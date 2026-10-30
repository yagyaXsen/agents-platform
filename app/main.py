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
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .channels import whatsapp
from .config import settings
from .core import pii
from .db import get_db, init_db
from .models import Booking, Conversation, Document, Lead, Message, Tenant
from .security import tenant_from_admin_key, tenant_from_public_key

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
    allow_methods=["GET", "POST", "DELETE"],
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


def _messages(db: Session, conv: Conversation, after_id: int = 0) -> list[MessageOut]:
    rows = db.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.id > after_id).order_by(Message.id)
    ).all()
    return [MessageOut(id=m.id, role=m.role, text=m.text, created_at=m.created_at) for m in rows]


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


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm": "enabled" if services.LLM.enabled else "extractive-fallback"}


# ---------------------------------------------------------------- public chat API (web widget)

@app.get("/v1/agent")
def agent_info(tenant: Tenant = Depends(tenant_from_public_key)) -> dict:
    cfg = services.agent_config(tenant)
    return {"name": tenant.name, "greeting": cfg.greeting, "theme": (tenant.settings or {}).get("theme", {})}


@app.post("/v1/conversations", response_model=ChatReply)
def create_conversation(tenant: Tenant = Depends(tenant_from_public_key), db: Session = Depends(get_db)):
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


# ---------------------------------------------------------------- admin API

@app.get("/v1/admin/me")
def admin_me(tenant: Tenant = Depends(tenant_from_admin_key)) -> dict:
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "public_key": tenant.public_key,
            "settings": tenant.settings}


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
    delivered, warning = True, None
    if conv.channel == "whatsapp":
        phone = ((conv.state or {}).get("lead") or {}).get("phone")
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_from and phone):
            delivered, warning = False, "WhatsApp sending isn't configured (TWILIO_ACCOUNT_SID, TWILIO_WHATSAPP_FROM)."
        else:
            try:
                whatsapp.send_message(settings.twilio_account_sid, settings.twilio_auth_token,
                                      settings.twilio_whatsapp_from, phone, body.text)
            except whatsapp.SendError as e:
                logging.getLogger("agentgrid").warning("WhatsApp send failed: %s", e)
                delivered, warning = False, "Couldn't send on WhatsApp. Try again."
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
