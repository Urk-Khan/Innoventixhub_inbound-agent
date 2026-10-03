"""
Direct Supabase REST integration for the Innoventix bot's CRM-lite needs: knowing who's
calling (for cross-sell), tracking warm leads who didn't book, and logging meetings for
reporting. Same plain-aiohttp pattern as google_sheets.py/google_calendar.py — no SDK.

Three tables (see innoventix_schema.sql for the exact DDL):

  customers        — pre-populated externally when a deal closes; this bot only READS it
                      (via get_customer_by_phone) to know purchase history for cross-sell.
                      It does not create purchase records — that's outside an inbound call
                      bot's scope. If you want the bot itself to record a NEW sale from a
                      booked meeting, that's a real feature to design, not a default here.
  leads            — the bot DOES write here (mark_potential_lead in tools.py) whenever a
                      caller shows real interest but doesn't commit.
  meetings         — an audit-trail log of every booked meeting (both sales and support),
                      written after Sheets+Calendar both succeed. Sheets/Calendar remain the
                      operational source of truth; this table is for reporting/history only.

SUPABASE_SERVICE_KEY must be the service-role key (bypasses Row Level Security) — never the
public anon key, and never exposed outside this backend.
"""

import asyncio
import os

import aiohttp
from loguru import logger

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6)  # keep it fast; caller is on hold


def _get_config() -> tuple[str, str]:
    """Reads SUPABASE_URL/SUPABASE_SERVICE_KEY fresh on every call, rather than once at
    import time. This matters because Python evaluates module-level code (including
    `os.getenv(...)` at the top of a file) the instant the module is imported — if
    `import booking_db` happens before `load_dotenv()` runs (as it did in bot.py until this
    fix), a module-level constant would be permanently frozen as "" for the life of the
    process, even though .env gets loaded correctly moments later. Reading fresh inside each
    function makes this module correct regardless of import order in whatever script uses it."""
    return os.getenv("SUPABASE_URL", "").rstrip("/"), os.getenv("SUPABASE_SERVICE_KEY", "")


def _headers(service_key: str, prefer: str = "return=representation") -> dict:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


async def get_customer_by_phone(phone: str) -> dict | None:
    """Looks up a customer by phone number for the pre-greeting context injection in
    bot.py. Returns None if not configured, not found, or on any error — bot.py treats
    None as "unknown caller" and proceeds with a plain greeting, so this fails safe."""
    supabase_url, supabase_key = _get_config()
    if not supabase_url or not supabase_key or not phone:
        return None

    url = f"{supabase_url}/rest/v1/customers?phone=eq.{phone}&select=*"

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, headers=_headers(supabase_key)) as resp:
                if resp.status >= 400:
                    logger.error(f"Customer lookup failed ({resp.status}): {await resp.text()}")
                    return None
                rows = await resp.json()
    except Exception as e:
        logger.error(f"Customer lookup request failed: {e}")
        return None

    return rows[0] if rows else None


async def create_lead(
    *,
    name: str,
    phone: str,
    interested_service: str,
    reason: str,
    call_id: str | None,
) -> dict:
    """Logs a warm lead — a caller who showed real interest but didn't book. Returns
    {"success": bool, "error": str | None}."""
    supabase_url, supabase_key = _get_config()
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in .env")
        return {"success": False, "error": "db_not_configured"}

    payload = {
        "name": name,
        "phone": phone,
        "interested_service": interested_service,
        "reason": reason,
        "status": "warm",
        "call_id": call_id,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{supabase_url}/rest/v1/leads",
                json=payload,
                headers=_headers(supabase_key, prefer="return=minimal"),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error(f"Lead insert failed ({resp.status}): {body}")
                    return {"success": False, "error": "db_insert_failed"}
    except Exception as e:
        logger.error(f"Lead insert request failed: {e}")
        return {"success": False, "error": "db_unreachable"}

    logger.info(f"Logged warm lead: {name} ({phone}) — interested in {interested_service}")
    return {"success": True, "error": None}


async def log_meeting(
    *,
    meeting_type: str,
    customer_name: str,
    phone: str,
    email: str,
    topic: str,
    date: str,
    time_str: str,
    meet_link: str | None,
    call_id: str | None,
) -> None:
    """Fire-and-forget audit log of a successfully booked meeting. Never raises — Cal.com is
    the real booking; this is reporting only, so a logging failure here should never surface
    as a booking failure to the caller."""
    supabase_url, supabase_key = _get_config()
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in .env — meeting not logged")
        return

    payload = {
        "meeting_type": meeting_type,
        "customer_name": customer_name,
        "phone": phone,
        "email": email,
        "topic": topic,
        "date": date,
        "time": time_str,
        "meet_link": meet_link,
        "call_id": call_id,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{supabase_url}/rest/v1/meetings",
                json=payload,
                headers=_headers(supabase_key, prefer="return=minimal"),
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"Meeting log insert failed ({resp.status}): {await resp.text()}")
    except Exception as e:
        logger.warning(f"Meeting log request failed: {e}")
async def _classify_transcript_with_ai(transcript: str) -> str:
    """Uses LLM to classify non-meeting call outcomes with high precision."""
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key or not transcript.strip():
        return ""

    prompt = f"""You are an outcome classifier for an inbound AI receptionist voice agent.
Analyze the conversation transcript and classify the outcome into EXACTLY one of these labels:
- "warm_lead": The caller showed purchase interest, requested an email with details/rates/link, asked for a callback or quote, or is busy right now but wants follow-up, but did NOT book a live calendar slot.
- "info_inquiry": The caller asked general questions about services, pricing, company background, website, or declined to book/said they will visit in person.
- "transferred": The caller requested to speak to a person/agent and was transferred.

Transcript:
{transcript}

Return ONLY the single word label: warm_lead, info_inquiry, or transferred."""

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=openai_key)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_completion_tokens=10,
            ),
            timeout=4.0,
        )
        res = resp.choices[0].message.content.strip().lower().replace('"', '').replace("'", "")
        if res in ("warm_lead", "info_inquiry", "transferred"):
            return res
    except Exception as e:
        logger.debug(f"AI classification failed or timed out: {e}")
    return ""


