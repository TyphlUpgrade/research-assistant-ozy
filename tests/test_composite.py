"""
Unit tests for the deterministic Stage-1 composite scoring function (PR 2A.1).

Every scoring component has at least one branch-targeted test. Fixtures are
plain dicts / dataclass instances — no mocks required because
`compute_intrinsic_score` is a pure function.
"""
from __future__ import annotations

from typing import Optional

import pytest

from research_assistant.composite import (
    BASELINE_SCORE,
    INSIDER_BUYING_BONUS,
    INSIDER_SELLING_SCORE_CAP,
    LATE_DISCLOSURE_BONUS,
    PARABOLIC_SCORE_CAP,
    REGIME_MULTIPLIER,
    SCREENER_BONUS_PER_SOURCE,
    SCREENER_MAX_DISTINCT_SOURCES,
    SECTOR_ALIGNED_BONUS,
    SECTOR_MISALIGNED_PENALTY,
    TREND_MODERATE_BONUS,
    TREND_STRONG_BONUS,
    VOLUME_EXPANSION_BONUS,
    _count_independent_sources,
    _sector_bias_for_ticker_sector,
    compute_intrinsic_score,
)
from research_assistant.edgar import InsiderActivitySummary
from research_assistant.screeners import SetupCandidate


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _empty_insider_summary(
    *,
    total_filings: int = 0,
    buys_count: int = 0,
    sales_count: int = 0,
    net_dollars: float = 0.0,
    late_disclosure_count: int = 0,
) -> InsiderActivitySummary:
    """Minimal InsiderActivitySummary for scoring tests."""
    return InsiderActivitySummary(
        window_days=90,
        window_start="2026-02-24",
        window_end="2026-05-25",
        total_filings=total_filings,
        buys_count=buys_count,
        sales_count=sales_count,
        net_dollars=net_dollars,
        code_mix={},
        deriv_code_mix={},
        by_officer=[],
        latest_transaction_date=None,
        disclosed_filings_count=total_filings,
        late_disclosure_count=late_disclosure_count,
        late_disclosure_officers=0,
        latest_disclosure_date=None,
    )


def _setup_alert(screener: str, ticker: str = "NVDA") -> SetupCandidate:
    """Minimal SetupCandidate fixture for screener-count tests."""
    return SetupCandidate(
        ticker=ticker,
        screener=screener,
        asof="2026-05-25",
        entry_price=100.0,
        evidence={},
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_baseline_score() -> None:
    """Empty inputs produce the baseline (after the default regime
    multiplier of 1.00 for an unrecognised/missing regime)."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={},
        screener_alerts=[],
    )
    assert score == pytest.approx(BASELINE_SCORE)
    assert breakdown["baseline"] == BASELINE_SCORE
    assert breakdown["regime_multiplier"] == 1.00


# ---------------------------------------------------------------------------
# Regime multiplier
# ---------------------------------------------------------------------------

def test_regime_multiplier_bull_trending() -> None:
    """Bull-trending regime applies the 1.10x multiplier."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={"regime": "bull-trending"},
        screener_alerts=[],
    )
    assert breakdown["regime_multiplier"] == REGIME_MULTIPLIER["bull-trending"]
    assert score == pytest.approx(BASELINE_SCORE * 1.10)


def test_regime_multiplier_panic_caps_score() -> None:
    """Panic regime applies the 0.70x multiplier, dragging baseline down."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={"regime": "panic"},
        screener_alerts=[],
    )
    assert breakdown["regime_multiplier"] == REGIME_MULTIPLIER["panic"]
    assert score == pytest.approx(BASELINE_SCORE * 0.70)


def test_regime_multiplier_unknown_regime_defaults_to_one() -> None:
    """An unrecognised regime label gets the default 1.00 multiplier (no
    silent crater on schema drift)."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={"regime": "spaghetti"},
        screener_alerts=[],
    )
    assert breakdown["regime_multiplier"] == 1.00
    assert score == pytest.approx(BASELINE_SCORE)


