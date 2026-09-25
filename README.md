<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Fills and submits application forms — natively (zero LLM cost) on known ATS platforms, or via Claude Code for everything else |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |
| Discord bot | Native handlers DM you screening questions instead of falling back to Claude for them — see [Screening Questions](#screening-questions) |
| Google Cloud OAuth client | Powers `applypilot gmail sync` (rejection/ghosting detection for the tracker) — see [Application Tracker](#application-tracker) |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional), plus `DISCORD_BOT_TOKEN`/`DISCORD_USER_ID` and `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET`/`GMAIL_REFRESH_TOKEN` (both optional — see [Screening Questions](#screening-questions) and [Application Tracker](#application-tracker) below). See [.env.example](.env.example) for the full list.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
For each job, ApplyPilot first checks whether it has a **native handler** for that site — a hand-written or self-generated Playwright script that fills and submits the form directly, no LLM involved. If none matches (or the posting doesn't fit what the handler expects), it falls back to Claude Code: launches a Chrome instance, navigates to the application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time either way.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## Native Apply Handlers

Most auto-apply tools spend an LLM call reasoning through every single form, on every single application, forever. ApplyPilot instead keeps a small library of **native handlers** — plain Playwright code, one per ATS platform — that fill and submit a form directly for zero LLM cost. It ships with one for [Gem](https://gem.com) (`jobs.gem.com`) out of the box.

### It writes its own handlers

You don't have to ask. ApplyPilot tracks which domains keep falling through to the Claude agent, and once a domain has needed it 5+ times, it spawns Claude Code in the background — the *exact same* `claude` CLI call used for every LLM-driven application, so it works identically on a Claude Pro/Max subscription; no separate API key required — to inspect a real posting on that domain and write a new handler module. Generated handlers follow the same defensive contract as the hand-written ones: whenever anything is uncertain (unfamiliar layout, an unsupported field type, can't confirm the submission went through), they bail out to the LLM agent rather than guess. Worst case, a bad generated handler is no worse than having no handler at all.

```bash
applypilot sites status              # active handlers + which domains are close to auto-generating one
applypilot sites regenerate DOMAIN   # force (re)generation right now, e.g. after one is marked 'failed'
```

### Screening Questions

Native handlers don't call Claude to improvise answers to screening/EEO/custom questions — they resolve them through a cached-answer system instead:

1. Check the answer cache (exact match, or an alias you've defined).
2. If it's genuinely new: DM you the question over Discord, if that's configured and working.
3. If Discord isn't configured (or fails — broken token, etc.) it falls through automatically to a **local HTTP prompt**: no setup, no account, nothing to configure. It prints (and logs to the apply dashboard) a URL and a `curl` command, then blocks until you answer from a browser tab or a terminal on the same machine:
   ```bash
   curl 'http://localhost:8765/?answer=YOUR+ANSWER'
   ```
   Binds to `localhost` only by default; override the port with `SCREENING_PROMPT_PORT` in `.env` if 8765 is taken.
4. Cache the answer so the same (or an aliased) question is never asked twice.

```bash
applypilot questions list                          # cached answers + aliases
applypilot questions set "Years of Python exp?" "5+"   # seed an answer up front, no Discord needed
applypilot questions alias "Describe your Python background" "Years of Python exp?"  # reuse one answer for many phrasings
applypilot questions rm "..."                       # delete a cached question
applypilot questions rm-alias "..."                  # remove an alias
```

To enable the Discord fallback: create a bot at [discord.com/developers/applications](https://discord.com/developers/applications), invite it to any one server you're also in (a bot can only DM someone it shares a server with), enable **Message Content Intent**, then set `DISCORD_BOT_TOKEN` and `DISCORD_USER_ID` in `.env`. Without it, an unanswered new question just falls back to the Claude agent for that one job.

---

## Application Tracker

`applypilot tracker` maintains an Excel workbook (`~/.applypilot/tracker.xlsx`) of every job you've actually applied to — Position, Company, Role, Location, Date Applied, résumé/cover-letter filenames used, salary + an anticipated-takeaway formula, a hyperlinked Link, and a Status column with a dropdown (draft → interview rounds → rejection/ghosted/offer). It's regenerated automatically after every successful application; run it by hand with:

```bash
applypilot tracker            # refresh ~/.applypilot/tracker.xlsx
applypilot tracker --open     # refresh and open it
```

It's an upsert, not an overwrite — matched by the Link column, so re-running it never touches Notes, Connections?, Latest word, contact 1, or SHADE once you've filled them in by hand. This is its own file, not something that edits a tracker you already keep.

### Gmail response tracking (optional)

Set up `applypilot gmail auth` once (needs your own OAuth client from [console.cloud.google.com](https://console.cloud.google.com/apis/credentials) — Desktop app type, Gmail API enabled, read-only scope only) and `applypilot gmail sync` will scan your inbox for each applied job and fill in **Last Contact** and a first guess at **Status**:

- a rejection email → `rejection (interview)` or `rejection (application)`, depending on whether an interview happened first
- 30+ days of silence after an interview → `ghosted (interview)`
- 30+ days of silence with no contact at all → `ignored (application)`

Status is only ever a *starting* guess: the moment you type a different value into that cell yourself, it's yours — future syncs never overwrite a status you've set by hand.

```bash
applypilot gmail auth     # one-time OAuth setup (opens a browser)
applypilot gmail sync     # check for responses, update the tracker
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
applypilot tracker [--open]             # Export/open the Excel application tracker
applypilot sites status                 # Native handler status + domains close to auto-generation
applypilot sites regenerate DOMAIN      # Force (re)generate a native handler now
applypilot questions list               # Cached screening-question answers + aliases
applypilot questions set Q A            # Cache an answer without waiting for Discord
applypilot questions alias ALIAS Q      # Map a new phrasing onto an existing answer
applypilot gmail auth                   # One-time Gmail OAuth setup
applypilot gmail sync                   # Detect rejections/ghosting, update the tracker
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
