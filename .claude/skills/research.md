---
name: research
description: Single-ticker on-demand due diligence. Invokes the Stage 2 thesis + Stage 3 Skeptic pipeline for one symbol, writes findings into the per-ticker dossier with append-only ledger, and surfaces a layered summary with evidence anchors inline. Usage `/research <TICKER>` or just say "look at NVDA for me" inside CC.
---

When the user asks for research on a ticker (slash command `/research <TICKER>` or natural-language equivalents like "look at TSLA", "what's the case for AAPL", "dig into NVDA"):

1. **Parse the symbol** from the user's message. Default to uppercase. If ambiguous (e.g. "tesla"), confirm before proceeding.

2. **Load ticker data via yfinance** — bars (daily, 90-day window), latest quote, recent news (max 15-min cache TTL per spec §95). Compute the TA snapshot needed by Stage 3's momentum-band check:
   - `return_30d`, `return_90d` from bars
   - `weekly_rsi_14` from weekly-resampled closes
   - `volume_5d_trend` (rising/flat/declining via 5d-rolling avg slope)
   - `earnings_within_days` if known from yfinance calendar

3. **Build or load WorldState** — if a `.research/briefs/<today-ET>.json` cache exists from today's `/brief`, reuse its world_state. Otherwise build it via the Stage 0 prompt (cheaper than re-running per `/research`).

4. **Invoke `research_assistant.orchestrator.research_ticker(...)`** with the loaded inputs. The orchestrator runs Stage 2 + Stage 3, writes to the dossier, and returns a `ResearchResult`.

5. **Render the response** in the terminal:

   ```
   ## NVDA — Research result (chain: 20260514T143022-abc123)

   **Thesis (Sonnet):**
   <thesis_text>

   **Conviction:** 0.62 → 0.55 (post-Skeptic adjustment)

   **Key drivers:**
   - <driver 1> [anchor: tool_call_xy123]
   - <driver 2> [anchor: tool_call_xy124]

   **Risks (named):**
   - <risk 1>
   - <risk 2>

   **Skeptic critique:**
   <critique_text>

   **Flagged additional risks:**
   - <risk a>
   - <risk b>

   **Open questions to probe:**
   - <question 1>
   - <question 2>

   **News reactivity flag:** false
   **Cost so far this session:** $0.12

   Dossier appended at `.research/tickers/NVDA.md` (ledger entry tagged with chain ID).
   Run `/trace 20260514T143022-abc123` to see the full cascade JSONL.
   ```

6. **Inline anchor citations**: every driver and named claim in the rendered output should cite its evidence anchor from `result.evidence_anchors`. If a claim has no anchor, flag it visually (`[NO ANCHOR — visibility regression]`) and add it to a quality-contract regression report.

7. **On user pushback in the same conversation thread**: check `should_invoke_defender(prior_turn_had_recommendation=True, user_message=<user msg>)`. If true, spawn the Defender subagent via Task tool with the 3-tuple `(recommendation, evidence_anchors, pushback)`. Render the Defender's HOLD/REVISE decision inline.

## Failure modes to surface

- yfinance fetch fails / returns stale data → refuse to produce a thesis. Tell the user "I don't have fresh data on `{symbol}` right now — try again or fix the data path."
- Stage 2 or Stage 3 JSON parse fails → log the chain_id, surface partial dossier write if any, tell the user to retry.
- Conviction is very low (`adjusted_score < 0.3`) → surface clearly: "Low conviction — skeptic flagged X. The case here is weak."

## Quality contract enforcement points

- **Factual:** every claim has an anchor in `result.evidence_anchors`. Anchorless claims fail visibility regression.
- **Backbone:** Defender pattern fires on the heuristic — do NOT capitulate in the conversational layer.
- **Depth:** if Stage 2 output reads as headline-summary level (no filings/transcripts/segment-data references), flag for v1.x evaluator review.
- **Visibility:** the full cascade trace is captured in `.research/traces/<date>/<chain_id>.jsonl`. The `/trace` skill renders it.
