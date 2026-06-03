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


def test_render_unrecognized_verdict_with_no_enriched_returns_renders_na():
    """The unrecognized-verdict branch has a `'n/a'` fallback for
    `median=None`; covers the case where a non-default-vocabulary verdict
    appears with zero enriched returns."""
    entries = [
        _scored(composite=0.5, verdict="MYSTERY_VERDICT", r10=None),
    ]
    out = scoreboard.render_scoreboard(entries, horizon_field="return_10d")
    assert "MYSTERY_VERDICT" in out
    assert "unrecognized" in out
    assert "n/a" in out


def test_render_with_custom_verdict_order_uses_stage3_vocabulary():
    """Phase 3 will pass Stage 3 Skeptic verdicts here. Verify the
    renderer accepts an arbitrary verdict_order tuple and surfaces
    known buckets in that order, leaving the rest in `(unrecognized)`."""
    entries = [
        _scored(composite=0.6, verdict="CONFIRM", r10=0.10),
        _scored(composite=0.6, verdict="CONFIRM", r10=0.15),
        _scored(composite=0.4, verdict="TEMPER", r10=-0.02),
        _scored(composite=0.2, verdict="CHALLENGE", r10=-0.10),
        _scored(composite=0.5, verdict="LEFTOVER_BRIEF_VERDICT", r10=0.01),
    ]
    out = scoreboard.render_scoreboard(
        entries,
        horizon_field="return_10d",
        verdict_order=("CONFIRM", "TEMPER", "CHALLENGE", "INVALIDATE"),
    )
    assert "**CONFIRM**" in out
    assert "**TEMPER**" in out
    assert "**CHALLENGE**" in out
    # LEFTOVER_BRIEF_VERDICT is not in the passed vocabulary → renders
    # under the "(unrecognized)" tail.
    assert "LEFTOVER_BRIEF_VERDICT" in out
    assert "unrecognized" in out


def test_render_includes_under_firing_caveat():
    """Operators reading the scoreboard without the hit-rate surface
    could falsely conclude the cascade is calibrated. The renderer
    forward-references the Hit-rate section in the caveat."""
    entries = [_scored(composite=0.5, verdict="AGREE", r10=0.05)]
    out = scoreboard.render_scoreboard(entries, horizon_field="return_10d")
    assert "Type II error" in out or "tickers we missed" in out
    assert "Hit-rate" in out


# ---------------------------------------------------------------------------
# Ticker validation — defense in depth at the reader
# ---------------------------------------------------------------------------

