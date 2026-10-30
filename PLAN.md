# AgentGrid PoC: plan to submission

**RFP:** TTG-AI-AGENTGRID (The Talent Grid)
**Deadline:** Monday 2 November 2026. Send by **1:00 pm IST**, not at night.
**Send to:** rfp@thetalentgrid.co.in
**Subject:** `TTG-AI-AGENTGRID | Technical Proposal Submission`

The core product is already built in this folder (agent brain, API, chat widget, admin console,
WhatsApp webhook, tests). The remaining days are for running it, adding your LLM key, deploying,
and packaging the submission.

---

## Friday 30 Oct (tonight, about 2 hours): run it on your Mac

- [ ] Open Terminal in this folder: `cd ~/Desktop/"agent grid"`
- [ ] Set up Python with uv (needs Python 3.10 or newer; uv downloads it for you):
      ```bash
      uv venv --python 3.12
      source .venv/bin/activate
      uv pip install -r requirements-dev.txt
      ```
- [ ] Run the tests: `python -m unittest tests.test_core` (26 tests) and `pytest -q` (API tests)
      → if anything fails, paste the error to Claude
- [ ] Create the demo businesses: `python -m app.seed`
- [ ] Start the server: `uvicorn app.main:app --reload`
- [ ] Try it at http://localhost:8000
      - Demo website + chat: http://localhost:8000/demo
      - Admin console: http://localhost:8000/admin (click "Demo: Smile Dental")
      - API docs: http://localhost:8000/docs
- [ ] Walk through the demo script once (docs/demo-video-script.md) to see every feature
- [ ] Create a **new private GitHub repo** `agentgrid-poc`, then:
      ```bash
      git init && git add . && git commit -m "AgentGrid PoC"
      git branch -M main
      git remote add origin https://github.com/<your-username>/agentgrid-poc.git
      git push -u origin main
      ```
      (Make it public, or add their reviewer, only when you submit.)

## Saturday 31 Oct: AI quality and WhatsApp

- [ ] Get an LLM API key (Groq has a free tier; OpenAI or Gemini also work). Copy `.env.example`
      to `.env`, fill `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Restart and check that
      http://localhost:8000/health says `"llm": "enabled"`.
- [ ] Ask 15–20 real questions per demo business. Note any wrong or weak answer and fix the
      knowledge document (admin → Knowledge base) or tell Claude what went wrong.
- [ ] Check the safety behaviour: ask something not in the documents → it should say it isn't
      sure, and the second time hand over to the team.
- [ ] WhatsApp (bonus, about 1 hour): Twilio account → Messaging → Try WhatsApp sandbox.
      You need a public URL for your local server: `ngrok http 8000` (or do this after
      deploying on Sunday). Set "When a message comes in" to
      `https://<your-url>/v1/channels/whatsapp/smile-dental`. Fill the `TWILIO_*` values in `.env`.
- [ ] Optional, only if everything above works: plug DataPilot's hybrid retrieval into
      `app/core/retrieval.py` (keep the same `retrieve()` interface).
- [ ] Commit and push.

## Sunday 1 Nov: deploy, record, write

- [ ] Deploy on Render: render.com → New → Blueprint → pick the repo (uses `render.yaml`;
      creates the app and a Postgres database). Fill the env values it asks for, with
      `PUBLIC_BASE_URL` = your Render URL.
      Note: free Render services sleep when idle, so the first request takes 30–60 seconds.
      Open the link yourself before sending, or switch to a paid instance for the evaluation week.
- [ ] Test the live link from your phone: chat, booking, handover, admin reply.
- [ ] Record the 3-minute demo video (Loom or QuickTime) following docs/demo-video-script.md.
      Upload it (Loom, YouTube unlisted, or Google Drive with link sharing on).
- [ ] Finish the proposal: fill the `[…]` placeholders in docs/technical-proposal.md,
      then export it as a PDF (ask Claude to turn it into a document you can download as PDF).
- [ ] Architecture diagrams are ready: docs/architecture-poc.png and docs/architecture-target.png
      (also rendered from docs/architecture.md on GitHub). Update them if you change the design.
- [ ] Portfolio: bigO website link + Nexora + DataPilot links, a short profile paragraph.
- [ ] Update the README "Live demo" section with the real links.

## Monday 2 Nov (morning): submit

- [ ] Open the live demo once (wake it up) and run through the chat again.
- [ ] Make the GitHub repo public (or invite their reviewer).
- [ ] Fill the links in docs/submission-email.md and send it **before 1:00 pm**.
      Attach: technical proposal PDF, company profile/portfolio PDF.
- [ ] Log the submission in your bigO outreach tracker.

---

## If you fall behind, cut in this order

1. DataPilot retrieval upgrade (the built-in retrieval already works)
2. WhatsApp (say "WhatsApp adapter built and tested; sandbox demo on request" in the proposal)
3. Postgres on Render (SQLite also works for a demo)

**Never cut:** RAG answers with sources, lead qualification, booking, human handover,
multi-tenancy, the demo video. These are what the evaluation looks at.
