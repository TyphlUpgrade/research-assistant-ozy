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

Status: **OPEN** (scoped 2026-06-02, hybrid framing landed after architecture
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
