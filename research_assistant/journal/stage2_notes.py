"""
Append-only Stage 2 note journal at `.research/stage2/<TICKER>.jsonl`.

One JSONL file per ticker, append-only, no dedup at write time. The operator
may re-run `/brief` multiple times per day with different inputs and we want
all reads recorded for trajectory analysis. Read paths return rows in
insertion order (or most-recent-first for the bounded `read_stage2_history`).

Atomicity: single-line writes through O_APPEND are atomic on POSIX for writes
≤ PIPE_BUF (typically 4 KB). A serialized compact Stage 2 note row is well
under that. Mirrors the `journal/alerts.py` pattern.

single-writer assumption: brief is operator-invoked, not crontab. Concurrent
writes from a second process are out of scope for v1 — if cron is added in
v1.5 it must coordinate via flock or migrate to SQLite.

SCHEMA CONTRACT: This journal is append-only and additive-only. New fields
may be ADDED with sensible defaults (Optional with None default). Existing
fields MUST NOT be renamed, removed, or have their type changed. Breaking
changes require a coordinated migration as part of the same PR.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research_assistant.orchestrator import Stage2Note


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage2_path(ticker: str, base: Path) -> Path:
    return base / "stage2" / f"{ticker.upper()}.jsonl"


def _note_to_row(note: "Stage2Note") -> dict:
    """Serialize a Stage2Note into the COMPACT JSONL row form.

    We persist the compact view (not the full Stage2Note) so injecting 5
    prior notes into the next Stage 2 prompt adds ~500 input tokens, not
    ~5000. The full note remains in the brief cache + trace for the
    original ET date.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": note.ticker,
        "asof": datetime.now(timezone.utc).date().isoformat(),
        "recorded_at": _now_iso(),
        "bull_anchor": note.bull_anchor,
        "bear_anchor": note.bear_anchor,
        "conviction": dict(note.conviction),
        "composite_conviction": note.composite_conviction,
        "decision_tag": note.decision_tag,
        "skeptic_verdict": note.skeptic_verdict,
    }


def _read_raw(path: Path) -> list[dict]:
    """Read raw rows from one ticker-file. Tolerates corrupted lines without
    losing the rest (mirrors `journal/alerts._read_day_raw` behavior)."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def append_stage2_note(note: "Stage2Note", base: Path) -> None:
    """Append-only write to .research/stage2/<note.ticker>.jsonl.

    Always-append; no dedup at write time (operator may re-run brief
    multiple times per day with different Stage 2 outputs)."""
    path = _stage2_path(note.ticker, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _note_to_row(note)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def read_stage2_history(
    ticker: str,
    base: Path,
    *,
    limit: int = 5,
) -> list[dict]:
    """Read the most recent N notes for ticker, ordered most-recent first.

    Returns empty list if no history exists. Each entry is the raw JSONL
    dict (not parsed back into Stage2Note — just the recorded fields for
    prompt-context use).
    """
    rows = _read_raw(_stage2_path(ticker, base))
    if not rows:
        return []
    # Insertion-order is chronological (oldest first); reverse for
    # most-recent first then truncate.
    rows.reverse()
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    return rows


def read_stage2_full_history(ticker: str, base: Path) -> list[dict]:
    """Read all history for ticker, oldest first. Used by the trajectory
    CLI subcommand to render full history."""
    return _read_raw(_stage2_path(ticker, base))
