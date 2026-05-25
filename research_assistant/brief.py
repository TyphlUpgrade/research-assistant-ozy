"""
Morning brief orchestrator — `/brief` on-demand surface.

Pipeline:
1. Load watchlist from `.research/watchlist.txt` (newline-delimited tickers,
   `#` comments allowed)
2. Build Stage 0 world state via the cascade prompt (cached per ET date in
   `.research/briefs/<date-ET>.json` so a second /brief same day is cheap)
3. Stage 1 deterministic composite — `_stage_1_composite` ranks every
   watchlist ticker via a pure-function scoring pass over ticker_data,
   insider summaries, world_state and screener alerts (PR 2A.1; replaces
   the previous Haiku-driven Stage 1 batched filter)
4. Top 4-8 survivors → Stage 2 thesis (Sonnet) in parallel, semaphore-bounded
   per Critic iter1 #17 (concurrency cap to respect Anthropic + yfinance rate
   limits)
5. Build layered output: top-level scannable summary + drill-down per item
6. Cache the full brief to `.research/briefs/<date-ET>.json` for /trace and
   for the SessionStart hook's "brief exists today?" check

Stage 3 (Skeptic) is INTENTIONALLY skipped in /brief. Running Skeptic on 4-8
surface tickers per morning costs more than the marginal value — the user
selects a name they want to probe and runs `/research <TICKER>` for the full
Stage 2 + Stage 3 + Defender treatment.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from research_assistant.claude_sdk import ClaudeClient
from research_assistant.edgar import InsiderActivitySummary
from research_assistant.observations import Observation, append_observation, now_iso
from research_assistant.prompts import chain_id as _chain_id
from research_assistant.prompts import load_prompt as _load_prompt
from research_assistant.prompts import render as _render
from research_assistant.screeners import SetupCandidate
from research_assistant.trace_renderer import append_stage_event
from ozymandias.intelligence.claude_json import parse_claude_response

log = logging.getLogger(__name__)


ET = ZoneInfo("America/New_York")

# Concurrency cap per Critic iter1 #17 (yfinance + Anthropic rate-limit awareness)
_STAGE_2_CONCURRENCY = 5

# Survivor count from Stage 1 advancing to Stage 2
SURVIVORS_PER_BRIEF = (4, 8)  # min, max


@dataclass
class BriefItem:
    """One ticker in the /brief opportunity surface."""
    ticker: str
    intrinsic_score: float                # Stage 1
    stage_1_reason: str
    thesis_text: Optional[str] = None     # Stage 2
    conviction_score: Optional[float] = None
    key_drivers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    evidence_anchors: list[dict] = field(default_factory=list)
    chain_id: Optional[str] = None
    # PR 2A.1: screener hits that pinned this ticker. Each dict mirrors the
    # SetupCandidate.evidence dict plus a "screener" key so the render layer
    # can show e.g. `[sector_rotation: rank 7→2 on 30d basis]` inline. Empty
    # list means "no screener fired on this ticker."
    screener_evidence: list[dict] = field(default_factory=list)


@dataclass
class Brief:
    """The complete morning brief — one per ET trading day."""
    date_et: str
    chain_id: str
    world_state: dict
    items: list[BriefItem]
    cost_usd: float
    # Tickers fed into Stage 1; persisted so `/brief --refresh` re-runs the
    # LLM pipeline against the same universe without intra-day Yahoo drift.
    # Pass --rediscover (or --static-only) to override.
    discovered_universe: list[str] = field(default_factory=list)
    # NOTE on the dropped `setups` field (PR 2A.1):
    # The setups list previously lived at the Brief level (rendered as a
    # standalone `## Setups` block above the opportunity surface). Per the
    # plan §PR 2A.1, screener hits now attach to individual BriefItems via
    # `screener_evidence`, and the render layer surfaces them inline.
    # Backward-compat on cached briefs: the cli cache loader drops the
    # legacy `setups` key cleanly; new code never persists it.


# ---------------------------------------------------------------------------
# Watchlist loader
# ---------------------------------------------------------------------------

def load_watchlist(base: Path) -> list[str]:
    """Read .research/watchlist.txt. Skip blank lines and `#` comments."""
    path = base / "watchlist.txt"
    if not path.exists():
        log.warning("Watchlist not found at %s — returning empty", path)
        return []
    tickers: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.append(line.upper())
    return tickers


# ---------------------------------------------------------------------------
# Stage invocations (Stage 0 + Stage 1; Stage 2 reuses orchestrator helpers)
# ---------------------------------------------------------------------------

async def _stage_0_world_state(client: ClaudeClient, context: dict) -> Optional[dict]:
    template = _load_prompt("world_state")
    prompt = _render(template, context_json=json.dumps(context, indent=2))
    raw = await client.call(prompt, model="claude-sonnet-4-6")
    return parse_claude_response(raw.text)


def _breakdown_summary(breakdown: dict) -> str:
    """Render a composite breakdown dict as a one-line operator summary.

    Picks the top 3 named signal contributions by absolute value (skipping
    `baseline` and `regime_multiplier` since they're applied to every
    ticker). Empty breakdown → empty string. Format example:
        "trend_strong +0.10, screener_confirmations +0.16, sector_aligned +0.05"
    """
    if not breakdown:
        return ""
    skip = {"baseline", "regime_multiplier", "distinct_screener_sources"}
    contributions: list[tuple[str, float]] = []
    for key, value in breakdown.items():
        if key in skip:
            continue
        if isinstance(value, bool):
            # Boolean breakdown flags (parabolic_cap, insider_selling_cap)
            # surface as labelled markers rather than signed numbers.
            if value:
                contributions.append((key, 0.0))
            continue
        if isinstance(value, (int, float)):
            contributions.append((key, float(value)))
    if not contributions:
        return ""
    contributions.sort(key=lambda kv: -abs(kv[1]))
    parts = [
        f"{k} {v:+.2f}" if v != 0.0 else f"{k}"
        for k, v in contributions[:3]
    ]
    return ", ".join(parts)


def _stage_1_composite(
    world_state: dict,
    ticker_data_by_symbol: dict[str, dict],
    insider_activities: dict[str, Optional[InsiderActivitySummary]],
    screener_alerts: list[SetupCandidate],
) -> list[dict]:
    """Deterministic Stage-1 ranking (PR 2A.1).

    Pure function — no I/O, no LLM. Replaces `_stage_1_filter` (Haiku call).
    For every ticker in `ticker_data_by_symbol` it computes an intrinsic
    score + breakdown via `compute_intrinsic_score`, attaches any matching
    screener_alerts (keyed by ticker), and returns a list sorted by
    `intrinsic_score` descending.

    Output shape per item: `{ticker, intrinsic_score, breakdown,
    screener_evidence}`. Downstream Stage-2 code reads `ticker` +
    `intrinsic_score`; `breakdown` and `screener_evidence` are additional
    fields for trace logging and rendering. The breakdown is intentionally
    NOT threaded into Stage-2 inputs — see plan §"Core principle 1".
    """
    from research_assistant.composite import compute_intrinsic_score

    # Group screener alerts by ticker so each ticker sees only its hits.
    alerts_by_ticker: dict[str, list[SetupCandidate]] = {}
    for alert in screener_alerts or []:
        alerts_by_ticker.setdefault(alert.ticker.upper(), []).append(alert)

    results: list[dict] = []
    for ticker, ticker_data in ticker_data_by_symbol.items():
        upper = ticker.upper()
        ticker_alerts = alerts_by_ticker.get(upper, [])
        score, breakdown = compute_intrinsic_score(
            ticker_data=ticker_data,
            insider_summary=insider_activities.get(upper),
            world_state=world_state,
            screener_alerts=ticker_alerts,
        )
        results.append({
            "ticker": upper,
            "intrinsic_score": score,
            "breakdown": breakdown,
            "screener_evidence": [
                {"screener": a.screener, **(a.evidence or {})}
                for a in ticker_alerts
            ],
        })

    results.sort(key=lambda r: r.get("intrinsic_score", 0.0), reverse=True)
    return results


async def _stage_2_for_survivor(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    stage_1_result: dict,
    headlines: list[dict],
    semaphore: asyncio.Semaphore,
    *,
    chain_id: str,
    traces_base: Path,
) -> Optional[dict]:
    """Stage 2 thesis for one survivor, semaphore-bounded.

    Emits a `stage_2_thesis` trace event stamped with the survivor's symbol
    so downstream Defender citation verification can scope the anchor corpus
    to one survivor (the chain_id is shared across all survivors in a brief).
    """
    async with semaphore:
        from research_assistant.orchestrator import _stage_2_thesis
        stage_2, s2_meta = await _stage_2_thesis(
            client, world_state, ticker_data, stage_1_result, headlines
        )
        append_stage_event(
            chain_id=chain_id,
            stage_id="stage_2_thesis",
            model=s2_meta.model if s2_meta else "unknown",
            tokens_in=s2_meta.input_tokens if s2_meta else 0,
            tokens_out=s2_meta.output_tokens if s2_meta else 0,
            cost_usd=s2_meta.cost_usd if s2_meta else 0.0,
            latency_ms=s2_meta.latency_ms if s2_meta else 0,
            parsed=stage_2,
            raw_response=s2_meta.text if s2_meta else None,
            traces_base=traces_base,
            error=None if stage_2 else "Stage 2 JSON parse failed",
            symbol=stage_1_result.get("ticker"),
        )
        return stage_2


# ---------------------------------------------------------------------------
# Brief builder
# ---------------------------------------------------------------------------

def _insider_summary_line(
    summary: Optional[InsiderActivitySummary],
) -> str:
    """Stage 1 candidate-line rendering for FOLLOWUPS #3.
    None → unavailable; empty window → distinct neutral string; populated
    → stage_1_line() (e.g. 'insider net flow last 90d: -$42.0M / 4 sales / 0 buys')."""
    if summary is None:
        return "(insider data unavailable)"
    if summary.total_filings == 0:
        return f"(no Form 4 last {summary.window_days}d)"
    return summary.stage_1_line()


async def build_brief(
    *,
    market_context: dict,
    universe: list[str],
    watchlist_tickers_with_data: dict[str, dict],
    headlines_per_ticker: dict[str, list[dict]],
    research_base: Path,
    client: Optional[ClaudeClient] = None,
    insider_activities: Optional[
        dict[str, Optional[InsiderActivitySummary]]
    ] = None,
    screener_alerts: Optional[list[SetupCandidate]] = None,
) -> Brief:
    """
    Build the morning brief. Caller provides pre-loaded market context +
    per-ticker data + headlines (research_assistant doesn't itself fetch yfinance
    in v1 — the /brief skill wires that up at invocation time).

    Args:
        market_context: output of `research_assistant.market_context.build_research_context(...)`
        watchlist_tickers_with_data: dict of {ticker: ticker_data_dict} for the
            full watchlist
        headlines_per_ticker: dict of {ticker: [headline_dicts]}
        research_base: path to `.research/` directory
        client: optional ClaudeClient (cost continuity across stages)
        insider_activities: optional dict of {ticker: Optional[InsiderActivitySummary]}
            from `load_insider_activities_batch` (FOLLOWUPS #3). Threaded into
            the deterministic Stage-1 composite scorer so insider buying /
            severe selling caps influence ranking. None values per-ticker are
            tolerated (graceful degrade per failed ticker).
        screener_alerts: optional list of SetupCandidate from
            `run_screeners_and_journal`. PR 2A.1: alerts feed the composite
            score (multi-source confirmation bonus) AND attach as inline
            `screener_evidence` on each surviving BriefItem.

    Returns:
        Brief with world_state + ranked items. Cached to
        `.research/briefs/<date-ET>.json`.
    """
    if client is None:
        client = ClaudeClient()

    chain = _chain_id()
    date_et = datetime.now(ET).date().isoformat()

    # Stage 0 — world state
    world_state = await _stage_0_world_state(client, market_context)
    if world_state is None:
        raise RuntimeError("Stage 0 (world state) JSON parse failed")

    # Stage 1 — deterministic composite (PR 2A.1). No LLM call here.
    insider_activities = insider_activities or {}
    screener_alerts = list(screener_alerts or [])
    ranked = _stage_1_composite(
        world_state=world_state,
        ticker_data_by_symbol=watchlist_tickers_with_data,
        insider_activities=insider_activities,
        screener_alerts=screener_alerts,
    )

    # Take top N survivors (within [min, max] range)
    survivors = ranked[:SURVIVORS_PER_BRIEF[1]]
    # Drop low-conviction survivors below min count threshold
    if len(survivors) > SURVIVORS_PER_BRIEF[0]:
        survivors = [s for s in survivors if s.get("intrinsic_score", 0.0) >= 0.4]
        survivors = survivors[:SURVIVORS_PER_BRIEF[1]]
        # Guarantee minimum if available
        if len(survivors) < SURVIVORS_PER_BRIEF[0] and len(ranked) >= SURVIVORS_PER_BRIEF[0]:
            survivors = ranked[:SURVIVORS_PER_BRIEF[0]]

    # Stage 2 — parallel theses with semaphore cap.
    # Strip the composite `breakdown` from each survivor before threading
    # into Stage 2: per the plan's data-isolation principle, Stage 2 must
    # do original analysis from raw ticker_data, not rationalize an
    # upstream score. `ticker` + `intrinsic_score` is the minimum Stage 2
    # downstream code expects.
    semaphore = asyncio.Semaphore(_STAGE_2_CONCURRENCY)
    traces_base = research_base / "traces"
    stage_2_tasks = [
        _stage_2_for_survivor(
            client,
            world_state,
            watchlist_tickers_with_data.get(s["ticker"], {}),
            {"ticker": s["ticker"], "intrinsic_score": s.get("intrinsic_score", 0.0)},
            headlines_per_ticker.get(s["ticker"], []),
            semaphore,
            chain_id=chain,
            traces_base=traces_base,
        )
        for s in survivors
    ]
    stage_2_results = await asyncio.gather(*stage_2_tasks)

    items: list[BriefItem] = []
    regime = world_state.get("regime") if isinstance(world_state, dict) else None
    obs_ts = now_iso()
    for survivor, stage_2 in zip(survivors, stage_2_results):
        item = BriefItem(
            ticker=survivor["ticker"],
            intrinsic_score=survivor.get("intrinsic_score", 0.0),
            # PR 2A.1: stage_1_reason is a compact composite-breakdown
            # summary string (no LLM-authored prose) — the operator sees
            # which signals lifted this ticker into Stage 2 without needing
            # the trace.
            stage_1_reason=_breakdown_summary(survivor.get("breakdown") or {}),
            screener_evidence=list(survivor.get("screener_evidence", [])),
            chain_id=chain,
        )
        if stage_2 is not None:
            item.thesis_text = stage_2.get("thesis_text")
            item.conviction_score = stage_2.get("conviction_score")
            item.key_drivers = stage_2.get("key_drivers", [])
            item.risks = stage_2.get("risks", [])
            item.open_questions = stage_2.get("open_questions", [])
            item.evidence_anchors = stage_2.get("evidence_anchors", [])
            append_observation(
                Observation(
                    ts=obs_ts,
                    kind="brief",
                    symbol=item.ticker,
                    chain_id=chain,
                    thesis=item.thesis_text or "",
                    conviction=item.conviction_score,
                    regime=regime,
                    drivers=list(item.key_drivers),
                    risks=list(item.risks),
                    open_questions=list(item.open_questions),
                    anchors=list(item.evidence_anchors),
                ),
                research_base,
            )
        items.append(item)

    brief = Brief(
        date_et=date_et,
        chain_id=chain,
        world_state=world_state,
        items=items,
        cost_usd=client.cost.total_usd,
        discovered_universe=list(universe),
    )

    # Cache to .research/briefs/<date-ET>.json — used by SessionStart hook
    # to detect "brief generated today?". PR 1.3: `setups` may be re-written
    # by `_cmd_brief` after `run_screeners_and_journal` returns — see
    # `write_brief_cache` below.
    write_brief_cache(brief, research_base)

    return brief


def write_brief_cache(brief: Brief, research_base: Path) -> Path:
    """Write the brief cache JSON. Called by `build_brief` after Stage 2
    completion AND by `_cmd_brief` after `run_screeners_and_journal` runs —
    re-writing is cheap and keeps the on-disk cache canonical.
    """
    briefs_dir = research_base / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    cache_path = briefs_dir / f"{brief.date_et}.json"
    cache_payload = {
        "date_et": brief.date_et,
        "chain_id": brief.chain_id,
        "world_state": brief.world_state,
        "items": [
            {
                "ticker": i.ticker,
                "intrinsic_score": i.intrinsic_score,
                "stage_1_reason": i.stage_1_reason,
                "thesis_text": i.thesis_text,
                "conviction_score": i.conviction_score,
                "key_drivers": i.key_drivers,
                "risks": i.risks,
                "open_questions": i.open_questions,
                "evidence_anchors": i.evidence_anchors,
                # PR 2A.1: per-item screener hits, threaded through to the
                # render layer so the unified opportunity surface can show
                # `[screener: evidence]` inline.
                "screener_evidence": i.screener_evidence,
            }
            for i in brief.items
        ],
        "cost_usd": brief.cost_usd,
        "discovered_universe": brief.discovered_universe,
    }
    cache_path.write_text(json.dumps(cache_payload, indent=2))
    return cache_path


def render_brief_top_level(brief: Brief) -> str:
    """
    Scannable top-level summary (~5min read).

    Format:
      - regime + dispersion + top macro signals (1 paragraph)
      - top 4-8 opportunities: ticker + 1-line thesis + conviction
      - "Run `/research <TICKER>` for full DD on any item."
    """
    lines = [f"# Morning Brief — {brief.date_et} (ET)", ""]
    ws = brief.world_state
    lines.append("## Market regime")
    lines.append(
        f"- **Regime:** {ws.get('regime', '?')} "
        f"(confidence {ws.get('regime_confidence', 0):.2f})"
    )
    lines.append(
        f"- **Dispersion:** {ws.get('dispersion', 0):.2f}  ·  "
        f"**Rationale:** {ws.get('rationale', '')}"
    )
    macro = ws.get("macro_signals", {})
    if macro:
        catalysts = macro.get("active_catalysts", [])
        lines.append(
            f"- **VIX:** {macro.get('vix_level', '?')} ({macro.get('vix_trend', '?')})  ·  "
            f"**Active catalysts:** {', '.join(catalysts) if catalysts else 'none'}"
        )
    lines.append("")

    # PR 2A.1: Unified opportunity surface — no more standalone `## Setups`
    # section. Screener hits surface inline per item as
    # `[screener: evidence]` suffixes when present.
    lines.append(f"## Opportunity surface ({len(brief.items)} items)")
    for item in brief.items:
        conviction_str = (
            f"conviction {item.conviction_score:.2f}"
            if item.conviction_score is not None else "(no thesis)"
        )
        evidence_suffix = _render_screener_evidence_inline(item.screener_evidence)
        thesis_preview = (item.thesis_text or item.stage_1_reason or "")[:140]
        line = f"- **{item.ticker}** — {conviction_str}: "
        if evidence_suffix:
            line += f"{evidence_suffix} {thesis_preview}"
        else:
            line += thesis_preview
        lines.append(line)
    lines.append("")
    lines.append(f"_Run `/research <TICKER>` for full DD. Trace chain: `{brief.chain_id}`._")
    return "\n".join(lines)


def _render_screener_evidence_inline(evidence_list: list[dict]) -> str:
    """Render per-item screener hits as `[screener: evidence-summary]` blobs.

    Examples (sector_rotation):
        `[sector_rotation: rank 7→2 on 30d basis]`
    Multiple hits chain with spaces. Empty list → empty string.
    """
    if not evidence_list:
        return ""
    parts: list[str] = []
    for ev in evidence_list:
        screener = ev.get("screener", "screener")
        summary = _summarize_evidence(screener, ev)
        if summary:
            parts.append(f"[{screener}: {summary}]")
        else:
            parts.append(f"[{screener}]")
    return " ".join(parts)


def _summarize_evidence(screener: str, ev: dict) -> str:
    """Per-screener compact evidence string. Centralised here so the brief
    render layer doesn't need to learn each screener's evidence schema.

    Unknown screeners surface as the bare `[screener]` tag (empty summary).
    """
    if screener == "sector_rotation":
        rp = ev.get("rs_rank_prior")
        rn = ev.get("rs_rank_now")
        basis = ev.get("basis_days")
        if rp is not None and rn is not None and basis is not None:
            return f"rank {rp}→{rn} on {basis}d basis"
    return ""


def render_brief_drill_down(brief: Brief, ticker: str) -> str:
    """Full per-item detail with anchors inline."""
    item = next((i for i in brief.items if i.ticker == ticker.upper()), None)
    if item is None:
        return f"No brief item for {ticker}. Items: {[i.ticker for i in brief.items]}"

    lines = [f"# {item.ticker} — Brief drill-down ({brief.date_et} ET)", ""]
    if item.thesis_text:
        lines.append(f"**Thesis:** {item.thesis_text}")
        lines.append(f"**Conviction:** {item.conviction_score:.2f}")
    else:
        lines.append(f"**Stage 1 reason:** {item.stage_1_reason}")
        lines.append(f"(Stage 2 thesis not generated — ticker below Stage 1 floor.)")
    lines.append("")

    if item.key_drivers:
        lines.append("**Key drivers:**")
        anchors_by_claim = {
            a.get("claim", "").lower(): a.get("source", "")
            for a in (item.evidence_anchors or [])
        }
        for d in item.key_drivers:
            anchor = anchors_by_claim.get(d.lower(), "[NO ANCHOR — visibility regression]")
            lines.append(f"- {d}  ← {anchor}")
    if item.risks:
        lines.append("\n**Risks:**")
        for r in item.risks:
            lines.append(f"- {r}")
    if item.open_questions:
        lines.append("\n**Open questions to probe:**")
        for q in item.open_questions:
            lines.append(f"- {q}")
    lines.append("")
    lines.append(f"_Run `/research {item.ticker}` for full Skeptic + Defender DD._")
    return "\n".join(lines)
