"""Autonomous native-handler generation.

Tracks how many times each ATS domain needed the full Claude apply agent
(because no native handler existed, or the one that existed declined). Once
a domain crosses a threshold, this spawns Claude Code -- the exact same
`claude` CLI subprocess the apply agent already uses for every LLM-driven
application, so it authenticates and bills the same way (a Claude Pro/Max
subscription login works here exactly like it does for a normal apply run;
no separate API key is needed) -- to inspect a real posting on that domain
and write a new handler module into `apply/sites/`, following the same
contract and defensive-fallback style as the hand-written ones.

Generation runs once per domain, on a background thread so it never blocks
the job currently being applied to. It's conservative by construction: any
failure just leaves that domain on the existing Claude fallback path, and
a generated handler that returns None too often is no worse than having no
handler at all.
"""

import json
import logging
import re
import subprocess
import threading
from pathlib import Path

from applypilot import config
from applypilot.database import record_llm_apply, get_site_stats, set_handler_status

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 5
SITES_DIR = Path(__file__).parent / "sites"
_REPO_ROOT = Path(__file__).resolve().parents[3]

_GEN_LOCK = threading.Lock()
_IN_PROGRESS: set[str] = set()

# Reserved worker id for the throwaway inspection Chrome, well clear of any
# real apply worker (--workers is realistically single digits).
_GEN_WORKER_ID = 900


def domain_of(url: str | None) -> str | None:
    from urllib.parse import urlparse
    if not url:
        return None
    return urlparse(url).netloc.lower() or None


def _reference_handler() -> str:
    return (SITES_DIR / "gem.py").read_text(encoding="utf-8")


def _build_generation_prompt(domain: str, sample_job: dict) -> str:
    reference = _reference_handler()
    url = sample_job.get("application_url") or sample_job["url"]

    return f"""You are writing a new native (LLM-free) apply handler for ApplyPilot.

== GOAL ==
Domain "{domain}" has come up {DEFAULT_THRESHOLD}+ times needing the full Claude browsing agent to
apply. Write a Python module that fills and submits this site's application form directly via
Playwright, so future applications on this domain don't need an LLM agent at all.

== REFERENCE IMPLEMENTATION (existing handler for a different ATS -- copy this contract exactly) ==
```python
{reference}
```

== YOUR TASK ==
1. Use the Playwright MCP tools to navigate to this real posting and inspect its form:
   {url}
   Take a snapshot. Check for name/id/aria attributes on the inputs (use browser_evaluate to read
   the raw DOM if needed -- many ATS's, like the one in the reference above, have NONE, and fields
   must be matched by label text proximity instead). Note every field: label, input type, required.
   DO NOT click the final Submit/Apply button. Do not submit a real application. You are only
   inspecting the form's structure.

2. Write a new file at: src/applypilot/apply/sites/<slug>.py
   (pick <slug> as a short snake_case name for this ATS/platform -- not the specific company)

   It MUST define exactly these two functions, same signature as the reference:
     def matches(url: str) -> bool
     def apply(page, job: dict, profile: dict, dry_run: bool = False) -> str | None

   Requirements (non-negotiable, matches the house style in the reference):
   - `matches()` should match the ATS platform's domain (e.g. by netloc), not this one company.
   - Fill known fields (name, email, phone, LinkedIn, location) straight from `profile["personal"]`.
   - Use `applypilot.apply.prompt.resolve_upload_files(job, profile)` for the resume/cover-letter
     PDFs -- do not reimplement file staging.
   - For ANY field that isn't one of the standard identity fields (screening questions, EEO,
     custom questions, salary, etc.), resolve it via:
       from applypilot import screening
       answer = screening.resolve_or_ask(label_text)
       if answer is None: return None   # fall back to the LLM agent, don't guess
   - Return None (never raise, never guess) whenever: the page layout doesn't match what you
     expected, a field type you don't handle appears (checkbox/radio/file beyond resume/cover
     letter), or anything else is uncertain. None means "let the full Claude agent handle this
     one instead" -- it is always the SAFE default, never a failure.
   - Only return "applied" after actually clicking submit (skip the click if dry_run=True and
     return "applied" anyway, per the reference's dry-run convention) and confirming success text
     appears on the page. If you can't confirm, return None rather than guessing "applied".
   - Return "expired" if the posting is closed. Return f"failed:<reason>" only for a definitive,
     unambiguous failure (e.g. missing resume file) -- everything uncertain should be None instead.

3. After writing the file, verify it's syntactically valid Python (you can use the Bash tool to
   run `python3 -m py_compile <path>`), then stop. Do not edit any other file -- handler modules
   are auto-discovered, nothing else needs to change.

Output exactly one line at the end: RESULT:GENERATED:<slug> on success, or RESULT:FAILED:<reason>
if you could not produce a working handler (e.g. the form is too unusual, requires login, etc.)."""


