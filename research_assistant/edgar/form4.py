"""
Form 4 insider transaction parsing + aggregation (FOLLOWUPS #3).

Layers on the EdgarClient adapter from `client.py`. Parses Form 4 XML
(handles SEC's <value>-wrapper convention + joint filings), aggregates
across a trailing window per (issuer, owner), and emits compressed
strings for the Stage 1 batched filter and Stage 2 thesis prompts.

Public surface:
  - Dataclasses: Form4Owner, Form4Transaction, Form4Filing,
    OfficerActivity, InsiderActivitySummary
  - parse_form4(xml_text, *, accession_number, filing_date)
  - aggregate_insider_activity(filings, *, window_days, as_of)
  - load_insider_activity(symbol, ...)
  - load_insider_activities_batch(symbols, ...)
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from research_assistant.edgar.client import EdgarClient, Filing

log = logging.getLogger(__name__)


# Transaction codes that move shares on the open market (signal codes).
# Other codes (A grant, M option exercise, F tax withholding, G gift, D
# non-sale disposition, X in-the-money exercise, I discretionary) are tracked
# in the code mix but don't roll into the "buys" / "sales" counters.
_BUY_CODE = "P"
_SALE_CODE = "S"


@dataclass
class Form4Owner:
    """A single reporting owner on a Form 4 filing.

    Form 4s may list multiple owners (joint filers). The aggregation layer
    credits transactions to the first owner — sufficient for the
    individual-vs-bulk-insider signal at our compression level."""
    cik: str                       # 10-digit zero-padded
    name: str
    is_director: bool = False
    is_officer: bool = False
    officer_title: Optional[str] = None
    is_ten_percent_owner: bool = False


@dataclass
class Form4Transaction:
    """One row from either the nonDerivative or derivative table.

    transaction codes (subset relevant to signal):
      P = open-market purchase    (counted as buy)
      S = open-market sale        (counted as sale)
      A = grant/award             (no cash; price_per_share usually $0)
      M = option exercise / RSU vesting
      F = tax withholding on vesting
      G = gift
      D = non-sale disposition
      X = exercise of in-the-money derivative
      I = discretionary
    """
    date: str                       # ISO; transactionDate
    code: str                       # P / S / A / M / F / G / D / X / I / etc.
    shares: float
    price_per_share: float          # USD; 0 for grants and many derivative rows
    acquired_disposed: str          # "A" (acquired) or "D" (disposed)
    security_title: str             # e.g. "Common Stock"
    post_transaction_shares: Optional[float] = None
    is_derivative: bool = False

    @property
    def net_dollars(self) -> float:
        """Signed dollar amount: positive for acquisitions, negative for
        dispositions. Reports zero for $0-price entries (grants, exercises)
        without backfilling from market data — matches the scope decision
        not to introduce yfinance dependency in the aggregator."""
        sign = 1 if self.acquired_disposed == "A" else -1
        return sign * self.shares * self.price_per_share


@dataclass
class Form4Filing:
    """Parsed Form 4 with structured non-derivative and derivative tables."""
    accession_number: str
    filing_date: str
    period_of_report: str           # date the transaction(s) actually occurred
    issuer_cik: str
    issuer_ticker: str
    owners: list[Form4Owner]
    non_derivative: list[Form4Transaction] = field(default_factory=list)
    derivative: list[Form4Transaction] = field(default_factory=list)

    @property
    def primary_owner(self) -> Optional[Form4Owner]:
        return self.owners[0] if self.owners else None


@dataclass
class OfficerActivity:
    """Per-officer roll-up over the aggregation window."""
    cik: str
    name: str
    relationship: str               # human-readable: "CEO" / "Director" / "10% Owner"
    buys_count: int = 0             # code-P transactions, non-derivative
    sales_count: int = 0            # code-S transactions, non-derivative
    other_count: int = 0            # all other codes, non-derivative
    net_shares: float = 0.0         # signed; non-derivative only
    net_dollars: float = 0.0        # signed; non-derivative only
    latest_transaction_date: Optional[str] = None


@dataclass
class InsiderActivitySummary:
    """Compressed insider-activity view for orchestrator prompts.

    Common-stock (non-derivative) flows roll into buys/sales/net_dollars.
    Derivative-table activity surfaces only as deriv_code_mix — option
    exercises and grants are noisy and shouldn't dilute the cash-flow signal."""
    window_days: int
    window_start: str
    window_end: str
    total_filings: int
    buys_count: int
    sales_count: int
    net_dollars: float                       # signed; common-stock only
    code_mix: dict[str, int]                 # non-derivative codes
    deriv_code_mix: dict[str, int]           # derivative codes
    by_officer: list[OfficerActivity]        # sorted by abs(net_dollars) desc
    latest_transaction_date: Optional[str]

    def stage_1_line(self) -> str:
        """One-line filter summary for batched Stage 1 Haiku prompts.
        Format mirrors the FOLLOWUPS #3 example:
        "insider net flow last 90d: -$42M / 4 sales / 0 buys"."""
        return (
            f"insider net flow last {self.window_days}d: "
            f"{_fmt_dollars(self.net_dollars)} / "
            f"{self.sales_count} sales / {self.buys_count} buys"
        )

    def stage_2_block(self) -> str:
        """Multi-line Stage 2 enrichment block (committed-ticker DD).

        Includes counts, net $, code mix, latest transaction date, and the
        top-3 officers ranked by absolute dollar impact."""
        lines: list[str] = []
        head = (
            f"{self.sales_count} sales / {self.buys_count} buys last "
            f"{self.window_days}d, net {_fmt_dollars(self.net_dollars)}"
        )
        if self.latest_transaction_date:
            head += f", latest {self.latest_transaction_date}"
        lines.append(head)
        if self.code_mix:
            ordered = sorted(self.code_mix.items(), key=lambda kv: -kv[1])
            lines.append("codes: " + ", ".join(f"{c}×{n}" for c, n in ordered))
        top = [o for o in self.by_officer if o.net_dollars != 0][:3]
        if top:
            officer_strs = [
                f"{o.relationship} {_fmt_dollars(o.net_dollars)}" for o in top
            ]
            lines.append("top: " + "; ".join(officer_strs))
        return "\n".join(lines)

    @classmethod
    def render_for_prompt(cls, summary: Optional["InsiderActivitySummary"]) -> str:
        """Three-state prompt rendering: None (EDGAR fetch failed or
        ticker not in SEC universe) / empty (no Form 4 in window) /
        populated (full stage_2_block). Single source of truth for how
        the cascade sees insider-activity signal across /research and
        /probe — was previously split between this dataclass and an
        orchestrator-side helper."""
        if summary is None:
            return (
                "(insider activity unavailable — EDGAR fetch failed or "
                "ticker not in SEC universe)"
            )
        if summary.total_filings == 0:
            return f"(no Form 4 filings last {summary.window_days}d)"
        return summary.stage_2_block()


