"""
Phase A deterministic substrate — KPI extraction + earnings calendar.

Pulls multi-year income-statement + balance-sheet KPIs from yfinance
(`yfinance.Ticker(sym).financials`, `.balance_sheet`, `.info`) and emits a
compressed prompt block for Stage 2 thesis. The synthesizer (Phase B) will
read the same block as substrate; the deep filing reader (Phase D) will
complement it with line-item decomposition.

Why this lives outside data_loader.py: the existing module's docstring
claims to be "the SINGLE integration point with live yfinance" but it's
scoped to ticker_data / headlines / world_state. Phase A intentionally
introduces sibling modules under `fundamentals/`, `positioning/`,
`deep_reads/` so each substrate axis owns its yfinance integration. Same
asyncio.to_thread wrapping pattern (yfinance is sync; we run it in a
thread to keep the gather non-blocking).

Three-state semantics (mirrors InsiderActivitySummary):
  - None        → fetch failed / ticker not in yfinance universe
  - empty       → fetch succeeded but no usable data (e.g. brand-new IPO)
  - populated   → render_for_prompt emits the compressed block
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


# Number of trailing fiscal years to surface. Four covers the multi-year
# trend cases EXP1 caught (PLUG's 4-year negative gross profit, RGTI's
# 3-year revenue decline) without bloating the prompt past ~600 tokens.
_HISTORICAL_YEARS = 4


@dataclass
class AnnualLine:
    """One fiscal-year row of a single line item."""
    fiscal_year: int                     # 2024 / 2025 / etc.
    value: Optional[float]               # dollars; None means missing/NaN


@dataclass
class KPISummary:
    """Compressed multi-year KPI view for Stage 2 thesis substrate.

    Income-statement series carry the last `_HISTORICAL_YEARS` of annual
    values, oldest first. Balance items are point-in-time (most-recent FY).
    Multiples reflect current quote vs trailing-year fundamentals.

    Gross margin is computed (not separately fetched) when both revenue
    and gross profit are present for the same FY — the synthesizer reads
    margin trajectory directly off this field.

    Three-state contract:
      - `revenue` empty AND `balance_cash` None means "fetch returned no
        usable data" → render_for_prompt emits the empty-data sentinel
      - mixed-null fields are rendered as `?` so the prompt doesn't lose
        the line entirely
    """
    symbol: str
    asof: str                            # ISO date when loaded
    # Multi-year income statement (oldest first; up to _HISTORICAL_YEARS)
    revenue: list[AnnualLine] = field(default_factory=list)
    gross_profit: list[AnnualLine] = field(default_factory=list)
    operating_income: list[AnnualLine] = field(default_factory=list)
    net_income: list[AnnualLine] = field(default_factory=list)
    # Point-in-time balance items (most-recent FY)
    balance_cash: Optional[float] = None
    balance_total_debt: Optional[float] = None
    balance_asof: Optional[str] = None   # YYYY-MM-DD of the latest FY end
    # Multiples (current quote vs trailing/forward fundamentals)
    market_cap: Optional[float] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_sales: Optional[float] = None
    price_to_book: Optional[float] = None
    # Short-interest snapshot
    short_percent_of_float: Optional[float] = None  # fraction, 0.10 = 10%
    short_ratio: Optional[float] = None             # days to cover

    @property
    def is_empty(self) -> bool:
        """True when no usable substrate was retrieved.

        We treat "no income-statement rows AND no balance data" as the
        empty case so a partial-success fetch (e.g. multiples populated
        from .info but financials() failed) still renders. The synthesizer
        can decide whether a multiples-only view is signal."""
        return (
            not self.revenue
            and not self.gross_profit
            and not self.operating_income
            and not self.net_income
            and self.balance_cash is None
            and self.balance_total_debt is None
        )

    def gross_margin_series(self) -> list[tuple[int, Optional[float]]]:
        """Per-FY gross margin as fraction (0.42 = 42%). None for rows
        missing either revenue or gross_profit."""
        rev_by_fy = {row.fiscal_year: row.value for row in self.revenue}
        gp_by_fy = {row.fiscal_year: row.value for row in self.gross_profit}
        years = sorted(set(rev_by_fy) | set(gp_by_fy))
        out: list[tuple[int, Optional[float]]] = []
        for fy in years:
            r = rev_by_fy.get(fy)
            g = gp_by_fy.get(fy)
            if r and g is not None and r != 0:
                out.append((fy, g / r))
            else:
                out.append((fy, None))
        return out

    def stage_2_block(self) -> str:
        """Multi-line Stage-2 prompt block.

        Sections, in render order:
          1. Income-statement trend (revenue / gross_profit / operating /
             net) with gross margin computed inline
          2. Balance liquidity (cash + total debt, latest FY)
          3. Multiples (P/E trailing+forward, P/S, P/B, market cap)
          4. Short positioning (% of float, days to cover) when present
        """
        lines: list[str] = []

        # Income-statement trend (one line per series, oldest → newest).
        for label, series in [
            ("revenue", self.revenue),
            ("gross_profit", self.gross_profit),
            ("operating_income", self.operating_income),
            ("net_income", self.net_income),
        ]:
            if series:
                cells = [
                    f"{row.fiscal_year}: {_fmt_dollars(row.value)}"
                    for row in series
                ]
                lines.append(f"{label}: " + " | ".join(cells))

        # Gross-margin trajectory: derived, so render it only when at
        # least one year resolves.
        gm = self.gross_margin_series()
        if any(v is not None for _, v in gm):
            cells = [
                f"{fy}: {_fmt_pct(v) if v is not None else '?'}"
                for fy, v in gm
            ]
            lines.append("gross_margin: " + " | ".join(cells))

        # Balance — single line summarising point-in-time liquidity.
        if self.balance_cash is not None or self.balance_total_debt is not None:
            parts = []
            if self.balance_cash is not None:
                parts.append(f"cash {_fmt_dollars(self.balance_cash)}")
            if self.balance_total_debt is not None:
                parts.append(f"debt {_fmt_dollars(self.balance_total_debt)}")
            asof = f" (as of {self.balance_asof})" if self.balance_asof else ""
            lines.append("balance: " + ", ".join(parts) + asof)

        # Multiples — one line; only emit fields that resolved.
        mult_parts = []
        if self.trailing_pe is not None:
            mult_parts.append(f"PE {self.trailing_pe:.1f}")
        if self.forward_pe is not None:
            mult_parts.append(f"fwdPE {self.forward_pe:.1f}")
        if self.price_to_sales is not None:
            # P/S extremes (e.g. RGTI 653x) matter; render to 1 decimal
            # at all magnitudes.
            mult_parts.append(f"P/S {self.price_to_sales:.1f}x")
        if self.price_to_book is not None:
            mult_parts.append(f"P/B {self.price_to_book:.1f}x")
        if self.market_cap is not None:
            mult_parts.append(f"mcap {_fmt_dollars(self.market_cap)}")
        if mult_parts:
            lines.append("multiples: " + ", ".join(mult_parts))

        # Short positioning — only when at least one short field is
        # present (many tickers lack short data on yfinance).
        if self.short_percent_of_float is not None or self.short_ratio is not None:
            short_parts = []
            if self.short_percent_of_float is not None:
                short_parts.append(
                    f"{_fmt_pct(self.short_percent_of_float)} of float"
                )
            if self.short_ratio is not None:
                short_parts.append(f"days-to-cover {self.short_ratio:.1f}")
            lines.append("short: " + ", ".join(short_parts))

        return "\n".join(lines)

    @classmethod
    def render_for_prompt(cls, summary: Optional["KPISummary"]) -> str:
        """Three-state rendering matching InsiderActivitySummary's contract.

        None         → fetch failure sentinel; populated → stage_2_block;
        empty (loaded but no usable data) → distinct sentinel so the
        synthesizer can tell a fetch failure from a sparse ticker
        (e.g. fresh IPO, foreign ADR with no .financials)."""
        if summary is None:
            return (
                "(KPI summary unavailable — yfinance fetch failed or "
                "ticker not in fundamentals universe)"
            )
        if summary.is_empty:
            return "(no KPI data — sparse fundamentals universe)"
        return summary.stage_2_block()


@dataclass
class EarningsCalendar:
    """Earnings-date calendar surface.

    Yfinance exposes a `.calendar` dict whose shape changed across versions
    (DataFrame in older builds, plain dict in 0.2.x+). The loader normalises
    both into this dataclass; the prompt block surfaces the next-earnings
    distance + most-recent-reported date as compressed text.
    """
    symbol: str
    asof: str
    next_earnings_date: Optional[str] = None     # YYYY-MM-DD
    days_until_earnings: Optional[int] = None
    last_earnings_date: Optional[str] = None
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        return (
            self.next_earnings_date is None
            and self.last_earnings_date is None
            and self.eps_estimate is None
        )

    def stage_2_block(self) -> str:
        lines: list[str] = []
        if self.next_earnings_date:
            distance = (
                f" ({self.days_until_earnings}d out)"
                if self.days_until_earnings is not None
                else ""
            )
            head = f"next earnings: {self.next_earnings_date}{distance}"
            estimates = []
            if self.eps_estimate is not None:
                estimates.append(f"EPS est {self.eps_estimate:.2f}")
            if self.revenue_estimate is not None:
                estimates.append(f"rev est {_fmt_dollars(self.revenue_estimate)}")
            if estimates:
                head += " — " + ", ".join(estimates)
            lines.append(head)
        if self.last_earnings_date:
            lines.append(f"last earnings: {self.last_earnings_date}")
        return "\n".join(lines) if lines else "(no calendar data)"

    @classmethod
    def render_for_prompt(cls, cal: Optional["EarningsCalendar"]) -> str:
        if cal is None:
            return (
                "(earnings calendar unavailable — yfinance fetch failed)"
            )
        if cal.is_empty:
            return "(no scheduled earnings date)"
        return cal.stage_2_block()


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

async def load_kpi_summary(
    symbol: str,
    *,
    asof: Optional[date] = None,
) -> Optional[KPISummary]:
    """Fetch + assemble KPISummary for `symbol`.

    Returns None on any unrecoverable error so the caller can gracefully
    degrade (same pattern as load_insider_activity). Partial successes
    are returned as populated KPISummary with sparse fields.
    """
    try:
        return await asyncio.to_thread(_download_kpi_summary, symbol, asof)
    except Exception as exc:
        log.warning("load_kpi_summary failed for %s: %s", symbol, exc)
        return None


async def load_earnings_calendar(
    symbol: str,
    *,
    asof: Optional[date] = None,
) -> Optional[EarningsCalendar]:
    """Fetch + normalise the next/last earnings calendar for `symbol`.

    Yfinance's `.calendar` returns either a DataFrame or a plain dict
    depending on installed version. Both are handled here. Returns None
    on any error."""
    try:
        return await asyncio.to_thread(_download_earnings_calendar, symbol, asof)
    except Exception as exc:
        log.warning("load_earnings_calendar failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Yfinance integration (sync; run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _download_kpi_summary(
    symbol: str,
    asof: Optional[date],
) -> KPISummary:
    """Sync yfinance pull, normalised into KPISummary.

    Three sub-fetches:
      - .financials (annual income statement) → revenue + GP + OI + NI
        4-year series, oldest first
      - .balance_sheet (annual balance) → cash + total_debt point-in-time
      - .info (mixed metadata dict) → market cap, multiples, short stats

    Any sub-fetch failure is non-fatal — empty list / None returned for
    the relevant fields, summary is still emitted with partial substrate."""
    import yfinance as yf

    asof = asof or date.today()
    ticker = yf.Ticker(symbol)
    summary = KPISummary(symbol=symbol.upper(), asof=asof.isoformat())

    # Income statement (annual)
    try:
        fin = ticker.financials  # DataFrame: rows = line items, cols = FY ends
        summary.revenue = _extract_series(fin, ["Total Revenue", "Revenue"])
        summary.gross_profit = _extract_series(fin, ["Gross Profit"])
        summary.operating_income = _extract_series(
            fin, ["Operating Income", "Total Operating Income As Reported"]
        )
        summary.net_income = _extract_series(
            fin, ["Net Income", "Net Income Common Stockholders"]
        )
    except Exception as exc:
        log.debug("KPI: financials read failed for %s: %s", symbol, exc)

    # Balance sheet (annual)
    try:
        bs = ticker.balance_sheet
        summary.balance_cash = _latest_value(
            bs, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]
        )
        summary.balance_total_debt = _latest_value(
            bs, ["Total Debt", "Long Term Debt", "Net Debt"]
        )
        summary.balance_asof = _latest_column_iso(bs)
    except Exception as exc:
        log.debug("KPI: balance_sheet read failed for %s: %s", symbol, exc)

    # Multiples + short stats via .info (slowest fetch; do last so the
    # cheaper income/balance pulls aren't blocked).
    try:
        info = ticker.info or {}
        summary.market_cap = _finite_or_none(info.get("marketCap"))
        summary.trailing_pe = _finite_or_none(info.get("trailingPE"))
        summary.forward_pe = _finite_or_none(info.get("forwardPE"))
        summary.price_to_sales = _finite_or_none(
            info.get("priceToSalesTrailing12Months") or info.get("priceToSales")
        )
        summary.price_to_book = _finite_or_none(info.get("priceToBook"))
        summary.short_percent_of_float = _finite_or_none(
            info.get("shortPercentOfFloat")
        )
        summary.short_ratio = _finite_or_none(info.get("shortRatio"))
    except Exception as exc:
        log.debug("KPI: info read failed for %s: %s", symbol, exc)

    return summary


def _download_earnings_calendar(
    symbol: str,
    asof: Optional[date],
) -> EarningsCalendar:
    import yfinance as yf

    asof = asof or date.today()
    ticker = yf.Ticker(symbol)
    cal = EarningsCalendar(symbol=symbol.upper(), asof=asof.isoformat())

    try:
        raw = ticker.calendar
    except Exception as exc:
        log.debug("calendar: read failed for %s: %s", symbol, exc)
        return cal

    if raw is None:
        return cal

    # Yfinance 0.2.x returns a dict; older versions returned a DataFrame.
    # Normalise to dict by reading either the dict directly or the first
    # column of the DataFrame.
    parsed = _normalise_calendar(raw)
    if not parsed:
        return cal

    next_earnings = parsed.get("Earnings Date")
    if isinstance(next_earnings, (list, tuple)) and next_earnings:
        next_earnings = next_earnings[0]
    if next_earnings is not None:
        iso = _coerce_iso_date(next_earnings)
        if iso:
            cal.next_earnings_date = iso
            try:
                cal.days_until_earnings = (
                    datetime.fromisoformat(iso).date() - asof
                ).days
            except Exception:
                cal.days_until_earnings = None

    cal.eps_estimate = _finite_or_none(parsed.get("Earnings Average"))
    cal.revenue_estimate = _finite_or_none(parsed.get("Revenue Average"))

    return cal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_series(df, candidate_rows: list[str]) -> list[AnnualLine]:
    """Pull a line item across years from a yfinance financials DataFrame.

    Yfinance returns rows-as-line-items, cols-as-FY-ends (oldest right).
    We try `candidate_rows` in order until one resolves; missing rows
    return []. The output is oldest-first and capped at _HISTORICAL_YEARS.
    """
    if df is None or getattr(df, "empty", True):
        return []
    row = None
    for label in candidate_rows:
        if label in df.index:
            row = df.loc[label]
            break
    if row is None:
        return []
    # Sort columns by date so we can take the trailing window deterministically.
    try:
        cols_sorted = sorted(row.index)
    except TypeError:
        cols_sorted = list(row.index)
    cols_trimmed = cols_sorted[-_HISTORICAL_YEARS:]
    out: list[AnnualLine] = []
    for col in cols_trimmed:
        fy = _column_to_fy(col)
        if fy is None:
            continue
        raw = row[col]
        try:
            v = float(raw)
            if not math.isfinite(v):
                v = None
        except (TypeError, ValueError):
            v = None
        out.append(AnnualLine(fiscal_year=fy, value=v))
    return out


def _latest_value(df, candidate_rows: list[str]) -> Optional[float]:
    """Latest FY value of the first matching row label."""
    if df is None or getattr(df, "empty", True):
        return None
    row = None
    for label in candidate_rows:
        if label in df.index:
            row = df.loc[label]
            break
    if row is None:
        return None
    try:
        cols_sorted = sorted(row.index)
    except TypeError:
        cols_sorted = list(row.index)
    if not cols_sorted:
        return None
    raw = row[cols_sorted[-1]]
    try:
        v = float(raw)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _latest_column_iso(df) -> Optional[str]:
    """ISO date of the most-recent column (FY end) in a yfinance frame."""
    if df is None or getattr(df, "empty", True):
        return None
    try:
        cols_sorted = sorted(df.columns)
    except TypeError:
        cols_sorted = list(df.columns)
    if not cols_sorted:
        return None
    return _coerce_iso_date(cols_sorted[-1])


def _column_to_fy(col) -> Optional[int]:
    """Map a yfinance column header (Timestamp / date / str) to a fiscal year."""
    if hasattr(col, "year"):
        try:
            return int(col.year)
        except Exception:
            return None
    try:
        return int(str(col)[:4])
    except Exception:
        return None


def _coerce_iso_date(value) -> Optional[str]:
    """Coerce a yfinance date-ish value to YYYY-MM-DD; None on failure."""
    if value is None:
        return None
    if isinstance(value, str):
        # Already ISO-ish?
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return None
    return None


def _finite_or_none(value) -> Optional[float]:
    """Float-or-None coercion that drops NaN / inf / unset sentinels."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _normalise_calendar(raw) -> dict:
    """Yfinance's `.calendar` is dict in 0.2.x, DataFrame in older versions.
    Return a flat dict either way."""
    if isinstance(raw, dict):
        return raw
    # DataFrame: first column is "Value"; pivot to a flat dict.
    if hasattr(raw, "to_dict"):
        try:
            d = raw.to_dict()
            if d and "Value" in d:
                return d["Value"]
            # Otherwise flatten — pick the first column.
            for col_key, col_dict in d.items():
                if isinstance(col_dict, dict):
                    return col_dict
        except Exception:
            return {}
    return {}


def _fmt_dollars(amount: Optional[float]) -> str:
    """Compact human dollar formatting: $1.2B / $42M / $850K / $200 / ?."""
    if amount is None:
        return "?"
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.0f}"


def _fmt_pct(fraction: Optional[float]) -> str:
    """0.42 → "42%"; None → "?"."""
    if fraction is None:
        return "?"
    return f"{fraction * 100:.1f}%"
