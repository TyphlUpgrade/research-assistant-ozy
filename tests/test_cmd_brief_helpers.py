"""
Focused tests on the PR 2A.0 helpers extracted from `_cmd_brief`:

- `_PipelineInputs` dataclass
- `_pipeline_inputs_for_universe(...)` — shared loader for both branches
- `_load_or_build_brief(...)` — cache routing + Brief assembly

These tests are independent of `_cmd_brief` itself; the iter-1 CRITICAL #1
regression sentinel (`test_cmd_brief_screener_invocation.py`) still pins the
end-to-end pipeline contract.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from research_assistant import cli


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _today_et_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _write_cache(base: Path, date_et: str) -> Path:
    briefs_dir = base / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{date_et}.json"
    payload = {
        "date_et": date_et,
        "chain_id": "20260525T093000-cached",
        "world_state": {
            "regime": "bull-trending",
            "regime_confidence": 0.7,
            "dispersion": 0.3,
            "rationale": "Cached test brief.",
            "macro_signals": {"vix_level": 14.0, "vix_trend": "flat",
                              "active_catalysts": []},
        },
        "items": [],
        "cost_usd": 0.0,
        "discovered_universe": ["XLK", "XLF"],
        "setups": [],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


class _NoopAdapter:
    """Adapter stub — pipeline tests never inspect loader internals."""
    def __init__(self) -> None:
        self.research_base = None


def _stub_loaders(monkeypatch, *, sector_perf: dict | None = None,
                  ticker_data: dict | None = None,
                  headlines: dict | None = None):
    """Patch the data_loader + edgar collaborators with deterministic
    stubs. Returns the (world_state_input, ticker_data, headlines, insider)
    fixtures the stubs will return — so tests can assert on threading."""
    ws_input = {"sector_performance": sector_perf if sector_perf is not None
                else {"XLK": 0.02, "XLF": 0.01}}
    td = ticker_data if ticker_data is not None else {
        "XLK": {"sector": "Technology", "return_30d": 0.05},
        "XLF": {"sector": "Financials", "return_30d": 0.03},
    }
    hl = headlines if headlines is not None else {"XLK": [], "XLF": []}
    insider_results: dict = {"XLK": None, "XLF": None}

    async def _fake_world(adapter, watchlist_news_for=None):
        return ws_input

    async def _fake_watchlist_data(universe, adapter):
        return (td, hl)

    async def _fake_insider(*a, **kw):
        return insider_results

    class _EdgarClientStub:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(
        "research_assistant.data_loader.build_world_state_input", _fake_world,
    )
    monkeypatch.setattr(
        "research_assistant.data_loader.load_watchlist_data", _fake_watchlist_data,
    )
    monkeypatch.setattr(
        "research_assistant.edgar.load_insider_activities_batch", _fake_insider,
    )
    monkeypatch.setattr("research_assistant.edgar.EdgarClient", _EdgarClientStub)
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )
    return ws_input, td, hl, insider_results


# ---------------------------------------------------------------------------
# _pipeline_inputs_for_universe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_inputs_for_universe_produces_all_fields(monkeypatch) -> None:
    """Happy path: every documented field is populated, including the new
    headlines_per_ticker + insider_activities (when fetch_insider=True)."""
    ws_input, td, hl, insider = _stub_loaders(monkeypatch)

    inputs = await cli._pipeline_inputs_for_universe(
        ["XLK", "XLF"], news_seed=["XLK"], fetch_insider=True,
    )

    assert isinstance(inputs, cli._PipelineInputs)
    assert inputs.universe == ["XLK", "XLF"]
    assert inputs.ticker_data == td
    assert inputs.headlines_per_ticker == hl
    assert inputs.insider_activities == insider
    # sector_performance flows through unchanged when the world-state input
    # already supplies it.
    assert inputs.world_state["sector_performance"] == ws_input["sector_performance"]
    # Forward-compat fields default to None.
    assert inputs.earnings_calendar is None
    assert inputs.sector_breadth is None


@pytest.mark.asyncio
async def test_pipeline_inputs_handles_missing_sector_performance(
    monkeypatch,
) -> None:
    """When build_world_state_input returns no sector_performance, the
    OR-fallback to compute_sector_performance(ticker_data) fires. This is
    the only place that knows about that fallback (per plan §1F)."""
    ws_input, td, hl, insider = _stub_loaders(
        monkeypatch,
        sector_perf={},          # falsy → OR-fallback should fire
        ticker_data={"XLK": {"sector": "Technology", "return_30d": 0.05}},
    )

    sentinel = {"sentinel": "computed"}
    monkeypatch.setattr(
        "research_assistant.screeners.compute_sector_performance",
        lambda data: sentinel,
    )

    inputs = await cli._pipeline_inputs_for_universe(
        ["XLK"], news_seed=["XLK"], fetch_insider=False,
    )
    assert inputs.world_state["sector_performance"] is sentinel


# ---------------------------------------------------------------------------
# _load_or_build_brief — cache-hit path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_or_build_brief_cache_hit_returns_cached_brief_plus_fresh_inputs(
    tmp_path: Path, monkeypatch,
) -> None:
    """Cache present + no --refresh → cached Brief returned, BUT pipeline
    inputs are freshly loaded (Option α per plan §1F)."""
    cache_path = _write_cache(tmp_path, _today_et_iso())
    ws_input, td, hl, _insider = _stub_loaders(monkeypatch)

    # If build_brief were called, this would explode the test.
    build_brief_spy = AsyncMock(side_effect=AssertionError(
        "build_brief MUST NOT be called on cache-hit"
    ))
    monkeypatch.setattr("research_assistant.brief.build_brief", build_brief_spy)

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief",
    ])
    brief, inputs, branch = await cli._load_or_build_brief(
        args, tmp_path, cache_path,
    )

    assert branch == "hit"
    # Cached Brief came from disk (chain_id distinguishes it from a fresh build)
    assert brief.chain_id == "20260525T093000-cached"
    assert brief.discovered_universe == ["XLK", "XLF"]
    # Fresh inputs: ticker_data is populated from the (stubbed) loader,
    # not pulled from the cache file (which has none).
    assert inputs.ticker_data == td
    assert inputs.universe == ["XLK", "XLF"]
    build_brief_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_or_build_brief_skip_screeners_on_cache_hit_skips_loader(
    tmp_path: Path, monkeypatch,
) -> None:
    """--skip-screeners on cache-hit short-circuits the loader re-fetch
    (preserves the ~5s fast path). pipeline_inputs.ticker_data is {}."""
    cache_path = _write_cache(tmp_path, _today_et_iso())

    # If the loaders fire, the test fails loud.
    async def _boom(*a, **kw):
        raise AssertionError("loader MUST NOT be called when --skip-screeners")
    monkeypatch.setattr(
        "research_assistant.data_loader.build_world_state_input", _boom,
    )
    monkeypatch.setattr(
        "research_assistant.data_loader.load_watchlist_data", _boom,
    )
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief", "--skip-screeners",
    ])
    brief, inputs, branch = await cli._load_or_build_brief(
        args, tmp_path, cache_path,
    )
    assert branch == "hit"
    assert inputs.ticker_data == {}
    assert inputs.universe == brief.discovered_universe


# ---------------------------------------------------------------------------
# _load_or_build_brief — cache-miss path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_or_build_brief_cache_miss_runs_full_build(
    tmp_path: Path, monkeypatch,
) -> None:
    """No cache → build_brief IS called, fresh inputs returned, branch='miss'."""
    (tmp_path / "watchlist.txt").write_text("XLK\nXLF\n")
    ws_input, td, hl, _insider = _stub_loaders(monkeypatch)

    async def _fake_build_brief(**kwargs):
        from research_assistant.brief import Brief
        return Brief(
            date_et=_today_et_iso(),
            chain_id="20260525T093000-fresh",
            world_state={"regime": "bull-trending"},
            items=[],
            cost_usd=0.0,
            discovered_universe=list(kwargs.get("universe", [])),
        )
    build_brief_spy = AsyncMock(side_effect=_fake_build_brief)
    monkeypatch.setattr("research_assistant.brief.build_brief", build_brief_spy)

    cache_path = tmp_path / "briefs" / f"{_today_et_iso()}.json"
    args = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief", "--static-only",
    ])
    brief, inputs, branch = await cli._load_or_build_brief(
        args, tmp_path, cache_path,
    )

    assert branch == "miss"
    assert brief.chain_id == "20260525T093000-fresh"
    build_brief_spy.assert_awaited_once()
    # Fresh inputs reflect the loader stubs.
    assert inputs.ticker_data == td
    assert inputs.universe == ["XLK", "XLF"]


@pytest.mark.asyncio
async def test_load_or_build_brief_refresh_flag_forces_rebuild(
    tmp_path: Path, monkeypatch,
) -> None:
    """Cache exists BUT --refresh set → build_brief IS called (cache bypassed)."""
    cache_path = _write_cache(tmp_path, _today_et_iso())
    (tmp_path / "watchlist.txt").write_text("XLK\nXLF\n")
    _stub_loaders(monkeypatch)

    async def _fake_build_brief(**kwargs):
        from research_assistant.brief import Brief
        return Brief(
            date_et=_today_et_iso(),
            chain_id="20260525T093000-refreshed",
            world_state={"regime": "choppy"},
            items=[],
            cost_usd=0.0,
            discovered_universe=list(kwargs.get("universe", [])),
        )
    build_brief_spy = AsyncMock(side_effect=_fake_build_brief)
    monkeypatch.setattr("research_assistant.brief.build_brief", build_brief_spy)

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief",
        "--refresh", "--static-only",
    ])
    brief, inputs, branch = await cli._load_or_build_brief(
        args, tmp_path, cache_path,
    )

    assert branch == "miss"
    # Did NOT come from the cached file (which had chain_id "...-cached").
    assert brief.chain_id == "20260525T093000-refreshed"
    build_brief_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# Empty-universe error path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_or_build_brief_empty_universe_raises(
    tmp_path: Path, monkeypatch,
) -> None:
    """No cache, no pins, --static-only → empty universe → _EmptyUniverseError
    (the caller renders ERROR + returns exit 1)."""
    cache_path = tmp_path / "briefs" / f"{_today_et_iso()}.json"
    args = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief", "--static-only",
    ])
    with pytest.raises(cli._EmptyUniverseError):
        await cli._load_or_build_brief(args, tmp_path, cache_path)