def _fmt_dollars(amount: float) -> str:
    """Compact human dollar formatting: $1.2B / $42M / $850K / $200."""
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.0f}"


def _relationship_label(owner: Form4Owner) -> str:
    if owner.officer_title:
        return owner.officer_title
    if owner.is_officer:
        return "Officer"
    if owner.is_director:
        return "Director"
    if owner.is_ten_percent_owner:
        return "10% Owner"
    return "Insider"


# ---------------------------------------------------------------------------
# Form 4 XML parser
# ---------------------------------------------------------------------------

def parse_form4(
    xml_text: str,
    *,
    accession_number: str,
    filing_date: str,
) -> Form4Filing:
    """Parse a Form 4 XML document into a structured Form4Filing.

    The SEC Form 4 schema wraps most leaf values in `<value>` subelements
    (e.g. `<transactionShares><value>120000</value></transactionShares>`)
    while simple identification fields (issuerCik, periodOfReport) use
    direct text. `_xml_text` accepts either."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(
            f"Form 4 XML parse failed for {accession_number}: {exc}"
        ) from exc

    issuer = root.find("issuer")
    issuer_cik_raw = _xml_text(issuer, "issuerCik") if issuer is not None else None
    issuer_cik = (issuer_cik_raw or "").zfill(10) if issuer_cik_raw else ""
    issuer_ticker = (
        _xml_text(issuer, "issuerTradingSymbol") if issuer is not None else None
    ) or ""

    period = _xml_text(root, "periodOfReport") or ""

    owners = [_parse_owner(el) for el in root.findall("reportingOwner")]

    non_derivative = [
        _parse_transaction(t, is_derivative=False)
        for t in root.findall("nonDerivativeTable/nonDerivativeTransaction")
    ]
    derivative = [
        _parse_transaction(t, is_derivative=True)
        for t in root.findall("derivativeTable/derivativeTransaction")
    ]

    return Form4Filing(
        accession_number=accession_number,
        filing_date=filing_date,
        period_of_report=period,
        issuer_cik=issuer_cik,
        issuer_ticker=issuer_ticker,
        owners=owners,
        non_derivative=non_derivative,
        derivative=derivative,
    )


def _parse_owner(el: ET.Element) -> Form4Owner:
    owner_id = el.find("reportingOwnerId")
    rel = el.find("reportingOwnerRelationship")
    cik_raw = _xml_text(owner_id, "rptOwnerCik") if owner_id is not None else None
    name = (_xml_text(owner_id, "rptOwnerName") if owner_id is not None else None) or ""
    return Form4Owner(
        cik=(cik_raw or "").zfill(10) if cik_raw else "",
        name=name,
        is_director=_xml_bool(rel, "isDirector"),
        is_officer=_xml_bool(rel, "isOfficer"),
        officer_title=_xml_text(rel, "officerTitle"),
        is_ten_percent_owner=_xml_bool(rel, "isTenPercentOwner"),
    )


def _parse_transaction(el: ET.Element, *, is_derivative: bool) -> Form4Transaction:
    amounts = el.find("transactionAmounts")
    coding = el.find("transactionCoding")
    post = el.find("postTransactionAmounts")
    return Form4Transaction(
        date=_xml_text(el, "transactionDate") or "",
        code=_xml_text(coding, "transactionCode") or "",
        shares=_xml_float(amounts, "transactionShares"),
        price_per_share=_xml_float(amounts, "transactionPricePerShare"),
        acquired_disposed=_xml_text(amounts, "transactionAcquiredDisposedCode") or "",
        security_title=_xml_text(el, "securityTitle") or "",
        post_transaction_shares=_xml_float_or_none(post, "sharesOwnedFollowingTransaction"),
        is_derivative=is_derivative,
    )


def _xml_text(parent: Optional[ET.Element], path: str) -> Optional[str]:
    """Find `path` under parent; return its text content. Handles both
    direct text (<periodOfReport>2026-05-19</...>) and the SEC's
    <value>-wrapper convention (<transactionDate><value>2026-05-19</...></...>)."""
    if parent is None:
        return None
    el = parent.find(path)
    if el is None:
        return None
    if el.text and el.text.strip():
        return el.text.strip()
    value_el = el.find("value")
    if value_el is not None and value_el.text:
        return value_el.text.strip()
    return None


def _xml_bool(parent: Optional[ET.Element], path: str) -> bool:
    txt = _xml_text(parent, path)
    if txt is None:
        return False
    return txt.lower() in ("1", "true")


def _xml_float(parent: Optional[ET.Element], path: str, default: float = 0.0) -> float:
    txt = _xml_text(parent, path)
    if txt is None:
        return default
    try:
        return float(txt)
    except ValueError:
        return default


def _xml_float_or_none(parent: Optional[ET.Element], path: str) -> Optional[float]:
    txt = _xml_text(parent, path)
    if txt is None:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Form 4 fetch (free function — inverted from a client method to keep
# EdgarClient free of form-type knowledge; the lazy-import workaround
# in the old EdgarClient.fetch_form4 was a symptom of that inversion.)
# ---------------------------------------------------------------------------

async def fetch_form4(client: EdgarClient, filing: Filing) -> Form4Filing:
    """Fetch + parse one Form 4 filing.

    Strict form_type check stays here (not on EdgarClient) so the client
    primitive itself doesn't gain form-aware behavior."""
    if filing.form_type != "4":
        raise ValueError(
            f"fetch_form4 requires form_type='4', got {filing.form_type!r}"
        )
    response = await client.get(filing.archive_url)
    return parse_form4(
        response.text,
        accession_number=filing.accession_number,
        filing_date=filing.filing_date,
    )