def _make_mcp_config(port: int) -> dict:
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
        }
    }


def generate_handler(domain: str, sample_job: dict, model: str = "sonnet",
                     timeout: int = 900) -> bool:
    """Spawn Claude Code to write a new handler module for `domain`.

    Returns True if a valid, contract-satisfying handler module was
    produced and is now active.
    """
    from applypilot.apply import chrome, sites as site_handlers

    set_handler_status(domain, "generating")
    logger.info("sitegen: generating handler for '%s'...", domain)

    port = chrome.BASE_CDP_PORT + _GEN_WORKER_ID
    chrome_proc = None

    try:
        chrome_proc = chrome.launch_chrome(_GEN_WORKER_ID, port=port, headless=True)

        mcp_path = config.APP_DIR / ".mcp-sitegen.json"
        mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

        cmd = [
            "claude", "--model", model, "-p",
            "--mcp-config", str(mcp_path),
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--allowedTools", (
                "Read,Write,Bash(python3 -m py_compile*),"
                "mcp__playwright__browser_navigate,mcp__playwright__browser_snapshot,"
                "mcp__playwright__browser_evaluate,mcp__playwright__browser_wait_for"
            ),
            "--output-format", "stream-json",
            "--verbose", "-",
        ]

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            cwd=str(_REPO_ROOT),
        )
        proc.stdin.write(_build_generation_prompt(domain, sample_job))
        proc.stdin.close()

        result_text = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    result_text = msg.get("result", "")
            except json.JSONDecodeError:
                pass
        proc.wait(timeout=timeout)

    except Exception:
        logger.exception("sitegen: generation crashed for '%s'", domain)
        set_handler_status(domain, "failed")
        return False
    finally:
        if chrome_proc is not None:
            chrome.cleanup_worker(_GEN_WORKER_ID, chrome_proc)

    m = re.search(r"RESULT:GENERATED:(\S+)", result_text)
    if not m:
        logger.warning("sitegen: no handler generated for '%s' (result: %s)",
                       domain, result_text[-300:])
        set_handler_status(domain, "failed")
        return False

    slug = m.group(1).strip().removesuffix(".py")
    handler_path = SITES_DIR / f"{slug}.py"
    if not handler_path.exists():
        logger.warning("sitegen: claimed '%s' but %s doesn't exist", slug, handler_path)
        set_handler_status(domain, "failed")
        return False

    try:
        compile(handler_path.read_text(encoding="utf-8"), str(handler_path), "exec")
    except SyntaxError:
        logger.exception("sitegen: generated file has a syntax error, discarding")
        set_handler_status(domain, "failed")
        return False

    site_handlers.reload_handlers()
    mod = next(
        (h for h in site_handlers._HANDLERS if h.__name__.rsplit(".", 1)[-1] == slug),
        None,
    )
    if mod is None or not callable(getattr(mod, "matches", None)) or not callable(getattr(mod, "apply", None)):
        logger.warning("sitegen: generated module '%s' doesn't satisfy the handler contract, discarding", slug)
        set_handler_status(domain, "failed")
        return False

    set_handler_status(domain, "generated", handler_module=f"{slug}.py")
    logger.info("sitegen: handler '%s' generated and active for '%s'", slug, domain)

    try:
        from applypilot import discord_bot
        discord_bot.notify(
            f"Generated a native handler for **{domain}** after {DEFAULT_THRESHOLD}+ applications "
            f"(`sites/{slug}.py`). It's active now -- worth keeping an eye on its first few runs."
        )
    except Exception:
        logger.debug("sitegen: notify failed (non-fatal)", exc_info=True)

    return True


def record_and_maybe_generate(job: dict, threshold: int = DEFAULT_THRESHOLD,
                              model: str = "sonnet") -> None:
    """Call after falling back to the Claude agent for a job with no native
    handler. Bumps the domain's LLM-apply counter and, once it crosses
    `threshold`, kicks off handler generation in the background.
    """
    url = job.get("application_url") or job["url"]
    domain = domain_of(url)
    if not domain:
        return

    count = record_llm_apply(domain)
    stats = get_site_stats(domain)
    status = stats["handler_status"] if stats else "none"

    if count < threshold or status != "none":
        return

    with _GEN_LOCK:
        if domain in _IN_PROGRESS:
            return
        _IN_PROGRESS.add(domain)

    def _run():
        try:
            generate_handler(domain, job, model=model)
        finally:
            with _GEN_LOCK:
                _IN_PROGRESS.discard(domain)

    threading.Thread(target=_run, name=f"sitegen-{domain}", daemon=True).start()
