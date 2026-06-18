"""
Stage 1.7 Synthesizer — the panopticon's cross-source observer.

Per FOLLOWUPS #28 / `.omc/specs/research-deep-substrate-v1.md` Phase B.

This stage fires AFTER all per-ticker data has loaded (Phase A
deterministic substrate + existing ticker_data / headlines / insider
aggregate / 13F) and BEFORE Stage 2 thesis. Its job is NOT to write a
verdict; it's to surface specific cross-source observations the thesis
writer should consider — places where one source's framing differs from
what another source's evidence supports.

Operator framing (v2 critic M1 + operator pushback):
  - Output is EVIDENCE, not directive. Severity = evidence strength,
    NOT verdict-shift weight. The Stage 2 thesis writer integrates the
    flags alongside macro context, fundamentals, technicals, insider,
    and news — and decides what they mean.
  - Empty output (`flags: []`) is FIRST-class — when sources align,
    `axes_agreed: true` telemetry fires and the prompt block renders
    "(no cross-source divergences surfaced)".

Anchor-discipline contract:
  - Every flag carries TWO structured anchors (shallow_source +
    deep_source). Both must resolve to substrate text via the corpus
    builder.
  - Role-bound re-extraction: a separate Haiku-class extractor that
    sees only the anchor text + field name (NOT the synthesizer's
    claim) independently produces its own reading. A judge call
    compares the extraction to the synthesizer's claim and votes
    SUPPORTS / CONTRADICTS / UNCLEAR. Failed verification → retry once
    with tightened prompt → second failure → drop flag.

What this module does NOT do:
  - Write thesis verdicts (Stage 2 owns that)
  - Recommend trade direction (operator decides)
  - Touch /brief (panopticon is /research-only)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from research_assistant.claude_sdk import CallResult, ClaudeClient
from research_assistant.deep_reads import InsiderBehaviorDetail
from research_assistant.edgar import (
    InsiderActivitySummary,
    InstitutionalOwnership,
)
from research_assistant.fundamentals import EarningsCalendar, KPISummary
from research_assistant.positioning import AnalystRevisions, OptionsPositioning
from research_assistant.prompts import load_prompt as _load_prompt
from research_assistant.prompts import render as _render
from ozymandias.intelligence.claude_json import parse_claude_response

log = logging.getLogger(__name__)


SCHEMA_VERSION: int = 2
SYNTHESIZER_MODEL: str = "claude-sonnet-4-6"
VERIFIER_MODEL: str = "claude-haiku-4-5-20251001"

# Severity enum. The synthesizer prompt rubric anchors each level:
#   HIGH    — could change swing-trade thesis direction
#   MEDIUM  — could change position sizing / holding-horizon
#   LOW     — informative for confidence rating
SEVERITY_VALUES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")

# Cross-source observation type taxonomy. The synthesizer assigns ONE per
# flag at emit time. Open enum — the prompt suggests these but doesn't
# enforce; the parser accepts any string so novel observation types
# don't get silently dropped.
SUGGESTED_OBSERVATION_TYPES: tuple[str, ...] = (
    "headline_vs_decomposition",
    "prose_vs_numbers",
    "shallow_vs_deep",
    "narrative_vs_positioning",
    "guidance_vs_consensus",
    "insider_vs_management_tone",
    "valuation_vs_fundamentals",
    "term_structure_anomaly",
)

# Verifier verdict enum (from the judge call).
VERIFIER_VERDICTS: tuple[str, ...] = ("SUPPORTS", "CONTRADICTS", "UNCLEAR")

# Forbidden output keys — these are directive-shape leaks the v2 schema
# explicitly excludes. If the model emits any of these, parse_flag()
# raises so the verifier doesn't waste calls on directive-shaped output.
_FORBIDDEN_FLAG_KEYS: frozenset[str] = frozenset({
    "thesis_implication",
    "recommendation",
    "verdict",
    "trade_direction",
    "buy_sell",
    "action",
})


@dataclass
class SynthesisFlag:
    """One cross-source observation surfaced by the synthesizer.

    All five fields below are required. None of them carry a directive —
    they describe the observation, where the shallow framing lives,
    where the deeper evidence lives, and how strong the evidence is.

    The verification record `verifier_verdict` is set by `verify_flag()`
    AFTER the synthesizer's call; flags that fail verification twice are
    not added to SynthesisOutput.flags (they're dropped silently — the
    Stage 2 thesis must never see an unverified flag)."""
    severity: str                              # one of SEVERITY_VALUES
    observation_type: str                      # SUGGESTED_OBSERVATION_TYPES or novel
    shallow_source: str                        # anchor label
    shallow_observation: str                   # tone-neutral text
    deep_source: str                           # anchor label
    deep_observation: str                      # tone-neutral text
    # Verification telemetry (set by verify_flag; default UNCLEAR pre-verify)
    verifier_shallow_verdict: str = "UNCLEAR"
    verifier_deep_verdict: str = "UNCLEAR"
    verifier_reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "observation_type": self.observation_type,
            "shallow_source": self.shallow_source,
            "shallow_observation": self.shallow_observation,
            "deep_source": self.deep_source,
            "deep_observation": self.deep_observation,
        }


@dataclass
class SynthesisOutput:
    """Cascade-stage output of the synthesizer.

    `axes_agreed=True` when the synthesizer returned an empty flag list —
    the panopticon's "no surprises here" signal. Surfaced ONLY in
    /scoreboard retrospective analysis (per critic M1 replacement of
    `consensus_unchecked`), NOT in the per-ticker dossier rendering.
    """
    ticker: str
    schema_version: int
    axes_agreed: bool
    flags: list[SynthesisFlag] = field(default_factory=list)
    # Telemetry — populated during synthesize_research()
    synthesizer_cost_usd: float = 0.0
    verifier_cost_usd: float = 0.0
    flags_pre_verification: int = 0
    flags_dropped_verification: int = 0
    refresh_ts: str = ""

    @property
    def total_cost_usd(self) -> float:
        return self.synthesizer_cost_usd + self.verifier_cost_usd

    def stage_2_block(self) -> str:
        """Render for the {synthesis_flags_block} Stage 2 prompt slot.

        Each flag is rendered as:
          [SEVERITY] observation_type
          - shallow (<source>): <text>
          - deep    (<source>): <text>

        Empty case emits a clear "no divergences" sentinel so the Stage 2
        writer never confuses "we ran the synthesizer and it found
        nothing" with "we didn't run the synthesizer at all"."""
        if self.axes_agreed and not self.flags:
            return (
                "(no cross-source divergences surfaced — substrate "
                "axes agreed on this ticker)"
            )
        if not self.flags:
            return "(synthesizer surfaced no verified flags)"
        lines: list[str] = []
        for i, f in enumerate(self.flags, start=1):
            lines.append(f"[{f.severity}] {f.observation_type} (#{i})")
            lines.append(f"  - shallow ({f.shallow_source}): {f.shallow_observation}")
            lines.append(f"  - deep    ({f.deep_source}): {f.deep_observation}")
        return "\n".join(lines)

    @classmethod
    def render_for_prompt(cls, output: Optional["SynthesisOutput"]) -> str:
        """Three-state rendering matching the broader Phase A pattern.

        None         → synthesizer disabled / call failed sentinel
        populated    → stage_2_block (empty case included)
        """
        if output is None:
            return (
                "(synthesis flags unavailable — Stage 1.7 disabled or "
                "synthesizer call failed)"
            )
        return output.stage_2_block()


