# AgentGrid PoC

Proof of concept for **TTG-AI-AGENTGRID**: a multi-tenant platform for AI agents that answer
customers from a business's own documents, qualify leads, book appointments and hand over to a
person when needed, across web chat and WhatsApp.

Built by Alok Kumar, [bigO](https://www.buildwithbigo.com/).

> **Live demo:** `[add Render URL]` · **Video walkthrough:** `[add link]`

## What you can try

Two demo businesses run on the same deployment, with fully separate data:

| Business | Website with chat | Admin key | Widget key |
|---|---|---|---|
| Smile Dental Clinic (fictional) | `/demo?tenant=smile-dental` | `sk_demo_smile_dental` | `pk_demo_smile_dental` |
| Skyline Homes (fictional) | `/demo?tenant=skyline-homes` | `sk_demo_skyline_homes` | `pk_demo_skyline_homes` |

1. Open `/demo`, ask *"How much is teeth whitening?"*: the answer comes from the clinic's
   document, with its source.
2. Say *yes* to booking: the agent asks the clinic's qualification questions, offers real free
   slots (respecting "next week", "tomorrow", a weekday…) and books one. A slot can't be booked twice.
3. Type *"I want to talk to a human"*: the chat moves to the **Handover inbox** in `/admin`.
   Reply from there and the reply appears in the customer's chat.
4. In `/admin`: leads with scores, bookings, conversations, knowledge base (add text or upload
   .txt/.md/.pdf, and the agent answers from it immediately), analytics, embed code.

## How it works

```
Web widget ─┐                      ┌─ Dialogue manager (rules: qualify, book, hand over)
            ├─► one message ──────►│
WhatsApp ───┘   pipeline           └─ Knowledge answers: retrieval ─► LLM (grounded) or extractive
                    │
                    ├─► Postgres/SQLite: tenants, conversations, messages, leads, bookings, events
                    └─► Admin API + console: inbox, replies, leads, bookings, docs, analytics
```

- **Deterministic where it matters, LLM where it helps.** A rule-based dialogue manager
  (`app/core/dialogue.py`) runs qualification, booking and handover. The LLM is only used to
  phrase answers from retrieved passages, under a strict "answer only from context" prompt.
- **RAG with sources and a confidence score.** Retrieval returns how much of the question the
  best passage covers. Below the threshold, the agent says it isn't sure; the second time it
  hands over to a person instead of guessing.
- **Works without an LLM.** With no API key, answers are extracted from the documents (still with
  sources). Any OpenAI-compatible provider works (OpenAI, Groq, Gemini…). Each tenant has a token quota.
- **Multi-tenant by key.** The widget uses a public key; admins use a secret key stored only as
  a SHA-256 hash. Every query is filtered by the tenant resolved from the key (see `tests/test_api.py`).
- **Privacy.** Emails and phone numbers are redacted from logs and the audit trail; WhatsApp
  conversations are looked up by a hashed number and shown masked (`•••• 3210`).

More detail: [docs/architecture.md](docs/architecture.md) · Proposal: [docs/technical-proposal.md](docs/technical-proposal.md)

## Run locally

Requires Python 3.10+. With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
cp .env.example .env          # optional: add an LLM key
python -m app.seed            # creates the two demo businesses
uvicorn app.main:app --reload # http://localhost:8000
```

Or with Docker and Postgres: `docker compose up --build`.

## Tests

```bash
python -m unittest tests.test_core -v   # agent logic, no packages needed
pytest -q                               # API end-to-end: booking, handover, isolation, WhatsApp
```

## Deploy

`render.yaml` deploys the app and a Postgres database on Render (New → Blueprint). Set
`PUBLIC_BASE_URL` to the deployed URL. The Dockerfile works on Railway, Fly.io or any container host.

## Project layout

```
app/
  core/          agent logic, no web or database code (dialogue, retrieval, slots, PII, LLM client)
  channels/      WhatsApp (Twilio) signature check, replies, outbound sending
  main.py        HTTP API, webhook, pages
  services.py    connects the agent logic to the database
  models.py      tables (every business row carries tenant_id)
  seed.py        demo businesses
  static/        chat widget (widget.js), demo website, admin console
tests/           core tests (stdlib) and API tests (pytest)
docs/            architecture, proposal, demo script, submission email
```

## Scope of this PoC, and what comes next

Built for the PoC: everything above. Deliberately left for the roadmap, and designed for:

- **Event streaming (Kafka/pub-sub) and Redis.** At PoC volume one service handles ingestion and
  replies synchronously. The channel adapters already normalise every message into one pipeline,
  which is the seam where a queue goes.
- **Dense embeddings (pgvector) and hybrid retrieval**, behind the same `retrieve()` interface.
- **User accounts, RBAC and SSO** instead of per-tenant keys; per-tenant encryption keys.
- **White-label UI, reseller dashboards, billing and usage metering** (RFP phase 3).
- **Messenger, SMS and CRM adapters (HubSpot, Salesforce)**, calendar sync (Google, Cal.com).
- Rate limiting, consent capture, configurable retention jobs, observability (Prometheus/Grafana).
