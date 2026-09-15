# Innoventix Hub Voice Agent — Project Documentation

This is the living reference for how this project works and why it's built the way it is.
**Update the Changelog section at the bottom every time you make a real change.**

---

## 1. What this is

An inbound AI voice agent for Innoventix Hub that handles four real call scenarios, not just
a single scripted flow:

1. **Info / general support** — answered directly from the knowledge base, no tools needed.
2. **Sales / purchase interest** — books a meeting with the team.
3. **Cross-sell** — for existing customers, the bot may pitch one complementary service
   based on what they've already bought, then folds into scenario 2 if they're interested.
4. **Support escalation** — when an existing customer has an issue the bot genuinely can't
   resolve, it books a meeting (a *different* meeting type than sales) rather than leaving
   them stuck.

There's also a fifth outcome that isn't a "scenario" exactly: a caller who shows real
interest but doesn't commit gets logged as a **warm lead** for the sales team to follow up,
instead of just being lost.

## 2. Architecture

```
Caller's phone
      |
      v
   Telnyx  --(TeXML)-->  bot.py (Pipecat runner, /ws)
      |                        |
      |                        +--> booking_db.get_customer_by_phone()  [BEFORE greeting]
      |                        |      (Supabase -- is this a known customer?)
      |                        |
      |<-- audio in/out ------>|
                                |
                  +-------------+-------------+
                  |      cascade pipeline      |
                  |  Cartesia STT               |
                  |  -> LLM (OpenAI/Anthropic)  |
                  |  -> Cartesia TTS            |
                  +-------------+-------------+
                                |
                     tool calls from the LLM
                                |
        +-----------+-----------+-----------+--------------+
        v           v           v           v              v
  google_sheets  google_     email_       booking_db    (transfer_to_human,
  .py (slots)    calendar    sender.py    .py (leads +   end_call -- no
                 .py (real   (Resend      audit log)     external call)
                 avail. +    confirmation)
                 Meet link)
```

No N8N — every integration above is a direct API call from Python via `aiohttp` (or
`google-auth` for Google's token exchange, which itself just returns a bearer token that
`aiohttp` then uses).

## 3. File-by-file

| File | Role |
|---|---|
| `bot.py` | Builds the pipeline for one call AND is the launcher. Also does the pre-greeting customer lookup. |
| `prompts.py` | The system prompt — all four scenarios, lead tracking, tone, knowledge base |
| `tools.py` | `check_availability`, `book_meeting`, `mark_potential_lead`, `transfer_to_human`, `end_call` |
| `google_sheets.py` | Slot truth per meeting type (Sales/Support tabs), intersected with real Calendar availability |
| `google_calendar.py` | freeBusy checks + event creation (this is what generates the Meet link) |
| `email_sender.py` | Confirmation email via Resend |
| `booking_db.py` | Supabase: customer lookup (cross-sell), lead logging, meeting audit log |
| `innoventix_schema.sql` | The three Supabase tables this needs (run once) |
| `latency_logger.py` | Logs STT/LLM/TTS time-to-first-byte per turn |
| `warmup.py` | Preloads VAD + warms Cartesia's connection path at startup |
| `auto_tunnel.py` | Starts `cloudflared` and updates Telnyx automatically |
| `list_texml_apps.py` | Finds your `TELNYX_TEXML_APP_ID` |
| `test_console_chat.py` | Plain-text prompt testing, no audio/Telnyx (doesn't exercise tools) |

## 4. How each scenario actually runs

**Scenario 1 (info/support)**: no tools — the LLM answers straight from `prompts.py`'s
knowledge base section.

**Scenario 2 (sales)**: `check_availability(meeting_type="sales")` calls
`google_sheets.get_open_slots("sales")`, which reads the `Sales_Slots` tab and cross-checks
each candidate against `GOOGLE_CALENDAR_ID_SALES` via `google_calendar.is_slot_free()`
(concurrently, not one at a time — see the comment in `get_open_slots`), returning the
intersection. Caller picks a time; `book_meeting(meeting_type="sales", ...)` writes the
Sheet row, creates the Calendar event (which is what generates the Meet link), sends the
confirmation email, and logs the meeting to Supabase.

**Scenario 3 (cross-sell)**: driven entirely by `bot.py`'s pre-greeting lookup plus
`prompts.py`'s pairing table (Automation to Voice Agents, etc. — see prompts.py; this
pairing is a default guess, not confirmed business logic). If accepted, becomes Scenario 2.

