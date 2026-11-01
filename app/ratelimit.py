"""Request rate limits: sliding window counters kept in memory.

Buckets (requests per minute, set by environment variables):
- new chats, per business and visitor IP     RATE_LIMIT_PUBLIC_PER_MIN  (default 30)
- messages, per conversation                  RATE_LIMIT_PUBLIC_PER_MIN
- admin API, per business                     RATE_LIMIT_ADMIN_PER_MIN   (default 300)
- WhatsApp, per business and sender           RATE_LIMIT_PUBLIC_PER_MIN
- failed key checks, per IP (stops key guessing; a blocked IP is refused
  before its key is even looked up)           RATE_LIMIT_AUTH_FAILS_PER_MIN (default 10)

Counters live in this process, which is right for a single instance. With
several instances behind a load balancer they move to Redis (roadmap); the
`hit()` interface stays the same.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, status

from .config import settings

WINDOW = 60.0  # seconds

LIMITS = {
    "public": settings.rate_limit_public_per_min,
    "admin": settings.rate_limit_admin_per_min,
    "auth_fail": settings.rate_limit_auth_fails_per_min,
}


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _window(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - WINDOW:
            q.popleft()
        return q

    def blocked(self, key: str, limit: int) -> float:
        """Seconds until `key` may try again (0 if it is not blocked). Does not count a hit."""
        if limit <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            q = self._window(key, now)
            return q[0] + WINDOW - now if len(q) >= limit else 0.0

    def hit(self, key: str, limit: int) -> float:
        """Count one request. Returns seconds to wait if over the limit, else 0."""
        if limit <= 0:  # 0 disables the limit
            return 0.0
        now = time.monotonic()
        with self._lock:
            q = self._window(key, now)
            if len(q) >= limit:
                return q[0] + WINDOW - now
            q.append(now)
            if len(self._hits) > 50_000:  # drop idle buckets so memory stays bounded
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - WINDOW]:
                    del self._hits[k]
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """The visitor's IP. Behind N trusted proxies (TRUSTED_PROXY_HOPS=N), each proxy
    appends the address it received the request from to X-Forwarded-For, so the
    visitor is the Nth entry from the right. Entries further left can be forged
    by the client and are ignored."""
    hops = settings.trusted_proxy_hops
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if parts:
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else "unknown"


def too_many(retry_after: float) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests. Please wait a moment and try again.",
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


def enforce(bucket: str, key: str) -> None:
    wait = limiter.hit(f"{bucket}:{key}", LIMITS[bucket])
    if wait:
        raise too_many(wait)
