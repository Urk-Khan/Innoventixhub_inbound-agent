# Innoventix Hub — Inbound AI Voice Agent System

A production-grade, end-to-end inbound voice agent system and management console built for **Innoventix Hub** ([innoventixhub.tech](https://innoventixhub.tech)).

This repository contains both the real-time AI voice receptionist backend and the administrative dashboard frontend:

```text
Innoventixhub_inbound-agent/
├── Backend-main_IH/      # Python Pipecat AI Voice Agent, Telephony & Integrations
├── Frontend-main_IH/     # Next.js 14 Management Console & Analytics Dashboard
└── README.md             # Monorepo documentation
```

---

## Architecture & Subsystems

```
                               ┌─────────────────────────────────────────┐
                               │           Inbound Caller                │
                               │        (+18555010702 / Telnyx)          │
                               └────────────────────┬────────────────────┘
                                                    │
                                                    ▼
                               ┌─────────────────────────────────────────┐
                               │   Backend-main_IH (Python Pipecat)      │
                               │   • Cartesia Real-Time STT & TTS        │
                               │   • OpenAI / Anthropic LLM Reasoning    │
                               │   • Telnyx Live Call Transfer Bridge    │
                               │   • Cal.com Slot Checking & Booking     │
                               │   • Resend Confirmation Emails          │
                               └────────────────────┬────────────────────┘
                                                    │
                                                    ▼
                               ┌─────────────────────────────────────────┐
                               │         Supabase Cloud Database         │
                               │   • call_logs  • leads  • meetings      │
                               └────────────────────┬────────────────────┘
                                                    │
                                                    ▼
                               ┌─────────────────────────────────────────┐
                               │   Frontend-main_IH (Next.js Dashboard)  │
                               │   • Live Call Transcripts & Filters     │
                               │   • Warm Leads Pipeline Management      │
                               │   • Meeting Audit Logs & Metrics        │
                               └─────────────────────────────────────────┘
```

---

## Modules

### 1. [Backend Service (`Backend-main_IH`)](./Backend-main_IH)
The core Python voice agent powered by **Pipecat 1.8.1**:
- **Sub-Second Telephony Pipeline:** Built on Telnyx WebSockets, Cartesia Ink-Whisper STT and Sonic TTS with Silero VAD.
- **Smart Receptionist ("Clara"):** Answers inquiries on AI Automation, Web Dev, Mobile Apps, and SEO.
- **Cal.com Integration:** Live calendar availability checking and meeting scheduling with auto-generated Google Meet links.
- **Live Human Transfer:** Immediate call bridging to a human specialist (`+19177954404`) via Telnyx Call Control API.
- **AI Outcome Classifier:** High-precision call classification (`meeting_booked`, `warm_lead`, `info_inquiry`, `transferred`, `dropped_call`).
- **Automated Cloudflare Tunneling:** One-click startup with dynamic webhook registration (`auto_tunnel.py`).

👉 See [`Backend-main_IH/README.md`](./Backend-main_IH/README.md) for installation and runtime instructions.

---

### 2. [Frontend Dashboard (`Frontend-main_IH`)](./Frontend-main_IH)
The administrative console built with **Next.js 14 (App Router)** and **TypeScript**:
- **Call Analytics:** Rolling 24-hour call metrics, outcome distributions, and searchable call logs with full conversational transcripts.
- **Leads Manager:** Pipeline management for warm leads captured by Clara (status updates: warm, contacted, converted, cold).
- **Bookings Viewer:** Audit trail of all Cal.com scheduled consultations with direct meeting links.
- **Authentication:** Secure cookie-based password gate protecting caller data and transcripts.

👉 See [`Frontend-main_IH/README.md`](./Frontend-main_IH/README.md) for dashboard setup and deployment instructions.

---

## Quick Start

### Starting the Voice Agent Backend
```powershell
cd Backend-main_IH
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # Configure API keys
python bot.py
```

### Starting the Management Dashboard
```powershell
cd Frontend-main_IH
npm install
cp .env.local.example .env.local   # Configure Supabase credentials
npm run dev
```

---

## Security & Best Practices
- Never commit active `.env` or `.env.local` files containing secrets.
- Audio recordings (`*.wav`) and raw runtime logs are excluded via `.gitignore`.
- Telephony webhooks and API routes use validated endpoints.

---

## License
Proprietary — Developed for **Innoventix Hub** ([innoventixhub.tech](https://innoventixhub.tech)).
