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

from research_assistant.claude_sdk import ClaudeClient
from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    read_dossier,
    write_dossier_atomic,
)
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

    Uses literal `.replace()` rather than `str.format()` so that JSON schema
    examples inside the prompt body (e.g. `{ "ticker": "<symbol>", ... }`)
    don't get interpreted as format substitution fields and KeyError.
    Only the named placeholders in `subs` are substituted; everything else
    in the template is preserved verbatim.
    """
    result = template
    for key, value in subs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


async def _stage_2_thesis(
    client: ClaudeClient,
    world_state: dict,
    ticker_data: dict,
    stage_1_result: dict,
    headlines: list[dict],
) -> Optional[dict]:
    """Invoke Stage 2 (Sonnet thesis). Returns parsed JSON or None on parse failure."""
    template = _load_prompt("stage_2_thesis")
    prompt = _render(
        template,
        ticker_json=json.dumps(ticker_data, indent=2),
        stage_1_json=json.dumps(stage_1_result, indent=2),
        headlines_json=json.dumps(headlines, indent=2),
    )
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    raw = await client.call(prompt, model="claude-sonnet-4-6", system=system)
    return parse_claude_response(raw)


async def _stage_3_skeptic(
    client: ClaudeClient,
    world_state: dict,
    thesis_with_ticker_data: dict,
    model: str = "claude-sonnet-4-6",
) -> Optional[dict]:
    """Invoke Stage 3 (Skeptic). Returns parsed JSON or None on parse failure."""
    template = _load_prompt("stage_3_skeptic")
    prompt = _render(template, thesis_json=json.dumps(thesis_with_ticker_data, indent=2))
    system = f"WORLD_STATE for this session:\n{json.dumps(world_state, indent=2)}"
    raw = await client.call(prompt, model=model, system=system)
    return parse_claude_response(raw)


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
    stage_2 = await _stage_2_thesis(
        client, world_state, ticker_data, stage_1_placeholder, headlines
    )
    if stage_2 is None:
        raise RuntimeError(f"Stage 2 JSON parse failed for {symbol}")

    # Stage 3 — supply Stage 2 + ticker_data for momentum/exhaustion fields
    thesis_with_data = dict(stage_2)
    thesis_with_data["ticker_data"] = ticker_data
    stage_3 = await _stage_3_skeptic(client, world_state, thesis_with_data)
    if stage_3 is None:
        raise RuntimeError(f"Stage 3 JSON parse failed for {symbol}")

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
# Defender invocation heuristic (3-condition AND, per Critic iter1 #11)
# ---------------------------------------------------------------------------

_DISAGREEMENT_RE = re.compile(
    r"\b(i\s+disagree|i\s+don'?t\s+think|you'?re\s+wrong|that'?s\s+wrong|"
    r"no,?\s+i|that'?s\s+(too|overly)|stop\s+being)\b",
    re.IGNORECASE,
)
_EVIDENCE_RE = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}\b|"           # ISO date
    r"\bQ[1-4]\s+(FY|20)\d{2,4}\b|"      # earnings call ref
    r"\bper\s+the\b|\baccording\s+to\b|"  # citation markers
    r"\b10-?[QK]\b|\bfiling\b|\btranscript\b|"  # filing refs
    r"\$\d|\d+\s*%|\bbps\b)",            # numeric/financial
    re.IGNORECASE,
)


def should_invoke_defender(prior_turn_had_recommendation: bool, user_message: str) -> bool:
    """
    3-condition AND per Critic iter1 #11:
    1. Prior turn produced a Recommendation (caller tracks this)
    2. User message expresses disagreement (regex match)
    3. User message contains NO evidence markers (regex non-match)
    """
    if not prior_turn_had_recommendation:
        return False
    if not _DISAGREEMENT_RE.search(user_message):
        return False
    if _EVIDENCE_RE.search(user_message):
        return False  # has evidence — let the orchestrator handle it normally
    return True
