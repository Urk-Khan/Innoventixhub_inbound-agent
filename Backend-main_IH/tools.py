"""
Function-calling tools exposed to the LLM (Pipecat 1.8.1 API).

Pipecat auto-generates each tool's schema from its name, type hints, and docstring — list
these on the LLMContext: LLMContext(tools=INNOVENTIX_TOOLS).

Scheduling backend: Cal.com (cal_com.py) — replaces Calendly (calendly.py is now parked,
same as the earlier Google Sheets/Calendar attempt before it, and email_sender.py). Easy to
revert to either later without rebuilding from scratch, since all three keep the same
get_open_slots/book_slot/format_display interface.

No N8N anywhere — every tool talks to its backend directly via aiohttp.
"""

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame
from pipecat.services.llm_service import FunctionCallParams

import os
import re

import booking_db
import cal_com as scheduler
import email_sender


def _normalize_email(raw: str) -> str:
    """Convert spoken email transcriptions to valid addresses.
    Handles forms like:
      'john dot doe at gmail dot com' -> 'john.doe@gmail.com'
      'alex underscore smith at acme dot co dot uk' -> 'alex_smith@acme.co.uk'
      'j-o-h-n at company dot com' -> 'john@company.com'
    Strips accidental whitespace throughout.
    """
    if not raw:
        return ""
    s = str(raw).lower().strip()
    # Replace spoken punctuation
    s = re.sub(r"\s+dot\s+", ".", s)       # "dot" -> "."
    s = re.sub(r"\s+at\s+", "@", s)        # "at" -> "@"
    s = re.sub(r"\s+underscore\s+", "_", s) # "underscore" -> "_"
    s = re.sub(r"\s+dash\s+", "-", s)      # "dash" -> "-"
    # Remove spaces between spelled-out letters (e.g. "j o h n" -> "john")
    # Only do this for single-char segments between spaces, not whole words
    s = re.sub(r"(?<=\b[a-z])\s+(?=[a-z]\b)", "", s)
    # Strip any remaining whitespace
    s = s.replace(" ", "")
    return s


async def check_availability(params: FunctionCallParams, meeting_type: str):
    """Look up currently open meeting slots. Call this whenever the caller is ready to book
    a meeting, before offering any specific times. Returns a short list of open date/time
    options — present 2-3 of them naturally, don't read the whole list.

    Args:
        meeting_type: "sales" for anything involving buying a service, a cross-sell pitch,
            or general purchase interest. "support" only when the caller has an unresolved
            issue with a service they already have that you (the AI) genuinely could not
            answer from your own knowledge — not for routine questions you already handled.
    """
    slots = await scheduler.get_open_slots(meeting_type, limit=15)

    if not slots:
        await params.result_callback({"success": True, "slots": []})
        return

    await params.result_callback(
        {
            "success": True,
            "slots": [
                {"date": s["date"], "time": s["time"], "start_time": s["start_time"]}
                for s in slots
            ],
        }
    )


async def book_meeting(
    params: FunctionCallParams,
    meeting_type: str,
    start_time: str,
    customer_name: str,
    email: str,
    topic: str,
    phone: str = "",
):
    """Book a meeting once the caller has picked a specific open time and confirmed their
    details. `start_time` MUST be copied EXACTLY as it came back from check_availability's
    results — it's an internal identifier, don't reformat it, don't construct your own.

    Args:
        meeting_type: "sales" or "support" — same value used in the check_availability call
            that produced this start_time.
        start_time: The exact start_time string from check_availability's results.
        customer_name: Caller's name.
        email: Caller's email address — required, Cal.com's confirmation and the video
            link go here.
        topic: A SPECIFIC one-line summary of what will be discussed — e.g. "AI Automation
            pricing for a 10-person sales team" or "Invoice #4521 billing discrepancy", not
            a generic "sales call" or "support issue". This is what the rep sees before the
            meeting, so vague topics leave them unprepared.
        phone: Caller's phone number. Leave blank — the system already has it from caller
            ID, don't ask the caller for it.
    """
    app_resources = params.app_resources or {}
    call_id = app_resources.get("session_id")
    caller_phone = phone or app_resources.get("caller_phone") or ""
    email = _normalize_email(email)

    if not start_time:
        booking_url = os.getenv("CAL_BOOKING_URL", "")
        fallback_sent = False
        if email and booking_url:
            fb = await email_sender.send_booking_fallback_email(
                to=email,
                customer_name=customer_name,
                booking_url=booking_url,
            )
            fallback_sent = fb.get("success", False)
        await params.result_callback({
            "success": False,
            "error": "missing_start_time",
            "fallback_link_sent": fallback_sent,
        })
        return

    result = await scheduler.book_slot(
        meeting_type=meeting_type,
        start_time=start_time,
        customer_name=customer_name,
        phone=caller_phone,
        email=email,
        topic=topic,
    )

    if not result["success"]:
        # Fallback: if we have the caller's email, send them a direct booking link so they
        # can still book themselves even when the API booking fails.
        booking_url = os.getenv("CAL_BOOKING_URL", "")
        fallback_sent = False
        if email and booking_url:
            fb = await email_sender.send_booking_fallback_email(
                to=email,
                customer_name=customer_name,
                booking_url=booking_url,
            )
            fallback_sent = fb["success"]

        await params.result_callback({
            "success": False,
            "error": result["error"],
            "fallback_link_sent": fallback_sent,
        })
        return

    date_str, time_str = scheduler.format_display(start_time)
    await booking_db.log_meeting(
        meeting_type=meeting_type,
        customer_name=customer_name,
        phone=caller_phone,
        email=email,
        topic=topic,
        date=date_str,
        time_str=time_str,
        meet_link=result["meet_link"],
        call_id=call_id,
    )

    # Fire-and-forget confirmation email — never fails the booking if email fails.
    await email_sender.send_meeting_confirmation(
        to=email,
        customer_name=customer_name,
        date=date_str,
        time_str=time_str,
        meet_link=result["meet_link"],
        meeting_type=meeting_type,
    )

    await params.result_callback(
        {
            "success": True,
            "meet_link": result["meet_link"],
        }
    )


