"""
Tests for `scoreboard` CLI subcommand + the underlying enrichment/strat logic
(FOLLOWUPS #19, Phase 1).

Covers:
- Empty journal renders the "no data" branch
- Verdict stratification groups by `skeptic_verdict` and computes median/p25/p75
- Decile analysis sorts by composite_conviction and buckets correctly
- Forward-return enrichment fetches once and caches in the sidecar
- Cache hit avoids re-fetching on a second run
- Matured horizon: a cached row with null `return_5d` whose 5d window has
  since elapsed gets re-fetched and the cache is updated
- Render output contains the expected headline sections
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_assistant import scoreboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stage2_row(
    *,
    ticker: str,
    asof: str,
    recorded_at: str | None = None,
    composite: float = 0.50,
    verdict: str = "AGREE",
    decision_tag: str = "WATCH",
    bull: str = "bull",
    bear: str = "bear",
) -> dict:
    return {
        "schema_version": 1,
        "ticker": ticker,
        "asof": asof,
        "recorded_at": recorded_at or f"{asof}T12:00:00.000000+00:00",
        "bull_anchor": bull,
        "bear_anchor": bear,
        "conviction": {
            "technical": composite,
            "fundamental": composite,
            "catalyst": composite,
            "regime": composite,
        },
        "composite_conviction": composite,
        "decision_tag": decision_tag,
        "skeptic_verdict": verdict,
    }


def _write_stage2(base: Path, ticker: str, rows: list[dict]) -> Path:
    path = base / "stage2" / f"{ticker}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


class _CannedAdapter:
    """Stub adapter that returns prices from a dict[(ticker, date), float]
    and counts fetch invocations so cache-hit tests can assert no calls."""

    def __init__(self, prices: dict[tuple[str, date], float]):
        self.prices = prices
        self.fetch_count = 0

    async def fetch_price_at(self, ticker: str, target):  # noqa: D401
        self.fetch_count += 1
        return self.prices.get((ticker, target))


# ---------------------------------------------------------------------------
# I/O — reading journal + cache
# ---------------------------------------------------------------------------

def test_read_all_stage2_empty(tmp_path: Path):
    assert scoreboard.read_all_stage2(tmp_path) == []


def test_read_all_stage2_multi_ticker(tmp_path: Path):
    _write_stage2(tmp_path, "AAA", [
        _stage2_row(ticker="AAA", asof="2026-05-01"),
        _stage2_row(ticker="AAA", asof="2026-05-02"),
    ])
    _write_stage2(tmp_path, "BBB", [
        _stage2_row(ticker="BBB", asof="2026-05-01"),
    ])
    rows = scoreboard.read_all_stage2(tmp_path)
    assert len(rows) == 3
    assert sorted({r["ticker"] for r in rows}) == ["AAA", "BBB"]


def test_read_all_stage2_skips_malformed(tmp_path: Path):
    path = tmp_path / "stage2" / "AAA.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_stage2_row(ticker="AAA", asof="2026-05-01")) + "\n")
        f.write("not valid json\n")
        f.write("[]\n")  # valid json but non-dict
        f.write(json.dumps(_stage2_row(ticker="AAA", asof="2026-05-02")) + "\n")
    rows = scoreboard.read_all_stage2(tmp_path)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Stratification logic — pure functions, no I/O
# ---------------------------------------------------------------------------

def _scored(
    *,
    ticker: str = "X",
    composite: float,
    verdict: str,
    r5: float | None = None,
    r10: float | None = None,
    r30: float | None = None,
) -> scoreboard.ScoredEntry:
    return scoreboard.ScoredEntry(
        ticker=ticker,
        asof="2026-05-01",
        recorded_at="2026-05-01T12:00:00+00:00",
        composite_conviction=composite,
        skeptic_verdict=verdict,
        decision_tag="WATCH",
        entry_price=100.0,
        return_5d=r5,
        return_10d=r10,
        return_30d=r30,
    )


def test_stratify_by_verdict_basic():
    entries = [
        _scored(composite=0.5, verdict="AGREE", r10=0.10),
        _scored(composite=0.5, verdict="AGREE", r10=0.20),
        _scored(composite=0.5, verdict="WEAKEN", r10=-0.05),
    ]
    out = scoreboard.stratify_by_verdict(entries, "return_10d")
    assert out["AGREE"]["count"] == 2
    assert out["AGREE"]["enriched_count"] == 2
    assert out["AGREE"]["median"] == pytest.approx(0.15)
    assert out["AGREE"]["small_n"] is True  # 2 < 5
    assert out["WEAKEN"]["count"] == 1
    assert out["WEAKEN"]["median"] == pytest.approx(-0.05)


def test_stratify_by_verdict_excludes_null_returns_from_stats():
    entries = [
        _scored(composite=0.5, verdict="AGREE", r10=None),
        _scored(composite=0.5, verdict="AGREE", r10=0.10),
    ]
    out = scoreboard.stratify_by_verdict(entries, "return_10d")
    assert out["AGREE"]["count"] == 2  # both counted in count
    assert out["AGREE"]["enriched_count"] == 1  # but only one has return
    assert out["AGREE"]["median"] == pytest.approx(0.10)


def test_decile_analysis_monotonic_signal():
    # Construct 20 entries where higher conviction → higher return (strong signal)
    entries = []
    for i in range(20):
        conv = 0.05 + i * 0.05
        ret = -0.05 + i * 0.01
        entries.append(_scored(composite=conv, verdict="AGREE", r10=ret))
    deciles = scoreboard.decile_analysis(entries, "return_10d")
    assert len(deciles) == 10
    # Medians should be strictly increasing
    medians = [d["median_return"] for d in deciles]
    assert medians == sorted(medians)
    # Bucket numbering ascending
    assert [d["bucket"] for d in deciles] == list(range(1, 11))


def test_decile_analysis_few_buckets_when_sparse():
    # N < 10 → bucket count clamps to N
    entries = [
        _scored(composite=0.2, verdict="AGREE", r10=0.0),
        _scored(composite=0.5, verdict="AGREE", r10=0.05),
        _scored(composite=0.8, verdict="AGREE", r10=0.10),
    ]
    deciles = scoreboard.decile_analysis(entries, "return_10d")
    assert len(deciles) == 3


def test_decile_analysis_excludes_null_returns():
    entries = [
        _scored(composite=0.2, verdict="AGREE", r10=None),
        _scored(composite=0.5, verdict="AGREE", r10=0.05),
        _scored(composite=0.8, verdict="AGREE", r10=0.10),
    ]
    deciles = scoreboard.decile_analysis(entries, "return_10d")
    assert len(deciles) == 2  # only the two enriched rows
    assert sum(d["count"] for d in deciles) == 2


# ---------------------------------------------------------------------------
# Enrichment + cache
# ---------------------------------------------------------------------------

def test_enrich_writes_cache_then_reads_on_repeat(tmp_path: Path):
    # asof far enough back that all three horizons should have elapsed.
    today = date.today()
    asof = today - timedelta(days=60)
    asof_str = asof.isoformat()
    row = _stage2_row(ticker="ABC", asof=asof_str, composite=0.6, verdict="AGREE")

    prices = {
        ("ABC", asof): 100.0,
        ("ABC", asof + timedelta(days=5)): 110.0,
        ("ABC", asof + timedelta(days=10)): 105.0,
        ("ABC", asof + timedelta(days=30)): 120.0,
    }
    adapter = _CannedAdapter(prices)

    # First run: fetches all 4 prices, writes cache.
    entries = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter, tmp_path)
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_price == pytest.approx(100.0)
    assert e.return_5d == pytest.approx(0.10)
    assert e.return_10d == pytest.approx(0.05)
    assert e.return_30d == pytest.approx(0.20)
    assert adapter.fetch_count == 4

    cache_path = tmp_path / "stage2_returns" / "ABC.jsonl"
    assert cache_path.exists()

    # Second run: cache hit, zero fetches.
    adapter2 = _CannedAdapter(prices)
    entries2 = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter2, tmp_path)
    )
    assert adapter2.fetch_count == 0
    assert entries2[0].return_30d == pytest.approx(0.20)


def test_enrich_handles_missing_entry_price(tmp_path: Path):
    today = date.today()
    asof = today - timedelta(days=60)
    row = _stage2_row(ticker="DEAD", asof=asof.isoformat())
    adapter = _CannedAdapter({})  # no prices known → entry_price = None
    entries = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter, tmp_path)
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_price is None
    assert e.return_5d is None
    assert e.return_10d is None
    assert e.return_30d is None
    # No horizon fetches after entry_price failed.
    assert adapter.fetch_count == 1
    # Failed-fetch rows must NOT pollute the cache — next run gets a fresh shot.
    cache_path = tmp_path / "stage2_returns" / "DEAD.jsonl"
    assert not cache_path.exists()


def test_enrich_retries_after_previously_failed_fetch(tmp_path: Path):
    """If a previous run failed to fetch entry_price (so didn't cache),
    a subsequent run with working adapter populates the row."""
    today = date.today()
    asof = today - timedelta(days=60)
    row = _stage2_row(ticker="RETRY", asof=asof.isoformat())

    # Run 1: adapter is broken (no prices).
    bad_adapter = _CannedAdapter({})
    entries1 = asyncio.run(scoreboard.enrich_stage2_rows([row], bad_adapter, tmp_path))
    assert entries1[0].entry_price is None
    assert not (tmp_path / "stage2_returns" / "RETRY.jsonl").exists()

    # Run 2: adapter now returns prices. Cache empty, so we retry.
    good_adapter = _CannedAdapter({
        ("RETRY", asof): 50.0,
        ("RETRY", asof + timedelta(days=5)): 55.0,
        ("RETRY", asof + timedelta(days=10)): 52.5,
        ("RETRY", asof + timedelta(days=30)): 60.0,
    })
    entries2 = asyncio.run(scoreboard.enrich_stage2_rows([row], good_adapter, tmp_path))
    assert entries2[0].entry_price == pytest.approx(50.0)
    assert entries2[0].return_5d == pytest.approx(0.10)
    assert entries2[0].return_30d == pytest.approx(0.20)
    assert (tmp_path / "stage2_returns" / "RETRY.jsonl").exists()


def test_enrich_future_horizons_stay_null(tmp_path: Path):
    today = date.today()
    asof = today - timedelta(days=2)  # 5d/10d/30d all still in the future
    row = _stage2_row(ticker="FUT", asof=asof.isoformat())
    adapter = _CannedAdapter({("FUT", asof): 100.0})
    entries = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter, tmp_path)
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_price == pytest.approx(100.0)
    assert e.return_5d is None
    assert e.return_10d is None
    assert e.return_30d is None


def test_enrich_refetches_when_cached_horizon_has_matured(tmp_path: Path):
    """A previously-cached row with null return_5d (because 5d hadn't elapsed
    when first cached) gets re-fetched once the 5d window has matured."""
    today = date.today()
    asof = today - timedelta(days=6)  # 5d elapsed, 10d/30d not
    asof_str = asof.isoformat()

    # Seed the cache with a stale row: 5d=null even though 5d window has elapsed.
    cache_path = tmp_path / "stage2_returns" / "ZZZ.jsonl"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stale = {
        "schema_version": 1,
        "recorded_at": f"{asof_str}T12:00:00+00:00",
        "ticker": "ZZZ",
        "asof": asof_str,
        "entry_price": 100.0,
        "return_5d": None,
        "return_10d": None,
        "return_30d": None,
        "enriched_at": (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat(),
    }
    with cache_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(stale) + "\n")

    row = _stage2_row(
        ticker="ZZZ",
        asof=asof_str,
        recorded_at=f"{asof_str}T12:00:00+00:00",
    )
    adapter = _CannedAdapter({
        ("ZZZ", asof): 100.0,
        ("ZZZ", asof + timedelta(days=5)): 108.0,
    })
    entries = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter, tmp_path)
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.return_5d == pytest.approx(0.08)
    # adapter was called — at minimum for the entry-price re-fetch + 5d.
    assert adapter.fetch_count >= 2


# ---------------------------------------------------------------------------
# Render — text output sanity
# ---------------------------------------------------------------------------

def test_render_empty():
    out = scoreboard.render_scoreboard([], horizon_field="return_10d")
    assert "Total Stage 2 entries: 0" in out
    assert "no Stage 2 journal data" in out


def test_render_with_entries_includes_both_sections():
    entries = [
        _scored(composite=0.3, verdict="AGREE", r10=0.05),
        _scored(composite=0.6, verdict="AGREE", r10=0.10),
        _scored(composite=0.4, verdict="WEAKEN", r10=-0.02),
        _scored(composite=0.7, verdict="WEAKEN", r10=-0.05),
    ]
    out = scoreboard.render_scoreboard(entries, horizon_field="return_10d")
    assert "# Scoreboard" in out
    assert "Total Stage 2 entries: 4" in out
    assert "## Verdict → 10d return" in out
    assert "## Conviction decile → 10d return" in out
    assert "**AGREE**" in out
    assert "**WEAKEN**" in out
    assert "| Decile |" in out


def test_render_warns_on_small_n():
    entries = [
        _scored(composite=0.5, verdict="AGREE", r10=0.05),
        _scored(composite=0.5, verdict="AGREE", r10=0.10),
    ]
    out = scoreboard.render_scoreboard(entries, horizon_field="return_10d")
    assert "SMALL N" in out


def test_render_unrecognized_verdict_surfaced():
    entries = [
        _scored(composite=0.5, verdict="MYSTERY_VERDICT", r10=0.05),
    ]
    out = scoreboard.render_scoreboard(entries, horizon_field="return_10d")
    assert "MYSTERY_VERDICT" in out
    assert "unrecognized" in out


# ---------------------------------------------------------------------------
# BarsBackedPriceAdapter — the shim that wraps yfinance's fetch_bars
# ---------------------------------------------------------------------------

class _StubBarsInner:
    """Fake `fetch_bars`-style adapter for testing the price shim.

    `bars_by_symbol` maps a symbol to a list of (date, close) pairs in
    ascending date order. fetch_bars returns a tiny DataFrame-like object
    matching what the shim needs (DatetimeIndex with `.date` + a `close`
    column accessible via `df["close"]`).
    """

    def __init__(self, bars_by_symbol: dict[str, list[tuple[date, float]]]):
        self.bars_by_symbol = bars_by_symbol
        self.fetch_count = 0

    async def fetch_bars(self, symbol: str, interval: str, period: str):
        self.fetch_count += 1
        bars = self.bars_by_symbol.get(symbol)
        if bars is None:
            import pandas as pd
            return pd.DataFrame()
        import pandas as pd
        idx = pd.to_datetime([b[0] for b in bars])
        return pd.DataFrame(
            {"close": [b[1] for b in bars]},
            index=idx,
        )


def test_bars_backed_adapter_returns_close_on_target():
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),
            (date(2026, 5, 2), 101.0),
            (date(2026, 5, 3), 102.0),
        ],
    })
    shim = scoreboard.BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 2)))
    assert got == pytest.approx(101.0)


def test_bars_backed_adapter_falls_back_to_last_bar_before_target():
    """Weekend / holiday case: target has no bar; use most recent prior bar."""
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),  # Friday
            (date(2026, 5, 4), 105.0),  # Monday (skip weekend)
        ],
    })
    shim = scoreboard.BarsBackedPriceAdapter(inner)
    # Sunday 2026-05-03 — no bar exists; expect the Friday close.
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 3)))
    assert got == pytest.approx(100.0)


def test_bars_backed_adapter_returns_none_before_history():
    inner = _StubBarsInner({
        "FOO": [(date(2026, 5, 10), 100.0)],
    })
    shim = scoreboard.BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 1)))
    assert got is None


def test_bars_backed_adapter_caches_per_symbol():
    """Multiple price lookups for the same symbol → one fetch_bars call."""
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),
            (date(2026, 5, 5), 110.0),
            (date(2026, 5, 10), 120.0),
        ],
    })
    shim = scoreboard.BarsBackedPriceAdapter(inner)

    async def _go():
        a = await shim.fetch_price_at("FOO", date(2026, 5, 1))
        b = await shim.fetch_price_at("FOO", date(2026, 5, 5))
        c = await shim.fetch_price_at("FOO", date(2026, 5, 10))
        return a, b, c

    a, b, c = asyncio.run(_go())
    assert (a, b, c) == (pytest.approx(100.0), pytest.approx(110.0), pytest.approx(120.0))
    assert inner.fetch_count == 1


def test_bars_backed_adapter_unknown_symbol_returns_none():
    inner = _StubBarsInner({})
    shim = scoreboard.BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("MISSING", date(2026, 5, 1)))
    assert got is None


def test_bars_backed_adapter_fetch_exception_returns_none():
    class _BadInner:
        async def fetch_bars(self, symbol, interval, period):
            raise RuntimeError("network down")

    shim = scoreboard.BarsBackedPriceAdapter(_BadInner())
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 1)))
    assert got is None
