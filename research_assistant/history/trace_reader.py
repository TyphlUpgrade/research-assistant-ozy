"""
Per-chain trace event reader (FOLLOWUPS #21).

Cascade trace events live at `.research/traces/<YYYY-MM-DD>/<chain_id>.jsonl`
— one file per cascade chain, one event per stage. The chain_id encodes
its own UTC date (`YYYYMMDDTHHMMSS-XXXXXX`), so chain → file path lookup
is O(1) and the reader doesn't have to walk the trace tree.

The reader's purpose is to enrich `Stage2HistoryEntry` records produced
by the ledger reader. Ledger entries capture chain_id + truncated prose;
the trace captures the matching `conviction_score` (pre-Skeptic, from
Stage 2) and `adjusted_score` (post-Skeptic, from Stage 3) as
structured floats. With both, the scoreboard and trajectory views can
show the post-Skeptic number — and the pre→post delta, which measures
how much the Skeptic actually moved the read.

Missing trace files are not an error. Trace pruning, partial runs, or
chains created on a different machine can all leave a ledger entry
without a backing trace. The reader returns an empty `TraceEnrichment`
in those cases; downstream renderers display the field as `?`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


# chain_id shape: `YYYYMMDDTHHMMSS-XXXXXX` (UTC date+time prefix, dash,
# 6-char random suffix). Confirmed against `.research/traces/*/*.jsonl`
# entries across 2026-05-19 → 2026-06-05.
_CHAIN_ID_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T")


@dataclass(frozen=True)
class TraceEnrichment:
    """Structured fields recovered from one chain's trace file.

    All fields Optional — absence means the trace file was missing /
    unreadable / didn't carry that field. Renderers treat None as "show
    `?`" rather than as a hard error.

    `pre_skeptic_conviction` is the Stage 2 thesis's `conviction_score`
    (or the brief's `composite_conviction` pre-Skeptic, when available).
    `adjusted_score` is Stage 3 Skeptic's discounted number. The delta
    is what the Skeptic moved.
    """
    pre_skeptic_conviction: Optional[float] = None
    adjusted_score: Optional[float] = None
    decision_tag: Optional[str] = None
    conviction_dimensions: Optional[dict] = None
    bull_anchor: Optional[str] = None
    bear_anchor: Optional[str] = None


def _chain_id_to_trace_path(
    chain_id: str, traces_base: Path
) -> Optional[Path]:
    """Resolve chain_id → trace file path using the embedded date prefix.

    Returns None when chain_id doesn't match the expected shape — defends
    against ledger entries whose evidence_anchor was hand-edited or
    written by an older format.
    """
    match = _CHAIN_ID_DATE_RE.match(chain_id)
    if match is None:
        return None
    date_dir = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return traces_base / date_dir / f"{chain_id}.jsonl"


def read_chain_enrichment(
    chain_id: str, traces_base: Path
) -> TraceEnrichment:
    """Return structured Stage 2 + Stage 3 fields for one chain.

    Reads the per-chain JSONL file once, walking each event. Stage 2
    fields (pre-Skeptic conviction, decision_tag, conviction dimensions,
    bull/bear anchors) come from `stage_2_thesis` or `stage_2_note`
    events; `adjusted_score` comes from `stage_3_skeptic`. Returns an
    empty `TraceEnrichment` when the trace file is missing, malformed,
    or unreadable — never raises on absent data.

    Concurrent writers are out of scope (the trace writer in
    `orchestrator.py` is single-process per cascade). The reader is
    pure-read; a half-flushed trace line is skipped via the per-line
    JSON parse guard.
    """
    path = _chain_id_to_trace_path(chain_id, traces_base)
    if path is None or not path.exists():
        return TraceEnrichment()

    pre_skeptic: Optional[float] = None
    adjusted: Optional[float] = None
    decision_tag: Optional[str] = None
    conviction_dims: Optional[dict] = None
    bull: Optional[str] = None
    bear: Optional[str] = None

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                parsed = event.get("parsed")
                if not isinstance(parsed, dict):
                    continue
                stage_id = event.get("stage_id")
                if stage_id in ("stage_2_thesis", "stage_2_note"):
                    if pre_skeptic is None:
                        # Research uses `conviction_score`; brief writes
                        # `composite_conviction` (post-Skeptic). For brief
                        # rows the pre-Skeptic value isn't preserved here,
                        # so callers should prefer the brief journal row
                        # over trace lookup for brief-source entries.
                        candidate = parsed.get("conviction_score")
                        if not isinstance(candidate, (int, float)):
                            candidate = parsed.get("composite_conviction")
                        if isinstance(candidate, (int, float)):
                            pre_skeptic = float(candidate)
                    if decision_tag is None:
                        dt = parsed.get("decision_tag")
                        if isinstance(dt, str) and dt:
                            decision_tag = dt
                    if conviction_dims is None:
                        cd = parsed.get("conviction")
                        if isinstance(cd, dict):
                            conviction_dims = dict(cd)
                    if bull is None:
                        ba = parsed.get("bull_anchor")
                        if isinstance(ba, str) and ba:
                            bull = ba
                    if bear is None:
                        be = parsed.get("bear_anchor")
                        if isinstance(be, str) and be:
                            bear = be
                elif stage_id == "stage_3_skeptic":
                    if adjusted is None:
                        a = parsed.get("adjusted_score")
                        if isinstance(a, (int, float)):
                            adjusted = float(a)
    except OSError as exc:
        log.warning("trace read failed for chain %s: %s", chain_id, exc)
        return TraceEnrichment()

    return TraceEnrichment(
        pre_skeptic_conviction=pre_skeptic,
        adjusted_score=adjusted,
        decision_tag=decision_tag,
        conviction_dimensions=conviction_dims,
        bull_anchor=bull,
        bear_anchor=bear,
    )
