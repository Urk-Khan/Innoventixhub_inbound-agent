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


async def save_call_record(
    *,
    call_id: str | None,
    caller_phone: str | None,
    start_time: float,
    messages: list[dict],
) -> None:
    """Fire-and-forget call log: extracts transcript, classifies outcome, and inserts into
    the call_logs table. Never raises — a logging failure must never surface as a call failure.

    `messages` is the raw LLM context message list (list of dicts with 'role' and 'content').
    Only 'user' and 'assistant' turns with non-system content are included in the transcript.
    """
    supabase_url, supabase_key = _get_config()
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set in .env — call not logged")
        return

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

    # Auto-classify outcome from transcript keywords.
    t_lower = transcript.lower()
    if "meeting is confirmed" in t_lower or "booked" in t_lower or "locked that in" in t_lower:
        outcome = "meeting_booked"
    elif "warm lead" in t_lower or "mark_potential_lead" in t_lower or "let me think" in t_lower:
        outcome = "warm_lead"
    elif "transfer" in t_lower or "human" in t_lower:
        outcome = "transferred"
    elif len(lines) <= 2:
        outcome = "dropped_call"
    else:
        outcome = "info_inquiry"

    duration = int(__import__("time").time() - start_time)

    payload = {
        "call_id": call_id,
        "caller_phone": caller_phone,
        "duration_seconds": duration,
        "transcript": transcript,
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
                    logger.warning(f"Call log insert failed ({resp.status}): {await resp.text()}")
                else:
                    logger.info(f"Call record saved: {outcome}, {duration}s, caller={caller_phone}")
    except Exception as e:
        logger.warning(f"Call log request failed: {e}")
