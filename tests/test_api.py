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
os.environ["PLATFORM_ADMIN_KEY"] = "pl_test_platform_key"
for _k in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_WHATSAPP_FROM"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import ratelimit  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402

CLINIC = {"X-Agent-Key": "pk_demo_smile_dental"}
CLINIC_ADMIN = {"Authorization": "Bearer sk_demo_smile_dental"}
SKYLINE_ADMIN = {"Authorization": "Bearer sk_demo_skyline_homes"}
PLATFORM = {"Authorization": "Bearer pl_test_platform_key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        seed(reset=True)
        yield c


@pytest.fixture(autouse=True)
def fresh_limits():
    ratelimit.limiter.reset()  # each test starts with empty rate-limit counters
    yield
    ratelimit.limiter.reset()


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


def test_public_chat_rate_limit(client, monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, "public", 2)
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    say(client, cid, "hello")
    say(client, cid, "what are your timings?")
    # a third message in the same minute is refused, with a Retry-After hint
    r = client.post(f"/v1/conversations/{cid}/messages", json={"text": "and parking?"}, headers=CLINIC)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    # reading messages (the widget's polling) is not limited
    assert client.get(f"/v1/conversations/{cid}/messages", headers=CLINIC).status_code == 200
    # new chats are limited per business and visitor IP
    assert client.post("/v1/conversations", headers=CLINIC).status_code == 200
    assert client.post("/v1/conversations", headers=CLINIC).status_code == 429
    # another business's widget on the same IP has its own budget
    assert client.post("/v1/conversations", headers={"X-Agent-Key": "pk_demo_skyline_homes"}).status_code == 200


def test_client_ip_behind_proxies(monkeypatch):
    import dataclasses

    from starlette.requests import Request

    def ip(xff, hops):
        monkeypatch.setattr(ratelimit, "settings", dataclasses.replace(ratelimit.settings, trusted_proxy_hops=hops))
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return ratelimit.client_ip(Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1234)}))

    assert ip("1.2.3.4", 0) == "10.0.0.9"  # proxies not trusted: socket address
    assert ip("6.6.6.6, 1.2.3.4", 1) == "1.2.3.4"  # forged left entry ignored
    assert ip("6.6.6.6, 1.2.3.4, 172.16.0.1", 2) == "1.2.3.4"
    assert ip("", 1) == "10.0.0.9"


def test_key_guessing_is_blocked(client, monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, "auth_fail", 3)
    for _ in range(3):
        assert client.get("/v1/admin/me", headers={"Authorization": "Bearer sk_guess"}).status_code == 401
    # this IP is now refused, even with a valid key, until the window passes
    assert client.get("/v1/admin/me", headers=CLINIC_ADMIN).status_code == 429
    assert client.post("/v1/conversations", headers=CLINIC).status_code == 429


def test_admin_rate_limit(client, monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, "admin", 2)
    assert client.get("/v1/admin/me", headers=CLINIC_ADMIN).status_code == 200
    assert client.get("/v1/admin/leads", headers=CLINIC_ADMIN).status_code == 200
    assert client.get("/v1/admin/leads", headers=CLINIC_ADMIN).status_code == 429
    assert client.get("/v1/admin/leads", headers=SKYLINE_ADMIN).status_code == 200  # per business


def test_whatsapp_rate_limit_drops_quietly(client, monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, "public", 1)
    form = {"From": "whatsapp:+919800000001", "Body": "what are your timings?"}
    assert "<Message>" in client.post("/v1/channels/whatsapp/smile-dental", data=form).text
    r = client.post("/v1/channels/whatsapp/smile-dental", data=form)
    assert r.status_code == 200 and "<Message>" not in r.text


