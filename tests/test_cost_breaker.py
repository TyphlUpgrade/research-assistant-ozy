"""
Panopticon daily cost circuit breaker tests (FOLLOWUPS #28 / critic C3).

Covers the persistent ledger (round-trip across simulated fresh
processes, ET-day rollover, per-base isolation, fail-open), and the two
degrade paths in synthesize_research (Level-1 synthesizer skip, Level-2
anchor-existence-only fallback). ET date is injected, never the real
clock; caps are forced via PANOPTICON_DAILY_CAP_USD.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from research_assistant import cost_breaker
from research_assistant.synthesizer import (
    SynthesisFlag,
    SynthesisOutput,
    VERDICT_UNVERIFIED_CAPPED,
    synthesize_research,
)

DAY = "2026-06-17"


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------

def test_add_spend_is_capped_round_trip(tmp_path: Path):
    """Cumulative spend persists across calls (each call re-reads the file,
    simulating fresh /research processes); cap trips once it's reached."""
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et=DAY) is False
    cost_breaker.add_spend(tmp_path, 0.05, date_et=DAY, count_research=True)
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et=DAY) is False
    cost_breaker.add_spend(tmp_path, 0.06, date_et=DAY)
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et=DAY) is True


def test_ledger_resets_per_et_day(tmp_path: Path):
    """A new ET-day starts from zero spend (separate ledger file)."""
    cost_breaker.add_spend(tmp_path, 0.50, date_et=DAY, count_research=True)
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et=DAY) is True
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et="2026-06-18") is False


