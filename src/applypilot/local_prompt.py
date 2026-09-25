"""Local, zero-setup fallback for screening questions.

No token, no account, nothing to configure -- this is the last resort
before a question falls back to Claude entirely. It starts a one-shot
local HTTP server and blocks until you answer it: a browser tab, curl,
or a phone if you port-forward it. Binds to localhost only by default
(not your LAN) since anyone who can reach it can feed an answer into
whatever's currently applying.

`screening.resolve_or_ask()` tries Discord first (if configured), then
falls back to this automatically -- so if Discord's token is broken or
it's never been set up, this just quietly takes over.
"""

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from applypilot import config

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765

_FORM_HTML = """<html><body style="font-family:sans-serif;max-width:480px;margin:60px auto">
<h3>ApplyPilot is asking:</h3>
<p style="font-size:1.1em">{question}</p>
<form method="GET">
  <input name="answer" autofocus style="width:100%;padding:8px;font-size:1em" autocomplete="off">
  <button type="submit" style="margin-top:8px;padding:6px 16px">Answer</button>
</form>
</body></html>"""


class _AnswerHandler(BaseHTTPRequestHandler):
    def _respond(self, code: int, body: str, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        answer = (parse_qs(urlparse(self.path).query).get("answer") or [None])[0]
        if answer:
            self.server.answer = answer  # type: ignore[attr-defined]
            self._respond(200, f"Got it: {answer}\nYou can close this tab.")
        else:
            question = getattr(self.server, "question", "")
            self._respond(200, _FORM_HTML.format(question=question), "text/html")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        answer = None
        try:
            answer = json.loads(body).get("answer")
        except (json.JSONDecodeError, AttributeError):
            qs = parse_qs(body)
            answer = (qs.get("answer") or [None])[0] or (body.strip() or None)

        if answer:
            self.server.answer = answer  # type: ignore[attr-defined]
            self._respond(200, "Got it, thanks.")
        else:
            self._respond(400, "Missing 'answer'")

    def log_message(self, *args):
        pass  # silence default request logging


def _port() -> int:
    config.load_env()
    try:
        return int(os.environ.get("SCREENING_PROMPT_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def ask(question: str, timeout: int = 600) -> str | None:
    """Start a one-shot local HTTP server, block until answered or timed out."""
    port = _port()

    try:
        server = HTTPServer(("127.0.0.1", port), _AnswerHandler)
    except OSError:
        logger.exception("local_prompt: couldn't bind port %d (already in use?)", port)
        return None

    server.answer = None  # type: ignore[attr-defined]
    server.question = question  # type: ignore[attr-defined]
    server.timeout = 1  # handle_request() poll granularity, not the overall deadline

    curl_cmd = f"curl 'http://localhost:{port}/?answer=YOUR+ANSWER'"
    message = (
        f"Screening question needs an answer (waiting up to {timeout}s):\n"
        f"  {question}\n"
        f"  -> Browser: http://localhost:{port}/\n"
        f"  -> Terminal: {curl_cmd}"
    )
    logger.warning(message)
    try:
        from applypilot.apply.dashboard import add_event
        add_event(f"Q: {question[:70]} -> http://localhost:{port}/")
    except Exception:
        pass  # dashboard not active in this context (e.g. CLI use) -- fine

    deadline = time.time() + timeout
    while server.answer is None and time.time() < deadline:  # type: ignore[attr-defined]
        server.handle_request()
    server.server_close()

    return server.answer  # type: ignore[attr-defined]
