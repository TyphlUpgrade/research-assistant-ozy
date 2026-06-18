# Open Follow-ups

Canonical list of deferred work, ordered by sensible development sequence.
Reconstructed from in-code references (`grep "Follow-up #" -r .`); the
original v1 plan doc was never committed.

Status legend: `OPEN` · `PARTIAL` · `CLOSED`

Cross-checked against `.omc/specs/deep-interview-research-assistant.md`
on 2026-05-22.

## Pinned priority (2026-06-08)

Operator-pinned during a watchlist-wide /brief + /research pass. The
scoreboard surfaced that only the post-Skeptic ≥0.44 decile predicts
positive forward returns (+19.14% median 10d); everything below is
noise-to-negative, and the brief-inline anchor-only Skeptic is anti-
predictive (AGREE → -8.89%). The blind spots below either silently
degrade verdict quality today or close the data gap that's keeping the
brief Skeptic broken. Polymarket (#4) intentionally excluded from this
pin — operator decision, revisit later.

In ship order:

1. **#24 — 13F silently 404'ing** ✅ SHIPPED 2026-06-08 in commit
   `bf445a6`. BlackRock CIK fix + index.json-based filename discovery.
   Live NVDA query now returns BlackRock $336B, State Street $173B,
   FMR $173B (previously: silent None).
2. **#25 — VIX delisted in yfinance** ✅ SHIPPED 2026-06-08 in commit
   `bf445a6`. `^VIX` → `^VIX9D` swap; Stage 0 now emits numeric VIX
   level + measured trend.
3. **#28 — Research-surface deep substrate (panopticon)** ✅ SPEC
   READY 2026-06-09 at `.omc/specs/research-deep-substrate-v1.md`.
   v3 spec passed architect + 2 critic rounds + 3 empirical
   experiments (M4 + EXP1 + EXP2, $0.74 total). Empirical premise
   validated from three angles: Phase D substrate (filing forensic
   detail) catches losers; Phase A substrate (KPIs + financials)
   catches losers via different mechanism; Phase D doesn't false-
   positive on winners. Phase A and Phase D are complementary, not
   redundant — both ship. Ready for implementation. Empirical gate
   for SHIPPING resolved; #28 supersedes the "depth-of-synthesis
   gap" hypothesis #20 and #26 were rationalizing around.
4. **#26 — Scoreboard 2.0 trajectory-mode** (NEW). Spec drafted
   2026-06-08 at `.omc/specs/scoreboard-trajectory-mode-v1.md`.
   Surfaced when bootstrap + paired-observation analysis revealed
   the #20 empirical gate (decile-10 +19.14%) was N=6 with 3 of 6
   entries being MRVL triple-counted. Trajectory framing credits
   adaptive behaviour (the MRVL ideal: high conviction at the catalyst
   → declining as risk surfaces → exhaustion-low at the post-pop
   drawdown) that point-in-time decile analysis structurally cannot
   see. **Now blocks #20** — re-opens once trajectory PEAK-AND-FADE
   hit rate > 50% AND median P&L > 0 across N≥10 classified
   trajectories.
4. **#20 + #18 — Skeptic hybrid investigation** (BLOCKED on #26).
   Spec drafted 2026-06-08 at
   `.omc/specs/skeptic-hybrid-investigation-v1.md`; architect + critic
   review surfaced (a) load-bearing "reuse Stage 2 tool-use pattern"
   factual error (no such pattern exists in repo), (b) decile-10
   empirical gate is duplicate-data noise. Spec now BLOCKED on #26
   delivering a trajectory-aware empirical gate. Volume composition
   tool stays as a #20 optional when #20 re-opens.
5. **#11 — FRED macro time series**. Non-tech sector context that
   today's brief is missing (yield curves, employment, inflation).
   Cheapest open Stage 0 enrichment now that the silent-bug fixes
   shipped.

---

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

Status: **CLOSED** — adapter + `/probe --filing` + Defender corpus
integration all shipped (2026-05-22 / 2026-05-23).

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

Closure pieces (2026-05-23):
- `research_assistant/edgar/excerpts.py` — `FilingExcerpts` dataclass
  + `extract_keywords` (drops stopwords + short tokens) +
  `load_filing_excerpts(ticker, form, question, *, max_paragraphs=10)`.
  Greps the latest filing of `form` for paragraphs matching ANY
  content keyword from the question; returns anchored excerpts.
- CLI `/probe --filing <FORM>` flag opts the operator into the
  fetch; `probe_ticker` / `_stage_2_probe` accept the new
  `filing_excerpts` kwarg and render via
  `_format_filing_excerpts_block` into the new
  `{filing_excerpts_block}` slot in `probe.txt`. Source-rule list
  extended with `edgar:<form>:<accession>:para_<N>`.
- Orchestrator post-processes Stage 2 output: for any
  `evidence_anchor` whose `source` matches the paragraph anchor
  regex, splice the corresponding paragraph text into the dict as
  `para_text`. Enriched anchors persist to the trace JSONL.
- Defender's existing `_flatten_anchors_to_corpus` picks up
  `para_text` automatically — zero Defender code changes. End-to-end
  test covers the spec's *"per the 10-K page 47"* path: pushback
  citing a number that IS in para_text does NOT fire Defender;
  invented numbers still do.

Gate: **`/probe` only** for full filing text. Stage 2 never sees raw
10-K text (the cascade Stage 2 prompt has no `filing_excerpts_block`
slot — only `/probe` does).

Surfaced incidents:
- 2026-05-19 RIG / NVDA brief session — Stage 3 Skeptic flagged
  missing real-time macro/news (spot WTI sensitivity, options-implied
  move) the system structurally cannot fetch beyond yfinance.
- 2026-05-22 IONQ × 3 cycles — federal-quantum-policy catalyst
  identified by name but absence of an EDGAR adapter blocked checking
  IONQ 8-K filings; the Skeptic's new ATM-dilution thesis is
  uncheckable without Form 4.

## 2. Bare-citation suppression floor (closes v1 #2)

Status: **CLOSED** — shipped 2026-05-23.

Shipped:
- `research_assistant/quality_contract.py` — new module exporting
  `passes_depth_floor(text, window=80)`. Tightened from the v1 regex
  by requiring a substance signal (%, $, ISO date, Q[1-4], FY, 3+
  digit number) within 80 chars of any depth term (10-K, 10-Q, 8-K,
  transcript, segment, etc.). Closes the *"See the 10-K for risk
  factors."* known-weakness gap.
- `tests/test_quality_contract.py` — consumes
  `passes_depth_floor` from source; the prior
  `test_depth_floor_known_weakness_acknowledged` test is replaced by
  `test_depth_floor_suppresses_bare_citation` (asserts bare citations
  NOW fail) plus mixed / adjacent-substance / ISO-date coverage.
- `tests/test_defender_heuristic.py` — Defender hardening tests
  covering the new `edgar:form4:aggregate` and `edgar:8-K:...:para_N`
  anchor sources. Verified that pushback citing resolved insider $
  figures does NOT fire Defender; fake $ figures still do.

The full LLM-evaluator upgrade (structured per-axis grading) is
tracked separately as #10.

## 3. EDGAR Form 4 insider transactions

Status: **CLOSED** — all three wiring surfaces shipped 2026-05-22.
Parser + aggregation + Stage 1 / Stage 2 / `/probe` integration all live.

Shipped:
- `parse_form4(xml)` over stdlib ElementTree — handles SEC's
  `<value>`-wrapper convention, HTML-entity decoding, multiple
  reporting owners (joint filings), empty/relationship-only filings.
  Splits non-derivative (common stock) and derivative (options /
  RSUs) tables.
- `Form4Transaction.net_dollars` — signed by acquired/disposed code;
  $0-price entries (grants, exercises) contribute $0 (no yfinance
  backfill per scope decision).
- `form4.fetch_form4(client, filing)` — free function (inverted from
  a client method post-review to keep `EdgarClient` free of
  form-type knowledge); strict `form_type=="4"` check.
- `aggregate_insider_activity(filings, window_days=90, as_of=)` →
  `InsiderActivitySummary` with per-officer rollup (sorted by
  |net $|), separated `code_mix` vs `deriv_code_mix`, window
  filtering on `period_of_report`.
- `stage_1_line()` — one-liner matching the spec format
  ("insider net flow last 90d: -$42.0M / 4 sales / 0 buys").
- `stage_2_block()` — 3-line enrichment block: counts + net $ +
  latest tx date / code mix / top-3 officers by absolute $ impact.

Wiring (all shipped 2026-05-22):
- **`/research` Stage 2** — `load_insider_activity(symbol)` runs in
  parallel with `load_ticker_data` / `load_headlines`.
  `_stage_2_thesis` injects `stage_2_block()` into the new
  `{insider_activity_block}` slot in `stage_2_thesis.txt`. Source rule
  list extended with `edgar:form4:aggregate`.
- **`/probe`** — same wiring pattern as Stage 2 in `probe_ticker` /
  `_stage_2_probe` / `probe.txt`.
- **Stage 1 brief filter** — `load_insider_activities_batch(universe)`
  fans out across the universe through a shared `EdgarClient`
  (CIK ticker-index amortized to one HTTP call). `build_brief`
  injects an `insider_summary` line per candidate; `stage_1_filter.txt`
  gains a severe-insider-selling gate (net ≤ -$10M AND ≥3 sales AND
  0 buys → cap `intrinsic_score` at 0.4 unless an explicit bullish
  catalyst dominates). Graceful degrade per-ticker: unavailable /
  no Form 4 last 90d strings are neutral.

Graceful degrade: at every surface, `None` summary maps to
"(insider data unavailable)" while `total_filings=0` maps to
"(no Form 4 last 90d)" so Stage 1/Stage 2/probe can distinguish
"data missing" from "no activity".

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

Status: **CLOSED** — adapter + aggregation + Stage 2 + `/probe`
wiring all shipped 2026-05-23.

Shipped:
- `parse_13f(xml)` — namespace-stripped ElementTree parse of
  infotable.xml. Normalizes pre-2023 $thousands → post-2023
  $whole-dollars based on derived `period_of_report`. Handles SH and
  PRN share-type entries.
- `_quarter_end_for_filing_date` — derives period deterministically
  (13F-HRs due 45d after quarter end), avoiding a second HTTP fetch
  per filing for the cover form.
- `aggregate_institutional_ownership(current, prior, *, ticker,
  issuer_match)` — flips per-fund holdings into per-stock view;
  computes new_positions, exited_positions, funds_holding (current
  and prior). Consolidates multi-class entries per manager.
- `form13f.fetch_13f(client, filing)` (free function, symmetric with
  `fetch_form4`) retrieves `infotable.xml` at the deterministic
  archive URL.
- `load_institutional_ownership(ticker, *, tracked_funds=, issuer_match=)`
  fetches latest 2 quarters per tracked fund via `asyncio.gather`,
  auto-derives `issuer_match` from SEC submissions.json's company
  name (stripping common suffixes — "NVIDIA CORPORATION" → "NVIDIA").
  Graceful degrade per fund.
- `DEFAULT_TRACKED_FUNDS` starter list (BlackRock, Vanguard, State
  Street, Berkshire, FMR). Operator-configurable per call.

Wiring (all shipped 2026-05-23):
- **`/research` Stage 2** — `load_institutional_ownership(symbol)`
  runs in `asyncio.gather` alongside yfinance + Form 4. `research_ticker`
  / `_stage_2_thesis` accept the new `institutional_ownership` kwarg;
  `_format_institutional_ownership_block` injects `stage_2_line()` into
  the new `{institutional_ownership_block}` slot in `stage_2_thesis.txt`.
- **`/probe`** — same pattern in `probe_ticker` / `_stage_2_probe` /
  `probe.txt`.
- Source-rule lists in both prompts extended with
  `edgar:13f:aggregate` and note the 45-day reporting lag (fundamental
  signal, not catalyst-timing).

Gate: **`/research` Stage 2 only** + **`/probe`**. Stage 1 (brief)
intentionally excluded — 45-day lag makes 13F flow useless for
catalyst-driven brief filtering.

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

## 8. Per-ticker observations stream — read phase (pull-model substrate for #20)

Status: **OPEN — reframed 2026-06-02 from push to pull**. Original gate
("≥1-2 weeks of write data") has been satisfied since 2026-05-22 + 2 weeks
= 2026-06-05; the data is there. The reframe changes what ships.

**Original (push) framing — superseded:** Stage 2 prompts in `brief.py` /
`orchestrator.py` accept a `prior_observations` field; orchestrator tails
the last N events from `tickers/<T>/observations.jsonl` and injects them
into every Stage 2 thesis prompt automatically.

**New (pull) framing — pairs with #20:** Expose the observations stream as
a queryable **tool** named `get_prior_reads(ticker, n=5)` (and possibly
`get_prior_observations(ticker, kind=..., n=...)` for richer access).
Any tool-enabled stage opts in by listing it in its tool registry. #20's
brief Skeptic AND research Skeptic make it a **mandatory** tool (every
Skeptic call gets the trajectory; can't miss). Stage 2 thesis — when it
eventually converts to tool-use — would list it as an optional tool the
thesis writer pulls when continuity matters.

Why pull over push:
- One tool definition serves multiple consumers (Skeptic both surfaces,
  Stage 2 future, /probe). Push requires per-stage prompt-slot wiring.
- Typed structured returns (JSON dict of trajectory entries) the model
  parses with type guarantees, vs free-form prompt text it reads loosely.
- Individually cached per chain_id (same `get_prior_reads(MRVL, 5)`
  serves all stages in a /research run without re-reading the JSONL).
- Stage 9 derived views (timeline.md, _index.json) become read-side
  conveniences over the same substrate — operator and model query the
  same source of truth.

Caveat: brief output stops being a pure function of the day's market
data once this is on. Cached re-runs of the same `chain_id` still
reproduce (tool returns are persisted to trace JSONL and replayed); only
fresh runs reference day-(N-1) observations.

Ship order: lands as part of #20 (mandatory tool on both Skeptic surfaces)
— the substrate is built but exposing it as a tool is one of #20's tool
registry entries. If #20 is gated by #19 data, #8 is gated transitively
on the same condition.

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

Status: **OPEN — re-evaluate against #19 findings before any code**
(revised 2026-06-02).

Original scope: replace heuristic quality gates ("fundamentals/filings
depth" check, etc.) with a small evaluator LLM call that scores Stage 2
output against the quality contract and returns structured pass/fail per
dimension. Largest scope of the open list; wants a stable foundation
underneath, and benefits from being able to read prior observations
(write-phase shipped; read-phase = #8) and grep filings (#1, #3, #5,
plus #11–#13 as they land) as evaluation inputs. Highest cost item —
ship last among the build queue.

Referenced in `.claude/skills/brief.md:60`,
`tests/test_quality_contract.py:13`, `tests/test_quality_contract.py:236`.

**2026-06-02 re-evaluation note:** #19 (`/scoreboard`) empirically measures
Stage 2 thesis quality via forward returns (does composite_conviction
predict ticker movement?). That's the same question #10's evaluator LLM
would answer *heuristically*. If #19 shows Stage 2 outputs already have
forward-return signal that correlates with depth, #10's value is small —
heuristic scoring agrees with empirical performance. If #19 shows weak
or absent signal, #10 was solving a measurement problem when the
underlying problem is upstream (cascade can't reason well regardless of
depth scoring). Either way, **do not start #10 until #19 data is in.**
The decision flowchart:
- #19 data shows depth-scoring agrees with forward-return outcomes →
  #10 retires, the heuristic gates are sufficient.
- #19 data shows depth-scoring disagrees with forward returns →
  re-scope #10 around the specific dimensions where disagreement is
  largest, OR retire #10 in favor of upstream cascade fixes.
- #19 data shows verdict has no forward-return signal at all →
  #10 retires; the architecture itself is the problem, not the
  evaluator.

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

## 16. Swing-horizon realignment — brief surface (PR-2)

Status: **STRUCK / SUPERSEDED** (revised 2026-06-02). Both halves of PR-2
are abandoned as originally scoped:

- **16a — `composite.py` constants (euphoria multiplier 0.90 → 1.00,
  parabolic cap 0.40 → 0.55):** STRUCK. Moving hardcoded constants doesn't
  fix brittleness, it relocates it. The fix is to remove the mechanical
  reflex entirely and let regime weight qualitatively via prompt context
  (#21), or to ship the change ONLY if #19 empirically shows the multiplier
  is suppressing genuine winners. No constant-flip without measurement.
- **16b — `stage_2_skeptic_check.txt` anchor-native momentum-continuation
  AGREE clause:** SUPERSEDED by #20. The anchor-native gate was engineered
  around the anchor-only data constraint; once #20 lands the brief Skeptic
  with anchors + tool access, the AGREE clause is rewritten against that
  surface instead of being a one-shot prompt-only fix.

Original scope preserved below for historical context — DO NOT ship as written.
The PR-2 plan doc at `.omc/plans/2026-05-28-swing-horizon-realignment.md` and
the calibration lesson (brief AGREE band [20%, 35%]) remain valuable inputs
for #20's design, even though the PR-2 implementation itself is abandoned.

---

Original status (pre-2026-06-02): **OPEN** (backlogged 2026-05-29). PR-1
shipped — see Closed below.

Full design + ADR: `.omc/plans/2026-05-28-swing-horizon-realignment.md`
(ralplan consensus, Architect SOUND + Critic APPROVE).

Motivation: the cascade carried an inherited long-horizon bias-defense prior,
but the product's mission is swing trading (days-to-weeks) — so in a
momentum/euphoria tape it could never produce a buy. PR-1 fixed the `/research`
path (prompt-only, shipped). PR-2 carries the same realignment to the BRIEF
surface and is the remaining work:
- `composite.py`: euphoria regime multiplier 0.90 → 1.00 (neutral for signal;
  risk surfaced via a `late_cycle_regime` tag); parabolic hard cap 0.40 → 0.55
  (keep `min(score, CAP)` semantics so bonuses can't rescue; 0.55 clears the
  0.4 brief survivor floor so a confirmed momentum leader ranks).
- `stage_2_skeptic_check.txt`: an anchor-text-native momentum-continuation
  AGREE clause (the brief inline Skeptic sees only the two anchors, so it gates
  on anchor semantics, NOT TA fields — intentionally different from the
  `/research` gate).

CALIBRATION LESSON (PR-2 measurement gate, 2026-05-29 — do NOT lose): a first
cut of the anchor-native AGREE clause overshot the brief AGREE band to 50%
(ceiling 35%). Root cause: the "generic extension" carve-out was too broad and
swallowed NAMED bearish mechanisms (INTC's bearish-MACD divergence + declining
volume got waved through as "generic extension"). Fix before resuming: the
carve-out must apply ONLY to pure RSI / percentile / "extended" / "priced-in"
language and EXCLUDE bear anchors that name a bearish-indicator state (MACD
bearish/divergence, MA breakdown, declining-volume-on-the-move). Then
re-measure the brief AGREE rate into [20%, 35%] (that band is the brief AGREE
rate specifically; the `/research` CONFIRM rate is a separate per-surface
metric). The reverted PR-2 implementation is reconstructable from the plan doc.

Deferred by operator decision: PR-1's `/research` results were judged
sufficient on their own; PR-2 resumes only if the brief surface needs it.

## 17. Insider `net_dollars` conflates discretionary sales with comp mechanics

Status: **CLOSED** (2026-06-02 audit). Both candidate fixes shipped:
- `discretionary_net_dollars` field added to `Form4Transaction` and
  `aggregate_insider_activity` (commit `037728e`); excludes F/M comp
  mechanics, keys on S/P codes only.
- `composite.py:226` now keys `INSIDER_SELLING_SCORE_CAP` on
  `discretionary_net_dollars`, not the all-disposals `net_dollars`.
- Dossier "discretionary net" label fires when the discretionary figure
  diverges materially from the all-disposals figure (commit `5149c33`,
  `edgar/form4.py:194-199`).

Residual concern split out: per-insider 10b5-1 status resolution requires a
multi-filing pull that today's `/probe --filing 4` (latest single Form 4)
can't satisfy. That gap lives in #18 (Skeptic prompt-layer mitigation) and
is fully resolved by #20's `get_form4_codes()` mandatory tool.

Original triage notes (kept for historical context):

`Form4Transaction.net_dollars` (`edgar/form4.py:79-85`) signs every
non-derivative disposal negative (`shares × price_per_share`), and
`aggregate_insider_activity` (`edgar/form4.py:~508`) sums ALL disposals into
`net_dollars` — including **code-F (shares surrendered for tax withholding on
vesting)** — while `sales_count` counts only **code-S (open-market sales)**.
Result: the headline "net -$X / N sales / 0 buys" can be heavily negative from
routine vesting/tax mechanics even when discretionary selling is small.

Empirical (MRVL, 2026-05-29): aggregate codes were M×25, F×25, S×13. The COO's
-$20.7M was entirely M/F (PSU vesting + tax withholding, zero S) yet rolled into
the -$148.9M "net selling" headline — only 13 of 63 transactions were actual
sales, but `net_dollars` treated the F-code disposals as "distribution."

Why it matters: `net_dollars` is the load-bearing insider signal. It feeds
(a) the composite `INSIDER_SELLING_SCORE_CAP` (caps intrinsic at 0.40 when
net ≤ -$10M AND sales_count ≥ 3 AND buys_count == 0) and (b) the Stage 2/3
prompts' "informed institutional distribution" reasoning. F-code tax-withholding
is NOT informed selling, so counting it inflates the bearish "smart money
exiting" read across every ticker (seen this session on IONQ / CRDO / MU / AAL /
MRVL).

Candidate fix: expose a `discretionary_net_dollars` (S/P codes only) alongside
the all-disposals `net_dollars`; key the cap + the prompt "distribution" framing
on the discretionary figure, keep the full figure for supply-overhang context.
Decision needed: does any disposal count as supply (current behavior), or only
discretionary sales count as informed-distribution signal? For the pillar the
Skeptic actually uses, discretionary-only is the right denominator.

Related gap: `/probe --filing 4` retrieves only the latest SINGLE Form 4 (it got
the COO's vesting filing, not the CEO's), so per-insider 10b5-1 / code resolution
needs a targeted multi-filing pull — a separate probe/EDGAR enhancement.

## 18. Skeptic uncertainty-discount regression on 10b5-1 status

Status: **OPEN — will retire on #20 ship** (revised 2026-06-02). The
prompt-layer fix is still a valid interim if #20 stalls, but #20's
mandatory `get_form4_codes()` tool resolves the underlying question by
*answering* it rather than by prompt-forbidding the discount.

Original triage (found 2026-05-29 during the post-#17 MRVL `/research`). Sits
in the lineage of PR 2A.7 / 2A.8 / 2A.9, all of which closed a specific Skeptic
escape hatch ("priced into composite" → "default landing spot" → "uncertainty
premium"). #18 closes the next one: even with the honest discretionary insider
figure in hand (commit `037728e`), the Skeptic discounted MRVL to TEMPER on:

> *"if any significant portion of those sales are discretionary code-S
> rather than 10b5-1 plan disposals, the 'institutional accumulation' pillar
> is directly weakened."*

This is the exact pattern `stage_3_skeptic.txt`'s "UNCERTAINTY IS NOT GROUNDS
FOR DISCOUNT" clause already forbids — the unknown S-vs-10b5-1 split is a risk
factor that does NOT dismantle a pillar via a named mechanism, so it should
route to `open_questions_added`, not `adjusted_score`. The Skeptic regressed.

Empirical (MRVL, 2026-05-29 22:23 UTC): gate opens cleanly (ADX 47.1, volume
expanding 2.07x, no voiding, zero exhaustion shapes confirmed), yet the verdict
was TEMPER on the 10b5-1 conditional. By the rubric the verdict should have
been CONFIRM with the 10b5-1 question logged as an open question.

Mitigation (one shipping in this PR): the `stage_2_block` head now labels the
figure as **"discretionary net"** when it diverges from total disposals (vs
the previous bare "net"), so the Skeptic recognises it as already filtered and
doesn't reach back for an unknown-split mystery framing. That removes one
trigger but does NOT close the 10b5-1 axis itself.

Candidate prompt fix (FOLLOWUPS #18): extend the 2A.9 forbiddance in
`stage_3_skeptic.txt` to explicitly call out 10b5-1 plan-status uncertainty —
"do not discount because the S/P codes' 10b5-1 status is unknown; route to
`open_questions_added`." Calibration check against the KO/JNJ/MU/IONQ/CRDO
suite + the MRVL fixture before shipping.

**Likely subsumed by #20.** Once the Skeptic can actively call
`get_form4_codes()` and resolve the S/F/M code mix itself, the 10b5-1
uncertainty stops being a *prompt* problem and becomes an *investigation*
problem — the Skeptic answers the question instead of being told not to
discount on the unknown. #18 still ships as an interim prompt fix if #20 is
months away; if #20 lands first, #18 retires unstarted.

---

## 19. `/scoreboard` — operator-facing verdict→outcome calibration

Status: **PARTIAL — Phase 1 shipped 2026-06-02 (commit `d1428ab`); Phase 1.5
+ Phase 2 + Phase 3 open**. Architect review on 2026-06-03 surfaced a
silently-dropped surface from the original spec: candidate-coverage /
hit-rate. The other three Phase 1 surfaces (verdict stratification,
conviction decile, anchor-only vs full-data comparison) are either
shipped or correctly deferred. Re-scoped:

- **Phase 1 (shipped)**: `python -m research_assistant scoreboard` reads
  the Stage 2 journal, joins forward returns (5d/10d/30d) from yfinance
  cached at `.research/stage2_returns/`, renders verdict→return
  stratification + conviction decile analysis. Brief-inline Skeptic
  verdicts only. Operator-facing only.
- **Phase 1.5 (shipped 2026-06-03)**: candidate-coverage / hit-rate
  surface. Detects *under-firing* — tickers that ran but the cascade
  never surfaced at meaningful conviction. Joins the alerts journal
  (`.research/alerts/*.jsonl`, the system's record of "interesting
  candidates") with the Stage 2 journal by `(ticker, asof + lookback)`
  and reports: of top-quantile movers, what fraction were surfaced
  within `lookback_days` of the alert at composite_conviction ≥ X
  for X ∈ {0.4, 0.5, 0.6}? Operator flags: `--hit-rate-horizon`
  (7d/30d/90d), `--top-quantile` (default 0.75), `--lookback-days`
  (default 3), `--alerts-window-days` (default 60), `--no-hit-rate`.

  Empirical note (2026-06-03 smoke): only the sector_rotation screener
  is currently firing alerts, and those alerts are on sector ETFs
  (XLB, XLK, …), not stocks the brief separately surfaces. So today's
  hit-rate output shows 0/3 surfaced at all thresholds — correct
  given the only "movers" are sector ETFs the brief doesn't pick
  individually. The surface will produce meaningful signal once
  stock-level screeners (momentum, breakout) are added to the
  pipeline.
- **Phase 2 (OPEN)**: regime + momentum-gate stratification. Requires
  extending the Stage 2 journal schema additively with `regime` and
  `momentum_gate_state` (per its existing additive-only contract).
  `orchestrator.py:680,883` already passes `regime` at write time;
  Phase 2 plumbs it into `Stage2Note` + `_note_to_row`.
- **Phase 3 (OPEN)**: anchor-only (`stage_2_skeptic_check`) vs full-data
  (`stage_3_skeptic`) Skeptic comparison. Stage 3 verdicts live in
  trace JSONLs, not the journal — needs trace-event reader + join by
  `(ticker, chain_id)`. The renderer already accepts a `verdict_order`
  parameter (landed in Phase 1's review-followup commit) so Phase 3
  passes the Stage 3 vocabulary without refactoring the rendering layer.

Phase 1 caveat: the rendered output now carries an inline note ("ⓘ This
view scores tickers we surfaced; it does NOT detect tickers we missed
— see #19 Phase 1.5") so an operator reading scoreboard without
Phase 1.5 cannot conclude "the cascade is calibrated."

Original scope notes preserved below — superseded by the phased breakout
above.

Original status (pre-2026-06-03): **OPEN** (scoped 2026-06-02 after MRVL/DELL both ran sharply higher on
6/02 against the cascade's 5/29 TEMPER on MRVL). Gates the deferred
"richer-inputs" spec at `.omc/specs/richer-inputs-v1.md` — measurement before
intervention.

The lead-engineer call (2026-06-02): we don't yet know whether the
temper-on-momentum-extension pattern is a *systematic* mispricing or an N=2
recency-bias illusion from one trading day. The richer-inputs spec would add
breadth + calendar + regime persistence + Stage 3 prior_reads to address a
phantom we haven't measured. Build the measurement first.

What to ship:
- New CLI subcommand `python -m research_assistant scoreboard` (cli.py
  follows the existing `_cmd_trajectory` / `_cmd_alerts` shape).
- Reads existing `.research/stage2/<TICKER>.jsonl` (Stage 2 note journal,
  schema in `journal/stage2_notes.py`) joined with screener-alert forward
  returns (already enriched per `.research/alerts/*.jsonl`).
- **Verdict→return stratification.** Distribution of forward returns
  (5d / 10d / 30d) stratified by (a) Skeptic verdict bucket
  (CONFIRM / TEMPER / CHALLENGE / INVALIDATE), (b) regime at decision time
  (bull-trending / euphoria / choppy / etc.), (c) momentum-continuation gate
  state (clean-continuation / voided / not-applicable). Surfaces: median +
  p25/p75 per bucket, count per bucket (small-N warning when count < 5),
  median delta CONFIRM-minus-TEMPER per regime.
- **Conviction calibration (decile analysis).** Sort all journal entries by
  composite_conviction (post-Skeptic), bucket into deciles, compute median
  forward return per decile. Monotonic increasing across deciles = score has
  predictive signal; flat = score is decorative. This is THE foundational
  measurement — if conviction doesn't predict returns, the entire scoring
  architecture is fancy noise and downstream design questions (#20, #16,
  richer-inputs spec) need to be reset against that finding.
- **Candidate coverage (hit rate).** Of the universe's top-quartile movers
  over each forward-return window, what fraction did the brief surface at
  conviction ≥ threshold? Reports hit rate at conviction ≥ {0.4, 0.5, 0.6}
  cutoffs. Catches the Type II error the verdict-stratification misses
  ("system says no to everything that runs"). Requires a universe-return
  fetch alongside the journal data — heavier than the other two measures.
- **Anchor-only vs full-data Skeptic comparison** (PR 2A.7 empirical test).
  Joins `stage_2_skeptic_check` events (brief inline, anchor-only) with
  `stage_3_skeptic` events (`/research`, full data) for tickers covered by
  both paths in the same window. Reports verdict agreement rate and
  forward-return predictive accuracy of each path. If full-data Skeptic
  systematically predicts forward returns better than anchor-only Skeptic,
  PR 2A.7's data isolation is empirically costing accuracy → feeds the #20
  redesign decision. If they're comparable predictors, isolation was the
  right call.
- Optional `--window 30d` flag; default = all-time-since-journal.

Why this shape:
- The stratification axes are exactly the ones the richer-inputs spec was
  going to feed the Skeptic. If TEMPERs on clean-continuation setups in
  euphoria regime *systematically underperform* CONFIRMs in the same bucket
  by a wide margin → richer-inputs has empirical justification. If they
  perform comparably → the cascade is doing its job and richer-inputs is
  solving the wrong problem.
- Operator-facing only — the model never sees this dashboard. Preserves the
  counter-cyclical backbone (see `project_mission_swing_trading.md` linked
  guidance + the explicit non-goal in the richer-inputs spec).
- No cascade changes, no prompt changes, no scoring changes — pure read-side
  view. Zero risk to live system.

Gates / non-gates:
- Gate for richer-inputs (spec `.omc/specs/richer-inputs-v1.md`): does NOT
  ship until `/scoreboard` data answers "is the TEMPER rate on
  momentum-continuation systematically wrong in current regime?" Sit with the
  dashboard for 2-4 weeks of journal entries before re-opening.
- Does NOT gate the structural sub-fix (bucket B alone — wire `prior_reads`
  into Stage 3 Skeptic to close the orchestrator.py:422-vs-:542 asymmetry).
  That one is unambiguous regardless of calibration data; can ship anytime.

Estimated lift: small. ~1 file (`cli.py` subcommand + helpers), ~200 lines,
~5 tests covering empty-journal / sparse-bucket / multi-regime stratification.
No new dependencies.

Cross-references:
- Source spec: `.omc/specs/richer-inputs-v1.md` (deferred follow-up #1 listed
  there as the "operator-facing calibration dashboard")
- Plan + ralplan critique: `.omc/plans/2026-06-02-richer-inputs-v1.md`
- Mission memory: `project_mission_swing_trading.md` (forward-return journal
  named as mission-critical for swing horizon)

---

## 20. Skeptic hybrid-investigation rewrite (supersedes #16b, subsumes #18)

Status: **BLOCKED on #26** (revised 2026-06-08). Spec drafted at
`.omc/specs/skeptic-hybrid-investigation-v1.md`; architect + critic
review surfaced two blockers: (a) load-bearing factual error in the
spec — "reuse Stage 2's existing tool-use pattern" claim is false
(`ClaudeClient.call` has no `tools=` kwarg, no tool-use loop;
~150 LOC of new SDK plumbing needed); (b) the empirical gate
(decile-10 +19.14% in scoreboard) was shown by bootstrap analysis
to be N=6 with 3 of 6 entries being MRVL on 2026-05-29 triple-
counted — median collapses to ≈ -4.9% on unique trades, bootstrap
p50 = +0.64%, only 51% of resamples positive. Without a working
empirical gate, the spec's "the research Skeptic works at the top,
let's give the brief Skeptic the same data" premise is unsupported.

#26 (trajectory-mode scoreboard) is the replacement gate. #20
re-opens when trajectory analysis shows: (1) PEAK-AND-FADE
hit rate > 50% AND median P&L > 0 across N≥10 classified
trajectories; AND (2) brief-Skeptic-AGREE entries don't
systematically underperform brief-WEAKEN entries on the
trajectory metric. Either failing means cascade-level issues,
not Skeptic-surface issues.

Original status (pre-2026-06-08): **OPEN** (scoped 2026-06-02, hybrid framing landed after architecture
discussion). Architectural rewrite of how the Skeptic stage operates.
Supersedes the prompt-only half of #16. Likely subsumes #18 (10b5-1
uncertainty regression) on landing.

### Motivation: both pure push and pure pull have load-bearing failure modes

Today's Skeptic surfaces are imperfect along two different axes:

- **Brief inline Skeptic** (`stage_2_skeptic_check`, anchors-only per PR
  2A.7) — too data-starved. It critiques rhetorical summaries instead of
  evidence; the anchor-text-native AGREE gate (#16b) engineered around the
  constraint rather than fixing it.
- **`/research` Stage 3 Skeptic** (`stage_3_skeptic`, full thesis_json) —
  too data-dumped. Cherry-picks one weakening signal from the pre-loaded
  set (e.g. MRVL 5/29: clean momentum gate ALSO present in the prompt
  alongside extension shapes; Skeptic picked extension). Attention
  dilutes; upstream-score escape valves leak in (PR 2A.7's failure mode).

A pure-pull "active investigator" design would let the Skeptic call tools
adaptively based on what the anchors raise. That gives adaptive depth and
typed-structured returns, but loses push's **can't-miss guarantee**: the
model can skip a critical pull by laziness or by convenience.

The hybrid synthesis preserves push's guarantee where it matters and adds
pull's flexibility where it earns its keep.

### Design: mandatory tools (push floor) + optional tools (investigation layer)

**Mandatory tools** fire on every Skeptic invocation. Non-negotiable. The
Skeptic cannot skip them. These migrate today's pre-loaded prompt content
from prompt-slot to typed tool-call — same data the model sees today, but
returned as structured JSON the model parses with type guarantees, cached
per chain_id, and individually traced. Identical "can't miss" guarantee
as push, plus better cross-cascade caching and auditability.

- For **brief inline Skeptic**: `get_world_state_macro()`,
  `get_bull_bear_anchors()`, `get_prior_reads(ticker, n=3)`.
- For **`/research` Stage 3 Skeptic**: the brief floor PLUS
  `get_form4_codes(ticker)`, `get_recent_headlines(ticker, hours=24)`,
  `get_insider_summary(ticker)`.

The mandatory set is the engineering team's commitment: "these inputs are
load-bearing for every Skeptic call, and we will not let the model skip
them." Adding or removing a mandatory tool is a code change, reviewable.

**Optional tools** fire only when the Skeptic explicitly invokes them
*and* pre-registers a verdict-shift hypothesis. Each optional pull must
declare: "if this tool returns X, my verdict shifts to Y." If a tool
isn't named in advance as decision-relevant, it doesn't run. Budget cap:
max 3 optional pulls per Skeptic pass.

- `get_filing_excerpt(form, ticker, query)` — when a claim cites a
  specific 10-K / 8-K passage that needs verification
- `get_sector_relative_momentum(ticker)` — when sector-vs-SPY context
  is anchor-relevant
- `get_volume_profile_detail(ticker, days)` — when the bear anchor
  names a volume-confirmation question
- `get_options_iv_term_structure(ticker)` — when event proximity makes
  IV-vs-HV decisional
- `get_13f_position_delta(ticker)` — when an "institutional accumulation"
  pillar needs evidence
- (Tool surface grows over time as new investigation patterns surface
  from journal review)

### The synthesis turn — recovers what hybrid loses vs pure push

Pure push presents all inputs together; the model synthesises across them
in one read. Sequential tool calls break that — by the time the model
reads insider data, the macro context may have left its active attention
window. The mitigation: after all mandatory + elected optional tools have
completed, the orchestrator constructs a **synthesis block** that
re-presents every tool's return in a single concluding prompt turn. Tools
fire → results accumulate → final reasoning turn reads them all together
→ verdict + reasoning. This recovers cross-input synthesis while keeping
the typed-tool guarantees on input collection.

### What ships

- **Tool-loop wrapper in `orchestrator.py`** for the Skeptic stage
  (parallels Stage 2's existing tool-use pattern — pattern reused, not
  invented).
- **Tool registry** in a new `research_assistant/skeptic_tools.py`
  exposing the read-only functions. Mandatory tools tagged
  `MANDATORY=True`; optional tools tagged `MANDATORY=False`. Single
  source of truth — the prompt's advertised menu is generated from the
  registry, with mandatory tools listed separately from optional.
- **Revised `stage_2_skeptic_check.txt` and `stage_3_skeptic.txt`** —
  output schema requires (a) pre-registration list for any optional
  tool the Skeptic intends to invoke, (b) the synthesis-turn reasoning
  reading all tool returns, (c) final verdict + critique_text +
  tools_called list (for trace + auditability).
- **Mandatory-tool execution** happens automatically — orchestrator
  fires them in parallel before the model's first reasoning turn, so
  the model's first prompt already contains their returns.
- **Optional-tool execution** happens in a second turn where the model
  emits pre-registration + tool-call requests; orchestrator validates
  the pre-registration field is non-empty per call, then executes;
  results feed the synthesis turn.

### Gates / non-gates

- **Gate**: #19 (`/scoreboard`) data first. The anchor-only vs full-data
  Skeptic comparison output (added to #19's scope) tells us empirically
  whether the current PR 2A.7 isolation is costing accuracy. If the
  data says yes, #20 is justified. If the data says no, #20 drops to
  LOW priority — the current architecture is working and the
  engineering cost isn't earned.
- **Pairs with**: #8 (per-ticker observations stream read phase —
  reframe from push-model "inject prior_observations into Stage 2
  prompt" to pull-model "expose as `get_prior_reads()` tool"). #8's
  substrate becomes one of the mandatory tools.
- **Pairs with**: #9 (derived views on the same observations substrate).
- **Independent of**: composite.py (not touched), verdict enum (not
  touched), REGIME_MULTIPLIER (not touched). Bias-defense backbone
  preserved.

### Estimated lift

Medium-large. ~3 new files (`skeptic_tools.py` tool registry, tool-loop
wrapper, mandatory-runner), ~2 prompt rewrites, ~15-20 tests covering
mandatory-tool execution, optional-tool pre-registration enforcement,
synthesis-turn rendering, budget-cap enforcement, schema-drift detection.

Latency cost: ~+30-60s per Skeptic invocation due to sequential tool
turns (mandatory parallel, then optional, then synthesis). Cache hits on
mandatory tools amortise within a brief (same `get_world_state_macro()`
return served 5x to 5 brief tickers). Real-money cost: per-Skeptic
tokens increase ~2-3x; partially offset by smaller pre-loaded prompts.

### Risks

- **Cherry-picking on the optional surface** — model could pre-register
  a verdict-shift rule that *favours* its preferred narrative. Mitigation:
  the trace records pre-registration verbatim, so post-hoc review can
  catch "model pre-registered a rule that essentially can't fail." If a
  rule has degenerate triggers (e.g. "if get_form4_codes() returns
  anything, CONFIRM"), it's visible.
- **Cost spikes if optional cap is exceeded** — orchestrator-enforced
  hard cap at 3 pulls; over-cap calls return an error to the model.
- **Schema drift between advertised menu and implementation** —
  auto-generate the prompt menu from the registry; failing test if a
  hand-written prompt edits the menu manually.
- **Implementation that bypasses pre-registration** (model calls tool,
  sees result, rationalises) — enforced at the prompt schema layer: the
  pre-registration field is required output *before* the tool-call turn;
  orchestrator rejects optional calls without a corresponding
  pre-registration entry.
- **Loss of cross-input synthesis** vs pure push — mitigated by the
  synthesis turn that re-presents all tool returns together.
- **Non-determinism across runs** — mitigated by replay-load tool returns
  from the trace JSONL rather than re-fetching on replay. Same chain_id
  reproduces same evidence base.

### Cross-references

- Supersedes: #16b (anchor-native AGREE clause)
- Subsumes: #18 (10b5-1 uncertainty regression — likely retires unstarted
  if #20 ships first; `get_form4_codes()` answers the question instead of
  the prompt being told not to discount on the unknown)
- Pairs with: #8 (observations stream as `get_prior_reads` tool) and #9
  (derived views on the same substrate)
- Gated by: #19 (`/scoreboard`) — anchor-only vs full-data comparison
- Original PR 2A.7 design rationale: `orchestrator.py:454-460` (data
  isolation as no-escape-valve invariant — preserved by mandatory tools
  expressing the same data through typed structured returns rather than
  loose prompt text, and by composite_conviction NOT being a mandatory
  tool)
- Architecture lineage: this is the "hybrid mandatory-plus-optional"
  synthesis from the 2026-06-02 design discussion. Pure-push gives
  can't-miss guarantee at cost of attention dilution + cherry-picking.
  Pure-pull gives adaptive depth at cost of laziness + non-determinism.
  Hybrid preserves can't-miss (mandatory floor) + adaptive depth
  (optional layer with pre-registration) + cross-input synthesis
  (synthesis turn).

---

## 21. History read-path unification — single source of truth across ledger / journal / briefs

Status: **PARTIAL — shipped 2026-06-05.**

- Unified reader at `research_assistant/history/reader.py` unions the
  brief journal and dossier ledger into a common `Stage2HistoryEntry`
  shape, sorted chronologically.
- Per-chain trace enrichment at
  `research_assistant/history/trace_reader.py` recovers the structured
  `conviction_score` (pre-Skeptic) and `adjusted_score` (post-Skeptic)
  from `.research/traces/<date>/<chain_id>.jsonl` for research entries.
  Missing trace files leave the fields None (operator sees `?`).
- `trajectory` CLI repointed; MU now shows 7 entries (was 1), each
  row showing the pre→post-Skeptic conviction delta:
  `2026-05-29 [research] · conviction 0.28 → 0.13 (Skeptic CHALLENGE)`.
- 549 tests passing (24 new in `tests/test_history_reader.py`).
- Scoreboard left on its journal-only path — adding research Skeptic
  there is #23 (planned next, builds on this reader).
- Lossy-ledger-summary cleanup (160-char truncation in
  `orchestrator.py:643,647`) still deferred — trace enrichment covers
  the conviction-number gap; the prose preview being lossy doesn't
  block downstream analytics.

Originally caught 2026-06-05 during a real operator scoreboard
query. Prerequisite for #22 (`/history` operator surface) and #23
(scoreboard research-Skeptic + discount-magnitude calibration).

**The two write paths.** Verified by grep in `brief.py` and
`orchestrator.py`:

- `/brief` writes brief-Stage 2 + inline Skeptic results to the
  journal `.research/stage2/<TKR>.jsonl` only. Verdict vocabulary:
  AGREE / WEAKEN / STRONG_OBJECTION.
- `/research` writes Stage 2 thesis + Stage 3 Skeptic critique to
  the dossier ledger `.research/tickers/<TKR>.md` only. Verdict
  vocabulary: AGREE / WEAKEN / TEMPER / CHALLENGE /
  STRONG_OBJECTION (Stage 3 vocabulary, richer).

Neither path writes to both files. They record **different events**
on the same ticker (a brief scan vs. a deep DD). `trajectory` reads
the journal and so sees only the brief stream; the operator sees
"no prior reads" for tickers that have only been `/research`'d. The
scoreboard is already scoped to brief-Skeptic by design (docstring,
Phase 3 deferred).

**Empirical (2026-06-05 session).** Operator asked for prior verdicts
on the semiconductor names ahead of today's brief. `trajectory MU
--limit 5` reported "1 note (today only)" and `trajectory SNDK`
reported "No Stage 2 history for SNDK." Grepping the dossier ledger
directly:

- `MU.md` ledger lines 66-83: **5 prior CHALLENGE verdicts** between
  2026-05-26 and 2026-06-05 02:31, plus today's TEMPER. None of the
  five prior reads are in `stage2/MU.jsonl` (which has 1 entry —
  today's run).
- `SNDK.md` ledger lines 44-51: thesis + skeptic on 2026-05-23 plus
  an earlier 2026-06-05 02:33 TEMPER. `stage2/SNDK.jsonl` is empty.

**Operator impact.** The session output told the operator MU had no
prior coverage and today's TEMPER was a first-touch verdict. The
actual record is "Skeptic CHALLENGED MU five times in 10 days." That
would have materially changed swing-position sizing. The scoreboard's
"trust the verdict ladder" narrative depends on the ladder being
visible; today it shows only the tail.

**The architectural framing.** The journal and ledger are
**both first-class history streams** — they record different events
(brief reads vs. research reads), not redundant copies of the same
event. Building a unified history view means union-reading both
sources, not picking a winner. Concretely:

- Add `research_assistant/history/reader.py` exposing
  `read_unified_history(ticker, base) -> list[Stage2HistoryEntry]`
  that loads journal rows AND parses the dossier ledger, projects
  both into a common entry shape (with a `source` discriminator),
  and returns the chronological union.
- Ledger parsing pairs `thesis:` + `skeptic:` ledger lines by
  shared `evidence_anchor` (chain_id), regex-extracts the verdict
  word from the skeptic summary's `Verdict: <WORD>` prefix, and
  uses each line's `summary` as the bull/bear preview text.
- Conviction numbers are degraded gracefully on ledger-only
  entries (rendered as `?`) since the ledger summary is lossy
  text; the structured `conviction` dict is journal-only.
- Repoint `_cmd_trajectory` in `cli.py` at the unified reader.
  Leave `scoreboard.py` on the journal-only path for now — the
  scoreboard intentionally scopes to brief-inline Skeptic
  verdicts (its docstring marks Stage 3 research Skeptic as the
  Phase 3 follow-up).

**Why this is now a foundation, not a leaf.** The user-facing pain
isn't `trajectory` being wrong on one ticker — it's that every
higher-level history view (per-ticker timeline, cross-ticker
verdict-cluster scan, sector trajectory roll-up — see #22) inherits
this blind-spot when built on top of either stream in isolation. Fix
the read path first so #22 has a clean substrate.

**Future cleanup (out of scope here).** The lossy ledger summary
(160-char truncation in `orchestrator.py:643,647`) is a separate
data-quality issue. Tracked as a future enhancement: extend ledger
entries to carry an optional JSON payload alongside the human text,
so structured conviction / verdict / decision-tag fields don't have
to be regex-extracted from prose. Out of scope for this item — the
unified reader works on what's on disk today.

**Out of scope here.** Cross-ticker views, brief-history queries, and
the operator-facing CLI — those are #22. This item is only "the four
sources (ledger, stage2 journal, stage2_returns, briefs) collapse to
one canonical reader."

Anchor: dossier evidence at `MU.md:66-83` and `SNDK.md:44-51`,
session 2026-06-05.

Related: #19 (scoreboard) consumes whichever reader this lands.

---

## 22. `/history` — cross-ticker operator surface over the unified reader

Status: **PARTIAL — shipped 2026-06-05.**

- `python -m research_assistant history brief <T>` — per-ticker brief
  history table, ISO-date or `Nd` `--since` filter.
- `python -m research_assistant history cohort <T1,T2,...> [--since]`
  — date × ticker grid of verdict + pre→post conviction. This is the
  scoreboard-comparison table the operator wanted in the 2026-06-05
  session.
- `python -m research_assistant history verdicts [--since] [--verdict]
  [--ticker]` — cross-ticker scan; surfaces verdict CLUSTERS rather
  than single reads (the 2026-05-29 brief had nine CHALLENGE /
  STRONG_OBJECTION entries across the AI/momentum complex — a
  market-regime signal visible only at the cohort level).
- All three accept `--json`, share the `Stage2HistoryEntry` projection,
  and use the unified reader from #21 (so trace-enriched convictions
  show up in the output without extra wiring).
- 20 new tests in `tests/test_history_views.py`; 569 total passing.
- Deferred from the original spec: `history sector <name>` — needs
  sector-roster substrate (config file or `ticker_data.sector`
  classification). Use `history cohort` with explicit ticker list
  until then. Tracked as a v1.1 followon.

Originally caught 2026-06-05 in the same
session that surfaced #21: the operator asked for a scoreboard
comparison across the seven prior-researched semis, and the assistant
ended up writing ~50 lines of ad-hoc Python that walked four
different data sources (`briefs/*.json`, `stage2/*.jsonl`,
`stage2_returns/*.jsonl`, `tickers/*.md` ledger) plus a yfinance
fetch for current prices. The work was correct but should not have
required custom code per session.

**The gap today.** Existing read-side tools each cover one slice:

| Tool | Scope | What it can't do |
|---|---|---|
| `trajectory <T>` | per-ticker Stage 2 timeline | one ticker; broken until #21 |
| `scoreboard` | global verdict→return calibration | no drill-down by ticker or cohort |
| `dossier <T>` | full one-ticker dump | not queryable across tickers |
| `trace <chain>` | one cascade | no cross-chain search |

Three operator questions fall through every existing surface:

1. *"What did we say about ticker X across every brief?"*
   — today: walk `briefs/*.json` manually
2. *"Show me every CHALLENGE / STRONG_OBJECTION in the last 7 days
   across all tickers."*
   — useful for spotting *clusters*, not individual reads (the
   rolling semi de-rating across MU / MRVL / INTC / SNDK was a
   pattern the operator only saw retrospectively because no view
   surfaced it as a cohort)
3. *"Trajectory but cross-ticker — e.g., timeline of every verdict
   on every name in the semi watchlist over the last 30 days."*
   — today: per-ticker `trajectory` loop + manual stitching

**Proposed CLI shape.**

```
python -m research_assistant history brief <TICKER>
    # every brief mention of TICKER: date, conviction, decision_tag,
    # one-line thesis preview, link to chain

python -m research_assistant history verdicts \
    [--since 7d] [--verdict CHALLENGE,STRONG_OBJECTION] \
    [--ticker MU,MRVL,INTC,...] [--sector semis]
    # cross-ticker scan of Stage 2 + Skeptic outputs; spot clusters

python -m research_assistant history cohort <ticker1>,<ticker2>,...
    # cross-ticker timeline table: date × ticker grid of
    # (conviction, skeptic_verdict, forward_return_5d)
    # — the table the operator asked for in the 2026-06-05 session

python -m research_assistant history sector <sector>
    # convenience wrapper around history cohort, with the sector
    # roster pulled from a config file (or eventually from
    # ticker_data.sector classification)
```

All subcommands accept `--json` for chaining, match the existing CLI
output style, and depend exclusively on the #21 unified reader. No
new data sources, no LLM calls (the data is already on disk).

**Schema sketch — one record shape across all subcommands.**

```python
@dataclass
class HistoryEntry:
    ticker: str
    asof: date
    source: Literal["brief", "research", "probe"]
    chain_id: str
    composite_conviction: float | None
    skeptic_verdict: str | None      # AGREE / WEAKEN / TEMPER / CHALLENGE / STRONG_OBJECTION
    decision_tag: str | None         # RESEARCH / PASS / ...
    forward_return_5d: float | None  # joined from stage2_returns
    forward_return_10d: float | None
    forward_return_30d: float | None
    thesis_preview: str              # first 120 chars
    bull_anchor: str | None
    bear_anchor: str | None
```

Every subcommand projects this shape; the only difference is the
WHERE / GROUP BY.

**Non-goals.** Not a replacement for `/research`, `/probe`, `/trace`,
or `/dossier` — those are write-or-render surfaces. `/history` is
read-only and projects existing on-disk data into operator-useful
shapes. Doesn't issue LLM calls. Doesn't fetch live prices (joins on
pre-enriched `stage2_returns/<TKR>.jsonl`; if a horizon hasn't matured
yet, the field is `null` and rendered as `n/a`).

**Why this matters for the mission.** Per
`project_mission_swing_trading.md`, the north star is days-to-weeks
swing trades. The verdict ladder is only useful if the operator can
*see* it across the watchlist on the morning of a trade. Today the
ladder is per-ticker, retrospective, and requires manual stitching.
`/history` makes the ladder a first-class operator surface.

**Implementation order.**

1. Land #21 (unified ledger reader).
2. `history brief <T>` — simplest projection, smallest blast radius.
3. `history cohort` — the table the operator wanted in the 2026-06-05
   session; lands the cross-ticker GROUP BY shape.
4. `history verdicts` — the cluster-detection view.
5. `history sector` — convenience wrapper once the sector roster
   substrate (today's ad-hoc hard-coding) is decided.

Anchor: session 2026-06-05 "scoreboard comparisons" exchange — the
operator explicitly asked whether this should be a tool vs. ad-hoc
grep, and the ad-hoc grep this session needed four data sources.

Related: #6 (watchlist-vs-universe scope flag), #7 (`/watch` CLI),
#19 (scoreboard verdict→outcome — its global view becomes one
projection of `/history`).

---

## 23. IPO-mode surgical patches (pre-listing DD lane)

Status: **OPEN**

Origin: SPCX session 2026-06-06 / 07. Drove the existing toolchain
against an active S-1/A and a live roadshow without touching the
orchestrator — the conviction / Skeptic / journal layer worked
unchanged, but two specific gaps cost most of the manual time.
Motivated by Anthropic and OpenAI both having credible 2026 H2 IPO
windows; this is the **surgical** version, not a full `pre_ipo`
orchestrator mode (deferred until pre-IPO becomes a sustained lane,
not a 3-name campaign).

What hurt in the SPCX walk:
- `EdgarClient.resolve_cik` only handles ticker-indexed entities
  (`company_tickers.json`). Pre-IPO names have no ticker, so the
  operator had to hit `https://www.sec.gov/cgi-bin/browse-edgar` by
  hand to find the CIK (SPCX → 0001181412).
- `_extract_paragraphs` collapsed a 1.5M-char S-1/A to a single
  paragraph — the prospectus HTML structure doesn't match the
  div/p patterns the extractor expects. Workable for keyword
  search but the `edgar:S-1/A:...:para_0` anchor loses all
  citation granularity; every fact in the Stage 2 note pointed at
  the same anchor.
- Stage2Note has no `edgar_anchors` field; the cited filings
  (S-1/A accession + FWP set) lived in the conversation, not the
  journal row — so the row isn't re-runnable as evidence.

Patches (small, additive, each lands independently):

a. **CIK-by-company-name resolver on `EdgarClient`.** New method
   `async def resolve_cik_by_name(query: str) -> list[tuple[str, str]]`
   that hits `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=...`,
   parses the result, returns `[(cik, display_name)]` pairs.
   Operator disambiguates from a short list rather than guessing.
   Keep `resolve_cik(ticker)` unchanged — fast path for tickered
   names. Test against SpaceX / Anthropic / OpenAI / Stripe.

b. **Prospectus-aware paragraph segmenter.** S-1 / S-1/A / 424B
   forms have a canonical section vocabulary (PROSPECTUS SUMMARY,
   RISK FACTORS, USE OF PROCEEDS, CAPITALIZATION, DILUTION,
   MANAGEMENT'S DISCUSSION, BUSINESS, UNDERWRITING, SHARES
   ELIGIBLE FOR FUTURE SALE, etc.). Detect heading-shaped lines
   and split on them, then sub-split within sections on
   sentence-boundary heuristics. Reuse the same `FilingText`
   contract; just produce ~200-600 paragraphs instead of 1.
   Anchor format unchanged. Smoke test: SPCX S-1/A AMD#2 must
   yield ≥ 100 anchorable paragraphs.

c. **Additive `edgar_anchors` field on Stage2Note + journal row.**
   `edgar_anchors: tuple[str, ...] = ()` — list of
   `edgar:{form}:{accession}:para_{n}` anchors backing the
   bull/bear text. Per the schema contract (additive only,
   `Optional` with sensible default), no migration needed. Update
   `_note_to_row` to persist when non-empty. Sanitize each
   anchor through `_sanitize_text` (the existing 240-char cap is
   already over-budget for an anchor — actual anchors are ~50
   chars).

d. **Documented IPO-mode template.** A markdown crib (somewhere
   under `docs/` or `.omc/specs/`) showing the SPCX walk as a
   reusable recipe: CIK lookup → list filings (DRS, S-1, S-1/A,
   FWP) → `fetch_filing` on the latest S-1/A → keyword probes
   for offering size / price / lockup / use of proceeds /
   selling stockholders / stock split / accumulated deficit
   / underwriter syndicate → isolated Skeptic → Stage2Note
   write. Includes the lockup-mechanics extraction template
   (T+70 / T+90 / First Earnings Release Date / 30% trigger /
   demand registration rights) because lockup is the dominant
   short-horizon trade-killer on every IPO, not a SPCX quirk.

Scope NOT in this item (deferred until sustained pre-IPO volume):
- Full `pre_ipo` orchestrator mode that skips price-dependent
  stages
- Scoreboard / `stage2_returns` adaptation for pre-listing entries
  (today they silently no-op for ~30 days post-listing, which is
  fine)
- New conviction pillars / dimension renames — the canonical
  technical / fundamental / catalyst / regime quartet handled
  SPCX unchanged; the bull/bear-anchor text does the reinterpretation
- A "pre-IPO ticker" namespace (`PRE:NAME`) — the assigned IPO
  ticker passes existing validation; SPCX worked unchanged

Reference data already on disk: `.research/stage2/SPCX.jsonl`
(row written 2026-06-06) — the first IPO-mode entry. Use as
schema regression for patch (c).

Trigger to escalate to a real `pre_ipo` mode: if Anthropic and
OpenAI both file by Q4 2026 and operator is still hand-driving
EDGAR from Python, build the full lane.

Related: #1 (EDGAR foundation — patches a + b extend it), #20
(Skeptic isolation — IPO-mode confirmed canonical Skeptic prompt
already handles IPO anchors correctly with no template changes).

---

## 24. 13F adapter silently 404'ing across all tracked funds

Status: **OPEN — bug, not a feature gap.** Surfaced 2026-06-08 during a
12-ticker /research pass.

Every single dossier ran today emitted:

```
WARNING research_assistant.edgar.form13f: EDGAR: 13F fetch failed for
BlackRock (0001086364-24-008417): Client error '404 Not Found' for url
'https://www.sec.gov/Archives/edgar/data/1364742/000108636424008417/infotable.xml'
...
INFO research_assistant.edgar.form13f: EDGAR 13F: no usable filings for
<TICKER> across 5 tracked funds
```

Same pattern for all five `DEFAULT_TRACKED_FUNDS` (BlackRock, Vanguard,
State Street, FMR, Berkshire) across all 12 tickers — *every* infotable
URL 404s. The institutional-flow side of every thesis is silently
empty, but the dossier line "No 13F coverage available" looks like
*absence of activity* rather than *failure to fetch*.

Why it matters: 13F is one of the four conviction pillars (per the
Skeptic prompt: institutional-accumulation pillar). When it's silently
missing, the Skeptic falls back on insider Form 4 as the entire
fundamental-flow read, and the "institutional accumulation" sub-pillar
in Stage 2 theses gets pattern-matched to "no coverage" instead of
"checked and unsupportive."

Candidate diagnoses:
- URL pattern changed (SEC moved from `infotable.xml` to a different
  filename, or accessions are now zero-padded differently).
- CIK or accession resolution is stale — the fetch is computing the
  wrong target.
- The default fund list itself is stale (CIK 1364742 vs 1086364 for
  BlackRock — the warning string shows a mismatch between the bracket
  and the URL).

Fix:
- Reproduce against one filing manually (curl the URL) to confirm 404
  is real, not a User-Agent / throttle issue.
- Audit `form13f.py` URL construction vs the SEC archive layout.
- Add an integration test that hits *one* real archive URL and asserts
  the parser gets a non-empty position list — current tests likely mock
  the HTTP layer.
- On systemic 404 (not parser bug): widen the surface so
  "fetch_failed" is distinct from "no_filings_in_window" in
  `aggregate_institutional_ownership`, and surface fetch_failed in the
  Stage 2 block as `(13F: fetch_failed)` instead of `(no 13F coverage)`
  — at minimum the operator should see the bug, not silently lose the
  pillar.

Cross-ref: closes-broken-#5. The original #5 ship was correct in
shape; the URL/CIK layer is what regressed.

---

## 25. VIX delisted in yfinance — Stage 0 regime confidence has a hole

Status: **OPEN — data adapter fix.** Surfaced 2026-06-08 across all
12 /research runs and the morning /brief.

Every dossier and the brief emit:

```
ERROR yfinance: $^VIX: possibly delisted; no price data found  (period=3mo)
WARNING ozymandias.data.adapters.yfinance_adapter: fetch_bars failed for ^VIX
WARNING research_assistant.data_loader: instrument snapshot failed for ^VIX
```

The brief still renders `VIX: None (falling)` in Stage 0 — but "falling"
is inferred from headline text, not a measured value. Regime confidence
of 0.62 reported today was therefore computed with VIX = null and a
narrative-extracted direction. The brief's choppy/euphoria classifier
weights VIX trend non-trivially, so this silently downgrades regime
quality on every run.

Fix candidates:
- Switch `^VIX` → `^VIX9D` or `^VVIX` in the data adapter (both
  currently active in yfinance).
- Front-month VIX futures (`VX=F` via Yahoo, or a direct CBOE pull).
- Reconstruct from SPX options chain — heavier, but removes the
  upstream-data-vendor dependency entirely.

Smoke test after swap: brief Stage 0 should emit a numeric VIX value
+ measured trend tag (rising/falling/flat from EMA20 vs spot).

Independent of #24 (different adapter, different failure mode), but
both belong to the same "silent data degradation" class — Stage 0
regime quality and Stage 2 institutional pillar are both partially
blind in production right now.

---

## 26. Scoreboard 2.0 — trajectory-aware calibration

Status: **OPEN — spec drafted 2026-06-08** at
`.omc/specs/scoreboard-trajectory-mode-v1.md`. Blocks #20 (replaces its
broken empirical gate). Awaiting architect + critic review.

### Origin

Surfaced 2026-06-08 during the same scoreboard-audit session that
shipped #24 + #25. The architect+critic review of the #20 spec
prompted a bootstrap analysis of the "decile-10 +19.14%" finding the
spec was leaning on; the finding turned out to be **N=6 with 3 of 6
entries being MRVL on 2026-05-29 triple-counted** — three /research
re-runs on the same ticker-date, each writing a separate journal row,
each keyed to the same +40.9% forward return. Without duplicates:
4 unique trades, median ≈ -4.9%. Bootstrap p50 = +0.64%; 51% of
resamples positive.

But the operator review of *the same MRVL data* surfaced something
the point-in-time scoreboard structurally can't see: the system
correctly recommended MRVL into the catalyst at conviction ≥0.4 on
5/29, then **adaptively downgraded** to TEMPER (6/02) and CHALLENGE
(6/08) as the catalyst absorbed and risk surfaced — exactly the
operator-ideal trajectory ("recommends MRVL right before the pop,
adjusts conviction daily based on signals in the days following,
until low ratings basically guarantee a divestment"). The decile
view attributes the same +40.9% forward return to every high-
conviction day independently and can't credit the adaptive arc as
a single trade.

### What it is

A sibling read-side calibration surface to the existing
`/scoreboard`. Treats per-ticker conviction series as **trajectories**
classified into archetypes (PEAK-AND-FADE, FLAT-HIGH, FLAT-LOW,
RISING-LATE, WHIPSAW), defines operational entry/exit signals
mirroring how an operator uses daily updates, and reports
entry-to-exit P&L per trajectory plus divestment-signal forward
returns.

### What ships

- `research_assistant/trajectory.py` — archetype classifier + entry/
  exit signal computation (pure Python, no LLM)
- `scoreboard trajectory <TICKER>` — single-ticker time series view
- `scoreboard trajectories --classify [--since 30d]` — cross-ticker
  archetype counts
- `scoreboard trajectories --pnl [--archetype X]` — entry-to-exit P&L
  aggregate with hit rate + p25/p75
- `scoreboard trajectories --divest-signal` — forward-return test on
  low-rating signals ("do low ratings precede drawdowns?")
- All read-only over the unified history reader (FOLLOWUPS #21).
  No new data sources, no LLM calls, no cascade changes.

### Why this matters for the mission

`project_mission_swing_trading` north star is days-to-weeks swing
trades. The operator updates daily, enters when conviction crosses
up, exits when conviction crosses down. The current scoreboard
evaluates the system as a batch-prediction model (hold blindly for
exactly 10 days from each observation); trajectory mode evaluates it
as the daily-decision tool it actually is. Trajectory framing is
also the only framing that can *credit* the MRVL case correctly —
under point-in-time framing, MRVL on 5/29 looks good and MRVL on
6/08 looks bad, but they're the same trade.

### Open empirical questions the spec must answer before #20 re-opens

1. PEAK-AND-FADE hit rate > 50% AND median P&L > 0 across N≥10
   classified trajectories on existing journal data.
2. Brief-Skeptic-AGREE entries don't systematically underperform
   brief-WEAKEN entries on the trajectory metric (if they do, the
   anti-predictive pattern is confirmed and #20 is justified; if
   they don't, the brief-AGREE → -8.89% finding was point-in-time
   artifact and #20 should be retired).

### Cross-references

- Spec: `.omc/specs/scoreboard-trajectory-mode-v1.md`
- Blocks: #20 (Skeptic hybrid)
- Builds on: #21 (unified history reader), #22 (`/history` surface)
- Augments: #19 (`/scoreboard` point-in-time view stays sibling)
- Origin session: 2026-06-08 (commit `bf445a6` shipped #24/#25 in
  the same session)

---

## 28. Research-surface deep substrate (the panopticon)

Status: **SPEC READY 2026-06-09** at
`.omc/specs/research-deep-substrate-v1.md`. Architect + critic-v1 +
critic-v2 + M4 experiment + EXP1 + EXP2 all incorporated. Empirical
premise validated. Ready for Phase A implementation.

### Origin

Operator review on 2026-06-08 surfaced the architectural weakness:
the cascade today is data-rich on technical / insider / news /
world_state, and **data-poor on depth-of-synthesis**. The Coinbase
puts case was the canonical "would have caught it" example —
operator lost money on a bearish thesis where the 10-Q decomposition
showed the headline loss was unrealized crypto fair-value markdown,
not operational. A human institutional analyst with 90 minutes of
attention catches that; the cascade as architected does not.

The prior `fundamentals-substrate-v1.md` (archived same day) tried to
rearrange reactive substrate — KPIs + industry context from filings.
Operator pushback identified the load-bearing architectural error:
*industry context filtered through management's self-presentation
embeds the company's voice as ground truth, AND the substrate
rearrangement doesn't address the depth-of-synthesis gap*.

### What ships (5 deep readers + synthesizer on /research only)

- **Phase A** ✅ SHIPPED (commit 8e3bd55) — Deterministic substrate
  (KPIs + earnings calendar + options positioning + analyst revisions +
  insider behavior detail). No LLM calls. Flag: `STAGE_2_DETERMINISTIC_SUBSTRATE=on`.
- **Phase B** ✅ SHIPPED (commit 3b48f90, branch `feat/phase-b-synthesizer`) —
  Stage 1.7 Synthesizer reading Phase A substrate. Emits structured
  flags with two-anchor citations + Haiku role-bound verification.
  Flag: `STAGE_1_7_SYNTHESIZER=on`. See "Phase B post-ship" below for
  the live-smoke findings + the anchor-contract fix that landed with it.
- **Phase C** — Replay harness + falsifiability gate (hand-grading
  with decoy-ticker arm). Gates Phase D. ~1 week. **Now unblocked**:
  the trace event persists kept+dropped flag detail
  (`parsed.flags`) and the `panopticon_degraded` flag Gate 3 needs.
- **Phase D** — Agent A deep filing reader + XBRL-stripped extractor.
  Lift revised to 2-3 weeks after EXP2 showed XBRL extraction failure
  on 3/7 candidate filings (existing `_extract_paragraphs` returns
  XBRL context tags only on iXBRL-heavy 10-Qs).
- **Phase E** — Agent E 8-K event classifier (Haiku). ~3-5 days.
- **Phase F** — `/probe-cohort` with 13F-overlap peer auto-suggestion.
  ~3-5 days.

### Empirical gate — three sub-experiments, all PASSED (2026-06-09)

- **Sub-gate 1a (M4)**: Phase D deep-read on losing trades.
  3/3 evaluable HITs (PLUG mechanical accounting reversal, RGTI
  liquidity-vs-cash divergence, QUBT going-concern contradiction).
  $0.33.
- **Sub-gate 1b (EXP1)**: Phase A-only synthesizer on the same
  losing trades. 3/3 HIT (PLUG multi-year negative gross profit,
  RGTI revenue declined 3 consecutive years + P/S 653x, QUBT P/S
  495x). $0.05.
- **Sub-gate 1c (EXP2)**: Phase D deep-read on winning trades
  (precision arm). 3/3 NO false-positive contrarian flags. MRVL
  deep-read found bullish-supportive detail (non-cash fair value
  marks depressing P&L while business news was good). $0.36.
- **Total experimental cost: $0.74.**

### Cost shape (real numbers anchored on today's traces)

- Modal week (25 /research/wk × $0.18 avg, 5 briefs, 20-30 probes):
  ~$7-8/wk
- P90 week (30 /research/day spike day + 4 normal days): ~$12-15
- `PANOPTICON_DAILY_CAP_USD=$10` circuit breaker with graceful
  per-agent degrade

### Key architectural decisions ratified

- Per-ticker synthesizer by default; per-cohort exposed as
  `/probe-cohort` option (architect rec #5: 13F-overlap auto-suggest)
- Stage 1.7 as own cascade stage (not sub-step in Stage 2)
- Two-anchor citation via role-bound re-extraction (Haiku, ~$0.005
  per anchor) — closes the paraphrase-gameable failure mode
- Synthesizer output is EVIDENCE, not directives — schema dropped
  `thesis_implication`; renamed `*_claim` → `*_observation`;
  Stage 2 prompt framing explicitly tells writer "do not let any
  single flag drive your conviction"
- `axes_agreed: true|false` telemetry replaces architect's killed
  `consensus_unchecked` flag (per critic M1 — surfaced only in
  /scoreboard, not per-ticker dossier)
- Arm assignment by `hash(ticker, iso_week) mod 3` (per critic M3 —
  prevents cache-leakage + probe-cascade confounds)
- Phase order: A → B (synthesizer-on-A) → C (gate) → D (Agent A) →
  E → F. Front-loads measurement.

### Supersedes

- This spec is the empirical landing zone for the "depth-of-synthesis
  gap" hypothesis that #20 and #26 were architecturally rationalizing
  toward without measurement. #20 stays blocked; #26 stays
  downgraded. #28 doesn't compete with them — it solves a different
  failure mode with grounded evidence.

### Phase B post-ship (2026-06-17)

Live-smoke on `/research SOFI` with both flags on exposed an
anchor-contract bug: first real run produced 10 flags, **0 kept** — the
role-bound verifier correctly rejected every flag because the
synthesizer was citing thin anchors (bare-scalar `TICKER_DATA:*`
sub-keys + metadata-only news) it couldn't support. Fixed (landed in
commit 3b48f90): removed scalar sub-keys from corpus + allow-list
(framing claims must cite `daily_signals`), constrained news anchors to
headline-fact claims, replaced the futile per-flag whole-synthesizer
retry (the ~10× cost amplifier) with drop-and-record. Verified
0→3 flags on SOFI; generalizes (PLUG 6/10, QUBT 4/12 — losers surface
more, as predicted). Synthesizer cost $0.47→$0.044.

Landed as follow-ons (branch `feat/phase-b-synthesizer`):
- **Trace persistence** — `parsed.flags = {kept, dropped}` with
  per-anchor verdicts + reasoning (Phase C hand-grading input).
- **Cost circuit breaker** — `research_assistant/cost_breaker.py`:
  per-ET-day per-base ledger, `PANOPTICON_DAILY_CAP_USD` (default $10),
  two-level degrade (skip synthesizer / anchor-existence-only fallback),
  `panopticon_degraded` per-event telemetry + scoreboard surface.

Open follow-ups surfaced:
- **Brief insider aggregate is misleading** — the morning brief
  reported SOFI insiders as "net +$241K buy-side" while the deep Form-4
  fetch showed −$1.6M discretionary / −$10.6M total. The brief's shallow
  aggregate masks net selling. Wire Phase A insider-detail into the
  brief, or report discretionary-vs-total instead of a rosy net.
- **No live `axes_agreed: true`** observed yet — validate the
  clean-agreement happy path on a quiet large-cap.

### Cross-references

- Spec: `.omc/specs/research-deep-substrate-v1.md` (v3)
- Archived predecessor: `.omc/specs/fundamentals-substrate-v1.md`
  (wrong-direction lessons preserved as archaeology)
- Built on: #1 (EDGAR), #17 (discretionary_net_dollars, Agent D
  extends), #21 (history reader), #24 (13F now loading), #25 (VIX)
- Orthogonal to: #19 (/scoreboard), #20 (Skeptic), #26 (trajectory)
- Origin session: 2026-06-08/09 (commit shipping #24/#25 + scoreboard
  dedup #19 Phase 1.7 + this spec)

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

- **Swing-horizon realignment — `/research` path (PR-1, PR 2A.10).** Shipped
  2026-05-29 in commit `0ed13f5`. `stage_2_thesis.txt` + `stage_3_skeptic.txt`:
  swing HOLDING HORIZON blocks (no-DCF is not a demerit for the trade horizon;
  depth floor preserved) + a TA-field-native MOMENTUM-CONTINUATION CONFIRM gate
  (uptrend + adx_14d≥20 + MACD bullish + volume confirmation; voided by
  rsi_14d≥80 / range_pct_20d≥95 / roc_5d≥25; fail-closed on missing trend data;
  self-names the path in `critique_text`). Measurement gate (7-name re-score):
  AGREE/CONFIRM moved off 0% (AAL clean CONFIRM), anti-yes-man guards all fired,
  zero CONFIRM on any voiding ticker. Brief surface continuation = #16.
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