def test_per_base_isolation(tmp_path: Path):
    """Spend in one base dir does not affect the cap in another."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    cost_breaker.add_spend(a, 0.50, date_et=DAY, count_research=True)
    assert cost_breaker.is_capped(a, cap=0.10, date_et=DAY) is True
    assert cost_breaker.is_capped(b, cap=0.10, date_et=DAY) is False


def test_corrupt_ledger_fails_open(tmp_path: Path):
    """A corrupt ledger reads as $0 (fail-open) rather than crashing."""
    ledger = tmp_path / "panopticon_spend" / f"{DAY}.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not valid json", encoding="utf-8")
    assert cost_breaker.is_capped(tmp_path, cap=0.10, date_et=DAY) is False


def test_degrade_summary_counts_days_and_events(tmp_path: Path):
    """Summary reports (cost-cap-hit days, cumulative degraded events)."""
    cost_breaker.add_spend(tmp_path, 0.0, date_et=DAY,
                           was_degraded=True, count_research=True)
    cost_breaker.add_spend(tmp_path, 0.0, date_et="2026-06-18",
                           was_degraded=True, count_research=True)
    cost_breaker.add_spend(tmp_path, 0.0, date_et="2026-06-18",
                           was_degraded=True, count_research=True)
    days, events = cost_breaker.degrade_summary(tmp_path)
    assert days == 2
    assert events == 3


def test_degrade_summary_empty_when_unused(tmp_path: Path):
    assert cost_breaker.degrade_summary(tmp_path) == (0, 0)


# ---------------------------------------------------------------------------
# Level-1 degrade — synthesizer skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level1_cap_skips_synthesizer(tmp_path: Path, monkeypatch):
    """When the day's cap is already reached, the synthesizer SDK call is
    never made; output is cap_reached with no flags and the cap sentinel
    renders."""
    monkeypatch.setenv("PANOPTICON_DAILY_CAP_USD", "0.0")  # cap=0 → instantly capped
    synth_mock = AsyncMock()
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer", synth_mock), \
         patch("research_assistant.synthesizer.cost_breaker.current_et_date",
               return_value=DAY):
        result = await synthesize_research(
            AsyncMock(),
            ticker="X",
            world_state={"regime": "neutral"},
            ticker_data={"symbol": "X", "daily_signals": {"adx": 19.5}},
            headlines=[],
            base=tmp_path,
        )
    synth_mock.assert_not_called()
    assert result is not None
    assert result.cap_reached is True
    assert result.flags == []
    assert SynthesisOutput.render_for_prompt(result) == (
        "(panopticon disabled — daily cost cap reached)"
    )
    # The degraded event is recorded for the scoreboard surface.
    days, events = cost_breaker.degrade_summary(tmp_path)
    assert (days, events) == (1, 1)


# ---------------------------------------------------------------------------
# Level-2 degrade — anchor-existence-only fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level2_cap_falls_back_to_anchor_existence(tmp_path: Path, monkeypatch):
    """Synth runs (cap not yet hit), its cost pushes cumulative over the
    cap, so the verify loop falls back to anchor-existence-only: no Haiku
    verify_flag calls, flags kept iff anchors resolve, tagged
    UNVERIFIED_CAPPED."""
    monkeypatch.setenv("PANOPTICON_DAILY_CAP_USD", "0.10")
    cost_breaker.add_spend(tmp_path, 0.06, date_et=DAY)  # pre-seed just under cap

    flags = [
        SynthesisFlag(severity="HIGH", observation_type="a",
                      shallow_source="world_state", shallow_observation="s",
                      deep_source="TICKER_DATA", deep_observation="d"),
        SynthesisFlag(severity="LOW", observation_type="b",
                      shallow_source="phantom:nope", shallow_observation="s",
                      deep_source="world_state", deep_observation="d"),
    ]
    synth_out = SynthesisOutput(ticker="X", schema_version=2,
                                axes_agreed=False, flags=flags)
    syn_meta = type("M", (), {"cost_usd": 0.05})()  # pushes 0.06 → 0.11 > 0.10
    verify_mock = AsyncMock()
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer",
               AsyncMock(return_value=(synth_out, syn_meta))), \
         patch("research_assistant.synthesizer.verify_flag", verify_mock), \
         patch("research_assistant.synthesizer.cost_breaker.current_et_date",
               return_value=DAY):
        result = await synthesize_research(
            AsyncMock(),
            ticker="X",
            world_state={"regime": "neutral"},
            ticker_data={"symbol": "X", "daily_signals": {"adx": 1}},
            headlines=[],
            base=tmp_path,
        )
    # No role-bound verification happened after the cap tripped.
    verify_mock.assert_not_called()
    assert result.cap_reached is True
    # Flag 1's anchors (world_state + TICKER_DATA) resolve → kept; flag 2's
    # phantom shallow anchor doesn't → dropped. Both tagged capped.
    assert len(result.flags) == 1
    assert len(result.dropped_flags) == 1
    assert result.flags[0].verifier_shallow_verdict == VERDICT_UNVERIFIED_CAPPED
    assert result.dropped_flags[0].verifier_shallow_verdict == VERDICT_UNVERIFIED_CAPPED


@pytest.mark.asyncio
async def test_happy_path_increments_research_count_once(tmp_path: Path, monkeypatch):
    """A normal non-degraded run records research_count == 1, degraded == 0,
    and the real synthesizer + verifier spend (no double/under-count)."""
    monkeypatch.setenv("PANOPTICON_DAILY_CAP_USD", "100.0")  # never trips
    flag = SynthesisFlag(severity="HIGH", observation_type="a",
                         shallow_source="world_state", shallow_observation="s",
                         deep_source="TICKER_DATA", deep_observation="d")
    synth_out = SynthesisOutput(ticker="X", schema_version=2,
                                axes_agreed=False, flags=[flag])
    syn_meta = type("M", (), {"cost_usd": 0.04})()
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer",
               AsyncMock(return_value=(synth_out, syn_meta))), \
         patch("research_assistant.synthesizer.verify_flag",
               AsyncMock(return_value=(True, 0.01))), \
         patch("research_assistant.synthesizer.cost_breaker.current_et_date",
               return_value=DAY):
        await synthesize_research(
            AsyncMock(), ticker="X", world_state={}, ticker_data={"symbol": "X"},
            headlines=[], base=tmp_path,
        )
    ledger = cost_breaker._read_ledger(
        cost_breaker._ledger_path(tmp_path, DAY), DAY)
    assert ledger["research_count"] == 1
    assert ledger["degraded_count"] == 0
    assert ledger["panopticon_spend_usd"] == pytest.approx(0.05)  # 0.04 synth + 0.01 verify


@pytest.mark.asyncio
async def test_synthesizer_parse_failure_still_records_spend(tmp_path: Path, monkeypatch):
    """Synthesizer parse/schema failure returns (None, meta) with REAL
    cost; that spend must hit the ledger (the breaker's most likely
    runaway path) even though synthesize_research returns None."""
    monkeypatch.setenv("PANOPTICON_DAILY_CAP_USD", "100.0")
    failed_meta = type("M", (), {"cost_usd": 0.045})()
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer",
               AsyncMock(return_value=(None, failed_meta))), \
         patch("research_assistant.synthesizer.cost_breaker.current_et_date",
               return_value=DAY):
        result = await synthesize_research(
            AsyncMock(), ticker="X", world_state={}, ticker_data={"symbol": "X"},
            headlines=[], base=tmp_path,
        )
    assert result is None
    ledger = cost_breaker._read_ledger(
        cost_breaker._ledger_path(tmp_path, DAY), DAY)
    assert ledger["panopticon_spend_usd"] == pytest.approx(0.045)
    assert ledger["research_count"] == 1


@pytest.mark.asyncio
async def test_no_base_disables_breaker(tmp_path: Path, monkeypatch):
    """base=None → breaker off: no ledger written even with cap=0."""
    monkeypatch.setenv("PANOPTICON_DAILY_CAP_USD", "0.0")
    synth_mock = AsyncMock(return_value=(
        SynthesisOutput(ticker="X", schema_version=2, axes_agreed=True), None
    ))
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer", synth_mock):
        result = await synthesize_research(
            AsyncMock(), ticker="X", world_state={}, ticker_data={"symbol": "X"},
            headlines=[], base=None,
        )
    synth_mock.assert_called_once()  # breaker did NOT short-circuit
    assert result is not None
    assert not (tmp_path / "panopticon_spend").exists()
