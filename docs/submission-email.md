# Submission email (send Monday 2 Nov, before 1:00 pm)

**To:** rfp@thetalentgrid.co.in
**Subject:** TTG-AI-AGENTGRID | Technical Proposal Submission
**Attach:** technical proposal (PDF), company profile / portfolio (PDF)

Replace every `[...]` before sending.

---

Dear The Talent Grid team,

Please find my submission for RFP TTG-AI-AGENTGRID, AgentGrid: AI-Powered Customer Engagement and Business Automation Platform.

I have built a working proof of concept: a multi-tenant platform where each business runs its own AI agent on web chat and WhatsApp. The agent answers customers from the business's own documents with sources, qualifies leads, books appointments into real free slots, and hands the conversation to a person with the full history when it isn't confident or the customer asks. Two demo businesses run on the same deployment with fully separate data.

**Links**
- Live demo: [URL]
- Demo video (3 min): [URL]
- GitHub repository: [URL]
- Technical proposal and architecture: attached (PDF)
- Company profile: attached, and https://www.buildwithbigo.com/

**Demo access**
- Smile Dental Clinic: website and chat at [URL]/demo?tenant=smile-dental, admin console at [URL]/admin with key `sk_demo_smile_dental`
- Skyline Homes: website and chat at [URL]/demo?tenant=skyline-homes, admin key `sk_demo_skyline_homes`

The server may take up to a minute to wake on the first visit.

**A two-minute test**
1. On the clinic site, ask "How much is teeth whitening?" The answer comes from the clinic's document, with its source.
2. Reply "yes" and complete the booking. The agent qualifies you and books a free slot.
3. Type "talk to a human", then reply from the Handover inbox in the admin console. The reply appears in the customer's chat.

**Approach in brief**
- A rule-based dialogue manager for qualification, booking and handover, with the LLM used only to answer from retrieved documents
- Retrieval with sources and a confidence score; low confidence leads to handover instead of guessing
- Tenant isolation on every query, hashed admin keys, personal data redacted from logs
- One message pipeline for web and WhatsApp; Docker deployment; automated tests

I'd be glad to walk you through the code and architecture on a call.

Best regards,
Alok Kumar
Founder, bigO
[phone] · [email]
https://www.buildwithbigo.com/
