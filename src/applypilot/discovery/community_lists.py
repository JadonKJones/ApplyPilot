"""Community-maintained job list scraper.

Pulls from crowd-sourced new-grad/intern tracker repos on GitHub -- the
SimplifyJobs/New-Grad-Positions and speedyapply/2027-SWE-College-Jobs style
lists. These are plain markdown files (some embed raw HTML tables, some use
pipe-table syntax with HTML links inside cells), updated by bots/PRs many
times a day, so each one is just refetched and diffed against the DB on
every discovery run -- the existing url-based dedup in store_jobs() means
re-running this costs nothing beyond a couple of HTTP GETs.

Zero LLM, zero browser -- pure HTTP + text parsing, same spirit as
workday.py.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

# Each source is a raw markdown URL plus which parser reads it. Add more
# lists here -- both formats below cover everything seen in the wild so far.
SOURCES: list[dict] = [
    {
        "name": "SimplifyJobs New Grad",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "format": "html_table",
        "strategy": "simplify_new_grad",
    },
    {
        "name": "speedyapply New Grad",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md",
        "format": "pipe_table",
        "strategy": "speedyapply_new_grad",
    },
]

# These lists don't expire old rows on their own -- a posting stays listed
# (often for months) until someone marks it closed, so the closed marker
# below is the only "is this still open" signal worth trusting. An
# age-based cutoff would just discard genuinely-open roles.
_CLOSED_MARKER = "\U0001F512"  # 🔒


def _parse_html_table_source(markdown: str) -> list[dict]:
    """SimplifyJobs-style: raw <table>...</table> HTML embedded in the .md."""
    soup = BeautifulSoup(markdown, "html.parser")
    jobs: list[dict] = []

    for table in soup.find_all("table"):
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue

            row_text = tr.get_text()
            if _CLOSED_MARKER in row_text:
                continue

            company_a = tds[0].find("a")
            company = (company_a or tds[0]).get_text(strip=True)
            title = tds[1].get_text(strip=True)
            location = tds[2].get_text(separator=", ", strip=True)

            apply_url = None
            for a in tds[3].find_all("a", href=True):
                img = a.find("img")
                if img and img.get("alt", "").lower() == "apply":
                    apply_url = a["href"]
                    break
            if not apply_url:
                a = tds[3].find("a", href=True)
                apply_url = a["href"] if a else None

            if not apply_url or not company or not title:
                continue

            jobs.append({
                "url": apply_url, "title": title, "company": company,
                "location": location, "salary": None,
            })

    return jobs


def _parse_pipe_table_source(markdown: str) -> list[dict]:
    """speedyapply-style: markdown pipe tables with HTML <a>/<img> in cells.

    Column count varies by section -- FAANG+/Quant include a Salary column
    (6 cells: Company, Position, Location, Salary, Posting, Age), but the
    much larger "Other" section doesn't (5 cells, no Salary). Rather than
    assume a fixed layout, the Posting cell is found by searching for
    whichever cell actually contains the apply <a><img> pair.
    """
    jobs: list[dict] = []

    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        if re.match(r"^\|[\s:-]+\|$", line) or "---" in line:
            continue  # header separator row

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue

        if _CLOSED_MARKER in line:
            continue

        company_soup = BeautifulSoup(cells[0], "html.parser")
        company_a = company_soup.find("a")
        company = (company_a or company_soup).get_text(strip=True)
        title = BeautifulSoup(cells[1], "html.parser").get_text(strip=True)
        location = BeautifulSoup(cells[2], "html.parser").get_text(strip=True)

        apply_url = None
        salary = None
        for cell in cells[3:-1]:  # everything between Location and Age
            soup = BeautifulSoup(cell, "html.parser")
            a = soup.find("a", href=True)
            if a and soup.find("img"):
                apply_url = a["href"]
            else:
                text = soup.get_text(strip=True)
                if text:
                    salary = text

        if not apply_url or not company or not title:
            continue

        jobs.append({
            "url": apply_url, "title": title, "company": company,
            "location": location, "salary": salary,
        })

    return jobs


_PARSERS = {
    "html_table": _parse_html_table_source,
    "pipe_table": _parse_pipe_table_source,
}


def _store(conn: sqlite3.Connection, jobs: list[dict], strategy: str) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new, existing = 0, 0

    for job in jobs:
        if config.is_excluded_title(job["title"]):
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, application_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["url"], job["title"], job.get("salary"), None, job.get("location"),
                 job["company"], strategy, now, job["url"]),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def run_community_lists_discovery() -> dict:
    """Fetch every configured community list, parse it, and store new jobs."""
    init_db()
    conn = get_connection()
    stats: dict = {}

    for source in SOURCES:
        parser = _PARSERS[source["format"]]
        try:
            resp = httpx.get(source["url"], timeout=20, follow_redirects=True)
            resp.raise_for_status()
            jobs = parser(resp.text)
            new, existing = _store(conn, jobs, source["strategy"])
            log.info("%s: %d found, %d new, %d already in DB",
                     source["name"], len(jobs), new, existing)
            stats[source["name"]] = {"found": len(jobs), "new": new, "existing": existing}
        except Exception as e:
            log.error("%s: ERROR: %s", source["name"], e)
            stats[source["name"]] = {"error": str(e)}

    return stats