# ---------------------------------------------------------------------------
# Insider activity aggregation
# ---------------------------------------------------------------------------

def aggregate_insider_activity(
    filings: list[Form4Filing],
    *,
    window_days: int = 90,
    as_of: Optional[date] = None,
) -> InsiderActivitySummary:
    """Compress a list of Form 4 filings into a per-window summary.

    Filings are filtered to those whose `period_of_report` falls inside
    [as_of - window_days, as_of]. Transactions are attributed to the
    filing's primary_owner.
    """
    as_of = as_of or date.today()
    window_start = (as_of - timedelta(days=window_days)).isoformat()
    window_end = as_of.isoformat()

    def _in_window(f: Form4Filing) -> bool:
        anchor = f.period_of_report or f.filing_date
        return bool(anchor) and window_start <= anchor <= window_end

    in_window = [f for f in filings if _in_window(f)]

    buys_count = 0
    sales_count = 0
    net_dollars = 0.0
    code_mix: dict[str, int] = {}
    deriv_code_mix: dict[str, int] = {}
    by_officer: dict[str, OfficerActivity] = {}
    latest_tx: Optional[str] = None

    for f in in_window:
        # Top-line counters accumulate for every filing in the window.
        # Per-officer attribution is gated on having an owner — an empty
        # <reportingOwner> (rare but possible from malformed XML) must not
        # silently drop the transactions from buys/sales/net_dollars.
        owner = f.primary_owner
        oa: Optional[OfficerActivity] = None
        if owner is not None:
            oa = by_officer.setdefault(
                owner.cik,
                OfficerActivity(
                    cik=owner.cik,
                    name=owner.name,
                    relationship=_relationship_label(owner),
                ),
            )
        for t in f.non_derivative:
            if not t.code:
                continue
            code_mix[t.code] = code_mix.get(t.code, 0) + 1
            if t.code == _BUY_CODE:
                buys_count += 1
                if oa is not None:
                    oa.buys_count += 1
            elif t.code == _SALE_CODE:
                sales_count += 1
                if oa is not None:
                    oa.sales_count += 1
            elif oa is not None:
                oa.other_count += 1
            tx_value = t.net_dollars
            net_dollars += tx_value
            if oa is not None:
                oa.net_dollars += tx_value
                sign = 1 if t.acquired_disposed == "A" else -1
                oa.net_shares += sign * t.shares
                if t.date and (
                    oa.latest_transaction_date is None
                    or t.date > oa.latest_transaction_date
                ):
                    oa.latest_transaction_date = t.date
            if t.date and (latest_tx is None or t.date > latest_tx):
                latest_tx = t.date
        for t in f.derivative:
            if not t.code:
                continue
            deriv_code_mix[t.code] = deriv_code_mix.get(t.code, 0) + 1

    by_officer_sorted = sorted(
        by_officer.values(), key=lambda o: -abs(o.net_dollars)
    )

    return InsiderActivitySummary(
        window_days=window_days,
        window_start=window_start,
        window_end=window_end,
        total_filings=len(in_window),
        buys_count=buys_count,
        sales_count=sales_count,
        net_dollars=net_dollars,
        code_mix=code_mix,
        deriv_code_mix=deriv_code_mix,
        by_officer=by_officer_sorted,
        latest_transaction_date=latest_tx,
    )


