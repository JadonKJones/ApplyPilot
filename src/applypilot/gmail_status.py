"""Gmail-based application response tracking.

Separate from the `gmail` MCP server the apply agent uses to read verification
codes mid-application (that one only exists inside a live Claude Code
session). This is a standalone OAuth client ApplyPilot owns itself, so
`applypilot gmail sync` can run on its own -- via cron, or by hand -- without
spawning Claude Code at all. It never sends, modifies, or deletes anything;
read-only access (gmail.readonly) is all it asks for.

For each job marked "applied", it searches Gmail for messages from/about
that company since the application date and classifies them by keyword:

    - a rejection email found  -> "rejection (interview)" if an interview
      signal appeared first, else "rejection (application)"
    - no rejection, but 30+ days of silence since the last signal (or since
      applying, if there was never any contact) -> "ghosted (interview)" if
      an interview happened, else "ignored (application)"
    - otherwise (too recent to call, or genuine back-and-forth ongoing) ->
      leaves the status alone; there's nothing confident to say yet

Classification is keyword-based, not an LLM call -- cheap, fast, and good
enough for the extremely formulaic language of ATS rejection/interview
emails. It will occasionally misclassify; see tracker.py for how a status
you set by hand in the spreadsheet always wins over the automatic guess.

Setup: create an OAuth client at https://console.cloud.google.com/apis/credentials
(type "Desktop app"), enable the Gmail API for that project, then run:

    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... applypilot gmail auth

which walks through the one-time browser consent and stores a refresh token
in ~/.applypilot/.env.
"""

import logging
import os
import re
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from applypilot import config
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

GHOST_DAYS = 30

_REJECTION_PATTERNS = (
    "move forward with other candidates", "moving forward with other candidates",
    "will not be moving forward", "not be moving forward",
    "decided not to move forward", "not been selected", "not selected for",
    "unable to offer you", "pursue other candidates",
    "other applicants whose qualifications", "position has been filled",
    "won't be moving forward", "not be proceeding with your application",
    "regret to inform", "after careful consideration, we",
    "we are unable to move", "decided to proceed with other",
    "will not be extending an offer",
)
_INTERVIEW_PATTERNS = (
    "schedule an interview", "schedule a call", "phone screen",
    "would like to speak with you", "next steps in our process",
    "interview process", "set up a time to chat", "phone interview",
    "video interview", "technical interview", "onsite interview",
    "invite you to interview", "chat about the role", "get to know you better",
    "move forward with your application to the next",
)


def is_configured() -> bool:
    config.load_env()
    return bool(
        os.environ.get("GMAIL_CLIENT_ID")
        and os.environ.get("GMAIL_CLIENT_SECRET")
        and os.environ.get("GMAIL_REFRESH_TOKEN")
    )


# ---------------------------------------------------------------------------
# One-time OAuth setup
# ---------------------------------------------------------------------------

class _RedirectCaptureHandler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        _RedirectCaptureHandler.code = qs.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Authorized. You can close this tab.</body></html>")

    def log_message(self, *args):
        pass  # silence default request logging