# ---------------------------------------------------------------------------
# Trend strength
# ---------------------------------------------------------------------------

def test_trend_strong_adds_bonus() -> None:
    """return_30d > 20% adds the strong-trend bonus."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"return_30d": 0.25},
        insider_summary=None,
        world_state={},
        screener_alerts=[],
    )
    assert breakdown.get("trend_strong") == TREND_STRONG_BONUS
    assert "trend_moderate" not in breakdown
    assert score == pytest.approx(BASELINE_SCORE + TREND_STRONG_BONUS)


def test_trend_moderate_adds_bonus() -> None:
    """return_30d in (10%, 20%] adds the moderate-trend bonus."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"return_30d": 0.15},
        insider_summary=None,
        world_state={},
        screener_alerts=[],
    )
    assert breakdown.get("trend_moderate") == TREND_MODERATE_BONUS
    assert "trend_strong" not in breakdown
    assert score == pytest.approx(BASELINE_SCORE + TREND_MODERATE_BONUS)


# ---------------------------------------------------------------------------
# Anti-parabolic cap
# ---------------------------------------------------------------------------

def test_anti_parabolic_cap() -> None:
    """RSI > 70 AND 5d return > 20% caps score at 0.40 even after bonuses."""
    score, breakdown = compute_intrinsic_score(
        # Add bonuses that would otherwise push score above 0.40
        ticker_data={
            "return_30d": 0.30,
            "recent_return_5d": 0.25,
            "weekly_rsi_14": 75.0,
            "volume_ratio": 2.0,
        },
        insider_summary=None,
        # Bull regime multiplier on top — should still cap.
        world_state={"regime": "bull-trending"},
        screener_alerts=[],
    )
    assert breakdown.get("parabolic_cap") is True
    assert score <= PARABOLIC_SCORE_CAP


def test_parabolic_cap_does_not_fire_without_both_conditions() -> None:
    """RSI alone or 5d return alone does NOT trigger the cap."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={
            "recent_return_5d": 0.25,
            "weekly_rsi_14": 65.0,  # below 70 threshold
        },
        insider_summary=None,
        world_state={},
        screener_alerts=[],
    )
    assert "parabolic_cap" not in breakdown


# ---------------------------------------------------------------------------
# Volume confirmation
# ---------------------------------------------------------------------------

def test_volume_expansion_bonus() -> None:
    """volume_ratio > 1.5 adds the volume bonus."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"volume_ratio": 2.0},
        insider_summary=None,
        world_state={},
        screener_alerts=[],
    )
    assert breakdown.get("volume_expansion") == VOLUME_EXPANSION_BONUS
    assert score == pytest.approx(BASELINE_SCORE + VOLUME_EXPANSION_BONUS)


# ---------------------------------------------------------------------------
# Insider activity
# ---------------------------------------------------------------------------

def test_insider_buying_bonus() -> None:
    """net_dollars > $100K (with at least one filing) adds the insider-buying
    bonus."""
    summary = _empty_insider_summary(
        total_filings=2, buys_count=2, net_dollars=500_000.0,
    )
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=summary,
        world_state={},
        screener_alerts=[],
    )
    assert breakdown.get("insider_buying") == INSIDER_BUYING_BONUS
    assert score == pytest.approx(BASELINE_SCORE + INSIDER_BUYING_BONUS)


def test_severe_insider_selling_cap() -> None:
    """net_dollars < -$10M AND sales >= 3 AND buys = 0 caps score at 0.40."""
    summary = _empty_insider_summary(
        total_filings=4,
        buys_count=0,
        sales_count=4,
        net_dollars=-15_000_000.0,
    )
    score, breakdown = compute_intrinsic_score(
        # Inputs that would otherwise push score well above 0.40
        ticker_data={"return_30d": 0.30, "volume_ratio": 2.0},
        insider_summary=summary,
        world_state={"regime": "bull-trending"},
        screener_alerts=[
            _setup_alert("sector_rotation"),
            _setup_alert("pead"),
            _setup_alert("pre_catalyst"),
        ],
    )
    assert breakdown.get("insider_selling_cap") is True
    assert score <= INSIDER_SELLING_SCORE_CAP


