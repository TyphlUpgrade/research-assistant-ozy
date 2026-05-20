"""Tests for research_assistant.universe — discovery wrapper around Ozy's UniverseFetcher."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from research_assistant.universe import discover_universe


def _run(coro):
    return asyncio.run(coro)


@patch("research_assistant.universe.UniverseFetcher")
def test_empty_discovery_yields_pins_only(mock_fetcher_cls) -> None:
    mock_fetcher_cls.return_value.get_universe = AsyncMock(return_value=[])
    result = _run(discover_universe(pins=["AAPL", "NVDA"], cap=30))
    assert result == ["AAPL", "NVDA"]


@patch("research_assistant.universe.UniverseFetcher")
def test_pins_lead_result_order(mock_fetcher_cls) -> None:
    mock_fetcher_cls.return_value.get_universe = AsyncMock(
        return_value=["TSLA", "AMD", "META"]
    )
    result = _run(discover_universe(pins=["AAPL", "NVDA"], cap=30))
    assert result[:2] == ["AAPL", "NVDA"]
    assert result[2:] == ["TSLA", "AMD", "META"]


@patch("research_assistant.universe.UniverseFetcher")
def test_cap_enforced(mock_fetcher_cls) -> None:
    mock_fetcher_cls.return_value.get_universe = AsyncMock(
        return_value=[f"T{i}" for i in range(50)]
    )
    result = _run(discover_universe(pins=["AAPL"], cap=10))
    assert len(result) == 10
    assert result[0] == "AAPL"


@patch("research_assistant.universe.UniverseFetcher")
def test_dedupe_pins_against_discovered(mock_fetcher_cls) -> None:
    # AAPL appears in both pins and discovery — must only appear once.
    mock_fetcher_cls.return_value.get_universe = AsyncMock(
        return_value=["AAPL", "TSLA", "AMD"]
    )
    result = _run(discover_universe(pins=["AAPL", "NVDA"], cap=30))
    assert result == ["AAPL", "NVDA", "TSLA", "AMD"]


@patch("research_assistant.universe.UniverseFetcher")
def test_pins_normalized_to_uppercase(mock_fetcher_cls) -> None:
    mock_fetcher_cls.return_value.get_universe = AsyncMock(return_value=["tsla"])
    result = _run(discover_universe(pins=["aapl", "Nvda"], cap=30))
    assert result == ["AAPL", "NVDA", "TSLA"]


@patch("research_assistant.universe.UniverseFetcher")
def test_cap_zero_returns_empty(mock_fetcher_cls) -> None:
    mock_fetcher_cls.return_value.get_universe = AsyncMock(return_value=["X", "Y"])
    result = _run(discover_universe(pins=["AAPL"], cap=0))
    assert result == []


@patch("research_assistant.universe.UniverseFetcher")
def test_pins_alone_exceed_cap_skips_fetch(mock_fetcher_cls) -> None:
    # When pins already meet/exceed cap, the fetcher should not be invoked.
    get_uni = AsyncMock(return_value=["TSLA"])
    mock_fetcher_cls.return_value.get_universe = get_uni
    result = _run(discover_universe(pins=["A", "B", "C", "D"], cap=3))
    assert result == ["A", "B", "C"]
    get_uni.assert_not_called()
