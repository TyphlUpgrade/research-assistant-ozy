"""
Phase A — Agent C (deterministic analyst revisions).

Reads yfinance's `.recommendations` (consensus rating history) and
`.analyst_price_targets` (target distribution). Emits a compressed view:
current consensus rating mix, upward/downward revision counts last 30d,
median price target + spread, and a directional "revision velocity" tag.

Three-state semantics mirror KPISummary:
  - None        → fetch failed
  - empty       → fetch succeeded but no analyst coverage
  - populated   → render_for_prompt emits the compressed block
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class AnalystRevisions:
    """Compressed analyst-revision surface."""
    symbol: str
    asof: str

    # Current rating mix (snapshot from the most-recent .recommendations row).
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0

    # Revision activity (30d window, derived from .recommendations diffs).
    upward_revisions_30d: int = 0
    downward_revisions_30d: int = 0

    # Price-target distribution
    target_low: Optional[float] = None
    target_high: Optional[float] = None
    target_median: Optional[float] = None
    target_mean: Optional[float] = None
    # Median target delta over 30d (when historical .recommendations has
    # enough rows). Positive = consensus PT rising.
    target_median_delta_30d: Optional[float] = None
    target_count: int = 0

    # Directional tag derived from upward vs downward and target delta.
    velocity_tag: str = "flat"          # "accelerating-up" / "accelerating-down" / "flat"

    @property
    def total_ratings(self) -> int:
        return (
            self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell
        )

    @property
    def is_empty(self) -> bool:
        return self.total_ratings == 0 and self.target_count == 0

    def stage_2_block(self) -> str:
        lines: list[str] = []

        # Rating mix
        if self.total_ratings > 0:
            mix = []
            for label, n in [
                ("SB", self.strong_buy), ("B", self.buy), ("H", self.hold),
                ("S", self.sell), ("SS", self.strong_sell),
            ]:
                if n:
                    mix.append(f"{label}×{n}")
            lines.append(
                f"ratings ({self.total_ratings}): " + " ".join(mix)
            )

        # Revisions
        if self.upward_revisions_30d or self.downward_revisions_30d:
            lines.append(
                f"revisions 30d: ↑{self.upward_revisions_30d} ↓{self.downward_revisions_30d}"
            )

        # Price target distribution
        if self.target_count > 0:
            pt_parts = [f"PT ({self.target_count} analysts)"]
            if self.target_median is not None:
                pt_parts.append(f"median {self.target_median:.2f}")
            if self.target_low is not None and self.target_high is not None:
                pt_parts.append(
                    f"range {self.target_low:.2f}-{self.target_high:.2f}"
                )
            if self.target_median_delta_30d is not None:
                sign = "+" if self.target_median_delta_30d >= 0 else ""
                pt_parts.append(
                    f"30d Δ {sign}{self.target_median_delta_30d:.2f}"
                )
            lines.append(", ".join(pt_parts))

        # Velocity tag
        lines.append(f"velocity: {self.velocity_tag}")
        return "\n".join(lines)

    @classmethod
    def render_for_prompt(cls, rev: Optional["AnalystRevisions"]) -> str:
        if rev is None:
            return (
                "(analyst revisions unavailable — yfinance fetch failed)"
            )
        if rev.is_empty:
            return "(no analyst coverage)"
        return rev.stage_2_block()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

async def load_analyst_revisions(
    symbol: str,
    *,
    asof: Optional[date] = None,
    window_days: int = 30,
) -> Optional[AnalystRevisions]:
    try:
        return await asyncio.to_thread(
            _download_analyst_revisions, symbol, asof, window_days,
        )
    except Exception as exc:
        log.warning("load_analyst_revisions failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Yfinance integration (sync; run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _download_analyst_revisions(
    symbol: str,
    asof: Optional[date],
    window_days: int,
) -> AnalystRevisions:
    import yfinance as yf

    asof = asof or date.today()
    ticker = yf.Ticker(symbol)
    rev = AnalystRevisions(symbol=symbol.upper(), asof=asof.isoformat())

    # Recommendations history (DataFrame with cols varying by yfinance
    # version: strongBuy/buy/hold/sell/strongSell, or 'To Grade'/'From Grade').
    try:
        recs = ticker.recommendations
    except Exception as exc:
        log.debug("revisions: recommendations fetch failed for %s: %s", symbol, exc)
        recs = None

    if recs is not None and not getattr(recs, "empty", True):
        # Modern schema: rows per period with strongBuy/buy/hold/sell/strongSell
        if {"strongBuy", "buy", "hold", "sell", "strongSell"} <= set(recs.columns):
            latest = recs.iloc[0]
            rev.strong_buy = int(latest.get("strongBuy") or 0)
            rev.buy = int(latest.get("buy") or 0)
            rev.hold = int(latest.get("hold") or 0)
            rev.sell = int(latest.get("sell") or 0)
            rev.strong_sell = int(latest.get("strongSell") or 0)
            # Revision direction via diff vs prior period
            if len(recs) >= 2:
                prior = recs.iloc[1]
                # Up: cur strong_buy + buy > prior; Down: cur sell + strong_sell > prior
                cur_pos = (latest.get("strongBuy") or 0) + (latest.get("buy") or 0)
                prv_pos = (prior.get("strongBuy") or 0) + (prior.get("buy") or 0)
                cur_neg = (latest.get("sell") or 0) + (latest.get("strongSell") or 0)
                prv_neg = (prior.get("sell") or 0) + (prior.get("strongSell") or 0)
                if cur_pos > prv_pos:
                    rev.upward_revisions_30d = int(cur_pos - prv_pos)
                if cur_neg > prv_neg:
                    rev.downward_revisions_30d = int(cur_neg - prv_neg)
        # Legacy schema: per-row analyst actions ("To Grade" column)
        elif "To Grade" in recs.columns:
            cutoff = asof - timedelta(days=window_days)
            try:
                within = recs[recs.index >= cutoff.isoformat()]  # tz-naive ok
            except Exception:
                within = recs.head(20)
            for _, row in within.iterrows():
                to_grade = str(row.get("To Grade", "")).lower()
                from_grade = str(row.get("From Grade", "")).lower()
                if _is_upward(from_grade, to_grade):
                    rev.upward_revisions_30d += 1
                elif _is_downward(from_grade, to_grade):
                    rev.downward_revisions_30d += 1
                _bucket_rating(rev, to_grade)

    # Price targets
    try:
        pt = ticker.analyst_price_targets
    except Exception as exc:
        log.debug("revisions: price targets fetch failed for %s: %s", symbol, exc)
        pt = None

    if isinstance(pt, dict) and pt:
        rev.target_low = _finite_or_none(pt.get("low"))
        rev.target_high = _finite_or_none(pt.get("high"))
        rev.target_median = _finite_or_none(pt.get("median"))
        rev.target_mean = _finite_or_none(pt.get("mean"))
        rev.target_count = int(pt.get("numberOfAnalysts") or 0)

    # If we have no target dict, fall back to .info (older yfinance versions).
    if rev.target_count == 0:
        try:
            info = ticker.info or {}
            tgt_count = int(info.get("numberOfAnalystOpinions") or 0)
            if tgt_count > 0:
                rev.target_count = tgt_count
                rev.target_low = _finite_or_none(info.get("targetLowPrice"))
                rev.target_high = _finite_or_none(info.get("targetHighPrice"))
                rev.target_median = _finite_or_none(info.get("targetMedianPrice"))
                rev.target_mean = _finite_or_none(info.get("targetMeanPrice"))
        except Exception as exc:
            log.debug("revisions: info fallback failed for %s: %s", symbol, exc)

    # Velocity tag
    if rev.upward_revisions_30d > rev.downward_revisions_30d:
        rev.velocity_tag = "accelerating-up"
    elif rev.downward_revisions_30d > rev.upward_revisions_30d:
        rev.velocity_tag = "accelerating-down"
    else:
        rev.velocity_tag = "flat"

    return rev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BULLISH_GRADES = {"strong buy", "buy", "outperform", "overweight", "positive"}
_BEARISH_GRADES = {"strong sell", "sell", "underperform", "underweight", "negative"}
_NEUTRAL_GRADES = {"hold", "neutral", "market perform", "equal-weight", "equalweight"}


def _grade_rank(grade: str) -> int:
    g = grade.lower().strip()
    if g in _BULLISH_GRADES:
        return 1 if "strong" in g else 2
    if g in _NEUTRAL_GRADES:
        return 3
    if g in _BEARISH_GRADES:
        return 5 if "strong" in g else 4
    return 3  # default neutral


def _is_upward(from_grade: str, to_grade: str) -> bool:
    return _grade_rank(to_grade) < _grade_rank(from_grade)


def _is_downward(from_grade: str, to_grade: str) -> bool:
    return _grade_rank(to_grade) > _grade_rank(from_grade)


def _bucket_rating(rev: AnalystRevisions, grade: str) -> None:
    g = grade.lower().strip()
    if "strong buy" in g:
        rev.strong_buy += 1
    elif g in _BULLISH_GRADES:
        rev.buy += 1
    elif g in _NEUTRAL_GRADES:
        rev.hold += 1
    elif "strong sell" in g:
        rev.strong_sell += 1
    elif g in _BEARISH_GRADES:
        rev.sell += 1


def _finite_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


_ = statistics  # reserved for future Phase versions; keeps ruff quiet
