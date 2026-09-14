"""
3 end-to-end test scenarios covering every major call path.

Scenario A — Sales booking (with fallback email when Calendly Free plan blocks API booking)
Scenario B — Warm lead capture (interested caller who doesn't commit)
Scenario C — General info inquiry (no tools, pure knowledge-base Q&A)

Run:  python test_scenario.py
"""
import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv(override=True)

import openai

import booking_db
import calendly
import email_sender
from prompts import INNOVENTIX_SYSTEM_PROMPT
from tools import _normalize_email

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
SCENARIOS = {
    "A — Sales Booking (Calendly fallback)": [
        "Hi, I run a dental clinic and I'm looking for an AI voice agent to handle our incoming appointment calls. Can Innoventix Hub help with that?",
        "That sounds exactly what we need. What are the next steps?",
        "Yes let's book a call. My name is Ahmed.",
        "Let's do the first slot you have.",
        "My email is ahmed dot clinic at gmail dot com.",
        "Yes that's right.",
    ],
    "B — Warm Lead (interested but not ready)": [
        "Hi, I saw your post about AI automation. We're a small e-commerce brand and I'm curious if you could automate our order follow-ups.",
        "That sounds great. How much would something like that cost?",
        "Hmm, I'd need to discuss the budget with my business partner first. I'll call back.",
    ],
    "C — General Info Inquiry (no booking)": [
        "Hey, can you tell me what services Innoventix Hub offers?",
        "Who's on the team?",
        "Do you do custom mobile apps?",
        "Alright, thanks for the info. I'll think about it and maybe call back.",
    ],
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Look up open meeting slots from Calendly.",
            "parameters": {
                "type": "object",
                "properties": {"meeting_type": {"type": "string", "enum": ["sales", "support"]}},
                "required": ["meeting_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_meeting",
            "description": "Book a meeting slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "meeting_type": {"type": "string"},
                    "start_time": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "email": {"type": "string"},
                    "topic": {"type": "string"},
                    "phone": {"type": "string"},
                },
                "required": ["meeting_type", "start_time", "customer_name", "email", "topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_potential_lead",
            "description": "Log a warm lead who showed interest but did not commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "interested_service": {"type": "string"},
                    "reason": {"type": "string"},
                    "phone": {"type": "string"},
                },
                "required": ["customer_name", "interested_service", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "Transfer the call to a human agent immediately.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "End the call when there is nothing left to do.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def handle_tool(name: str, args: dict) -> str:
    """Execute each tool and print what happened."""
    if name == "check_availability":
        print(f"  [TOOL] check_availability(meeting_type={args['meeting_type']})")
        slots = await calendly.get_open_slots(args["meeting_type"], limit=5)
        if slots:
            print(f"  [OK] {len(slots)} slots. First 3:")
            for s in slots[:3]:
                print(f"       -> {s['date']} at {s['time']}")
        else:
            print("  [WARN] No slots returned")
        return json.dumps({"success": True, "slots": slots})

    elif name == "book_meeting":
        email = _normalize_email(args.get("email", ""))
        print(f"  [TOOL] book_meeting(name={args['customer_name']}, email={email}, time={args['start_time']})")
        result = await calendly.book_slot(
            meeting_type=args["meeting_type"],
            start_time=args["start_time"],
            customer_name=args["customer_name"],
            phone=args.get("phone", ""),
            email=email,
            topic=args["topic"],
        )
        print(f"  [BOOKING] success={result['success']}, error={result.get('error')}")

        if result["success"]:
            date_str, time_str = calendly.format_display(args["start_time"])
            print(f"  [EMAIL] Sending booking confirmation to {email}...")
            er = await email_sender.send_meeting_confirmation(
                to=email, customer_name=args["customer_name"],
                date=date_str, time_str=time_str,
                meet_link=result["meet_link"], meeting_type=args["meeting_type"],
            )
            print(f"  [EMAIL] Confirmation: {er}")
            return json.dumps({"success": True, "meet_link": result["meet_link"]})
        else:
            # Fallback: send booking link email
            booking_url = os.getenv("CALENDLY_BOOKING_URL", "")
            fallback_sent = False
            if email and booking_url:
                print(f"  [FALLBACK] Booking API failed — sending booking link to {email}...")
                fb = await email_sender.send_booking_fallback_email(
                    to=email, customer_name=args["customer_name"], booking_url=booking_url
                )
                fallback_sent = fb["success"]
                print(f"  [FALLBACK] Email sent: {fallback_sent}")
            return json.dumps({"success": False, "error": result["error"], "fallback_link_sent": fallback_sent})

    elif name == "mark_potential_lead":
        print(f"  [TOOL] mark_potential_lead(name={args['customer_name']}, service={args['interested_service']})")
        result = await booking_db.create_lead(
            name=args["customer_name"],
            phone=args.get("phone", ""),
            interested_service=args["interested_service"],
            reason=args["reason"],
            call_id="test-scenario",
        )
        print(f"  [LEAD] Logged: success={result['success']}")
        return json.dumps(result)

    elif name == "transfer_to_human":
        print(f"  [TOOL] transfer_to_human(reason={args['reason']})")
        return json.dumps({"success": True, "transferring": True})

    elif name == "end_call":
        print("  [TOOL] end_call() — call ended.")
        return json.dumps({"success": True})

    return json.dumps({"error": "unknown_tool"})


async def run_scenario(label: str, turns: list[str], client, model: str):
    print()
    print("=" * 65)
    print(f"  SCENARIO {label}")
    print("=" * 65)

    messages = [{"role": "system", "content": INNOVENTIX_SYSTEM_PROMPT}]

    for i, user_turn in enumerate(turns):
        print(f"\n[Turn {i+1}] Caller: {user_turn}")
        messages.append({"role": "user", "content": user_turn})

        while True:
            resp = await client.chat.completions.create(
                model=model, messages=messages,
                tools=TOOLS_SCHEMA, tool_choice="auto",
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result_str = await handle_tool(tc.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
            else:
                reply = msg.content or ""
                print(f"   Bot: {reply}")
                messages.append({"role": "assistant", "content": reply})
                break


async def main():
    client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

    print("=" * 65)
    print("  INNOVENTIX HUB -- FULL SCENARIO TEST SUITE")
    print("=" * 65)
    print(f"  Model    : {model}")
    print(f"  Timezone : {os.getenv('TIMEZONE', 'UTC')}")
    print(f"  Scenarios: {len(SCENARIOS)}")

    for label, turns in SCENARIOS.items():
        await run_scenario(label, turns, client, model)

    print()
    print("=" * 65)
    print("  ALL SCENARIOS COMPLETE")
    print("=" * 65)


asyncio.run(main())
