---
name: brief
description: On-demand morning market brief. Builds world-state regime classification + ranked opportunity surface (top 4-8 from the user's static watchlist) with one-line thesis per item. Layered output — scannable top-level summary first, drill-down via `/brief <TICKER>` for any item. No Skeptic in brief; user runs `/research <TICKER>` for full bias-defense treatment on selected names.
---

When the user runs `/brief` (no args) or says equivalents like "what's the market doing", "morning brief", "what should I look at today":

## Invocation

Run the CLI via Bash:

```bash
python -m research_assistant brief
```

The CLI does cache-first lookup — if today's brief (ET date) already exists at `.research/briefs/<date-ET>.json`, it returns from cache instantly (no API spend). To force a rebuild:

```bash
python -m research_assistant brief --refresh
```

To drill down into a specific item from the brief:

```bash
python -m research_assistant brief --ticker NVDA
```

JSON output for chaining: add `--json`.

## Display the output

The CLI's stdout is the brief — markdown formatted, ready to show directly. It includes:
- Market regime (bull-trending / bear-trending / choppy / panic / euphoria) + confidence + dispersion
- VIX level + trend, active catalysts (FOMC, CPI, etc.)
- Top 4-8 opportunities with ticker + 1-line thesis + conviction
- Chain ID footer

Drill-down view (when `--ticker` is set) adds:
- Full Stage 2 thesis text
- Key drivers with `[anchor: tool_call_X]` citations inline
- Named risks
- Open questions to probe further
- `[NO ANCHOR — visibility regression]` flags on any unanchored claim

## Failure modes

- Empty watchlist → CLI exits 1, asks user to populate `.research/watchlist.txt`.
- yfinance fetch fails → exits 1; surface the error.
- Missing `ANTHROPIC_API_KEY` → exits 3.
- Stage 0 / Stage 1 JSON parse fail → exits 1; chain_id logged.

## Why no Skeptic in /brief

Stage 3 Skeptic costs Sonnet × 8 calls per morning brief — significant daily spend on idle days. The brief is for SCANNING — the user picks 1-3 names worth probing and runs `/research <TICKER>` for the full Stage 2 + Stage 3 + Defender treatment. Keeps brief cheap (~$0.20-0.50 per generation) and concentrates the expensive Skeptic call on names the user is about to act on.

## Quality contract enforcement

- **Factual:** every item's drivers cite an anchor; orphans flagged in drill-down.
- **Backbone:** Defender does NOT fire on /brief output — brief is informational. Activates only in /research conversation context after a Recommendation.
- **Depth:** Stage 2 prompt mandates fundamentals/filings depth. v1.x evaluator LLM in Open Follow-ups.
- **Visibility:** `/trace <chain_id>` works on the brief's chain — surfaces all stage events with per-claim anchors.
