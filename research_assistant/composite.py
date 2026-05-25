"""
Deterministic Stage 1 composite scoring (PR 2A.1).

Replaces the LLM-driven Stage 1 batched filter with a pure-function scoring
pass over the same inputs. Stage 2 receives the ranked survivors but NOT
the breakdown — preserves the "Stage 2 does original analysis, not
rationalization" principle even before PR 2A.2 ships the input isolation.

Public surface:
  - compute_intrinsic_score(ticker_data, insider_summary, world_state,
                            screener_alerts) -> (score, breakdown)

Weights are exposed as module-level constants so they're tunable in one
place (the plan calls out educated-guess weights to be refined after
4 weeks of journal data).
"""
from __future__ import annotations

from typing import Optional

from research_assistant.edgar import InsiderActivitySummary
from research_assistant.screeners import SetupCandidate


# ---------------------------------------------------------------------------
# Scoring weights (single source of truth — tune here, not at call sites)
# ---------------------------------------------------------------------------

BASELINE_SCORE = 0.30

# Regime multiplier: categorical → numerical map. Unknown regimes default to
# 1.00 (no boost, no penalty) so an unrecognised regime label can't silently
# crater scores.
REGIME_MULTIPLIER = {
    "bull-trending": 1.10,
    "choppy": 1.00,
    "bear-trending": 0.85,
    "panic": 0.70,
    "euphoria": 0.90,  # euphoria is regime-risk, mild discount
}
REGIME_MULTIPLIER_DEFAULT = 1.00

# Trend strength gates (uses ticker_data.return_30d).
TREND_STRONG_THRESHOLD = 0.20
TREND_STRONG_BONUS = 0.10
TREND_MODERATE_THRESHOLD = 0.10
TREND_MODERATE_BONUS = 0.05

# Anti-parabolic cap: extension flag based on (weekly RSI, 5-day return).
PARABOLIC_RSI_THRESHOLD = 70.0
PARABOLIC_RETURN_5D_THRESHOLD = 0.20
PARABOLIC_SCORE_CAP = 0.40

# Volume confirmation.
VOLUME_RATIO_THRESHOLD = 1.5
VOLUME_EXPANSION_BONUS = 0.05

# Insider activity weights.
INSIDER_BUYING_DOLLAR_THRESHOLD = 100_000.0
INSIDER_BUYING_BONUS = 0.08
INSIDER_SELLING_DOLLAR_THRESHOLD = -10_000_000.0
INSIDER_SELLING_MIN_SALES = 3
INSIDER_SELLING_SCORE_CAP = 0.40
LATE_DISCLOSURE_THRESHOLD = 3
LATE_DISCLOSURE_BONUS = 0.05

# Sector alignment (with world_state["sector_rotation"][etf]["bias"]).
SECTOR_ALIGNED_BONUS = 0.05
SECTOR_MISALIGNED_PENALTY = -0.05

# Screener confirmation: per-distinct-source bonus, capped at 3 sources.
SCREENER_BONUS_PER_SOURCE = 0.08
SCREENER_MAX_DISTINCT_SOURCES = 3  # bonus caps at 0.08 * 3 = 0.24


# ---------------------------------------------------------------------------
# Sector → ETF mapping (hand-coded GICS-style sector strings → XL-series ETFs)
# ---------------------------------------------------------------------------

# Maps the `sector` field that yfinance / data_loader writes on ticker_data
# to the XL-series sector ETF whose bias serves as the regime-relative
# alignment signal. Keep this table close to the weights so tuning lives in
# one file.
_SECTOR_TO_ETF: dict[str, str] = {
    # GICS-style strings (the format yfinance returns)
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Healthcare": "XLV",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
}


def _sector_bias_for_ticker_sector(
    sector: Optional[str], world_state: dict
) -> Optional[str]:
    """Resolve a ticker's GICS-style sector string to its sector-ETF bias.

    Returns the bias string ("bullish" / "bearish" / "neutral") from
    `world_state["sector_rotation"][etf]["bias"]`, or None when the sector
    can't be mapped or the world_state lacks rotation info.
    """
    if not sector:
        return None
    etf = _SECTOR_TO_ETF.get(sector)
    if etf is None:
        return None
    rotation = world_state.get("sector_rotation") if isinstance(world_state, dict) else None
    if not isinstance(rotation, dict):
        return None
    entry = rotation.get(etf)
    if not isinstance(entry, dict):
        return None
    bias = entry.get("bias")
    if isinstance(bias, str):
        return bias
    return None


def _count_independent_sources(alerts: list[SetupCandidate]) -> int:
    """Count distinct screener sources across `alerts`.

    Today: distinct `screener` field count. v1.5 will be smarter about
    correlated sources (e.g. dedupe sector_rotation + relative_strength
    when both fire on the same regime-shift signal).
    """
    return len({a.screener for a in alerts})