def test_enrich_drops_row_with_invalid_ticker(tmp_path: Path, caplog):
    """Rows with malformed tickers (path-traversal attempts, lookalikes,
    or just journal corruption) are dropped from enrichment results with
    a warning, never threaded into a filesystem path."""
    today = date.today()
    asof = today - timedelta(days=60)
    rows = [
        _stage2_row(ticker="VALID", asof=asof.isoformat()),
        _stage2_row(ticker="../etc/passwd", asof=asof.isoformat()),
        _stage2_row(ticker="not_a_ticker", asof=asof.isoformat()),
        _stage2_row(ticker="", asof=asof.isoformat()),
    ]
    adapter = _CannedAdapter({
        ("VALID", asof): 100.0,
        ("VALID", asof + timedelta(days=5)): 105.0,
        ("VALID", asof + timedelta(days=10)): 110.0,
        ("VALID", asof + timedelta(days=30)): 120.0,
    })
    with caplog.at_level("WARNING"):
        entries = asyncio.run(
            scoreboard.enrich_stage2_rows(rows, adapter, tmp_path)
        )
    # Only the VALID row survives.
    assert len(entries) == 1
    assert entries[0].ticker == "VALID"
    # No file was created outside the stage2_returns directory for the
    # malformed tickers — confirm by listing what's actually there.
    returns_dir = tmp_path / "stage2_returns"
    written = sorted(p.name for p in returns_dir.iterdir() if p.is_file())
    assert written == ["VALID.jsonl"]
    # No `etc/` subdir was created from `../etc/passwd`.
    assert not (tmp_path / "etc").exists()
    # At least one drop was logged.
    assert any("invalid ticker" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Horizon-fetch failure when entry-price succeeds
# ---------------------------------------------------------------------------

class _PartialFailureAdapter:
    """Returns entry_price OK, but raises on subsequent horizon fetches.

    Tests the path where the adapter succeeds on the first call (asof
    price) and then transiently fails on the next (5d / 10d / 30d). The
    row should still be persisted (entry_price was real) and horizons
    should land as null so the next run can try to fill them.
    """

    def __init__(self, entry_price: float):
        self.entry_price = entry_price
        self.calls: list[date] = []

    async def fetch_price_at(self, ticker: str, target):  # noqa: D401
        self.calls.append(target)
        if len(self.calls) == 1:
            return self.entry_price
        raise RuntimeError("transient horizon failure")


def test_enrich_persists_row_when_horizon_fetch_fails(tmp_path: Path):
    today = date.today()
    asof = today - timedelta(days=60)
    row = _stage2_row(ticker="PART", asof=asof.isoformat())
    adapter = _PartialFailureAdapter(entry_price=100.0)
    entries = asyncio.run(
        scoreboard.enrich_stage2_rows([row], adapter, tmp_path)
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_price == pytest.approx(100.0)
    assert e.return_5d is None
    assert e.return_10d is None
    assert e.return_30d is None
    # Row IS cached because we have a real entry price — the horizons
    # being None is expected (refresh-on-mature handles future retries).
    cache_path = tmp_path / "stage2_returns" / "PART.jsonl"
    assert cache_path.exists()


# ---------------------------------------------------------------------------
# Oversize-line guard — defense in depth at the reader
# ---------------------------------------------------------------------------

def test_read_all_stage2_skips_oversize_lines(tmp_path: Path, caplog):
    path = tmp_path / "stage2" / "BIG.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_stage2_row(ticker="BIG", asof="2026-05-01")) + "\n")
        # 32KB oversize row — over the 16KB guard.
        f.write(json.dumps({"x": "a" * 32_000}) + "\n")
        f.write(json.dumps(_stage2_row(ticker="BIG", asof="2026-05-02")) + "\n")
    with caplog.at_level("WARNING"):
        rows = scoreboard.read_all_stage2(tmp_path)
    assert len(rows) == 2  # oversize row skipped, two valid rows survive
    assert any("line >" in r.message for r in caplog.records)


# Shim tests live in tests/test_price.py — the `BarsBackedPriceAdapter`
# moved to `research_assistant/price.py` after the 2026-06-03 review.


# ---------------------------------------------------------------------------
# Phase 1.5 — hit-rate / candidate-coverage
# ---------------------------------------------------------------------------

def _alert(
    *,
    ticker: str,
    asof: str,
    return_7d: float | None = None,
    return_30d: float | None = None,
    return_90d: float | None = None,
    screener: str = "sector_rotation",
    entry_price: float = 100.0,
) -> dict:
    return {
        "schema_version": 1,
        "ticker": ticker,
        "screener": screener,
        "asof": asof,
        "entry_price": entry_price,
        "evidence": {},
        "created_at": f"{asof}T00:00:00+00:00",
        "return_7d": return_7d,
        "return_30d": return_30d,
        "return_90d": return_90d,
        "enriched_at": f"{asof}T12:00:00+00:00" if return_30d is not None else None,
    }


def test_compute_hit_rate_empty_alerts():
    result = scoreboard.compute_hit_rate(
        [], [], horizon_field="return_30d",
    )
    assert result["total_alerts"] == 0
    assert result["enriched_alerts"] == 0
    assert result["movers"] == 0
    assert result["cutoff_return"] is None
    assert result["by_threshold"] == []


def test_compute_hit_rate_no_enriched_alerts():
    alerts = [_alert(ticker="AAA", asof="2026-05-01", return_30d=None)]
    result = scoreboard.compute_hit_rate(
        alerts, [], horizon_field="return_30d",
    )
    assert result["total_alerts"] == 1
    assert result["enriched_alerts"] == 0
    assert result["movers"] == 0


def test_compute_hit_rate_invalid_horizon_field_raises():
    with pytest.raises(ValueError, match="horizon_field"):
        scoreboard.compute_hit_rate([], [], horizon_field="return_99d")


def test_compute_hit_rate_movers_identified_above_quartile_cutoff():
    """8 alerts with returns 0.01 to 0.08; top quartile cutoff at ~0.0625;
    top quartile should be the highest 2-3 alerts."""
    alerts = [
        _alert(ticker=f"T{i}", asof="2026-04-01", return_30d=0.01 * (i + 1))
        for i in range(8)
    ]
    result = scoreboard.compute_hit_rate(
        alerts, [], horizon_field="return_30d", top_quantile=0.75,
    )
    assert result["enriched_alerts"] == 8
    # top quartile of [0.01..0.08] → cutoff near 0.0625
    assert result["movers"] >= 2
    assert result["movers"] <= 3
    assert result["cutoff_return"] is not None
    assert result["cutoff_return"] > 0.05


def test_compute_hit_rate_surfaces_match_via_lookback_window():
    """Mover alert on day D; Stage 2 entry for the same ticker on D+2 with
    conviction 0.55. With lookback_days=3, the entry counts as surfaced
    at thresholds ≤0.55 and not at >0.55."""
    alerts = [
        _alert(ticker="ABC", asof="2026-04-01", return_30d=0.20),
    ]
    entries = [
        _scored(ticker="ABC", composite=0.55, verdict="AGREE", r10=None),
    ]
    # Patch the entry's asof to be 2 days after the alert.
    entries[0] = scoreboard.ScoredEntry(
        ticker=entries[0].ticker,
        asof="2026-04-03",
        recorded_at=entries[0].recorded_at,
        composite_conviction=entries[0].composite_conviction,
        skeptic_verdict=entries[0].skeptic_verdict,
        decision_tag=entries[0].decision_tag,
        entry_price=entries[0].entry_price,
        return_5d=entries[0].return_5d,
        return_10d=entries[0].return_10d,
        return_30d=entries[0].return_30d,
    )
    result = scoreboard.compute_hit_rate(
        alerts, entries,
        horizon_field="return_30d",
        lookback_days=3,
        conviction_thresholds=(0.40, 0.50, 0.60),
    )
    assert result["movers"] == 1
    by_t = {b["threshold"]: b for b in result["by_threshold"]}
    assert by_t[0.40]["surfaced"] == 1  # 0.55 >= 0.40
    assert by_t[0.40]["hit_rate"] == pytest.approx(1.0)
    assert by_t[0.50]["surfaced"] == 1  # 0.55 >= 0.50
    assert by_t[0.60]["surfaced"] == 0  # 0.55 < 0.60
    assert by_t[0.60]["hit_rate"] == pytest.approx(0.0)


def test_compute_hit_rate_excludes_entries_outside_lookback_window():
    """Stage 2 entry on D+10 is outside the default 3d lookback — must
    NOT count as surfaced even though the ticker matches."""
    alerts = [_alert(ticker="ABC", asof="2026-04-01", return_30d=0.20)]
    entries = [
        scoreboard.ScoredEntry(
            ticker="ABC",
            asof="2026-04-11",  # 10 days after alert
            recorded_at="2026-04-11T12:00:00+00:00",
            composite_conviction=0.80,
            skeptic_verdict="AGREE",
            decision_tag="WATCH",
            entry_price=100.0,
            return_5d=None,
            return_10d=None,
            return_30d=None,
        ),
    ]
    result = scoreboard.compute_hit_rate(
        alerts, entries, horizon_field="return_30d", lookback_days=3,
    )
    assert result["movers"] == 1
    # No threshold should show this entry as surfaced.
    for b in result["by_threshold"]:
        assert b["surfaced"] == 0
        assert b["hit_rate"] == pytest.approx(0.0)


def test_compute_hit_rate_with_zero_movers_returns_zero_hit_rate():
    """If all alert returns are negative and top_quantile=0.75, the
    'movers' set is still the top 25% by return but those returns are
    negative — define what hit_rate even means here."""
    alerts = [
        _alert(ticker=f"T{i}", asof="2026-04-01", return_30d=-0.05)
        for i in range(4)
    ]
    result = scoreboard.compute_hit_rate(
        alerts, [], horizon_field="return_30d", top_quantile=0.75,
    )
    # All four alerts are tied at -0.05; the cutoff equals -0.05 and all
    # four count as movers (because >= cutoff). hit_rate at all thresholds
    # is 0.0 because no entries.
    assert result["movers"] >= 1
    for b in result["by_threshold"]:
        assert b["hit_rate"] == pytest.approx(0.0)


def test_compute_hit_rate_only_first_qualifying_entry_per_alert_counted():
    """An alert with two qualifying Stage 2 entries should still count
    once — the metric is 'did the cascade surface this ticker?', not
    'how many times did the cascade surface this ticker?'."""
    alerts = [_alert(ticker="ABC", asof="2026-04-01", return_30d=0.20)]
    entries = [
        scoreboard.ScoredEntry(
            ticker="ABC", asof="2026-04-01",
            recorded_at="2026-04-01T08:00:00+00:00",
            composite_conviction=0.55, skeptic_verdict="AGREE",
            decision_tag="WATCH", entry_price=100.0,
            return_5d=None, return_10d=None, return_30d=None,
        ),
        scoreboard.ScoredEntry(
            ticker="ABC", asof="2026-04-02",
            recorded_at="2026-04-02T08:00:00+00:00",
            composite_conviction=0.65, skeptic_verdict="AGREE",
            decision_tag="WATCH", entry_price=100.0,
            return_5d=None, return_10d=None, return_30d=None,
        ),
    ]
    result = scoreboard.compute_hit_rate(
        alerts, entries,
        horizon_field="return_30d",
        conviction_thresholds=(0.40,),
    )
    assert result["by_threshold"][0]["surfaced"] == 1  # not 2


def test_render_hit_rate_empty_window():
    out = scoreboard.render_hit_rate({
        "total_alerts": 0,
        "enriched_alerts": 0,
        "movers": 0,
        "cutoff_return": None,
        "by_threshold": [],
        "horizon_field": "return_30d",
        "lookback_days": 3,
        "top_quantile": 0.75,
    })
    assert "Hit rate" in out
    assert "no alerts" in out.lower() or "Alerts in window: 0" in out


def test_render_hit_rate_with_movers_renders_table():
    result = {
        "total_alerts": 10,
        "enriched_alerts": 8,
        "movers": 2,
        "cutoff_return": 0.0625,
        "by_threshold": [
            {"threshold": 0.40, "surfaced": 1, "hit_rate": 0.5, "small_n": True},
            {"threshold": 0.50, "surfaced": 0, "hit_rate": 0.0, "small_n": True},
        ],
        "horizon_field": "return_30d",
        "lookback_days": 3,
        "top_quantile": 0.75,
    }
    out = scoreboard.render_hit_rate(result)
    assert "Movers" in out
    assert "+6.25%" in out  # cutoff_return formatted
    assert "SMALL N" in out  # 2 < 5
    assert "≥0.40" in out
    assert "1 / 2" in out
    assert "50%" in out
    assert "Interpretation" in out


def test_render_hit_rate_top_half_label():
    """top_quantile=0.5 should render as 'top-half', not 'top-quartile'."""
    result = {
        "total_alerts": 4,
        "enriched_alerts": 4,
        "movers": 2,
        "cutoff_return": 0.05,
        "by_threshold": [
            {"threshold": 0.40, "surfaced": 1, "hit_rate": 0.5, "small_n": True},
        ],
        "horizon_field": "return_30d",
        "lookback_days": 3,
        "top_quantile": 0.5,
    }
    out = scoreboard.render_hit_rate(result)
    assert "top-half" in out
