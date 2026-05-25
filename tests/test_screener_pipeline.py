"""
Tests for the screener pipeline (PR 1.1 foundation).

Covers:
- Empty registry returns [] and writes nothing
- Registered screener emits candidates → journaled via append_alert
- Per-screener exception isolates (other screeners still run; metrics record degrade)
- `register_formatter` + `render_setup_line` dispatch + fallback
- Pipeline observability log includes cache_branch + per_screener metrics
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from research_assistant.screeners import (
    SetupCandidate,
    evaluate_all,
    register_formatter,
    register_screener,
    render_setup_line,
    run_screeners_and_journal,
)
from research_assistant.screeners import _pipeline as pipeline_mod


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset the screener registry around each test to avoid cross-test pollution."""
    saved = dict(pipeline_mod._REGISTRY)
    pipeline_mod._REGISTRY.clear()
    yield
    pipeline_mod._REGISTRY.clear()
    pipeline_mod._REGISTRY.update(saved)


def _make_candidate(ticker: str = "NVDA") -> SetupCandidate:
    return SetupCandidate(
        ticker=ticker,
        screener="dummy",
        asof="2026-05-23",
        entry_price=100.0,
        evidence={"reason": "test"},
    )


@pytest.mark.asyncio
async def test_empty_registry_returns_empty(tmp_path: Path) -> None:
    result = await run_screeners_and_journal(
        world_state={},
        universe=["NVDA"],
        ticker_data={"NVDA": {"price": 100.0}},
        research_base=tmp_path,
        cache_branch="hit",
    )
    assert result == []
    assert not (tmp_path / "alerts").exists()


@pytest.mark.asyncio
async def test_registered_screener_journals_candidates(tmp_path: Path) -> None:
    def _evaluate(ticker_data, world_state, earnings_event=None, sector_rs=None):
        symbol = ticker_data.get("symbol")
        if symbol != "NVDA":
            return None
        return SetupCandidate(
            ticker="NVDA",
            screener="dummy",
            asof="2026-05-23",
            entry_price=100.0,
            evidence={"hit": True},
        )

    register_screener("dummy", _evaluate)
    result = await run_screeners_and_journal(
        world_state={"regime": "bull"},
        universe=["NVDA", "AAPL"],
        ticker_data={"NVDA": {"symbol": "NVDA"}, "AAPL": {"symbol": "AAPL"}},
        research_base=tmp_path,
        cache_branch="miss",
    )
    assert len(result) == 1
    assert result[0].ticker == "NVDA"

    day_file = tmp_path / "alerts" / "2026-05-23.jsonl"
    assert day_file.exists()
    assert day_file.read_text().count("\n") == 1


def test_evaluate_all_isolates_screener_failures() -> None:
    def _bad(ticker_data, world_state, earnings_event=None, sector_rs=None):
        raise RuntimeError("kaboom")

    def _good(ticker_data, world_state, earnings_event=None, sector_rs=None):
        return SetupCandidate(
            ticker="NVDA",
            screener="good",
            asof="2026-05-23",
            entry_price=100.0,
            evidence={},
        )

    register_screener("bad", _bad)
    register_screener("good", _good)
    candidates, metrics = evaluate_all(
        world_state={},
        universe=["NVDA"],
        ticker_data={"NVDA": {"symbol": "NVDA"}},
    )
    assert len(candidates) == 1
    assert candidates[0].screener == "good"
    assert metrics["bad"]["status"] == "degraded"
    assert metrics["bad"]["reason"] == "RuntimeError"
    assert metrics["good"]["count"] == 1


def test_evaluate_all_threads_earnings_and_sector_rs() -> None:
    seen: dict = {}

    def _evaluate(ticker_data, world_state, earnings_event=None, sector_rs=None):
        seen["earnings_event"] = earnings_event
        seen["sector_rs"] = sector_rs
        return None

    register_screener("inspector", _evaluate)
    evaluate_all(
        world_state={},
        universe=["NVDA"],
        ticker_data={"NVDA": {"symbol": "NVDA", "sector": "Technology"}},
        earnings_calendar={"NVDA": {"date": "2026-06-01"}},
        sector_breadth={"Technology": 0.42},
    )
    assert seen["earnings_event"] == {"date": "2026-06-01"}
    assert seen["sector_rs"] == 0.42


def test_render_setup_line_fallback_when_unregistered() -> None:
    c = _make_candidate()
    line = render_setup_line(c)
    assert "NVDA" in line
    assert "dummy" in line


def test_render_setup_line_uses_registered_formatter() -> None:
    register_formatter("dummy", lambda c: f"DUMMY={c.ticker}@{c.entry_price}")
    c = _make_candidate()
    assert render_setup_line(c) == "DUMMY=NVDA@100.0"


@pytest.mark.asyncio
async def test_pipeline_logs_cache_branch_and_metrics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _evaluate(ticker_data, world_state, earnings_event=None, sector_rs=None):
        return _make_candidate()

    register_screener("dummy", _evaluate)
    with caplog.at_level(logging.INFO, logger="research_assistant.screeners._pipeline"):
        await run_screeners_and_journal(
            world_state={},
            universe=["NVDA"],
            ticker_data={"NVDA": {"symbol": "NVDA"}},
            research_base=tmp_path,
            cache_branch="hit",
        )
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "cache_branch=hit" in messages
    assert "alert_count=1" in messages
    assert "per_screener" in messages
