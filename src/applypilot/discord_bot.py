"""Discord DM bridge for screening questions.

Sends a screening question to you as a Discord DM and blocks (polling, not
a persistent gateway connection -- no extra dependency beyond httpx, which
ApplyPilot already requires) until you reply or it times out.

Setup (see README): create a bot at https://discord.com/developers/applications,
invite it to any one server you're also in (Discord only lets a bot open a DM
with someone it shares a server with), enable "Message Content Intent", then set:

    DISCORD_BOT_TOKEN=...   # Bot tab -> Reset Token
    DISCORD_USER_ID=...     # your Discord user ID (enable Developer Mode -> right-click your name -> Copy User ID)

in ~/.applypilot/.env.
"""

import logging
import os
import time

import httpx

from applypilot import config

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
POLL_INTERVAL = 3  # seconds


def is_configured() -> bool:
    config.load_env()
    return bool(os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_USER_ID"))


def _headers() -> dict:
    token = os.environ["DISCORD_BOT_TOKEN"]
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _get_dm_channel(client: httpx.Client, user_id: str) -> str:
    resp = client.post(f"{API_BASE}/users/@me/channels", json={"recipient_id": user_id})
    resp.raise_for_status()
    return resp.json()["id"]


def _send_message(client: httpx.Client, channel_id: str, content: str) -> str:
    resp = client.post(f"{API_BASE}/channels/{channel_id}/messages", json={"content": content})
    resp.raise_for_status()
    return resp.json()["id"]


def _poll_for_reply(client: httpx.Client, channel_id: str, after_message_id: str,
                    user_id: str, timeout: int) -> str | None:
    deadline = time.time() + timeout
    latest_seen = after_message_id

    while time.time() < deadline:
        resp = client.get(
            f"{API_BASE}/channels/{channel_id}/messages",
            params={"after": latest_seen, "limit": 50},
        )
        resp.raise_for_status()
        messages = resp.json()  # newest first

        human_replies = [
            m for m in messages
            if m.get("author", {}).get("id") == user_id and m.get("content")
        ]
        if human_replies:
            # newest first -> take the most recent
            return human_replies[0]["content"].strip()

        if messages:
            latest_seen = max((m["id"] for m in messages), key=int)

        time.sleep(POLL_INTERVAL)

    return None


def ask(question: str, timeout: int = 600) -> str | None:
    """DM the question, wait for a reply. Returns None on timeout/error.

    Blocks the calling thread for up to `timeout` seconds -- called from an
    apply worker, so that worker (and only that worker, when running with
    --workers > 1) is paused waiting on you.
    """
    config.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    user_id = os.environ.get("DISCORD_USER_ID")
    if not token or not user_id:
        logger.warning("Discord not configured (DISCORD_BOT_TOKEN / DISCORD_USER_ID missing)")
        return None

    try:
        with httpx.Client(headers=_headers(), timeout=15) as client:
            channel_id = _get_dm_channel(client, user_id)
            msg_id = _send_message(
                client, channel_id,
                f"**Screening question:**\n{question}\n\n"
                f"_Reply here with your answer -- I'll remember it for future applications._"
            )
            logger.info("Discord: asked '%s...', waiting up to %ds", question[:60], timeout)
            return _poll_for_reply(client, channel_id, msg_id, user_id, timeout)
    except httpx.HTTPError:
        logger.exception("Discord API error while asking screening question")
        return None


def notify(text: str) -> None:
    """Fire-and-forget DM -- no reply expected. Never raises; logs on failure."""
    config.load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    user_id = os.environ.get("DISCORD_USER_ID")
    if not token or not user_id:
        return

    try:
        with httpx.Client(headers=_headers(), timeout=15) as client:
            channel_id = _get_dm_channel(client, user_id)
            _send_message(client, channel_id, text)
    except httpx.HTTPError:
        logger.exception("Discord API error while sending notification")
