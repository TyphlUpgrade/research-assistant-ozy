# Spec: Fundamentals + AI-Synthesized Industry Context (v1)

**Status:** ARCHIVED 2026-06-08 — superseded by `research-deep-substrate-v1.md`
**Author:** lead engineer
**Date:** 2026-06-08 (archived same day)
**Closes FOLLOWUPS:** none (deprecated before merge)
**Relates to:** #1 (EDGAR foundation — substrate), #19 (scoreboard — calibration gate)

## ARCHIVAL NOTE

This spec was archived during the same session it was drafted, after
operator pushback identified the load-bearing architectural error
that it was solving the wrong problem. The KPI + industry-context
framing rearranged reactive substrate instead of addressing the
**depth-of-synthesis gap** that institutional analysts exploit.

Concrete trigger: the Coinbase puts case (operator narrative 2026-
06-08) — a trade that went against the operator despite all the
relevant evidence being in public filings, because the failure mode
was *shallow reading* of multi-source reactive data, not *missing*
forward-looking data sources. A human analyst with bandwidth catches
"unrealized crypto fair-value markdown ≠ operational loss" by
reading the 10-Q decomposition; the cascade as architected does not.

Reasons for archival:
1. The "industry context synthesis from 10-K Item 1 / Item 1A"
   layer this spec proposed was filtered through management's
   self-presentation — embedding management's voice as substrate
   ground truth was a category error the architect review caught
   (rec #5, the `management_described_competitors` reframing) but
   couldn't structurally undo.
2. Layer 1 (KPI extraction) survives as a foundational deterministic
   adapter, but it's now Phase A of the replacement spec rather than
   a standalone ship.
3. The Stage 1.5 vs cached-slot architectural choice this spec
   labored over was a false dichotomy — the right architecture is
   neither (it's a synchronous deep-read stage that fires only on
   `/research`, not per cascade, gated by the existing
   shallow→deep brief→research pattern the cascade already uses).
4. The falsifiability gate this spec built around per-cascade
   measurement was solving the wrong measurement problem.

The replacement spec at `.omc/specs/research-deep-substrate-v1.md`
keeps the architect's good recommendations (role-trigger
verification for any LLM-synthesis claim, decoupled per-phase
falsifiability gates, two-anchor claim citation, three-arm
randomized assignment for measurement) and discards the bad
framing (Stage 1.5 industry synthesizer from filings as
substrate proxy for institutional analysis).

Preserved here for archaeology — every revision in this file
captures a decision lineage we don't want to lose:
- v1 → v2 changes: architect review incorporation
- v2 → ARCHIVED: operator-surfaced depth-of-synthesis gap

---

**v2 changes incorporated from architect review:**
- Role-trigger verification (`role_evidence_substring`) on every role-bearing
  field, catching the true-token/false-context recombination failure mode
  that bare substring containment misses
- Schema fields renamed to reflect management-self-disclosure provenance
  (`management_described_competitors`, not `top_competitors`)
- Phase 1 + Phase 3 merged into single deterministic-substrate ship
- Phase 2 falsifiability gate decoupled from Phase 1 outcome — each
  substrate hypothesis measured independently against control
- Falsifiability gate widened (60-day window + symmetric CI test)
  to address Type II risk on real +1-1.5% effects at observed dispersion
- New **Architectural Choice** section: Stage 1.5 (synchronous cascade
  stage) vs cached-slot (render-time disk-cache feed) — operator
  decision required, not auto-resolved
- New **Phase 4 placeholder**: flow/positioning substrate (short
  interest, options skew) — architect identified as more directly tied
  to swing horizon than fundamentals; deferred from v1 scope
- PR 2A.2 data-isolation principle: explicit framing in Stage 2 prompt
  that industry context is "raw substrate, NOT a recommendation"

---

## Why

### The substrate gap

