"""Tenant resolution from API keys.

- Chat widget / public API: `X-Agent-Key: pk_…` (public, safe to embed in a website)
- Admin API: `Authorization: Bearer sk_…` (secret; stored only as a SHA-256 hash)

Every endpoint resolves exactly one tenant from the key, and every query is
filtered by that tenant's id, so one business can never read another's data.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Tenant


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_keys(slug: str) -> tuple[str, str]:
    return f"pk_{slug}_{secrets.token_urlsafe(12)}", f"sk_{slug}_{secrets.token_urlsafe(24)}"


def tenant_from_public_key(
    x_agent_key: str = Header(..., alias="X-Agent-Key"),
    db: Session = Depends(get_db),
) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.public_key == x_agent_key))
    if not tenant:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent key")
    return tenant


def tenant_from_admin_key(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> Tenant:
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Use: Authorization: Bearer <admin key>")
    tenant = db.scalar(select(Tenant).where(Tenant.admin_key_hash == hash_key(key.strip())))
    if not tenant:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin key")
    return tenant