def _clip(value: float, low: float, high: float) -> float:
    """Clip `value` to [low, high]."""
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Public scoring function
# ---------------------------------------------------------------------------

def compute_intrinsic_score(
    ticker_data: dict,
    insider_summary: Optional[InsiderActivitySummary],
    world_state: dict,
    screener_alerts: list[SetupCandidate],
) -> tuple[float, dict]:
    """Compute the deterministic Stage-1 intrinsic score for one ticker.

    Returns (intrinsic_score in [0,1], breakdown_dict). The breakdown
    explains which signals contributed how much — for trace logging and
    operator debugging. Breakdown is NOT passed to Stage 2 (Stage 2 must do
    original analysis from raw data, not rationalize an upstream score).
    """
    ticker_data = ticker_data or {}
    world_state = world_state or {}
    screener_alerts = screener_alerts or []

    score = BASELINE_SCORE
    breakdown: dict = {"baseline": BASELINE_SCORE}

    # Regime alignment (categorical → numerical multiplier)
    regime = world_state.get("regime") if isinstance(world_state, dict) else None
    mult = REGIME_MULTIPLIER.get(regime, REGIME_MULTIPLIER_DEFAULT)
    score *= mult
    breakdown["regime_multiplier"] = mult

    # Trend strength (return_30d gated)
    r30 = ticker_data.get("return_30d") or 0.0
    if r30 > TREND_STRONG_THRESHOLD:
        score += TREND_STRONG_BONUS
        breakdown["trend_strong"] = TREND_STRONG_BONUS
    elif r30 > TREND_MODERATE_THRESHOLD:
        score += TREND_MODERATE_BONUS
        breakdown["trend_moderate"] = TREND_MODERATE_BONUS

    # Anti-parabolic penalty (hard cap)
    r5 = ticker_data.get("recent_return_5d") or 0.0
    rsi = ticker_data.get("weekly_rsi_14") or 50.0
    if rsi > PARABOLIC_RSI_THRESHOLD and r5 > PARABOLIC_RETURN_5D_THRESHOLD:
        score = min(score, PARABOLIC_SCORE_CAP)
        breakdown["parabolic_cap"] = True

    # Volume confirmation
    vr = ticker_data.get("volume_ratio") or 1.0
    if vr > VOLUME_RATIO_THRESHOLD:
        score += VOLUME_EXPANSION_BONUS
        breakdown["volume_expansion"] = VOLUME_EXPANSION_BONUS

    # Insider activity (uses structured dataclass directly)
    if insider_summary is not None and insider_summary.total_filings > 0:
        if insider_summary.net_dollars > INSIDER_BUYING_DOLLAR_THRESHOLD:
            score += INSIDER_BUYING_BONUS
            breakdown["insider_buying"] = INSIDER_BUYING_BONUS
        elif (
            insider_summary.net_dollars < INSIDER_SELLING_DOLLAR_THRESHOLD
            and insider_summary.sales_count >= INSIDER_SELLING_MIN_SALES
            and insider_summary.buys_count == 0
        ):
            score = min(score, INSIDER_SELLING_SCORE_CAP)
            breakdown["insider_selling_cap"] = True
        if insider_summary.late_disclosure_count >= LATE_DISCLOSURE_THRESHOLD:
            score += LATE_DISCLOSURE_BONUS
            breakdown["late_disclosure_cluster"] = LATE_DISCLOSURE_BONUS

    # Sector alignment with regime (via sector-ETF bias)
    sector = ticker_data.get("sector")
    sector_bias = _sector_bias_for_ticker_sector(sector, world_state)
    if sector_bias == "bullish":
        score += SECTOR_ALIGNED_BONUS
        breakdown["sector_aligned"] = SECTOR_ALIGNED_BONUS
    elif sector_bias == "bearish":
        score += SECTOR_MISALIGNED_PENALTY
        breakdown["sector_misaligned"] = SECTOR_MISALIGNED_PENALTY

    # Screener confirmation — multi-signal weighting (cap at +0.24 for 3+)
    distinct_sources = _count_independent_sources(screener_alerts)
    if distinct_sources > 0:
        bonus = SCREENER_BONUS_PER_SOURCE * min(
            distinct_sources, SCREENER_MAX_DISTINCT_SOURCES
        )
        score += bonus
        breakdown["screener_confirmations"] = bonus
        breakdown["distinct_screener_sources"] = distinct_sources

    # Re-apply hard caps AFTER all bonuses — "hard cap" semantics demand
    # that downstream confirmations can't lift a ticker out of the cap
    # (otherwise volume/screener bonuses could rescue a parabolic chart or
    # a coordinated insider sell-off).
    if breakdown.get("parabolic_cap"):
        score = min(score, PARABOLIC_SCORE_CAP)
    if breakdown.get("insider_selling_cap"):
        score = min(score, INSIDER_SELLING_SCORE_CAP)

    return _clip(score, 0.0, 1.0), breakdown
