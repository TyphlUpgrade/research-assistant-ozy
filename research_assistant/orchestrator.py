"""
Research orchestrator — single-ticker DD pipeline (`/research <TICKER>`).

Pipeline:
1. Load ticker data via yfinance_adapter (bars, quote, news)
2. Build ticker_data dict + Stage 1 placeholder (single-ticker mode skips
   batched filter — we already chose this ticker)
3. Stage 2: Sonnet thesis call → conviction + drivers + risks +
   open_questions + evidence_anchors
4. Stage 3: Sonnet Skeptic critique → adjusted_score + flagged_risks +
   open_questions_added + news_reactivity_flag
5. Merge into Dossier; append Ledger entries citing evidence anchors
6. Write atomic; return rendered summary

Defender invocation is NOT in this module — it lives in the conversational
loop that calls `defend()` when the user pushback heuristic fires. The
orchestrator's job ends at "research complete, dossier updated."
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from research_assistant.claude_sdk import CallResult, ClaudeClient
from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    read_dossier,
    write_dossier_atomic,
)
from research_assistant.edgar import (
    FilingExcerpts,
    InsiderActivitySummary,
    InstitutionalOwnership,
)
from research_assistant.observations import Observation, append_observation
from research_assistant.prompts import chain_id as _chain_id
from research_assistant.prompts import load_prompt as _load_prompt
from research_assistant.prompts import render as _render
from research_assistant.trace_renderer import append_stage_event
from ozymandias.intelligence.claude_json import parse_claude_response

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 2 structured note (PR 2A.2)
# ---------------------------------------------------------------------------

# The five decision tags Stage 2 may emit. Parser rejects anything else
# (preserves the operator's ability to grep/filter by tag without coping
# with free-form variants).
STAGE2_DECISION_TAGS: tuple[str, ...] = (
    "CHASE", "WATCH", "PROBE", "RESEARCH", "PASS",
)

# The four conviction dimensions Stage 2 scores. Order is fixed so the
# composite math + the render layer have a stable iteration order.
STAGE2_CONVICTION_DIMENSIONS: tuple[str, ...] = (
    "technical", "fundamental", "catalyst", "regime",
)

# Inline-Skeptic verdict enum (PR 2A.3). UNAVAILABLE is the graceful-degrade
# sentinel used when the Skeptic call fails (network / parse / API outage);
# operator should NOT treat UNAVAILABLE as AGREE — it's "we don't know".
SKEPTIC_VERDICTS: tuple[str, ...] = (
    "AGREE", "WEAKEN", "STRONG_OBJECTION", "UNAVAILABLE",
)

# Discrete adjustment multipliers applied to `composite_conviction` based on
# Skeptic verdict. AGREE leaves the score unchanged; WEAKEN is a moderate
# down-adjustment (~15%); STRONG_OBJECTION is a sharp down-adjustment (~35%).
# UNAVAILABLE → 1.0 (no adjustment; we preserve the original score and rely on
# the verdict tag to signal "Skeptic didn't run" to the operator). Module-level
# so tuning is one edit, not buried in the helper.
SKEPTIC_ADJUSTMENT_MULTIPLIERS: dict[str, float] = {
    "AGREE": 1.00,
    "WEAKEN": 0.85,
    "STRONG_OBJECTION": 0.65,
    "UNAVAILABLE": 1.00,
}


@dataclass(frozen=True)
class Stage2Note:
    """Structured-note output of Stage 2 (PR 2A.2).

    Replaces the prose-thesis schema (thesis_text + key_drivers + risks +
    open_questions + evidence_anchors). Designed to make honest data
    interpretation the path of least resistance: ONE bull anchor + ONE bear
    anchor (no symmetric false-balance), specific `what_would_change`
    triggers (no vague open questions), multi-dimensional conviction (so
    the weak dimension is visible), enum decision tag (so downstream code
    can branch cleanly).

    `composite_conviction` is the geometric mean of the four dimension
    scores. Geometric (not arithmetic) so one weak dimension drags the
    composite — matches the design principle that high conviction requires
    convergence across dimensions, not averaging away a red flag. The
    function `compute_composite_conviction` is the single source of truth
    for the math; tests pin its behavior.
    """
    ticker: str
    observation: tuple[str, ...]                  # immutable so frozen=True survives
    bull_anchor: str
    bear_anchor: str
    what_would_change: tuple[str, ...]
    conviction: Mapping[str, float]                # {technical, fundamental, catalyst, regime} — MappingProxyType at runtime
    composite_conviction: float                   # POST-Skeptic value (see PR 2A.3 below)
    decision_tag: str                             # one of STAGE2_DECISION_TAGS
    # PR 2A.3: inline Skeptic adversarial check. `composite_conviction`
    # above is the POST-Skeptic value (displayed to operator). The pre-
    # Skeptic geometric mean is preserved in `composite_conviction_pre_skeptic`
    # for trace / debug. New fields default so cached Stage2Notes from
    # before PR 2A.3 deserialize without crash; backward-compat path in
    # cli._brief_item_from_cache also passes UNAVAILABLE explicitly.
    skeptic_verdict: str = "UNAVAILABLE"          # one of SKEPTIC_VERDICTS
    skeptic_reasoning: str = ""
    composite_conviction_pre_skeptic: Optional[float] = None
    # PR 2A.4: cross-day trajectory awareness. The LLM observes PRIOR_READS
    # in the prompt and summarises its directional drift in one sentence
    # here. Default empty so cached Stage2Notes from before PR 2A.4
    # deserialize without crash; backward-compat path in
    # cli._brief_item_from_cache treats missing as "".
    trajectory_summary: str = ""


def compute_composite_conviction(conviction: dict[str, float]) -> float:
    """Geometric mean of the four conviction dimensions.

    Returns 0.0 if any dimension is 0.0 (geometric-mean behavior — a single
    zero zeros the product). Missing dimensions are treated as 0.0 (forces
    Stage 2 to score all four; missing == admit weakness, not skip).

    Pinned by `test_composite_conviction_geometric_mean`. Sanity: dimensions
    all 0.5 → composite 0.5 (geometric mean of identical values equals the
    value).
    """
    if not conviction:
        return 0.0
    n = len(STAGE2_CONVICTION_DIMENSIONS)
    product = 1.0
    for dim in STAGE2_CONVICTION_DIMENSIONS:
        v = float(conviction.get(dim, 0.0))
        # Clamp into [0.0, 1.0] — defensive against LLM out-of-range output.
        v = max(0.0, min(1.0, v))
        product *= v
    return product ** (1.0 / n)


def parse_stage2_note(payload: dict, *, default_ticker: Optional[str] = None) -> Stage2Note:
    """Parse a Stage 2 JSON response into a Stage2Note.

    Required fields: `bull_anchor`, `bear_anchor`, `conviction` (with all
    four dimension keys), `decision_tag`. Missing required fields raise
    ValueError so callers (trace event + brief render) get a clean signal
    rather than a silently-degraded Stage2Note.

    Defensive transforms (logged as WARN, not raised):
      - `bull_anchor` / `bear_anchor` arriving as a list → collapse to
        first element. The prompt is explicit that these are scalars; if
        the model returns a list anyway we take the first rather than
        crashing the brief render. WARN logs the violation.
      - `observation` / `what_would_change` arriving as a scalar string →
        wrap in a single-element tuple.
      - `decision_tag` arriving in unexpected case → uppercase + validate
        against STAGE2_DECISION_TAGS.
    """
    # SECURITY: caller-supplied ticker wins unconditionally when provided.
    # The LLM output is untrusted; allowing payload["ticker"] to override
    # default_ticker means a prompt-injected response can drive arbitrary
    # downstream file writes (stage2 journal path).
    if default_ticker is not None:
        ticker = str(default_ticker).strip().upper()
    else:
        ticker = str(payload.get("ticker", "")).strip().upper()
    if not ticker:
        raise ValueError("Stage2Note missing required field: ticker")

    def _collapse_anchor(value, label: str) -> str:
        if isinstance(value, list):
            log.warning(
                "Stage2Note %s arrived as list (prompt requires scalar); collapsing to first element: %r",
                label, value,
            )
            value = value[0] if value else ""
        if value is None:
            raise ValueError(f"Stage2Note missing required field: {label}")
        return str(value)

    def _normalize_string_list(value) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(v) for v in value)

    bull_anchor = _collapse_anchor(payload.get("bull_anchor"), "bull_anchor")
    bear_anchor = _collapse_anchor(payload.get("bear_anchor"), "bear_anchor")

    observation = _normalize_string_list(payload.get("observation"))
    what_would_change = _normalize_string_list(payload.get("what_would_change"))

    raw_conviction = payload.get("conviction")
    if not isinstance(raw_conviction, dict):
        raise ValueError(
            f"Stage2Note conviction must be a dict with keys {STAGE2_CONVICTION_DIMENSIONS}; "
            f"got {type(raw_conviction).__name__}"
        )
    conviction: dict[str, float] = {}
    for dim in STAGE2_CONVICTION_DIMENSIONS:
        if dim not in raw_conviction:
            raise ValueError(f"Stage2Note conviction missing dimension: {dim}")
        raw = raw_conviction[dim]
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Stage2Note conviction[{dim!r}] not numeric: {raw!r}"
            ) from exc
        if math.isnan(v) or math.isinf(v):
            raise ValueError(
                f"Stage2Note conviction[{dim!r}] not finite: {raw!r}"
            )
        conviction[dim] = max(0.0, min(1.0, v))

    raw_tag = payload.get("decision_tag")
    if raw_tag is None:
        raise ValueError("Stage2Note missing required field: decision_tag")
    decision_tag = str(raw_tag).strip().upper()
    if decision_tag not in STAGE2_DECISION_TAGS:
        raise ValueError(
            f"Stage2Note decision_tag must be one of {STAGE2_DECISION_TAGS}; "
            f"got {raw_tag!r}"
        )

    composite = compute_composite_conviction(conviction)

    # PR 2A.4: trajectory_summary is OPTIONAL — older payloads (pre-PR-2A.4)
    # may not carry it, and the prompt instructs the model to emit
    # "no prior reads" when PRIOR_READS is empty. Default to "" so the
    # parser stays permissive across both cases.
    trajectory_summary = str(payload.get("trajectory_summary", "") or "")

    return Stage2Note(
        ticker=ticker,
        observation=observation,
        bull_anchor=bull_anchor,
        bear_anchor=bear_anchor,
        what_would_change=what_would_change,
        conviction=MappingProxyType(conviction),  # frozen view — matches frozen=True intent
        composite_conviction=composite,
        decision_tag=decision_tag,
        trajectory_summary=trajectory_summary,
    )


def _render_screener_evidence_block(screener_evidence: list[dict]) -> str:
    """Render `screener_evidence` as a Stage-2 prompt block.

    Stage 2 sees the raw evidence dicts framed as "these screeners flagged
    this ticker" — NOT as a Stage-1 score or ranking justification. The
    block deliberately omits any intrinsic_score / breakdown fields.
    Empty list → explicit "(no screener hits)" so the prompt placeholder
    never leaks.
    """
    if not screener_evidence:
        return "(no screener hits for this ticker)"
    lines: list[str] = []
    for ev in screener_evidence:
        screener = ev.get("screener", "<unknown>")
        # Strip any accidental intrinsic_score/breakdown keys from the
        # rendered detail — defense-in-depth against caller leakage.
        detail = {
            k: v for k, v in ev.items()
            if k not in {"screener", "intrinsic_score", "breakdown"}
        }
        if detail:
            lines.append(f"- {screener}: {detail}")
        else:
            lines.append(f"- {screener}")
    return "\n".join(lines)


@dataclass
class ResearchResult:
    """Output of one `/research <TICKER>` invocation."""
    symbol: str
    thesis_text: str
    conviction_score: float
    adjusted_score: float          # post-Skeptic
    key_drivers: list[str]
    risks: list[str]
    flagged_risks: list[str]       # from Skeptic
    open_questions: list[str]
    evidence_anchors: list[dict]
    critique_text: str
    news_reactivity_flag: bool
    chain_id: str                  # for /trace renderer
    cost_usd: float


# Prompt-block rendering for each EDGAR data source is owned by the
# dataclass itself via `render_for_prompt(cls, optional_instance)`. The
# orchestrator just calls those classmethods — keeps the "what does
# Stage 2 see when EDGAR is down?" decision in one place per source.


# Matches the FilingText.anchor format: edgar:<form>:<accession>:para_<n>.
# Three colon-separated segments after the "edgar:" prefix — form
# (e.g. "10-K", "13F-HR"), accession (e.g. "0001234567-26-000045"),
# para_N. Used by _enrich_anchors_with_filing_text to detect citable
# paragraph anchors so Defender (#2) can verify pushback citations
# against the fetched paragraph text without needing its own EDGAR
# fetch path.
_EDGAR_PARA_ANCHOR_RE = re.compile(r"^edgar:[\w.-]+:[\w.-]+:para_\d+$")


def _enrich_anchors_with_filing_text(
    anchors: list,
    filing_excerpts: Optional[FilingExcerpts],
) -> list:
    """For any anchor dict whose `source` matches the edgar paragraph
    pattern and lines up with a paragraph in `filing_excerpts`, splice
    the paragraph text into the dict as `para_text`. Defender's
    `_flatten_anchors_to_corpus` then picks up the text automatically
    (no Defender code change required).

    Non-dict entries and non-matching sources pass through unchanged.
    """
    if not filing_excerpts:
        return list(anchors)
    out: list = []
    for a in anchors:
        if not isinstance(a, dict):
            out.append(a)
            continue
        source = a.get("source", "")
        if not _EDGAR_PARA_ANCHOR_RE.match(source):
            out.append(a)
            continue
        text = filing_excerpts.by_anchor(source)
        if text is None:
            out.append(a)
            continue
        out.append({**a, "para_text": text})
    return out


async def _stage_2_thesis(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    stage_1_result: dict,
    headlines: list[dict],
    insider_activity: Optional[InsiderActivitySummary] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
) -> tuple[Optional[dict], Optional[CallResult]]:
    """
    Invoke Stage 2 (Sonnet thesis). Returns (parsed_json, call_metadata).
    parsed_json is None on parse failure; call_metadata is always returned
    (so trace events can record even when JSON parse fails).
    """
    template = _load_prompt("stage_2_thesis")
    prompt = _render(
        template,
        ticker_json=json.dumps(ticker_data, indent=2),
        stage_1_json=json.dumps(stage_1_result, indent=2),
        headlines_json=json.dumps(headlines, indent=2),
        insider_activity_block=InsiderActivitySummary.render_for_prompt(insider_activity),
        institutional_ownership_block=InstitutionalOwnership.render_for_prompt(institutional_ownership),
    )
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    result = await client.call(prompt, model="claude-sonnet-4-6", system=system)
    return parse_claude_response(result.text), result


async def _stage_2_note(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    *,
    insider_activity: Optional[InsiderActivitySummary] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
    screener_evidence: Optional[list[dict]] = None,
    prior_reads: Optional[list[dict]] = None,
    default_ticker: Optional[str] = None,
) -> tuple[Optional[Stage2Note], Optional[CallResult]]:
    """Invoke Stage 2 (Sonnet structured-note). Returns (note, call_metadata).

    `note` is None on parse failure or schema-validation failure (logged at
    WARN). `call_metadata` is always returned (so trace events can record
    even when the parse fails).

    Stage 1 score / breakdown is NOT a parameter — that's the data
    isolation principle that PR 2A.2 enforces structurally. The signature
    literally cannot leak Stage 1's read into Stage 2.

    PR 2A.4: `prior_reads` is the compact history persisted via
    `journal.append_stage2_note`, most-recent first. Rendered into the
    prompt as the `PRIOR_READS` block; the LLM observes drift and writes
    a `trajectory_summary` sentence. Empty list (or None) → empty JSON
    array; the prompt's "no prior reads" escape covers the new-ticker case.
    """
    template = _load_prompt("stage_2_note")
    prompt = _render(
        template,
        ticker_json=json.dumps(ticker_data, indent=2),
        headlines_json=json.dumps(headlines, indent=2),
        insider_activity_block=InsiderActivitySummary.render_for_prompt(insider_activity),
        institutional_ownership_block=InstitutionalOwnership.render_for_prompt(institutional_ownership),
        screener_evidence_block=_render_screener_evidence_block(screener_evidence or []),
        prior_reads_json=json.dumps(prior_reads or [], indent=2),
    )
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    result = await client.call(prompt, model="claude-sonnet-4-6", system=system)
    raw = parse_claude_response(result.text)
    if raw is None:
        return None, result
    try:
        note = parse_stage2_note(raw, default_ticker=default_ticker)
    except ValueError as exc:
        log.warning("Stage2Note parse failed (%s); raw=%r", exc, raw)
        return None, result
    return note, result


async def _stage_2_skeptic_check(
    client: ClaudeClient,
    note: Stage2Note,
    *,
    chain_id: str,
    traces_base: Path,
) -> tuple[str, str, float]:
    """Inline Skeptic adversarial pass on a Stage2Note (PR 2A.3).

    Returns `(verdict, reasoning, composite_after_adjustment)`.

    Skeptic challenges the bull/bear anchors and emits a discrete verdict that
    drives a multiplicative adjustment to `composite_conviction`:
        AGREE → 1.00
        WEAKEN → 0.85
        STRONG_OBJECTION → 0.65

    Data isolation: the Skeptic prompt receives ONLY `bull_anchor` and
    `bear_anchor`. NOT composite_conviction (PR 2A.7 — removed because the
    model was using it as an "already priced upstream" escape valve to
    AGREE on everything), NOT ticker_data, NOT headlines, NOT screener
    evidence. The structural point of an adversarial pass is to challenge
    the *read* on the anchors alone, not redo the analysis or rationalise
    an upstream score.

    Graceful degrade: on network failure, parse failure, or invalid verdict,
    returns `("UNAVAILABLE", "(Skeptic call failed)", note.composite_conviction)`.
    The brief still ships; the operator sees the explicit UNAVAILABLE tag and
    can re-run or run `/research <TICKER>` for the full Stage 3 pass.

    Emits a `stage_2_skeptic_check` trace event (distinct stage_id from
    Stage 3 Skeptic in /research) so cost + verdict are visible in /trace.
    """
    template = _load_prompt("stage_2_skeptic_check")
    prompt = _render(
        template,
        bull_anchor=note.bull_anchor,
        bear_anchor=note.bear_anchor,
    )

    parsed: Optional[dict] = None
    s_meta: Optional[CallResult] = None
    error: Optional[str] = None
    try:
        s_meta = await client.call(prompt, model="claude-sonnet-4-6")
        parsed = parse_claude_response(s_meta.text)
        if parsed is None:
            error = "Skeptic JSON parse failed"
    except Exception as exc:  # network / API outage / unexpected SDK error
        log.warning("Skeptic call failed for %s: %s", note.ticker, exc)
        error = f"Skeptic call exception: {type(exc).__name__}"

    verdict = "UNAVAILABLE"
    reasoning = "(Skeptic call failed)"
    if parsed is not None:
        raw_verdict = str(parsed.get("verdict", "")).strip().upper()
        # Only the three live verdicts are valid model output. UNAVAILABLE is
        # reserved for the graceful-degrade path — if a model emits it,
        # treat as parse failure (don't let the LLM short-circuit the check).
        if raw_verdict in ("AGREE", "WEAKEN", "STRONG_OBJECTION"):
            verdict = raw_verdict
            reasoning = str(parsed.get("reasoning", "")).strip() or "(no reasoning)"
        else:
            log.warning(
                "Skeptic returned unknown verdict for %s: %r",
                note.ticker, parsed.get("verdict"),
            )
            error = f"Skeptic unknown verdict: {parsed.get('verdict')!r}"

    multiplier = SKEPTIC_ADJUSTMENT_MULTIPLIERS.get(verdict, 1.0)
    adjusted = max(0.0, min(1.0, note.composite_conviction * multiplier))

    append_stage_event(
        chain_id=chain_id,
        stage_id="stage_2_skeptic_check",
        model=s_meta.model if s_meta else "claude-sonnet-4-6",
        tokens_in=s_meta.input_tokens if s_meta else 0,
        tokens_out=s_meta.output_tokens if s_meta else 0,
        cost_usd=s_meta.cost_usd if s_meta else 0.0,
        latency_ms=s_meta.latency_ms if s_meta else 0,
        parsed={
            "verdict": verdict,
            "reasoning": reasoning,
            "composite_pre": note.composite_conviction,
            "composite_post": adjusted,
            "multiplier": multiplier,
        },
        raw_response=s_meta.text if s_meta else None,
        traces_base=traces_base,
        error=error,
        symbol=note.ticker,
    )
    return verdict, reasoning, adjusted


async def _stage_3_skeptic(
    client: ClaudeClient,
    world_state: dict,
    thesis_with_ticker_data: dict,
    model: str = "claude-sonnet-4-6",
) -> tuple[Optional[dict], Optional[CallResult]]:
    """
    Invoke Stage 3 (Skeptic). Returns (parsed_json, call_metadata).
    """
    template = _load_prompt("stage_3_skeptic")
    prompt = _render(template, thesis_json=json.dumps(thesis_with_ticker_data, indent=2))
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    result = await client.call(prompt, model=model, system=system)
    return parse_claude_response(result.text), result


async def research_ticker(
    symbol: str,
    *,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    base: Path,
    client: Optional[ClaudeClient] = None,
    insider_activity: Optional[InsiderActivitySummary] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
) -> ResearchResult:
    """
    Run the mini-cascade for one ticker. Stage 2 thesis + Stage 3 Skeptic,
    write to dossier, return ResearchResult for rendering.

    Args:
        symbol: e.g. "TSLA"
        world_state: output of Stage 0 (provided by caller — typically cached
            across multiple /research calls in a session for cost efficiency)
        ticker_data: market data + TA snapshot for this ticker. Must include
            price, recent_return_5d, volume_ratio, return_30d, return_90d,
            weekly_rsi_14, volume_5d_trend, optional earnings_within_days.
        headlines: list of {title, publisher, age_hours, absorption_stage}
        base: research data dir (.research/)
        client: optional ClaudeClient to reuse (cost tracking continuity)
        insider_activity: optional Form 4 aggregate for the trailing window
            (FOLLOWUPS #3). None means EDGAR fetch failed or ticker is not
            in the SEC universe; an empty summary (total_filings=0) means
            no activity in the window — both are signal worth distinguishing
            in the prompt.

    Returns:
        ResearchResult with full Stage 2 + Stage 3 output.
    """
    symbol = symbol.upper()
    if client is None:
        client = ClaudeClient()
    chain = _chain_id()

    # Stage 2
    stage_1_placeholder = {
        "ticker": symbol,
        "intrinsic_score": 0.5,
        "reason": "single-ticker on-demand DD (Stage 1 batched filter skipped)",
    }
    stage_2, s2_meta = await _stage_2_thesis(
        client, world_state, ticker_data, stage_1_placeholder, headlines,
        insider_activity=insider_activity,
        institutional_ownership=institutional_ownership,
    )
    traces_base = base / "traces"
    append_stage_event(
        chain_id=chain,
        stage_id="stage_2_thesis",
        model=s2_meta.model if s2_meta else "unknown",
        tokens_in=s2_meta.input_tokens if s2_meta else 0,
        tokens_out=s2_meta.output_tokens if s2_meta else 0,
        cost_usd=s2_meta.cost_usd if s2_meta else 0.0,
        latency_ms=s2_meta.latency_ms if s2_meta else 0,
        parsed=stage_2,
        raw_response=s2_meta.text if s2_meta else None,
        traces_base=traces_base,
        error=None if stage_2 else "Stage 2 JSON parse failed",
        symbol=symbol,
    )
    if stage_2 is None:
        raise RuntimeError(f"Stage 2 JSON parse failed for {symbol} (chain={chain})")

    # Stage 3 — supply Stage 2 + ticker_data for momentum/exhaustion fields
    thesis_with_data = dict(stage_2)
    thesis_with_data["ticker_data"] = ticker_data
    stage_3, s3_meta = await _stage_3_skeptic(client, world_state, thesis_with_data)
    append_stage_event(
        chain_id=chain,
        stage_id="stage_3_skeptic",
        model=s3_meta.model if s3_meta else "unknown",
        tokens_in=s3_meta.input_tokens if s3_meta else 0,
        tokens_out=s3_meta.output_tokens if s3_meta else 0,
        cost_usd=s3_meta.cost_usd if s3_meta else 0.0,
        latency_ms=s3_meta.latency_ms if s3_meta else 0,
        parsed=stage_3,
        raw_response=s3_meta.text if s3_meta else None,
        traces_base=traces_base,
        error=None if stage_3 else "Stage 3 JSON parse failed",
        symbol=symbol,
    )
    if stage_3 is None:
        raise RuntimeError(f"Stage 3 JSON parse failed for {symbol} (chain={chain})")

    # Merge into dossier
    dossier = read_dossier(symbol, base) or Dossier(symbol=symbol)
    dossier.conviction = stage_3.get("adjusted_score", stage_2["conviction_score"])

    # Append ledger entries citing evidence anchors
    ts = datetime.now(timezone.utc).isoformat()
    dossier.ledger.append(LedgerEntry(
        timestamp=ts, kind="thesis", summary=stage_2["thesis_text"][:160],
        evidence_anchor=chain,
    ))
    dossier.ledger.append(LedgerEntry(
        timestamp=ts, kind="skeptic", summary=stage_3.get("critique_text", "")[:160],
        evidence_anchor=chain,
    ))

    # Open Questions = union of Stage 2 + Stage 3 additions
    new_questions = list(stage_2.get("open_questions", [])) + list(
        stage_3.get("open_questions_added", [])
    )
    dossier.open_questions = list(dict.fromkeys(dossier.open_questions + new_questions))

    # Rebuild State narrative
    dossier.state_md = (
        f"**Thesis:** {stage_2['thesis_text']}\n\n"
        f"**Conviction (post-Skeptic):** {dossier.conviction:.2f}\n\n"
        f"**Key drivers:** " + "; ".join(stage_2.get("key_drivers", [])) + "\n\n"
        f"**Risks (named):** " + "; ".join(stage_2.get("risks", [])) + "\n\n"
        f"**Skeptic critique:** {stage_3.get('critique_text', '')}\n\n"
        f"**Flagged additional risks:** " + "; ".join(stage_3.get("flagged_risks", []))
    )

    write_dossier_atomic(dossier, base)

    append_observation(
        Observation(
            ts=ts,
            kind="research",
            symbol=symbol,
            chain_id=chain,
            thesis=stage_2["thesis_text"],
            conviction=dossier.conviction,
            regime=world_state.get("regime"),
            drivers=list(stage_2.get("key_drivers", [])),
            risks=list(stage_2.get("risks", [])),
            flagged_risks=list(stage_3.get("flagged_risks", [])),
            open_questions=new_questions,
            anchors=list(stage_2.get("evidence_anchors", [])),
        ),
        base,
    )

    return ResearchResult(
        symbol=symbol,
        thesis_text=stage_2["thesis_text"],
        conviction_score=stage_2["conviction_score"],
        adjusted_score=stage_3.get("adjusted_score", stage_2["conviction_score"]),
        key_drivers=stage_2.get("key_drivers", []),
        risks=stage_2.get("risks", []),
        flagged_risks=stage_3.get("flagged_risks", []),
        open_questions=new_questions,
        evidence_anchors=stage_2.get("evidence_anchors", []),
        critique_text=stage_3.get("critique_text", ""),
        news_reactivity_flag=stage_3.get("news_reactivity_flag", False),
        chain_id=chain,
        cost_usd=client.cost.total_usd,
    )


# ---------------------------------------------------------------------------
# /probe — focused dossier-scoped query
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Output of one `/probe <TICKER> <QUESTION>` invocation."""
    symbol: str
    question: str
    answer: str
    evidence_anchors: list[dict]
    closes_questions: list[str]
    new_open_questions: list[str]
    chain_id: str = ""
    cost_usd: float = 0.0


def _format_dossier_context(dossier: Dossier, *, ledger_tail: int = 6) -> str:
    """Render the dossier into the probe prompt's `dossier_context` slot.
    Includes State, all Open Questions, and the most recent `ledger_tail`
    ledger entries (the probe stage doesn't need full ledger history)."""
    lines = ["## DOSSIER STATE", dossier.state_md or "(empty)"]
    lines.append("\n## OPEN QUESTIONS (verbatim — use exact strings in closes_questions)")
    if dossier.open_questions:
        for q in dossier.open_questions:
            lines.append(f"- {q}")
    else:
        lines.append("(none)")
    lines.append(f"\n## RECENT LEDGER (last {ledger_tail})")
    tail = dossier.ledger[-ledger_tail:] if dossier.ledger else []
    if tail:
        for entry in tail:
            anchor = f" [anchor: {entry.evidence_anchor}]" if entry.evidence_anchor else ""
            lines.append(f"- {entry.timestamp} — {entry.kind}: {entry.summary}{anchor}")
    else:
        lines.append("(empty)")
    return "\n".join(lines)


async def _stage_2_probe(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    dossier_context: str,
    focused_question: str,
    insider_activity: Optional[InsiderActivitySummary] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
    filing_excerpts: Optional[FilingExcerpts] = None,
) -> tuple[Optional[dict], Optional[CallResult]]:
    """Invoke the probe stage (Sonnet, dossier-scoped focused answer).
    Returns (parsed_json, call_metadata)."""
    template = _load_prompt("probe")
    prompt = _render(
        template,
        ticker_json=json.dumps(ticker_data, indent=2),
        headlines_json=json.dumps(headlines, indent=2),
        dossier_context=dossier_context,
        focused_question=focused_question,
        insider_activity_block=InsiderActivitySummary.render_for_prompt(insider_activity),
        institutional_ownership_block=InstitutionalOwnership.render_for_prompt(institutional_ownership),
        filing_excerpts_block=FilingExcerpts.render_for_prompt(filing_excerpts),
    )
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    result = await client.call(prompt, model="claude-sonnet-4-6", system=system)
    return parse_claude_response(result.text), result


async def probe_ticker(
    symbol: str,
    question: str,
    *,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    base: Path,
    client: Optional[ClaudeClient] = None,
    insider_activity: Optional[InsiderActivitySummary] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
    filing_excerpts: Optional[FilingExcerpts] = None,
) -> ProbeResult:
    """Run a focused probe against an existing dossier.

    Raises FileNotFoundError if no dossier exists for `symbol` — probe is the
    cold-start entry point against a SAVED dossier, not a way to create one.
    Use `/research <TICKER>` first if no dossier is present.

    Args:
        insider_activity: optional Form 4 aggregate for the trailing window
            (FOLLOWUPS #3). Same semantics as research_ticker: None means
            EDGAR fetch failed or ticker not in SEC universe; empty summary
            means no activity in window.

    Side effects (atomic w.r.t. dossier write):
      * appends a kind="probe" ledger entry citing the chain_id
      * drops resolved questions from dossier.open_questions
      * appends new_open_questions surfaced by the probe
      * appends a kind="probe" observation to the per-ticker stream
    """
    symbol = symbol.upper()
    dossier = read_dossier(symbol, base)
    if dossier is None:
        raise FileNotFoundError(
            f"No dossier found for {symbol}. Run `/research {symbol}` first to "
            f"build one before probing."
        )

    if client is None:
        client = ClaudeClient()
    chain = _chain_id()
    traces_base = base / "traces"

    dossier_context = _format_dossier_context(dossier)
    stage_2, s2_meta = await _stage_2_probe(
        client, world_state, ticker_data, headlines, dossier_context, question,
        insider_activity=insider_activity,
        institutional_ownership=institutional_ownership,
        filing_excerpts=filing_excerpts,
    )
    # Enrich edgar:<form>:<acc>:para_N anchors with their paragraph text
    # BEFORE the trace event is appended, so the trace JSONL persists the
    # enrichment and the defender-check loader picks it up later.
    if stage_2 is not None and filing_excerpts is not None:
        stage_2["evidence_anchors"] = _enrich_anchors_with_filing_text(
            stage_2.get("evidence_anchors") or [],
            filing_excerpts,
        )
    append_stage_event(
        chain_id=chain,
        stage_id="stage_2_probe",
        model=s2_meta.model if s2_meta else "unknown",
        tokens_in=s2_meta.input_tokens if s2_meta else 0,
        tokens_out=s2_meta.output_tokens if s2_meta else 0,
        cost_usd=s2_meta.cost_usd if s2_meta else 0.0,
        latency_ms=s2_meta.latency_ms if s2_meta else 0,
        parsed=stage_2,
        raw_response=s2_meta.text if s2_meta else None,
        traces_base=traces_base,
        error=None if stage_2 else "Probe Stage 2 JSON parse failed",
        symbol=symbol,
    )
    if stage_2 is None:
        raise RuntimeError(f"Probe JSON parse failed for {symbol} (chain={chain})")

    answer = stage_2.get("answer", "")
    closes = list(stage_2.get("closes_questions", []))
    new_qs = list(stage_2.get("new_open_questions", []))
    anchors = list(stage_2.get("evidence_anchors", []))

    # Update dossier: drop closed questions, append new ones, append probe ledger entry.
    ts = datetime.now(timezone.utc).isoformat()
    dossier.open_questions = [q for q in dossier.open_questions if q not in closes]
    dossier.open_questions = list(dict.fromkeys(dossier.open_questions + new_qs))
    ledger_summary = f"Probed: {question[:120]} → {answer[:120]}"
    dossier.ledger.append(LedgerEntry(
        timestamp=ts, kind="probe", summary=ledger_summary, evidence_anchor=chain,
    ))
    write_dossier_atomic(dossier, base)

    append_observation(
        Observation(
            ts=ts,
            kind="probe",
            symbol=symbol,
            chain_id=chain,
            thesis=answer,
            conviction=dossier.conviction,
            regime=world_state.get("regime"),
            open_questions=list(dossier.open_questions),
            anchors=anchors,
        ),
        base,
    )

    return ProbeResult(
        symbol=symbol,
        question=question,
        answer=answer,
        evidence_anchors=anchors,
        closes_questions=closes,
        new_open_questions=new_qs,
        chain_id=chain,
        cost_usd=client.cost.total_usd,
    )


# ---------------------------------------------------------------------------
# Defender invocation heuristic (3-condition AND, per Critic iter1 #11)
# ---------------------------------------------------------------------------

_DISAGREEMENT_RE = re.compile(
    r"\b(i\s+disagree|i\s+don'?t\s+think|you'?re\s+wrong|that'?s\s+wrong|"
    r"no,?\s+i|that'?s\s+(too|overly)|stop\s+being)\b",
    re.IGNORECASE,
)
_EVIDENCE_RE = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}\b|"           # ISO date
    r"\bQ[1-4]\s+(FY|20)\d{2,4}\b|"      # Q1 FY25 / Q1 2025
    r"\b[1-4]Q\d{2,4}\b|"                # 3Q24
    r"\b[1-4]H\d{2,4}\b|"                # 1H25
    r"\bFY\d{2,4}\b|"                    # FY2025
    r"\bper\s+the\b|\baccording\s+to\b|"  # citation markers
    r"\b10-?[QK]\b|\bfiling\b|\btranscript\b|"  # filing refs
    r"\$\d|\d+\s*%|\bbps\b)",            # numeric/financial
    re.IGNORECASE,
)