def test_insider_selling_cap_requires_zero_buys() -> None:
    """Even with severe selling, presence of ANY buy means no cap (mixed
    signal — let other signals weigh in)."""
    summary = _empty_insider_summary(
        total_filings=5,
        buys_count=1,  # one buy → no cap
        sales_count=4,
        net_dollars=-15_000_000.0,
    )
    score, breakdown = compute_intrinsic_score(
        ticker_data={"return_30d": 0.30},
        insider_summary=summary,
        world_state={},
        screener_alerts=[],
    )
    assert "insider_selling_cap" not in breakdown


def test_late_disclosure_cluster_bonus() -> None:
    """late_disclosure_count >= 3 (with at least one filing in window) adds
    the cluster bonus."""
    summary = _empty_insider_summary(
        total_filings=3,
        late_disclosure_count=3,
    )
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=summary,
        world_state={},
        screener_alerts=[],
    )
    assert breakdown.get("late_disclosure_cluster") == LATE_DISCLOSURE_BONUS


def test_insider_zero_filings_no_effect() -> None:
    """An empty insider summary (no filings) leaves the score unchanged."""
    summary = _empty_insider_summary(total_filings=0)
    score_with, _ = compute_intrinsic_score(
        ticker_data={}, insider_summary=summary, world_state={},
        screener_alerts=[],
    )
    score_without, _ = compute_intrinsic_score(
        ticker_data={}, insider_summary=None, world_state={},
        screener_alerts=[],
    )
    assert score_with == pytest.approx(score_without)


# ---------------------------------------------------------------------------
# Sector alignment
# ---------------------------------------------------------------------------

def test_sector_alignment_bullish() -> None:
    """A ticker in a bullish-bias sector gets the alignment bonus."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"sector": "Technology"},
        insider_summary=None,
        world_state={
            "sector_rotation": {
                "XLK": {"bias": "bullish", "strength": 0.8},
            },
        },
        screener_alerts=[],
    )
    assert breakdown.get("sector_aligned") == SECTOR_ALIGNED_BONUS
    assert score == pytest.approx(BASELINE_SCORE + SECTOR_ALIGNED_BONUS)


def test_sector_misalignment_bearish() -> None:
    """A ticker in a bearish-bias sector takes the misalignment penalty."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"sector": "Energy"},
        insider_summary=None,
        world_state={
            "sector_rotation": {
                "XLE": {"bias": "bearish", "strength": 0.6},
            },
        },
        screener_alerts=[],
    )
    assert breakdown.get("sector_misaligned") == SECTOR_MISALIGNED_PENALTY
    assert score == pytest.approx(BASELINE_SCORE + SECTOR_MISALIGNED_PENALTY)


def test_sector_neutral_no_effect() -> None:
    """A neutral-bias sector does not affect the score either way."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={"sector": "Utilities"},
        insider_summary=None,
        world_state={
            "sector_rotation": {
                "XLU": {"bias": "neutral", "strength": 0.5},
            },
        },
        screener_alerts=[],
    )
    assert "sector_aligned" not in breakdown
    assert "sector_misaligned" not in breakdown


def test_sector_bias_helper_handles_missing_data() -> None:
    """The mapping helper returns None when world_state lacks rotation or
    when the sector string isn't recognised."""
    assert _sector_bias_for_ticker_sector(None, {}) is None
    assert _sector_bias_for_ticker_sector("Technology", {}) is None
    assert _sector_bias_for_ticker_sector("UnknownSector", {
        "sector_rotation": {"XLK": {"bias": "bullish"}}
    }) is None


# ---------------------------------------------------------------------------
# Screener confirmation
# ---------------------------------------------------------------------------