**Scenario 4 (support escalation)**: same booking flow as Scenario 2, but
`meeting_type="support"` — different Sheet tab, different Calendar, and the prompt requires
a *specific* topic (the actual issue) rather than a generic label, since the rep needs to
know what's wrong before the call.

**Warm lead outcome**: `mark_potential_lead` calls `booking_db.create_lead()`, writing to
the `leads` table with `status="warm"`. Only for genuine hesitation, not outright rejection
— see `prompts.py`'s LEAD TRACKING section for the exact instruction.

## 5. Required setup (three separate services)

### Google Sheets + Calendar (one service account covers both)
1. Google Cloud Console — enable **both** the Sheets API and the Calendar API.
2. Create a Service Account, download its JSON key, point `GOOGLE_SHEETS_CREDENTIALS_PATH` at it.
3. Create your spreadsheet with two tabs (or one, if Sales/Support share availability):
   `Sales_Slots` and `Support_Slots`. Row 1 = headers, data from row 2:

   | Date | Time | Status | Booked By | Phone | Email | Topic | Booked At |
   |---|---|---|---|---|---|---|---|
   | 2026-09-15 | 10:00 AM | Open | | | | | |

   **Date/time format matters**: `google_calendar.py` parses these with
   `"%Y-%m-%d %I:%M %p"` — stick to `YYYY-MM-DD` and `H:MM AM/PM` exactly, or availability
   checks will silently skip malformed rows (logged as a warning, not a crash).
4. Share the spreadsheet with the service account's email (in the JSON key file) as **Editor**.
5. Create/choose the Google Calendar(s) for Sales and Support reps. Get each calendar's ID
   (Calendar Settings, "Integrate calendar", Calendar ID) into `.env`. **Share each
   calendar with the same service account email**, with "Make changes to events" permission
   — this is separate from sharing the spreadsheet, easy to miss.
6. Set `TIMEZONE` in `.env` to your actual IANA timezone. Get this wrong and every booked
   meeting lands at the wrong time on everyone's calendar.

### Email (Resend)
1. Sign up at resend.com, get an API key.
2. For testing: `RESEND_FROM_ADDRESS=onboarding@resend.dev` works without domain
   verification. For production: verify your own domain in Resend first.

### Supabase
1. Run `innoventix_schema.sql` in the SQL editor — creates `customers`, `leads`, `meetings`.
2. Fill in `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` (service role, not anon) in `.env`.
3. **Pre-populate `customers` manually** as deals close — this bot has no way to know a
   purchase happened on its own; it only reads what's already there.

## 6. Known limitations / open items

- **Phone number matching is exact-string**: `get_customer_by_phone` does an exact match
  against whatever Telnyx reports as the caller's number (usually E.164, e.g.
  `+442012345678`). If your `customers` table has numbers in a different format, lookups
  will silently fail to match. Worth normalizing both sides consistently if this becomes an
  issue.
- **The cross-sell pairing table in `prompts.py` is a default guess**, not confirmed
  business logic (Automation to Voice Agents was the one explicit example given; the other
  three pairings were inferred). Revisit if the sales team has a different view.
- **`transfer_to_human` still only speaks a handoff line** — no real SIP transfer wired in.
- Double-booking protection (both Sheet-level in `book_slot`, and the Sheet-vs-Calendar
  rollback in `release_slot`) is best-effort, not atomic — fine at single-line call volume.
- No admin dashboard, no production VPS deployment yet.

---

### 2026-09-15 — Comprehensive Deep Testing & Final Verification of All Call Scenarios

Conducted a deep audit and automated verification across all system components, integrations, and call paths:

