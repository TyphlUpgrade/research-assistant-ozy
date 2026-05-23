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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from research_assistant.claude_sdk import CallResult, ClaudeClient
from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    read_dossier,
    write_dossier_atomic,
)
from research_assistant.edgar import InsiderActivitySummary
from research_assistant.observations import Observation, append_observation
from research_assistant.trace_renderer import append_stage_event
from ozymandias.intelligence.claude_json import parse_claude_response

log = logging.getLogger(__name__)


# Prompt file locations relative to research-repo root
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "research-v1.0.0"


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


def _load_prompt(stem: str) -> str:
    """Load a research-v1.0.0 prompt template (text with {placeholder} slots)."""
    path = PROMPTS_DIR / f"{stem}.txt"
    return path.read_text()


def _render(template: str, **subs: Any) -> str:
    """
    Substitute `{key}` placeholders in a prompt template.

    Single-pass via regex so that values containing `{other_key}` strings
    (e.g. persisted user content that survives into a future prompt's
    dossier_context block) are NOT re-scanned for further substitution.
    Only `{key}` for `key` in `subs` is substituted; other brace-wrapped
    content (e.g. JSON schema examples like `{ "ticker": "<symbol>", ... }`
    or unmatched placeholders) is preserved verbatim.
    """
    if not subs:
        return template
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in subs) + r")\}")
    return pattern.sub(lambda m: str(subs[m.group(1)]), template)


def _format_insider_activity_block(
    insider_activity: Optional[InsiderActivitySummary],
) -> str:
    """Render an InsiderActivitySummary into the {insider_activity_block}
    slot. Distinguishes "no data available" (None — EDGAR fetch failed or
    ticker not in SEC universe) from "no activity in window" (empty
    summary) so Stage 2 can weight the signal appropriately."""
    if insider_activity is None:
        return "(insider activity unavailable — EDGAR fetch failed or ticker not in SEC universe)"
    if insider_activity.total_filings == 0:
        return f"(no Form 4 filings last {insider_activity.window_days}d)"
    return insider_activity.stage_2_block()


async def _stage_2_thesis(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    stage_1_result: dict,
    headlines: list[dict],
    insider_activity: Optional[InsiderActivitySummary] = None,
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
        insider_activity_block=_format_insider_activity_block(insider_activity),
    )
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    result = await client.call(prompt, model="claude-sonnet-4-6", system=system)
    return parse_claude_response(result.text), result


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


def _chain_id() -> str:
    """Generate a chain ID for the cascade trace (date + epoch-ms hex)."""
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{int(now.timestamp()*1000) & 0xFFFFFF:06x}"


async def research_ticker(
    symbol: str,
    *,
    world_state: dict,
    ticker_data: dict,
    headlines: list[dict],
    base: Path,
    client: Optional[ClaudeClient] = None,
    insider_activity: Optional[InsiderActivitySummary] = None,
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
) -> ProbeResult:
    """Run a focused probe against an existing dossier.

    Raises FileNotFoundError if no dossier exists for `symbol` — probe is the
    cold-start entry point against a SAVED dossier, not a way to create one.
    Use `/research <TICKER>` first if no dossier is present.

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
        client, world_state, ticker_data, headlines, dossier_context, question
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


def _flatten_anchors_to_corpus(anchors: list) -> str:
    """Join {claim, source} dicts (or bare strings) into one lowercased blob
    suitable for substring containment checks."""
    parts: list[str] = []
    for a in anchors or []:
        if isinstance(a, dict):
            parts.extend(str(v) for v in a.values())
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