# ---------------------------------------------------------------------------
# High-level loader: ticker → InsiderActivitySummary
# ---------------------------------------------------------------------------

INSIDER_DEFAULT_WINDOW_DAYS = 90
INSIDER_DEFAULT_MAX_FILINGS = 25


async def load_insider_activities_batch(
    symbols: list[str],
    *,
    window_days: int = INSIDER_DEFAULT_WINDOW_DAYS,
    max_filings_per_ticker: int = INSIDER_DEFAULT_MAX_FILINGS,
    client: Optional[EdgarClient] = None,
    as_of: Optional[date] = None,
) -> dict[str, Optional[InsiderActivitySummary]]:
    """Batch version of `load_insider_activity` for /brief's universe scan.

    Shares one EdgarClient across all tickers so the CIK ticker-index is
    fetched once and reused. Symbols are fanned out via `asyncio.gather`;
    the client's rate limiter serializes outbound requests naturally.

    Returns a dict keyed by uppercase symbol. Each value is the
    InsiderActivitySummary (which may itself be empty — total_filings=0)
    or None when the per-ticker load failed (graceful degrade).
    """
    owns_client = client is None
    if client is None:
        client = EdgarClient()
    try:
        results = await asyncio.gather(
            *[
                load_insider_activity(
                    s,
                    window_days=window_days,
                    max_filings=max_filings_per_ticker,
                    client=client,
                    as_of=as_of,
                )
                for s in symbols
            ],
        )
        return {s.upper(): r for s, r in zip(symbols, results)}
    finally:
        if owns_client:
            await client.close()