def test_platform_onboards_a_new_business(client):
    body = {"name": "Green Leaf Salon", "template": "clinic", "greeting": "Hi! Welcome to Green Leaf.",
            "hours": {"open_hour": 11, "close_hour": 20},
            "knowledge": "## Prices\nA haircut costs Rs 700. Hair colouring starts at Rs 2,500."}
    r = client.post("/v1/platform/tenants", json=body, headers=PLATFORM)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["slug"] == "green-leaf-salon" and new["admin_key"].startswith("sk_") and new["public_key"].startswith("pk_")

    admin = {"Authorization": f"Bearer {new['admin_key']}"}
    me = client.get("/v1/admin/me", headers=admin).json()
    assert me["name"] == "Green Leaf Salon" and me["settings"]["hours"]["open_hour"] == 11

    widget = {"X-Agent-Key": new["public_key"]}
    conv = client.post("/v1/conversations", headers=widget).json()
    assert conv["replies"] == ["Hi! Welcome to Green Leaf."]
    r = client.post(f"/v1/conversations/{conv['conversation_id']}/messages", json={"text": "How much is a haircut?"},
                    headers=widget).json()
    assert "700" in r["replies"][0]
    # the new business sees only its own data
    assert client.get("/v1/admin/leads", headers=admin).json() == []
    assert any(t["slug"] == "green-leaf-salon" for t in client.get("/v1/platform/tenants", headers=PLATFORM).json())

    # same slug again is refused; bad input is refused
    assert client.post("/v1/platform/tenants", json={"name": "Green Leaf Salon"}, headers=PLATFORM).status_code == 400
    assert client.post("/v1/platform/tenants", json={"name": "X Y", "template": "nope"}, headers=PLATFORM).status_code == 400
    bad_tz = {"name": "Tz Test", "hours": {"timezone": "Mars/Base"}}
    assert client.post("/v1/platform/tenants", json=bad_tz, headers=PLATFORM).status_code == 400

    # rotating the admin key locks out the old one
    rotated = client.post(f"/v1/platform/tenants/{new['id']}/rotate-admin-key", headers=PLATFORM).json()["admin_key"]
    assert client.get("/v1/admin/me", headers=admin).status_code == 401
    assert client.get("/v1/admin/me", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200


def test_platform_needs_the_platform_key(client, monkeypatch):
    assert client.get("/v1/platform/tenants", headers=CLINIC_ADMIN).status_code == 401  # a business key is not enough
    assert client.post("/v1/platform/tenants", json={"name": "Sneaky"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    import dataclasses

    from app import security
    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, platform_admin_key=""))
    assert client.get("/v1/platform/tenants", headers=PLATFORM).status_code == 403


def test_delete_conversation_removes_everything(client):
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    for t in ["book an appointment", "Kavya Iyer", "check-up", "soon", "kavya@example.com", "1"]:
        say(client, cid, t)
    assert any(b["conversation_id"] == cid for b in client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json())

    # another business can't delete it
    assert client.delete(f"/v1/admin/conversations/{cid}", headers=SKYLINE_ADMIN).status_code == 404
    assert client.delete(f"/v1/admin/conversations/{cid}", headers=CLINIC_ADMIN).json()["deleted"] == 1
    assert client.get(f"/v1/admin/conversations/{cid}", headers=CLINIC_ADMIN).status_code == 404
    assert not any(l["conversation_id"] == cid for l in client.get("/v1/admin/leads", headers=CLINIC_ADMIN).json())
    assert not any(b["conversation_id"] == cid for b in client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json())
    # the widget can't read it any more either
    assert client.get(f"/v1/conversations/{cid}/messages", headers=CLINIC).status_code == 404


def test_erase_customer_by_phone_and_email(client):
    # web chat lead with a phone number
    web = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    for t in ["book an appointment", "Rohan Mehta", "cleaning", "soon", "+91 99887 76655"]:
        say(client, web, t)
    # the same person on WhatsApp (number written differently)
    client.post("/v1/channels/whatsapp/smile-dental", data={"From": "whatsapp:+919988776655", "Body": "hi"})
    # someone else, identified by email
    other = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    for t in ["book an appointment", "Sara Khan", "whitening", "soon", "sara@example.com"]:
        say(client, other, t)

    assert client.post("/v1/admin/privacy/erase", json={}, headers=CLINIC_ADMIN).status_code == 400
    # erasing in another business finds nothing
    assert client.post("/v1/admin/privacy/erase", json={"phone": "9988776655"}, headers=SKYLINE_ADMIN).json()["deleted"] == 0

    r = client.post("/v1/admin/privacy/erase", json={"phone": "99887 76655"}, headers=CLINIC_ADMIN).json()
    assert r["deleted"] == 2
    convs = client.get("/v1/admin/conversations", headers=CLINIC_ADMIN).json()
    assert web not in [c["id"] for c in convs]
    assert not any("6655" in c["customer"] for c in convs)
    assert other in [c["id"] for c in convs]  # untouched

    assert client.post("/v1/admin/privacy/erase", json={"email": "SARA@example.com"}, headers=CLINIC_ADMIN).json()["deleted"] == 1


def test_retention_purge(client):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import Conversation, utcnow

    old = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    new = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    with SessionLocal() as db:
        db.get(Conversation, old).updated_at = utcnow() - timedelta(days=100)
        db.commit()
    assert client.post("/v1/admin/privacy/purge?older_than_days=0", headers=CLINIC_ADMIN).status_code == 400
    assert client.post("/v1/admin/privacy/purge?older_than_days=90", headers=CLINIC_ADMIN).json()["deleted"] >= 1
    ids = [c["id"] for c in client.get("/v1/admin/conversations", headers=CLINIC_ADMIN).json()]
    assert old not in ids and new in ids


def test_cancel_booking_frees_the_slot_and_tells_the_customer(client):
    def book(name):
        cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
        for t in ["book an appointment", name, "check-up", "soon", f"{name.lower()}@example.com"]:
            say(client, cid, t)
        return cid, say(client, cid, "1")["replies"][0]

    cid, reply = book("Neha")
    booking = next(b for b in client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json() if b["conversation_id"] == cid)
    assert booking["label"] in reply

    assert client.post(f"/v1/admin/bookings/{booking['id']}/cancel", headers=SKYLINE_ADMIN).status_code == 404
    r = client.post(f"/v1/admin/bookings/{booking['id']}/cancel", headers=CLINIC_ADMIN)
    assert r.status_code == 200 and r.json()["delivered"] is True
    assert client.post(f"/v1/admin/bookings/{booking['id']}/cancel", headers=CLINIC_ADMIN).status_code == 409

    msgs = client.get(f"/v1/conversations/{cid}/messages", headers=CLINIC).json()["messages"]
    assert msgs[-1]["role"] == "system" and "cancelled" in msgs[-1]["text"]
    lead = next(l for l in client.get("/v1/admin/leads", headers=CLINIC_ADMIN).json() if l["conversation_id"] == cid)
    assert lead["status"] == "needs_follow_up"

    # the freed slot is offered again and can be booked by someone else
    cid2, reply2 = book("Omar")
    assert booking["label"] in reply2
    rows = [b for b in client.get("/v1/admin/bookings", headers=CLINIC_ADMIN).json() if b["label"] == booking["label"]]
    assert len(rows) == 1 and rows[0]["status"] == "confirmed" and rows[0]["conversation_id"] == cid2


def test_audit_log(client):
    cid = client.post("/v1/conversations", headers=CLINIC).json()["conversation_id"]
    say(client, cid, "I want to talk to a human. My number is 98765 11111")
    events = client.get("/v1/admin/events?limit=20", headers=CLINIC_ADMIN).json()
    assert events[0]["id"] > events[-1]["id"]  # newest first
    assert any(e["type"] == "handover" and e["conversation_id"] == cid for e in events)
    assert "98765" not in str(events)  # nothing personal in the audit trail
    only = client.get("/v1/admin/events?type=conversation_started", headers=CLINIC_ADMIN).json()
    assert only and all(e["type"] == "conversation_started" for e in only)
    # another business sees none of these
    assert not any(e["conversation_id"] == cid for e in client.get("/v1/admin/events", headers=SKYLINE_ADMIN).json())


def test_widget_only_works_on_allowed_websites(client):
    sky = {"X-Agent-Key": "pk_demo_skyline_homes"}
    r = client.put("/v1/admin/settings/allowed-origins", headers=SKYLINE_ADMIN,
                   json={"origins": ["https://www.skylinehomes.in/", "https://*.skyline.example"]})
    assert r.json()["allowed_origins"] == ["https://www.skylinehomes.in", "https://*.skyline.example"]
    assert client.put("/v1/admin/settings/allowed-origins", headers=SKYLINE_ADMIN,
                      json={"origins": ["not a website"]}).status_code == 400
    try:
        def start(origin):
            return client.post("/v1/conversations", headers={**sky, "Origin": origin}).status_code

        assert start("https://www.skylinehomes.in") == 200
        assert start("https://listings.skyline.example") == 200
        assert start("http://listings.skyline.example") == 403  # wrong scheme
        assert start("https://evil.example") == 403
        assert start("https://skylinehomes.in.evil.example") == 403
        assert start("null") == 403
        assert start("http://testserver") == 200  # the platform's own demo page
        assert client.post("/v1/conversations", headers=sky).status_code == 200  # no Origin: not a browser
        # other businesses are unaffected
        assert client.post("/v1/conversations", headers={**CLINIC, "Origin": "https://evil.example"}).status_code == 200
    finally:
        client.put("/v1/admin/settings/allowed-origins", headers=SKYLINE_ADMIN, json={"origins": []})


def test_pages_answer_head_requests(client):
    for path in ["/", "/demo", "/admin", "/platform", "/health"]:
        assert client.head(path).status_code == 200, path
