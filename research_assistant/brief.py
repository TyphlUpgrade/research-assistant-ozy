"""
Morning brief orchestrator — `/brief` on-demand surface.

Pipeline:
1. Load watchlist from `.research/watchlist.txt` (newline-delimited tickers,
   `#` comments allowed)
2. Build Stage 0 world state via the cascade prompt (cached per ET date in
   `.research/briefs/<date-ET>.json` so a second /brief same day is cheap)
3. Stage 1 batched filter (Haiku) — one call covering all watchlist tickers
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
from research_assistant.orchestrator import _load_prompt, _render, _chain_id
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


async def _stage_1_filter(
    client: ClaudeClient, world_state: dict, candidates: list[dict]
) -> Optional[dict]:
    template = _load_prompt("stage_1_filter")
    prompt = _render(template, candidates_json=json.dumps(candidates, indent=2))
    system = f"WORLD_STATE:\n{json.dumps(world_state, indent=2)}"
    raw = await client.call(prompt, model="claude-haiku-4-5-20251001", system=system)
    return parse_claude_response(raw.text)


async def _stage_2_for_survivor(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    stage_1_result: dict,
    headlines: list[dict],
    semaphore: asyncio.Semaphore,
) -> Optional[dict]:
    """Stage 2 thesis for one survivor, semaphore-bounded."""
    async with semaphore:
        from research_assistant.orchestrator import _stage_2_thesis
        stage_2, _meta = await _stage_2_thesis(
            client, world_state, ticker_data, stage_1_result, headlines
        )
        return stage_2


# ---------------------------------------------------------------------------
# Brief builder
# ---------------------------------------------------------------------------

async def build_brief(
    *,
    market_context: dict,
    universe: list[str],
    watchlist_tickers_with_data: dict[str, dict],
    headlines_per_ticker: dict[str, list[dict]],
    research_base: Path,
    client: Optional[ClaudeClient] = None,
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

    # Stage 1 — batched filter
    candidates = [
        {"ticker": ticker, **data}
        for ticker, data in watchlist_tickers_with_data.items()
    ]
    stage_1 = await _stage_1_filter(client, world_state, candidates)
    if stage_1 is None:
        raise RuntimeError("Stage 1 (batched filter) JSON parse failed")

    # Take top N survivors (within [min, max] range)
    ranked = sorted(
        stage_1.get("results", []),
        key=lambda r: r.get("intrinsic_score", 0.0),
        reverse=True,
    )
    survivors = ranked[:SURVIVORS_PER_BRIEF[1]]
    # Drop low-conviction survivors below min count threshold
    if len(survivors) > SURVIVORS_PER_BRIEF[0]:
        survivors = [s for s in survivors if s.get("intrinsic_score", 0.0) >= 0.4]
        survivors = survivors[:SURVIVORS_PER_BRIEF[1]]
        # Guarantee minimum if available
        if len(survivors) < SURVIVORS_PER_BRIEF[0] and len(ranked) >= SURVIVORS_PER_BRIEF[0]:
            survivors = ranked[:SURVIVORS_PER_BRIEF[0]]

    # Stage 2 — parallel theses with semaphore cap
    semaphore = asyncio.Semaphore(_STAGE_2_CONCURRENCY)
    stage_2_tasks = [
        _stage_2_for_survivor(
            client,
            world_state,
            watchlist_tickers_with_data.get(s["ticker"], {}),
            s,
            headlines_per_ticker.get(s["ticker"], []),
            semaphore,
        )
        for s in survivors
    ]
    stage_2_results = await asyncio.gather(*stage_2_tasks)

    items: list[BriefItem] = []
    for survivor, stage_2 in zip(survivors, stage_2_results):
        item = BriefItem(
            ticker=survivor["ticker"],
            intrinsic_score=survivor.get("intrinsic_score", 0.0),
            stage_1_reason=survivor.get("reason", ""),
            chain_id=chain,
        )
        if stage_2 is not None:
            item.thesis_text = stage_2.get("thesis_text")
            item.conviction_score = stage_2.get("conviction_score")
            item.key_drivers = stage_2.get("key_drivers", [])
            item.risks = stage_2.get("risks", [])
            item.open_questions = stage_2.get("open_questions", [])
            item.evidence_anchors = stage_2.get("evidence_anchors", [])
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
    # to detect "brief generated today?"
    briefs_dir = research_base / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    cache_path = briefs_dir / f"{date_et}.json"
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
            }
            for i in brief.items
        ],
        "cost_usd": brief.cost_usd,
        "discovered_universe": brief.discovered_universe,
    }
    cache_path.write_text(json.dumps(cache_payload, indent=2))

    return brief


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

    lines.append(f"## Opportunity surface ({len(brief.items)} items)")
    for item in brief.items:
        conviction_str = (
            f"conviction {item.conviction_score:.2f}"
            if item.conviction_score is not None else "(no thesis)"
        )
        thesis_preview = (item.thesis_text or item.stage_1_reason or "")[:140]
        lines.append(f"- **{item.ticker}** — {conviction_str}: {thesis_preview}")
    lines.append("")
    lines.append(f"_Run `/research <TICKER>` for full DD. Trace chain: `{brief.chain_id}`._")
    return "\n".join(lines)


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