# ---------------------------------------------------------------------------
# Anchor corpus — substrate label → rendered text
# ---------------------------------------------------------------------------

def build_anchor_corpus(
    *,
    ticker: str,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    insider_activity: Optional[InsiderActivitySummary] = None,
    insider_detail: Optional[InsiderBehaviorDetail] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
    kpi_summary: Optional[KPISummary] = None,
    earnings_calendar: Optional[EarningsCalendar] = None,
    options_positioning: Optional[OptionsPositioning] = None,
    analyst_revisions: Optional[AnalystRevisions] = None,
) -> dict[str, str]:
    """Build a label → rendered-text map for every anchor a flag might cite.

    The verifier consults this corpus to give the role-bound re-extractor
    "the cited anchor's text" — verbatim from the rendered substrate, never
    the synthesizer's paraphrase. Labels missing from the corpus are
    treated as unresolved citations and the flag is rejected outright
    (cheaper than burning a Haiku call on a phantom anchor).

    The label space tracks what the Stage 2 prompt's source-rule allow-
    list documents — synthesizer is taught (via prompt) to use the same
    label vocabulary.
    """
    corpus: dict[str, str] = {}

    # world_state
    corpus["world_state"] = json.dumps(world_state, indent=2)

    # TICKER_DATA — broad catch-all + the rich daily_signals blob ONLY.
    # Scalar sub-keys (recent_return_5d, volume_ratio, price, …) were
    # REMOVED: rendered as bare numbers they cannot support the framing
    # claims the synthesizer wants to attach to them, so the blind
    # role-bound re-extractor always votes CONTRADICTS ("just a number,
    # no context") and every such flag is dropped. The same values live
    # inside daily_signals (with MACD/EMA/ADX/percentile context) and the
    # full TICKER_DATA blob, so no information is lost — only the
    # unverifiable bare-scalar anchors. Framing/interpretive claims MUST
    # cite daily_signals (see the synthesizer prompt's anchor contract).
    corpus["TICKER_DATA"] = json.dumps(ticker_data, indent=2)
    daily_signals = ticker_data.get("daily_signals")
    if daily_signals is not None:
        corpus["TICKER_DATA:daily_signals"] = (
            json.dumps(daily_signals, indent=2)
            if not isinstance(daily_signals, str) else daily_signals
        )

    # headlines — title/publisher/age metadata ONLY (no article body exists
    # in the pipeline). The explicit prefix keeps the role-bound extractor
    # honest: a flag may cite what the headline LITERALLY states, never
    # article narrative/tone/framing the metadata cannot contain.
    for i, h in enumerate(headlines, start=1):
        label = f"yfinance:fetch_news:{ticker}:item_{i}"
        corpus[label] = (
            "HEADLINE METADATA ONLY (no article body):\n"
            + json.dumps(h, indent=2)
        )

    # yfinance:fetch_bars / fetch_quote — point to TICKER_DATA price/return
    corpus[f"yfinance:fetch_bars:{ticker}"] = corpus.get("TICKER_DATA", "")
    corpus[f"yfinance:fetch_quote:{ticker}"] = corpus.get("TICKER_DATA", "")

    # Insider aggregate (edgar:form4:aggregate)
    if insider_activity is not None:
        corpus["edgar:form4:aggregate"] = (
            InsiderActivitySummary.render_for_prompt(insider_activity)
        )

    # Insider behavior detail (Phase A — edgar:form4:behavior_detail)
    if insider_detail is not None:
        corpus["edgar:form4:behavior_detail"] = (
            InsiderBehaviorDetail.render_for_prompt(insider_detail)
        )

    # Institutional ownership (edgar:13f:aggregate)
    if institutional_ownership is not None:
        corpus["edgar:13f:aggregate"] = (
            InstitutionalOwnership.render_for_prompt(institutional_ownership)
        )

    # Phase A substrate — fundamentals
    if kpi_summary is not None:
        rendered = KPISummary.render_for_prompt(kpi_summary)
        corpus["yfinance:financials:annual"] = rendered
        corpus["yfinance:financials:balance"] = rendered
        corpus["yfinance:financials:multiples"] = rendered

    # Phase A — earnings calendar
    if earnings_calendar is not None:
        rendered = EarningsCalendar.render_for_prompt(earnings_calendar)
        corpus["yfinance:calendar:next"] = rendered
        corpus["yfinance:calendar:last"] = rendered

    # Phase A — options positioning (per-expiry + aggregate)
    if options_positioning is not None:
        agg_rendered = OptionsPositioning.render_for_prompt(options_positioning)
        corpus["yfinance:options:aggregate"] = agg_rendered
        for ex in options_positioning.expiries:
            corpus[f"yfinance:options:{ex.expiry}"] = (
                f"expiry {ex.expiry} ({ex.days_to_expiry}d): "
                f"ATM IV "
                f"{('%.1f%%' % (ex.atm_iv * 100)) if ex.atm_iv else '?'}, "
                f"P_OI {ex.put_oi} / C_OI {ex.call_oi}, "
                f"P_vol {ex.put_volume} / C_vol {ex.call_volume}"
                + (
                    f", unusual {ex.unusual_oi_side} strike "
                    f"${ex.unusual_oi_strike:.0f} ({ex.unusual_oi_multiple:.1f}x)"
                    if ex.unusual_oi_strike is not None else ""
                )
            )

    # Phase A — analyst revisions
    if analyst_revisions is not None:
        rendered = AnalystRevisions.render_for_prompt(analyst_revisions)
        corpus["yfinance:recommendations:aggregate"] = rendered

    # no_fetch — explicit sentinel; the synthesizer should NEVER cite this
    # (verifier rejects), but the source rule documents the label, so
    # include it for completeness.
    corpus["no_fetch"] = "(no_fetch is not a valid synthesizer anchor)"

    return corpus


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------