def test_screener_confirmation_single_source() -> None:
    """One distinct screener source adds 0.08."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={},
        screener_alerts=[_setup_alert("sector_rotation")],
    )
    assert breakdown["distinct_screener_sources"] == 1
    assert breakdown["screener_confirmations"] == pytest.approx(
        SCREENER_BONUS_PER_SOURCE
    )


def test_screener_confirmation_caps_at_3() -> None:
    """5 distinct screener sources caps the bonus at 3 * 0.08 = 0.24."""
    score, breakdown = compute_intrinsic_score(
        ticker_data={},
        insider_summary=None,
        world_state={},
        screener_alerts=[
            _setup_alert("sector_rotation"),
            _setup_alert("pead"),
            _setup_alert("pre_catalyst"),
            _setup_alert("rs_breakout"),
            _setup_alert("vol_compression"),
        ],
    )
    expected_cap = SCREENER_BONUS_PER_SOURCE * SCREENER_MAX_DISTINCT_SOURCES
    assert breakdown["distinct_screener_sources"] == 5
    assert breakdown["screener_confirmations"] == pytest.approx(expected_cap)
    assert breakdown["screener_confirmations"] == pytest.approx(0.24)


def test_screener_confirmation_dedupes_same_source() -> None:
    """Two alerts from the SAME screener count as one distinct source."""
    assert _count_independent_sources([
        _setup_alert("sector_rotation", ticker="XLK"),
        _setup_alert("sector_rotation", ticker="XLF"),
    ]) == 1


# ---------------------------------------------------------------------------
# Output invariants
# ---------------------------------------------------------------------------

def test_score_clipped_to_unit_interval_upper() -> None:
    """Even with every positive signal firing, score stays <= 1.0."""
    score, _ = compute_intrinsic_score(
        ticker_data={"return_30d": 0.50, "volume_ratio": 5.0,
                     "sector": "Technology"},
        insider_summary=_empty_insider_summary(
            total_filings=5, buys_count=5, net_dollars=2_000_000.0,
            late_disclosure_count=4,
        ),
        world_state={
            "regime": "bull-trending",
            "sector_rotation": {"XLK": {"bias": "bullish"}},
        },
        screener_alerts=[
            _setup_alert("sector_rotation"),
            _setup_alert("pead"),
            _setup_alert("pre_catalyst"),
            _setup_alert("rs_breakout"),
        ],
    )
    assert 0.0 <= score <= 1.0


def test_score_clipped_to_unit_interval_lower() -> None:
    """Even with every negative signal firing, score stays >= 0.0."""
    score, _ = compute_intrinsic_score(
        ticker_data={"sector": "Energy"},
        insider_summary=None,
        world_state={
            "regime": "panic",
            "sector_rotation": {"XLE": {"bias": "bearish"}},
        },
        screener_alerts=[],
    )
    assert 0.0 <= score <= 1.0


def test_breakdown_explains_score() -> None:
    """Every score component that fires appears in breakdown as an explicit
    key, so trace logs and the operator can debug WHY a ticker scored high."""
    summary = _empty_insider_summary(
        total_filings=3, buys_count=3, net_dollars=500_000.0,
        late_disclosure_count=3,
    )
    score, breakdown = compute_intrinsic_score(
        ticker_data={
            "return_30d": 0.25,
            "volume_ratio": 2.0,
            "sector": "Technology",
        },
        insider_summary=summary,
        world_state={
            "regime": "bull-trending",
            "sector_rotation": {"XLK": {"bias": "bullish"}},
        },
        screener_alerts=[_setup_alert("sector_rotation")],
    )
    # Required keys: every signal that fired must be explained.
    required = {
        "baseline",
        "regime_multiplier",
        "trend_strong",
        "volume_expansion",
        "insider_buying",
        "late_disclosure_cluster",
        "sector_aligned",
        "screener_confirmations",
        "distinct_screener_sources",
    }
    assert required.issubset(breakdown.keys())