1. **`test_scenario.py` Rebuild & Expansion**: Replaced out-of-date Calendly references with Cal.com (`cal_com.py`), updated tool schemas to include `send_booking_link`, and expanded the test suite from 3 basic flows to **7 comprehensive scenarios**:
   - **Scenario A (Sales Booking)**: `check_availability` -> user slot selection -> `book_meeting` -> Cal.com booking -> Resend confirmation email -> Supabase meeting audit log.
   - **Scenario B (Warm Lead)**: Caller hesitates -> `mark_potential_lead` -> Supabase `leads` table logged as `status="warm"`.
   - **Scenario C (General Info Inquiry)**: Prompt knowledge base Q&A -> direct conversational answer with zero tool overhead.
   - **Scenario D (Cross-sell - Known Customer)**: Context injection on caller lookup -> natural pitch for complementary service (AI Voice Agents) -> transition to sales booking.
   - **Scenario E (Support Escalation - Known Customer)**: Technical issue gap -> `check_availability(meeting_type="support")` -> `book_meeting` with specific issue topic -> support meeting booked.
   - **Scenario F (Booking Link Request)**: Caller requests link by email -> `send_booking_link` -> fallback email sent without touching calendar.
   - **Scenario G (Immediate Human Transfer)**: Urgent production failure -> `transfer_to_human`.

2. **Fixed `booking_db.py` Call Log Transcript Formatting**: `save_call_record` previously converted `msg.get("content")` to `str()` directly, turning `None` content (typical of assistant tool-call messages) into the literal string `"None"` in saved Supabase call transcripts. Added an explicit `if raw_content is None: continue` check.

3. **Guarded `_normalize_email()` in `tools.py`**: Added explicit null/non-string checks (`if not raw: return ""`) to prevent `AttributeError` if `email` is omitted or `None` in tool parameters.

4. **Upgraded `test_console_chat.py`**: Added full live function-calling tool execution to terminal chat mode so interactive console testing matches the full voice pipeline capability.

### 2026-09-15 — Fixed Supabase tables (leads, meetings, call_logs) staying permanently empty

Reported symptom: leads, meetings, and call_logs tables all stayed empty despite calls
happening and (separately) emails sending correctly — which was the key clue, since it
meant credentials in `.env` were fine and being loaded correctly by SOMETHING, just not by
`booking_db.py`.

**Root cause**: `booking_db.py` read `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` as **module-level
constants** — `SUPABASE_URL = os.getenv(...)` at the top of the file, evaluated exactly once,
the instant the module is imported. In `bot.py`, `import booking_db` happened at line 68,
while `load_dotenv(override=True)` didn't run until line 70 — two lines later. Since Python
executes a module's top-level code immediately on import, `booking_db.py`'s constants got
permanently frozen as empty strings before `.env` was ever loaded, for the entire lifetime of
the process. Every function's `if not SUPABASE_URL or not SUPABASE_SERVICE_KEY: return`
guard then silently short-circuited forever — including `get_customer_by_phone`, meaning the
known-caller cross-sell context injection had also likely never fired, a second symptom that
hadn't been noticed yet.

`email_sender.py` had the exact same pattern (module-level `RESEND_API_KEY`/
`RESEND_FROM_ADDRESS`) but happened not to exhibit the bug in the version tested, because
whichever script/order was used at the time loaded `.env` before importing it — this is
exactly the kind of bug that depends on import order and can appear to "work" in one script
and silently fail in another that imports the same modules in a different sequence.

**Fix** (defense in depth, three layers):
1. `booking_db.py` and `email_sender.py` now read their env vars fresh inside every function
   (`_get_config()` helper in booking_db.py) instead of once at import time — matching the
   pattern `cal_com.py`'s API-key reads already used correctly. This makes both modules
   correct regardless of import order, in this script or any other that imports them.
2. `cal_com.py`'s `TIMEZONE` constant had the identical latent issue (used directly in
   `format_display`/`get_open_slots`/`book_slot` instead of re-read) — fixed the same way.
3. `bot.py` now calls `load_dotenv(override=True)` immediately after importing `dotenv`,
   before any of the project's own modules are imported, removing the root cause outright
   rather than relying solely on every module being individually import-order-safe.

Also added error logging to `log_meeting`/`save_call_record`'s "not configured" early
returns — previously these failed completely silently (no log line at all), unlike
`create_lead` which did log an error. All three now log clearly if Supabase isn't reachable,
so this class of bug is visible in the logs immediately next time rather than requiring a
line-by-line code read to find.

### 2026-09-14 — Fixed premature/unconfirmed bookings and abrupt call-ending

Live-call report: "books the slot without confirmation, ends the call without confirmation,
sends the email but doesn't follow the script." Root cause was in `prompts.py`, not
`cal_com.py` — the scheduling client itself was fine.

