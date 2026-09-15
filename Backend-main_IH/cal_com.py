"""
Cal.com v2 integration — replaces calendly.py. Same plain-aiohttp pattern as the rest of
this project, no SDK. Exposes the same three functions calendly.py did (get_open_slots,
book_slot, format_display) with the same argument/return shapes, so tools.py only needs its
import changed, not its logic.

Why the switch from Calendly: Calendly's POST /invitees (booking creation) returns 403 on
their Free plan — Standard plan or above is required. Cal.com's booking-creation endpoint
has no equivalent plan restriction, so this unblocks testing/production on a free Cal.com
account.

Auth: a Cal.com API key (Settings -> Security -> API Keys in the Cal.com dashboard). Test-mode
keys start with `cal_`, live-mode keys with `cal_live_`. Like Calendly's PAT, this doesn't
auto-refresh — it's a fixed Bearer token from .env.

One-time setup:
  1. Create your Event Type in Cal.com's UI (this project uses ONE event type for both Sales
     and Support, same as the current Calendly setup — see _resolve_event_type below if you
     later want to split them).
  2. Enable a video-conferencing location (Google Meet or Zoom) in that event type's location
     settings, the same way you would have for Calendly.
  3. Copy the event's PUBLIC BOOKING URL (e.g. https://cal.com/your-username/your-event-slug)
     into .env as CAL_BOOKING_URL. This module parses the username and event slug directly
     out of that URL — no need to separately look up a numeric event type ID.

API notes (verified against Cal.com's own v2 docs, not guessed):
  - GET /v2/slots requires a `cal-api-version: 2024-09-04` header. Response groups slots by
    date: {"data": {"2026-09-15": [{"start": "..."}], "2026-09-16": [...]}} — this is a
    different shape than Calendly's flat list, so get_open_slots flattens it here.
  - POST /v2/bookings requires a DIFFERENT pinned version header: `cal-api-version:
    2024-08-13`. Body shape: {start, attendee: {name, email, timeZone, phoneNumber,
    language}, eventTypeSlug, username}. No plan-tier restriction on this endpoint.
  - The created booking's video link can show up in a few different places depending on
    which conferencing integration is configured: top-level `meetingUrl`, nested
    `metadata.videoCallUrl`, or occasionally `location` itself. _extract_meet_link checks
    all three so this doesn't silently break if your event type's video provider changes.

Design note (same anti-reformatting principle as calendly.py): slots are identified by the
exact ISO `start` string Cal.com returns. tools.py must pass this back to book_slot EXACTLY
as received — never reconstruct or reformat it.
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
from loguru import logger

CAL_API_KEY = os.getenv("CAL_API_KEY", "")
TIMEZONE = os.getenv("TIMEZONE", "UTC")
LOOKAHEAD_DAYS = int(os.getenv("CAL_LOOKAHEAD_DAYS", "14"))

_API_BASE = "https://api.cal.com/v2"
_SLOTS_API_VERSION = "2024-09-04"
_BOOKINGS_API_VERSION = "2024-08-13"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Validate timezone at import time, same reasoning as calendly.py: catch misconfiguration in
# logs immediately rather than silently at booking time.
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    ZoneInfo(TIMEZONE)
    if TIMEZONE == "UTC":
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "TIMEZONE is set to UTC in .env — meeting slots will be displayed in UTC. "
            "If your business operates in a different timezone, set e.g. TIMEZONE=Asia/Karachi "
            "or TIMEZONE=America/New_York to show callers accurate local times."
        )
except Exception:
    import logging as _logging
    _logging.getLogger(__name__).error(
        f"TIMEZONE='{TIMEZONE}' is not a valid IANA timezone name. "
        "Defaulting display to UTC. Fix this in .env — see https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
    )
    TIMEZONE = "UTC"


def _parse_booking_url(url: str) -> tuple[str, str]:
    """Extracts (username, event_slug) from a Cal.com public booking URL, e.g.
    'https://cal.com/ubaid-persistbrands/free-consultation' -> ('ubaid-persistbrands',
    'free-consultation'). Returns ('', '') if the URL doesn't parse into exactly two path
    segments (also handles a stray trailing slash)."""
    if not url:
        return "", ""
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        logger.error(f"CAL_BOOKING_URL doesn't look like a Cal.com event booking URL: {url!r}")
        return "", ""
    # Cal.com team-event URLs can have an extra segment (team/org slug) before the event
    # slug — the event slug is always the LAST segment, the username/team is the first.
    return parts[0], parts[-1]


def _resolve_event_type(meeting_type: str) -> tuple[str, str]:
    """Returns (username, event_slug) for the given meeting_type. Currently one Cal.com
    event type is used for both 'sales' and 'support' (mirrors the current Calendly setup).
    If you later create a separate event type per meeting type, add a second env var here
    (e.g. CAL_SUPPORT_BOOKING_URL) and branch on meeting_type.lower() like calendly.py did."""
    booking_url = os.getenv("CAL_BOOKING_URL", "")
    return _parse_booking_url(booking_url)


def _headers(api_version: str) -> dict:
    api_key = os.getenv("CAL_API_KEY", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "cal-api-version": api_version,
    }


def format_display(start_time_iso: str) -> tuple[str, str]:
    """ISO datetime -> (date, time) display strings in the configured TIMEZONE, for the LLM
    to read aloud. Purely cosmetic — book_slot keys off the raw start string, not these
    strings. Reads TIMEZONE fresh (not the module-level constant) for the same reason
    booking_db.py now reads its config fresh — see that file's _get_config for the full
    explanation of why a module-level `os.getenv` constant can get permanently stuck on a
    stale default if this module is ever imported before .env is loaded."""
    from zoneinfo import ZoneInfo

    tz_name = os.getenv("TIMEZONE", "UTC")
    dt = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name))
    time_str = dt.strftime("%I:%M %p").lstrip("0") or dt.strftime("%I:%M %p")
    return dt.strftime("%Y-%m-%d"), time_str


def _extract_meet_link(booking: dict) -> str | None:
    """Cal.com's video link location varies by which conferencing integration is configured
    on the event type — check the known spots in order of how commonly they're populated."""
    meeting_url = booking.get("meetingUrl")
    if meeting_url and meeting_url.startswith("http"):
        return meeting_url
    metadata_url = (booking.get("metadata") or {}).get("videoCallUrl")
    if metadata_url:
        return metadata_url
    location = booking.get("location")
    if isinstance(location, str) and location.startswith("http"):
        return location
    return None


