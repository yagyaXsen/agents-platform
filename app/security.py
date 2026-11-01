"""Tenant resolution from API keys.

- Chat widget / public API: `X-Agent-Key: pk_…` (public, safe to embed in a website)
- Admin API: `Authorization: Bearer sk_…` (secret; stored only as a SHA-256 hash)

Every endpoint resolves exactly one tenant from the key, and every query is
filtered by that tenant's id, so one business can never read another's data.

Wrong keys count against the caller's IP; too many in a minute and that IP is
refused before any key is checked (see ratelimit.py).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Tenant
from .ratelimit import LIMITS, client_ip, enforce, limiter, too_many


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_keys(slug: str) -> tuple[str, str]:
    return f"pk_{slug}_{secrets.token_urlsafe(12)}", f"sk_{slug}_{secrets.token_urlsafe(24)}"


def _check_not_blocked(ip: str) -> None:
    wait = limiter.blocked(f"auth_fail:{ip}", LIMITS["auth_fail"])
    if wait:
        raise too_many(wait)


def _reject(ip: str, detail: str) -> HTTPException:
    limiter.hit(f"auth_fail:{ip}", LIMITS["auth_fail"])
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


def tenant_from_public_key(
    request: Request,
    x_agent_key: str = Header(..., alias="X-Agent-Key"),
    db: Session = Depends(get_db),
) -> Tenant:
    ip = client_ip(request)
    _check_not_blocked(ip)
    tenant = db.scalar(select(Tenant).where(Tenant.public_key == x_agent_key))
    if not tenant:
        raise _reject(ip, "Invalid agent key")
    if not origin_allowed(request, (tenant.settings or {}).get("allowed_origins") or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This chat widget isn't enabled for this website")
    return tenant


def normalize_origin(value: str) -> str | None:
    """'https://Shop.example.com/' -> 'https://shop.example.com'. Allows a
    leading '*.' for subdomains. Returns None if it isn't an origin."""
    value = value.strip().rstrip("/").lower()
    m = re.fullmatch(r"(https?)://(\*\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)*)(:\d{1,5})?", value)
    return value if m else None


def origin_allowed(request: Request, allowed: list[str]) -> bool:
    """The widget key works only on the business's own websites, when it has
    listed them. Browsers always send Origin on cross-site calls; calls with no
    Origin (same-site pages, servers) and this platform's own pages pass."""
    if not allowed:
        return True
    origin = (request.headers.get("Origin") or "").rstrip("/").lower()
    if not origin:
        return True  # "null" (sandboxed frames, local files) is checked like any origin and fails
    host = urlsplit(origin).netloc
    if host and host == (request.headers.get("Host") or "").lower():
        return True  # our own pages, e.g. /demo
    for rule in allowed:
        if rule == origin:
            return True
        if "://*." in rule:
            scheme, suffix = rule.split("://*.", 1)
            if origin.startswith(scheme + "://") and host.endswith("." + suffix):
                return True
    return False


def tenant_from_admin_key(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> Tenant:
    ip = client_ip(request)
    _check_not_blocked(ip)
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise _reject(ip, "Use: Authorization: Bearer <admin key>")
    tenant = db.scalar(select(Tenant).where(Tenant.admin_key_hash == hash_key(key.strip())))
    if not tenant:
        raise _reject(ip, "Invalid admin key")
    enforce("admin", tenant.id)
    return tenant


def require_platform_admin(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> None:
    """The platform operator (not a business): creates businesses and resets their keys.
    Disabled unless PLATFORM_ADMIN_KEY is set."""
    if not settings.platform_admin_key:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin is disabled. Set PLATFORM_ADMIN_KEY on the server.")
    ip = client_ip(request)
    _check_not_blocked(ip)
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(key.strip().encode(), settings.platform_admin_key.encode()):
        raise _reject(ip, "Invalid platform key")
    enforce("admin", "platform")
