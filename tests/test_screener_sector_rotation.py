"""
Tests for the sector_rotation screener (PR 1.2).

Covers:
- Fires when an ETF moves from bottom-half to top-quartile RS rank
- Suppressed when no shift occurs (all ETFs stable)
- Suppressed when ETF was already in top quartile (not a fresh rotation)
- Degrades silently when sector_performance is None/empty (returns [])
- Pure function: no I/O, no side effects
- Skips ETF with WARN when price is missing from both sources
- Emits multiple candidates when multiple sectors qualify
"""
from __future__ import annotations

import logging
import math

import pytest

from research_assistant.screeners.sector_rotation import _SECTOR_ETFS, evaluate
from research_assistant.screeners import SetupCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sector_perf(
    returns_5d: dict[str, float],
    returns_30d: dict[str, float],
    prices: dict[str, float] | None = None,
) -> dict:
    """Build a sector_performance dict from return maps."""
    result: dict[str, dict] = {}
    for etf in _SECTOR_ETFS:
        snap: dict = {
            "symbol": etf,
            "return_5d": returns_5d.get(etf, 0.0),
            "return_30d": returns_30d.get(etf, 0.0),
        }
        if prices and etf in prices:
            snap["price"] = prices[etf]
        result[etf] = snap
    return result


def _world_state(sector_perf) -> dict:
    return {
        "sector_performance": sector_perf,
        "asof": "2026-05-23",
    }


# ---------------------------------------------------------------------------
# Core fire condition
# ---------------------------------------------------------------------------

def test_fires_on_bottom_half_to_top_quartile_shift():
    """XLK: 30d-rank=7 (bottom half of 11), 5d-rank=2 (top quartile ≤3). Fires."""
    # Build returns so XLK has rank 7 on 30d and rank 2 on 5d.
    # N=11, bottom_half: rank > 5.5 → rank ≥ 6; top_quartile: rank ≤ ceil(11/4)=3.
    # Give all other ETFs predictable returns so we can control ranks precisely.
    etfs = list(_SECTOR_ETFS)  # 11 ETFs

    # 30d returns: XLK gets 5th-worst (rank 7 = index 6 from top)
    # Make 6 ETFs above XLK and 4 below it on 30d
    returns_30d = {}
    returns_30d["XLK"] = 0.04   # rank 7
    above_etfs = [e for e in etfs if e != "XLK"][:6]
    below_etfs = [e for e in etfs if e != "XLK"][6:]
    for i, etf in enumerate(above_etfs):
        returns_30d[etf] = 0.10 + i * 0.01   # ranks 1-6
    for i, etf in enumerate(below_etfs):
        returns_30d[etf] = 0.01 - i * 0.01   # ranks 8-11

    # 5d returns: XLK gets rank 2 (second best)
    returns_5d = {}
    returns_5d["XLK"] = 0.09  # rank 2
    other_etfs = [e for e in etfs if e != "XLK"]
    returns_5d[other_etfs[0]] = 0.10  # rank 1
    for i, etf in enumerate(other_etfs[1:]):
        returns_5d[etf] = 0.01 - i * 0.005  # ranks 3-11

    prices = {etf: 100.0 + i for i, etf in enumerate(etfs)}
    sp = _make_sector_perf(returns_5d, returns_30d, prices)
    candidates = evaluate(ticker_data={}, world_state=_world_state(sp))

    xlk_hits = [c for c in candidates if c.ticker == "XLK"]
    assert len(xlk_hits) == 1, f"Expected XLK to fire; got candidates: {[c.ticker for c in candidates]}"

    c = xlk_hits[0]
    assert c.screener == "sector_rotation"
    assert c.asof == "2026-05-23"
    assert c.entry_price == pytest.approx(prices["XLK"])
    assert c.evidence["sector_etf"] == "XLK"
    assert c.evidence["rs_rank_now"] == 2
    assert c.evidence["rs_rank_prior"] == 7
    assert c.evidence["basis_days"] == 30
    assert "return_5d" in c.evidence
    assert "return_20d" in c.evidence


def test_no_shift_suppresses():
    """All ETFs keep same relative order on 5d and 30d. No bottom→top transition."""
    etfs = list(_SECTOR_ETFS)
    # Same return ordering on both windows — no rank change
    returns_30d = {etf: 0.10 - i * 0.01 for i, etf in enumerate(etfs)}
    returns_5d = {etf: 0.05 - i * 0.005 for i, etf in enumerate(etfs)}
    prices = {etf: 50.0 for etf in etfs}

    sp = _make_sector_perf(returns_5d, returns_30d, prices)
    candidates = evaluate(ticker_data={}, world_state=_world_state(sp))
    assert candidates == []


