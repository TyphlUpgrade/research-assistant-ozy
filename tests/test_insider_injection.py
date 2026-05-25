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
    FundPosition,
    InsiderActivitySummary,
    InstitutionalOwnership,
    OfficerActivity,
)
from research_assistant.brief import _insider_summary_line, build_brief
from research_assistant.dossier_io import Dossier, write_dossier_atomic
from research_assistant.orchestrator import (
    _stage_2_probe,
    _stage_2_thesis,
    probe_ticker,
    research_ticker,
)

# The three-state prompt-block rendering used to live as
# _format_*_block helpers on the orchestrator; it now lives as
# `render_for_prompt` classmethods on the dataclasses. These aliases
# keep the existing test names readable.
_format_insider_activity_block = InsiderActivitySummary.render_for_prompt
_format_institutional_ownership_block = InstitutionalOwnership.render_for_prompt


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

    async def fake_stage_2(client, ws, td, s1, h, insider_activity=None, institutional_ownership=None):
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

    async def fake_stage_2(client, ws, td, s1, h, insider_activity=None, institutional_ownership=None):
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


# ---------------------------------------------------------------------------
# /probe injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_ticker_forwards_insider_activity(tmp_path: Path) -> None:
    """probe_ticker MUST pass its insider_activity kwarg through to
    _stage_2_probe without dropping or transforming it."""
    # probe_ticker requires an existing dossier
    write_dossier_atomic(Dossier(symbol="NVDA", state_md="prior thesis"), tmp_path)
    captured: dict = {}

    async def fake_probe(client, ws, td, h, dc, q, insider_activity=None, institutional_ownership=None, filing_excerpts=None):
        captured["insider_activity"] = insider_activity
        return {
            "ticker": "NVDA", "answer": "ans",
            "evidence_anchors": [{"claim": "ans", "source": "x"}],
            "closes_questions": [], "new_open_questions": [],
        }, None

    summary = _summary()
    with patch("research_assistant.orchestrator._stage_2_probe", fake_probe):
        await probe_ticker(
            "NVDA", "are insiders selling?",
            world_state={}, ticker_data={"price": 150.0}, headlines=[],
            base=tmp_path, insider_activity=summary,
        )
    assert captured["insider_activity"] is summary


@pytest.mark.asyncio
async def test_probe_prompt_includes_insider_block() -> None:
    """End-to-end probe prompt render must contain the stage_2_block()
    output verbatim — same placeholder/template wiring as Stage 2."""
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt

            class _Result:
                text = '{"ticker":"NVDA","answer":"a","evidence_anchors":[{"claim":"a","source":"x"}],"closes_questions":[],"new_open_questions":[]}'
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    summary = _summary()
    parsed, _ = await _stage_2_probe(
        _FakeClient(),
        world_state={"regime": "bull-trending"},
        ticker_data={"price": 150.0},
        headlines=[],
        dossier_context="## DOSSIER STATE\n(empty)",
        focused_question="are insiders selling?",
        insider_activity=summary,
    )
    assert parsed is not None
    prompt = captured["prompt"]
    assert "INSIDER_ACTIVITY" in prompt
    assert summary.stage_2_block() in prompt


# ---------------------------------------------------------------------------
# Stage 1 brief candidate-line injection
# ---------------------------------------------------------------------------

def test_insider_summary_line_none() -> None:
    assert _insider_summary_line(None) == "(insider data unavailable)"


def test_insider_summary_line_empty_window() -> None:
    s = _summary(
        total_filings=0, buys_count=0, sales_count=0, net_dollars=0.0,
        code_mix={}, deriv_code_mix={}, by_officer=[],
        latest_transaction_date=None,
    )
    assert _insider_summary_line(s) == "(no Form 4 last 90d)"


def test_insider_summary_line_populated() -> None:
    s = _summary()
    line = _insider_summary_line(s)
    assert line == s.stage_1_line()
    assert "insider net flow last 90d" in line


