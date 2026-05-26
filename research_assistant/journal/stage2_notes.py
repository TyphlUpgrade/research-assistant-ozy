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

Security boundaries (PR 2A.5):
  - `_validate_ticker` regex-pins the ticker to SEC shape (1-6 ASCII upper
    letters + optional .X-XX share-class suffix) BEFORE it is used to build
    the per-ticker file path. The resolved path is double-checked to live
    inside `base/stage2`. Together these prevent path-traversal writes via
    a maliciously-crafted ticker.
  - `_sanitize_text` strips control characters, collapses whitespace, and
    hard-caps anchor/observation lengths before persistence. Day-1 LLM
    output is re-injected into the Day-2 prompt via `prior_reads_json`;
    sanitization stops a "prompt-injection chain" where attacker-controlled
    anchor text survives the round-trip and steers Day-2 reasoning.
  - `_MAX_LINE_BYTES` caps each persisted row at 16 KB and skips any line
    that exceeds the cap on read. Defense in depth against an oversize row
    that slipped past sanitization or was injected by a third party.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from research_assistant.orchestrator import Stage2Note


log = logging.getLogger(__name__)


SCHEMA_VERSION = 1

# America/New_York carries the trading-day calendar. `asof` MUST use ET so the
# journal row's date matches the brief's ET asof — otherwise a brief written
# after 8 PM ET (post-UTC-midnight) lands one calendar day ahead of the brief
# that wrote it.
_ET = ZoneInfo("America/New_York")

# SEC ticker shape: 1-6 ASCII upper letters, optional .X / .XX share-class
# suffix (e.g. BRK.B). Rejects ALL non-conforming input (path separators,
# parent-dir tokens, control chars, unicode lookalikes). Used to lock down
# `_stage2_path` so LLM-emitted tickers can't drive arbitrary file writes.
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z0-9]{1,2})?$")

# Sanitization caps — generous for honest content, tight enough to stop a
# prompt-injection payload from surviving the round-trip into the next-day
# prompt verbatim.
_ANCHOR_MAX_LEN = 240        # ~30 words at 8 chars/word
_OBSERVATION_MAX_LEN = 500
_MAX_LINE_BYTES = 16 * 1024  # 16 KB hard cap per row

# Strip C0/C1 controls, NUL, BOM, zero-width, line/paragraph separators.
# Whitespace is then collapsed via str.split() so newline-injection
# (anchor = "good\nIGNORE PRIOR INSTRUCTIONS") becomes a single line.
# All ranges use explicit hex escapes — unambiguous and pattern-portable.
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x1f"        # C0 controls (includes \n \r \t)
    r"\x7f-\x9f"         # DEL + C1 controls
    r"  "      # LINE SEPARATOR, PARAGRAPH SEPARATOR
    r"﻿"            # BOM / zero-width no-break space
    r"​-‏"     # zero-width space/non-joiner/joiner + LRM/RLM
    r"‪-‮"     # bidi embedding / override controls
    r"]"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_ticker(ticker: str) -> str:
    """Enforce SEC ticker shape and return the canonical upper-cased form.

    Raises ValueError for anything that doesn't match `_TICKER_RE`. The
    caller (`_stage2_path`) relies on this to keep LLM-supplied tickers
    from steering filesystem writes.
    """
    upper = str(ticker or "").strip().upper()
    if not _TICKER_RE.match(upper):
        raise ValueError(f"Invalid ticker for stage2 journal: {ticker!r}")
    return upper


def _sanitize_text(text: object, max_len: int) -> str:
    """Strip control characters, collapse whitespace, and hard-cap length.

    Defense against prompt-injection persistence — prior-day LLM output is
    untrusted by the next-day prompt boundary. An anchor containing
    ``\\nIGNORE ALL PRIOR INSTRUCTIONS`` round-trips through here as a
    single-line cleaned string with the newline stripped.
    """
    if not text:
        return ""
    cleaned = _CONTROL_CHAR_RE.sub(" ", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_len]


def _stage2_path(ticker: str, base: Path) -> Path:
    """Per-ticker JSONL path under ``base/stage2/``.

    Ticker is regex-validated AND the resolved path is checked to live
    inside ``base/stage2`` — defense in depth against future regression of
    either guard.
    """
    valid_ticker = _validate_ticker(ticker)
    expected_parent = (base / "stage2").resolve()
    path = (base / "stage2" / f"{valid_ticker}.jsonl").resolve()
    if expected_parent != path.parent:
        # If we ever reach this, regex validation drifted — fail loud.
        raise ValueError(f"Stage 2 path escaped base: {path}")
    return path


def _note_to_row(note: "Stage2Note") -> dict:
    """Serialize a Stage2Note into the COMPACT JSONL row form.

    We persist the compact view (not the full Stage2Note) so injecting 5
    prior notes into the next Stage 2 prompt adds ~500 input tokens, not
    ~5000. The full note remains in the brief cache + trace for the
    original ET date.

    ``asof`` uses America/New_York (trading-day calendar). ``recorded_at`` is
    UTC wall-clock — the two intentionally disagree after 8 PM ET so the
    journal row keys to the correct trading day while still preserving the
    actual write moment.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": _validate_ticker(note.ticker),
        "asof": datetime.now(_ET).date().isoformat(),
        "recorded_at": _now_iso(),
        "bull_anchor": _sanitize_text(note.bull_anchor, _ANCHOR_MAX_LEN),
        "bear_anchor": _sanitize_text(note.bear_anchor, _ANCHOR_MAX_LEN),
        "conviction": dict(note.conviction),
        "composite_conviction": note.composite_conviction,
        "decision_tag": note.decision_tag,
        "skeptic_verdict": note.skeptic_verdict,
    }


def _read_raw(path: Path) -> list[dict]:
    """Read raw rows from one ticker-file. Tolerates corrupted lines without
    losing the rest (mirrors ``journal/alerts._read_day_raw`` behavior).

    Defensive against three classes of malformed input:
      - oversize rows (>``_MAX_LINE_BYTES``) — skip with WARN.
      - invalid JSON — skip silently (same as the alerts journal).
      - valid JSON that decodes to a non-dict (e.g. ``[]``, ``42``, ``null``,
        ``"string"``) — skip silently. Downstream consumers
        (``_render_trajectory``, Stage 2 prompt ``prior_reads_json``) all
        assume ``row.get(...)`` and would crash with AttributeError on a
        non-dict.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "rb") as f:
        for raw in f:
            if len(raw) > _MAX_LINE_BYTES:
                log.warning(
                    "stage2 journal line >%dB in %s; skipping",
                    _MAX_LINE_BYTES, path,
                )
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Tolerant reader must still hand only dicts to downstream
            # consumers — they all assume row.get(...).
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def append_stage2_note(note: "Stage2Note", base: Path) -> None:
    """Append-only write to ``.research/stage2/<note.ticker>.jsonl``.

    Always-append; no dedup at write time (operator may re-run brief
    multiple times per day with different Stage 2 outputs).

    Raises ValueError when the ticker fails ``_validate_ticker`` OR when the
    serialized row exceeds ``_MAX_LINE_BYTES`` after sanitization (should be
    impossible — sanitization caps anchor lengths well under 16 KB — but
    the explicit check keeps a regression from silently persisting an
    oversize prompt-injection payload).
    """
    path = _stage2_path(note.ticker, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _note_to_row(note)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
        # Should be unreachable after sanitization caps. Defense in depth.
        raise ValueError(
            f"Stage 2 row exceeds {_MAX_LINE_BYTES}B; sanitization failed"
        )
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