def test_top_quartile_already_suppresses():
    """XLK: 30d-rank=2, 5d-rank=1. Already in top quartile on 30d — not a fresh rotation."""
    etfs = list(_SECTOR_ETFS)
    # XLK: rank 2 on both windows — was already in top quartile, no bottom→top move
    returns_30d = {etf: 0.10 - i * 0.01 for i, etf in enumerate(etfs)}  # XLK=etfs[0] → rank 1
    # Swap XLK and etfs[1] for 30d so XLK is rank 2
    returns_30d["XLK"] = 0.095
    returns_30d[etfs[1]] = 0.10

    returns_5d = dict(returns_30d)  # same order → XLK rank 2 on 5d too

    prices = {etf: 100.0 for etf in etfs}
    sp = _make_sector_perf(returns_5d, returns_30d, prices)
    candidates = evaluate(ticker_data={}, world_state=_world_state(sp))

    xlk_hits = [c for c in candidates if c.ticker == "XLK"]
    assert xlk_hits == [], "XLK was already top-quartile; should NOT appear as rotation"


def test_missing_sector_data_degrades_silently(caplog):
    """world_state with sector_performance=None → empty list + WARN logged."""
    with caplog.at_level(logging.WARNING, logger="research_assistant.screeners.sector_rotation"):
        result = evaluate(ticker_data={}, world_state={"sector_performance": None})

    assert result == []
    warn_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "degraded=missing_sector_data" in warn_messages


def test_evaluate_is_pure():
    """evaluate() with minimal empty inputs doesn't crash and returns empty list."""
    result = evaluate(ticker_data={}, world_state={})
    assert result == []


def test_missing_price_skips_with_warn(caplog):
    """XLK qualifies for rotation but has no price in either source → skipped + WARN."""
    etfs = list(_SECTOR_ETFS)

    # Same setup as test_fires_on_bottom_half_to_top_quartile_shift but NO price for XLK
    returns_30d = {etf: 0.10 - i * 0.01 for i, etf in enumerate(etfs)}
    returns_30d["XLK"] = 0.04  # rank 7

    returns_5d = {etf: 0.01 - i * 0.005 for i, etf in enumerate(etfs)}
    returns_5d[etfs[0]] = 0.10   # rank 1
    returns_5d["XLK"] = 0.09    # rank 2

    # Build sector_perf WITHOUT XLK price (omit from prices dict)
    prices = {etf: 100.0 for etf in etfs if etf != "XLK"}
    sp = _make_sector_perf(returns_5d, returns_30d, prices)
    # Also remove price key from XLK snap explicitly
    if "price" in sp.get("XLK", {}):
        del sp["XLK"]["price"]

    with caplog.at_level(logging.WARNING, logger="research_assistant.screeners.sector_rotation"):
        result = evaluate(ticker_data={}, world_state=_world_state(sp))

    xlk_hits = [c for c in result if c.ticker == "XLK"]
    assert xlk_hits == [], "XLK missing price → should be skipped"

    warn_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "skipped=missing_price" in warn_messages
    assert "ticker=XLK" in warn_messages


def test_multiple_sectors_fire():
    """Two sector ETFs both qualify (bottom-half → top-quartile); both emitted."""
    etfs = list(_SECTOR_ETFS)  # 11 ETFs
    # N=11, top_quartile cutoff = ceil(11/4) = 3, bottom_half cutoff = 5.5

    # Arrange returns so XLK (rank 2 on 5d, rank 7 on 30d) and XLF (rank 3 on 5d, rank 8 on 30d) both fire.
    returns_30d = {}
    returns_5d = {}

    # Assign 30d returns: XLK rank 7, XLF rank 8, rest fill ranks 1-6, 9-11
    non_target = [e for e in etfs if e not in ("XLK", "XLF")]
    # ranks 1-6 on 30d:
    for i, etf in enumerate(non_target[:6]):
        returns_30d[etf] = 0.12 - i * 0.01
    returns_30d["XLK"] = 0.05   # rank 7
    returns_30d["XLF"] = 0.04   # rank 8
    # ranks 9-11 on 30d:
    for i, etf in enumerate(non_target[6:]):
        returns_30d[etf] = 0.02 - i * 0.01

    # Assign 5d returns: non_target[0] rank 1, XLK rank 2, XLF rank 3, rest ranks 4-11
    returns_5d[non_target[0]] = 0.15   # rank 1
    returns_5d["XLK"] = 0.14           # rank 2
    returns_5d["XLF"] = 0.13           # rank 3
    for i, etf in enumerate(non_target[1:]):
        returns_5d[etf] = 0.05 - i * 0.01  # ranks 4-11

    prices = {etf: 100.0 for etf in etfs}
    sp = _make_sector_perf(returns_5d, returns_30d, prices)
    candidates = evaluate(ticker_data={}, world_state=_world_state(sp))

    tickers = [c.ticker for c in candidates]
    assert "XLK" in tickers, f"XLK should fire; got: {tickers}"
    assert "XLF" in tickers, f"XLF should fire; got: {tickers}"
    assert len(candidates) == 2, f"Expected exactly 2 candidates; got {len(candidates)}: {tickers}"
