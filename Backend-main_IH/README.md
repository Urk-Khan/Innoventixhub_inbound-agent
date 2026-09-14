# Innoventix Hub — Inbound AI Voice Agent

Handles four call scenarios — info/support, sales, cross-sell, and support escalation — plus
warm-lead tracking for callers who don't commit. Books meetings via Google Sheets +
Calendar (which generates a real Google Meet link) and sends confirmation emails via Resend.
Cartesia (STT+TTS) + OpenAI/Anthropic (LLM) + Telnyx (telephony) + Supabase (CRM-lite). No
N8N — every integration is a direct API call from Python.

**For architecture, the full scenario breakdown, and the running changelog, see
[`PROJECT_DOCUMENTATION.md`](./PROJECT_DOCUMENTATION.md).** This README is just the "get it
running" steps.

## 1. Install

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

## 2. Set up the three integrations

Follow `PROJECT_DOCUMENTATION.md` section 5 for each:
- **Google Sheets + Calendar** — one service account, two APIs enabled, spreadsheet with
  `Sales_Slots`/`Support_Slots` tabs, calendar(s) shared with the service account
- **Resend** — API key + a from-address
- **Supabase** — run `innoventix_schema.sql`, fill in the URL/service key

## 3. Fill in the rest of `.env`

```
TELNYX_API_KEY=...
TELNYX_ACCOUNT_SID=...
TELNYX_TEXML_APP_ID=...      # see step 5 below for how to get this
CARTESIA_API_KEY=...
CARTESIA_VOICE_ID=...
LLM_PROVIDER=openai
OPENAI_API_KEY=...
TIMEZONE=Europe/London        # or whatever's actually correct — this matters
```

## 4. Test without a phone call

```powershell
python test_console_chat.py
```

Plain-text chat — validates your LLM provider and the knowledge-base/scenario prompt. This
does NOT exercise the booking/lead tools (no `tools=` passed in text-mode) — use a real call
via `python bot.py` to test the full flow.

## 5. One-time Telnyx portal setup

1. **Buy a number** — Portal → Numbers → Buy Numbers.
2. **Create a TeXML Application** — Portal → Call Control → TeXML → "Add new TeXML app".
   Leave the Voice URL blank — `bot.py` sets it automatically every run.
3. **Assign the number** — Portal → Numbers → My Numbers → your number → Connection/App =
   the TeXML Application from step 2.
4. **Get the App's ID into `.env`**: `python list_texml_apps.py`, copy the `id` into
   `TELNYX_TEXML_APP_ID`.

## 6. Run it

```powershell
python bot.py
```

or double-click `start_bot.bat`. One command: starts `cloudflared`, updates Telnyx
automatically, pre-warms VAD/Cartesia, starts the server. Call your number — every turn logs
a `[LATENCY]` line.

## Testing each scenario

- **Info/support**: ask a general question about the four services.
- **Sales**: say you want to buy a service or discuss pricing — should offer a meeting.
- **Cross-sell**: add a row to Supabase's `customers` table with your test caller's phone
  number and `purchased_services="AI Automation"`, then call — the bot may naturally pitch
  AI Voice Agents at some point.
- **Support escalation**: as the same "known customer," ask something the bot genuinely
  can't answer — should offer a *support* meeting, not sales.
- **Warm lead**: express real interest in a service, then decline to pick a time ("let me
  think about it") — check the `leads` table in Supabase afterward.

## Troubleshooting

- **"An error has occurred" on the call, nothing in the terminal** → check `cloudflared` is
  running, `python list_texml_apps.py` shows today's tunnel URL, and the number is assigned
  to the right app.
- **Booking fails every time** → check the Sheet is shared with the service account as
  Editor, the Calendar is shared with the service account too (separate step, easy to miss),
  and your Date/Time format in the Sheet matches `YYYY-MM-DD` / `H:MM AM/PM` exactly.
- **No Meet link in the confirmation** → check the logs for "Event created but no Meet link
  came back" — usually means `conferenceDataVersion=1` isn't reaching the API correctly, or
  the calendar itself doesn't support auto-generated Meet links (some Workspace admin
  policies restrict this).
- **Cross-sell never happens** → confirm the caller's phone number in `customers` matches
  exactly what Telnyx reports (check the logs for the `From number` line) — format
  mismatches (e.g. missing `+`) will silently fail to match.