def run_oauth_flow(client_id: str, client_secret: str, port: int = 8734) -> str:
    """Interactive one-time setup. Returns the refresh token (also saved to .env)."""
    redirect_uri = f"http://localhost:{port}/"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print(f"Opening browser for Gmail authorization...\nIf it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    _RedirectCaptureHandler.code = None
    server = HTTPServer(("localhost", port), _RedirectCaptureHandler)
    while _RedirectCaptureHandler.code is None:
        server.handle_request()
    server.server_close()

    resp = httpx.post(TOKEN_URL, data={
        "code": _RedirectCaptureHandler.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    tokens = resp.json()
    refresh_token = tokens["refresh_token"]

    _save_env_var("GMAIL_CLIENT_ID", client_id)
    _save_env_var("GMAIL_CLIENT_SECRET", client_secret)
    _save_env_var("GMAIL_REFRESH_TOKEN", refresh_token)

    return refresh_token


def _save_env_var(key: str, value: str) -> None:
    config.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if config.ENV_PATH.exists():
        lines = config.ENV_PATH.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    config.ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


# ---------------------------------------------------------------------------
# Gmail API access
# ---------------------------------------------------------------------------

_access_token_cache: dict = {}


def _get_access_token() -> str:
    now = datetime.now(timezone.utc).timestamp()
    if _access_token_cache.get("token") and _access_token_cache.get("expires_at", 0) > now + 30:
        return _access_token_cache["token"]

    config.load_env()
    resp = httpx.post(TOKEN_URL, data={
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    data = resp.json()
    _access_token_cache["token"] = data["access_token"]
    _access_token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return data["access_token"]


def _search_messages(client: httpx.Client, query: str, max_results: int = 20) -> list[str]:
    resp = client.get(f"{API_BASE}/messages", params={"q": query, "maxResults": max_results})
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("messages", [])]


def _get_message(client: httpx.Client, msg_id: str) -> dict:
    resp = client.get(
        f"{API_BASE}/messages/{msg_id}",
        params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
    )
    resp.raise_for_status()
    data = resp.json()
    headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
    internal_date = datetime.fromtimestamp(
        int(data["internalDate"]) / 1000, tz=timezone.utc
    )
    return {
        "subject": headers.get("Subject", ""),
        "from": headers.get("From", ""),
        "snippet": data.get("snippet", ""),
        "date": internal_date,
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_message(subject: str, snippet: str) -> str:
    """'rejection' | 'interview' | 'other', by keyword match."""
    text = f"{subject} {snippet}".lower()
    if any(p in text for p in _REJECTION_PATTERNS):
        return "rejection"
    if any(p in text for p in _INTERVIEW_PATTERNS):
        return "interview"
    return "other"


def _resolve_job_status(messages: list[dict], applied_at: datetime,
                        now: datetime) -> tuple[str | None, datetime | None]:
    """Returns (status, last_contact_at). status is None when there's
    nothing confident to say yet (too recent, or ongoing back-and-forth)."""
    had_interview = False
    last_contact_at: datetime | None = None

    for msg in sorted(messages, key=lambda m: m["date"]):
        last_contact_at = msg["date"]
        kind = classify_message(msg["subject"], msg["snippet"])
        if kind == "interview":
            had_interview = True
        elif kind == "rejection":
            return ("rejection (interview)" if had_interview else "rejection (application)"), last_contact_at

    reference = last_contact_at or applied_at
    if (now - reference).days >= GHOST_DAYS:
        status = "ghosted (interview)" if had_interview else "ignored (application)"
        return status, last_contact_at

    return None, last_contact_at


def sync() -> int:
    """Check Gmail for every applied job and update gmail_status where a
    confident classification is available. Returns the number updated."""
    if not is_configured():
        logger.warning("Gmail not configured (run 'applypilot gmail auth')")
        return 0

    conn = get_connection()
    jobs = [dict(r) for r in conn.execute(
        "SELECT url, site, applied_at FROM jobs WHERE apply_status = 'applied'"
    ).fetchall()]

    now = datetime.now(timezone.utc)
    updated = 0

    headers = {"Authorization": f"Bearer {_get_access_token()}"}
    with httpx.Client(headers=headers, timeout=15) as client:
        for job in jobs:
            company = job.get("site") or ""
            applied_at_raw = job.get("applied_at")
            if not company or not applied_at_raw:
                continue
            try:
                applied_at = datetime.fromisoformat(applied_at_raw)
            except ValueError:
                continue

            date_str = applied_at.strftime("%Y/%m/%d")
            query = f'"{company}" after:{date_str}'

            try:
                msg_ids = _search_messages(client, query)
                messages = [_get_message(client, mid) for mid in msg_ids]
            except httpx.HTTPError:
                logger.exception("Gmail search failed for '%s'", company)
                continue

            status, last_contact_at = _resolve_job_status(messages, applied_at, now)
            if status is None and last_contact_at is None:
                continue  # nothing to update

            conn.execute(
                "UPDATE jobs SET gmail_status = ?, gmail_status_at = ?, "
                "gmail_last_contact_at = ? WHERE url = ?",
                (status, now.isoformat(),
                 last_contact_at.isoformat() if last_contact_at else None, job["url"]),
            )
            updated += 1

    conn.commit()
    logger.info("gmail_status: checked %d applied job(s), updated %d", len(jobs), updated)
    return updated
