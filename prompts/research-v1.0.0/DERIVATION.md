# Prompt Fork Derivation — research-v1.0.0

Each prompt in this directory is derived from Ozymandias v4.1.0 with documented
research-mode deltas. The derivation pointer in each file's header cites the
exact source path and git SHA in the Ozy repo. The hygiene test
(`tests/test_prompt_fork_hygiene.py`) asserts:

1. Every prompt has a `DERIVED_FROM:` line with a parseable source + SHA.
2. Every cited SHA is reachable via `git rev-parse` in the Ozy repo (i.e.,
   not a stale or invented hash).

This prevents copy-paste prompt drift: if a research-mode prompt ever cites
a SHA that has been garbage-collected, CI fails.

## Source

All derivations point at:
- **Ozy repo:** `/home/typhlupgrade/.local/share/ozy-bot-v3`
- **Ozy SHA:** `df4f8a00beb4ec577a1cff82f168893f4037d917` (HEAD at Phase 3 fork time)
- **Ozy version directory:** `ozymandias/config/prompts/v4.1.0/`

## Files

| Research prompt | Source | Stage | Model tier | Key deltas |
|---|---|---|---|---|
| `world_state.txt` | `v4.1.0/world_state.txt` | 0 | sonnet | Reframing only (research vs trading); schema unchanged |
| `stage_1_filter.txt` | `v4.1.0/stage_1_filter.txt` | 1 | haiku | Candidate source clarified as `.research/watchlist.txt`; earnings flag note-only (not trade-block) |
| `stage_2_thesis.txt` | `v4.1.0/stage_2_thesis.txt` | 2 | sonnet | **Removed** `suggested_entry`/`stop`/`target`. **Added** `open_questions` + `evidence_anchors` (per-claim citation contract). |
| `stage_3_skeptic.txt` | `v4.1.0/stage_3_skeptic.txt` | 3 | sonnet (default; Opus opt-in) | **Removed** stop-side defensibility check (n/a — no stops). **Added** `open_questions_added`. Kept momentum-band, news reactivity, earnings proximity. |

## Stages skipped

Per spec module-classification, Ozy's Stage 4 (PortfolioFit) and Stage 5 (composite ranker)
are NOT forked. Research-mode has no portfolio correlation math and presents options
unranked-by-sizing (user makes discretionary calls).

## Update protocol

If Ozy ships a new v4.x.x prompt set and we want to inherit changes:

1. Read the new Ozy prompt source.
2. Identify what changed semantically.
3. If the change is research-mode-relevant (e.g. better instruction phrasing,
   new heuristic): bump research version (e.g. `research-v1.1.0/`), copy with
   new derivation header citing the new Ozy SHA.
4. Update this DERIVATION.md table.
5. Run `tests/test_prompt_fork_hygiene.py` — must pass.

If the change is Ozy-trading-specific (e.g. new position_size_pct field): do
NOT inherit. The deltas table documents why our prompts diverged.
