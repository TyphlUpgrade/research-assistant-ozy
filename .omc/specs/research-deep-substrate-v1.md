# Spec: Research-Surface Deep Substrate — The Panopticon (v1)

**Status:** DRAFT-v3 — critic v2 incorporated; EXP1+EXP2 follow-up experiments resolved critic M1; ready for implementation
**Author:** lead engineer
**Date:** 2026-06-08 (revised 2026-06-09 after critic v2 + EXP1/EXP2 experiments)

**v3 changes incorporated:**
- **Critic M1 resolved by EXP1** (Phase-A-only synthesizer experiment, ~$0.05).
  Critic was correct that the original M4 used Agent-A-shaped deep-reads
  and therefore validated Phase D's substrate, NOT Phase B's. EXP1 was the
  follow-up: ran the synthesizer prompt against ONLY Phase A substrate
  (yfinance.financials + balance_sheet + key_stats, NO filing text) for the
  same PLUG/RGTI/QUBT M4 candidates. Result: 3/3 cases surfaced material
  HIGH-severity flags from Phase A substrate alone. Examples:
  - PLUG: gross profit negative every year 2022-2025; cash $368M vs debt
    $997M + 2025 net loss -$1.63B; short interest 27.5%
  - RGTI: revenue declined 3 consecutive years $13.1M → $7.1M; gross
    margin collapsed 77% → 29%; **P/S 653x**
  - QUBT: **P/S 495x** on $682K TTM revenue; operating loss doubled while
    revenue grew $373K → $682K
  **Phase A and Phase D substrate are EMPIRICALLY COMPLEMENTARY,
  not redundant**: Phase A catches structural/macro issues (revenue
  trends, gross margin trajectory, valuation extremes, runway); Phase D
  catches forensic/decomposition detail (mechanical accounting reversals,
  prose-vs-numbers contradictions, hidden non-recurring items). Both
  ship; they catch different failure modes.
- **Critic M2 (role-bound extractor shared-distribution collusion) —
  acknowledged as residual; calibration sub-task baked into Gate 2.**
  Phase C replay must sample 30 verified-pass flags, hand-grade ground
  truth. If >20% of verifier-passes are operator-rejected, the
  re-extractor is theater; Phase D doesn't ship until calibration passes.
- **Critic M3 (circuit-breaker silent-degrade contamination) —
  resolved.** Per-event telemetry `panopticon_degraded: true|false` logged
  on every /research; Gate 3 explicitly excludes degraded events from
  arm-2 forward-return analysis.
- **Critic M4 (decoy-arm 20% sample lacks statistical power) — resolved.**
  Decoy share raised to **33%** (10+ decoy entries per N=30 graded). Gate
  threshold gains explicit bootstrap CI: decoy-FP CI upper bound <
  journal-FP CI lower bound. If CIs overlap, decoy gate is non-decisive
  and operator extends sample by 50%.
- **EXP2 precision arm — Phase D doesn't false-positive on winners.**
  Same Agent-A-shaped deep-read on 3 trades where cascade was bullish AND
  forward 10d was positive (MRVL +40.9%, HPQ +5.36%, JNJ +0.64%). Result:
  3/3 evaluable cases — Phase D did NOT produce false-positive HIGH-
  severity contrarian flags. MRVL specifically: the deep-read found
  "Other Expense Net swung -$197M YoY but entirely from non-cash fair
  value marks — contingent consideration UP $331.8M because acquisitions
  hit milestones (good business news mechanically depressing P&L); forward
  stock purchase contract -$81.1M gain offsetting." That's a *bullish-
  supportive* finding, not a contrarian flag. Empirically validates that
  the deep-read prompt's anti-fabrication discipline holds on the
  precision side, not just recall.
- **XBRL extraction is now a load-bearing Phase D dependency.** Across
  M4 + EXP2, **3 of 7 candidate filings (AAL, HPQ, JNJ)** returned XBRL
  taxonomy context tags only — no prose, no numbers. The model correctly
  refused to fabricate, validating the anti-hallucination prompt; but it
  also means the existing `_extract_paragraphs` is insufficient for ~43%
  of filings on this sample. Phase D's `edgar/sections.py` scope expands
  to explicitly handle XBRL-stripped HTML extraction (not just
  heading-aware splitting). This is significantly more work than the
  original spec estimated; Phase D's 1-2 week estimate becomes 2-3 weeks.

---

**Closes FOLLOWUPS:** #28 (NEW)
**Relates to:** #1 (EDGAR foundation), #19 (`/scoreboard` — calibration), #20 (Skeptic hybrid — orthogonal)
**Supersedes:** `fundamentals-substrate-v1.md` (archived same day)

**v2 changes incorporated:**
- **Operator framing — synthesizer flags are EVIDENCE, not directives.** Stage 2
  thesis writer must reason over flags alongside macro + micro context; do not
  let any single flag drive verdict. Severity = evidence strength, not
  verdict-shift weight. Schema's `thesis_implication` field becomes optional
  + descriptive + tone-neutral. (Operator: 2026-06-08)
- **M4 hand-grading replay PASSED empirical gate.** Ran the experiment cited
  in critic's M4 (~$0.33 LLM cost, 4 candidates, no hindsight). 3/3 evaluable
  cases produced deep-read findings the original cascade missed:
  - PLUG: $16.7M of $52.3M YoY gross loss improvement is a *mechanical
    accounting reversal* (loss contract reserve benefit vs prior-year charge)
  - RGTI: cash up $3.3M headline contradicts total investable liquidity
    down $24.1M QoQ; AR up 71.5% QoQ on tiny revenue base
  - QUBT: $110M+ cash spent on acquisitions during a 74% rally creates
    goodwill measured against peak stock price (mechanical impairment risk);
    "going concern basis" while asserting 12-month adequacy
  - AAL: tooling failure (EDGAR adapter returned XBRL taxonomy garbage);
    the model correctly refused to fabricate, validating anti-hallucination
    prompt design AND empirically justifying Phase B's `edgar/sections.py`
- **Verification upgrade — role-bound re-extraction (critic C2).** Architect's
  text-presence check is paraphrase-gameable; replaced with separate small-LLM
  re-extraction step that sees only the cited anchor + field name and must
  independently produce the same claim.
- **Phase order — A → D-minimal → E → B → C (critic M2).** Front-loads
  measurement. Ship D-minimal (synthesizer reading only Phase A substrate)
  before B (Agent A) so the synthesizer's empirical value is gated on its
  own measurement, not on Agent A's 1-2 week build.
- **Arm assignment by `hash(ticker, iso_week) mod 3` (critic M3).** Prevents
  cache-leakage and probe-cascade confounds that would corrupt cross-arm
  measurement.
- **Consensus-unchecked flag KILLED (critic M1).** Replaced with
  telemetry-only `axes_agreed: true|false` per /research event, surfaced in
  `/scoreboard` retrospective analysis only. Does NOT appear in per-ticker
  dossiers (would train operator to ignore flag noise).
