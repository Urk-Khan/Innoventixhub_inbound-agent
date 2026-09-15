"""
Innoventix Hub — Pipecat voice pipeline (pipecat-ai 1.8.1 API).

Cascade pipeline: Telnyx audio in -> Cartesia STT -> LLM (English-only, with meeting-booking
and lead-tracking tools) -> Cartesia TTS -> Telnyx audio out. STT and TTS always use
Cartesia. LLM provider is picked at runtime from .env (LLM_PROVIDER: openai or anthropic).

Before the greeting fires, the caller's phone number is looked up in Supabase
(booking_db.get_customer_by_phone) — if they're an existing customer, their purchase
history is injected into the LLM's context so it can consider a cross-sell, per
prompts.py's SCENARIO 3.

No N8N anywhere — check_availability/book_meeting talk straight to Cal.com's v2 API
(cal_com.py), confirmations go out via email_sender.py, and everything gets logged to
Supabase (booking_db.py). See PROJECT_DOCUMENTATION.md for the full architecture writeup.

Run with:
    python bot.py

This one command does everything: starts a cloudflared tunnel, updates your Telnyx TeXML
Application's Voice URL automatically, pre-warms the VAD model and Cartesia's connection
path, then starts Pipecat's built-in server (default: http://localhost:7860), which serves
the Telnyx WebSocket at /ws.
"""

import asyncio
import os
import socket
import sys

# Force IPv4 socket resolution on Windows to avoid AWS/Cartesia WebSocket handshake timeouts.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(override=True)

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from auto_tunnel import setup_tunnel_and_telnyx
from latency_logger import LatencyLogger
from prompts import GREETING_EN, INNOVENTIX_SYSTEM_PROMPT
from tools import INNOVENTIX_TOOLS
from warmup import warmup_all
import booking_db

# Same VAD tuning proven out on the Ashad project — tuned to ignore background speech and
# ambient room/road noise on real phone calls.
VAD_PARAMS = VADParams(
    confidence=0.85,
    min_volume=0.70,
    start_secs=0.20,
    stop_secs=0.35,
)


def _build_stt_service():
    """Cartesia only (Ink-Whisper)."""
    from pipecat.services.cartesia.stt import CartesiaSTTService

    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError("CARTESIA_API_KEY is not set in .env")
    return CartesiaSTTService(api_key=api_key)


def _build_tts_service():
    """Cartesia only (Sonic)."""
    from pipecat.services.cartesia.tts import CartesiaTTSService

    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError("CARTESIA_API_KEY is not set in .env")
    return CartesiaTTSService(
        api_key=api_key,
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "86e30c1d-714b-4074-a1f2-1cb6b552fb49"),
        ),
    )


def _build_llm_service():
    """openai or anthropic — both cloud APIs, both require a key."""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            settings=AnthropicLLMService.Settings(
                model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                system_instruction=INNOVENTIX_SYSTEM_PROMPT,
            ),
        )
    elif provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            settings=OpenAILLMService.Settings(
                model=os.getenv("OPENAI_MODEL", "gpt-5.4-2026-03-05"),
                system_instruction=INNOVENTIX_SYSTEM_PROMPT,
            ),
        )
    else:
        raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run the voice bot for one incoming call session."""
    logger.info("Starting Innoventix Hub session")
    _call_start_time = __import__("time").time()

    stt = _build_stt_service()
    tts = _build_tts_service()
    llm = _build_llm_service()

    context = LLMContext(tools=INNOVENTIX_TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VAD_PARAMS),
            user_turn_stop_timeout=0.35,
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            LatencyLogger(),
            transport.output(),
            assistant_aggregator,
        ]
    )

    call_data = runner_args.call_data
    caller_phone = call_data.from_number if call_data else None

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=8000,  # Telnyx PSTN audio is 8kHz
            audio_out_sample_rate=8000,
        ),
        app_resources={"caller_phone": caller_phone, "session_id": runner_args.session_id},
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Call connected, caller={caller_phone}")

        # Look up the caller BEFORE greeting, so the LLM already knows whether this is a
        # known customer (and what they've purchased) for the cross-sell scenario — this
        # adds one DB round-trip's worth of latency before the greeting starts, which is the
        # trade-off for the LLM having that context from turn one instead of discovering it
        # reactively mid-call.
        customer = await booking_db.get_customer_by_phone(caller_phone) if caller_phone else None
        if customer:
            purchased = customer.get("purchased_services") or "no specific services on file"
            context.add_message(
                {
                    "role": "user",
                    "content": (
                        f"[KNOWN CALLER CONTEXT: This caller is an existing customer, "
                        f"name={customer.get('name', 'unknown')}, purchased={purchased}. "
                        f"Use this per the CROSS-SELL scenario in your instructions — never "
                        f"mention this context to the caller directly.]"
                    ),
                }
            )

        context.add_message(
            {
                "role": "user",
                "content": f"[Call connected. Greet the caller warmly: '{GREETING_EN}']",
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Call ended")
        # Fire-and-forget: log the full call record to Supabase.
        try:
            await booking_db.save_call_record(
                call_id=runner_args.session_id,
                caller_phone=caller_phone,
                start_time=_call_start_time,
                messages=context.messages,
            )
        except Exception as e:
            logger.warning(f"save_call_record raised unexpectedly: {e}")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point the Pipecat runner calls for each new connection."""
    transport_params = {
        "telnyx": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
    }

    transport = await create_transport(runner_args, transport_params)

    call_data = runner_args.call_data
    if call_data:
        logger.info(f"From number: {call_data.from_number}")

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    import atexit

    from pipecat.runner.run import main

    _PORT = int(os.getenv("PORT", "7860"))

    async def _startup():
        (tunnel_process, tunnel_hostname), _ = await asyncio.gather(
            setup_tunnel_and_telnyx(_PORT),
            warmup_all(),
        )
        return tunnel_process, tunnel_hostname

    _tunnel_process, _tunnel_hostname = asyncio.run(_startup())

    if _tunnel_process is not None:

        @atexit.register
        def _cleanup_tunnel():
            logger.info("Stopping cloudflared tunnel...")
            _tunnel_process.terminate()

    sys.argv = [sys.argv[0], "-t", "telnyx", "--port", str(_PORT)]
    if _tunnel_hostname:
        sys.argv += ["--proxy", _tunnel_hostname]
    else:
        logger.warning("No tunnel hostname available — the server will only be reachable locally.")

    logger.info("Ready to receive calls.")
    main()
