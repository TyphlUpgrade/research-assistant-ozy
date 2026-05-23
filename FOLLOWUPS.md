# Open Follow-ups

Canonical list of deferred work, ordered by sensible development sequence.
Reconstructed from in-code references (`grep "Follow-up #" -r .`); the
original v1 plan doc was never committed.

Status legend: `OPEN` · `PARTIAL` · `CLOSED`

Cross-checked against `.omc/specs/deep-interview-research-assistant.md`
on 2026-05-22.

## Ship-order revision (2026-05-22)

Today's session ran three `/research` cycles on IONQ plus one `/probe`.
All four terminated at the same data gap: yfinance can't surface 10-K
fundamentals, 8-K announcement text, or Form 4 insider transactions,
and the Skeptic's strongest pushback (ATM dilution risk during a +121%
/ 30d rally) is structurally uncheckable. NVDA dossier hit the same
wall on 2026-05-18.

The original order optimized for system-internal coherence (foundational
stream → enrichment → enforcement → views → corpus → evaluator).
Operator decision-blocking wasn't weighted highly enough. Items below
have been reordered: EDGAR client + Form 4 promoted ahead of
observations-stream read-phase, watchlist gating, and derived views,
because those items enrich capabilities the system *already has* while
EDGAR adds capabilities that close the depth-axis regression spec §247
TBD #4 was written for.

## Cross-cutting constraint: data-source gating

