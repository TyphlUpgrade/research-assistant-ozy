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

## 8. Data source expansion (parent — spec §247 TBD #4)

Status: **OPEN** (parent; sub-items have independent ship gates).

Brings new data sources into the cascade. Existing data is `yfinance`
(quotes, bars, news) + Stage 0 world-state aggregation. Sub-items here
extend that surface.

**Context-budget discipline (binding on every sub-item):** brief Stage
2 stays unmodified. No per-ticker enrichment block lands at brief time
— the brief is for scanning, and 8× parallel Stage 2 calls compound
input noise into ranking error. Each sub-item must declare exactly one
**gate**:

- **Stage 0 (regime)** — shared across all items in the brief; ≤3 lines
  added to world-state. For regime/macro signal only.
- **Stage 1 (filter)** — rank-decisional only, ≤1 line per candidate
  for the batched Haiku filter. Adjusts survivor selection without
  polluting Stage 2 narrative.
- **`/research` Stage 2 only** — per-ticker enrichment block; lands
  here because the operator committed to deep DD on this name.
- **`/probe` only** — fetch on demand; never pre-loaded into any
  cascade prompt.

Sub-items that don't fit one of these gates don't ship. This makes
context-budget a contract, not a per-source vigilance task.

Two downstream consumers reuse whatever lands: #6 (bare-citation
suppression — anchors gain literal-document resolution as sub-items
ship) and #9 (evaluator LLM — uses the document corpus as one of its
depth-axis inputs).

Surfaced incidents:
- 2026-05-19 RIG / NVDA brief session — Stage 3 Skeptic flagged
  missing real-time macro/news (spot WTI sensitivity, options-implied
  move) the system structurally cannot fetch beyond yfinance.
- 2026-05-22 IONQ `/research` session — federal-quantum-policy
  catalyst identified, but absence of an EDGAR adapter blocked
  checking IONQ 8-K filings and Form 4 insider transactions during a
  +117% / 30d move. NVDA and IONQ dossiers both explicitly raised
  insider-positioning questions as Open Questions the cascade
  couldn't answer.

Ship order favors **highest depth-axis lift per token added**: 8b
(Form 4) closes existing Open Questions the cascade is *currently
generating*; 8c (Polymarket) is the cheapest integration and the only
candidate that makes Stage 0 quantitative. 13F and the rest follow.

### 8a. EDGAR client foundation + full-text filings (10-K / 10-Q / 8-K)

Gate: **`/probe` only** for full filing text. Citation-anchor
resolution (via #6) gets the raw filing text injected into Defender's
anchor corpus when a pushback cites *"per the 10-K page 47"*. Stage 2
never sees raw 10-K text.

Foundational: builds the rate-limited EDGAR HTTP client + accession-
number resolver + per-form parser that 8b (Form 4) and 8d (13F)
reuse. Worth landing first as infrastructure even though 8b is the
higher-value endpoint.

### 8b. EDGAR Form 4 insider transactions

Gate: **Stage 1 (filter)** + **`/research` Stage 2 only** + **`/probe`**.

Best academic alpha evidence in the candidate set, and the source
that directly closes Open Questions the cascade is already generating
("What is the current insider transaction profile?" — NVDA + IONQ
dossiers, 2026-05-18 to 2026-05-22).

Surfaces:
- **Stage 1 filter:** 1-line per candidate ("insider net flow last
  90d: -$42M / 4 sales / 0 buys"). Decisional, not narrative-affecting.
  Disqualifies names with severe insider selling before they reach
  brief Stage 2.
- **`/research` Stage 2 enrichment:** compressed 3-line summary
  ("3 sales last 90d, net -2.1% of insider holdings, codes 100% S,
  latest 2026-05-19; CFO held flat, CEO sold $18M") in the Stage 2
  prompt for the committed ticker.
- **`/probe`:** full historical insider lookup with per-officer
  breakdown.

Adapter-side compression is non-negotiable; transaction-code parsing
(P / S / A / M) is the failure mode to guard against.

### 8c. Polymarket odds

Gate: **Stage 0 (regime)** + **`/research` Stage 2 only**.

Surfaces:
- **Stage 0:** 2-3 lines of regime-relevant markets shared across all
  brief items ("Fed cuts May 2026: 0.28 | S&P year-end target $X:
  0.42 | CHIPS Act funding passes: 0.61"). Quantifies catalysts that
  today live as narrative strings (e.g. today's brief has
  `Trump_bull_market_narrative` with no probability attached).
- **`/research` Stage 2 enrichment:** any ticker-specific Polymarket
  markets that exist (earnings beats, M&A, regulatory events). 1-line
  each. Most tickers will have none — that's a valid "" enrichment.

Min-volume filter required ($1M+ resting liquidity) to drop noise
markets. Read-only CLOB API access (free) is enough; we don't trade.

Cheapest integration in the candidate set. Anchors are stable
(`polymarket:market:0x_abc:price_yes=0.42:ts=…`).

### 8d. EDGAR 13F institutional filings

Gate: **`/research` Stage 2 only** + **`/probe`**.

Pairs with 8b: insiders + institutions = full ownership signature.
45-day lag makes it weaker than 8b standalone (best for fundamental
theses, not catalyst-driven trades).

Adapter-side compression to ≤1 line per ticker ("5 new positions
>100K shares last quarter, 2 exited, net concentration index 0.42").
Per-stock aggregation requires flipping the per-fund 13F orientation
— non-trivial; pre-aggregated free sources (13F.info) may be the
cheapest path.

### 8e. FRED macro time series

Gate: **Stage 0 (regime)**.

Regime/macro signal only — yield curves, employment, inflation
prints. Stage 0's existing world-state assembly gains a small block
of FRED-sourced series. ≤3 lines added.

### 8f. Earnings transcripts

Gate: **`/probe` only**.

Sparse-signal, valuable-when-present. Probe-fetch on user demand
(e.g. *"what did NVDA management say about data-center backlog last
quarter?"*). Stage 2/3 never see raw transcript text by default.

Paid (Tikr, AlphaSense) or scraped; sourcing decision is per-source.

### 8g. Congressional trading disclosures

Gate: **`/probe` only**.

Sparse signal, 45-day reporting lag, real risk of optical bias if
surfaced into Stage 2 prompts. Pull on user demand only via a `/probe
congressional <TICKER>` invocation. Never a default cascade input.

Free aggregators (Senate Stock Watcher, House Stock Watcher) require
some scraping; paid (CapitolTrades, QuiverQuant) have cleaner APIs.

## 9. Evaluator LLM for quality-contract depth (closes v1 #3)

Status: **OPEN** — referenced in `.claude/skills/brief.md:60`,
`tests/test_quality_contract.py:13`,
`tests/test_quality_contract.py:236`.

Replace heuristic quality gates ("fundamentals/filings depth" check,
etc.) with a small evaluator LLM call that scores Stage 2 output
against the quality contract and returns structured pass/fail per
dimension. Largest scope of the open list; wants a stable foundation
underneath, and benefits from being able to read prior observations
(#1, #5) and grep filings (#8a, plus 8b–8g as they land) as evaluation
inputs. Highest cost item
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
  (bare-citation suppression) and #8a (EDGAR full filings, where
  document-citation verification lives) become required-before-launch.
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
