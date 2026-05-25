"""
Append-only alert journal at `.research/alerts/<YYYY-MM-DD>.jsonl`.

Two write paths (plan §1 Principle 3):
  - `append_alert` — STRICT DEDUP creation path. Reads the day-file,
    no-ops if `(asof, ticker, screener)` is already present, otherwise
    appends one line with `enriched_at: null`. Returns True if written.
  - `append_enriched_alert` — ALWAYS-APPEND enrichment path. Writes one
    new row with `enriched_at` set to the current timestamp. Readers
    apply Last-Writer-Wins on `enriched_at` per `(asof, ticker, screener)`.

Atomicity: single-line writes through O_APPEND are atomic on POSIX for writes
≤ PIPE_BUF (typically 4 KB). A serialized SetupCandidate row is well under
that. Mirrors the `observations.py` pattern.

single-writer assumption: brief is operator-invoked, not crontab. Concurrent
writes from a second process are out of scope for v1 — if cron is added in
v1.5 it must coordinate via flock or migrate to SQLite.

SCHEMA CONTRACT: This journal is append-only and additive-only. New fields
may be ADDED with sensible defaults (Optional with None default). Existing
fields MUST NOT be renamed, removed, or have their type changed. Breaking
changes require a coordinated migration as part of the same PR (no
standalone `alerts migrate` CLI in v1; see Pre-Mortem Scenario 5).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from research_assistant.screeners import SetupCandidate


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alerts_path(asof: str, base: Path) -> Path:
    return base / "alerts" / f"{asof}.jsonl"


def _candidate_to_row(
    candidate: SetupCandidate,
    *,
    enriched_at: Optional[str],
) -> dict:
    """Serialize a SetupCandidate to its JSONL row form. `enriched_at` is
    written explicitly (None on creation, ISO timestamp on enrichment) so the
    LWW read-path can sort without parsing other fields."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": candidate.ticker,
        "screener": candidate.screener,
        "asof": candidate.asof,
        "entry_price": candidate.entry_price,
        "evidence": candidate.evidence,
        "created_at": _now_iso(),
        "return_7d": candidate.return_7d,
        "return_30d": candidate.return_30d,
        "return_90d": candidate.return_90d,
        "enriched_at": enriched_at,
    }


def _read_day_raw(path: Path) -> list[dict]:
    """Read raw rows from one day-file. Tolerates a corrupted line without
    losing the rest (mirrors `read_observations` behavior)."""
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
# Write paths
# ---------------------------------------------------------------------------

def append_alert(candidate: SetupCandidate, base: Path) -> bool:
    """STRICT-DEDUP creation path.

    Reads the day-file for `candidate.asof`, no-ops if `(asof, ticker, screener)`
    is already present (returns False). Otherwise appends one line and returns
    True. Atomic single-line O_APPEND write.
    """
    path = _alerts_path(candidate.asof, base)
    existing = _read_day_raw(path)
    key = (candidate.asof, candidate.ticker, candidate.screener)
    for row in existing:
        existing_key = (row.get("asof"), row.get("ticker"), row.get("screener"))
        if existing_key == key:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    row = _candidate_to_row(candidate, enriched_at=None)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return True


def append_enriched_alert(candidate: SetupCandidate, base: Path) -> None:
    """ALWAYS-APPEND enrichment path.

    Writes one new row stamped with the current `enriched_at` timestamp.
    Never dedups on write — readers apply LWW per `(asof, ticker, screener)`.
    This preserves atomic O_APPEND while making the enrichment semantic
    explicit.
    """
    path = _alerts_path(candidate.asof, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _candidate_to_row(candidate, enriched_at=_now_iso())
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def read_alerts(base: Path, date_iso: str) -> list[dict]:
    """Return raw rows for one date (no LWW collapsing — that's `read_alerts_window`)."""
    return _read_day_raw(_alerts_path(date_iso, base))


def _enriched_at_sortkey(row: dict) -> tuple[int, str]:
    """Sort key for LWW: enrichment rows (enriched_at != None) always beat
    creation rows; among enrichment rows, later timestamp wins."""
    ts = row.get("enriched_at")
    if ts is None:
        return (0, "")
    return (1, ts)


def read_alerts_window(base: Path, start: str, end: str) -> list[dict]:
    """Read raw rows from every day-file in [start, end] (ISO YYYY-MM-DD,
    inclusive), apply LWW dedup per `(asof, ticker, screener)`, return rows
    sorted by `asof` ascending then ticker.

    Creation-only rows (enriched_at is None) are superseded by ANY enrichment
    row for the same key. Among enrichment rows, the highest `enriched_at`
    wins.
    """
    alerts_dir = base / "alerts"
    if not alerts_dir.exists():
        return []

    rows: list[dict] = []
    for path in sorted(alerts_dir.glob("*.jsonl")):
        date_iso = path.stem
        if start <= date_iso <= end:
            rows.extend(_read_day_raw(path))

    # LWW collapse per key
    by_key: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row.get("asof", ""), row.get("ticker", ""), row.get("screener", ""))
        winner = by_key.get(key)
        if winner is None or _enriched_at_sortkey(row) > _enriched_at_sortkey(winner):
            by_key[key] = row

    return sorted(by_key.values(), key=lambda r: (r.get("asof", ""), r.get("ticker", "")))
