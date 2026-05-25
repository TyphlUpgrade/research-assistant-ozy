"""
THE iter-1 CRITICAL #1 regression test (PR 1.3).

Pins the cache-bypass defect fix: `_cmd_brief` MUST invoke
`run_screeners_and_journal` on BOTH the cache-hit and cache-miss branches.
Prior to PR 1.3 the cache-hit branch silently skipped screeners.

Covers:
- Cache-miss: pipeline fires with cache_branch="miss"
- Cache-hit:  pipeline fires with cache_branch="hit" (THE bug)
- --skip-screeners suppresses pipeline on both branches + emits skip breadcrumb
- Breadcrumb log line appears on both branches
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from research_assistant import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _today_et_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _write_cache(base: Path, date_et: str, *, with_setups: bool = False) -> Path:
    briefs_dir = base / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{date_et}.json"
    payload: dict = {
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
    }
    if with_setups:
        payload["setups"] = []
    path.write_text(json.dumps(payload, indent=2))
    return path


class _NoopAdapter:
    """Adapter stub for the cache-hit re-fetch path. Returns no real data —
    the pipeline spy never inspects the inputs, so blanks are fine."""
    def __init__(self) -> None:
        self.research_base = None

    async def fetch_bars(self, *a, **kw):  # noqa: D401
        return None

    async def fetch_quote(self, *a, **kw):  # noqa: D401
        class _Q:
            last = None
        return _Q()

    async def fetch_news(self, *a, **kw):  # noqa: D401
        return []

    async def fetch_price_at(self, *a, **kw):  # noqa: D401
        return None


# ---------------------------------------------------------------------------
# Cache-miss branch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_fires_on_cache_miss(tmp_path: Path, monkeypatch) -> None:
    """No cache file → build_brief runs → pipeline MUST fire with
    cache_branch='miss'."""
    # No cache file present. Provide a watchlist so --static-only resolves
    # a non-empty universe.
    (tmp_path / "watchlist.txt").write_text("XLK\nXLF\n")
    spy = AsyncMock(return_value=[])
    # Patch the symbol that `_cmd_brief` imports locally.
    monkeypatch.setattr(
        "research_assistant.screeners.run_screeners_and_journal", spy,
    )

    # Stub the heavy collaborators so we never touch network / Anthropic.
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )

    async def _fake_build_brief(**kwargs):
        from research_assistant.brief import Brief
        return Brief(
            date_et=_today_et_iso(),
            chain_id="20260525T093000-fresh",
            world_state={
                "regime": "bull-trending",
                "regime_confidence": 0.7,
                "dispersion": 0.3,
                "rationale": "Fresh test brief.",
                "macro_signals": {"vix_level": 14.0, "vix_trend": "flat",
                                  "active_catalysts": []},
            },
            items=[],
            cost_usd=0.0,
            discovered_universe=list(kwargs.get("universe", [])),
        )
    monkeypatch.setattr("research_assistant.brief.build_brief", _fake_build_brief)

    async def _fake_world(*a, **kw):
        return {"sector_performance": {}}
    async def _fake_watchlist_data(*a, **kw):
        return ({}, {})
    monkeypatch.setattr(
        "research_assistant.data_loader.build_world_state_input", _fake_world,
    )
    monkeypatch.setattr(
        "research_assistant.data_loader.load_watchlist_data", _fake_watchlist_data,
    )

    async def _fake_insider(*a, **kw):
        return {}
    monkeypatch.setattr(
        "research_assistant.edgar.load_insider_activities_batch", _fake_insider,
    )

    async def _fake_discover(*a, **kw):
        return ["XLK", "XLF"]
    monkeypatch.setattr("research_assistant.universe.discover_universe", _fake_discover)

    # No EdgarClient connection — short-circuit the context manager.
    class _EdgarClientStub:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
    monkeypatch.setattr("research_assistant.edgar.EdgarClient", _EdgarClientStub)

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "--quiet",
        "brief",
        "--static-only",
    ])
    rc = await cli._cmd_brief(args)
    assert rc == 0
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs.get("cache_branch") == "miss"


# ---------------------------------------------------------------------------
# Cache-hit branch — THE regression test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_fires_on_cache_hit(tmp_path: Path, monkeypatch) -> None:
    """Cache file exists for today → pipeline MUST STILL fire with
    cache_branch='hit'. This is the iter-1 CRITICAL #1 regression sentinel:
    if this test ever fails, the cache-bypass defect has returned."""
    _write_cache(tmp_path, _today_et_iso(), with_setups=True)

    spy = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "research_assistant.screeners.run_screeners_and_journal", spy,
    )
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )

    async def _fake_world(*a, **kw):
        return {"sector_performance": {}}
    async def _fake_watchlist_data(*a, **kw):
        return ({}, {})
    monkeypatch.setattr(
        "research_assistant.data_loader.build_world_state_input", _fake_world,
    )
    monkeypatch.setattr(
        "research_assistant.data_loader.load_watchlist_data", _fake_watchlist_data,
    )

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "--quiet",
        "brief",
    ])
    rc = await cli._cmd_brief(args)
    assert rc == 0
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs.get("cache_branch") == "hit", (
        "iter-1 CRITICAL #1 regression: pipeline must fire on cache-hit too. "
        f"Got cache_branch={kwargs.get('cache_branch')!r}"
    )


# ---------------------------------------------------------------------------
# --skip-screeners suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_screeners_flag_suppresses_pipeline(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """--skip-screeners → pipeline NOT called + skip breadcrumb on stderr."""
    _write_cache(tmp_path, _today_et_iso(), with_setups=True)

    spy = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "research_assistant.screeners.run_screeners_and_journal", spy,
    )
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )

    args = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "--quiet",
        "brief",
        "--skip-screeners",
    ])
    rc = await cli._cmd_brief(args)
    assert rc == 0
    spy.assert_not_awaited()
    err = capsys.readouterr().err
    assert "[skip-screeners]" in err


# ---------------------------------------------------------------------------
# Breadcrumb log emitted on both branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breadcrumb_emitted_on_both_branches(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """`screeners.pipeline cache_branch=hit alert_count=N` log line appears
    on cache-hit, and the corresponding `cache_branch=miss` line appears on
    cache-miss. Verified via the REAL run_screeners_and_journal (no spy) so
    the log assertion exercises the actual breadcrumb."""
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter", _NoopAdapter,
    )

    async def _fake_world(*a, **kw):
        return {"sector_performance": {}}
    async def _fake_watchlist_data(*a, **kw):
        return ({}, {})
    monkeypatch.setattr(
        "research_assistant.data_loader.build_world_state_input", _fake_world,
    )
    monkeypatch.setattr(
        "research_assistant.data_loader.load_watchlist_data", _fake_watchlist_data,
    )

    # ----- cache-hit -----
    _write_cache(tmp_path, _today_et_iso(), with_setups=True)
    args_hit = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief",
    ])
    with caplog.at_level(logging.INFO, logger="research_assistant.screeners._pipeline"):
        caplog.clear()
        rc = await cli._cmd_brief(args_hit)
    assert rc == 0
    hit_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "cache_branch=hit" in hit_messages
    assert "alert_count=" in hit_messages

    # ----- cache-miss -----
    # Wipe the cache so the next invocation hits the cache-miss branch.
    (tmp_path / "briefs" / f"{_today_et_iso()}.json").unlink()
    # Provide a watchlist so --static-only resolves a non-empty universe.
    (tmp_path / "watchlist.txt").write_text("XLK\nXLF\n")

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
    monkeypatch.setattr("research_assistant.brief.build_brief", _fake_build_brief)

    async def _fake_insider(*a, **kw):
        return {}
    monkeypatch.setattr(
        "research_assistant.edgar.load_insider_activities_batch", _fake_insider,
    )

    async def _fake_discover(*a, **kw):
        return ["XLK", "XLF"]
    monkeypatch.setattr("research_assistant.universe.discover_universe", _fake_discover)

    class _EdgarClientStub:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
    monkeypatch.setattr("research_assistant.edgar.EdgarClient", _EdgarClientStub)

    args_miss = cli._build_parser().parse_args([
        "--base", str(tmp_path), "--quiet", "brief", "--static-only",
    ])
    with caplog.at_level(logging.INFO, logger="research_assistant.screeners._pipeline"):
        caplog.clear()
        rc = await cli._cmd_brief(args_miss)
    assert rc == 0
    miss_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "cache_branch=miss" in miss_messages
    assert "alert_count=" in miss_messages
