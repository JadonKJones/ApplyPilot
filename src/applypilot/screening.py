"""Screening-question answer cache, with user-defined aliases.

Any screening question a native site handler can't fill from the profile
(salary expectations, "why do you want this role", EEO, whatever a company
bolts onto its form) goes through here instead of Claude:

  1. Normalize the question text and look it up directly.
  2. If that misses, look it up through the alias table -- aliases let you
     map differently-worded questions ("What's your Python experience?")
     onto one canonical question you've already answered.
  3. If both miss, the caller (e.g. discord_bot.ask) is responsible for
     getting a fresh answer from the user and calling save_answer() so the
     same question never has to be asked twice.

No fuzzy/semantic matching here on purpose -- aliases are explicit and
user-controlled, not guessed.
"""

import re
import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection


def normalize(text: str) -> str:
    """Case/whitespace/punctuation-insensitive normalization for matching."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_answer(question: str) -> str | None:
    """Look up a cached answer by exact match or alias. None if unknown."""
    conn = get_connection()
    norm = normalize(question)

    row = conn.execute(
        "SELECT answer FROM screening_answers WHERE question = ?", (norm,)
    ).fetchone()
    if row and row["answer"] is not None:
        return row["answer"]

    row = conn.execute("""
        SELECT sa.answer FROM screening_aliases al
        JOIN screening_answers sa ON sa.id = al.answer_id
        WHERE al.alias = ?
    """, (norm,)).fetchone()
    if row and row["answer"] is not None:
        return row["answer"]

    return None


def save_answer(question: str, answer: str, source: str = "manual") -> int:
    """Cache an answer for a question (creates or updates the entry).

    Returns the screening_answers row id.
    """
    conn = get_connection()
    norm = normalize(question)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        INSERT INTO screening_answers (question, question_raw, answer, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(question) DO UPDATE SET
            answer = excluded.answer,
            source = excluded.source,
            updated_at = excluded.updated_at
    """, (norm, question.strip(), answer, source, now, now))
    conn.commit()

    return conn.execute(
        "SELECT id FROM screening_answers WHERE question = ?", (norm,)
    ).fetchone()["id"]


def add_alias(alias: str, canonical_question: str) -> None:
    """Map `alias` to whatever answer `canonical_question` resolves to.

    If the canonical question has no cached answer yet, it's created with a
    NULL answer -- set it with `save_answer()` (or answer it once for real
    and it'll be filled in).
    """
    conn = get_connection()
    canon_norm = normalize(canonical_question)
    now = datetime.now(timezone.utc).isoformat()

    row = conn.execute(
        "SELECT id FROM screening_answers WHERE question = ?", (canon_norm,)
    ).fetchone()
    if row:
        answer_id = row["id"]
    else:
        conn.execute("""
            INSERT INTO screening_answers (question, question_raw, answer, source, created_at, updated_at)
            VALUES (?, ?, NULL, 'alias-placeholder', ?, ?)
        """, (canon_norm, canonical_question.strip(), now, now))
        conn.commit()
        answer_id = conn.execute(
            "SELECT id FROM screening_answers WHERE question = ?", (canon_norm,)
        ).fetchone()["id"]

    alias_norm = normalize(alias)
    conn.execute("""
        INSERT INTO screening_aliases (alias, alias_raw, answer_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET answer_id = excluded.answer_id
    """, (alias_norm, alias.strip(), answer_id, now))
    conn.commit()


def remove_alias(alias: str) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM screening_aliases WHERE alias = ?", (normalize(alias),))
    conn.commit()
    return cur.rowcount > 0


def delete_answer(question: str) -> bool:
    """Delete a cached question (and any aliases pointing to it)."""
    conn = get_connection()
    cur = conn.execute("DELETE FROM screening_answers WHERE question = ?", (normalize(question),))
    conn.commit()
    return cur.rowcount > 0


def list_answers() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, question_raw, answer, source, updated_at FROM screening_answers ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_aliases() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT al.alias_raw, sa.question_raw AS canonical_raw, sa.answer
        FROM screening_aliases al
        JOIN screening_answers sa ON sa.id = al.answer_id
        ORDER BY al.alias_raw
    """).fetchall()
    return [dict(r) for r in rows]


def resolve_or_ask(question: str, timeout: int = 600) -> str | None:
    """Resolve a question from cache/alias, or ask via Discord and cache it.

    Returns None if there's no cached answer AND Discord isn't configured
    (or the human doesn't reply within `timeout` seconds) -- callers should
    treat that as "couldn't handle this natively" and fall back to Claude.
    """
    cached = get_answer(question)
    if cached is not None:
        return cached

    from applypilot import discord_bot
    if not discord_bot.is_configured():
        return None

    answer = discord_bot.ask(question, timeout=timeout)
    if answer is None:
        return None

    save_answer(question, answer, source="discord")
    return answer