**The bug**: the MEETING BOOKING section's instruction for "caller wants a link" / "no slot
fits" told the LLM to silently pick the FIRST available slot from `check_availability` and
call `book_meeting` with it, purely to trigger an email. Unlike the earlier Calendly setup
(which was blocked from completing this by the Free-plan 403), Cal.com's booking endpoint
has no such restriction, so this instruction went all the way through: a real, specific
meeting got booked at an arbitrary time the caller never chose or heard, a real confirmation
email went out for it, and then the GENERAL RULES' "end_call after a booking" trigger (which
also contradicted `end_call`'s own tool docstring — "once the caller has said goodbye") fired
immediately afterward with no "anything else?" check.

**Fix**: added a new `send_booking_link` tool (`tools.py`) that only calls
`email_sender.send_booking_fallback_email` — it never touches the calendar. Rewrote the
prompt's "caller wants a link" case to call that instead of faking a booking. Rewrote the
GENERAL RULES end_call bullet to require asking "anything else?" and waiting for the
caller's reply before ever calling `end_call`, matching what `end_call`'s own docstring
already said.

Also fixed two stale doc comments that could mislead future edits: `email_sender.py`'s
top-of-file comment still claimed the module was inactive in favor of Cal.com's native
emails, when both its functions are called unconditionally from `tools.py`. `bot.py`'s
architecture comment still described the long-abandoned Google Sheets/Calendar path instead
of Cal.com.

### 2026-09-14 — Scheduling backend swapped to Cal.com (was Calendly; before that, Cal.com; before that, Google Sheets+Calendar)

Calendly integration replaced with `cal_com.py` — same interface (`get_open_slots`,
`book_slot`, `format_display`) so `tools.py` only needed its import changed, not its logic.
Reason for the swap: Calendly's `POST /invitees` (booking creation) requires Standard plan
or above (403 on Free); Cal.com's booking-creation endpoint has no equivalent plan
restriction.

Verified directly against Cal.com's own v2 docs before writing code (same discipline as the
Calendly swap): `GET /v2/slots` requires a `cal-api-version: 2024-09-04` header and groups
results by date (`{"data": {"2026-09-15": [{"start": "..."}]}}`) — a different shape than
Calendly's flat list, so `get_open_slots` flattens it. `POST /v2/bookings` requires a
DIFFERENT pinned version header (`cal-api-version: 2024-08-13`) and a distinct body shape
(`attendee` object + `eventTypeSlug`/`username` resolved from the event's public booking
URL, rather than a numeric event type ID). The created booking's video link location varies
by conferencing integration (`meetingUrl`, `metadata.videoCallUrl`, or `location`) —
`_extract_meet_link` checks all three.

**Design note**: one Cal.com event type is used for both Sales and Support (mirrors the
prior one-event Calendly setup), resolved from a single `CAL_BOOKING_URL` env var. If a
separate Support event type is created later, `_resolve_event_type` in `cal_com.py` is where
to branch on `meeting_type`.

Calendly, Google Sheets/Calendar are now all parked (files kept, unused) — the project has
cycled through four scheduling backends. `calendly.py` is untouched and can be swapped back
in by reverting `tools.py`'s import if Cal.com doesn't work out either.

**Security note (recurring)**: the real `.env` was included in this upload's zip again. Same
recommendation as before — consider a pre-zip checklist or gitignore-aware zip tooling
(`git archive`) rather than a raw folder zip.

### 2026-09-12 — Scheduling backend swapped to Calendly (was Cal.com; before that, Google Sheets+Calendar)