# Tokens that count as *specific* citation evidence (and so are checkable
# against the anchor corpus). Document-type words alone ("10-K", "filing")
# and pure citation phrases ("per the X") do NOT appear here — those need
# accompanying specifics to count as verifiable.
_STRONG_TOKEN_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|"            # ISO date
    r"\bQ[1-4]\s+(?:FY|20)\d{2,4}\b|"    # Q1 FY25 / Q1 2025
    r"\b[1-4]Q\d{2,4}\b|"                # 3Q24 / 2Q2025
    r"\b[1-4]H\d{2,4}\b|"                # 1H25 / 2H2024
    r"\bFY\d{2,4}\b|"                    # FY25 / FY2025 (bare)
    r"\d+(?:\.\d+)?\s*%|"                # 18% / 3.5%
    r"\$\d+(?:[.,]\d+)?\s*[BMK]?\b|"     # $5 / $5.2M / $500K
    r"\b\d+\s*bps\b",                    # 200 bps
    re.IGNORECASE,
)


def _extract_strong_tokens(text: str) -> list[str]:
    return [m.group(0).strip() for m in _STRONG_TOKEN_RE.finditer(text)]


# Citable fields — fields that legitimately belong in Defender's
# verification corpus. New anchor metadata (retrieved_at, cost_usd,
# cik, confidence, etc.) MUST stay out unless added here intentionally;
# otherwise a future field's value could resolve a fake citation token.
_CITABLE_ANCHOR_FIELDS = frozenset({"claim", "source", "para_text", "quote"})