async def send_booking_link(params: FunctionCallParams, customer_name: str, email: str):
    """Email the caller a direct link to the booking page, WITHOUT creating a real meeting.
    Use this when the caller wants a link to book themselves instead of picking a time live
    on the call, or when no open slot from check_availability works for their schedule.

    Never call `book_meeting` with a slot the caller hasn't actually chosen just to trigger
    an email — that creates a real meeting they never agreed to. This tool is the correct
    way to "just send a link": it only sends an email, it never touches the calendar.

    Args:
        customer_name: Caller's name.
        email: Caller's email address — read it back and get an explicit "yes" from the
            caller before calling this, same as you would before book_meeting.
    """
    email = _normalize_email(email)
    booking_url = os.getenv("CAL_BOOKING_URL", "")

    if not email or not booking_url:
        await params.result_callback({"success": False, "error": "missing_email_or_url"})
        return

    result = await email_sender.send_booking_fallback_email(
        to=email,
        customer_name=customer_name,
        booking_url=booking_url,
    )
    await params.result_callback({"success": result["success"], "error": result.get("error")})


async def mark_potential_lead(
    params: FunctionCallParams,
    customer_name: str,
    interested_service: str,
    reason: str,
    phone: str = "",
):
    """Log a caller as a warm lead when they show real interest in a service but don't
    commit to booking right now. Only call this for genuine hesitation — "let me think
    about it", "I need to check budget", "I need to check with my team", "maybe later" —
    NOT for callers who explicitly say they're not interested (just end the call politely
    for those, don't log anything), and NOT for casual info-only questions with no purchase
    intent at all.

    Args:
        customer_name: Caller's name, if given. Use "Unknown" if they didn't share it.
        interested_service: Which service they were considering (Content Creation, AI
            Automation, AI Voice Agents, or Web Development).
        reason: Short note on why they didn't commit, e.g. "wants to discuss with partner
            first" or "comparing against another vendor".
        phone: Leave blank — the system already has it from caller ID.
    """
    app_resources = params.app_resources or {}
    caller_phone = phone or app_resources.get("caller_phone") or ""

    result = await booking_db.create_lead(
        name=customer_name,
        phone=caller_phone,
        interested_service=interested_service,
        reason=reason,
        call_id=app_resources.get("session_id"),
    )
    await params.result_callback({"success": result["success"], "error": result["error"]})


async def transfer_to_human(params: FunctionCallParams, reason: str):
    """Transfer the live call to a human team member RIGHT NOW. Use only when the caller
    needs someone immediately — urgent, upset, or explicitly asking for a person — not for
    anything that can instead be a booked meeting via book_meeting (which is the right tool
    for both sales conversations and non-urgent support escalations).

    Args:
        reason: Short reason for the transfer.
    """
    # Speaks a handoff line; the actual SIP transfer needs a team SIP endpoint wired in here
    # via the Telnyx Call Control "transfer" API once you have one.
    logger.info(f"Transfer requested: {reason}")
    await params.result_callback({"success": True, "transferring": True})


async def end_call(params: FunctionCallParams):
    """End the call. Use once the caller has said goodbye and there's nothing left to do —
    e.g. right after a meeting is booked and the caller signs off, after logging a lead, or
    if they explicitly ask to hang up.
    """
    await params.result_callback({"success": True})
    await params.llm.push_frame(EndWorkerFrame())


INNOVENTIX_TOOLS = [
    check_availability,
    book_meeting,
    send_booking_link,
    mark_potential_lead,
    transfer_to_human,
    end_call,
]