Cal.com integration replaced with `calendly.py`. Verified three endpoints directly against
Calendly's own docs before writing any code, since a scheduling integration is exactly the
kind of thing that's easy to get subtly wrong: `GET /event_type_available_times` (returns
`{status, start_time, scheduling_url}` per slot — confirmed exact response shape),
`POST /invitees` (the actual booking-creation call — confirmed request/response shape and,
critically, confirmed it returns 403 on Calendly's Free plan; **Standard plan or above is
required**, not optional, for this integration to work at all), and `GET /scheduled_events/
{uuid}` (where the video-conferencing `location.join_url` lives — separate call needed after
booking, since the invitee-creation response doesn't include it directly).

**Design change from the Sheets-based approach**: slots are now identified by Calendly's own
`start_time` ISO string (returned by their API), not a separately-formatted date/time pair
we constructed ourselves. `check_availability` returns both a display-friendly date/time (for
the LLM to read aloud) and the raw `start_time` (which must be passed back to `book_meeting`
verbatim) — same anti-reformatting principle as before, different key.

**`tools.py` was missing from the uploaded zip** this session (only a stale compiled `.pyc`
remained) — rebuilt from what was directly viewed earlier in the same conversation, not
guessed. Rebuild kept the phone-auto-fill design (never ask the caller for their number,
`app_resources["caller_phone"]` fills it automatically) and added the same pattern to
`interested_service`/topic-style fields.

**Google Sheets/Calendar and Cal.com are now BOTH parked** (files kept, unused) — the project
has cycled through three scheduling backends. If Calendly doesn't work out either, the
Google implementation is the most thoroughly load-bearing tested of the three (verified
freeBusy intersection logic, verified `conferenceDataVersion=1` requirement, verified
timezone math) and is the recommended fallback over rebuilding Cal.com.

**Security note (recurring)**: the real `.env` was included in this upload's zip again
(third time this has happened across both projects). `.gitignore` protects it from git, but
not from being bundled into a zip and shared directly. If this keeps happening, consider a
pre-zip checklist step, or gitignore-aware zip tooling (e.g. `git archive` instead of a raw
folder zip) — worth setting up once rather than manually remembering every time.

### 2026-09-10 — Full scenario rebuild: sales/support meeting types, Calendar+Meet+email, cross-sell, warm leads

Rebuilt around four real call scenarios (info/support, sales, cross-sell, support
escalation) plus a fifth outcome (warm-lead logging for callers who don't commit), replacing
the single generic "book a meeting" flow from the initial build.

**New: `google_calendar.py`** — real availability checking (freeBusy) and event creation.
This is also how the Google Meet link actually gets generated: it's not a separate API,
it's `conferenceData.createRequest` on the event-insert call, and requires
`?conferenceDataVersion=1` on the request URL or the conference request is silently ignored
with no link returned — verified this behavior directly before relying on it.

**New: `email_sender.py`** — Resend for confirmation emails. Chosen over the Gmail API
specifically to avoid domain-wide delegation setup; swappable later if a real Innoventix
Gmail address is wanted instead (`email_sender.py` is the only file that would change).

**New: `booking_db.py`** — Supabase for three things: reading customer purchase history
(cross-sell), writing warm leads, and an audit-trail log of booked meetings. The bot never
writes purchase history itself — that table is populated externally when a deal closes.

**`google_sheets.py` restructured** for meeting-type-scoped tabs (`Sales_Slots`/
`Support_Slots`, configurable to share one tab) and now cross-checks Sheet-open slots
against real Calendar availability concurrently (not sequentially — a serial per-candidate
freeBusy check would add real latency while the caller's on hold). Verified this
intersection logic directly with mocked Sheet/Calendar responses (a slot marked Open in the
Sheet but busy on the Calendar is correctly excluded) before shipping.

**New: `release_slot()` in `google_sheets.py`** — if a Sheet booking succeeds but the
subsequent Calendar event creation fails, the slot would otherwise be stuck "Booked" with no
real meeting behind it. This reverts it back to Open.

**`bot.py`**: `on_client_connected` now looks up the caller by phone via
`booking_db.get_customer_by_phone()` before the greeting fires, injecting what it finds into
the LLM's context — this is what makes the cross-sell scenario possible (the LLM needs to
know purchase history from turn one, not discover it reactively).

**`tools.py` rewritten**: `search_location`/cab-specific tools from the template this project
started from are gone (wrong domain). New tools: `check_availability(meeting_type)`,
`book_meeting(meeting_type, ...)` (orchestrates Sheets, Calendar, Email, DB log, with
rollback on partial failure), `mark_potential_lead(...)`.

**`prompts.py` rewritten** around the four scenarios, with an explicit (default, unconfirmed)
cross-sell pairing table and clear rules for when to log a lead vs. not.

**Defaults chosen without an explicit answer from you — flag if wrong**: separate Sheet tabs
and Calendar IDs per meeting type (same value works for both if it's one team); Resend for
email over Gmail API.
