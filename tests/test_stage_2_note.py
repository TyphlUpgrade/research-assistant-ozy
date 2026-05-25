"""
Tests for the PR 2A.2 Stage 2 structured-note path.

Covers:
  - Stage2Note dataclass round-trip (construct → serialize → deserialize)
  - Composite conviction math (geometric mean; sanity at 0.5 across dims)
  - parse_stage2_note happy path
  - parse_stage2_note missing-required-field / bad-type error handling
  - parse_stage2_note collapses anchor-as-list with a WARN log
  - decision_tag enum validation (5 allowed values, others raise)
  - Prompt-rendering regression sentinels for the data-isolation principle
    (no intrinsic_score / breakdown / Stage 1 references leaking in)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pytest

from research_assistant.orchestrator import (
    STAGE2_CONVICTION_DIMENSIONS,
    STAGE2_DECISION_TAGS,
    Stage2Note,
    _render_screener_evidence_block,
    _stage_2_note,
    compute_composite_conviction,
    parse_stage2_note,
)
from research_assistant.prompts import load_prompt as _load_prompt
from research_assistant.prompts import render as _render


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _valid_payload(**overrides) -> dict:
    """Minimal payload that parse_stage2_note accepts. Override individual
    fields in tests to target the boundary."""
    base = {
        "ticker": "NVDA",
        "observation": [
            "Up 27% over 30d on rising volume",
            "Weekly RSI 65, weekly_rsi_14 just below extension threshold",
        ],
        "bull_anchor": "DC revenue +27% QoQ aligns with bull-trending regime",
        "bear_anchor": "Weekly RSI 65 + recent 5d return +12% — chase risk",
        "what_would_change": [
            "RSI breaks below 55 on weekly close",
            "Form 4 cluster buy: ≥2 distinct buyers in next 30d",
            "Q4 earnings miss revenue by 5%+",
        ],
        "conviction": {
            "technical": 0.55,
            "fundamental": 0.70,
            "catalyst": 0.40,
            "regime": 0.75,
        },
        "decision_tag": "WATCH",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Dataclass round-trip
# ---------------------------------------------------------------------------

def test_stage2note_dataclass_round_trip() -> None:
    """Construct a Stage2Note, JSON-serialize the field-by-field dict, parse
    it back through the parser — values are byte-identical."""
    conviction = {
        "technical": 0.55, "fundamental": 0.70, "catalyst": 0.40, "regime": 0.75,
    }
    original = Stage2Note(
        ticker="NVDA",
        observation=("Sentence 1", "Sentence 2"),
        bull_anchor="A bull thing",
        bear_anchor="A bear thing",
        what_would_change=("Trigger 1", "Trigger 2"),
        conviction=conviction,
        composite_conviction=compute_composite_conviction(conviction),
        decision_tag="WATCH",
    )
    payload = {
        "ticker": original.ticker,
        "observation": list(original.observation),
        "bull_anchor": original.bull_anchor,
        "bear_anchor": original.bear_anchor,
        "what_would_change": list(original.what_would_change),
        "conviction": original.conviction,
        "decision_tag": original.decision_tag,
    }
    # Round-trip through JSON to mimic the trace persistence path.
    reloaded = parse_stage2_note(json.loads(json.dumps(payload)))
    assert reloaded.ticker == original.ticker
    assert reloaded.observation == original.observation
    assert reloaded.bull_anchor == original.bull_anchor
    assert reloaded.bear_anchor == original.bear_anchor
    assert reloaded.what_would_change == original.what_would_change
    assert reloaded.conviction == original.conviction
    assert reloaded.composite_conviction == pytest.approx(original.composite_conviction)
    assert reloaded.decision_tag == original.decision_tag


# ---------------------------------------------------------------------------
# Composite conviction math
# ---------------------------------------------------------------------------

def test_composite_conviction_geometric_mean() -> None:
    """Sanity: all-dims-0.5 produces composite 0.5. Geometric mean of n
    identical values equals the value."""
    conviction = {dim: 0.5 for dim in STAGE2_CONVICTION_DIMENSIONS}
    assert compute_composite_conviction(conviction) == pytest.approx(0.5)


def test_composite_conviction_zero_drags_to_zero() -> None:
    """Geometric-mean characteristic: a single zero zeros the composite.
    This is INTENDED — the new schema penalises a dimension red flag
    instead of averaging it away."""
    conviction = {
        "technical": 0.9, "fundamental": 0.9, "catalyst": 0.0, "regime": 0.9,
    }
    assert compute_composite_conviction(conviction) == 0.0


def test_composite_conviction_clamps_out_of_range() -> None:
    """LLM may emit slightly-out-of-range values; parser should clamp into
    [0, 1] rather than producing nonsense composite scores."""
    conviction = {
        "technical": 1.2, "fundamental": -0.1, "catalyst": 0.5, "regime": 0.5,
    }
    # technical clamps to 1.0, fundamental clamps to 0.0 → composite zeroed
    assert compute_composite_conviction(conviction) == 0.0


def test_composite_conviction_known_value() -> None:
    """Pin a non-trivial geometric-mean result against a hand-computed value."""
    conviction = {
        "technical": 0.55, "fundamental": 0.70, "catalyst": 0.40, "regime": 0.75,
    }
    # geomean(0.55, 0.70, 0.40, 0.75) = (0.55*0.70*0.40*0.75) ** 0.25
    expected = (0.55 * 0.70 * 0.40 * 0.75) ** 0.25
    assert compute_composite_conviction(conviction) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Parser — happy path + error paths
# ---------------------------------------------------------------------------

def test_stage2note_parser_handles_valid_json() -> None:
    note = parse_stage2_note(_valid_payload())
    assert note.ticker == "NVDA"
    assert note.decision_tag == "WATCH"
    assert len(note.observation) == 2
    assert len(note.what_would_change) == 3
    assert set(note.conviction) == set(STAGE2_CONVICTION_DIMENSIONS)


def test_stage2note_parser_raises_on_missing_required_field() -> None:
    bad = _valid_payload()
    del bad["bull_anchor"]
    with pytest.raises(ValueError, match="bull_anchor"):
        parse_stage2_note(bad)


def test_stage2note_parser_raises_on_missing_conviction_dimension() -> None:
    bad = _valid_payload()
    del bad["conviction"]["catalyst"]
    with pytest.raises(ValueError, match="catalyst"):
        parse_stage2_note(bad)


def test_stage2note_parser_raises_on_non_dict_conviction() -> None:
    bad = _valid_payload(conviction=[0.5, 0.5, 0.5, 0.5])
    with pytest.raises(ValueError, match="conviction"):
        parse_stage2_note(bad)


def test_stage2note_parser_defaults_optional_lists_to_empty() -> None:
    """Missing observation / what_would_change → empty tuple (degraded but
    parseable)."""
    minimal = _valid_payload()
    del minimal["observation"]
    del minimal["what_would_change"]
    note = parse_stage2_note(minimal)
    assert note.observation == ()
    assert note.what_would_change == ()


def test_stage2note_parser_uses_default_ticker_when_payload_lacks_it() -> None:
    payload = _valid_payload()
    del payload["ticker"]
    note = parse_stage2_note(payload, default_ticker="NVDA")
    assert note.ticker == "NVDA"


# ---------------------------------------------------------------------------
# Decision tag enum
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag", list(STAGE2_DECISION_TAGS))
def test_stage2note_decision_tag_enum_accepts_each_allowed_value(tag: str) -> None:
    note = parse_stage2_note(_valid_payload(decision_tag=tag))
    assert note.decision_tag == tag


def test_stage2note_decision_tag_enum_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="decision_tag"):
        parse_stage2_note(_valid_payload(decision_tag="MAYBE"))


def test_stage2note_decision_tag_normalizes_case() -> None:
    note = parse_stage2_note(_valid_payload(decision_tag="watch"))
    assert note.decision_tag == "WATCH"


# ---------------------------------------------------------------------------
# Defensive collapse — anchor-as-list with WARN
# ---------------------------------------------------------------------------

def test_stage2note_parser_collapses_anchor_list_with_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The prompt is explicit: bull_anchor + bear_anchor are scalars. If the
    model returns a list anyway, parser takes the first element + WARN."""
    payload = _valid_payload(
        bull_anchor=["primary bull", "secondary bull"],
        bear_anchor=["primary bear"],
    )
    with caplog.at_level(logging.WARNING, logger="research_assistant.orchestrator"):
        note = parse_stage2_note(payload)
    assert note.bull_anchor == "primary bull"
    assert note.bear_anchor == "primary bear"
    assert any("bull_anchor" in rec.message for rec in caplog.records)
    assert any("bear_anchor" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Prompt-rendering regression sentinels
# ---------------------------------------------------------------------------

def _render_stage_2_note_prompt(
    *, screener_evidence: Optional[list[dict]] = None,
) -> str:
    """Render the stage_2_note prompt template the same way `_stage_2_note`
    does, so the assertions hit the literal text the model will see."""
    template = _load_prompt("stage_2_note")
    return _render(
        template,
        ticker_json=json.dumps({"price": 150.0, "return_30d": 0.27}, indent=2),
        headlines_json=json.dumps([], indent=2),
        insider_activity_block="(insider activity unavailable)",
        institutional_ownership_block="(institutional ownership unavailable)",
        screener_evidence_block=_render_screener_evidence_block(
            screener_evidence or [],
        ),
    )


def test_prompt_excludes_stage_1_score() -> None:
    """Data-isolation regression sentinel: the rendered Stage 2 prompt must
    NOT contain `intrinsic_score` or `breakdown` anywhere. This is the
    structural guarantee PR 2A.2 ships."""
    prompt = _render_stage_2_note_prompt()
    assert "intrinsic_score" not in prompt
    assert "breakdown" not in prompt


def test_prompt_excludes_stage_1_even_with_screener_evidence() -> None:
    """Even when screener_evidence contains an accidental intrinsic_score
    key, the prompt rendering strips it (defense-in-depth)."""
    prompt = _render_stage_2_note_prompt(screener_evidence=[
        {
            "screener": "sector_rotation",
            "intrinsic_score": 0.82,
            "breakdown": {"trend_strong": 0.10},
            "rs_rank_now": 2,
        },
    ])
    assert "intrinsic_score" not in prompt
    assert "breakdown" not in prompt
    # Screener metadata still surfaces (not stripped wholesale)
    assert "sector_rotation" in prompt
    assert "rs_rank_now" in prompt


def test_prompt_includes_data_isolation_instruction() -> None:
    """The prompt must explicitly tell Stage 2 to develop its own read.
    Regression sentinel: if someone removes the framing line, this fails."""
    prompt = _render_stage_2_note_prompt()
    # Specific load-bearing phrase from the prompt
    assert "develop your own read" in prompt.lower()


def test_prompt_includes_decision_tag_enum() -> None:
    prompt = _render_stage_2_note_prompt()
    for tag in STAGE2_DECISION_TAGS:
        assert tag in prompt


def test_prompt_includes_observe_framing_not_thesis_framing() -> None:
    """The new prompt uses observation / describe framing, not thesis /
    advocate framing. Specifically, the body asks the model to describe
    what it observes."""
    prompt = _render_stage_2_note_prompt()
    assert "observe" in prompt.lower() or "describe" in prompt.lower()


# ---------------------------------------------------------------------------
# _stage_2_note signature does not accept stage_1_result
# ---------------------------------------------------------------------------

def test_stage_2_note_signature_has_no_stage_1_result_parameter() -> None:
    """Structural guarantee: `_stage_2_note` literally cannot be passed a
    Stage 1 result, because it has no such parameter. If someone re-adds
    it (regression), this fails."""
    import inspect
    sig = inspect.signature(_stage_2_note)
    assert "stage_1_result" not in sig.parameters
    assert "stage_1_json" not in sig.parameters
    assert "intrinsic_score" not in sig.parameters


# ---------------------------------------------------------------------------
# Trace event does not leak Stage 1 (Smoke 4 sanity)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage_2_note_invocation_renders_prompt_without_stage_1(
    tmp_path: Path,
) -> None:
    """End-to-end: call `_stage_2_note` with a fake client and assert the
    rendered prompt sent to the model contains no Stage 1 references."""
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt
            captured["system"] = system

            class _Result:
                text = json.dumps(_valid_payload())
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    note, meta = await _stage_2_note(
        _FakeClient(),
        world_state={"regime": "bull-trending"},
        ticker_data={"price": 150.0, "return_30d": 0.27},
        headlines=[],
        screener_evidence=[
            {"screener": "sector_rotation", "rs_rank_now": 2,
             "rs_rank_prior": 7, "basis_days": 30},
        ],
        default_ticker="NVDA",
    )
    assert note is not None
    assert note.decision_tag == "WATCH"
    # The prompt text the model saw must not contain Stage 1 leakage
    assert "intrinsic_score" not in captured["prompt"]
    assert "breakdown" not in captured["prompt"]
    # The system block carries world_state — that's allowed (it's Stage 0,
    # not Stage 1). But intrinsic_score / breakdown must not appear there
    # either.
    assert "intrinsic_score" not in captured["system"]