def parse_synthesis_output(payload: dict, *, default_ticker: str) -> SynthesisOutput:
    """Parse synthesizer JSON into a SynthesisOutput.

    Rejects payloads with forbidden directive keys via _FORBIDDEN_FLAG_KEYS
    (the v2 schema doesn't allow `thesis_implication`, `recommendation`,
    or other verdict-shape leaks).

    Required top-level fields: `axes_agreed` (bool), `flags` (list).
    Each flag must carry the six required v2 schema fields. Missing
    fields raise ValueError so the caller surfaces a parse failure
    rather than emit a malformed flag.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"synthesizer output must be a JSON object; got {type(payload).__name__}"
        )

    ticker = str(payload.get("ticker") or default_ticker).strip().upper()
    if not ticker:
        raise ValueError("synthesizer output missing required field: ticker")

    schema_version = int(payload.get("schema_version") or SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        log.warning(
            "synthesizer schema_version %d; expected %d (parsing leniently)",
            schema_version, SCHEMA_VERSION,
        )

    raw_axes = payload.get("axes_agreed")
    if not isinstance(raw_axes, bool):
        raise ValueError(
            f"synthesizer output `axes_agreed` must be bool; got {raw_axes!r}"
        )

    raw_flags = payload.get("flags")
    if raw_flags is None:
        raw_flags = []
    if not isinstance(raw_flags, list):
        raise ValueError(
            f"synthesizer output `flags` must be a list; got {type(raw_flags).__name__}"
        )

    flags: list[SynthesisFlag] = []
    for i, raw in enumerate(raw_flags):
        try:
            flags.append(parse_flag(raw))
        except ValueError as exc:
            raise ValueError(f"flag[{i}] invalid: {exc}") from exc

    # Sanity: axes_agreed=True with non-empty flags is internally
    # inconsistent. The synthesizer was instructed to emit one or the
    # other. Raise to force a retry rather than silently accept.
    if raw_axes and flags:
        raise ValueError(
            "synthesizer output internally inconsistent: "
            "axes_agreed=true but flags is non-empty"
        )

    return SynthesisOutput(
        ticker=ticker,
        schema_version=schema_version,
        axes_agreed=raw_axes,
        flags=flags,
    )


def parse_flag(raw: dict) -> SynthesisFlag:
    """Parse one flag entry.

    Rejects forbidden directive keys (per _FORBIDDEN_FLAG_KEYS); raises
    on missing required fields; coerces severity to uppercase + validates
    against the enum.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"flag must be a dict; got {type(raw).__name__}")

    forbidden = _FORBIDDEN_FLAG_KEYS & raw.keys()
    if forbidden:
        raise ValueError(
            f"flag contains forbidden directive keys {sorted(forbidden)}; "
            f"v2 synthesizer schema is evidence-only"
        )

    required = ("severity", "observation_type", "shallow_source",
                "shallow_observation", "deep_source", "deep_observation")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"flag missing required fields: {missing}")

    severity = str(raw["severity"]).strip().upper()
    if severity not in SEVERITY_VALUES:
        raise ValueError(
            f"flag severity must be one of {SEVERITY_VALUES}; got {raw['severity']!r}"
        )

    return SynthesisFlag(
        severity=severity,
        observation_type=str(raw["observation_type"]).strip(),
        shallow_source=str(raw["shallow_source"]).strip(),
        shallow_observation=str(raw["shallow_observation"]).strip(),
        deep_source=str(raw["deep_source"]).strip(),
        deep_observation=str(raw["deep_observation"]).strip(),
    )


