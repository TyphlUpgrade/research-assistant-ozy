"""Tests for research_assistant.universe — discovery wrapper around Ozy's UniverseFetcher."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from research_assistant.universe import (
    _get_fetcher,
    _reset_fetcher_cache,
    discover_universe,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_fetcher(universe: list[str]) -> MagicMock:
    """Build a MagicMock UniverseFetcher whose get_universe() returns `universe`."""
    f = MagicMock()
    f.get_universe = AsyncMock(return_value=universe)
    return f


@pytest.fixture(autouse=True)
def _clear_singleton():
    """Each test starts with a clean module-level fetcher singleton."""
    _reset_fetcher_cache()
    yield
    _reset_fetcher_cache()


def test_empty_discovery_yields_pins_only() -> None:
    result = _run(discover_universe(pins=["AAPL", "NVDA"], cap=30, fetcher=_fake_fetcher([])))
    assert result == ["AAPL", "NVDA"]


def test_pins_lead_result_order() -> None:
    result = _run(discover_universe(
        pins=["AAPL", "NVDA"], cap=30, fetcher=_fake_fetcher(["TSLA", "AMD", "META"]),
    ))
    assert result[:2] == ["AAPL", "NVDA"]
    assert result[2:] == ["TSLA", "AMD", "META"]


def test_cap_enforced() -> None:
    result = _run(discover_universe(
        pins=["AAPL"], cap=10, fetcher=_fake_fetcher([f"T{i}" for i in range(50)]),
    ))
    assert len(result) == 10
    assert result[0] == "AAPL"


def test_dedupe_pins_against_discovered() -> None:
    # AAPL appears in both pins and discovery — must only appear once.
    result = _run(discover_universe(
        pins=["AAPL", "NVDA"], cap=30, fetcher=_fake_fetcher(["AAPL", "TSLA", "AMD"]),
    ))
    assert result == ["AAPL", "NVDA", "TSLA", "AMD"]


def test_pins_normalized_to_uppercase() -> None:
    result = _run(discover_universe(
        pins=["aapl", "Nvda"], cap=30, fetcher=_fake_fetcher(["tsla"]),
    ))
    assert result == ["AAPL", "NVDA", "TSLA"]


def test_cap_zero_returns_empty() -> None:
    fetcher = _fake_fetcher(["X", "Y"])
    result = _run(discover_universe(pins=["AAPL"], cap=0, fetcher=fetcher))
    assert result == []
    fetcher.get_universe.assert_not_called()


def test_pins_alone_exceed_cap_skips_fetch() -> None:
    fetcher = _fake_fetcher(["TSLA"])
    result = _run(discover_universe(pins=["A", "B", "C", "D"], cap=3, fetcher=fetcher))
    assert result == ["A", "B", "C"]
    fetcher.get_universe.assert_not_called()


# ---------------------------------------------------------------------------
# Singleton behavior
# ---------------------------------------------------------------------------

def test_singleton_reused_across_calls() -> None:
    """_get_fetcher returns the same instance on repeated calls (warms Ozy's 24h cache)."""
    a = _get_fetcher()
    b = _get_fetcher()
    assert a is b


def test_reset_cache_yields_new_instance() -> None:
    a = _get_fetcher()
    _reset_fetcher_cache()
    b = _get_fetcher()
    assert a is not b


def test_injected_fetcher_bypasses_singleton() -> None:
    """When fetcher= is provided, the module singleton is not constructed."""
    fetcher = _fake_fetcher(["X"])
    _run(discover_universe(pins=["AAPL"], cap=5, fetcher=fetcher))
    # Singleton was never asked for one
    from research_assistant import universe as universe_mod
    assert universe_mod._FETCHER is None