def _flatten_anchors_to_corpus(anchors: list) -> str:
    """Join anchor citable fields into one lowercased blob suitable for
    substring containment checks. Allowlist enforcement: only `claim`,
    `source`, `para_text`, `quote` enter the corpus — other dict fields
    are treated as metadata and excluded so future schema additions
    can't accidentally widen the verification surface."""
    parts: list[str] = []
    for a in anchors or []:
        if isinstance(a, dict):
            parts.extend(
                str(v) for k, v in a.items() if k in _CITABLE_ANCHOR_FIELDS
            )
        else:
            parts.append(str(a))
    return " ".join(parts).lower()


def _citation_resolves(user_message: str, anchors: list) -> bool:
    """
    True iff the user's strong citation tokens (dates, quarters, %, $, bps)
    each appear in the prior anchor corpus with digit/period boundaries
    on both sides — so "18%" does NOT resolve against a corpus containing
    "118%" or "18.05%". Returns False when the user supplied only weak
    markers (e.g., bare "10-K", "per the filing") — those can't be checked
    against a typed-anchor corpus and so default to unverified.

    Asymmetric notation (`$5B` vs `5 billion`, `$5.0B`) intentionally fails
    closed — Defender fires. The conservative direction is correct for the
    BACKBONE axis: over-firing the Defender is safe; under-firing isn't.
    """
    tokens = _extract_strong_tokens(user_message)
    if not tokens:
        return False
    corpus = _flatten_anchors_to_corpus(anchors)
    for token in tokens:
        pattern = re.compile(
            r"(?<![\d.])" + re.escape(token.lower()) + r"(?![\d.])"
        )
        if not pattern.search(corpus):
            return False
    return True


def should_invoke_defender(
    prior_turn_had_recommendation: bool,
    user_message: str,
    prior_evidence_anchors: Optional[list] = None,
) -> bool:
    """
    Fire Defender iff:
    1. Prior turn produced a Recommendation (caller tracks this)
    2. User message expresses disagreement
    3. User message has no evidence markers OR its strong-token citation
       does not resolve against `prior_evidence_anchors`.

    `prior_evidence_anchors` defaults to None (empty corpus), which means
    any cited evidence is unverified and Defender fires. Passing the prior
    Stage-2 anchors enables the corpus check that closes the fake-citation
    floor (Open Follow-up #2 from the v1 plan).
    """
    if not prior_turn_had_recommendation:
        return False
    if not _DISAGREEMENT_RE.search(user_message):
        return False
    if not _EVIDENCE_RE.search(user_message):
        return True  # bare disagreement
    return not _citation_resolves(user_message, prior_evidence_anchors or [])