Binding on every data-source sub-item below (#1, #3, #4, #5, #11, #12,
#13). Brief Stage 2 stays unmodified — no per-ticker enrichment lands
there. The brief is for scanning, and 8× parallel Stage 2 calls
compound input noise into ranking error.

Each new data source must declare exactly one **gate**:

- **Stage 0 (regime)** — shared across all brief items; ≤3 lines added
  to world-state. For regime/macro signal only.
- **Stage 1 (filter)** — rank-decisional only, ≤1 line per candidate
  for the batched Haiku filter. Adjusts survivor selection without
  polluting Stage 2 narrative.
- **`/research` Stage 2 only** — per-ticker enrichment block; lands
  here because the operator committed to deep DD on this name.
- **`/probe` only** — fetch on demand; never pre-loaded into any
  cascade prompt.

Sources that don't fit one of these gates don't ship. This makes
context-budget a contract, not a per-source vigilance task.

---

## 1. EDGAR client foundation + full-text filings (10-K / 10-Q / 8-K)

Status: **PARTIAL** — adapter foundation shipped 2026-05-22 in
`research_assistant/edgar.py` + `tests/test_edgar.py` (24 tests).

Shipped:
- `EdgarClient` async HTTP client, 5 req/sec sliding-window throttle,
  SEC-required User-Agent (default `research-assistant
  william.a.sit@gmail.com`, env override `EDGAR_USER_AGENT`).
- `resolve_cik(ticker)` via `company_tickers.json` (lazy single-fetch
  cache; case-insensitive).
- `list_filings(cik, form_type, since=, limit=)` via
  `data.sec.gov/submissions/CIK{cik}.json` — works for any form code
  (10-K / 10-Q / 8-K today; 4 / 13F-HR consumed by #3 and #5).
- `fetch_filing(filing)` returns `FilingText` with HTML→paragraph
  extraction (script/style stripped, whitespace collapsed,
  parent/child div+p dedupe).
- Stable anchor format `edgar:{form}:{accession}:para_{n}` matching the
  v1 spec; `FilingText.search(needle)` returns (anchor, paragraph)
  hits for Defender (#2) anchor-corpus injection.
- CLI smoke: `python -m research_assistant.edgar <TICKER> <FORM>`.

Remaining for full closure:
- `/probe` wiring — when a probe question references filings, fetch
  via `EdgarClient` and inject into the probe prompt's
  `dossier_context` slot (out of scope of this commit per scope
  decision; sequenced with #2).
- Defender anchor-corpus injection — pushback citations like *"per
  the 10-K page 47"* should resolve against `FilingText.search`
  hits. Lands with #2 (bare-citation suppression floor).

Gate (when fully wired): **`/probe` only** for full filing text.
Stage 2 never sees raw 10-K text.

Surfaced incidents:
- 2026-05-19 RIG / NVDA brief session — Stage 3 Skeptic flagged
  missing real-time macro/news (spot WTI sensitivity, options-implied
  move) the system structurally cannot fetch beyond yfinance.
- 2026-05-22 IONQ × 3 cycles — federal-quantum-policy catalyst
  identified by name but absence of an EDGAR adapter blocked checking
  IONQ 8-K filings; the Skeptic's new ATM-dilution thesis is
  uncheckable without Form 4.

## 2. Bare-citation suppression floor (closes v1 #2)

Status: **PARTIAL** — Defender closes the typed-anchor-corpus subset
(`research_assistant/orchestrator.py:357`,
`tests/test_defender_heuristic.py`,
`tests/test_quality_contract.py:199`).

Remaining: bare-citation suppression floor in the quality-contract
enforcement layer — today the floor is a known-weak heuristic per the
existing test marker.

Sequenced between #1 (EDGAR client) and #3 (Form 4) because once new
data sources start producing fresh anchor strings (e.g.
`edgar:8-K:0001234567-26-000045:para_17`), the typed-anchor-corpus
verification path must be hardened before Defender encounters
citations it has no way to validate.

## 3. EDGAR Form 4 insider transactions

Status: **PARTIAL** — parser + aggregation shipped 2026-05-22 in
`research_assistant/edgar.py` (extends #1's `EdgarClient`) +
`tests/test_edgar.py` (19 new tests).

Shipped:
- `parse_form4(xml)` over stdlib ElementTree — handles SEC's
  `<value>`-wrapper convention, HTML-entity decoding, multiple
  reporting owners (joint filings), empty/relationship-only filings.
  Splits non-derivative (common stock) and derivative (options /
  RSUs) tables.
- `Form4Transaction.net_dollars` — signed by acquired/disposed code;
  $0-price entries (grants, exercises) contribute $0 (no yfinance
  backfill per scope decision).
- `EdgarClient.fetch_form4(filing)` — strict `form_type=="4"` check.
- `aggregate_insider_activity(filings, window_days=90, as_of=)` →
  `InsiderActivitySummary` with per-officer rollup (sorted by
  |net $|), separated `code_mix` vs `deriv_code_mix`, window
  filtering on `period_of_report`.
- `stage_1_line()` — one-liner matching the spec format
  ("insider net flow last 90d: -$42.0M / 4 sales / 0 buys").
- `stage_2_block()` — 3-line enrichment block: counts + net $ +
  latest tx date / code mix / top-3 officers by absolute $ impact.

Wiring progress:
- **`/research` Stage 2** — SHIPPED 2026-05-22.
  `load_insider_activity(symbol)` in `edgar.py` fetches + aggregates
  in parallel with `load_ticker_data` / `load_headlines`.
  `orchestrator.research_ticker` accepts an optional
  `insider_activity` kwarg; `_stage_2_thesis` injects
  `stage_2_block()` into the new `{insider_activity_block}` slot in
  `stage_2_thesis.txt`. Graceful degrade: EDGAR failure / unknown
  CIK → None → "(insider activity unavailable …)" placeholder;
  empty window → "(no Form 4 filings last 90d)". Source rule list
  in the prompt extended with `edgar:form4:aggregate`.
- **`/probe`** — SHIPPED 2026-05-22. Same wiring pattern as Stage 2.
  `probe_ticker` and `_stage_2_probe` accept the kwarg; `probe.txt`
  gains the `{insider_activity_block}` slot. CLI `_cmd_probe` now
  fetches yfinance + EDGAR in one `asyncio.gather`. Operator can now
  ask *"are insiders selling?"* on any dossier and get a structured
  answer.
- **Stage 1 filter (brief)** — OPEN. `brief.py` Stage 1 batched-
  Haiku prompt should consume `stage_1_line()` per candidate. Cost
  caveat: ~30 universe tickers × ~6 HTTP/s cap = ~5 min worst-case
  per brief without parallelism work.

The remaining Stage 1 integration layers on top of
`load_insider_activity` without adapter changes.

Gate (when fully wired): **Stage 1 (filter)** + **`/research`
Stage 2 only** + **`/probe`**.

## 4. Polymarket odds

Status: **OPEN** — third priority. Cheapest integration in the
candidate set and the only candidate that makes Stage 0 quantitative.

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

Anchors are stable
(`polymarket:market:0x_abc:price_yes=0.42:ts=…`).

## 5. EDGAR 13F institutional filings

Status: **OPEN** — paired with #3 (Form 4) for full ownership signature.

Gate: **`/research` Stage 2 only** + **`/probe`**.

Pairs with #3: insiders + institutions = full ownership picture. 45-
day lag makes it weaker than #3 standalone (best for fundamental
theses, not catalyst-driven trades).

Adapter-side compression to ≤1 line per ticker ("5 new positions
>100K shares last quarter, 2 exited, net concentration index 0.42").
Per-stock aggregation requires flipping the per-fund 13F orientation
— non-trivial; pre-aggregated free sources (13F.info) may be the
cheapest path.

## 6. Watchlist-vs-universe persistence gate

Status: **OPEN** (knob on top of the now-shipped observations stream).

Policy at brief-write time: persist observations for *all* surfaced
tickers, or only for pinned watchlist names. Discovered-universe tail
can balloon ticker directories with micro-caps that may never reappear.
A single config flag in `.research/watchlist.txt` header or env var
(e.g. `OBSERVATIONS_SCOPE=watchlist|all`).

Trivial to add.

## 7. `/watch` skill — watchlist management

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

Pairs naturally with #6 — the persistence gate consults the watchlist,
so the watchlist needs a clean management surface.

## 8. Per-ticker observations stream — read phase

Status: **OPEN** (depends on the now-shipped write phase having
accumulated ≥1-2 weeks of data).

Stage 2 prompts in `brief.py` and `orchestrator.py` accept a
`prior_observations` field; orchestrator tails the last N events from
`tickers/<T>/observations.jsonl` and injects them. Lets the thesis
writer condition on prior conviction, prior drivers, and regime changes
— the reasoning compounding unlock.

Caveat: brief output stops being a pure function of the day's market
data once this is on. Cached re-runs of the same `chain_id` still
reproduce, but day-N briefs reference day-(N-1) observations.
Naturally sequenced behind #1–#5 (the EDGAR/Polymarket work runs in
parallel with the 1-2-week write-only maturation period the read phase
needs).

## 9. Derived views from the observations stream

Status: **OPEN** (depends on the observations stream having real data).

Once the stream has matured:
- `tickers/<T>.md` regenerated from the stream instead of overwritten
  in place — `state_md` becomes the latest-snapshot view, `## Ledger`
  becomes a render of the JSONL tail.
- `tickers/<T>/timeline.md` — chronological human-readable rollup per
  ticker (one row per observation: date, kind, conviction, one-line
  thesis, regime).
- `tickers/_index.json` — rollup catalog: `first_seen, last_seen,
  brief_appearances, last_conviction, has_research_dossier`.

Cosmetic / operator-accessibility layer. Defer until #8 is on.

## 10. Evaluator LLM for quality-contract depth (closes v1 #3)

Status: **OPEN** — referenced in `.claude/skills/brief.md:60`,
`tests/test_quality_contract.py:13`,
`tests/test_quality_contract.py:236`.

Replace heuristic quality gates ("fundamentals/filings depth" check,
etc.) with a small evaluator LLM call that scores Stage 2 output
against the quality contract and returns structured pass/fail per
dimension. Largest scope of the open list; wants a stable foundation
underneath, and benefits from being able to read prior observations
(write-phase shipped; read-phase = #8) and grep filings (#1, #3, #5,
plus #11–#13 as they land) as evaluation inputs. Highest cost item —
ship last among the build queue.

## 11. FRED macro time series

Status: **OPEN** — demand-driven.

Gate: **Stage 0 (regime)**.

Regime/macro signal only — yield curves, employment, inflation
prints. Stage 0's existing world-state assembly gains a small block
of FRED-sourced series. ≤3 lines added.

## 12. Earnings transcripts

Status: **OPEN** — demand-driven.

Gate: **`/probe` only**.

Sparse-signal, valuable-when-present. Probe-fetch on user demand
(e.g. *"what did NVDA management say about data-center backlog last
quarter?"*). Stage 2/3 never see raw transcript text by default.

Paid (Tikr, AlphaSense) or scraped; sourcing decision is per-source.

## 13. Congressional trading disclosures

Status: **OPEN** — demand-driven.

Gate: **`/probe` only**.

Sparse signal, 45-day reporting lag, real risk of optical bias if
surfaced into Stage 2 prompts. Pull on user demand only via a
`/probe congressional <TICKER>` invocation. Never a default cascade
input.

Free aggregators (Senate Stock Watcher, House Stock Watcher) require
some scraping; paid (CapitolTrades, QuiverQuant) have cleaner APIs.

## 14. Cascade stages routed through CC Task tool

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

## 15. Discord channel surface (v2)

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
  pressure, so the 3-condition AND fires continuously. #2
  (bare-citation suppression) and #1 (EDGAR full filings, where
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
  as a `cost.hard_ceiling_usd` graduation in #15.
- **Defender model-cost validation** (spec §249 TBD #5). Default Opus
  today; validate cost-vs-quality after some real usage. Decision
  feeds the model frontmatter on `.claude/agents/defender.md`.

---

## Closed

- **v1 #1 — dynamic universe discovery.** Closed by `universe_fetcher`
  graduating to Ozy in v1.x (see `tests/test_import_boundaries.py:45`).
- **v1.x #1 — per-ticker observations stream — write phase.** Shipped
  2026-05-22 in commits `11ac0d0` + `cdad156`. JSONL-per-ticker append
  stream at `.research/tickers/<T>/observations.jsonl`; written by
  `/brief` (one per surviving Stage 2 item), `/research` (one per
  cascade run), and `/probe`. Schema-versioned, anchor-typed,
  malformed-line tolerant.
- **v1.x #2 — `/probe <question>` skill.** Shipped 2026-05-22 in
  commits `2b0f283` + `cdad156`. Focused dossier-scoped query;
  reads dossier State + Open Questions + Ledger tail as context,
  emits a Probe ledger entry citing the chain_id, drops resolved
  Open Questions, appends new ones, and writes a `kind="probe"`
  observation through the stream.