# ---------------------------------------------------------------------------
# Synthesizer call (Sonnet)
# ---------------------------------------------------------------------------

async def _stage_1_7_synthesizer(
    client: ClaudeClient,
    *,
    ticker: str,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    insider_activity: Optional[InsiderActivitySummary],
    insider_detail: Optional[InsiderBehaviorDetail],
    institutional_ownership: Optional[InstitutionalOwnership],
    kpi_summary: Optional[KPISummary],
    earnings_calendar: Optional[EarningsCalendar],
    options_positioning: Optional[OptionsPositioning],
    analyst_revisions: Optional[AnalystRevisions],
    extra_instructions: str = "",
) -> tuple[Optional[SynthesisOutput], Optional[CallResult]]:
    """Single Sonnet call: synthesizer reads all substrate blocks and
    emits the v2 schema JSON. Returns (parsed, call_metadata).

    `extra_instructions` is appended to the system prompt for retry
    flows — the verify-then-retry path uses it to name which flag
    failed and why.
    """
    template = _load_prompt("stage_1_7_synthesizer")
    prompt = _render(
        template,
        ticker=ticker,
        ticker_json=json.dumps(ticker_data, indent=2),
        headlines_json=json.dumps(headlines, indent=2),
        insider_activity_block=InsiderActivitySummary.render_for_prompt(insider_activity),
        insider_detail_block=InsiderBehaviorDetail.render_for_prompt(insider_detail),
        institutional_ownership_block=InstitutionalOwnership.render_for_prompt(institutional_ownership),
        fundamentals_block=KPISummary.render_for_prompt(kpi_summary),
        earnings_calendar_block=EarningsCalendar.render_for_prompt(earnings_calendar),
        options_positioning_block=OptionsPositioning.render_for_prompt(options_positioning),
        analyst_revisions_block=AnalystRevisions.render_for_prompt(analyst_revisions),
    )
    base_system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    if extra_instructions:
        base_system += "\n\nADDITIONAL INSTRUCTIONS (retry):\n" + extra_instructions
    result = await client.call(prompt, model=SYNTHESIZER_MODEL, system=base_system)
    raw = parse_claude_response(result.text)
    if raw is None:
        log.warning("synthesizer JSON parse failed for %s", ticker)
        return None, result
    try:
        out = parse_synthesis_output(raw, default_ticker=ticker)
    except ValueError as exc:
        log.warning("synthesizer schema-validate failed for %s: %s", ticker, exc)
        return None, result
    return out, result