Operator review on 2026-06-08 named the architectural weakness directly:
the cascade's Stage 2 thesis reads `TICKER_DATA` (RSI/ADX/MACD/EMAs/
returns/volume), `edgar:form4:aggregate` (insider activity),
`edgar:13f:aggregate` (now actually loading post-#24), `yfinance:fetch_news`
(headlines with absorption-stage labels), and `world_state` (regime,
dispersion, sector rotation). Quantitative technical, quantitative
behavioral, surface-level news. What it does **not** read:

- Revenue, gross margin, operating margin, FCF trends
- Balance sheet position (debt, cash, leverage)
- Forward earnings estimates / analyst revisions
- Valuation multiples vs sector peers
- Industry-cycle position, competitive landscape, customer/supplier
  concentration, structural risks beyond company-specific signals
- Earnings calendar + consensus expectations

Today's dossiers use phrases like *"in the AI infrastructure theme"* or
*"semiconductor recovery"* — but that's the model pattern-matching from
headlines, not analyzing fundamentals from financial statements. Stage 2
is doing qualitative *framing* without qualitative *analysis*.

### What this costs

The scoreboard's dedup fix (Phase 1.7, commit `3929123`) confirms that
top-decile conviction does NOT predict positive forward returns at 17%
bootstrap positive mass. Two competing hypotheses for that result:

1. **Substrate-thin**: cascade reasons fine but has too little data
2. **Structurally limited**: cascade can't reason about swing trades
   regardless of substrate richness

This spec is the experiment that discriminates between them. Build
the substrate layer cheaply, feature-flag it, measure whether
forward-return calibration improves. If yes, substrate is the lever
and we keep building. If no, deeper architectural work is needed and
substrate work stops.

### Why AI-native synthesis, not operator-curated

Initial draft of this spec proposed an operator-curated industry
context file per ticker (`.research/industry_context/<TICKER>.md`).
**That direction was rejected by the operator and rightly so**: the
entire pitch of an AI-driven research assistant is to do humanly-
unreasonable amounts of research in machine timeframes, with timely
digestion as core value. Asking the operator to hand-maintain industry
notes treats the LLM as a knowledge renderer over operator input —
it gives back the value-add. Industry context must be **AI-
synthesized from primary sources**, not human-curated.

### Why this respects the anchor-discipline contract

Every field in both layers below cites a stable source anchor. KPIs
anchor as `yfinance:financials:<period>` / `yfinance:balance_sheet:
<period>`. Industry context anchors as `edgar:10-K:<accession>:para_N`
— the same anchor type the existing EDGAR adapter already produces.
The Skeptic can verify both layers using the same verification path
it already uses for insider data and filing excerpts. **No new
anchor type means no new Defender corpus integration needed.**

---

## Goals

1. **Layer 1 — Structured KPIs**: pull a fixed deterministic block of
   financial-statement fields (revenue trend, margins, FCF, leverage,
   valuation multiples vs sector) and inject as `{fundamentals_block}`
   in Stage 2 prompt.
2. **Layer 2 — AI-synthesized industry context**: synthesis surface
   (either synchronous Stage 1.5 cascade stage or async cached-slot
   feed — see Architectural Choice section for the operator decision)
   that reads 10-K Item 1 (Business) + Item 1A (Risk Factors) + most
   recent 10-Q MD&A, synthesizes a structured industry view, anchored
   back to source paragraphs with role-trigger verification. Cached
   per ticker; refreshed weekly or on new filing.
3. **Earnings calendar enrichment** (ships with Layer 1 as Phase A):
   next earnings date + consensus EPS via yfinance.calendar; added to
   TICKER_DATA so Stage 2 can see binary-event proximity.
4. **Feature-flag both layers** for empirical comparison before full
   rollout. The cascade's calibration outcome on the new substrate is
   the only gate that justifies keeping it.
5. **Preserve anchor discipline + Skeptic verifiability** — every
   field cites a primary source.

## Non-goals

- **Operator-curated industry notes** — rejected. AI must do the
  research, not the operator.
- **Paid data sources in v1** — no Tikr, AlphaSense, FactSet,
  Refinitiv. Earnings transcripts deferred to v1.x.
- **Web search in v1** — deferred until a vetted web-search tool
  integration exists. Stage 1.5 stays grounded in EDGAR filings.
- **Touching the Skeptic stage** — orthogonal track. FOLLOWUPS #20
  stays on its own gate. The Skeptic *will* eventually call
  `get_industry_context(ticker)` as one of its optional tools when
  #20 re-opens, but that's downstream.
- **Sector-level industry reports** — per-ticker only. Sector-level
  qualitative synthesis is a different surface.

---

## Architecture

### Architectural Choice — Stage 1.5 vs cached-slot (operator decision required)

Before either layer is wired into Stage 2, one architectural shape
choice must be made for Layer 2 (industry context synthesis). The
architect review surfaced both options as defensible; this section
preserves both views and recommends one, but the choice has runtime
and operational consequences the operator should ratify rather than
auto-resolve.

#### Option A — Stage 1.5 (synchronous, new cascade stage)

Industry-context synthesis fires as a dedicated cascade stage between
Stage 1 (filter) and Stage 2 (thesis). Its own prompt template, model
selection, and trace event. Cold tickers pay LLM-call latency on the
cascade's critical path.

- **Pros:** cascade-level trace event for free; synchronous freshness
  guarantee; matches the "new layer = new stage" mental model that
  Stage 0 / 1 / 2 / 3 / Defender already use; explicit auditability
  per cascade run.
- **Cons:** ~$0.02-0.05 + ~5-15s per cold ticker on critical path;
  Stage 0 universe blowup (30+ tickers) costs ~30-60s of EDGAR-rate-
  limited (5 req/s) sequenced calls; new cascade stage to maintain
  alongside the existing five; couples LLM call to cascade run when
  the data refresh cadence (weekly) is much slower than cascade
  cadence (daily).

#### Option B — Cached-slot (asynchronous, render-time disk-cache feed)

Industry-context synthesis runs out-of-band (operator cron + on-new-
filing webhook + manual `/refresh-industry <TICKER>`). The cascade
only ever *reads* `.research/industry_context/<TICKER>.json` at Stage
2 render time and renders the `{industry_context_block}` slot from
cache. Cache misses degrade to `(industry context unavailable —
refresh queued)`, the same shape `InsiderActivitySummary` /
`InstitutionalOwnership` already use when their data sources are
absent.

- **Pros:** zero critical-path latency; fits the existing substrate-
  rendering classmethod pattern (`render_for_prompt()` on dataclasses
  fed pre-loaded data — see `orchestrator.py:312-314` for the
  established design principle); trivial graceful degrade; decouples
  refresh cadence (weekly) from cascade cadence (daily); no new
  cascade stage to maintain.
- **Cons:** async refresh infrastructure required (cron job + filing-
  detection trigger); staleness window can grow if refresh fails
  silently; cache-write trace event needs explicit plumbing rather
  than coming for free.

#### Lead-engineer recommendation: Option B (cached-slot)

Three reasons:

1. **The existing substrate pattern is `render_for_prompt()`
   classmethod feeds.** Stage 1.5 would be the only stage that's "I
   produce a slot, not a verdict" — an awkward fit in the cascade
   taxonomy. Per `orchestrator.py:312-314`, the established design
   principle is *"prompt-block rendering for each EDGAR data source
   is owned by the dataclass itself…keeps the 'what does Stage 2
   see when EDGAR is down?' decision in one place per source."*
   Industry context is more data source than cascade stage.
2. **Refresh cadence ≠ cascade cadence.** Industry context changes
   on quarterly filings, not daily news. Coupling its LLM-cost
   payment to every cascade run is a category error — like firing
   the 13F adapter daily even though 13F filings land 45 days
   after quarter end.
3. **Graceful degrade matches existing pattern.** Missing 13F shows
   `(no 13F coverage available)`; missing insider summary shows
   `(no Form 4 last 90d)`. Missing industry context can show
   `(industry context unavailable — refresh queued)` and Stage 2
   writes a thesis without it, the same way it writes a thesis
   without 13F today.

#### What the operator should weigh

- If "every cascade run has fresh-synthesized industry context" is a
  hard operational requirement, Option A is the right call despite
  its latency cost.
- If "industry context can be 0-7 days stale and that's fine, but
  every cascade run must complete in under N seconds" is the
  operational reality, Option B.

Spec's defaults below assume **Option B**. If the operator chooses
Option A, sections marked **[Option B-specific]** flip to Stage 1.5
shape (own prompt + own model + own trace event; cascade-stage
implementation).

---

### Two layers, complementary

| | Layer 1 — KPIs | Layer 2 — Industry context |
|---|---|---|
| Mechanism | Deterministic extraction | LLM synthesis |
| Source | yfinance.financials / balance_sheet / cashflow | EDGAR 10-K + 10-Q corpus |
| Cadence | Refreshed each cascade run | Cached per ticker, weekly refresh |
| Cost | Free (yfinance) | One Sonnet call per refresh |
| Anchor format | `yfinance:financials:<period>` | `edgar:10-K:<accession>:para_N` |
| Prompt slot | `{fundamentals_block}` | `{industry_context_block}` |
| Failure mode | Sparse data → block degrades to "(data unavailable)" | Filing absent or stale → block degrades to last cached version + age tag |

### Layer 1 — Structured KPIs

#### Fields

```
revenue_ttm_usd               # TTM revenue
revenue_yoy_growth_pct        # YoY growth on TTM
gross_margin_ttm_pct          # TTM gross margin
gross_margin_trend            # "expanding" | "stable" | "compressing"
                              # (last-4-Q slope, classified)
operating_margin_ttm_pct      # TTM operating margin
fcf_margin_ttm_pct            # FCF / revenue
debt_to_equity                # Latest balance sheet
cash_and_equiv_usd            # Latest balance sheet
pe_ttm                        # Trailing P/E
ev_to_ebitda                  # Latest enterprise value / TTM EBITDA
ps_ttm                        # Trailing P/S
peer_set                      # Optional: ["TICKER", ...] from yfinance.recommendations
                              # or from sector ETF holdings; null if unresolvable
valuation_vs_sector           # "premium" | "in-line" | "discount" relative to
                              # the sector ETF's median multiples; null if unresolvable
```

#### Anchor format

```
yfinance:financials:income_statement:2026-Q1
yfinance:financials:balance_sheet:2026-Q1
yfinance:financials:cashflow:2026-Q1
yfinance:quote:current  (for market-cap-derived multiples)
```

All deterministic; no LLM involved in the extraction. Same shape as
the existing `insider_summary` / `institutional_ownership` blocks.

### Layer 2 — AI-synthesized industry context

#### Mechanism (depends on Option A vs B — see Architectural Choice above)

- **Option B (recommended, default below)**: synthesis runs out-of-
  band via `refresh_industry_context(ticker)`. Cron + filing-detection
  + manual `/refresh-industry` populate the cache. Cascade reads cache
  at Stage 2 render time via `IndustryContext.render_for_prompt()`.
  Auditability: cache-write produces its own trace event with the
  same `chain_id`-like discriminator (here, a `refresh_id`) that
  cascade-stage events carry today.
- **Option A**: `stage_1_5_industry_context` runs between Stage 1
  (filter) and Stage 2 (thesis) — own prompt template, own model
  selection, own trace event. Synchronous; cold tickers pay LLM
  latency on cascade critical path.

The rest of this Layer 2 spec (inputs, schema, verification, caching,
cost) is shape-identical under both options. The only differences are
*when* the synthesis call fires (sync vs async) and *who* triggers
it (cascade vs cron+events).

#### Inputs

For each ticker the cascade is processing:
1. 10-K Item 1 (Business) — latest annual filing
2. 10-K Item 1A (Risk Factors) — latest annual filing
3. 10-Q most recent MD&A — most recent quarterly filing

Section extraction is done by the EDGAR adapter (need a section-aware
extractor on top of the existing paragraph extractor — heading-based
splitter for "Item 1.", "Item 1A.", "Management's Discussion").

#### Prompt + output schema (revised v2 — role-trigger verification)

Industry-context synthesis prompt instructs the model to synthesize
a strict-JSON output. Field names reflect **management-self-
disclosure provenance** (architect rec #5) — every role-bearing
entry also carries `role_evidence_substring`, the phrase in the
cited paragraph that establishes the relational claim. Verification
upgrades from "entity-token in paragraph" to "entity-token AND
role-trigger phrase BOTH in cited paragraph."

```json
{
  "industry": "<specific industry — more granular than ETF, e.g. 'NAND flash memory' not 'semiconductors'>",
  "industry_anchor": "edgar:10-K:<accession>:para_N",
  "cycle_position": "<early | mid | late | structural-decline | structural-growth | unclassifiable>",
  "cycle_position_anchor": "edgar:10-K:<accession>:para_N",
  "management_described_customers": [
    {
      "name": "<customer>",
      "anchor": "edgar:10-K:<accession>:para_N",
      "role_evidence_substring": "<substring of the cited paragraph that establishes the customer relationship, e.g. 'we sell to' or 'our principal customers include'>"
    }
  ],
  "management_described_competitors": [
    {
      "name": "<competitor>",
      "anchor": "edgar:10-K:<accession>:para_N",
      "role_evidence_substring": "<substring establishing the competitive relationship, e.g. 'we compete with' or 'our primary competitors are'>"
    }
  ],
  "structural_risks": [
    {
      "risk": "<industry-level risk, NOT company-specific>",
      "anchor": "edgar:10-K:<accession>:para_N",
      "role_evidence_substring": "<substring framing the risk at the industry level vs the company level>"
    }
  ],
  "active_industry_catalysts": [
    {
      "catalyst": "<forward event affecting the industry>",
      "anchor": "edgar:10-Q:<accession>:para_N",
      "role_evidence_substring": "<substring establishing the catalyst's forward-looking character>"
    }
  ],
  "comparable_companies": ["<ticker>", "<ticker>"]
}
```

**Why provenance-named fields matter:** management's 10-K Item 1 lists
the *competitors management chooses to acknowledge* — often selected
to flatter the company's positioning. Naming the field
`management_described_competitors` rather than `top_competitors`
keeps Stage 2 honest: this is the *company's self-reported view of
its competitive set*, not an independent assessment. Stage 2 prompt
instructions will explicitly call out the provenance ("this block is
management's self-disclosure of competitive position from filings;
weigh it as such, not as industry-truth").

**Verification path:** for each entry, the verifier confirms BOTH:
1. The cited entity's name (substring match) appears in the
   paragraph at `anchor` or the 2 adjacent paragraphs.
2. The `role_evidence_substring` appears in the paragraph at `anchor`.

This catches the dominant failure mode the bare-substring check
misses: model emits `"Samsung as a top competitor for MU"` citing a
paragraph where Samsung is mentioned as a *supplier*. Samsung token
match passes (#1); the substring "we compete with" or equivalent
won't be in that paragraph (#2 fails). Verification rejects.

Verification failures retry the synthesis once with a tightened
prompt; second failure surfaces as `(industry context unavailable —
verification failure)` and Stage 2 writes a thesis without the
block.

#### Caching

- Cache file: `.research/industry_context/<TICKER>.json`
- Refresh triggers:
  1. Cache age > 7 days OR
  2. A new 10-K or 10-Q filing detected since cache was written OR
  3. Operator runs `/refresh-industry <TICKER>` explicitly
- Schema-versioned (`schema_version: 1`). Future schema additions are
  additive only (consistent with existing `Stage2Note` contract).

#### Cost shape

- 10-K Item 1 + 1A typically ~5-15K tokens
- 10-Q MD&A typically ~3-8K tokens
- Synthesis prompt + completion: ~$0.02-0.05 per ticker per refresh
- 13-ticker watchlist refreshed weekly: ~$0.30-0.65/week
- Within all operator cost ceilings
- **Option A (synchronous Stage 1.5):** additionally pays ~5-15s
  cascade-critical-path latency per cold ticker; Stage 0 universe
  blowup (e.g., 30 new tickers in a stress regime) adds
  ~30-60s sequenced LLM call latency to that day's cascade.
- **Option B (async cached-slot):** zero cascade-critical-path latency.
  Refresh job is bounded by SEC's 5 req/s throttle on EDGAR fetches
  but happens out-of-band; cache misses surface as graceful degrade,
  not as latency drag.

### Stage 2 integration

`stage_2_thesis.txt` gains two new prompt slots:

```
{fundamentals_block}
{industry_context_block}
```

Both populated by `orchestrator._stage_2_thesis` after parallel fetch
of:
- Existing: `load_ticker_data`, `load_headlines`, `load_insider_activity`,
  `load_institutional_ownership`
- New: `load_fundamentals`, `load_industry_context`

Source-rule list extended in `stage_2_thesis.txt` to include:
- `yfinance:financials:*`
- `yfinance:balance_sheet:*`
- `yfinance:cashflow:*`
- `yfinance:calendar:*`
- (no new `edgar:*` types — industry context reuses existing
  `edgar:10-K:...:para_N` shape)

---

## What ships (phased — restructured per architect rec #4)

### Phase A — Deterministic substrate (KPIs + earnings calendar — single PR)

Merges what v1 had as Phase 1 + Phase 3. Both are deterministic,
both use the existing yfinance adapter, neither involves an LLM call.
Shipping together saves a context-switch and gives the measurement
arm a single, cohesive substrate-enrichment variant to compare
against control.

- New module `research_assistant/fundamentals/kpis.py`:
  - `KPISummary` dataclass with all KPI fields
  - `EarningsCalendar` dataclass with `next_earnings_date`,
    `days_to_next_earnings`, `consensus_eps_estimate`
  - `load_fundamentals(ticker, *, adapter=)` — wraps
    `yfinance.financials` / `.balance_sheet` / `.cashflow`
  - `load_earnings_calendar(ticker, *, adapter=)` — wraps
    `yfinance.calendar` / `.recommendations`
  - `kpis.render_for_prompt()` and `earnings.render_for_prompt()`
    producing the respective Stage 2 prompt blocks
- Source-rule list extended in `stage_2_thesis.txt`:
  `yfinance:financials:*`, `yfinance:balance_sheet:*`,
  `yfinance:cashflow:*`, `yfinance:calendar:*`,
  `yfinance:recommendations:*`
- Orchestrator wiring: `load_fundamentals` + `load_earnings_calendar`
  run in `asyncio.gather` alongside existing per-ticker loads
- New Stage 2 prompt slots: `{fundamentals_block}`,
  `{earnings_calendar_block}`
- Feature flag: `STAGE_2_DETERMINISTIC_SUBSTRATE=on|off`
- Tests:
  - Synthetic financials + calendar → expected blocks
  - Missing data → graceful "(KPI data unavailable)" / "(no calendar)"
  - Stale financials (>6mo old) → `_data_quality: stale` tag
  - Anchor format stability tests
  - End-to-end: Stage 2 prompt renders both blocks correctly

Estimated lift: ~4-5 days combined (was 3+1 days separately).

### Phase B — AI-synthesized industry context (gated on Phase B's OWN measurement vs control, NOT Phase A's)

**Architect rec #3 — gate decoupling**: Phase B's empirical justification
is whether industry-context synthesis moves calibration *on its own*,
measured against the same control (no-substrate-enrichment) baseline,
not against Phase A as baseline. Each substrate hypothesis tested on
its own merits.

- New synthesis surface:
  - **[Option B / cached-slot — recommended]**: new module
    `research_assistant/fundamentals/industry_context.py` with
    `IndustryContext` dataclass + `render_for_prompt()` classmethod;
    `refresh_industry_context(ticker)` async function that performs
    the LLM call and writes `.research/industry_context/<TICKER>.json`;
    background refresh script in `scripts/refresh_industry_context.py`
    runs on cron + filing-detection trigger
  - **[Option A / Stage 1.5 — if operator chooses]**: new cascade stage
    `stage_1_5_industry_context` in `orchestrator.py`; new prompt
    template `research_assistant/prompts/stage_1_5_industry_context.txt`;
    new orchestrator function `_stage_1_5_industry_context`; cache
    layer same as Option B but written synchronously from cascade
- New prompt template `prompts/industry_context_synthesizer.txt`
  (shared between Options A and B)
- EDGAR section-aware extractor in `research_assistant/edgar/sections.py`:
  - Heading-based splitter for "Item 1.", "Item 1A.",
    "Management's Discussion and Analysis"
  - Reuses existing `FilingText.search` for paragraph-level anchors
- Verification: `industry_context.verify(synthesis, source_corpus)` —
  enforces entity-token AND `role_evidence_substring` BOTH in cited
  paragraph (architect rec #2). On verification failure, retry once
  with tightened prompt; second failure → degraded slot.
- New CLI subcommand: `/refresh-industry <TICKER>` for forced refresh
- New Stage 2 prompt slot: `{industry_context_block}` (with explicit
  framing: "raw substrate of management's self-disclosed industry
  position, NOT a recommendation — weigh as one perspective alongside
  technical + insider + flow signals")
- Feature flag: `STAGE_2_INDUSTRY_CONTEXT=on|off`
- Tests:
  - Synthesis prompt + fixture → expected JSON schema
  - Anchor verification: entity-token + role-trigger both present
  - Verification failure → retry path
  - Verification failure (2x) → graceful degrade
  - Cache hit / miss / refresh behavior
  - Schema-drift detection (additive-only contract)
  - Stage 2 prompt renders `{industry_context_block}` with the
    "raw substrate, NOT recommendation" framing visible

Estimated lift: ~1.5-2.5 weeks. Larger range than v1 because Option
A vs Option B has different infrastructure costs; cached-slot adds
the cron/webhook layer while Stage 1.5 adds the cascade-stage
plumbing.

### Phase C — Flow / positioning substrate [PLACEHOLDER, deferred from v1 scope]

**Architect rec #6 — substrate axis the spec missed.** Short
interest, days-to-cover, options skew (put/call premium ratio,
term structure). yfinance has `info["sharesShort"]` and
`info["shortPercentOfFloat"]`; options chain provides put/call
volume + open interest. For a swing-trading mission (days-to-
weeks), flow/positioning signals are more directly tied to
forward returns than 10-K industry context — the architect's
observation is well-supported in trading literature.

Scope: deferred to a separate spec, NOT v1. Captured here so the
substrate map is honest about what we're not doing. Likely the
next spec after Phase A + Phase B measurement lands.

Estimated lift (when scoped): ~3-5 days deterministic substrate,
single PR.

---

## Risks & mitigations

### Hallucination in industry synthesis (revised v2 — role-trigger verification)

Risk: the synthesis model invents competitor or customer names not in
the source corpus, OR — the more subtle and dominant failure mode —
the model picks a true entity name from the source but assigns it the
wrong relational role. 10-Ks are dense with cross-references: a
named company can appear as competitor in one paragraph, supplier in
another, customer in a third. Substring-only verification (which
v1 of this spec proposed) checks token-presence and is **a category
error for relational claims**.

Concrete failure case bare-substring verification misses:

> Model emits `"management_described_competitors": [{"name": "Samsung",
> "anchor": "edgar:10-K:0001234567-26-...:para_412"}]`. MU's 10-K
> Item 1A para 412 actually says *"…we depend on Samsung Display for
> certain LCD modules in our SSD product line…"* — Samsung is in
> the paragraph, just not as a competitor. Bare substring matches
> "Samsung". Verification passes. Skeptic now sees "Samsung is a
> competitor for MU" as substrate-grade fact.

Mitigation (v2): every role-bearing field schema carries an explicit
`role_evidence_substring`. Verification confirms BOTH:
1. The cited entity's name (substring) appears in the paragraph at
   `anchor` or the 2 adjacent paragraphs.
2. The `role_evidence_substring` (e.g., "we compete with", "our
   primary competitors", "we sell to") appears in the paragraph at
   `anchor`.

This closes the dominant failure mode: in the Samsung-as-competitor
example, the model would have to produce a `role_evidence_substring`
like "we compete with Samsung" and verify it against para 412 — which
doesn't contain that phrase. Verification rejects.

Remaining failure modes the role-trigger upgrade doesn't catch:
- Genuinely ambiguous paragraphs that contain both "compete with" and
  "supply us" near the entity. Low-frequency in real filings.
- Adjacent-paragraph splits where the role-trigger phrase is one
  paragraph earlier and the entity is one paragraph later.
  Mitigation: verification checks the cited paragraph + 2 adjacent
  paragraphs for BOTH conditions.

Synthesis outputs that fail verification retry once with a tightened
prompt. Second failure surfaces as `(industry context unavailable —
verification failure)` and Stage 2 writes a thesis without the block,
the same graceful-degrade path used for missing 13F or insider data.

### Stale industry context

Risk: 10-K is up to 12 months old by the next annual filing; industry
landscape may have shifted materially in that window (e.g. HBM
supply dynamics on a 6-month cycle).

Mitigation: cache carries the filing date in its anchor; Stage 2
prompt is instructed to weight the industry context against the
fresher per-ticker news + recent-quarter MD&A. The 10-Q MD&A in the
input set provides the most-recent-quarter perspective and is
refreshed up to 4× per year. Operators can force refresh via
`/refresh-industry`.

### Cost runaway from synthesis stage (Option A only)

Risk (Option A): cache misses on a large discovered universe
(Stage 0 surfaces 30+ tickers daily in stress regimes) could spike
LLM cost AND latency, sequenced on the cascade critical path. The
EDGAR adapter throttles at 5 req/s, so 30 cold tickers × 3 filings
each = ~18 seconds of EDGAR I/O alone, plus ~5-15s per LLM
synthesis call sequenced.

Mitigation (Option A): synthesis fires only for tickers that survive
Stage 1 filtering (watchlist + screener-confirmed candidates only).
Cache amortises across all subsequent runs. Per-cascade cost ceiling
shipped if needed (FOLLOWUPS #15-related, separate concern).

**Under Option B this risk does not apply** — cache misses on the
cascade critical path surface as `(industry context unavailable —
refresh queued)` and the cascade continues. The refresh job is
out-of-band and bounded by the SEC throttle but never blocks a
cascade run.

This is the strongest practical argument for Option B (cached-slot)
over Option A (synchronous Stage 1.5). Operator's choice of A vs B
implicitly chooses the cost/latency profile here.

### Substrate doesn't move calibration (revised v2 — decoupled per-phase gates)

Risk: after shipping Phase A or Phase B, the scoreboard still shows
no positive forward-return signal at top conviction.

Mitigation: each substrate hypothesis measured independently against
the same control (no-substrate-enrichment baseline). **Phase B's gate
is NOT conditional on Phase A succeeding** (architect rec #3) — the
two layers test different substrate hypotheses (quantitative
fundamentals vs qualitative management-described industry context)
and their measurement should be uncoupled.

Two parallel feature flags during the measurement window:
- `STAGE_2_DETERMINISTIC_SUBSTRATE=on|off` for Phase A
- `STAGE_2_INDUSTRY_CONTEXT=on|off` for Phase B

Three arms run in parallel during the measurement window:
- **Control**: both flags off (today's substrate)
- **Phase A arm**: deterministic substrate only
- **Phase B arm**: industry context only

Each cascade run assigned to one arm at random (or deterministically
by `chain_id` hash mod 3 to ensure replay determinism).

Decision tree per arm (locked in BEFORE shipping):
- Arm beats control by ≥1.5% absolute median forward-return
  improvement AND bootstrap CI on the difference excludes zero
  → ship that arm.
- Arm equal to control → that substrate is not the lever; investigate
  cascade-level issues before shipping the arm.
- Arm *worse* than control → that substrate is adding noise;
  investigate whether the model is over-weighting the new fields,
  revise prompt before shipping.

**Cross-arm interaction**: if BOTH arms beat control, run a fourth
arm (`STAGE_2_DETERMINISTIC_SUBSTRATE=on STAGE_2_INDUSTRY_CONTEXT=on`)
for another window to verify the effects compose. If they don't,
ship only the larger-effect arm.

This gate prevents the FOLLOWUPS #20 / #26 pattern (building the
next layer before measuring whether the previous one helped) AND
prevents the Phase 1-first-then-gate pattern that would smuggle in
the more important hypothesis (qualitative industry context) under
the success of the less important one (deterministic KPIs).

### Schema-drift in industry context cache

Risk: future schema changes break cached entries; reloading produces
errors.

Mitigation: `schema_version` field on every cached object. Additive-
only schema changes (per existing `Stage2Note` contract). On
unsupported schema version, fall back to forced refresh rather than
raising.

### Anchor verification false positives (revised v2)

Risk: legitimate synthesis fails verification because (a) the cited
paragraph is one of N adjacent paragraphs but the model picked the
wrong index, OR (b) the role-trigger phrase is implicit rather than
explicit (e.g., paragraph lists a comp set under a heading "Our
Principal Competitors" but no in-paragraph "we compete with"
phrasing).

Mitigation:
- **Adjacent-window for entity-token check**: paragraph at `anchor`
  PLUS the 2 paragraphs before and after.
- **Adjacent-window for role-trigger phrase**: same — role-trigger
  substring can land in any of the same 5 paragraphs.
- **Heading-aware verification**: if the cited paragraph or one of
  its adjacencies has a heading line like "Competitors" /
  "Customers" / "Risk Factors", the heading itself counts as the
  role-trigger evidence even if no inline phrase ("we compete with")
  appears.
- **Verification failures retried once** with tightened prompt
  (model is told the previous attempt's specific entity + cited
  paragraph couldn't be verified); second failure → graceful degrade
  to `(industry context unavailable — verification failure)`.

The conservative direction: false-positive (legitimate synthesis
rejected) is preferred to false-negative (hallucinated relational
claim shipped to Stage 2 substrate). Operator can review the
degraded-slot rate after 30 days; if it's >10% of attempts, the
verification heuristic is over-tight and we revise the heading and
adjacent-window rules.

---

## Migration / rollout (revised v2 — decoupled phase gates)

### Per-phase independence

- **Phase A** (deterministic substrate — KPIs + earnings calendar) ships
  as a single PR, smallest and cheapest. Feature-flagged on.
- **Phase B** (industry context synthesis) ships as a second PR after
  the Architectural Choice (A vs B) is resolved with operator
  approval. Feature-flagged on, gated on its OWN measurement vs
  control, NOT conditional on Phase A's measurement.
- **Phase C** (flow/positioning substrate) is out of v1 scope —
  separate spec when prioritised.

Each phase has its own falsifiability gate run against the SAME
control (no-substrate-enrichment baseline). Phase B is not
sequenced behind Phase A; the two test independent substrate
hypotheses (quantitative fundamentals vs qualitative management-
self-disclosed industry context).

### Feature flags (revised v2)

- `STAGE_2_DETERMINISTIC_SUBSTRATE=on|off` for Phase A — flips both
  `{fundamentals_block}` and `{earnings_calendar_block}` slot
  population on/off. Stage 2 prompt template stays the same; empty
  slots render as nothing.
- `STAGE_2_INDUSTRY_CONTEXT=on|off` for Phase B — flips the
  `{industry_context_block}` slot population on/off. Under Option B
  (cached-slot) the refresh job ignores this flag — it always writes
  cache; only Stage 2 slot population is flag-gated. Under Option A
  (Stage 1.5) the flag also gates whether the Stage 1.5 cascade stage
  fires at all.

### Three-arm randomized assignment

During measurement, each cascade run is assigned to one arm
deterministically by `chain_id` hash mod 3:
- arm 0 → both flags off (control)
- arm 1 → Phase A on (deterministic substrate)
- arm 2 → Phase B on (industry context)

Mod-3 assignment preserves replay determinism: re-running the same
`chain_id` lands in the same arm, so the arm assignment doesn't
become an extra source of non-reproducibility.

**Note on small-arm effects**: a 60-day window × ~10 tickers/day = ~600
cascades, ~200 per arm. That's the per-arm N for the falsifiability
gate. If 200 turns out underpowered after the first 30 days of
measurement (CI half-widths wider than expected), extend the window
to 90 days; do not relax the threshold.

### Sunset criterion (revised v2 — decoupled per-phase gates)

Each phase has its own sunset, measured against the SAME control
arm (no-substrate-enrichment baseline), not against each other:
- **Phase A** (deterministic substrate — KPIs + earnings calendar):
  60-day variant-vs-control measurement, ≥1.5% absolute median
  forward-return improvement with bootstrap CI excluding zero.
  Either result (ship or pause) is informative.
- **Phase B** (industry context synthesis): 60-day variant-vs-control
  measurement, same threshold. NOT conditional on Phase A's outcome.
- **Phase C** (flow/positioning substrate): out of v1 scope; sunset
  defined when its own spec is drafted.

---

## Open questions (for critic to attack — v2 set)

v1 Open Q #1 (Stage 1.5 vs cached-slot) has been promoted to the
new **Architectural Choice** section at the top of the Architecture
section, with explicit recommendation and operator-decision request.

1. **Section-aware EDGAR extractor**: 10-K Item 1 is mostly clean,
   Item 1A is heterogeneous (nested headings, bullet lists). What's
   the minimum-viable heading detector? Hand-written regexes or a
   markdown-tree parser? Architect noted existing `_extract_paragraphs`
   at `client.py:294-315` is generic, not section-aware — section
   splitter is genuinely new code, not a small extension.
2. **Peer set derivation**: where does `peer_set` come from in Phase
   A? Options: (a) hand-curated per sector ETF, (b) yfinance
   `.recommendations` keys, (c) ask the synthesis model to derive
   from the industry context output (but that creates Phase A →
   Phase B coupling). Default proposed: (a) for v1, (b) for v1.x.
3. **Sector ETF as the median benchmark**: most tickers' sector ETF
   is `XLK` / `XLF` / etc., but small-cap or niche tickers don't
   have clean sector mapping. Default proposed: when sector
   unresolvable, peer-set-based median; when peer set unresolvable
   too, emit `valuation_vs_sector: null` and let Stage 2 reason
   without it.
4. **Earnings transcripts (Phase B+ extension, deferred)**: Tikr /
   AlphaSense are paid; Seeking Alpha + Motley Fool transcripts are
   scraped (with ToS risk). What's the right path? Spec defers
   entirely; flags as future work.
5. **Caching scope for industry context**: per-ticker file is the
   v1 default. Could be per-`(ticker, filing-date)` for stronger
   replay determinism. Defer to v1.x — file-level cache survives
   most use cases.
6. **Cross-arm interaction measurement**: spec's Phase B gate says
   "if both arms beat control, run a fourth combined arm" — but
   the spec hasn't specified how long the combined-arm measurement
   runs, or whether it's gated on the same 1.5% threshold. Deferred
   to "when we actually need to run it" — pre-committing too far
   ahead invites optimizing the gate rather than the cascade.
7. **PR 2A.2 data-isolation violation magnitude**: architect flagged
   that industry-context block could let the model anchor on the
   synthesis instead of doing its own read (same risk PR 2A.2 closed
   for Stage 1 scores leaking into Stage 2). Spec's mitigation is
   "Stage 2 prompt frames the block as raw substrate, NOT a
   recommendation." Is prompt framing enough, or does this require
   structural mitigation (e.g., zero-out fields the synthesis
   model would have used to imply a verdict)? Probably enough for
   v1; revisit if measurement shows anchoring drift.

---

## Falsifiability gate (revised v2 — addresses Type II risk)

Two parallel hypotheses, each tested independently against the same
control:

- **H0_A (null)**: deterministic substrate doesn't move calibration.
- **H1_A (alt)**: deterministic substrate moves calibration. Median
  forward return at top decile improves by ≥1.5% absolute,
  bootstrap CI on the difference excludes zero.
- **H0_B (null)**: industry context doesn't move calibration.
- **H1_B (alt)**: industry context moves calibration. Same thresholds.

### Sample size + Type II risk (architect rec #7)

Today's dedup'd scoreboard shows top-decile median = -6.09% with N=4
per decile and per-ticker 10d-return std on the order of 8-12%.

At those parameters, the bootstrap CI half-width on a 3-arm randomized
comparison is approximately ±1.5-2.5% per pairwise difference. Setting
the gate at ≥2% absolute (v1) AND requiring CI exclusion of zero
creates serious Type II risk: a real +1-1.5% effect gets killed by
the gate.

v2 widens the experiment to address this:
- **Threshold lowered from ≥2% to ≥1.5%** absolute median forward-
  return improvement.
- **Window extended from 30 to 60 days** — doubles per-arm N to
  ~600 observations, tightens CI half-width by ~30%.
- **Bootstrap CI exclusion of zero** stays as the secondary check
  (not the primary).
- **Pre-commitment**: at 60 days, if the point estimate is positive
  but CI marginally includes zero (e.g., CI = [-0.3%, +3.0%]), extend
  by 30 days rather than auto-killing. Document the extension in
  the trace.

### What the decision actually buys

The gate is testing whether richer substrate is the lever for
calibration, not whether the cascade is "good." Two outcomes
are both informative:
- **Arm beats control**: substrate matters; ship and iterate.
- **Arm matches control**: substrate isn't the lever; the cascade's
  reasoning architecture is the upstream issue and substrate work
  should pause until that's addressed.

Either result is valuable. The trap (FOLLOWUPS #19 / #20 lesson) is
shipping the next architectural layer without measuring whether the
previous one mattered. Pre-commitment to *both* directions of the
gate (ship-if-positive, pause-if-flat) is what makes the experiment
honest.

### What this gate does NOT measure

- Whether the cascade should exist at all (architectural-level
  question — orthogonal)
- Whether trajectory analytics (#26 downgraded) would credit MRVL-
  shape adaptive behavior even when point-in-time deciles look flat
- Whether the Skeptic stage hybrid rewrite (#20 blocked) is needed
  (separate substrate concern — Skeptic verification surface, not
  Stage 2 reasoning surface)

Future spec follow-ups gated on this gate's outcome will name what
THEY are measuring; this gate is only about the substrate-layer
hypothesis.

---

## Cross-references

- FOLLOWUPS #1 (EDGAR foundation — Layer 2's substrate)
- FOLLOWUPS #19 (`/scoreboard` — calibration tool for the
  falsifiability gate; dedup fix shipped 2026-06-08)
- FOLLOWUPS #20 (Skeptic hybrid — blocked, industry context tool
  slots into its optional layer when #20 reopens)
- FOLLOWUPS #26 (trajectory mode — orthogonal measurement surface)
- Operator pushback 2026-06-08 that rejected operator-curated
  industry notes
- PR 2A.x lineage (anchor discipline + bias-defense backbone — both
  preserved here)
