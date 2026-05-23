"""
Tests for the orchestrator's insider-activity wiring (FOLLOWUPS #3 Stage 2).

Covers:
- _format_insider_activity_block renders three distinct strings for
  (None / empty summary / populated summary).
- research_ticker forwards the insider_activity kwarg through to
  _stage_2_thesis.
- The rendered Stage 2 prompt actually contains the stage_2_block() output
  when insider_activity is populated.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from research_assistant.edgar import (
    InsiderActivitySummary,
    OfficerActivity,
)
from research_assistant.orchestrator import (
    _format_insider_activity_block,
    _stage_2_thesis,
    research_ticker,
)


def _summary(**overrides) -> InsiderActivitySummary:
    defaults = dict(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=2, buys_count=1, sales_count=1,
        net_dollars=-16_600_000,
        code_mix={"S": 1, "P": 1, "A": 1},
        deriv_code_mix={"M": 1},
        by_officer=[
            OfficerActivity(
                cik="11", name="HUANG", relationship="President & CEO",
                sales_count=1, net_shares=-120_000, net_dollars=-18_000_000,
                latest_transaction_date="2026-05-19",
            ),
            OfficerActivity(
                cik="22", name="KRESS", relationship="EVP & CFO",
                buys_count=1, net_shares=10_000, net_dollars=1_400_000,
                latest_transaction_date="2026-04-10",
            ),
        ],
        latest_transaction_date="2026-05-19",
    )
    defaults.update(overrides)
    return InsiderActivitySummary(**defaults)


# ---------------------------------------------------------------------------
# Block formatter
# ---------------------------------------------------------------------------

def test_format_block_none_signals_unavailable() -> None:
    """None means EDGAR fetch failed or ticker is not in SEC universe;
    Stage 2 needs to know this is distinct from 'no activity in window'."""
    out = _format_insider_activity_block(None)
    assert "unavailable" in out.lower()


def test_format_block_empty_signals_no_activity() -> None:
    s = _summary(
        total_filings=0, buys_count=0, sales_count=0, net_dollars=0.0,
        code_mix={}, deriv_code_mix={}, by_officer=[],
        latest_transaction_date=None,
    )
    out = _format_insider_activity_block(s)
    assert "no Form 4 filings" in out
    assert "90d" in out
    assert "unavailable" not in out.lower()


def test_format_block_populated_uses_stage_2_block() -> None:
    s = _summary()
    out = _format_insider_activity_block(s)
    assert out == s.stage_2_block()
    assert "President & CEO -$18.0M" in out
    assert "EVP & CFO $1.4M" in out


# ---------------------------------------------------------------------------
# Orchestrator forwarding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_ticker_forwards_insider_activity(tmp_path: Path) -> None:
    """research_ticker MUST pass its insider_activity kwarg through to
    _stage_2_thesis without dropping or transforming it."""
    captured: dict = {}

    async def fake_stage_2(client, ws, td, s1, h, insider_activity=None):
        captured["insider_activity"] = insider_activity
        return {
            "ticker": "NVDA",
            "thesis_text": "thesis",
            "conviction_score": 0.5,
            "key_drivers": ["d"],
            "risks": ["r"],
            "open_questions": [],
            "evidence_anchors": [
                {"claim": "d", "source": "x"},
                {"claim": "r", "source": "y"},
            ],
        }, None

    async def fake_stage_3(client, ws, twd, model="x"):
        return {
            "ticker": "NVDA",
            "critique_text": "c", "adjusted_score": 0.5,
            "flagged_risks": [], "open_questions_added": [],
            "news_reactivity_flag": False,
        }, None

    summary = _summary()
    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3):
        await research_ticker(
            "NVDA",
            world_state={},
            ticker_data={"price": 150.0},
            headlines=[],
            base=tmp_path,
            insider_activity=summary,
        )
    assert captured["insider_activity"] is summary


@pytest.mark.asyncio
async def test_research_ticker_default_insider_activity_is_none(
    tmp_path: Path,
) -> None:
    """Callers that don't supply insider_activity (existing tests, legacy
    callers) get None forwarded — preserving the graceful-degrade signal."""
    captured: dict = {}

    async def fake_stage_2(client, ws, td, s1, h, insider_activity=None):
        captured["insider_activity"] = insider_activity
        return {
            "ticker": "NVDA",
            "thesis_text": "t", "conviction_score": 0.5,
            "key_drivers": ["d"], "risks": ["r"],
            "open_questions": [],
            "evidence_anchors": [
                {"claim": "d", "source": "x"},
                {"claim": "r", "source": "y"},
            ],
        }, None

    async def fake_stage_3(client, ws, twd, model="x"):
        return {
            "ticker": "NVDA", "critique_text": "c", "adjusted_score": 0.5,
            "flagged_risks": [], "open_questions_added": [],
            "news_reactivity_flag": False,
        }, None

    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3):
        await research_ticker(
            "NVDA",
            world_state={}, ticker_data={"price": 150.0}, headlines=[],
            base=tmp_path,
        )
    assert captured["insider_activity"] is None


# ---------------------------------------------------------------------------
# Rendered prompt content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage_2_prompt_includes_insider_block() -> None:
    """End-to-end render: _stage_2_thesis should embed the stage_2_block()
    output into the Stage 2 prompt sent to Claude. Catches accidental
    template/placeholder drift."""
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["system"] = system

            class _Result:
                text = '{"ticker":"NVDA","thesis_text":"t","conviction_score":0.5,"key_drivers":["d"],"risks":["r"],"open_questions":[],"evidence_anchors":[{"claim":"d","source":"x"},{"claim":"r","source":"y"}]}'
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    summary = _summary()
    parsed, meta = await _stage_2_thesis(
        _FakeClient(),
        world_state={"regime": "bull-trending"},
        ticker_data={"price": 150.0},
        stage_1_result={"ticker": "NVDA"},
        headlines=[],
        insider_activity=summary,
    )
    assert parsed is not None
    prompt = captured["prompt"]
    assert "INSIDER_ACTIVITY" in prompt
    # The stage_2_block content must be rendered into the prompt verbatim
    assert summary.stage_2_block() in prompt


@pytest.mark.asyncio
async def test_stage_2_prompt_when_insider_none() -> None:
    """When no insider_activity is supplied, the placeholder must still
    be substituted (no literal '{insider_activity_block}' leaking)."""
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt

            class _Result:
                text = '{"ticker":"NVDA","thesis_text":"t","conviction_score":0.5,"key_drivers":["d"],"risks":["r"],"open_questions":[],"evidence_anchors":[{"claim":"d","source":"x"},{"claim":"r","source":"y"}]}'
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    await _stage_2_thesis(
        _FakeClient(),
        world_state={}, ticker_data={"price": 150.0},
        stage_1_result={"ticker": "NVDA"}, headlines=[],
    )
    prompt = captured["prompt"]
    assert "{insider_activity_block}" not in prompt
    assert "unavailable" in prompt.lower()