# ---------------------------------------------------------------------------
# Role-bound verifier (Haiku)
# ---------------------------------------------------------------------------

async def _verify_one_anchor(
    client: ClaudeClient,
    *,
    ticker: str,
    anchor_label: str,
    anchor_text: str,
    field_name: str,
    synthesizer_observation: str,
) -> tuple[str, str, float]:
    """Two-step role-bound verification of ONE anchor.

    Step 1 (re-extract): Haiku sees ONLY the anchor text + ticker + field
    name. It produces its own independent reading of what the anchor
    says about that field. CRITICALLY: it does NOT see the synthesizer's
    observation. This is the role-binding that protects against
    paraphrase-gaming (critic C2).

    Step 2 (judge): Haiku sees the extractor's independent reading +
    the synthesizer's observation. Emits SUPPORTS / CONTRADICTS / UNCLEAR.

    Returns (verdict, reasoning, cost_usd). UNCLEAR on parse / call
    failure — conservative: when verification couldn't run cleanly the
    flag is treated as unverified.
    """
    cost = 0.0

    # Step 1 — re-extract
    extract_template = _load_prompt("stage_1_7_reextract")
    extract_prompt = _render(
        extract_template,
        ticker=ticker,
        anchor_label=anchor_label,
        anchor_text=anchor_text,
        field_name=field_name,
    )
    try:
        extract_result = await client.call(
            extract_prompt, model=VERIFIER_MODEL, max_tokens=512,
        )
        cost += extract_result.cost_usd
    except Exception as exc:
        log.warning("verifier: re-extract failed (%s): %s", anchor_label, exc)
        return "UNCLEAR", f"re-extract call failed: {exc}", cost
    extracted = extract_result.text.strip()
    if not extracted:
        return "UNCLEAR", "re-extractor returned empty text", cost

    # Step 2 — judge
    judge_template = _load_prompt("stage_1_7_judge")
    judge_prompt = _render(
        judge_template,
        ticker=ticker,
        anchor_label=anchor_label,
        field_name=field_name,
        extractor_reading=extracted,
        synthesizer_observation=synthesizer_observation,
    )
    try:
        judge_result = await client.call(
            judge_prompt, model=VERIFIER_MODEL, max_tokens=512,
        )
        cost += judge_result.cost_usd
    except Exception as exc:
        log.warning("verifier: judge failed (%s): %s", anchor_label, exc)
        return "UNCLEAR", f"judge call failed: {exc}", cost
    parsed = parse_claude_response(judge_result.text)
    if not isinstance(parsed, dict):
        return "UNCLEAR", "judge returned non-JSON", cost
    verdict = str(parsed.get("verdict") or "").strip().upper()
    reasoning = str(parsed.get("reasoning") or "").strip()
    if verdict not in VERIFIER_VERDICTS:
        return "UNCLEAR", f"judge returned invalid verdict: {verdict!r}", cost
    return verdict, reasoning, cost


