---
name: brief
description: On-demand morning market brief. Builds world-state regime classification + ranked opportunity surface (top 4-8 from user's static watchlist) with one-line thesis per item. Layered output — scannable top-level summary first, drill-down via `/brief <TICKER>` for any item. No Stage 3 Skeptic in brief; user runs `/research <TICKER>` for the full bias-defense treatment on names they want to probe.
---

When the user runs `/brief` (no args) or natural-language equivalents ("what's the market doing", "morning brief", "what should I look at today"):

1. **Check today's cache.** Look for `.research/briefs/<date-ET>.json` where `<date-ET>` is computed via `datetime.now(ZoneInfo("America/New_York")).date().isoformat()`. If cached and the user has not specified `--refresh`, render from cache (cheap, fast).

2. **If no cache:**
   a. Load watchlist from `.research/watchlist.txt` via `load_watchlist(base=Path(".research"))`.
   b. Fetch market context (SPY/QQQ bars, VIX, sector ETFs, macro headlines) via yfinance_adapter. Wrap via `research_assistant.market_context.build_research_context(...)`.
   c. Fetch per-watchlist-ticker data (price, return_5d, return_30d, return_90d, weekly_rsi_14, volume_5d_trend) and recent headlines.
   d. Call `research_assistant.brief.build_brief(...)` — runs Stage 0 + Stage 1 + parallel Stage 2 (semaphore-bounded at 5 concurrent).
   e. Brief is auto-cached at `.research/briefs/<date-ET>.json`.

3. **Render top-level** via `render_brief_top_level(brief)`:
   - Regime + dispersion + macro signals (1 paragraph)
   - Top 4-8 opportunities, each with: ticker, conviction, 1-line thesis preview
   - "Run `/research <TICKER>` for full DD" footer + chain ID

4. **Drill-down**: when the user asks for more on a specific ticker from the brief (`/brief NVDA`, "tell me more about NVDA from the brief", "expand NVDA"), render `render_brief_drill_down(brief, ticker)` showing:
   - Full thesis text
   - Conviction score
   - Key drivers with inline `[anchor: tool_call_X]` citations
   - Named risks
   - Open questions to probe further
   - "Run `/research <TICKER>` for full Skeptic + Defender DD" footer

5. **Visibility regression flagging**: in drill-down, any key driver whose claim text has no matching `evidence_anchors` entry shows as `[NO ANCHOR — visibility regression]` — the visibility-axis quality contract surfaces immediately.

## Why no Skeptic in /brief

Stage 3 Skeptic costs Sonnet × 8 calls per morning brief = significant daily spend even on idle days. The /brief surface is meant for SCANNING — the user picks 1-3 names they want to probe and runs `/research <TICKER>` for the full Stage 2 + Stage 3 + Defender treatment. This keeps /brief cheap (~$0.20-0.50 per generation) and concentrates the expensive Skeptic call on the names where the user is actually about to do something.

## Failure modes

- Watchlist file empty / missing → friendly error: "Add tickers to `.research/watchlist.txt` (one per line). Run again."
- yfinance fetch fails → refuse to produce stale data per Principle 1: "I don't have fresh market data right now. Try again or check your network."
- Stage 0 / Stage 1 JSON parse fails → log chain_id, surface partial output if any, ask user to retry.

## Quality contract enforcement

- **Factual:** every item's drivers cite an anchor; missing anchors flagged in drill-down.
- **Backbone:** Defender does NOT fire on /brief output — brief is informational, not a recommendation under user pushback. The Defender pattern activates only in /research conversation context where a Recommendation has been issued.
- **Depth:** Stage 2 prompts mandate fundamentals/filings depth; v1 ships with this floor. v1.x evaluator LLM upgrade in Open Follow-ups.
- **Visibility:** `/trace <chain_id>` works on the brief's chain too — surfaces all stage events with anchors per claim.