async def load_insider_activity(
    symbol: str,
    *,
    window_days: int = INSIDER_DEFAULT_WINDOW_DAYS,
    max_filings: int = INSIDER_DEFAULT_MAX_FILINGS,
    client: Optional[EdgarClient] = None,
    as_of: Optional[date] = None,
) -> Optional[InsiderActivitySummary]:
    """Fetch + aggregate Form 4 activity for `symbol` over the trailing
    `window_days` window. Returns None on any failure so the caller can
    gracefully degrade (matches the yfinance pattern in data_loader).

    Args:
        symbol: ticker (case-insensitive).
        window_days: trailing window for the aggregation summary.
        max_filings: hard cap on Form 4 XML fetches per call. Bounds the
            rate-limit cost; older filings beyond the cap are dropped.
        client: reuse an existing EdgarClient when called inside a loop;
            otherwise a one-shot client is created and closed.
        as_of: reference date for the window (defaults to today).
    """
    owns_client = client is None
    if client is None:
        client = EdgarClient()
    try:
        cik = await client.resolve_cik(symbol)
        if cik is None:
            log.info("EDGAR: no CIK for %s (foreign issuer / OTC / delisted)", symbol)
            return None
        as_of = as_of or date.today()
        since = (as_of - timedelta(days=window_days)).isoformat()
        filings = await client.list_filings(cik, "4", since=since, limit=max_filings)
        if not filings:
            return aggregate_insider_activity(
                [], window_days=window_days, as_of=as_of,
            )
        parsed = await asyncio.gather(
            *[fetch_form4(client, f) for f in filings],
            return_exceptions=True,
        )
        good: list[Form4Filing] = []
        for f, result in zip(filings, parsed):
            if isinstance(result, Exception):
                log.warning(
                    "EDGAR: Form 4 parse/fetch failed for %s (%s): %s",
                    symbol, f.accession_number, result,
                )
                continue
            good.append(result)
        return aggregate_insider_activity(
            good, window_days=window_days, as_of=as_of,
        )
    except Exception as exc:
        log.warning("EDGAR: load_insider_activity failed for %s: %s", symbol, exc)
        return None
    finally:
        if owns_client:
            await client.close()
