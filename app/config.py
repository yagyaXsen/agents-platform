"""Settings from environment variables (and a local .env file, if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    twilio_auth_token: str
    twilio_account_sid: str
    twilio_whatsapp_from: str
    public_base_url: str
    cors_origins: tuple[str, ...]
    log_level: str
    rate_limit_public_per_min: int
    rate_limit_admin_per_min: int
    rate_limit_auth_fails_per_min: int
    trusted_proxy_hops: int
    platform_admin_key: str
    max_tenants: int


def load() -> Settings:
    db = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'agentgrid.db'}")
    # Render/Heroku-style URLs use postgres:// — SQLAlchemy needs a driver name
    if db.startswith("postgres://"):
        db = "postgresql+psycopg://" + db[len("postgres://"):]
    elif db.startswith("postgresql://"):
        db = "postgresql+psycopg://" + db[len("postgresql://"):]
    return Settings(
        database_url=db,
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_whatsapp_from=os.environ.get("TWILIO_WHATSAPP_FROM", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        cors_origins=tuple(o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        rate_limit_public_per_min=int(os.environ.get("RATE_LIMIT_PUBLIC_PER_MIN", "30")),
        rate_limit_admin_per_min=int(os.environ.get("RATE_LIMIT_ADMIN_PER_MIN", "300")),
        rate_limit_auth_fails_per_min=int(os.environ.get("RATE_LIMIT_AUTH_FAILS_PER_MIN", "10")),
        trusted_proxy_hops=int(os.environ.get("TRUSTED_PROXY_HOPS", "0")),
        platform_admin_key=os.environ.get("PLATFORM_ADMIN_KEY", "").strip(),
        max_tenants=int(os.environ.get("MAX_TENANTS", "100")),
    )


settings = load()