async def get_open_slots(meeting_type: str, limit: int = 15) -> list[dict]:
    """Returns up to `limit` open slots:
    [{"start_time": "<exact ISO string>", "date": "...", "time": "..."}, ...].
    "start_time" is what must be passed back to book_slot verbatim. "date"/"time" are just
    for the LLM to read aloud."""
    api_key = os.getenv("CAL_API_KEY", "")
    lookahead_days = int(os.getenv("CAL_LOOKAHEAD_DAYS", "14"))
    tz_name = os.getenv("TIMEZONE", "UTC")

    if not api_key:
        logger.error("CAL_API_KEY not set in .env")
        return []

    username, event_slug = _resolve_event_type(meeting_type)
    if not username or not event_slug:
        logger.error(f"No Cal.com event configured (check CAL_BOOKING_URL) for meeting_type={meeting_type!r}")
        return []

    now = datetime.now(timezone.utc)
    # Same 60-second buffer as calendly.py — avoids "start must be in the future" from clock
    # skew between our server and Cal.com's.
    start = (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=lookahead_days)).isoformat().replace("+00:00", "Z")

    url = f"{_API_BASE}/slots"
    params = {
        "eventTypeSlug": event_slug,
        "username": username,
        "start": start,
        "end": end,
        "timeZone": tz_name,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=_headers(_SLOTS_API_VERSION)) as resp:
                if resp.status >= 400:
                    logger.error(f"Cal.com availability check failed ({resp.status}): {await resp.text()}")
                    return []
                data = await resp.json()
    except Exception as e:
        logger.error(f"Cal.com availability request failed: {e}")
        return []

    # /v2/slots groups results by date: {"data": {"2026-09-15": [{"start": "..."}], ...}}.
    # Flatten in date order, then by time within each date.
    by_date = data.get("data", {})
    slots = []
    for date_key in sorted(by_date.keys()):
        for item in by_date[date_key]:
            start_iso = item.get("start")
            if not start_iso:
                continue
            date_str, time_str = format_display(start_iso)
            slots.append({"start_time": start_iso, "date": date_str, "time": time_str})
            if len(slots) >= limit:
                return slots

    return slots


async def book_slot(
    *, meeting_type: str, start_time: str, customer_name: str, phone: str, email: str, topic: str
) -> dict:
    """Books the slot at the given start_time (must be the exact ISO string from
    get_open_slots). Returns
    {"success": bool, "meet_link": str | None, "event_uri": str | None, "error": str | None}.

    Note: `topic` isn't sent to Cal.com — like calendly.py, it's logged to our own Supabase
    audit trail (booking_db.log_meeting) instead, since it has no dedicated field here
    without custom booking-form questions configured on the event type."""
    api_key = os.getenv("CAL_API_KEY", "")
    if not api_key:
        return {"success": False, "meet_link": None, "event_uri": None, "error": "cal_not_configured"}

    username, event_slug = _resolve_event_type(meeting_type)
    if not username or not event_slug:
        return {"success": False, "meet_link": None, "event_uri": None, "error": "cal_not_configured"}

    attendee = {
        "name": customer_name,
        "email": email,
        "timeZone": os.getenv("TIMEZONE", "UTC"),
        "language": "en",
    }
    if phone:
        attendee["phoneNumber"] = phone

    payload = {
        "start": start_time,
        "attendee": attendee,
        "eventTypeSlug": event_slug,
        "username": username,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{_API_BASE}/bookings", json=payload, headers=_headers(_BOOKINGS_API_VERSION)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error(f"Cal.com booking failed ({resp.status}): {body}")
                    return {"success": False, "meet_link": None, "event_uri": None, "error": "booking_failed"}
                data = await resp.json()
    except Exception as e:
        logger.error(f"Cal.com booking request failed: {e}")
        return {"success": False, "meet_link": None, "event_uri": None, "error": "cal_unreachable"}

    booking = data.get("data", {})
    meet_link = _extract_meet_link(booking)
    event_uid = booking.get("uid")

    logger.info(f"Booked {meeting_type} meeting via Cal.com: {customer_name} at {start_time}")
    return {"success": True, "meet_link": meet_link, "event_uri": event_uid, "error": None}
