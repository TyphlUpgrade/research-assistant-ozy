"""
Tests for the PR 2A.3 inline Stage-2 Skeptic adversarial check.

Covers:
  - Multiplier table semantics (AGREE/WEAKEN/STRONG_OBJECTION → composite math)
  - Verdict enum validation (unknown values → UNAVAILABLE graceful degrade)
  - Prompt-rendering data-isolation: Skeptic must NOT see ticker_data,
    headlines, observation, screener evidence — only bull/bear anchors +
    composite conviction.
  - Graceful degrade on API / parse failure
  - Trace JSONL event emitted with stage_id == "stage_2_skeptic_check" and
    cost_usd > 0 on success.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from research_assistant.orchestrator import (
    SKEPTIC_ADJUSTMENT_MULTIPLIERS,
    SKEPTIC_VERDICTS,
    Stage2Note,
    _stage_2_skeptic_check,
    compute_composite_conviction,
)
from research_assistant.prompts import load_prompt as _load_prompt
from research_assistant.prompts import render as _render


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _sample_note(
    *,
    bull_anchor: str = "DC revenue +27% QoQ aligns with bull-trending regime",
    bear_anchor: str = "Weekly RSI 65 + recent 5d return +12% — chase risk",
    conviction: Optional[dict[str, float]] = None,
) -> Stage2Note:
    conviction = conviction or {
        "technical": 0.55,
        "fundamental": 0.70,
        "catalyst": 0.40,
        "regime": 0.75,
    }
    return Stage2Note(
        ticker="NVDA",
        observation=(
            "NVDA up 27% on 30d basis with weekly RSI 65",
            "Form 4 net flow last 90d: -$3.5M / 1 sale / 0 buys",
        ),
        bull_anchor=bull_anchor,
        bear_anchor=bear_anchor,
        what_would_change=("RSI breaks below 55 on weekly close",),
        conviction=conviction,
        composite_conviction=compute_composite_conviction(conviction),
        decision_tag="WATCH",
    )


class _FakeCallResult:
    """Mimics CallResult from research_assistant.claude_sdk."""

    def __init__(self, text: str, *, cost_usd: float = 0.0123) -> None:
        self.text = text
        self.input_tokens = 220
        self.output_tokens = 40
        self.cost_usd = cost_usd
        self.latency_ms = 412
        self.model = "claude-sonnet-4-6"


class _FakeClient:
    """Captures the rendered prompt and replies with a canned text."""

    def __init__(self, reply_text: str, *, cost_usd: float = 0.0123) -> None:
        self._reply_text = reply_text
        self._cost_usd = cost_usd
        self.last_prompt: Optional[str] = None
        self.last_model: Optional[str] = None
        self.last_system: Optional[str] = None

    async def call(self, prompt: str, *, model: str, system: Optional[str] = None):
        self.last_prompt = prompt
        self.last_model = model
        self.last_system = system
        return _FakeCallResult(self._reply_text, cost_usd=self._cost_usd)


class _RaisingClient:
    """Raises on every call — simulates network / API outage."""

    async def call(self, prompt: str, *, model: str, system: Optional[str] = None):
        raise RuntimeError("simulated network outage")


# ---------------------------------------------------------------------------
# Multiplier semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skeptic_agree_leaves_composite_unchanged(tmp_path: Path) -> None:
    note = _sample_note()
    client = _FakeClient(json.dumps({"verdict": "AGREE", "reasoning": "agreed"}))
    verdict, reasoning, adjusted = await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-aaaaaa",
        traces_base=tmp_path,
    )
    assert verdict == "AGREE"
    assert reasoning == "agreed"
    assert adjusted == pytest.approx(note.composite_conviction)


@pytest.mark.asyncio
async def test_skeptic_weaken_reduces_composite_by_15_pct(tmp_path: Path) -> None:
    note = _sample_note()
    client = _FakeClient(json.dumps({
        "verdict": "WEAKEN",
        "reasoning": "Bear anchor partially undercuts the bull (RSI extension is non-trivial).",
    }))
    _, _, adjusted = await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-bbbbbb",
        traces_base=tmp_path,
    )
    assert adjusted == pytest.approx(note.composite_conviction * 0.85)


@pytest.mark.asyncio
async def test_skeptic_strong_objection_reduces_composite_by_35_pct(
    tmp_path: Path,
) -> None:
    note = _sample_note()
    client = _FakeClient(json.dumps({
        "verdict": "STRONG_OBJECTION",
        "reasoning": "Bear anchor materially stronger: RSI 65 + 5d +12% is chase territory.",
    }))
    _, _, adjusted = await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-cccccc",
        traces_base=tmp_path,
    )
    assert adjusted == pytest.approx(note.composite_conviction * 0.65)


# ---------------------------------------------------------------------------
# Verdict enum validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skeptic_verdict_enum_validates(tmp_path: Path) -> None:
    """Unknown verdict → UNAVAILABLE graceful-degrade path. The Skeptic
    MUST NOT short-circuit by emitting UNAVAILABLE itself either — if it
    tries, treat as parse failure."""
    note = _sample_note()
    client = _FakeClient(json.dumps({"verdict": "MAYBE", "reasoning": "dunno"}))
    verdict, reasoning, adjusted = await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-dddddd",
        traces_base=tmp_path,
    )
    assert verdict == "UNAVAILABLE"
    assert adjusted == pytest.approx(note.composite_conviction)


def test_skeptic_verdicts_enum_includes_unavailable() -> None:
    """Sentinel: the operator-visible UNAVAILABLE state is part of the
    documented verdict enum (so callers reading the SKEPTIC_VERDICTS tuple
    see the graceful-degrade option)."""
    assert "UNAVAILABLE" in SKEPTIC_VERDICTS
    for verdict in ("AGREE", "WEAKEN", "STRONG_OBJECTION"):
        assert verdict in SKEPTIC_VERDICTS


def test_skeptic_multiplier_table_pins_adjustment_math() -> None:
    """Multiplier table is the single source of truth for the conviction
    adjustment math. If anyone retunes these values, this assertion
    forces an intentional update (and matches the prompt's documented
    semantics)."""
    assert SKEPTIC_ADJUSTMENT_MULTIPLIERS["AGREE"] == pytest.approx(1.00)
    assert SKEPTIC_ADJUSTMENT_MULTIPLIERS["WEAKEN"] == pytest.approx(0.85)
    assert SKEPTIC_ADJUSTMENT_MULTIPLIERS["STRONG_OBJECTION"] == pytest.approx(0.65)
    assert SKEPTIC_ADJUSTMENT_MULTIPLIERS["UNAVAILABLE"] == pytest.approx(1.00)


# ---------------------------------------------------------------------------
# Prompt data-isolation regression sentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skeptic_prompt_only_includes_anchors_not_full_data(
    tmp_path: Path,
) -> None:
    """Critical contract: the Skeptic sees ONLY bull_anchor + bear_anchor.
    NOT composite_conviction (PR 2A.7 — was giving the model an "already
    priced upstream" escape valve to reflexively AGREE), NOT observation,
    NOT ticker_data, NOT headlines, NOT screener evidence. If Skeptic had
    full data it would just be a second Stage 2 — defeating the structural
    point of an adversarial pass."""
    note = _sample_note()
    client = _FakeClient(json.dumps({"verdict": "AGREE", "reasoning": "agreed"}))
    await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-eeeeee",
        traces_base=tmp_path,
    )
    prompt = client.last_prompt or ""

    # ALLOWED: the two anchors only.
    assert note.bull_anchor in prompt
    assert note.bear_anchor in prompt
    # FORBIDDEN (PR 2A.7): composite_conviction must not appear — see
    # docstring above.
    assert f"{note.composite_conviction:.4f}" not in prompt

    # FORBIDDEN: full-data leakage. Each of these should NOT appear in the
    # rendered prompt.
    for forbidden in (
        "observation",          # the observation list itself
        "ticker_data",          # raw market data block name
        "headlines",            # news block name
        "screener_evidence",    # screener evidence block name
        "insider_activity",     # Form 4 block name
        "Form 4 net flow",      # actual observation text content
        "weekly RSI 65 + recent",  # observation sentence echo
        "what_would_change",    # trigger list
        "decision_tag",         # the upstream Stage 2 enum
        "WORLD_STATE",          # Stage 0 / system block leakage
    ):
        assert forbidden not in prompt, (
            f"Skeptic prompt must NOT include {forbidden!r}; "
            f"prompt was:\n{prompt[:2000]}"
        )
    # System block: orchestrator does NOT pass system context to the Skeptic
    # call (just `client.call(prompt, model=...)`), so the system slot must
    # stay None — no world_state leakage.
    assert client.last_system is None


def test_skeptic_prompt_template_does_not_carry_ticker_data_placeholder() -> None:
    """Render-only sanity check: the raw template has no `{ticker_json}`,
    `{headlines_json}`, `{observation}` placeholder. The structural
    guarantee is that the template literally cannot accept those slots."""
    template = _load_prompt("stage_2_skeptic_check")
    for forbidden_slot in (
        "{ticker_json}",
        "{headlines_json}",
        "{insider_activity_block}",
        "{screener_evidence_block}",
        "{observation}",
        "{what_would_change}",
        "{ticker_data}",
    ):
        assert forbidden_slot not in template, (
            f"Skeptic prompt template must not carry {forbidden_slot!r}; "
            f"that would let a future caller wire full data into it."
        )


# ---------------------------------------------------------------------------
# Graceful degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skeptic_failure_graceful_degrade(tmp_path: Path) -> None:
    """API failure: Stage2Note keeps the original composite + verdict
    flips to UNAVAILABLE + reasoning explains the failure. The brief MUST
    still ship; this is the operator's primary cost-vs-value contract."""
    note = _sample_note()
    verdict, reasoning, adjusted = await _stage_2_skeptic_check(
        _RaisingClient(), note,
        chain_id="20260525T000000-ffffff",
        traces_base=tmp_path,
    )
    assert verdict == "UNAVAILABLE"
    assert "failed" in reasoning.lower()
    assert adjusted == pytest.approx(note.composite_conviction)


@pytest.mark.asyncio
async def test_skeptic_invalid_json_graceful_degrade(tmp_path: Path) -> None:
    """The model emits non-JSON garbage. Same graceful-degrade path."""
    note = _sample_note()
    client = _FakeClient("not even close to JSON, just prose")
    verdict, reasoning, adjusted = await _stage_2_skeptic_check(
        client, note,
        chain_id="20260525T000000-111111",
        traces_base=tmp_path,
    )
    assert verdict == "UNAVAILABLE"
    assert adjusted == pytest.approx(note.composite_conviction)
    # The reasoning should explain the failure (either parse-fail or generic).
    assert reasoning  # non-empty


# ---------------------------------------------------------------------------
# Trace event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skeptic_trace_event_logged(tmp_path: Path) -> None:
    """Verify the trace JSONL gets a `stage_2_skeptic_check` event with
    cost_usd > 0 on the success path. Distinct stage_id from Stage 3
    Skeptic (which is `stage_3_skeptic`) so /trace can distinguish the
    brief-mode inline pass from the /research-mode full critique."""
    note = _sample_note()
    chain_id = "20260525T120000-deadbeef"
    client = _FakeClient(
        json.dumps({"verdict": "WEAKEN", "reasoning": "Bear anchor stronger than acknowledged."}),
        cost_usd=0.0234,
    )
    await _stage_2_skeptic_check(
        client, note,
        chain_id=chain_id,
        traces_base=tmp_path,
    )

    # The trace renderer writes under <traces_base>/<YYYY-MM-DD>/<chain_id>.jsonl
    jsonl_paths = list(tmp_path.rglob(f"*{chain_id}*.jsonl"))
    assert len(jsonl_paths) == 1, f"Expected 1 trace file, got {jsonl_paths}"
    lines = [
        json.loads(line)
        for line in jsonl_paths[0].read_text().splitlines()
        if line.strip()
    ]
    skeptic_events = [e for e in lines if e.get("stage_id") == "stage_2_skeptic_check"]
    assert len(skeptic_events) == 1
    ev = skeptic_events[0]
    assert ev["chain_id"] == chain_id
    assert ev["symbol"] == note.ticker
    assert ev["cost_usd"] > 0
    assert ev["model"] == "claude-sonnet-4-6"
    assert ev["parsed"]["verdict"] == "WEAKEN"
    # composite_pre vs composite_post pinning so /trace can audit the math
    assert ev["parsed"]["composite_pre"] == pytest.approx(note.composite_conviction)
    assert ev["parsed"]["composite_post"] == pytest.approx(
        note.composite_conviction * 0.85
    )
    assert ev["parsed"]["multiplier"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_skeptic_trace_event_on_failure_still_emitted(tmp_path: Path) -> None:
    """Failure path still writes a trace event — operator must see the
    Skeptic was attempted even when it failed (otherwise the cost ledger
    silently lies)."""
    note = _sample_note()
    chain_id = "20260525T120000-faceface"
    await _stage_2_skeptic_check(
        _RaisingClient(), note,
        chain_id=chain_id,
        traces_base=tmp_path,
    )
    jsonl_paths = list(tmp_path.rglob(f"*{chain_id}*.jsonl"))
    assert len(jsonl_paths) == 1
    lines = [
        json.loads(line)
        for line in jsonl_paths[0].read_text().splitlines()
        if line.strip()
    ]
    ev = next(e for e in lines if e.get("stage_id") == "stage_2_skeptic_check")
    assert ev["error"]  # error string set
    assert ev["parsed"]["verdict"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Dataclass extension — backward compat (default values for new fields)
# ---------------------------------------------------------------------------


def test_stage2note_defaults_skeptic_fields_for_backward_compat() -> None:
    """Pre-PR-2A.3 callers that construct a Stage2Note without skeptic_*
    keyword arguments still get a valid object — UNAVAILABLE / "" / None.
    Pin defaults so a future signature change can't silently break the
    backward-compat cache loader in cli._brief_item_from_cache."""
    conviction = {
        "technical": 0.55, "fundamental": 0.70, "catalyst": 0.40, "regime": 0.75,
    }
    note = Stage2Note(
        ticker="NVDA",
        observation=("obs1",),
        bull_anchor="bull",
        bear_anchor="bear",
        what_would_change=("trigger1",),
        conviction=conviction,
        composite_conviction=compute_composite_conviction(conviction),
        decision_tag="WATCH",
    )
    assert note.skeptic_verdict == "UNAVAILABLE"
    assert note.skeptic_reasoning == ""
    assert note.composite_conviction_pre_skeptic is None
