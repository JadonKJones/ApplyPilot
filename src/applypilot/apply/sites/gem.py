"""Native apply handler for Gem-powered career sites (jobs.gem.com).

Gem's public job board renders a fixed form with no name/id/aria attributes
on its inputs -- fields are only identifiable by the label text sitting next
to them in the DOM. This handler extracts every field on the page that way,
fills the standard ones (name, email, LinkedIn, phone, location, resume,
cover letter) straight from the profile, and resolves anything else --
screening questions, EEO, custom questions -- through the screening-answer
cache (`applypilot.screening`), which asks you over Discord the first time
and remembers the answer after that. None of this touches Claude.

`apply()` returns None (instead of a RESULT status) whenever it hits
something it can't handle confidently -- an unlabeled field, an unsupported
input type, a question with no cached answer and no way to ask -- so the
caller falls back to the full LLM agent rather than risk a wrong or
half-filled submission.
"""

import logging
import re
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from applypilot import screening
from applypilot.apply import prompt as prompt_mod

logger = logging.getLogger(__name__)

# Label (lowercased, asterisk stripped) -> profile key handled directly,
# without going through the screening-answer cache.
_KNOWN_LABELS = {
    "first name", "last name", "email", "linkedin url",
    "phone number", "location", "resume", "cover letter",
}

# Input types we're willing to auto-fill from a plain-text cached answer.
# Anything else (checkbox, radio, custom widgets) falls back to Claude.
_FILLABLE_TEXT_TYPES = {"text", "email", "tel", "number", ""}

_EXTRACT_FIELDS_JS = """
() => {
  const out = [];
  let idx = 0;
  document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(input => {
    let label = null;
    let el = input;
    for (let i = 0; i < 6 && el; i++) {
      el = el.parentElement;
      if (!el) break;
      const span = el.querySelector(':scope > span');
      if (span && span.textContent.trim()) { label = span.textContent.trim(); break; }
    }
    input.setAttribute('data-ap-idx', String(idx));
    out.push({idx, label, tag: input.tagName.toLowerCase(), type: (input.type || '').toLowerCase()});
    idx++;
  });
  return out;
}
"""


def matches(url: str) -> bool:
    if not url:
        return False
    return urlparse(url).netloc.lower() == "jobs.gem.com"


def _clean_label(label: str | None) -> str:
    return re.sub(r"\s*\*\s*$", "", label or "").strip()


def _extract_fields(page: Page) -> list[dict]:
    """Every input/textarea/select on the page, tagged with a data-ap-idx
    attribute and its best-guess label (found by walking up from the field
    to the nearest ancestor with a direct-child <span> of text)."""
    return page.evaluate(_EXTRACT_FIELDS_JS)


def _locator_for(page: Page, idx: int) -> Locator:
    return page.locator(f'[data-ap-idx="{idx}"]')


def _select_option(loc: Locator, answer: str) -> None:
    try:
        loc.select_option(label=answer)
        return
    except Exception:
        pass
    try:
        loc.select_option(answer)
        return
    except Exception:
        pass
    # Last resort: case-insensitive substring match against option text.
    for opt in loc.locator("option").all_inner_texts():
        if answer.strip().lower() in opt.strip().lower():
            loc.select_option(label=opt)
            return
    raise ValueError(f"no matching option for answer: {answer!r}")


def apply(page: Page, job: dict, profile: dict, dry_run: bool = False) -> str | None:
    """Fill and submit a Gem application.

    Returns a RESULT-style status string ("applied", "expired",
    "failed:reason"), or None if this posting needs the LLM agent instead
    (unrecognized layout, or a screening question we couldn't resolve).
    """
    personal = profile["personal"]
    url = job.get("application_url") or job["url"]

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("button[type=submit]", timeout=15000)
    except PlaywrightTimeoutError:
        logger.info("gem: form didn't load in time, falling back to LLM (%s)", url)
        return None

    body = page.content().lower()
    if "no longer accepting applications" in body or "position has been filled" in body:
        return "expired"

    fields = _extract_fields(page)
    if not fields:
        logger.info("gem: no recognizable fields, falling back to LLM (%s)", url)
        return None

    try:
        files = prompt_mod.resolve_upload_files(job, profile)
    except ValueError as e:
        logger.error("gem: %s", e)
        return f"failed:{e}"

    full_name = personal["full_name"]
    first_name = full_name.split()[0]
    last_name = full_name.split()[-1] if " " in full_name else ""
    location = ", ".join(p for p in (personal.get("city", ""), personal.get("province_state", "")) if p)

    known_values = {
        "first name": first_name,
        "last name": last_name,
        "email": personal["email"],
        "linkedin url": personal.get("linkedin_url", ""),
        "phone number": personal.get("phone", ""),
        "location": location,
    }

    try:
        for f in fields:
            label = _clean_label(f["label"])
            key = label.lower()
            loc = _locator_for(page, f["idx"])

            if key == "resume":
                loc.set_input_files(files["resume_pdf"])
                continue
            if key == "cover letter":
                if files["cover_letter_pdf"]:
                    loc.set_input_files(files["cover_letter_pdf"])
                continue
            if key in known_values:
                if known_values[key]:
                    loc.fill(known_values[key])
                continue

            # Anything else is a screening/EEO/custom question.
            if not label:
                logger.info("gem: unlabeled extra field, falling back to LLM (%s)", url)
                return None
            if f["tag"] == "input" and f["type"] not in _FILLABLE_TEXT_TYPES:
                logger.info("gem: unsupported field type '%s' for '%s', falling back to LLM",
                           f["type"], label)
                return None

            answer = screening.resolve_or_ask(label)
            if answer is None:
                logger.info("gem: no answer available for '%s', falling back to LLM (%s)", label, url)
                return None

            if f["tag"] == "select":
                _select_option(loc, answer)
            else:
                loc.fill(answer)
    except Exception:
        logger.exception("gem: failed filling form, falling back to LLM (%s)", url)
        return None

    if dry_run:
        logger.info("gem: dry run, not submitting (%s)", url)
        return "applied"

    submit = page.get_by_role("button", name="Apply without saving")
    if submit.count() == 0:
        submit = page.locator("button[type=submit]").last

    try:
        submit.click()
        page.wait_for_timeout(2000)
    except Exception:
        logger.exception("gem: submit click failed, falling back to LLM (%s)", url)
        return None

    body = page.content().lower()
    if any(s in body for s in (
        "thank you", "application received", "application submitted", "we've received",
    )):
        return "applied"

    # Couldn't confirm submission went through -- don't guess, let the LLM verify/retry.
    logger.info("gem: could not confirm submission, falling back to LLM (%s)", url)
    return None