async def save_call_record(
    *,
    call_id: str | None,
    caller_phone: str | None,
    start_time: float,
    messages: list[dict],
    explicit_outcome: str | None = None,
) -> bool:
    """Fire-and-forget call log: extracts transcript, classifies outcome, and inserts into
    the call_logs table. Never raises — a logging failure must never surface as a call failure.

    `messages` is the raw LLM context message list (list of dicts with 'role' and 'content').
    Only 'user' and 'assistant' turns with non-system content are included in the transcript.
    """
    supabase_url, supabase_key = _get_config()
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in .env — call not logged")
        return False

    # Build a clean dialog transcript, skipping internal system injections.
    lines = []
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            raw_content = msg.get("content")
        else:
            role = getattr(msg, "role", "")
            raw_content = getattr(msg, "content", "")

        if raw_content is None:
            continue
        content = str(raw_content).strip()
        if not content or content == "None":
            continue
        # Skip internal context-injection messages (bracketed system notes).
        if content.startswith("[") and content.endswith("]"):
            continue
        if role == "user":
            lines.append(f"Caller: {content}")
        elif role == "assistant":
            lines.append(f"Agent: {content}")

    transcript = "\n".join(lines)
    duration = max(1, int(__import__("time").time() - start_time))

    # Determine outcome with priority:
    # 1. Explicit outcome passed by caller/tools
    # 2. Database cross-check (did this call_id create a meeting or lead?)
    # 3. Intelligent AI / heuristic classification (NEVER falsely assume meeting_booked)
    outcome = explicit_outcome

    # 1. Only a real meeting booked via Cal.com / meetings table is "meeting_booked"
    if outcome != "meeting_booked" and call_id:
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{supabase_url}/rest/v1/meetings?call_id=eq.{call_id}&select=id",
                    headers=_headers(supabase_key),
                ) as m_resp:
                    if m_resp.status == 200:
                        m_rows = await m_resp.json()
                        if m_rows:
                            outcome = "meeting_booked"
        except Exception as e:
            logger.debug(f"Meetings check skipped: {e}")

    # 2. Check if already logged as warm lead in database
    if not outcome and call_id:
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{supabase_url}/rest/v1/leads?call_id=eq.{call_id}&select=id",
                    headers=_headers(supabase_key),
                ) as l_resp:
                    if l_resp.status == 200:
                        l_rows = await l_resp.json()
                        if l_rows:
                            outcome = "warm_lead"
        except Exception as e:
            logger.debug(f"Leads check skipped: {e}")

    # 3. Check for dropped call (short call with minimal speech)
    # Preserves user's frontend mapping to Customer Support
    if not outcome and len(lines) <= 2 and duration < 25:
        outcome = "dropped_call"

    # 4. Check for live transfer
    if not outcome and (outcome == "transferred" or explicit_outcome == "transferred"):
        outcome = "transferred"

    # 5. Intelligent classification for remaining non-meeting calls
    # CRITICAL: NEVER default to meeting_booked here!
    if not outcome:
        ai_res = await _classify_transcript_with_ai(transcript)
        if ai_res:
            outcome = ai_res
        else:
            # Rule-based fallback — NEVER set meeting_booked here!
            t_lower = transcript.lower()
            if any(w in t_lower for w in ["send me", "email", "booking link", "busy right now", "call back", "quote", "proposal", "check with my", "think about it"]):
                outcome = "warm_lead"
            elif any(w in t_lower for w in ["transfer", "human", "agent", "specialist"]):
                outcome = "transferred"
            else:
                outcome = "info_inquiry"

    # 6. If outcome is warm_lead and no lead entry exists yet in leads table, auto-log it
    if outcome == "warm_lead" and call_id:
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{supabase_url}/rest/v1/leads?call_id=eq.{call_id}&select=id",
                    headers=_headers(supabase_key),
                ) as l_chk:
                    existing_leads = (await l_chk.json()) if l_chk.status == 200 else []
            if not existing_leads:
                service = "General Services"
                t_lower = transcript.lower()
                if "website" in t_lower or "web dev" in t_lower:
                    service = "Web Development & SEO" if "seo" in t_lower else "Web Development"
                elif "seo" in t_lower:
                    service = "SEO"
                elif "voice agent" in t_lower or "receptionist" in t_lower:
                    service = "AI Voice Agents"
                elif "automation" in t_lower:
                    service = "AI Automation"
                elif "content" in t_lower or "video" in t_lower:
                    service = "Content Creation"

                await create_lead(
                    name="Caller (Interested Lead)",
                    phone=caller_phone or "Unknown",
                    interested_service=service,
                    reason="Showed interest / requested follow-up during call",
                    call_id=call_id,
                )
        except Exception as e:
            logger.debug(f"Auto lead creation skipped: {e}")

    payload = {
        "call_id": call_id,
        "caller_phone": caller_phone,
        "duration_seconds": duration,
        "transcript": transcript or "No transcript captured",
        "outcome": outcome,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{supabase_url}/rest/v1/call_logs",
                json=payload,
                headers=_headers(supabase_key, prefer="return=minimal"),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(f"Call log insert failed ({resp.status}): {body}")
                    return False
                else:
                    logger.info(f"Call record saved: {outcome}, {duration}s, caller={caller_phone}, id={call_id}")
                    return True
    except Exception as e:
        logger.warning(f"Call log request failed: {e}")
        return False
