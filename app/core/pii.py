"""Detect and redact personal data (emails, phone numbers) before logging."""
from __future__ import annotations

import hashlib
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 8+ digits, allowing spaces, dots, dashes, brackets and a leading +
PHONE_RE = re.compile(r"(?<![\w@])\+?\d[\d\s().-]{6,}\d(?![\w@])")


def find_email(text: str) -> str | None:
    m = EMAIL_RE.search(text)
    return m.group(0) if m else None


def find_phone(text: str) -> str | None:
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 8 <= len(digits) <= 15:
            return ("+" if m.group(0).strip().startswith("+") else "") + digits
    return None


def redact(text: str) -> str:
    """Replace emails and phone numbers with placeholders. Use for logs and exports."""
    text = EMAIL_RE.sub("[email]", text)

    def _phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[phone]" if 8 <= len(digits) <= 15 else m.group(0)

    return PHONE_RE.sub(_phone, text)


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"•••• {digits[-4:]}" if len(digits) >= 4 else "••••"


def fingerprint(value: str) -> str:
    """Stable one-way id for an identifier (e.g. a WhatsApp number) so we can
    look up a conversation without storing the raw number as the key."""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()