@pytest.mark.asyncio
async def test_build_brief_threads_insider_summary_into_composite(
    tmp_path: Path,
) -> None:
    """PR 2A.1: build_brief should pass insider_activities through to the
    deterministic Stage-1 composite (NOT the deleted Haiku batched filter).
    Severe insider selling on NVDA must trigger the cap in `breakdown`,
    surfacing on the returned BriefItem.stage_1_reason summary."""
    captured: dict = {}

    async def fake_stage_0(client, ctx):
        return {"regime": "bull-trending", "regime_confidence": 0.7}

    # Spy on _stage_1_composite to capture the insider_activities it sees.
    from research_assistant.brief import _stage_1_composite as real_composite

    def fake_composite(world_state, ticker_data_by_symbol, insider_activities, screener_alerts):
        captured["insider_activities"] = insider_activities
        return real_composite(
            world_state=world_state,
            ticker_data_by_symbol=ticker_data_by_symbol,
            insider_activities=insider_activities,
            screener_alerts=screener_alerts,
        )

    # No Stage 2 survivors — keep the test fast and focused.
    async def fake_stage_2_thesis(client, ws, td, s1, h, insider_activity=None,
                                  institutional_ownership=None):
        return None, None

    nvda_summary = _summary(
        total_filings=4, buys_count=0, sales_count=4, net_dollars=-42_000_000,
        code_mix={"S": 4}, deriv_code_mix={}, by_officer=[],
        latest_transaction_date="2026-05-19",
    )
    insider_activities = {
        "NVDA": nvda_summary,
        "AAPL": None,
        "TSLA": _summary(
            total_filings=0, buys_count=0, sales_count=0, net_dollars=0.0,
            code_mix={}, deriv_code_mix={}, by_officer=[],
            latest_transaction_date=None,
        ),
    }
    watchlist_data = {
        "NVDA": {"price": 150.0, "recent_return_5d": 0.05},
        "AAPL": {"price": 200.0, "recent_return_5d": 0.02},
        "TSLA": {"price": 250.0, "recent_return_5d": -0.01},
    }

    with patch("research_assistant.brief._stage_0_world_state", fake_stage_0), \
         patch("research_assistant.brief._stage_1_composite", fake_composite), \
         patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2_thesis):
        brief = await build_brief(
            market_context={},
            universe=["NVDA", "AAPL", "TSLA"],
            watchlist_tickers_with_data=watchlist_data,
            headlines_per_ticker={"NVDA": [], "AAPL": [], "TSLA": []},
            research_base=tmp_path,
            insider_activities=insider_activities,
        )

    # The composite saw the raw InsiderActivitySummary objects (not
    # pre-rendered strings) — that's the new contract.
    assert captured["insider_activities"]["NVDA"] is nvda_summary
    assert captured["insider_activities"]["AAPL"] is None
    # NVDA's severe-selling signal flows into the composite, capping its score.
    nvda_item = next((i for i in brief.items if i.ticker == "NVDA"), None)
    assert nvda_item is not None
    assert nvda_item.intrinsic_score <= 0.40, (
        f"Expected NVDA score capped by severe insider selling; "
        f"got {nvda_item.intrinsic_score}"
    )


@pytest.mark.asyncio
async def test_build_brief_default_insider_activities_unavailable(
    tmp_path: Path,
) -> None:
    """When insider_activities is omitted (legacy callers), build_brief
    still runs — composite treats missing entries as None which has no
    score effect (graceful degrade)."""

    async def fake_stage_0(client, ctx):
        return {"regime": "bull-trending"}

    async def fake_stage_2_thesis(client, ws, td, s1, h, insider_activity=None,
                                  institutional_ownership=None):
        return None, None

    with patch("research_assistant.brief._stage_0_world_state", fake_stage_0), \
         patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2_thesis):
        brief = await build_brief(
            market_context={},
            universe=["NVDA"],
            watchlist_tickers_with_data={"NVDA": {"price": 150.0}},
            headlines_per_ticker={"NVDA": []},
            research_base=tmp_path,
        )
    # NVDA still ranked despite no insider data — score equals baseline (after
    # bull-trending regime mult: 0.30 * 1.10 = 0.33).
    nvda_item = next((i for i in brief.items if i.ticker == "NVDA"), None)
    assert nvda_item is not None
    assert nvda_item.intrinsic_score == pytest.approx(0.33, abs=1e-6)


# ---------------------------------------------------------------------------
# FOLLOWUPS #5 — institutional ownership (13F) injection
# ---------------------------------------------------------------------------

def _ownership(**overrides) -> InstitutionalOwnership:
    defaults = dict(
        ticker="NVDA", issuer_match="NVIDIA",
        period="2026-03-31", prior_period="2025-12-31",
        funds_tracked=5, funds_holding=2, funds_holding_prior=1,
        new_positions=1, exited_positions=0,
        total_shares=21e6, total_value_usd=3.2e9,
        positions=[
            FundPosition(manager_cik="01", manager_name="BlackRock",
                         shares=12e6, value_usd=1.8e9, title_of_class="COM"),
            FundPosition(manager_cik="02", manager_name="Vanguard",
                         shares=9e6, value_usd=1.4e9, title_of_class="COM"),
        ],
    )
    defaults.update(overrides)
    return InstitutionalOwnership(**defaults)


def test_format_ownership_block_none_signals_unavailable() -> None:
    out = _format_institutional_ownership_block(None)
    assert "unavailable" in out.lower()


def test_format_ownership_block_empty_signals_no_positions() -> None:
    s = _ownership(
        funds_holding=0, funds_holding_prior=0, new_positions=0,
        exited_positions=0, total_shares=0, total_value_usd=0, positions=[],
    )
    out = _format_institutional_ownership_block(s)
    assert "no tracked-fund 13F positions" in out
    assert "unavailable" not in out.lower()


