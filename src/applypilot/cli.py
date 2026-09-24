"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
questions_app = typer.Typer(
    help="Manage cached screening-question answers and aliases (used by native site handlers).",
    no_args_is_help=True,
)
app.add_typer(questions_app, name="questions")

sites_app = typer.Typer(
    help="Native (LLM-free) apply handlers -- status, and self-generation of new ones.",
    no_args_is_help=True,
)
app.add_typer(sites_app, name="sites")

gmail_app = typer.Typer(
    help="Gmail-based response tracking -- detects rejections/ghosting for the Excel tracker.",
    no_args_is_help=True,
)
app.add_typer(gmail_app, name="gmail")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def tracker(
    open_file: bool = typer.Option(False, "--open", "-o", help="Open the tracker after exporting."),
) -> None:
    """Export the Excel application tracker (~/.applypilot/tracker.xlsx).

    Runs automatically after every successful application -- this is for
    forcing a refresh (e.g. after editing the DB by hand) or just opening it.
    """
    _bootstrap()

    from applypilot import tracker as tracker_mod

    path = tracker_mod.export()
    console.print(f"[green]Exported:[/green] {path}")

    if open_file:
        import platform
        import subprocess
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.run(["open", str(path)])
            elif system == "Windows":
                import os
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            console.print(f"[yellow]Couldn't auto-open:[/yellow] {e}")


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # Discord bot (optional -- lets native site handlers ask screening
    # questions without falling back to Claude)
    from applypilot import discord_bot
    if discord_bot.is_configured():
        results.append(("Discord bot", ok_mark, "Screening questions will DM you"))
    else:
        results.append(("Discord bot", "[dim]optional[/dim]",
                        "Set DISCORD_BOT_TOKEN + DISCORD_USER_ID in .env — "
                        "see README for setup"))

    # Gmail response tracking (optional -- feeds the tracker's Status column)
    from applypilot import gmail_status
    if gmail_status.is_configured():
        results.append(("Gmail tracking", ok_mark, "Run 'applypilot gmail sync' to check responses"))
    else:
        results.append(("Gmail tracking", "[dim]optional[/dim]",
                        "Run 'applypilot gmail auth' to detect rejections/ghosting"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


@questions_app.command("list")
def questions_list() -> None:
    """List all cached screening-question answers and aliases."""
    _bootstrap()
    from applypilot.screening import list_answers, list_aliases

    answers = list_answers()
    if not answers:
        console.print("[dim]No cached answers yet. Use 'applypilot questions set' to add one,[/dim]")
        console.print("[dim]or just let a native handler ask you over Discord.[/dim]")
    else:
        table = Table(title="Cached Answers", show_header=True, header_style="bold cyan")
        table.add_column("Question")
        table.add_column("Answer")
        table.add_column("Source", style="dim")
        for a in answers:
            table.add_row(a["question_raw"], a["answer"] or "[dim]<no answer yet>[/dim]", a["source"] or "")
        console.print(table)

    aliases = list_aliases()
    if aliases:
        atable = Table(title="Aliases", show_header=True, header_style="bold magenta")
        atable.add_column("Alias")
        atable.add_column("→ Canonical question")
        for al in aliases:
            atable.add_row(al["alias_raw"], al["canonical_raw"])
        console.print(atable)


@questions_app.command("set")
def questions_set(
    question: str = typer.Argument(..., help="The question text, as it appears on the form."),
    answer: str = typer.Argument(..., help="The answer to cache for it."),
) -> None:
    """Cache an answer for a question up front, without waiting for a Discord prompt."""
    _bootstrap()
    from applypilot.screening import save_answer

    save_answer(question, answer, source="manual")
    console.print(f'[green]Cached:[/green] "{question}" -> "{answer}"')


@questions_app.command("alias")
def questions_alias(
    alias: str = typer.Argument(..., help="A new phrasing you expect to see on some form."),
    canonical: str = typer.Argument(..., help="The question you've already answered (or will answer)."),
) -> None:
    """Map ALIAS onto whatever answer CANONICAL resolves to.

    Example: a form asks "Describe your Python background" and you've
    already answered "What is your experience with Python?" elsewhere --
    alias the new phrasing so it reuses that answer instead of asking again.
    """
    _bootstrap()
    from applypilot.screening import add_alias, get_answer

    add_alias(alias, canonical)
    if get_answer(canonical) is None:
        console.print(
            f'[yellow]Alias saved, but "{canonical}" has no answer yet.[/yellow] Set one with:\n'
            f'  applypilot questions set "{canonical}" "..."'
        )
    else:
        console.print(f'[green]Aliased:[/green] "{alias}" -> "{canonical}"')


@questions_app.command("rm")
def questions_rm(question: str = typer.Argument(..., help="Question to delete.")) -> None:
    """Delete a cached question (and any aliases pointing to it)."""
    _bootstrap()
    from applypilot.screening import delete_answer

    if delete_answer(question):
        console.print(f'[green]Deleted:[/green] "{question}"')
    else:
        console.print(f'[yellow]No cached question matched:[/yellow] "{question}"')


@questions_app.command("rm-alias")
def questions_rm_alias(alias: str = typer.Argument(..., help="Alias to remove.")) -> None:
    """Remove an alias mapping (the canonical question/answer is unaffected)."""
    _bootstrap()
    from applypilot.screening import remove_alias

    if remove_alias(alias):
        console.print(f'[green]Removed alias:[/green] "{alias}"')
    else:
        console.print(f'[yellow]No alias matched:[/yellow] "{alias}"')


@sites_app.command("status")
def sites_status() -> None:
    """Show active native handlers and which domains are approaching auto-generation."""
    _bootstrap()
    from applypilot.apply import sites as site_handlers
    from applypilot.apply.sitegen import DEFAULT_THRESHOLD
    from applypilot.database import list_site_stats

    handlers = [h.__name__.rsplit(".", 1)[-1] for h in site_handlers._HANDLERS]
    console.print(f"\n[bold]Active native handlers:[/bold] {', '.join(handlers) or '[dim]none[/dim]'}\n")

    stats = list_site_stats()
    if not stats:
        console.print("[dim]No domains tracked yet -- this fills in as jobs go through the Claude agent.[/dim]\n")
        return

    table = Table(title="Domains seen by the Claude apply agent", show_header=True, header_style="bold cyan")
    table.add_column("Domain")
    table.add_column("LLM applies", justify="right")
    table.add_column("Handler status")
    table.add_column("Module", style="dim")
    for s in stats:
        status = s["handler_status"] or "none"
        if status == "generated":
            status_disp = "[green]generated[/green]"
        elif status == "generating":
            status_disp = "[yellow]generating…[/yellow]"
        elif status == "failed":
            status_disp = "[red]failed[/red]"
        elif s["llm_apply_count"] >= DEFAULT_THRESHOLD:
            status_disp = "[yellow]due[/yellow]"
        else:
            status_disp = f"[dim]none ({DEFAULT_THRESHOLD - s['llm_apply_count']} to go)[/dim]"
        table.add_row(s["domain"], str(s["llm_apply_count"]), status_disp, s["handler_module"] or "")
    console.print(table)
    console.print()


@sites_app.command("regenerate")
def sites_regenerate(
    domain: str = typer.Argument(..., help="Domain to (re)generate a handler for, e.g. boards.greenhouse.io"),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model to use for generation."),
) -> None:
    """Force (re)generation of a native handler for a domain right now (blocks until done).

    Use this to retry a domain marked 'failed', or to jump the threshold for
    a domain you already know is worth handling natively.
    """
    _bootstrap()
    from applypilot import config as ap_config
    from applypilot.database import get_connection
    from applypilot.apply.sitegen import generate_handler

    conn = get_connection()
    row = conn.execute(
        "SELECT application_url, url FROM jobs WHERE (application_url LIKE ? OR url LIKE ?) LIMIT 1",
        (f"%{domain}%", f"%{domain}%"),
    ).fetchone()
    if not row:
        console.print(f"[red]No job in the database matches domain:[/red] {domain}")
        raise typer.Exit(code=1)

    sample_job = {"url": row["url"], "application_url": row["application_url"]}
    console.print(f"[cyan]Generating a handler for {domain}...[/cyan] (this spawns Claude Code and can take a few minutes)")
    ok = generate_handler(domain, sample_job, model=model)
    if ok:
        console.print(f"[green]Done.[/green] Run 'applypilot sites status' to see it.")
    else:
        console.print(f"[red]Generation failed.[/red] Check the logs in {ap_config.LOG_DIR}")


@gmail_app.command("auth")
def gmail_auth(
    client_id: Optional[str] = typer.Option(None, "--client-id", envvar="GMAIL_CLIENT_ID"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret", envvar="GMAIL_CLIENT_SECRET"),
) -> None:
    """One-time Gmail OAuth setup (opens a browser for consent).

    Create an OAuth client first at https://console.cloud.google.com/apis/credentials
    (type "Desktop app", with the Gmail API enabled on that project), then pass its
    id/secret here (or set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET). Only read-only
    Gmail access is requested.
    """
    _bootstrap()
    if not client_id or not client_secret:
        console.print("[red]Missing --client-id / --client-secret[/red] (or GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET).")
        console.print("Create one at: https://console.cloud.google.com/apis/credentials")
        raise typer.Exit(code=1)

    from applypilot.gmail_status import run_oauth_flow

    try:
        run_oauth_flow(client_id, client_secret)
    except Exception as e:
        console.print(f"[red]Auth failed:[/red] {e}")
        raise typer.Exit(code=1)

    console.print("[green]Gmail connected.[/green] Saved to ~/.applypilot/.env. Try 'applypilot gmail sync'.")


@gmail_app.command("sync")
def gmail_sync() -> None:
    """Scan Gmail for responses to your applications and update the tracker's Status column."""
    _bootstrap()
    from applypilot import gmail_status, tracker as tracker_mod

    if not gmail_status.is_configured():
        console.print("[red]Gmail not configured.[/red] Run 'applypilot gmail auth' first.")
        raise typer.Exit(code=1)

    console.print("[cyan]Checking Gmail for responses...[/cyan]")
    updated = gmail_status.sync()
    path = tracker_mod.export()
    console.print(f"[green]Updated {updated} job(s).[/green] Tracker refreshed: {path}")


if __name__ == "__main__":
    app()
