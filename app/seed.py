"""Create two demo businesses (tenants) with knowledge documents.

    python -m app.seed            # create if missing, print keys
    python -m app.seed --reset    # delete the demo tenants first

Demo keys are fixed so the demo pages work out of the box. In a real
deployment, create tenants with random keys (security.generate_keys).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from . import services
from .db import SessionLocal, init_db
from .models import Tenant
from .security import hash_key

SEED = Path(__file__).resolve().parent / "seed_data"

DEMO_TENANTS = [
    {
        "slug": "smile-dental",
        "name": "Smile Dental Clinic",
        "public_key": "pk_demo_smile_dental",
        "admin_key": "sk_demo_smile_dental",
        "doc": ("Clinic guide", "clinic.md"),
        "settings": {
            "greeting": "Hi! I'm the Smile Dental assistant. Ask me about treatments, prices or timings, or I can book you in.",
            "booking_offer": "Would you like to book a visit? A first consultation is free.",
            "theme": {"accent": "#0f766e"},
            "hours": {"timezone": "Asia/Kolkata", "open_hour": 10, "close_hour": 18, "slot_minutes": 60,
                      "workdays": [0, 1, 2, 3, 4, 5]},
            "questions": [
                {"key": "name", "prompt": "May I have your name?", "kind": "name"},
                {"key": "need", "prompt": "What would you like help with? For example a check-up, whitening or a specific concern."},
                {"key": "timeline", "prompt": "When would you like to come in?"},
                {"key": "contact", "prompt": "What's the best phone number or email to confirm your appointment?", "kind": "contact"},
            ],
        },
    },
    {
        "slug": "skyline-homes",
        "name": "Skyline Homes",
        "public_key": "pk_demo_skyline_homes",
        "admin_key": "sk_demo_skyline_homes",
        "doc": ("Skyline Homes guide", "realestate.md"),
        "settings": {
            "greeting": "Hi! I'm the Skyline Homes assistant. Looking to buy or rent? I can answer questions and set up a site visit.",
            "booking_offer": "Would you like to book a free site visit?",
            "theme": {"accent": "#1d4ed8"},
            "hours": {"timezone": "Asia/Kolkata", "open_hour": 10, "close_hour": 18, "slot_minutes": 60,
                      "workdays": [0, 1, 2, 3, 4, 5]},
            "questions": [
                {"key": "name", "prompt": "Great. May I have your name?", "kind": "name"},
                {"key": "need", "prompt": "Are you looking to buy or rent, and what type of home (for example 2 BHK or builder floor)?"},
                {"key": "location", "prompt": "Which areas are you interested in?"},
                {"key": "budget", "prompt": "What's your budget range?"},
                {"key": "timeline", "prompt": "When are you planning to move or buy?"},
                {"key": "contact", "prompt": "What's the best phone number or email to reach you?", "kind": "contact"},
            ],
        },
    },
]


def seed(reset: bool = False) -> None:
    init_db()
    with SessionLocal() as db:
        for spec in DEMO_TENANTS:
            existing = db.scalar(select(Tenant).where(Tenant.slug == spec["slug"]))
            if existing and reset:
                db.delete(existing)
                db.commit()
                existing = None
            if existing:
                print(f"= {spec['name']} already exists")
                continue
            tenant = Tenant(
                slug=spec["slug"],
                name=spec["name"],
                public_key=spec["public_key"],
                admin_key_hash=hash_key(spec["admin_key"]),
                settings=spec["settings"],
            )
            db.add(tenant)
            db.commit()
            title, filename = spec["doc"]
            services.add_document(db, tenant, title, (SEED / filename).read_text(), source="md")
            print(f"+ {spec['name']}")

    print("\nDemo logins")
    for spec in DEMO_TENANTS:
        print(f"  {spec['name']:<22} widget key: {spec['public_key']:<24} admin key: {spec['admin_key']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete and recreate the demo tenants")
    seed(parser.parse_args().reset)