def test_format_ownership_block_populated_uses_stage_2_line() -> None:
    s = _ownership()
    out = _format_institutional_ownership_block(s)
    assert out == s.stage_2_line()
    assert "BlackRock $1.8B" in out


@pytest.mark.asyncio
async def test_research_ticker_forwards_institutional_ownership(tmp_path: Path) -> None:
    captured: dict = {}

    async def fake_stage_2(client, ws, td, s1, h, insider_activity=None, institutional_ownership=None):
        captured["institutional_ownership"] = institutional_ownership
        return {
            "ticker": "NVDA", "thesis_text": "t", "conviction_score": 0.5,
            "key_drivers": ["d"], "risks": ["r"], "open_questions": [],
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

    ownership = _ownership()
    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3):
        await research_ticker(
            "NVDA",
            world_state={}, ticker_data={"price": 150.0}, headlines=[],
            base=tmp_path, institutional_ownership=ownership,
        )
    assert captured["institutional_ownership"] is ownership


@pytest.mark.asyncio
async def test_probe_ticker_forwards_institutional_ownership(tmp_path: Path) -> None:
    write_dossier_atomic(Dossier(symbol="NVDA", state_md="prior thesis"), tmp_path)
    captured: dict = {}

    async def fake_probe(client, ws, td, h, dc, q, insider_activity=None, institutional_ownership=None, filing_excerpts=None):
        captured["institutional_ownership"] = institutional_ownership
        return {
            "ticker": "NVDA", "answer": "ans",
            "evidence_anchors": [{"claim": "ans", "source": "x"}],
            "closes_questions": [], "new_open_questions": [],
        }, None

    ownership = _ownership()
    with patch("research_assistant.orchestrator._stage_2_probe", fake_probe):
        await probe_ticker(
            "NVDA", "who holds the most?",
            world_state={}, ticker_data={"price": 150.0}, headlines=[],
            base=tmp_path, institutional_ownership=ownership,
        )
    assert captured["institutional_ownership"] is ownership


@pytest.mark.asyncio
async def test_stage_2_prompt_includes_institutional_block() -> None:
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

    ownership = _ownership()
    parsed, _ = await _stage_2_thesis(
        _FakeClient(),
        world_state={"regime": "bull-trending"},
        ticker_data={"price": 150.0},
        stage_1_result={"ticker": "NVDA"},
        headlines=[],
        institutional_ownership=ownership,
    )
    assert parsed is not None
    prompt = captured["prompt"]
    assert "INSTITUTIONAL_OWNERSHIP" in prompt
    assert ownership.stage_2_line() in prompt
    # source-rule list line picked up too
    assert "edgar:13f:aggregate" in prompt


@pytest.mark.asyncio
async def test_probe_prompt_includes_institutional_block() -> None:
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt

            class _Result:
                text = '{"ticker":"NVDA","answer":"a","evidence_anchors":[{"claim":"a","source":"x"}],"closes_questions":[],"new_open_questions":[]}'
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    ownership = _ownership()
    parsed, _ = await _stage_2_probe(
        _FakeClient(),
        world_state={}, ticker_data={"price": 150.0}, headlines=[],
        dossier_context="", focused_question="who holds the most?",
        institutional_ownership=ownership,
    )
    assert parsed is not None
    prompt = captured["prompt"]
    assert "INSTITUTIONAL_OWNERSHIP" in prompt
    assert ownership.stage_2_line() in prompt
    assert "edgar:13f:aggregate" in prompt


@pytest.mark.asyncio
async def test_stage_2_prompt_institutional_default_none_substitutes() -> None:
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
    assert "{institutional_ownership_block}" not in prompt
    # Default None → unavailable placeholder for both blocks
    assert "institutional ownership unavailable" in prompt.lower()


@pytest.mark.asyncio
async def test_probe_prompt_when_insider_none() -> None:
    """Default-None path: placeholder is substituted, no '{...}' leakage."""
    captured: dict = {}

    class _FakeClient:
        async def call(self, prompt, *, model, system=None):
            captured["prompt"] = prompt

            class _Result:
                text = '{"ticker":"NVDA","answer":"a","evidence_anchors":[{"claim":"a","source":"x"}],"closes_questions":[],"new_open_questions":[]}'
                input_tokens = 0
                output_tokens = 0
                cost_usd = 0.0
                latency_ms = 0
                model = "claude-sonnet-4-6"

            return _Result()

    await _stage_2_probe(
        _FakeClient(),
        world_state={}, ticker_data={"price": 150.0}, headlines=[],
        dossier_context="", focused_question="q?",
    )
    prompt = captured["prompt"]
    assert "{insider_activity_block}" not in prompt
    assert "unavailable" in prompt.lower()
