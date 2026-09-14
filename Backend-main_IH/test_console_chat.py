"""
Quickest possible sanity check: talk to the Innoventix Hub bot in your terminal as plain
text, with no audio, no Telnyx. Useful for testing the knowledge-base prompt and the meeting
booking tools before touching the voice pipeline at all.

Run:
    python test_console_chat.py

Type 'quit' to exit.
"""

import asyncio
import os

from dotenv import load_dotenv

from prompts import INNOVENTIX_SYSTEM_PROMPT

load_dotenv(override=True)


async def main():
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    print(f"Using LLM_PROVIDER={provider}\n")

    if provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

        async def ask(messages):
            resp = await client.messages.create(
                model=model,
                max_tokens=300,
                system=INNOVENTIX_SYSTEM_PROMPT,
                messages=[m for m in messages if m["role"] != "system"],
            )
            return resp.content[0].text

    else:  # openai
        import openai

        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        model = os.getenv("OPENAI_MODEL", "gpt-5.4-2026-03-05")

        async def ask(messages):
            resp = await client.chat.completions.create(model=model, messages=messages)
            return resp.choices[0].message.content

    messages = [{"role": "system", "content": INNOVENTIX_SYSTEM_PROMPT}]
    print("Bot is ready. Type your message (or 'quit' to exit).\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit"):
            break
        messages.append({"role": "user", "content": user_input})
        reply = await ask(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"Bot: {reply}\n")


if __name__ == "__main__":
    asyncio.run(main())