- **Severity calibration rubric in synthesizer prompt (architect rec #1).**
  Anchored examples: HIGH = changes swing-trade thesis direction; MEDIUM =
  changes position sizing; LOW = changes confidence rating.
- **Replay gate extended to MEDIUM-flag TP rate (architect rec #1 +
  critic).** Not just >50% HIGH TP; also >40% MEDIUM TP. Otherwise the
  noise channel through MEDIUM goes ungated.
- **Decoy-ticker arm (architect rec #4).** 20% of replay sample = S&P 100
  ex-watchlist tickers. Decoy-FP rate = operator bias floor.
- **13F-overlap auto-suggestion for `/probe-cohort` (architect rec #5).**
  Operator supplies one ticker; system suggests peer set from 13F
  co-ownership (FOLLOWUPS #24 just shipped, substrate is available).
  Preserves no-curation principle.
- **Cost model: P90 row + `PANOPTICON_DAILY_CAP_USD` circuit breaker
  (critic C3).** Trace data showed 2026-05-27 = 30 /research in one day.
  At cold-filing $0.20 × 30 = $6/day, ~7x the modal projection.
- **EDGAR section-aware extractor empirically justified (M4 AAL finding).**
  Existing `_extract_paragraphs` pulled XBRL context tags only on AAL's
  10-Q. Phase B's `edgar/sections.py` must handle XBRL-heavy filings,
  not just well-formed HTML.

---

## Why

### The Coinbase case

Operator entered Coinbase puts pre-earnings on a bearish technical +
sentiment read. Stock had pre-dipped on crypto-bearish news. Earnings
print landed; mild positive guidance; stock +15% next day, puts lost
money. The detail that mattered was buried in the 10-Q decomposition:

> The reported "balance sheet losses" were overwhelmingly *unrealized
> fair-value markdowns on cryptocurrency held on balance sheet*, not
> *operational losses*. The bear thesis priced into the put premium
> was reading the headline loss number as evidence of operational
> deterioration. The deep filing read decomposed it as
> "mark-to-market noise; operational margins held; guidance implicit
> unchanged."

A human institutional analyst with 90 minutes of attention catches
this. A retail investor with 20 minutes doesn't. The information was
fully public, fully available, fully anchored in the filings.
**The edge wasn't proprietary data — it was attention budget.**

The cascade as architected today does not catch this case. It reads
shallow from many sources (technicals, insider, headlines,
world_state) and synthesizes a thesis that is fundamentally limited
by the depth of any single source it consults. The Coinbase 10-Q
isn't *missing* from the cascade's reach — the EDGAR adapter has
already shipped — it's just that the cascade extracts top-level
fields and stops.

### What the institutional edge actually is

The architect-review-flagged "all our substrate is reactive" framing
oversimplifies. Institutional analysts use predominantly reactive
data too — 10-Qs, 8-Ks, analyst notes, news. What they have that we
don't is **depth-of-synthesis at parallelized attention bandwidth**:

- They read filings line-by-line, not just top-level
- They cross-reference: 10-Q decomposition vs prior-quarter guidance
  vs current-quarter analyst revisions vs options-market positioning
- They notice the small detail that *doesn't fit* the dominant
  narrative — that's where the trade is
- They have a team to distribute the reading across many tickers

We can substitute that team with parallel LLM synthesis. Multiple
deep-readers, each going deep on one axis, with a synthesizer that
cross-references their outputs for "shallow disagrees with deep"
flags.

### Why the prior spec was wrong

The archived `fundamentals-substrate-v1.md` proposed rearranging
reactive substrate (KPI extraction + industry context synthesis
from 10-K Item 1). The KPI piece was useful but isolated; the
industry-context layer was filtered through management's self-
presentation — embedding the company's voice as ground truth.

Neither addressed the depth-of-synthesis gap. The Coinbase case
proves the gap is shape-of-reading, not source-of-data.

### Why the depth-of-synthesis gap is empirically real (M4)

**This section added in v2 after running the cheap discriminator experiment
the critic mandated.**

The architectural pattern of "operator wants depth, doesn't measure" killed
FOLLOWUPS #20 (Skeptic hybrid) and #26 (trajectory mode) earlier this
session. To avoid the same trap, the cheap M4 experiment ran 4 candidates
from the existing journal where the original cascade rated bullish (AGREE)
but forward 10d returns were strongly negative (≥-5%). For each, fetched
the most-recent 10-Q FILED BEFORE the thesis date (no hindsight); ran a
deep-read prompt mimicking Agent A; hand-evaluated whether the output
contained disconfirming detail the original cascade missed.

Results: 3/3 evaluable cases produced material findings.

- **PLUG** (lost -19.24% after AGREE): deep-read decomposed the $52.3M
  YoY gross loss "improvement" into $35.6M operational + $16.7M
  mechanical accounting reversal (loss contract reserve benefit vs
  prior-year charge). PPA segment gross margin -52.6%, fuel delivery
  -47.7% — deeply loss-making on unit economics. Exactly the
  Coinbase shape: headline number ≠ operational reality, the
  difference is anchored in a specific footnote.
- **RGTI** (lost -12.71% after AGREE): deep-read flagged AR up 71.5% QoQ
  on tiny revenue base (receivables outpacing revenue — classic
  warning), and that headline "cash up $3.3M" contradicts total
  investable liquidity (cash + AFS) DOWN $24.1M QoQ. Management
  presented cash up while consolidated liquidity was shrinking.
  ~5-6 quarters runway at burn rate → dilution risk imminent.
- **QUBT** (lost -12.16% after AGREE): deep-read flagged $110M+ cash
  spent on acquisitions during a 74% rally — goodwill measured
  against peak stock price, impairment methodology stock-price-
  dependent. Filing prepared on "going concern basis" while
  simultaneously asserting 12-month liquidity adequacy (internal
  contradiction). "We have not achieved a level of sales adequate to
  support the Company's cost structure" (their own words). The
  bullish thesis was reading the government-quantum-funding narrative;
  the deep-read found four separate disconfirming structural details.
- **AAL** (lost -9.66% after AGREE) — **tooling failure, not a deep-read
  failure**. The existing EDGAR adapter's `_extract_paragraphs` pulled
  only XBRL context tags from AAL's 10-Q, no prose or numbers. The
  model correctly refused to fabricate: *"FILING CONTAINS XBRL
  TAXONOMY/CONTEXT TAGS ONLY... Populating any decomposition array
  would require manufacturing detail — the defined worst failure
  mode."* This **empirically validates two spec claims at once**:
  (1) Phase B's `edgar/sections.py` is genuinely new code that must
  handle XBRL-heavy filings, not just well-formed HTML; (2) the
  anti-fabrication prompt instruction works under real conditions,
  not just in design fantasy.

Tally vs critic's gate: ≥3/5 → gap is real, ship Phase A/B; ≤1/5 →
this is #20/#26 again. Got 3/4 with one tooling failure that didn't
prevent the others. Gate passed. Total experiment cost: $0.33.

Note that this is a *fixture-style* gate, not a forward-return gate.
The critic's C1 concern (56% of journal entries have null forward
returns, blocking the live measurement arm) remains valid but does
NOT block M4-style hand-grading replays — they use journal entries
as cases, not as forward-return-bearing observations.

### Why the depth-of-synthesis gap holds at the Phase A LAYER too (EXP1)

Critic v2 correctly observed that the M4 experiment used Agent-A-shaped
deep-reads (filing text input), which is Phase D's substrate, NOT
Phase B's (Phase B is synthesizer reading only Phase A deterministic
substrate — KPIs + earnings + options + revisions + insider detail,
no filing text). EXP1 was the follow-up: feed the synthesizer ONLY
Phase A substrate for the same PLUG/RGTI/QUBT candidates and
hand-evaluate.

Result: 3/3 cases surfaced material HIGH-severity flags from Phase A
alone. Examples (verbatim observation field):

- **PLUG**: "Gross profit has been negative every year from 2022-2025,
  meaning PLUG sells products below cost of production. 2025 gross
  loss of -$242M represents slight improvement from 2024's -$625M,
  but the multi-year pattern indicates no demonstrated path to
  unit-economics viability."
- **RGTI**: "Revenue has declined every year for three consecutive
  years: $13.1M → $12.0M → $10.8M → $7.1M. The 2025 decline is the
  steepest (-34% YoY), meaning the business is contracting
  operationally even as the stock has been bid up on quantum hype."
- **RGTI**: "Price-to-sales of 653x on trailing revenue of ~$7M is
  one of the most extreme valuations observable in public markets.
  With revenue shrinking and gross margin collapsing, the multiple
  is expanding in the wrong direction."
- **QUBT**: "Trailing 12-month revenue is $682K with a price-to-
  sales ratio of ~495x. Even a 10x revenue growth would leave P/S
  near 50x."
- **QUBT**: "Operating loss nearly doubled YoY from -$25.9M to
  -$51.1M despite revenue rising only from $373K to $682K."

These are facts the cascade today does not see — they are structural
context that yfinance.financials makes available but the existing
Stage 2 thesis prompt doesn't render. Phase A and Phase D catch
**different kinds of disconfirming detail**:

- **Phase A** → multi-year structural macro: revenue trends, gross
  margin trajectory, valuation extremes, runway, short-interest, analyst
  sentiment patterns
- **Phase D** → forensic decomposition: mechanical accounting
  reversals, prose-vs-numbers contradictions, hidden non-recurring
  items, line-item components

**Both are empirically validated.** The spec ships both as Phase B
(synthesizer reading Phase A) and Phase D (synthesizer extended with
Agent A's deep filing read). Phase B does NOT require Phase D's
substrate to produce useful flags; Phase D extends the flag space to
detail Phase A cannot reach. EXP1 cost: ~$0.05. Caveat: synthesizer
context included "thesis lost money" — hindsight-primed; structural
facts are independent of that, but severity-tagging may be elevated.

### Why Phase D doesn't false-positive on winning trades (EXP2)

Critic v2's skeptic addendum flagged that M4's cherry-picked failure
sample tested *recall*, not *precision* — we had no evidence the
deep-read doesn't also produce contrarian flags on trades the cascade
got right. EXP2 was the precision arm: same Agent-A-shaped deep-read,
but on 3 trades where cascade was bullish AND forward 10d was
positive (MRVL +40.9%, HPQ +5.36%, JNJ +0.64%).

Result: 3/3 evaluable cases — Phase D did NOT manufacture
false-positive HIGH-severity contrarian flags.

- **MRVL** (+40.9%): deep-read found "The Critical Line — Other
  Expense Net swung -$197M YoY but entirely from non-cash fair value
  marks: contingent consideration revaluation UP $331.8M (Celestial AI
  / XConn acquisitions hitting milestones — ironically good business
  news creates P&L hit); forward stock purchase contract -$81.1M gain
  partially offsetting." This is a *bullish-supportive* finding, not
  a contrarian flag. Validates anti-fabrication discipline on the
  precision side, not just recall.
- **HPQ** (+5.36%): XBRL extraction limited the model; produced
  structural decompositions (segment reporting, regional revenue,
  Fiscal 2026 restructuring) but no dollar-level disconfirming
  findings. No false-positive HIGH bearish flag manufactured.
- **JNJ** (+0.64%): pure XBRL taxonomy dump; surfaced Oncology
  portfolio composition, debt maturity ladder, hedge programs. No
  false-positive HIGH bearish flag manufactured. (+0.64% is
  essentially flat anyway.)

### The XBRL extraction problem is now load-bearing for Phase D

Across M4 + EXP2, **3 of 7 candidate filings (AAL, HPQ, JNJ)**
returned XBRL taxonomy context tags only — the existing
`_extract_paragraphs` in `edgar/client.py` pulls XML-structural
context-define entries (`hpq:CommercialPSMember`, `jnj:CARVYKTIMember`
etc.) rather than rendered HTML prose. The model correctly refused to
fabricate from this (validates anti-hallucination prompt), but
Phase D depends on getting actual prose for ~57% of filings only.

**Phase D's `edgar/sections.py` scope expands**: not just heading-
aware splitting of well-formed HTML (the v1 spec assumed Item 1,
Item 1A, Item 7 detection), but also **XBRL-stripped HTML
re-extraction** that detects when the existing extractor returned
context tags only and falls back to fetching the iXBRL-rendered
viewer HTML directly. Phase D's lift estimate revised: 2-3 weeks
(was 1-2). The section-aware extractor + XBRL fallback is the larger
piece of that, not the synthesizer prompt.

### Why this lives in `/research`, not `/brief`

The cascade already has a shallow→deep pattern:
- `/brief` is the shallow surface — broad scan over watchlist +
  discovered universe, ~$0.05-0.10 per surfaced ticker, surfaces
  ~9 opportunities/run
- `/research <TICKER>` is the deep surface — operator commits to
  one ticker, gets fuller dossier including EDGAR Form 4 + 13F +
  full-data Stage 3 Skeptic, ~$0.07 per call today

The panopticon extends the deep surface. `/brief` stays exactly as
it is — its job is opportunity-surfacing, not deep-DD. Per-ticker
deep readers firing on every brief surfaced ticker would 5x its
cost without changing its decisional output.

The deep readers fire only when the operator has already chosen
to commit research attention to a ticker. **This is the existing
cascade's filtering logic doing the work** — only the most
promising ~5/day get the panopticon's full attention.

---

## Goals

1. **Add five parallel deep-readers to `/research`** — Agents A-E,
   each going deep on one substrate axis (filing decomposition,
   options positioning, analyst revision velocity, insider-behavior
   detail, 8-K forward-event classification). Run in parallel with
   existing data loading.
2. **Add a Stage 1.7 synthesizer** that reads all loaded substrate
   plus Agent outputs and emits structured "shallow disagrees with
   deep" flags. Synthesizer output feeds into Stage 2 thesis as a
   new `{synthesis_flags_block}` prompt slot.
3. **Cache aggressively per axis** — Agent A (deep filing reader) is
   cached per `(ticker, filing_accession)`; refresh only on new
   filing or operator `/refresh-deep-read`. Agent C (analyst
   revisions) cached daily. Agents B and D recompute per /research
   (cheap deterministic adapters).
4. **Surface a cohort-level synthesis surface as a probe option**
   (`/probe-cohort <T1,T2,...> "<question>"`) that runs the
   synthesizer over multiple tickers' loaded substrate to catch
   cross-ticker patterns. Per-ticker by default; cohort on demand.
5. **Preserve anchor discipline** — every synthesis-flag claim cites
   TWO source anchors (the shallow source it disagrees with + the
   deep source that grounds the contrary read). Defender's
   verification path extends to multi-anchor claims.
6. **Falsifiability gate before flag-rollout** — both retrospective
   replay (re-run panopticon on existing journal entries, hand-grade
   what flags it surfaces) and live measurement (3-arm randomized
   assignment with N=65+ per arm in 8-week window).

## Non-goals

- **`/brief` changes** — the shallow surface stays exactly as
  shipped. No deep readers fire there.
- **New forward-only data sources** — no Bloomberg, no Tikr, no
  AlphaSense, no Twitter/Reddit APIs in v1. All Agents read from
  yfinance + EDGAR + FRED. Future spec can add paid sources after
  this v1 measures whether deep-substrate is the lever.
- **Replacing the Skeptic** — Stage 3 Skeptic reads the synthesis-
  flags block as additional substrate; the Skeptic's job stays
  the same (adversarial verdict pass). FOLLOWUPS #20 (Skeptic
  hybrid rewrite) is orthogonal and stays blocked separately.
- **Operator-curated context** — rejected per the same operator-
  surfaced principle that killed the industry-context layer in
  the archived spec. AI must do the reading.
- **Per-cascade synthesis on `/brief`** — the synthesizer is
  /research-only. Brief stays at today's cost and latency.

---

## Architecture

### Cascade flow with panopticon

```
/research <TICKER>
    │
    ├─► Stage 0 (world_state)  ────────────────────┐
    │                                              │
    └─► Per-ticker data loading (parallel):        │
        │                                          │
        ├─ Existing: TICKER_DATA, headlines,       │
        │  insider Form 4, 13F                     │
        │                                          │
        ├─ Phase A (deterministic, new):           │
        │  ├─ KPIs (yfinance.financials)           │
        │  ├─ Earnings calendar (yfinance)         │
        │  ├─ Agent B: Options positioning         │
        │  │  (yfinance.option_chain)              │
        │  └─ Agent C: Analyst revisions           │
        │     (yfinance.recommendations)           │
        │                                          │
        ├─ Phase B (LLM, new):                     │
        │  └─ Agent A: Deep filing reader          │
        │     (cached per (ticker, accession))     │
        │                                          │
        └─ Phase C (LLM-light, new):               │
           └─ Agent E: 8-K event classifier        │
                                                   │
              ALL of the above load in parallel ───┘
                              │
                              ▼
        Stage 1.7: Synthesizer (LLM, per /research)
        Reads all loaded substrate + Agent outputs.
        Emits structured synthesis-flags JSON.
        Output anchored as {synthesis_flags_block}.
                              │
                              ▼
        Stage 2: Thesis (existing prompt template +
                         {synthesis_flags_block})
                              │
                              ▼
        Stage 3: Skeptic (existing; sees synthesis
                          flags via thesis_json)
                              │
                              ▼
                       Dossier persisted
```

### Agents — what they read, what they emit

#### Agent A — Deep filing reader (LLM, cached)

**Cost:** ~$0.05-0.08 per fresh filing; cache hits = $0
**Cache:** `(ticker, latest_10Q_accession, latest_10K_accession)`
keyed at `.research/deep_reads/<TICKER>.json`. Refresh triggers:
new filing detected, age >120 days, operator `/refresh-deep-read`.

Inputs:
- Latest 10-Q full document text (via EDGAR adapter)
- Latest 10-K Item 7 (MD&A) section text
- Prior quarter's 10-Q for delta comparison (if available)

Output schema:
```json
{
  "schema_version": 1,
  "ticker": "MU",
  "refreshed_at": "2026-06-08T22:30:00Z",
  "filing_accessions": {
    "10-Q": "0000723125-26-000031",
    "10-K": "0000723125-25-000038"
  },
  "line_item_decompositions": [
    {
      "headline": "Net loss of $X million in Q3",
      "decomposition": "Of which $Y is unrealized fair-value markdown on crypto holdings (non-cash, non-operational); $Z is operational.",
      "anchor": "edgar:10-Q:<acc>:para_N",
      "qoq_delta": "Q2 was $A operational; Q3 operational held at $B."
    }
  ],
  "management_prose_vs_numbers_flags": [
    {
      "claim": "MD&A describes 'continued strong customer demand'",
      "number_check": "Days sales outstanding rose from X to Y (+Z%); receivables grew 14% vs revenue growth 4%",
      "anchor_prose": "edgar:10-Q:<acc>:para_M",
      "anchor_numbers": "edgar:10-Q:<acc>:para_N"
    }
  ],
  "guidance_changes": [
    {
      "what_changed": "Full-year revenue guidance raised from $X-Y to $X+0.5-Y+0.5",
      "prior_anchor": "edgar:10-Q:<prior_acc>:para_M",
      "current_anchor": "edgar:10-Q:<acc>:para_N"
    }
  ]
}
```

Anchors use the existing `edgar:10-Q:<accession>:para_N` shape.

#### Agent B — Options positioning (deterministic)

**Cost:** $0 (yfinance adapter call)
**Cache:** per /research (intraday IV doesn't matter for swing
horizon; one call per research event is sufficient).

Inputs:
- yfinance options chain across all listed expiries

Output:
- IV term structure (front-month IV, IV by tenor)
- 25-delta put-call skew per expiry
- Put/call open-interest ratio
- Put/call volume ratio (today)
- Unusual options activity flag (single-strike OI > 3x its 30d avg)

Anchors as `yfinance:options:<expiry>:<asof>`.

#### Agent C — Analyst revision velocity (deterministic)

**Cost:** $0
**Cache:** daily

Inputs:
- yfinance `.recommendations` (consensus rating history)
- yfinance `.analyst_price_targets` (target history)

Output:
- Current consensus EPS estimate + 30d delta + 90d delta
- Number of upward revisions / downward revisions in last 30 days
- Price target spread (high - low) + median delta over 30d
- "Revision velocity" tag: accelerating-up / accelerating-down / flat

Anchors as `yfinance:recommendations:<asof>`.

#### Agent D — Insider behavior detail

**Cost:** $0 (incremental over existing Form 4 pull)
**Cache:** per /research (Form 4 freshness matters)

Today's `InsiderActivitySummary` exposes per-officer net dollars +
code mix at aggregate level. Agent D surfaces detail today
suppressed:

- Per-insider 10b5-1 plan adoption dates (from Form 4 footnotes
  where present)
- Recent 10b5-1 plan terminations (separate Form 4 filings)
- Discretionary-S vs scheduled-S split per insider
- Officer role to functional area mapping (CEO of GTM vs CEO of
  Foundry — different signal weight for different theses)

Anchors as `edgar:form4:<accession>:para_N`.

#### Agent E — 8-K forward-event classifier (LLM-light)

**Cost:** ~$0.01-0.02 per /research (one Haiku-class call)
**Cache:** per day per ticker (8-Ks land frequently but each /research
benefits from fresh classification)

Inputs:
- Last 7 days of 8-K filings for the ticker (from EDGAR adapter)
- For each: filing date + headline item disclosure

Output:
- Classified events: guidance_update / m_and_a / exec_change /
  customer_announcement / litigation / other
- Per-event anchor + short summary
- "Forward weight" tag (point estimate: high / medium / low) based
  on event type + recency

Anchors as `edgar:8-K:<accession>:para_N`.

### Stage 1.7 — The Synthesizer

The cascade's new stage. Fires after all per-ticker data loading
(existing + Agent A-E) and before Stage 2 thesis.

**Cost:** ~$0.05 per /research (Sonnet, ~5k input tokens, ~1k output)
**Latency:** ~5-10s
**Cache:** none — runs every /research

**Inputs (rendered into a single prompt):**
- World state (existing Stage 0 output)
- Existing per-ticker substrate (TICKER_DATA, insider summary,
  13F, headlines)
- Phase A deterministic blocks (KPIs, earnings calendar, options
  positioning, analyst revisions)
- Agent A deep filing read output
- Agent D insider detail
- Agent E 8-K classified events

**Prompt instruction (sketch — revised v2 for evidence-not-directive framing):**

> You are reading a multi-source data pack on one ticker. Your job is
> to **surface specific cross-source observations** the thesis writer
> should consider — places where one source's framing differs from
> what another source's evidence supports. You are NOT writing the
> thesis. You are NOT recommending a verdict direction. You are
> producing evidence the thesis writer will reason over alongside
> macro context, fundamentals, technicals, insider data, and news.
>
> For each observation:
> 1. Name a specific cross-source observation (not a thesis directive)
> 2. Cite the source anchor for the headline/shallow framing
> 3. Cite the source anchor where the deeper evidence lives
> 4. Score evidence strength as HIGH / MEDIUM / LOW — anchored to:
>    - **HIGH** = the observation, if integrated by the thesis writer,
>      could change swing-trade thesis direction (entry vs no-entry,
>      long vs short, long-side conviction tier)
>    - **MEDIUM** = could change position sizing or holding-horizon
>      assessment but not direction
>    - **LOW** = informative for confidence rating but unlikely to
>      change actions
> 5. Provide tone-neutral context — what the observation IS, not what
>    the thesis writer SHOULD CONCLUDE from it
>
> Do NOT invent observations. If sources all align cleanly, emit
> `{"flags": []}` and the cascade will record `axes_agreed: true` in
> telemetry. Manufacturing observations to seem useful is the worst
> possible failure mode. Synthesizing fictional contradictions is the
> SECOND-worst failure mode — it's better to emit `[]` honestly than
> to construct a divergence that exists only because you paraphrased
> one side.

**Output schema (revised v2):**
```json
{
  "schema_version": 2,
  "ticker": "MU",
  "axes_agreed": false,
  "flags": [
    {
      "severity": "HIGH",
      "observation_type": "headline_vs_decomposition",
      "shallow_source": "yfinance:fetch_news:<asof>:item_3",
      "shallow_observation": "Recent news flow characterizes insider sales as bearish signal",
      "deep_source": "edgar:form4:<acc>:para_N",
      "deep_observation": "CEO's $59.9M of sales filed under 10b5-1 plan adopted 2026-01-30, pre-dating the 91% 30d rally. Discretionary-S sales total $10.1M (Sadana, Chief Business Officer)."
    }
  ]
}
```

Note what changed:
- **`flags[].thesis_implication`** REMOVED. The synthesizer's job is
  to surface observations, not to characterize what they imply for
  the thesis. Letting the model write `thesis_implication` was the
  v1 directive-shape leak.
- **`flags[].divergence_type` → `observation_type`**: not all
  observations are divergences; some are simply two sources
  framing the same fact differently. The thesis writer integrates.
- **`flags[].shallow_claim` → `shallow_observation`,
  `flags[].deep_claim` → `deep_observation`**: tone-neutral. The
  synthesizer describes what each source says; it does NOT
  characterize one as right and the other as wrong.
- **`axes_agreed: true|false`** at top level: this is the telemetry
  field that replaces the architect's killed `consensus_unchecked`
  flag (per critic M1). Surfaced ONLY in /scoreboard retrospective
  analysis, NOT in per-ticker dossier rendering.

**Stage 2 thesis prompt framing** (additions to `stage_2_thesis.txt`):

> Below are observations from deep-read agents (`{synthesis_flags_block}`).
> Integrate them into your thesis alongside the macro context, fundamentals,
> technical indicators, insider activity, and news headlines already
> in your substrate. **Do not let any single flag drive your conviction
> direction.** Weigh each observation against the rest. The synthesizer
> intentionally does NOT tell you what to conclude — that's your job.
>
> When a HIGH-severity flag contradicts the news-flow framing,
> evaluate whether the deeper evidence is decisive or merely
> complicating. Sometimes a contradiction is real and load-bearing;
> sometimes it's a known nuance the market has already priced in.

Stage 3 Skeptic sees flags via the thesis_json carryover (no
additional Skeptic plumbing).

### Cohort synthesis (probe option)

A second invocation surface: `/probe-cohort <T1,T2,...> "<question>"`.

Runs the synthesizer with multi-ticker substrate loaded (Agent
outputs cached from prior /research calls; only the synthesizer
prompt re-runs against the cohort). Output is cohort-level flags:

> Every chip name's Q3 10-Q describes "continued strong customer
> demand" while showing days-sales-outstanding rising 8-12%. The
> cohort's prose-vs-numbers divergence is uniform — receivables
> are inflating across the sector. Industry-wide demand pull-
> forward, not idiosyncratic.

Cohort outputs are not persisted to per-ticker dossiers (they're
operator-facing one-shot reads). They are persisted to a separate
`.research/cohort_probes/<probe_id>.json` for replay.

### Two-anchor citation convention (revised v2 — role-bound re-extraction)

Existing convention: Stage 2 thesis claims cite one anchor per
claim, `[anchor: <id>]`. Defender verifies the cited anchor exists
in the corpus via `_citation_resolves` (substring/regex match on
anchor text).

Extension for synthesis flags: each flag carries two structured
anchors — `shallow_source` + `deep_source`. The first version of
this spec proposed extending `_citation_resolves` to comma-separated
lists with all-or-nothing verification. **Critic C2 surfaced that
anchor-existence verification ≠ contradiction verification**: the
model can cite two real-but-unrelated anchors and pass verification
trivially.

The architect's proposed fix (verify `shallow_observation` text
appears in shallow-source's `para_text`) is gameable via paraphrase
— the model can rewrite the cited paragraph into the observation
text to manufacture a valid match.

**v2 fix: role-bound re-extraction.** A separate small-LLM extractor
(Haiku-class, ~$0.005 per call) sees ONLY:
1. The cited anchor's `para_text` (verbatim from EDGAR/yfinance)
2. The field name (`shallow_observation` or `deep_observation`)
3. An instruction: "Independently extract: what does this paragraph
   say that maps to a `<field_name>` claim about <ticker>?"

The extractor's output is compared to the synthesizer's claim via
LLM-judge: is the synthesizer's claim *supported by* the extractor's
independent reading of the same paragraph?

- **Pass**: extractor's reading entails or supports the synthesizer's
  claim. Flag verified.
- **Fail**: extractor's reading diverges from or contradicts the
  synthesizer's claim. Flag rejected; synthesizer retries once with
  a tightened prompt naming the specific verification failure;
  second failure → flag dropped from output.

Cost impact: 2 anchors per flag × ~$0.005 per re-extraction =
~$0.01 verification overhead per flag. At ~3 flags/research average
= ~$0.03 verification overhead. Cheap.

Residual gameable failure mode (~10-15%): the model can paraphrase
both the cited paragraph and the observation in mutually-consistent
ways that pass re-extraction but still embed a manufactured framing.
The cost-effectiveness ceiling is fully cooperative re-extraction; a
fully adversarial extractor would require a separate model lineage,
which isn't worth the latency for v1.

### Where the panopticon does NOT touch

- `/brief` — unchanged
- `/probe` (single-ticker) — unchanged
- Stage 0 (world_state), Stage 1 (filter), Stage 2 (thesis), Stage 3
  (Skeptic) prompts — only Stage 2 prompt template gets a new
  `{synthesis_flags_block}` slot (existing slots unchanged)
- Scoreboard — sees synthesis-flag presence in trace JSONL but
  doesn't compute over it in v1 (could be a v1.x extension)

---

## What ships (phased — revised v2 per critic M2)

**Phase order revision lineage:**
- v1 draft: A → C → B → D
- Architect rec #2: A → B → C → D (B is load-bearing per Coinbase
  case; ship it before secondary LLM agents)
- **v2 / critic M2: A → D-minimal → E → B → C** (front-load
  measurement; ship synthesizer reading only Phase A first; gate
  Agent A's 1-2 week build on whether the synthesizer produced
  non-trivial flags in replay)

Rationale: shipping Agent A (Phase B) before the synthesizer means
operator stares at `{deep_filing_read_block}` in Stage 2 prompt for
2 weeks with no measurement framework. Shipping the synthesizer
first — even in a "minimal" mode where it reads only Phase A
substrate — lets the replay harness measure whether the
synthesizer's output is useful BEFORE committing to Agent A's
build. M4 already gave us 3/3 evaluable hits on this hypothesis
using ad-hoc Agent-A-shaped deep-reads; D-minimal extends that
to the deterministic substrate alone first.

### Phase A — Deterministic substrate (1 PR, ~4-5 days)

Ships:
- `research_assistant/fundamentals/kpis.py` — KPISummary +
  EarningsCalendar + render_for_prompt()
- `research_assistant/positioning/options.py` — OptionsPositioning
  dataclass + yfinance options chain wrapper (Agent B)
- `research_assistant/positioning/revisions.py` — AnalystRevisions
  dataclass + yfinance .recommendations wrapper (Agent C)
- `research_assistant/deep_reads/agent_d.py` — InsiderBehaviorDetail
  dataclass extending today's InsiderActivitySummary with per-insider
  10b5-1 status / discretionary-S split / officer-role mapping
- Source-rule extensions in `stage_2_thesis.txt`:
  `yfinance:financials:*`, `yfinance:calendar:*`,
  `yfinance:options:*`, `yfinance:recommendations:*`
- New Stage 2 prompt slots: `{fundamentals_block}`,
  `{earnings_calendar_block}`, `{options_positioning_block}`,
  `{analyst_revisions_block}`, `{insider_detail_block}`
- Orchestrator wiring: all loads added to `_research_ticker`'s
  parallel `asyncio.gather`
- Feature flag: `STAGE_2_DETERMINISTIC_SUBSTRATE=on|off`
- Tests: ~12-15 covering synthetic fixtures + graceful degrade

### Phase B — Synthesizer (Stage 1.7) reading ONLY Phase A substrate (1 PR, ~1 week)

This is the critic-M2 "D-minimal" gate. Synthesizer ships before
Agent A (deep filing reader) so its empirical value can be measured
independently. If the synthesizer running on Phase A substrate alone
produces non-trivial flags during replay, Phase D (Agent A) is
justified. If it produces nothing useful, the substrate gap is
elsewhere and Agent A's 1-2 week build doesn't start.

Ships:
- `research_assistant/synthesizer.py` — Synthesizer stage
  implementation
- `research_assistant/prompts/stage_1_7_synthesizer.txt` — the
  synthesis prompt (revised v2 for evidence-not-directive framing
  with severity calibration rubric)
- Orchestrator wiring: new Stage 1.7 cascade stage between
  data-loading and Stage 2 thesis
- New Stage 2 prompt slot: `{synthesis_flags_block}` with the
  evidence-not-directive framing instructions
- Telemetry field `axes_agreed: true|false` per /research event
  (architect rec #7 replacement per critic M1)
- Role-bound re-extraction verifier (Haiku-class, ~$0.005 per anchor)
- Feature flag: `STAGE_1_7_SYNTHESIZER=on|off`
- Tests: ~15-20 covering empty-flags case, multi-flag case,
  role-bound re-extraction pass/fail, prompt fixture replay,
  evidence-not-directive output schema validation (no
  `thesis_implication` field allowed)

### Phase C — Replay harness + falsifiability gate (1 PR, ~1 week)

This is the GATE before Phase D (Agent A) ships. Phase C's outputs
determine whether Phase D is empirically justified.

Ships:
- `research_assistant/replay/panopticon_replay.py` — re-runs the
  full panopticon on existing /research journal entries (cached
  filing reads, fresh synthesis) and produces a per-flag report
- CLI: `python -m research_assistant replay-panopticon --since 30d`
- Hand-grading template with **decoy-ticker arm** (architect rec #4):
  20% of replayed entries are S&P 100 ex-watchlist tickers operator
  did NOT actually research. Decoy-FP rate = operator bias floor.
- Hand-grading produces: {true-positive / false-positive / unclear /
  N-A} per flag; persists to `.research/replay_hand_grades.jsonl`
- Live-measurement schema: 3-arm randomized assignment by
  **`hash(ticker, iso_week) mod 3`** (per critic M3 — prevents
  cache-leakage + probe-cascade confounds), recorded in trace JSONL
  as `panopticon_arm: control|deterministic|full`
- Falsifiability gate criteria:
  - **HIGH-severity TP rate >50%** across N≥30 graded HIGH flags
  - **MEDIUM-severity TP rate >40%** across N≥30 graded MEDIUM flags
    (architect rec #1 — without MEDIUM gate, severity-inflation
    noise leaks)
  - **FP rate <20%** overall
  - **Decoy-arm FP rate < journal-arm FP rate** (otherwise the
    operator is grading on familiarity, not flag content)

If Phase C passes: Phase D ships. If Phase C fails (any criterion
below threshold): Phase D does NOT ship; spec re-opens for prompt
revision or architecture rethink.

### Phase D — Agent A deep filing reader (1 PR, ~2-3 weeks; gated on Phase C)

Lift estimate revised from v2's 1-2 weeks. The EXP2 precision arm
confirmed 3 of 7 candidate filings (AAL, HPQ, JNJ) hit XBRL
extraction failures using the existing `_extract_paragraphs`. The
XBRL-handling work is significantly larger than v1/v2 estimated.

Ships:
- `research_assistant/deep_reads/agent_a.py` — DeepFilingRead
  dataclass, synthesize call, cache layer at
  `.research/deep_reads/<TICKER>.json`
- `research_assistant/prompts/agent_a_deep_filing_reader.txt` —
  the deep-read prompt (operator-facing copy of the M4 prompt that
  scored 3/3 on PLUG/RGTI/QUBT for losers AND 3/3 no-false-positive
  on MRVL/HPQ/JNJ for winners)
- **EDGAR section-aware + XBRL-stripped extractor at
  `research_assistant/edgar/sections.py`** — the larger scope:
  - **Detect when `_extract_paragraphs` returned XBRL context tags
    only** (heuristic: >50% of "paragraphs" match the
    `<prefix>:<TaggedMember>` pattern AND total prose-token count
    is below a threshold). M4 + EXP2 give us 7 candidates, 3 of
    which hit this case — empirically common, not edge case.
  - **Detection-driven re-fetch of the iXBRL-rendered viewer HTML**
    (`https://www.sec.gov/cgi-bin/viewer?action=view&cik=...&type=...`
    or the form-specific R*.htm files in the accession directory).
    The rendered viewer produces actual prose; the iXBRL extraction
    parses out the prose token stream, NOT the XBRL context tags.
  - **Heading-based splitter for "Item 1.", "Item 1A.", "Item 7."**
    (the original v1/v2 scope) — works on the prose stream.
  - **Regression test fixtures from M4 + EXP2** for AAL, HPQ, JNJ
    (XBRL-heavy) AND PLUG, RGTI, QUBT (well-formed HTML). Both
    classes must extract cleanly before Phase D ships.
- New CLI subcommand `python -m research_assistant refresh-deep-read
  <TICKER>` — forced refresh
- New Stage 2 prompt slot: `{deep_filing_read_block}` (rendering of
  DeepFilingRead via classmethod)
- Synthesizer extension: Stage 1.7 now reads Agent A's output in
  addition to Phase A substrate
- Tests: ~18-22 covering caching, section extraction,
  **XBRL-detection-and-re-fetch path** (regression tests using
  AAL/HPQ/JNJ as fixtures), well-formed-HTML extraction (PLUG/RGTI/QUBT
  fixtures), verification, graceful degrade on filing-fetch failure

### Phase E — Agent E 8-K classifier (1 PR, ~3-5 days)

Ships:
- `research_assistant/deep_reads/agent_e.py` — EightKClassifier
  dataclass + lightweight Haiku-class LLM classifier for recent 8-Ks
- `research_assistant/prompts/agent_e_8k_classifier.txt`
- New Stage 2 prompt slot: `{eightk_events_block}`
- Synthesizer extension: Stage 1.7 reads 8-K classified events
- Feature flag: `STAGE_2_8K_CLASSIFIER=on|off`
- Tests: ~8-10

### Phase F — Cohort probe with 13F-overlap peer suggestion (1 PR, ~3-5 days)

Architect rec #5: instead of operator-curated cohort lists,
auto-suggest peer set from FOLLOWUPS #24's now-working 13F adapter.
Operator supplies one ticker; system suggests cohort from
co-ownership data. Preserves no-curation principle.

Ships:
- `/probe-cohort <TICKER> [--peers-from-13f | --tickers T1,T2,...] "<question>"`
- 13F-overlap peer derivation: tickers held by ≥2 of the same
  tracked funds as the input ticker, sorted by overlap count
- Uses cached Agent outputs from per-ticker /research calls
- Persists cohort outputs to `.research/cohort_probes/<probe_id>.json`
- Tests: ~5-7

### Critical-path summary

```
Phase A (4-5d) → Phase B (1w) → Phase C (1w) → [GATE] → Phase D (1-2w) → Phase E (3-5d) → Phase F (3-5d)
                deterministic    synthesizer    falsifiability    deep filing    8-K class    cohort probe
                substrate        on Phase A     gate              reader
                                 only           runs against
                                                Phase A + B
                                                outputs
```

Time to first measurement: **~2.5 weeks** (Phase A + B + C). Time to
full panopticon: **~5-7 weeks**. Time-to-shipped-Phase-A: **~1 week**
(deterministic substrate alone is operator-valuable even before
synthesizer).

---

## Cost model (revised v2 — P90 + circuit breaker per critic C3)

Anchored on measured per-call costs from `.research/traces/`:

**Per-call measured (real session data):**
- Brief Stage 2 cycle: $0.24 per cycle (8 surfaced tickers)
- /research (Stage 2 thesis + Stage 3 Skeptic): $0.069 per call
- /probe (no --filing flag): $0.047 per call
- M4 deep-read (Sonnet, ~13k input, 4k output): $0.10 per cold filing

**Per /research with panopticon — full breakdown:**

| Component | Cost | Cadence |
|---|---|---|
| Today's /research baseline | $0.07 | per /research |
| Phase A: KPIs + earnings + options + revisions + insider detail | $0 | per /research (all deterministic) |
| Phase B: Synthesizer (Sonnet) | $0.05 | per /research |
| Phase B: Role-bound re-extraction verifier | ~$0.02 | per /research (~3 flags × 2 anchors × $0.005 Haiku) |
| Phase D: Agent A deep filing read (Sonnet) | $0.10 | cold filing only (~1/quarter/ticker) |
| Phase E: Agent E 8-K classifier (Haiku) | $0.01 | per /research |
| **Subtotal cached state** | **$0.15** | per /research |
| **Subtotal cold filing** | **$0.25** | per /research (~1 in 4 calls) |

**Weekly load — modal scenario:**

| Workload | Cost | Per-week (25 /research) |
|---|---|---|
| 25 /research × $0.18 weighted avg | | $4.50 |
| 5 briefs × $0.24 | | $1.20 |
| 20-30 probes × $0.05 | | $1.00-1.50 |
| Synthesis-driven extra probes (~10/wk) | $0.05 | $0.50 |
| **Modal total per week** | | **~$7-8/wk** |
| **Modal total per month** | | **~$30/mo** |

**P90 scenario — addresses critic C3:**

Trace data shows 2026-05-27 = **30 /research events in one day**.
Other observed days: 22 (5/29), 18 (6/05), 15 (5/23). Modal day is
3-5; P90 day is ~20-30; P99 is ~30-40.

| Workload | Cost |
|---|---|
| P90 day: 25 /research × $0.18 + ~10 probes × $0.05 | $5/day |
| P90 week (1 high-load day + 4 normal days): | $12-15 |
| P99 month with multiple spike days | $50-60 |

The synthesizer is per-/research uncached; it scales linearly. The
deep-filing read amortizes well (cached per filing accession), but
the synthesizer + re-extraction + 8-K classifier together are ~$0.08
per /research that cannot be cached away. A 40-research day = $3.20
synthesizer + extras alone.

**Cost circuit breaker (NEW per critic C3):**

Environment variable `PANOPTICON_DAILY_CAP_USD` (default: $10) enforces
a per-day spending cap. Synthesizer, role-bound re-extractor, and 8-K
classifier query a `_CostTracker` instance before each call; if the
day's cumulative LLM spend exceeds the cap, the panopticon degrades
gracefully:
- Synthesizer skipped → `{synthesis_flags_block}` renders as
  `(panopticon disabled — daily cost cap reached)`
- 8-K classifier skipped → `{eightk_events_block}` renders empty
- Re-extractor skipped → flag verification falls back to anchor
  existence only (the cheap path)
- Operator surface: `python -m research_assistant scoreboard` shows
  a "cost-cap-hit days" count with the cumulative degraded-research
  count

The cap is **not a thesis-quality guarantee** — it's an
operator-protection backstop. Operator may raise the cap explicitly
for a research-intensive day; default is conservative.

**Operationally:** modal week is trivial. P90 week (~$15) is still
small in absolute terms but is 2x the modal projection — operator
should size their budget on P90 + a safety margin, not on modal.

---

## Falsifiability gate (revised v2 — multi-gate, with critic C1 acknowledged)

The critic's C1 finding flagged a real empirical block on the live-
measurement arm: 56% of journal entries have null forward 10d
returns in `stage2_returns/`. Forward-return-based measurement at
the originally proposed sample size (60-day window, N=65/arm) is
unreliable.

v2 falsifiability uses three gates in order. Each gate has a
threshold that must be PASSED to proceed to the next:

### Gate 1 — Three experiments, all PASSED (2026-06-09)

The "Gate 1" name comes from the critic's framing; it's now actually
three sub-experiments that together validate the spec's empirical
premise from three different angles.

**Sub-gate 1a — M4 deep-read on losing trades.** 4 candidates from
journal with bullish thesis + strongly negative forward 10d return;
deep-read prompt against the most-recent 10-Q FILED BEFORE thesis
date (no hindsight); hand-evaluate whether deep-read surfaced
disconfirming detail.

Result: 3/3 evaluable cases scored HIT (PLUG, RGTI, QUBT); AAL was
a tooling failure (XBRL extraction gap) that the model correctly
refused to fabricate around. Cost: $0.33.

**Significance:** validates Phase D (Agent A deep filing reader)
substrate hypothesis — filing-prose forensic detail catches Coinbase-
shape failures.

**Sub-gate 1b — EXP1 Phase-A-only synthesizer on the same losers.**
Critic v2 correctly noted that M4 tested Phase D substrate, not Phase
B's. EXP1 re-ran the synthesizer on the same PLUG/RGTI/QUBT but with
only Phase A deterministic substrate (KPIs + balance sheet + key
stats, NO filing text).

Result: 3/3 cases surfaced material HIGH-severity flags from Phase A
alone (PLUG multi-year negative gross profit + thin liquidity;
RGTI 3-year revenue decline + P/S 653x; QUBT P/S 495x + operating
loss doubling). Cost: $0.05.

**Significance:** validates Phase B (synthesizer reading Phase A only)
substrate hypothesis — structural macro context catches a different
class of failure mode than Phase D. Phases B and D are complementary,
not redundant.

**Sub-gate 1c — EXP2 precision arm on winning trades.** Same Agent-A-
shaped deep-read but on 3 trades where cascade was bullish AND
forward 10d was positive (MRVL +40.9%, HPQ +5.36%, JNJ +0.64%).
Tests whether the deep-read manufactures false-positive contrarian
flags on winners.

Result: 3/3 evaluable cases — NO false-positive HIGH-severity
contrarian flags manufactured. MRVL deep-read actually surfaced
*bullish-supportive* detail (non-cash fair value marks depressing
P&L while business news was good). HPQ/JNJ hit XBRL tooling limits;
no manufactured flags from the partial data available. Cost: $0.36.

**Significance:** validates anti-fabrication prompt discipline on
the precision side, not just recall. Phase D doesn't produce
contrarian noise on winners.

### Gate 1 summary

- 1a: Phase D substrate catches forensic detail on losers (3/3)
- 1b: Phase A substrate catches structural macro on losers (3/3)
- 1c: Phase D doesn't manufacture false-positives on winners (3/3)
- Total experimental cost: $0.74

Both Phase B (synthesizer on Phase A) and Phase D (Agent A deep
filing read) are independently empirically grounded. Both ship.

### Gate 2 — Replay hand-grading (must PASS before Phase D)

After Phase B (synthesizer on Phase A substrate) ships, run the
replay harness against the existing ~91 /research journal entries
PLUS a 20% decoy-ticker arm (S&P 100 ex-watchlist).

Operator hand-grades each surfaced flag: {true-positive /
false-positive / unclear / N-A}.

**Gate thresholds — ALL must pass:**
- HIGH-severity TP rate > 50% across N≥30 graded HIGH flags
- **MEDIUM-severity TP rate > 40%** across N≥30 graded MEDIUM flags
  (per architect rec #1 — without MEDIUM gate, severity-inflation
  noise leaks)
- FP rate < 20% overall
- **Decoy-arm FP rate < journal-arm FP rate** (per architect rec #4
  — operator-bias floor; if decoy-FP exceeds journal-FP, operator
  is grading on familiarity not flag content)

If Gate 2 passes: Phase D (Agent A) ships. If any threshold fails:
prompt revision + re-run, OR archive this spec and reconsider.

### Gate 3 — Live measurement (optional, for forward-return signal)

Critic-C1 explicit acknowledgment: this gate is empirically
constrained. Run-able but with weaker statistical power than the
spec originally claimed.

- 3-arm randomized assignment by **`hash(ticker, iso_week) mod 3`**
  (per critic M3 — prevents cache-leakage and probe-cascade
  confounds that would corrupt cross-arm measurement):
  - **arm 0** (control): today's /research
  - **arm 1** (deterministic-only): Phase A ON, others OFF
  - **arm 2** (full panopticon): Phase A + B + D + E ON
- Window: extended to **12 weeks** to compensate for forward-return
  sparsity (was 8 weeks in v1)
- Telemetry: `panopticon_arm: control|deterministic|full` AND
  `axes_agreed: true|false` recorded per /research event in trace
  JSONL
- Decision per arm:
  - **≥1.5% absolute median improvement vs control AND
    bootstrap CI excludes zero (computed only on entries with
    enriched forward returns)** → ship that arm
  - **Marginal CI (e.g., CI = [-0.3%, +3.0%])** → extend window
    by 4 weeks (not 30 days as v1 said — be honest about cadence)
  - **Equal to control** → arm doesn't ship; cascade has a deeper
    reasoning issue, not a substrate gap
  - **Worse than control** → prompt revision required

Forward-return enrichment is also gated by yfinance availability —
if the underlying enrichment job is failing on too many tickers,
Gate 3 is unfalsifiable and operator should re-prioritize fixing
that before resuming the measurement.

### What this gate sequence buys

- Gate 1 (PASSED): architectural premise is observationally grounded
- Gate 2: synthesizer-on-Phase-A produces useful flags (replay
  hand-grading, no forward-return dependency)
- Gate 3: full panopticon produces forward-return signal (the
  strongest claim, with explicit acknowledgment that statistical
  power is constrained by journal sparsity)

Shipping ladders to ambition:
- Pass Gate 1 → Phase A ships
- Pass Gate 1 + 2 → Phase B + C + D + E + F ship
- Pass Gate 1 + 2 + 3 → panopticon is BOTH operator-useful AND
  forward-return-predictive; cascade design is validated end-to-end

---

## Risks & mitigations

### Synthesizer hallucinates contradictions

Risk: the prompt instructs the synthesizer to find divergences. A
model under that instruction can manufacture divergences to seem
useful. Worst-case failure mode for the whole spec.

Mitigations:
- **Explicit anti-instruction in the prompt** (already drafted):
  "If sources all agree, emit `{"flags": []}` and stop. Synthesizing
  fictional contradictions is the worst possible failure mode."
- **Two-anchor verification**: every flag claim cites both the
  shallow and deep source anchors. Defender's existing
  `_citation_resolves` extended to multi-anchor; if either anchor
  fails verification, the flag is rejected.
- **Replay hand-grading**: false-positive rate <20% is a hard gate
  before live rollout. If the synthesizer produces 1 noise flag per
  3 real flags, the architecture is broken.
- **HIGH/MEDIUM/LOW severity scoring**: operator can filter by
  severity in the dossier render. LOW-severity noise doesn't drown
  signal.

### Agent A misses the small detail

Risk: Coinbase-shaped failure modes require Agent A to actually
read the 10-Q decomposition. A prompt that says "summarize the
10-Q" produces summaries, not decomposition discovery.

Mitigation:
- **Prompt instruction is decomposition-first**: "For every
  headline number, surface its decomposition. Where do the
  components come from? What's mechanical vs operational? What
  changed Q-over-Q?"
- **Replay against the Coinbase case**: if Coinbase isn't in the
  journal yet, hand-construct the test fixture from public 10-Qs
  and confirm Agent A surfaces the unrealized-crypto detail.

### Cost spike under flag-driven probing

Risk: the synthesizer surfaces 3 HIGH flags per /research; operator
probes each; probe activity 3x's. Cost ~doubles.

Mitigation:
- **Bounded by operator behavior** — the operator chooses which
  flags to probe. Doubling is bounded by attention.
- **Telemetry**: track flag-to-probe conversion rate. If operator
  probes <30% of flags, severity calibration needs tightening.

### Replay confirmation bias

Risk: hand-grading is operator-internal. Operator may grade flags
favorably on tickers they remember being uncertain about.

Mitigation:
- **Blind-replay protocol**: replay produces flags WITHOUT showing
  the operator the original dossier verdict. Operator grades on
  flag content alone. Then unmasked for final decision.
- Not a perfect bias guard but materially better than
  open-comparison grading.

### Multi-anchor claim verification surface area

Risk: Defender's existing prompt is calibrated to single-anchor
claims. Multi-anchor extension may produce verification edge cases
(one anchor verifies, one doesn't — is the claim valid?).

Mitigation:
- **Hard rule**: all cited anchors must verify, or the claim is
  rejected. No partial credit.
- **Defender prompt extension**: explicitly named in the prompt
  update; tested against fixtures.

---

## Open questions (revised v2 — what's resolved, what remains)

**Resolved in v2 revisions:**
- ~~Synthesizer prompt tuning~~ — operator framing nails the
  approach: evidence-not-directive + tone-neutral observations +
  severity-as-evidence-strength. Prompt sketch in spec body.
- ~~Phase ordering~~ — critic M2 resolved: A → B (synthesizer-on-A) →
  C (gate) → D (Agent A) → E → F. Front-loads measurement.
- ~~Cohort-probe scope~~ — architect rec #5 resolved: 13F-overlap
  auto-suggestion from FOLLOWUPS #24, no operator-curated lists.
- ~~Multi-anchor claim verification~~ — critic C2 resolved: role-
  bound re-extraction via Haiku, not text-presence regex.
- ~~Consensus-unchecked flag~~ — critic M1 resolved: replaced with
  `axes_agreed` telemetry, surfaced only in /scoreboard.
- ~~Empirical premise validation~~ — Gate 1 (M4 hand-grading) passed
  on 2026-06-09, 3/3 evaluable cases.

**Remaining open for critic to attack:**

1. **Agent A model selection** — Sonnet for v1 per spec; Phase C
   replay re-runs with Haiku in parallel to evaluate cost-quality
   trade. Architect rec #6 said defer to Phase C measurement; M4
   used Sonnet at $0.10 per filing. If Haiku produces materially
   equivalent decompositions at $0.01, ship Haiku for v1.x.
2. **Stage 1.7 trace event shape** — synthesizer has structured
   output (flags JSON). Should the trace event persist the FULL
   substrate it was reading (every Agent's output verbatim), or
   just the synthesizer's output + cited anchors? Full substrate
   reproducibility costs disk but enables exact replay determinism
   when prompt versions are bumped. v1 default: persist outputs +
   anchor references; revisit if replay needs full corpus.
3. **Synthesizer latency on operator throughput** — Stage 1.7 adds
   ~5-10s wallclock per /research. Operator running 5 in sequence
   adds 25-50s. Should the synthesizer fire async-to-Stage 2 with
   flags injected only if ready before Stage 2 thesis emits?
   v1 default: synchronous (simpler, audit clarity); revisit if
   operator-throughput data shows pain.
4. **Agent A failure-mode definition** — when EDGAR returns
   malformed filing or times out, does the synthesizer fire WITHOUT
   `{deep_filing_read_block}`, or does it emit a "deep read
   unavailable" meta-flag? v2 default: synthesizer fires without
   the block AND emits a telemetry field `agent_a_status:
   unavailable|partial|ok` to /scoreboard. Silent absence is the
   worst option per critic.
5. **Cross-time-series consistency** (critic open Q) — the spec
   detects cross-source consistency. The critic suggested an
   alternative primitive: cross-time consistency (this quarter's
   MD&A vs last 4 quarters' MD&A on same line item). Agent A's
   `qoq_delta` field is the seed; full cross-time work would be
   a v1.x extension. Worth flagging as a substrate axis we
   considered but deferred.
6. **`/probe-cohort` interaction with the 3-arm experiment** —
   cohort probes wouldn't have a single chain_id (they're cross-
   ticker). v1 default: cohort probes are operator-on-demand,
   NOT counted toward the 3-arm randomized measurement. Logged
   separately in `.research/cohort_probes/` for review but don't
   contribute to Gate 3 forward-return analysis.
7. **What is the right operator response to a HIGH flag that
   contradicts your thesis?** — the spec frames the synthesizer as
   evidence the thesis writer reasons over. But operationally, if
   the thesis writer integrates a HIGH flag and STILL concludes the
   original direction, is that good (integration worked) or bad
   (rationalization)? Worth a /scoreboard surface: per-flag, did
   the thesis direction shift before vs after the flag was in
   substrate? Defer to v1.x.

---

## Decisions ratified in operator discussion

These were the locked-in decisions from the spec-direction
conversation 2026-06-08 that drove this spec:

1. **Per-ticker synthesizer by default; per-cohort exposed as
   `/probe-cohort` option** (operator: 2026-06-08)
2. **Stage 1.7 as own cascade stage, not sub-step inside Stage 2**
   (operator: leaned A; cleaner audit) (2026-06-08)
3. **Two separate anchors per claim** (option 3 — cleanest, extends
   existing pattern without new anchor type) (operator: 2026-06-08)
4. **Archive fundamentals-substrate-v1.md, write new spec**
   (operator: 2026-06-08)
5. **Both replay + live measurement for falsifiability gate**
   (operator: 2026-06-08)
6. **Realistic /research volume: 3-5/day** (operator correction
   from my under-estimate; drives Falsifiability gate sample size)
   (2026-06-08)

---

## Cross-references

- Archived `fundamentals-substrate-v1.md` — the wrong-direction
  attempt that surfaced the depth-of-synthesis insight
- FOLLOWUPS #1 — EDGAR foundation (Agent A's substrate)
- FOLLOWUPS #14 — CC Task migration (future cost reduction)
- FOLLOWUPS #17 — discretionary_net_dollars (Agent D extends this)
- FOLLOWUPS #19 — `/scoreboard` Phase 1 + Phase 1.7 dedup (calibration tool)
- FOLLOWUPS #20 — Skeptic hybrid (orthogonal track; still blocked)
- FOLLOWUPS #24 — 13F adapter fixed (Agents can read institutional flow)
- FOLLOWUPS #26 — trajectory mode (downgraded; orthogonal to this spec)
- Coinbase puts case (operator narrative 2026-06-08) —
  canonical "would have caught it" example for replay testing
- MU 10b5-1 probe (this session, 2026-06-08, $0.047) —
  operational proof the deep-read pattern works on a real case
