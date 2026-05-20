---
name: research
description: Single-ticker on-demand due diligence. Runs the Stage 2 thesis + Stage 3 Skeptic pipeline for one symbol, writes findings into the per-ticker dossier with append-only ledger, surfaces a layered summary with evidence anchors inline. Usage `/research <TICKER>` or just say "look at NVDA for me" in CC.
---

When the user requests research on a ticker (slash command `/research <TICKER>` or natural-language equivalents like "look at TSLA", "what's the case for AAPL", "dig into NVDA"):

## Invocation

Run the CLI via Bash:

```bash
python -m research_assistant research <TICKER>
```

The CLI handles: yfinance data loading, world-state assembly (or reuse from today's cached brief), Stage 2 thesis (Sonnet), Stage 3 Skeptic (Sonnet), dossier write with append-only ledger validation, evidence-anchor citations, and rendered output.

Output is human-readable markdown by default; pass `--json` for machine-readable output if you need to chain further commands.

## Display the output

The CLI's stdout IS the research response. Show it directly to the user — it already includes:
- Thesis text + conviction (pre and post-Skeptic)
- Key drivers with inline `[anchor: tool_call_X]` citations
- Named risks
- Skeptic critique
- Flagged additional risks
- Open questions to probe
- News reactivity flag
- Session cost so far
- Dossier path + chain ID

If any driver renders as `[NO ANCHOR — visibility regression]`, surface that prominently — the user needs to see immediately when the assistant has made an unanchored claim.

## On user pushback in the same conversation

After a research result has been issued in the current conversation, monitor the next user message. If it matches the Defender heuristic (disagreement + no evidence marker), invoke the Defender subagent directly via the Task tool — do NOT capitulate in your conversational layer.

```python
# Pseudocode for the Defender invocation
# (replace with actual Task call)
Task(
    subagent_type="defender",
    description="Defender review on user pushback",
    prompt=json.dumps({
        "recommendation": <prior research result summary>,
        "evidence_anchors": <result.evidence_anchors>,
        "user_pushback": <user's message>,
    }),
)
```

The Defender returns HOLD or REVISE with reasoning. Surface its decision directly. If REVISE, append the revision to the dossier ledger with the new anchor that justified the change.

To programmatically check whether to fire the Defender, call:
```bash
python -m research_assistant defender-check \
    --user-message "<user msg>" \
    --chain-id <prior_result.chain_id> \
    [--ticker <TICKER>]
```

Prints `true` (fire Defender) or `false` (orchestrator handles normally). The
CLI loads `evidence_anchors` from the prior chain's Stage-2 trace events and
verifies the user's strong citation tokens (dates, quarters, %, $, bps) against
that corpus with word-boundary matching — uncorroborated citations (e.g., "per
the 10-K page 47" when no 10-K was actually fetched) no longer suppress the
Defender (closes the typed-anchor-corpus subset of Open Follow-up #2; full
cited-document grep is blocked on #9 EDGAR/FRED/transcripts).

**Pass `--ticker` when the prior result was a `/brief` chain** — the brief
shares one chain_id across all survivors, so the filter scopes verification
to the survivor the user is actually pushing back on. For `/research` chains
(single survivor), omit `--ticker`.

Agent loop:
1. Call `defender-check` with chain_id (and `--ticker` when applicable).
2. If `true` → invoke `Task(subagent_type="defender", ...)` with the prior
   recommendation, evidence_anchors, and user pushback.
3. If `false` → respond to the user directly; orchestrator handles normally.

## Failure modes

- yfinance fetch failure / insufficient data → CLI exits 1 with stderr explaining. Surface that to the user as "I don't have fresh data on `<symbol>` right now."
- Missing `ANTHROPIC_API_KEY` → CLI exits 3 with stderr explaining. Surface that to the user.
- Stage 2 or Stage 3 JSON parse fails → CLI exits 1; chain_id is logged. Suggest retry.
- Very low conviction (`adjusted_score < 0.3`) → surface plainly: "Low conviction — Skeptic flagged X. Case is weak."

## Quality contract enforcement points

- **Factual:** every claim has an anchor in `result.evidence_anchors`; orphans are flagged in render.
- **Backbone:** Defender subagent invoked via the heuristic on pushback — do NOT fold in your conversational layer.
- **Depth:** Stage 2 prompt mandates fundamentals/filings depth. If output reads as headline-summary level, flag for v1.x evaluator review.
- **Visibility:** full cascade trace captured at `.research/traces/<date>/<chain_id>.jsonl`. The `/trace` skill renders it.
