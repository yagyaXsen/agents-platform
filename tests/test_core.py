"""Tests for the agent's core logic. Standard library only:

    python -m unittest tests.test_core -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.channels import whatsapp
from app.core import dialogue, pii, slots
from app.core.chunking import chunk_text
from app.core.dialogue import AgentConfig, Answer, Question, SlotOption
from app.core.retrieval import Passage, best_sentences, retrieve

SEED = Path(__file__).resolve().parent.parent / "app" / "seed_data"


def clinic_passages() -> list[Passage]:
    text = (SEED / "clinic.md").read_text()
    return [Passage(i, "Clinic guide", c) for i, c in enumerate(chunk_text(text))]


class FakeCtx:
    """Answers from the clinic guide with extractive retrieval; fixed slots."""

    def __init__(self, passages=None, options=None):
        self.passages = passages if passages is not None else clinic_passages()
        self.options = options if options is not None else [
            SlotOption("2026-11-03T11:00:00+05:30", "Tue 3 Nov, 11:00"),
            SlotOption("2026-11-05T16:00:00+05:30", "Thu 5 Nov, 16:00"),
            SlotOption("2026-11-06T10:00:00+05:30", "Fri 6 Nov, 10:00"),
        ]

    def answer(self, q: str) -> Answer:
        r = retrieve(q, self.passages)
        if not r.hits:
            return Answer("", 0.0)
        return Answer(best_sentences(q, r.hits[0].passage.text), r.confidence, (r.hits[0].passage.title,))

    def slots(self, timeline=None):
        self.last_timeline = timeline
        return self.options


CFG = AgentConfig(
    business_name="Smile Dental Clinic",
    greeting="Hi! I'm the Smile Dental assistant. How can I help?",
    questions=(
        Question("name", "May I have your name?", "name"),
        Question("need", "What would you like help with?"),
        Question("timeline", "When would you like to come in?"),
        Question("contact", "What's the best phone number or email to reach you?", "contact"),
    ),
)


def run(texts, ctx=None, state=None):
    ctx = ctx or FakeCtx()
    results = []
    for t in texts:
        r = dialogue.step(CFG, state, t, ctx)
        state = r.state
        results.append(r)
    return results


class TestPII(unittest.TestCase):
    def test_redact(self):
        s = pii.redact("Call me on +91 98765 43210 or mail priya.s@example.com")
        self.assertNotIn("98765", s)
        self.assertNotIn("example.com", s)
        self.assertIn("[phone]", s)
        self.assertIn("[email]", s)

    def test_short_numbers_untouched(self):
        self.assertEqual(pii.redact("Room 404, 2 BHK, ₹30,000"), "Room 404, 2 BHK, ₹30,000")

    def test_find(self):
        self.assertEqual(pii.find_phone("its 98765-43210 thanks"), "9876543210")
        self.assertEqual(pii.find_email("a.b@c.io"), "a.b@c.io")
        self.assertIsNone(pii.find_phone("no number here"))


class TestRetrieval(unittest.TestCase):
    def test_answers_price_question(self):
        r = retrieve("How much does teeth whitening cost?", clinic_passages())
        self.assertGreaterEqual(r.confidence, 0.5)
        self.assertIn("6,500", best_sentences("teeth whitening cost", r.hits[0].passage.text))

    def test_synonyms_timings(self):
        r = retrieve("what are your timings", clinic_passages())
        self.assertGreaterEqual(r.confidence, 0.5)
        self.assertIn("10:00", r.hits[0].passage.text)

    def test_unrelated_question_is_low_confidence(self):
        r = retrieve("do you sell car insurance in Mumbai", clinic_passages())
        self.assertLess(r.confidence, 0.5)

    def test_chunking_overlaps_and_covers(self):
        text = (SEED / "realestate.md").read_text()
        chunks = chunk_text(text, max_words=60, overlap_words=15)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(c.split()) <= 75 for c in chunks))
        self.assertIn("brokerage", " ".join(chunks).lower())


class TestSlots(unittest.TestCase):
    def test_upcoming_skips_sunday_taken_and_lead_time(self):
        tz = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 10, 31, 16, 30, tzinfo=tz)  # Saturday 16:30
        taken = {datetime(2026, 11, 2, 10, 0, tzinfo=tz).astimezone(timezone.utc)}
        out = slots.upcoming(now, slots.Hours(), taken, limit=3)
        # Sat after 18:30 is closed, Sunday closed, Mon 10:00 taken -> Mon 11:00 first
        self.assertEqual([slots.label(d) for d in out], ["Mon 2 Nov, 11:00", "Mon 2 Nov, 12:00", "Mon 2 Nov, 13:00"])

    def test_timeline_phrases(self):
        from datetime import date
        fri = date(2026, 10, 30)
        self.assertEqual(slots.not_before("next week", fri), date(2026, 11, 2))
        self.assertEqual(slots.not_before("tomorrow evening", fri), date(2026, 10, 31))
        self.assertEqual(slots.not_before("this weekend", fri), date(2026, 10, 31))
        self.assertEqual(slots.not_before("thursday", fri), date(2026, 11, 5))
        self.assertIsNone(slots.not_before("as soon as possible", fri))

    def test_upcoming_from_start_date(self):
        from datetime import date
        tz = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 10, 30, 18, 0, tzinfo=tz)
        out = slots.upcoming(now, slots.Hours(), set(), limit=2, start=date(2026, 11, 2))
        self.assertEqual([slots.label(d) for d in out], ["Mon 2 Nov, 10:00", "Mon 2 Nov, 11:00"])

    def test_pick(self):
        labels = ["Tue 3 Nov, 11:00", "Thu 5 Nov, 16:00", "Fri 6 Nov, 10:00"]
        self.assertEqual(slots.pick("2", labels), 1)
        self.assertEqual(slots.pick("the first one", labels), 0)
        self.assertEqual(slots.pick("thursday please", labels), 1)
        self.assertEqual(slots.pick("4 pm works", labels), 1)
        self.assertIsNone(slots.pick("7", labels))
        self.assertIsNone(slots.pick("whenever", labels))


class TestDialogue(unittest.TestCase):
    def test_greeting(self):
        (r,) = run(["hello"])
        self.assertEqual(r.intent, "greeting")

    def test_answer_with_source_then_offer_booking(self):
        (r,) = run(["How much is teeth whitening?"])
        self.assertEqual(r.intent, "answer")
        self.assertIn("6,500", r.replies[0])
        self.assertIn("Source: Clinic guide", r.replies[0])
        self.assertIn("book", r.replies[-1].lower())

    def test_full_booking_flow(self):
        rs = run([
            "How much is teeth whitening?",
            "yes",
            "my name is priya sharma",
            "whitening",
            "next week",
            "you can reach me at 98765 43210",
            "2",
        ])
        self.assertEqual(rs[1].intent, "start_booking")
        self.assertIn("Thanks, Priya.", rs[3 - 1].replies[0])
        offer = rs[5]
        self.assertEqual(offer.intent, "offer_slots")
        lead_action = offer.actions[0]
        self.assertEqual(lead_action.type, "save_lead")
        self.assertEqual(lead_action.data["lead"]["phone"], "9876543210")
        self.assertEqual(lead_action.data["lead"]["name"], "Priya Sharma")
        self.assertEqual(lead_action.data["score"], 80)
        booked = rs[6]
        self.assertEqual(booked.intent, "booked")
        self.assertEqual(booked.actions[0].data["label"], "Thu 5 Nov, 16:00")
        self.assertIn("Priya", booked.replies[0])
        self.assertEqual(booked.state["stage"], "idle")

    def test_slots_receive_customer_timeline(self):
        ctx = FakeCtx()
        run(["book", "Priya", "cleaning", "next week", "priya@example.com"], ctx=ctx)
        self.assertEqual(ctx.last_timeline, "next week")

    def test_answers_skip_markdown_headings(self):
        text = (SEED / "realestate.md").read_text()
        ctx = FakeCtx(passages=[Passage(i, "Guide", c) for i, c in enumerate(chunk_text(text))])
        (r,) = run(["What is your brokerage fee?"], ctx=ctx)
        self.assertIn("15 days of rent", r.replies[0])
        self.assertNotIn("#", r.replies[0])

    def test_direct_booking_request_starts_flow(self):
        (r,) = run(["I want to book an appointment"])
        self.assertEqual(r.intent, "start_booking")
        self.assertIn("name", r.replies[0].lower())

    def test_side_question_during_qualification(self):
        rs = run(["book an appointment", "Priya", "what are your timings?"])
        self.assertEqual(rs[2].intent, "side_answer")
        self.assertIn("10:00", rs[2].replies[0])
        self.assertEqual(rs[2].replies[-1], "What would you like help with?")

    def test_contact_must_be_valid(self):
        rs = run(["book", "Priya", "cleaning", "tomorrow", "call me whenever"])
        self.assertEqual(rs[-1].intent, "ask_again")

    def test_cancel_mid_flow(self):
        rs = run(["book an appointment", "cancel"])
        self.assertEqual(rs[-1].intent, "cancel")
        self.assertEqual(rs[-1].state["stage"], "idle")

    def test_handover_on_request_then_bot_stays_quiet(self):
        rs = run(["I want to talk to a human", "hello?"])
        self.assertEqual(rs[0].intent, "handover")
        self.assertEqual(rs[0].actions[0].type, "handover")
        self.assertEqual(rs[1].replies, [])

    def test_low_confidence_twice_hands_over(self):
        rs = run(["do you sell car insurance", "what about crypto trading"])
        self.assertEqual(rs[0].intent, "low_confidence")
        self.assertEqual(rs[1].intent, "handover")
        self.assertEqual(rs[1].actions[0].data["reason"], "low_confidence")

    def test_other_time_keeps_lead(self):
        rs = run(["book", "Priya", "cleaning", "soon", "priya@example.com", "none of these"])
        self.assertEqual(rs[-1].intent, "other_time")
        self.assertEqual(rs[-1].actions[0].type, "save_lead")

    def test_no_slots_available(self):
        rs = run(["book", "Priya", "cleaning", "soon", "priya@example.com"], ctx=FakeCtx(options=[]))
        self.assertEqual(rs[-1].intent, "lead_saved")


class TestWhatsApp(unittest.TestCase):
    def test_signature_roundtrip(self):
        import base64, hashlib, hmac
        url = "https://example.com/v1/channels/whatsapp/clinic"
        params = {"From": "whatsapp:+919876543210", "Body": "hi"}
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        sig = base64.b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(whatsapp.verify_signature("secret", url, params, sig))
        self.assertFalse(whatsapp.verify_signature("secret", url, {**params, "Body": "x"}, sig))

    def test_twiml_escapes(self):
        xml = whatsapp.twiml(["a < b & c"])
        self.assertIn("<Message>a &lt; b &amp; c</Message>", xml)
        self.assertEqual(whatsapp.sender({"From": "whatsapp:+91123"}), "+91123")


if __name__ == "__main__":
    unittest.main()


class FakeLLMServer:
    """A local stand-in for an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, status=200, reply="Whitening costs ₹6,500 per session."):
        import http.server
        import json
        import threading

        outer = self
        self.status, self.reply, self.requests = status, reply, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "nope"}}')
                    return
                out = json.dumps({"choices": [{"message": {"content": outer.reply}}], "usage": {"total_tokens": 42}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out.encode())

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class LLMClientTests(unittest.TestCase):
    def cfg(self, url):
        from app.core.llm import LLMConfig
        return LLMConfig(url, "test-key", "test-model", timeout=5)

    def test_grounded_answer(self):
        from app.core.llm import grounded_answer
        srv = FakeLLMServer()
        try:
            text, used = grounded_answer(self.cfg(srv.url), "Smile Dental", "How much is whitening?",
                                         [("Clinic guide", "Teeth whitening costs ₹6,500 per session.")])
        finally:
            srv.close()
        self.assertIn("6,500", text)
        self.assertEqual(used, 42)
        req = srv.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-key")
        self.assertNotIn("Python-urllib", req["headers"]["User-Agent"])
        self.assertEqual(req["body"]["model"], "test-model")
        self.assertIn("Teeth whitening costs", req["body"]["messages"][0]["content"])

    def test_no_answer_escape(self):
        from app.core.llm import grounded_answer
        srv = FakeLLMServer(reply="NO_ANSWER")
        try:
            text, _ = grounded_answer(self.cfg(srv.url), "Smile Dental", "Do you sell cars?", [("Clinic guide", "...")])
        finally:
            srv.close()
        self.assertIsNone(text)

    def test_errors_become_llm_error(self):
        from app.core.llm import LLMError, chat
        srv = FakeLLMServer(status=403)
        try:
            with self.assertRaisesRegex(LLMError, "HTTP 403"):
                chat(self.cfg(srv.url), [{"role": "user", "content": "hi"}])
        finally:
            srv.close()
        with self.assertRaises(LLMError):  # nothing listening there any more
            chat(self.cfg(srv.url), [{"role": "user", "content": "hi"}])
