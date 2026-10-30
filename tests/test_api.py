"""End-to-end API tests (needs the packages in requirements-dev.txt):

    pytest -q
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["LLM_API_KEY"] = ""  # always test the offline, extractive path
for _k in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_WHATSAPP_FROM"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402

CLINIC = {"X-Agent-Key": "pk_demo_smile_dental"}
CLINIC_ADMIN = {"Authorization": "Bearer sk_demo_smile_dental"}
SKYLINE_ADMIN = {"Authorization": "Bearer sk_demo_skyline_homes"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        seed(reset=True)
        yield c


def say(client, conv_id, text):
    r = client.post(f"/v1/conversations/{conv_id}/messages", json={"text": text}, headers=CLINIC)
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json()["ok"] is True


def test_rejects_bad_keys(client):
    assert client.post("/v1/conversations", headers={"X-Agent-Key": "nope"}).status_code == 401
    assert client.get("/v1/admin/leads", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_answer_book_and_admin_views(client):
    conv = client.post("/v1/conversations", headers=CLINIC).json()
    cid = conv["conversation_id"]
    assert "Smile Dental" in conv["replies"][0]

    r = say(client, cid, "How much is teeth whitening?")
    assert "6,500" in r["replies"][0]

    say(client, cid, "yes")
    say(client, cid, "I'm Priya Sharma")
    say(client, cid, "whitening")
    say(client, cid, "next week")
    r = say(client, cid, "98765 43210")
    assert "1." in r["replies"][0]
    r = say(client, cid, "1")
    assert "booked" in r["replies"][0].lower()

    leads = client.get("/v1/admin/leads", headers=CLINIC_ADMIN).json()
    assert any(l["data"].get("name") == "Priya Sharma" and l["status"] == "booked" for l in leads)
    bookings = client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json()
    assert len(bookings) == 1 and bookings[0]["customer"] == "Priya Sharma"

    stats = client.get("/v1/admin/analytics", headers=CLINIC_ADMIN).json()
    assert stats["bookings"] == 1 and stats["leads"] == 1


def test_same_slot_cannot_be_double_booked(client):
    def book(name):
        cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
        for t in ["book an appointment", name, "cleaning", "soon", "someone@example.com"]:
            say(client, cid, t)
        return say(client, cid, "1")["replies"][0]

    first = book("Asha")
    second = book("Ravi")
    assert "booked" in first.lower()
    assert "booked" in second.lower()
    labels = [b["label"] for b in client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json()]
    assert len(labels) == len(set(labels))  # every booking is a different slot


def test_handover_and_agent_reply(client):
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    r = say(client, cid, "I want to speak to a human")
    assert r["status"] == "handover"
    assert say(client, cid, "hello?")["replies"] == []  # bot stays quiet

    inbox = client.get("/v1/admin/conversations?status_filter=handover", headers=CLINIC_ADMIN).json()
    assert any(c["id"] == cid for c in inbox)

    client.post(f"/v1/admin/conversations/{cid}/reply", json={"text": "Hi, this is Meera from the clinic."}, headers=CLINIC_ADMIN)
    msgs = client.get(f"/v1/conversations/{cid}/messages", headers=CLINIC).json()["messages"]
    assert msgs[-1]["role"] == "agent"

    client.post(f"/v1/admin/conversations/{cid}/resolve", headers=CLINIC_ADMIN)
    assert say(client, cid, "what are your timings?")["replies"]  # bot is back


def test_tenant_isolation(client):
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    # Skyline's admin cannot read the clinic's conversation
    assert client.get(f"/v1/admin/conversations/{cid}", headers=SKYLINE_ADMIN).status_code == 404
    # Skyline's widget key cannot post into it either
    r = client.post(f"/v1/conversations/{cid}/messages", json={"text": "hi"}, headers={"X-Agent-Key": "pk_demo_skyline_homes"})
    assert r.status_code == 404
    # Skyline knows nothing about teeth
    sky = client.post("/v1/conversations", headers={"X-Agent-Key": "pk_demo_skyline_homes"}).json()["conversation_id"]
    r = client.post(f"/v1/conversations/{sky}/messages", json={"text": "how much is teeth whitening?"},
                    headers={"X-Agent-Key": "pk_demo_skyline_homes"}).json()
    assert "6,500" not in " ".join(r["replies"])


def test_documents_crud(client):
    r = client.post("/v1/admin/documents", json={"title": "Offers", "text": "Diwali offer: 20% off whitening until 15 November."},
                    headers=CLINIC_ADMIN)
    assert r.status_code == 201
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    assert "20%" in say(client, cid, "Is there a Diwali offer?")["replies"][0]
    up = client.post("/v1/admin/documents/upload", files={"file": ("faq.txt", b"We offer free parking.", "text/plain")},
                     headers=CLINIC_ADMIN)
    assert up.status_code == 201
    doc_id = r.json()["id"]
    assert client.delete(f"/v1/admin/documents/{doc_id}", headers=CLINIC_ADMIN).json()["ok"]


def test_whatsapp_webhook(client):
    r = client.post("/v1/channels/whatsapp/smile-dental", data={"From": "whatsapp:+919876543210", "Body": "what are your timings?"})
    assert r.status_code == 200
    assert "<Message>" in r.text and "10:00" in r.text
    convs = client.get("/v1/admin/conversations", headers=CLINIC_ADMIN).json()
    wa = [c for c in convs if c["channel"] == "whatsapp"]
    assert wa and "3210" in wa[0]["customer"] and "98765" not in wa[0]["customer"]
    # Without Twilio credentials, a team reply is saved but flagged as not delivered
    r = client.post(f"/v1/admin/conversations/{wa[0]['id']}/reply", json={"text": "Hi from the team"}, headers=CLINIC_ADMIN).json()
    assert r["delivered"] is False and "configured" in r["warning"]
