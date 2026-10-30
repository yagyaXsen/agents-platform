"""WhatsApp via Twilio: verify the webhook signature and build the TwiML reply.

Twilio posts form fields (From, Body, …) to our webhook and reads the reply
from the XML we return. The same unified message handler serves web chat and
WhatsApp; this module only adapts the format.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape


def verify_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/values))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


def twiml(messages: list[str]) -> str:
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages if m)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def sender(params: dict[str, str]) -> str:
    """'whatsapp:+9198…' -> '+9198…'"""
    return (params.get("From") or "").replace("whatsapp:", "").strip()


class SendError(Exception):
    pass


def send_message(account_sid: str, auth_token: str, from_number: str, to_number: str, body: str, timeout: float = 15) -> str:
    """Send an outbound WhatsApp message (used when a team member replies after
    handover). Returns Twilio's message SID. Raises SendError on failure."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}",
        "To": f"whatsapp:{to_number}",
        "Body": body,
    }).encode()
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("sid", "")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise SendError(str(e)) from e
