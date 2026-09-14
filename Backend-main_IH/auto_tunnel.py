"""
Automates the two manual steps that used to precede every test call:
  1. Start a cloudflared quick tunnel pointing at this server.
  2. Tell Telnyx the new public URL (cloudflared gives a new random URL every restart).

With this, `python bot.py` is the only command you need.

How it works: Pipecat's own dev runner (pipecat.runner.run) has a built-in route (`POST /`)
that returns the correct TeXML for Telnyx, generated from a `--proxy <host>` value passed on
the command line. So the only moving part is: start the tunnel, grab its hostname, and (a)
pass it to the runner as --proxy, (b) tell Telnyx's TeXML Application to fetch TeXML from
that tunnel's root URL.

Requires:
  - `cloudflared` in your PATH, or `cloudflared.exe` (Windows) / `cloudflared` (Linux/macOS)
    sitting next to this file.
  - TELNYX_API_KEY and TELNYX_TEXML_APP_ID in .env (run `python list_texml_apps.py` to find
    your App ID — reading it from .env, not hardcoding it, is what this file does
    differently from an earlier version of this automation that hardcoded an App ID and
    silently broke when a different TeXML Application ended up assigned to the number).

If either isn't set up, `python bot.py` still runs the server locally — it just logs a
warning and you're back to doing the tunnel/portal update by hand.
"""

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

import aiohttp
from loguru import logger

TUNNEL_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
TUNNEL_STARTUP_TIMEOUT_SECS = 25


def _find_cloudflared() -> str | None:
    local_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local_path = Path(__file__).parent / local_name
    if local_path.exists():
        return str(local_path)
    return shutil.which("cloudflared")


async def _start_tunnel(port: int) -> tuple[asyncio.subprocess.Process, str]:
    cloudflared_path = _find_cloudflared()
    if not cloudflared_path:
        raise RuntimeError(
            "cloudflared not found. Put cloudflared.exe next to bot.py, or install it and "
            "make sure it's on your PATH: https://developers.cloudflare.com/cloudflare-one/"
            "connections/connect-networks/downloads/"
        )

    logger.info(f"Starting cloudflared tunnel -> http://localhost:{port} ...")
    process = await asyncio.create_subprocess_exec(
        cloudflared_path,
        "tunnel",
        "--url",
        f"http://localhost:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _read_until_url_found() -> str:
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            match = TUNNEL_URL_PATTERN.search(line)
            if match:
                return match.group(0)
        raise RuntimeError("cloudflared exited before printing a tunnel URL.")

    try:
        url = await asyncio.wait_for(_read_until_url_found(), timeout=TUNNEL_STARTUP_TIMEOUT_SECS)
    except asyncio.TimeoutError as e:
        process.terminate()
        raise RuntimeError(
            f"cloudflared didn't print a tunnel URL within {TUNNEL_STARTUP_TIMEOUT_SECS}s. "
            "Check that cloudflared is working: run it manually to see its output."
        ) from e

    logger.info(f"Tunnel ready: {url}")
    return process, url


async def _update_telnyx_voice_url(public_url: str) -> None:
    api_key = os.getenv("TELNYX_API_KEY")
    app_id = os.getenv("TELNYX_TEXML_APP_ID")

    if not api_key or not app_id:
        logger.warning(
            "TELNYX_API_KEY or TELNYX_TEXML_APP_ID not set — skipping automatic Telnyx "
            "update. Set both in .env (run `python list_texml_apps.py` to find your App "
            f"ID), or update the TeXML Application's Voice URL by hand: {public_url}/"
        )
        return

    url = f"https://api.telnyx.com/v2/texml_applications/{app_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"voice_url": f"{public_url}/"}

    async with aiohttp.ClientSession() as session:
        async with session.patch(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error(f"Failed to update Telnyx TeXML Application ({resp.status}): {body}")
                logger.warning(f"Update it manually in the portal instead: Voice URL = {public_url}/")
            else:
                logger.info(f"Telnyx TeXML Application updated — Voice URL = {public_url}/")


async def setup_tunnel_and_telnyx(port: int) -> tuple[asyncio.subprocess.Process | None, str | None]:
    """Returns (cloudflared_process, hostname) — hostname is None (server still runs, just
    not publicly reachable) if anything here fails."""
    try:
        process, public_url = await _start_tunnel(port)
    except Exception as e:
        logger.error(f"Tunnel setup failed, continuing with local-only server: {e}")
        return None, None

    await _update_telnyx_voice_url(public_url)

    hostname = public_url.replace("https://", "").replace("http://", "")
    return process, hostname
