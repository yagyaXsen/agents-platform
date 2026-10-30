# Demo video script (about 3 minutes)

Record the screen at 1080p with your voice. Have two windows ready: the demo website and the admin
console (logged in as Smile Dental). Clear old chats first if you want a clean run
(`python -m app.seed --reset` locally).

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | Landing page `/` | "This is my AgentGrid proof of concept. One platform, many businesses, each with its own AI agent. Here are two demo businesses on the same deployment." |
| 0:15–0:45 | Clinic website, open chat, ask "How much is teeth whitening?" | "The agent answers from the clinic's own documents and shows the source. It's only allowed to answer from these documents." |
| 0:45–1:20 | Say "yes", answer name, need, "next week", phone, pick a slot | "Booking is a rule-based flow, so it's predictable. It qualifies the lead with the clinic's questions, understood 'next week', and books a real free slot. The same slot can't be booked twice." |
| 1:20–1:35 | Ask an unrelated question twice | "When it isn't confident it says so, and on the second miss it hands over instead of guessing." |
| 1:35–2:10 | Type "talk to a human"; switch to admin → Handover inbox; open it; reply; show reply in chat | "The conversation lands in the team's inbox with the full history and the lead details. The team replies here, and the customer sees it in the chat. On WhatsApp it goes out through Twilio." |
| 2:10–2:30 | Admin → Leads, Bookings, Overview | "Every lead is scored and saved, bookings are listed, and the dashboard shows conversion and escalation rates." |
| 2:30–2:45 | Admin → Knowledge base: add a short offer; ask about it in chat | "Businesses update the knowledge base themselves; the agent uses it immediately." |
| 2:45–3:00 | Switch to Skyline Homes, ask about brokerage | "Same platform, different business, completely separate data and its own questions. Thanks for watching. Architecture and roadmap are in the proposal." |