async def verify_flag(
    client: ClaudeClient,
    flag: SynthesisFlag,
    corpus: dict[str, str],
    *,
    ticker: str,
) -> tuple[bool, float]:
    """Verify both anchors of a flag.

    Returns (passed, cost_usd). `passed = True` only when BOTH anchors'
    judge verdicts are SUPPORTS. CONTRADICTS on either side → fail.
    UNCLEAR on either side → fail (conservative).

    Mutates `flag` in place to record the per-anchor verdict + reasoning
    so the trace event can persist it.
    """
    cost = 0.0
    shallow_text = corpus.get(flag.shallow_source)
    deep_text = corpus.get(flag.deep_source)
    if shallow_text is None or deep_text is None:
        missing = []
        if shallow_text is None:
            missing.append(flag.shallow_source)
        if deep_text is None:
            missing.append(flag.deep_source)
        flag.verifier_reasoning = (
            f"unresolved anchor(s) in corpus: {missing}"
        )
        flag.verifier_shallow_verdict = "UNCLEAR"
        flag.verifier_deep_verdict = "UNCLEAR"
        return False, cost

    # Verify both anchors. Could run in parallel; for v1 we serialize so
    # rate-limit behaviour is easy to reason about (Haiku is cheap so
    # latency hit is small).
    shallow_verdict, shallow_reason, shallow_cost = await _verify_one_anchor(
        client,
        ticker=ticker,
        anchor_label=flag.shallow_source,
        anchor_text=shallow_text,
        field_name="shallow_observation",
        synthesizer_observation=flag.shallow_observation,
    )
    cost += shallow_cost
    deep_verdict, deep_reason, deep_cost = await _verify_one_anchor(
        client,
        ticker=ticker,
        anchor_label=flag.deep_source,
        anchor_text=deep_text,
        field_name="deep_observation",
        synthesizer_observation=flag.deep_observation,
    )
    cost += deep_cost

    flag.verifier_shallow_verdict = shallow_verdict
    flag.verifier_deep_verdict = deep_verdict
    flag.verifier_reasoning = (
        f"shallow: {shallow_reason} | deep: {deep_reason}"
    )
    passed = shallow_verdict == "SUPPORTS" and deep_verdict == "SUPPORTS"
    return passed, cost


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def synthesize_research(
    client: ClaudeClient,
    *,
    ticker: str,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    insider_activity: Optional[InsiderActivitySummary] = None,
    insider_detail: Optional[InsiderBehaviorDetail] = None,
    institutional_ownership: Optional[InstitutionalOwnership] = None,
    kpi_summary: Optional[KPISummary] = None,
    earnings_calendar: Optional[EarningsCalendar] = None,
    options_positioning: Optional[OptionsPositioning] = None,
    analyst_revisions: Optional[AnalystRevisions] = None,
) -> Optional[SynthesisOutput]:
    """End-to-end Stage 1.7: synthesizer → role-bound verify → output.

    Pipeline:
      1. Sonnet synthesizer call → SynthesisOutput (or None on parse fail)
      2. Build anchor corpus from substrate
      3. For each flag: verify both anchors via Haiku (re-extract + judge)
      4. Drop flags that failed verification (recording the per-anchor
         verdicts onto the flag for telemetry). No retry: a flag that
         fails under the (fixed) anchor contract is failing for a
         legitimate reason, and re-running the whole synthesizer to chase
         it is negative-value (it was the dominant cost on early runs).
      5. Return final SynthesisOutput with verified flags only

    Returns None on hard parse/call failure (Stage 2 prompt block then
    renders "synthesis flags unavailable").
    """
    refresh_ts = datetime.now(timezone.utc).isoformat()
    corpus = build_anchor_corpus(
        ticker=ticker,
        world_state=world_state,
        ticker_data=ticker_data,
        headlines=headlines,
        insider_activity=insider_activity,
        insider_detail=insider_detail,
        institutional_ownership=institutional_ownership,
        kpi_summary=kpi_summary,
        earnings_calendar=earnings_calendar,
        options_positioning=options_positioning,
        analyst_revisions=analyst_revisions,
    )

    output, syn_meta = await _stage_1_7_synthesizer(
        client,
        ticker=ticker,
        world_state=world_state,
        ticker_data=ticker_data,
        headlines=headlines,
        insider_activity=insider_activity,
        insider_detail=insider_detail,
        institutional_ownership=institutional_ownership,
        kpi_summary=kpi_summary,
        earnings_calendar=earnings_calendar,
        options_positioning=options_positioning,
        analyst_revisions=analyst_revisions,
    )
    if output is None:
        return None
    output.refresh_ts = refresh_ts
    output.synthesizer_cost_usd = syn_meta.cost_usd if syn_meta else 0.0
    output.flags_pre_verification = len(output.flags)

    if output.axes_agreed:
        # Synthesizer voluntarily reported no divergences. Nothing to
        # verify. Empty flags array carries through.
        return output

    verified: list[SynthesisFlag] = []
    verifier_cost = 0.0
    for flag in output.flags:
        passed, cost = await verify_flag(client, flag, corpus, ticker=ticker)
        verifier_cost += cost
        if passed:
            verified.append(flag)
            continue
        # Failed verification → drop. The per-anchor verdicts + reasoning
        # are already mutated onto `flag` by verify_flag; we log them so
        # the drop is diagnosable (and Phase C replay can hand-grade it).
        log.info(
            "synthesizer: flag dropped (severity=%s, shallow=%s[%s], "
            "deep=%s[%s]): %s",
            flag.severity,
            flag.shallow_source, flag.verifier_shallow_verdict,
            flag.deep_source, flag.verifier_deep_verdict,
            flag.verifier_reasoning,
        )

    output.verifier_cost_usd = verifier_cost
    output.flags_dropped_verification = (
        output.flags_pre_verification - len(verified)
    )
    output.flags = verified
    # axes_agreed REMAINS the synthesizer's original report — it is a
    # statement about substrate alignment at synthesis time, not about
    # what survived verification. If verification dropped everything,
    # axes_agreed stays False (because the synthesizer thought it saw
    # divergences); the prompt block reads "no verified flags" via the
    # render path, distinct from the "axes agreed" message.
    return output
