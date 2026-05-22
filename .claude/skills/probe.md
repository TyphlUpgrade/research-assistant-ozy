---
name: probe
description: Focused dossier-scoped question. Runs a single Sonnet call against an existing per-ticker dossier (State + Open Questions + Ledger) plus fresh yfinance data, answers one question, appends a `kind="probe"` ledger entry citing the chain_id, and updates the dossier's Open Questions list. Cheaper than `/research` — no Stage 3 Skeptic unless `--deep`. Usage `/probe <TICKER> "<question>"` or natural-language equivalents like "what's the latest on IONQ's federal funding?" against an already-analyzed ticker.
---

When the user asks a focused follow-up question about a ticker that already has a dossier — `/probe <TICKER> "<question>"`, or natural-language equivalents like "follow up on IONQ", "what's the latest on NVDA's earnings", "did the catalyst hold for BB" — and the dossier file exists at `.research/tickers/<TICKER>.md`:

## Invocation

Run the CLI via Bash:

```bash
python -m research_assistant probe <TICKER> "<QUESTION>"
```

Optional flags:
- `--deep` — also run Stage 3 Skeptic over the probe answer (roughly 2× cost)
- `--json` — emit JSON instead of human-readable text

## When to use `/probe` vs `/research` vs in-session follow-up

| Context | Surface |
|---|---|
| First analysis of a ticker — no dossier exists yet | `/research <TICKER>` |
| Follow-up question, **fresh session**, dossier already exists | **`/probe <TICKER> "<Q>"`** |
| Follow-up question, same session as the original `/research` | answer conversationally (in-session) |
| Want adversarial pressure on a thesis | `/research` (Skeptic) or `/probe --deep` |

`/probe` is the **cold-start** entry point against a saved dossier. It reads the dossier's State + Open Questions + Ledger as context and answers one question without rewriting the thesis. The Open Questions list is updated: questions the probe resolves are removed; new gaps the probe surfaces are appended.

## Display the output

The CLI's stdout IS the probe response. Show it directly — it includes:
- Question echoed back + answer (2-6 sentences)
- Evidence anchors per claim with `[anchor: tool_call_X]` citations inline
- Closed open questions (removed from dossier)
- New open questions (appended to dossier)
- Skeptic critique (only with `--deep`)
- Cost + dossier path + chain ID

## Failure modes

- No dossier for the ticker → CLI exits 1, message says to run `/research` first.
- yfinance fetch fails → exits 1; surface the error.
- Missing `ANTHROPIC_API_KEY` → exits 3.
- Probe JSON parse fail → exits 1; chain_id logged.

## Quality contract

The probe is bound by the same factual / backbone / depth / visibility axes as `/research`. Notably:
- **Factual:** every claim in `answer` cites an anchor; orphans render as `[NO ANCHOR — visibility regression]`.
- **Stop rule:** if the question can't be answered from `TICKER_DATA + RECENT_HEADLINES + dossier_context`, the prompt requires saying so and adding the missing data as a new open question. Do NOT invent.
- **Question-resolution rule:** `closes_questions` must contain only verbatim Open Questions the probe **materially** resolves — partial answers leave the question open.

The dossier update is atomic: a probe writes one ledger entry (`kind="probe"`, citing the chain_id), one observation row (`kind="probe"` in `.research/tickers/<T>/observations.jsonl`), and the updated Open Questions list, all under the same `write_dossier_atomic` call.
