"""
Unified Stage 2 history reader (FOLLOWUPS #21).

Reads from both write paths and projects to a common entry shape:

- Brief journal at `.research/stage2/<TKR>.jsonl` — structured rows
  written by `brief.py` (inline Skeptic vocabulary:
  AGREE / WEAKEN / STRONG_OBJECTION).
- Dossier ledger at `.research/tickers/<TKR>.md` `## Ledger` section —
  free-text rows written by `orchestrator.py` for `/research` runs
  (Stage 3 Skeptic vocabulary:
  AGREE / WEAKEN / TEMPER / CHALLENGE / STRONG_OBJECTION).

The reader is the substrate for `trajectory` and for the planned
`/history` operator surface (FOLLOWUPS #22). Conviction numbers and
structured fields degrade gracefully on ledger-only entries (the
ledger summary is lossy 160-char text — verdict is regex-extractable
but the composite conviction number is not).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from research_assistant.dossier_io import read_dossier
from research_assistant.history.trace_reader import (
    TraceEnrichment,
    read_chain_enrichment,
)
from research_assistant.journal import read_stage2_full_history


_ET = ZoneInfo("America/New_York")

# Matches the leading "Verdict: WORD." or "Verdict: WORD " in a Skeptic
# critique. Stage 3 critiques in the orchestrator start with this prefix
# (see `orchestrator.py` Stage 3 prompt contract).
_VERDICT_PREFIX_RE = re.compile(r"^Verdict:\s*([A-Z_]+)\b")

# Recognized Skeptic verdict words across both vocabularies. The reader
# accepts anything it can find; this set documents the contract for
# downstream filtering / sorting.
KNOWN_VERDICTS = frozenset(
    {"AGREE", "WEAKEN", "TEMPER", "CHALLENGE", "STRONG_OBJECTION"}
)


@dataclass(frozen=True)
class Stage2HistoryEntry:
    """One Stage 2 history datapoint, source-discriminated.

    Numeric conviction fields:
    - `composite_conviction` — post-Skeptic, the number the cascade
      actually publishes to the operator (post-discount).
    - `pre_skeptic_conviction` — pre-Skeptic, the raw Stage 2 thesis
      score. The pre→post delta is the Skeptic's actual discount.

    For research entries, these are sourced from the cascade trace JSONL
    at `traces/<date>/<chain_id>.jsonl` (the ledger summary itself is
    lossy 160-char prose). When the trace is missing — pruned, partial,
    or written on a different machine — the fields render as None.
    """
    ticker: str
    asof: str                              # ISO date, ET trading calendar
    recorded_at: str                       # ISO timestamp UTC
    source: Literal["brief", "research"]
    chain_id: Optional[str] = None
    composite_conviction: Optional[float] = None
    pre_skeptic_conviction: Optional[float] = None
    skeptic_verdict: Optional[str] = None
    decision_tag: Optional[str] = None
    conviction_dimensions: Optional[dict] = None
    bull_anchor: Optional[str] = None
    bear_anchor: Optional[str] = None


def _et_date_from_iso(ts: str) -> str:
    """Convert an ISO timestamp to its ET trading-day date.

    Ledger timestamps are UTC; the journal uses ET for `asof`. Projecting
    both onto ET keeps the chronological union sortable by `(asof,
    recorded_at)` without surprise off-by-one-day drift after 8 PM ET.
    """
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(_ET).date().isoformat()
    except ValueError:
        return ts[:10] if len(ts) >= 10 else ts


def _extract_verdict(skeptic_summary: str) -> Optional[str]:
    """Pull the verdict word from a skeptic critique's `Verdict: X` prefix.

    Returns None when no recognizable verdict header is present (the
    160-char ledger summary may truncate mid-word in degenerate cases;
    the trajectory renderer renders `UNAVAILABLE` in that case).
    """
    if not skeptic_summary:
        return None
    match = _VERDICT_PREFIX_RE.match(skeptic_summary.strip())
    if match is None:
        return None
    word = match.group(1).upper()
    return word if word in KNOWN_VERDICTS else None


def _strip_verdict_prefix(skeptic_summary: str) -> str:
    """Drop the leading `Verdict: WORD.` from a skeptic summary.

    The verdict word is surfaced separately in the rendered row header,
    so repeating it in the bear column is operator-visible noise.
    """
    if not skeptic_summary:
        return ""
    # Match the prefix plus the trailing period and any whitespace that
    # follows it. Falls through to the original string when the prefix
    # doesn't match the expected shape.
    stripped = re.sub(
        r"^Verdict:\s*[A-Z_]+\.\s*", "", skeptic_summary.strip()
    )
    return stripped


def _journal_rows_to_entries(
    ticker: str, rows: list[dict]
) -> list[Stage2HistoryEntry]:
    """Project journal jsonl rows into Stage2HistoryEntry, source=brief."""
    entries: list[Stage2HistoryEntry] = []
    for row in rows:
        composite = row.get("composite_conviction")
        if not isinstance(composite, (int, float)):
            composite = None
        conviction = row.get("conviction")
        if not isinstance(conviction, dict):
            conviction = None
        entries.append(
            Stage2HistoryEntry(
                ticker=ticker,
                asof=str(row.get("asof", "")),
                recorded_at=str(row.get("recorded_at", "")),
                source="brief",
                chain_id=None,  # journal schema doesn't carry chain_id today
                composite_conviction=float(composite) if composite is not None else None,
                skeptic_verdict=row.get("skeptic_verdict") or None,
                decision_tag=row.get("decision_tag") or None,
                conviction_dimensions=dict(conviction) if conviction else None,
                bull_anchor=row.get("bull_anchor") or None,
                bear_anchor=row.get("bear_anchor") or None,
            )
        )
    return entries


def _ledger_entries_to_history(
    ticker: str, base: Path, traces_base: Path
) -> list[Stage2HistoryEntry]:
    """Project ledger `thesis:`+`skeptic:` pairs into Stage2HistoryEntry.

    Pairs ledger entries by shared `evidence_anchor` (chain_id). For each
    chain, opens the matching trace file to recover the structured
    `conviction_score` (Stage 2) and `adjusted_score` (Stage 3) — the
    ledger summary itself is lossy prose. Orphan thesis-without-skeptic
    pairs are still emitted so the operator sees partial reads.
    """
    dossier = read_dossier(ticker, base)
    if dossier is None:
        return []

    by_chain: dict[str, dict[str, object]] = {}
    for entry in dossier.ledger:
        anchor = entry.evidence_anchor
        if anchor is None or entry.kind not in ("thesis", "skeptic"):
            continue
        bucket = by_chain.setdefault(anchor, {})
        bucket[entry.kind] = entry  # latest write wins on a duplicate kind

    history: list[Stage2HistoryEntry] = []
    for chain_id, bucket in by_chain.items():
        thesis = bucket.get("thesis")
        skeptic = bucket.get("skeptic")
        ref = thesis or skeptic
        if ref is None:
            continue
        ts = ref.timestamp  # type: ignore[union-attr]
        enrichment = read_chain_enrichment(chain_id, traces_base)
        history.append(
            Stage2HistoryEntry(
                ticker=ticker,
                asof=_et_date_from_iso(ts),
                recorded_at=ts,
                source="research",
                chain_id=chain_id,
                composite_conviction=enrichment.adjusted_score,
                pre_skeptic_conviction=enrichment.pre_skeptic_conviction,
                skeptic_verdict=(
                    _extract_verdict(skeptic.summary) if skeptic else None  # type: ignore[union-attr]
                ),
                decision_tag=enrichment.decision_tag,
                conviction_dimensions=enrichment.conviction_dimensions,
                bull_anchor=thesis.summary if thesis else None,  # type: ignore[union-attr]
                bear_anchor=(
                    _strip_verdict_prefix(skeptic.summary) if skeptic else None  # type: ignore[union-attr]
                ),
            )
        )
    return history


def read_unified_history(
    ticker: str, base: Path, *, traces_base: Optional[Path] = None,
) -> list[Stage2HistoryEntry]:
    """Return the chronological union of brief journal + research ledger.

    Oldest-first. Sort key is `(asof, recorded_at)` so ties on asof
    (multiple reads same trading day, e.g. brief at 14:30 + research
    at 14:32) preserve write order.

    A single chain that wrote to BOTH paths (today's `/brief` followed
    by today's `/research` on the same ticker) appears as two entries —
    they ARE two distinct events (different chain_ids, different
    Skeptic vocabularies). The operator surface labels them by source.

    `traces_base` defaults to `base / "traces"`. Override for testing
    (so a fixture can point the reader at an isolated trace tree
    independent of the main `base` path).
    """
    ticker_upper = ticker.upper()
    traces = traces_base if traces_base is not None else (base / "traces")
    journal = _journal_rows_to_entries(
        ticker_upper, read_stage2_full_history(ticker_upper, base)
    )
    ledger = _ledger_entries_to_history(ticker_upper, base, traces)
    combined = journal + ledger
    combined.sort(key=lambda e: (e.asof, e.recorded_at))
    return combined
