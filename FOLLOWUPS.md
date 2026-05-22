# Open Follow-ups

Canonical list of deferred work, ordered by sensible development sequence.
Reconstructed from in-code references (`grep "Follow-up #" -r .`); the
original v1 plan doc was never committed.

Status legend: `OPEN` · `PARTIAL` · `CLOSED`

Cross-checked against `.omc/specs/deep-interview-research-assistant.md`
on 2026-05-22.

---

## 1. Per-ticker observations stream — write phase

Status: **OPEN** (new)

Append-only `.research/tickers/<T>/observations.jsonl`, one event per line,
written by both `/brief` and `/research` whenever Stage 2 produces a thesis.
Today the per-ticker dossier (`tickers/<T>.md`) is the only ticker-keyed
artifact and only `/research` writes it, so brief surfacings leave no
per-ticker trail and history must be reassembled by scanning daily brief
JSONs.

Event shape: `{ts, kind, chain_id, conviction, regime, thesis, drivers,
risks, anchors}`. `kind ∈ {"brief","research","probe"}`. Existing dossier
markdown keeps working unchanged — it becomes a derived view later (#7).

Foundational: consumers (#5 read-phase, #7 derived views, #9 evaluator)
all depend on this existing first. #2 `/probe` also benefits from
writing through the same append path. Cheap and reversible; ship
write-only and observe the stream for ~1-2 weeks before turning on
reads.

## 2. `/probe <question>` skill — targeted dossier-scoped query

Status: **OPEN** — **v1 acceptance-criteria gap.** Spec §37, §211, §222
treat `Probe` as a first-class ontology entity and promise "open
questions closed only when a probe resolves them," but today the only
way to close an Open Question is a full Stage 2 + Stage 3 re-run.

Standalone slash command for targeted follow-up questions against an
existing per-ticker dossier without re-running the full cascade. Reads
the dossier State + Open Questions + Ledger as context, runs a single
Sonnet call scoped to the question, appends a Probe entry to the ledger
with the evidence anchor that drove the answer. Distinct from
conversational follow-ups inside `/research`: those continue the active
session; `/probe` is the cold-start entry point against a saved dossier
from a new session. Also surfaces as a tool in the Discord v2 surface
(#11).

Spec semantics: `Probe { tool, query, evidence_payload, timestamp }`
(spec §222). v1's `/research` writes ledger entries of kind `Research`;
`/probe` writes entries of kind `Probe`. Belongs at
`.claude/skills/probe.md` mirroring `research.md`'s structure;
orchestrator entry point is a thin wrapper over Stage 2 with a
dossier-context-only prompt (no Stage 3 Skeptic unless `--deep`).

Front-loaded because dossiers are accumulating Open Questions today
(NVDA 20+, IONQ +7 from one `/research`) with no in-system close path.
Cheap to ship once #1 (observations stream) lands so that `/probe`
writes flow through the same append path.

## 3. Watchlist-vs-universe persistence gate

Status: **OPEN** (new, optional knob on top of #1)

Policy at brief-write time: persist observations for *all* surfaced
tickers, or only for pinned watchlist names. Discovered-universe tail
can balloon ticker directories with micro-caps that may never reappear.
A single config flag in `.research/watchlist.txt` header or env var
(e.g. `OBSERVATIONS_SCOPE=watchlist|all`).

Trivial to add; ship together with #1 or immediately after.

## 4. `/watch` skill — watchlist management

Status: **OPEN** — closes spec §247 TBD #3 (manual JSON / CLI command /
file import / broker API options listed; CLI command is the obvious
v1.x cut). Spec §211 also asserts "User issues Probes via slash
commands" — analogous DX expectation for watchlist.

Today `.research/watchlist.txt` is hand-edited. Promote to a `/watch`
slash command with subcommands:
- `/watch list` — print current watchlist + Stage 0 discovered universe
- `/watch add <TICKER>` — append a pinned watchlist entry
- `/watch remove <TICKER>` — remove a pinned entry
- `/watch import <FILE>` — append from a file (one ticker per line)

Closes a real DX papercut. Pairs naturally with #3 — the persistence
gate consults the watchlist, so the watchlist needs a clean management
surface.

## 5. Per-ticker observations stream — read phase

Status: **OPEN** (new, depends on #1)

Stage 2 prompts in `brief.py` and `orchestrator.py` accept a
`prior_observations` field; orchestrator tails the last N events from
`tickers/<T>/observations.jsonl` and injects them. Lets the thesis
writer condition on prior conviction, prior drivers, and regime changes
— the actual reasoning compounding unlock.

Caveat: brief output stops being a pure function of the day's market
data once this is on. Cached re-runs of the same `chain_id` still
reproduce, but day-N briefs reference day-(N-1) observations. Worth one
to two weeks of write-only data first to validate stream quality.

## 6. Bare-citation suppression floor (closes v1 #2)

Status: **PARTIAL** — Defender closes the typed-anchor-corpus subset
(`research_assistant/orchestrator.py:357`,
`tests/test_defender_heuristic.py`,
`tests/test_quality_contract.py:199`).

Remaining: bare-citation suppression floor in the quality-contract
enforcement layer — today the floor is a known-weak heuristic per the
existing test marker. Independent of the observations work; pick up
when quality-contract regressions warrant it.

## 7. Derived views from the observations stream

Status: **OPEN** (new, depends on #1)

Once the stream exists:
- `tickers/<T>.md` regenerated from the stream instead of overwritten
  in place — `state_md` becomes the latest-snapshot view, `## Ledger`
  becomes a render of the JSONL tail.
- `tickers/<T>/timeline.md` — chronological human-readable rollup per
  ticker (one row per observation: date, kind, conviction, one-line
  thesis, regime).
- `tickers/_index.json` — rollup catalog: `first_seen, last_seen,
  brief_appearances, last_conviction, has_research_dossier`.

Cosmetic / operator-accessibility layer. Defer until the stream has
real data in it.

## 8. Document-sourced citation verification (deepens #6)

Status: **OPEN** (blocked on document-source integrations)

#6's typed-anchor-corpus check resolves cited tokens against the prior
Stage 2 anchor strings. The full backbone wave still wants literal
document grep — a citation like *"per the 10-K page 47"* should
resolve against the actual filing text, not against whatever the Stage
2 prompt happened to surface. Today such tokens are conservatively
treated as unverified; loses signal once the documents are local.

Pulls in EDGAR / FRED / earnings-transcript adapters (spec §247 TBD
#4). Tracked as the data dependency of this item rather than a separate
"integrate EDGAR" follow-up — the integrations only earn their keep
once a downstream consumer (this item + #9 as an evaluator input) needs
them.

Sequenced before #9 because #9's evaluator LLM benefits substantially
from being able to grep filings as part of the depth-axis check; the
adapters built here become its primary input.

Surfaced incidents:
- 2026-05-19 RIG / NVDA brief session, where Stage 3 Skeptic flagged
  missing real-time macro/news (spot WTI sensitivity, options-implied
  move) the system structurally cannot fetch beyond yfinance headlines.
- 2026-05-22 IONQ `/research` session, where the news-cycle pull
  identified a federal-quantum-policy catalyst but the absence of an
  EDGAR adapter blocked checking IONQ 8-K filings and Form 4 insider
  transactions during a +117% / 30d move. Same data-source gap; same
  wave.

## 9. Evaluator LLM for quality-contract depth (closes v1 #3)

Status: **OPEN** — referenced in `.claude/skills/brief.md:60`,
`tests/test_quality_contract.py:13`,
`tests/test_quality_contract.py:236`.

Replace heuristic quality gates ("fundamentals/filings depth" check,
etc.) with a small evaluator LLM call that scores Stage 2 output
against the quality contract and returns structured pass/fail per
dimension. Largest scope of the open list; wants a stable foundation
underneath, and benefits from being able to read prior observations
(#1, #5) and grep filings (#8) as evaluation inputs. Highest cost item
— ship last among the build queue.

## 10. Cascade stages routed through CC Task tool

Status: **OPEN** — candidate for `research_assistant/claude_sdk.py`
retirement. **Friction-triggered, not sequence-blocking.**

Stages 0–3 call the Anthropic Messages API directly via `claude_sdk.py`;
Defender already goes through `Task(subagent_type="defender", ...)`.
On the unlimited CC plan the direct-API levers (per-stage model
selection, per-call cost telemetry, semaphore-bounded
`asyncio.gather`) are largely noise, and the dual-billing surface (CC
sub + `ANTHROPIC_API_KEY`) is friction.

Migration: per-stage agent files at
`.claude/agents/stage_0_world_state.md`, `stage_1_filter.md`,
`stage_2_thesis.md`, `stage_3_skeptic.md` with `model:` in frontmatter;
rewrite each `ClaudeClient.call(...)` site as a `Task(...)`
invocation. Eliminates the API-key requirement, deletes
`claude_sdk.py`, removes the `CostTracker` surface. Preserves all four
quality-contract axes, prompt-fork lineage, dossier I/O, Defender
isolation.

Parallel Stage 2 invocation either (a) serializes via sequential
`Task` calls (acceptable cost on unlimited plan) or (b) uses CC's
tool-use parallelism. ~1 day of work.

Trigger: dual-billing or API-key-rotation friction in practice.

## 11. Discord channel surface (v2)

Status: **OPEN** — substrate is surface-agnostic by design. Spec §114
explicitly defers Discord beyond v1.

Out-of-CC surface for the same `research_assistant/` package. A
standalone Python process (~200 LOC) listens to a Discord channel,
imports `research_assistant` directly, runs a single Sonnet
conversational orchestrator with tool-use enabled — tools are
`research(ticker)`, `brief()`, `probe(question)`,
`get_dossier(ticker)`. No separate intent classifier; the LLM routes
via standard tool use. May lift plumbing from Ozy's v5 conversational
operator
(`.omc/plans/2026-04-26-discord-conversational-output.md`,
`.omc/wiki/v5-conversational-discord-operator.md`); orchestrator loop
is research-specific.

Two items promote from optional to load-bearing on this surface:
- **Defender heuristic graduation** — every chat message is potential
  pressure, so the 3-condition AND fires continuously. #6
  (bare-citation suppression) and #8 (document-citation verification)
  become required-before-launch.
- **Cost ceiling** — graduate `cost.hard_ceiling_usd` from default-OFF
  to default-ON with a per-session bound; intent classification +
  Defender both fire more often in continuous chat.

Discord is additive. CC-terminal `.claude/skills/` keeps working as the
primary surface.

---

## Tracked TBDs (process / validation, not build queue)

These belong to the spec's "Open Items" section (§243-249) but are not
code work — they're decisions or validations against real usage. Listed
here so they don't fall out of memory.

- **4-axis self-attestation cadence** (spec §247 TBD #1). Suggested
  rolling self-check every 2 weeks for first 3 months across
  informed / profit / time / stress. Calendar item, not code.
- **Per-session cost ceiling** (spec §247 TBD #2). Start uncapped;
  revisit if single session > $5 or week > $30. Threshold to surface
  as a `cost.hard_ceiling_usd` graduation in #11.
- **Defender model-cost validation** (spec §249 TBD #5). Default Opus
  today; validate cost-vs-quality after some real usage. Decision
  feeds the model frontmatter on `.claude/agents/defender.md`.

---

## Closed

- **v1 #1 — dynamic universe discovery.** Closed by `universe_fetcher`
  graduating to Ozy in v1.x (see `tests/test_import_boundaries.py:45`).
