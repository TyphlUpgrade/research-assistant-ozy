"""
Form 13F-HR institutional holdings parsing + per-stock aggregation
(FOLLOWUPS #5).

Layers on the EdgarClient adapter from `client.py`. Parses the SEC
infotable.xml schema, flips per-fund holdings into a per-stock view for
a target ticker, and computes new/exited/held position deltas across
two consecutive quarters.

Public surface:
  - Dataclasses: TrackedFund, Form13FHolding, Form13FFiling,
    FundPosition, InstitutionalOwnership
  - parse_13f(xml_text, *, accession_number, filing_date, manager_cik,
    manager_name)
  - aggregate_institutional_ownership(current, prior, *, ticker,
    issuer_match)
  - load_institutional_ownership(ticker, *, tracked_funds=, issuer_match=,
    client=)

Curated fund approach: per-stock aggregation requires the per-fund 13F
orientation to be flipped. Without a CUSIP master, ticker→issuer matching
is done by substring on the SEC-reported issuer name. Operators provide
a small curated list of fund CIKs (DEFAULT_TRACKED_FUNDS); the loader
fetches each fund's two most-recent 13F-HRs and aggregates positions
matching the issuer.

Value scaling: pre-2023 13Fs report value in $thousands; from 2023
onward in whole dollars. The parser normalizes both to whole dollars
based on `period_of_report`.

Period inference: 13F-HRs are due 45 days after quarter end. The
infotable.xml does not contain `periodOfReport` (that lives in the
sibling cover form); we derive it deterministically from `filing_date`
to avoid a second HTTP fetch per filing.
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


# ---------------------------------------------------------------------------
# Tracked-fund universe
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackedFund:
    """A fund the operator wants institutional-ownership signal for.
    `cik` must be the 10-digit zero-padded EDGAR CIK of the filing entity
    (NOT the fund's marketing name — many fund families file under
    multiple CIKs; pick the parent that holds the bulk of 13F AUM)."""
    cik: str
    name: str


# Starter set — operator-configurable. CIKs should be verified against
# data.sec.gov/submissions before relying on the signal in production.
# Pass a custom `tracked_funds=` argument to load_institutional_ownership
# to extend / replace this list per session.
DEFAULT_TRACKED_FUNDS: tuple[TrackedFund, ...] = (
    TrackedFund(cik="0001364742", name="BlackRock"),
    TrackedFund(cik="0000102909", name="Vanguard"),
    TrackedFund(cik="0000093751", name="State Street"),
    TrackedFund(cik="0001067983", name="Berkshire Hathaway"),
    TrackedFund(cik="0000315066", name="FMR (Fidelity)"),
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Form13FHolding:
    """One row from an infotable.xml — one issuer position held by one
    filing manager at one period."""
    cusip: str                       # 9-character CUSIP
    issuer_name: str                 # SEC-reported issuer text
    title_of_class: str              # e.g. "COM", "CL A"
    value_usd: float                 # market value in WHOLE dollars (normalized)
    shares: float                    # sshPrnamt
    shares_or_principal_type: str    # "SH" (shares) or "PRN" (principal amount)
    investment_discretion: str       # "SOLE" / "DFND" / "OTR"


@dataclass
class Form13FFiling:
    """A parsed 13F-HR information table for one (manager, quarter)."""
    accession_number: str
    filing_date: str
    period_of_report: str            # derived from filing_date (see _quarter_end_for_filing_date)
    manager_cik: str                 # 10-digit zero-padded
    manager_name: str
    holdings: list[Form13FHolding] = field(default_factory=list)


@dataclass
class FundPosition:
    """One fund's position in one ticker — output of per-stock aggregation."""
    manager_cik: str
    manager_name: str
    shares: float
    value_usd: float
    title_of_class: str


@dataclass
class InstitutionalOwnership:
    """Compressed per-stock institutional view for orchestrator prompts.

    The view is bounded by the curated tracked-fund list — coverage of
    smaller funds is intentionally absent. Best read as 'among the funds
    we watch, how does ownership look this quarter vs last.'"""
    ticker: str
    issuer_match: str                # substring used for name matching
    period: str                      # period_of_report — current quarter
    prior_period: Optional[str]      # period_of_report — prior quarter
    funds_tracked: int               # |tracked_funds|
    funds_holding: int               # count holding this quarter
    funds_holding_prior: int         # count holding prior quarter
    new_positions: int               # initiated this quarter
    exited_positions: int            # closed this quarter
    total_shares: float
    total_value_usd: float
    positions: list[FundPosition]    # sorted by value_usd desc

    def stage_2_line(self) -> str:
        """One/two-line compressed view for Stage 2 prompt.

        Example:
          "8 of 20 tracked funds hold this quarter; +2 new, -1 exited; "
          "total $4.2B / 32M shares. top: BlackRock $1.8B, Vanguard $1.4B."
        """
        delta = ""
        if self.prior_period is not None:
            delta = f"; +{self.new_positions} new, -{self.exited_positions} exited"
        head = (
            f"{self.funds_holding} of {self.funds_tracked} tracked funds "
            f"hold this quarter{delta}; total {_fmt_dollars(self.total_value_usd)} "
            f"/ {_fmt_shares(self.total_shares)} shares"
        )
        top = self.positions[:3]
        if top:
            tail = "; top: " + ", ".join(
                f"{p.manager_name} {_fmt_dollars(p.value_usd)}" for p in top
            )
            return head + tail + "."
        return head + "."

    @classmethod
    def render_for_prompt(cls, ownership: Optional["InstitutionalOwnership"]) -> str:
        """Three-state prompt rendering: None / no-positions / populated.
        Mirrors `InsiderActivitySummary.render_for_prompt`."""
        if ownership is None:
            return (
                "(institutional ownership unavailable — no tracked-fund "
                "13F coverage)"
            )
        if ownership.funds_holding == 0 and ownership.funds_holding_prior == 0:
            return (
                f"(no tracked-fund 13F positions in {ownership.ticker} as "
                f"of {ownership.period or 'last quarter'})"
            )
        return ownership.stage_2_line()


def _fmt_dollars(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.0f}"


def _fmt_shares(n: float) -> str:
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.1f}B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}M"
    if a >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


# ---------------------------------------------------------------------------
# Period inference
# ---------------------------------------------------------------------------

def _quarter_end_for_filing_date(filing_date: str) -> str:
    """Derive a 13F-HR's `period_of_report` from its filing date.

    13F-HRs are due 45 days after quarter end. We subtract 45 days from
    the filing date to land in the reporting quarter, then snap to that
    quarter's last day. Deterministic and avoids a second HTTP fetch
    (cover form) per filing.
    """
    try:
        d = date.fromisoformat(filing_date)
    except ValueError:
        return ""
    in_quarter = d - timedelta(days=45)
    year = in_quarter.year
    month = in_quarter.month
    if month <= 3:
        return f"{year}-03-31"
    if month <= 6:
        return f"{year}-06-30"
    if month <= 9:
        return f"{year}-09-30"
    return f"{year}-12-31"


def _value_multiplier(period_of_report: str) -> int:
    """Pre-2023 13F filings report `value` in $thousands; 2023 onward in
    whole dollars. We multiply by 1000 for the pre-2023 schema so the
    Form13FHolding.value_usd field is uniformly in whole dollars."""
    if not period_of_report:
        return 1
    return 1000 if period_of_report < "2023-01-01" else 1


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def parse_13f(
    xml_text: str,
    *,
    accession_number: str,
    filing_date: str,
    manager_cik: str,
    manager_name: str,
) -> Form13FFiling:
    """Parse a 13F-HR infotable.xml into a structured Form13FFiling.

    The SEC schema is namespaced
    (`http://www.sec.gov/edgar/document/thirteenf/informationtable`);
    we strip namespaces so findall paths don't need a prefix.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(
            f"13F XML parse failed for {accession_number}: {exc}"
        ) from exc

    _strip_namespace(root)

    period = _quarter_end_for_filing_date(filing_date)
    mult = _value_multiplier(period)

    holdings: list[Form13FHolding] = []
    for entry in root.findall("infoTable"):
        shrs = entry.find("shrsOrPrnAmt")
        holdings.append(Form13FHolding(
            cusip=_text(entry, "cusip") or "",
            issuer_name=_text(entry, "nameOfIssuer") or "",
            title_of_class=_text(entry, "titleOfClass") or "",
            value_usd=_float(entry, "value") * mult,
            shares=_float(shrs, "sshPrnamt") if shrs is not None else 0.0,
            shares_or_principal_type=(_text(shrs, "sshPrnamtType") if shrs is not None else "") or "",
            investment_discretion=_text(entry, "investmentDiscretion") or "",
        ))

    return Form13FFiling(
        accession_number=accession_number,
        filing_date=filing_date,
        period_of_report=period,
        manager_cik=manager_cik.zfill(10) if manager_cik else "",
        manager_name=manager_name,
        holdings=holdings,
    )


def _strip_namespace(root: ET.Element) -> None:
    """In-place: rewrite `{ns}tag` element names to bare `tag`."""
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(parent: Optional[ET.Element], path: str) -> Optional[str]:
    if parent is None:
        return None
    el = parent.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _float(parent: Optional[ET.Element], path: str, default: float = 0.0) -> float:
    txt = _text(parent, path)
    if txt is None:
        return default
    try:
        return float(txt)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Per-stock aggregation
# ---------------------------------------------------------------------------

def aggregate_institutional_ownership(
    current_filings: list[Form13FFiling],
    prior_filings: list[Form13FFiling],
    *,
    ticker: str,
    issuer_match: str,
    funds_tracked: Optional[int] = None,
) -> InstitutionalOwnership:
    """Flip per-fund holdings into a per-stock view for `ticker`.

    Matches holdings by case-insensitive substring on
    `Form13FHolding.issuer_name`. A fund counts as "holding" if any of
    its holdings match and have non-zero shares.

    Diff against `prior_filings` (matched by manager_cik) yields
    new_positions / exited_positions / funds_holding_prior.

    `funds_tracked` should be the caller's universe size (e.g.
    `len(tracked_funds)`) so the Stage 2 summary line can render
    "8 of 20" coverage rather than "8 of 8" (which would collapse the
    denominator to the numerator after per-fund filtering). Falls back
    to the larger of the two filing list lengths when omitted —
    preserves backward-compat for callers that don't yet pass it.
    """
    needle = issuer_match.lower().strip()
    period = current_filings[0].period_of_report if current_filings else ""
    prior_period = prior_filings[0].period_of_report if prior_filings else None

    def _fund_positions(filing: Form13FFiling) -> list[FundPosition]:
        out: list[FundPosition] = []
        for h in filing.holdings:
            if needle and needle not in h.issuer_name.lower():
                continue
            if h.shares <= 0:
                continue
            out.append(FundPosition(
                manager_cik=filing.manager_cik,
                manager_name=filing.manager_name,
                shares=h.shares,
                value_usd=h.value_usd,
                title_of_class=h.title_of_class,
            ))
        return out

    current_by_cik: dict[str, list[FundPosition]] = {}
    for f in current_filings:
        pos = _fund_positions(f)
        if pos:
            current_by_cik[f.manager_cik] = pos

    prior_by_cik: dict[str, list[FundPosition]] = {}
    for f in prior_filings:
        pos = _fund_positions(f)
        if pos:
            prior_by_cik[f.manager_cik] = pos

    current_holders = set(current_by_cik.keys())
    prior_holders = set(prior_by_cik.keys())
    new_positions = len(current_holders - prior_holders)
    exited_positions = len(prior_holders - current_holders)

    # Flatten current_by_cik into a positions list (combine multiple
    # class-of-stock entries for the same manager into one row per
    # manager, summed)
    consolidated: list[FundPosition] = []
    for cik, positions in current_by_cik.items():
        total_shares = sum(p.shares for p in positions)
        total_value = sum(p.value_usd for p in positions)
        # Use the first position's manager_name / title_of_class as the label
        consolidated.append(FundPosition(
            manager_cik=cik,
            manager_name=positions[0].manager_name,
            shares=total_shares,
            value_usd=total_value,
            title_of_class=positions[0].title_of_class,
        ))
    consolidated.sort(key=lambda p: -p.value_usd)

    return InstitutionalOwnership(
        ticker=ticker.upper(),
        issuer_match=issuer_match,
        period=period,
        prior_period=prior_period,
        funds_tracked=(
            funds_tracked if funds_tracked is not None
            else max(len(current_filings), len(prior_filings))
        ),
        funds_holding=len(current_holders),
        funds_holding_prior=len(prior_holders),
        new_positions=new_positions,
        exited_positions=exited_positions,
        total_shares=sum(p.shares for p in consolidated),
        total_value_usd=sum(p.value_usd for p in consolidated),
        positions=consolidated,
    )


# ---------------------------------------------------------------------------
# High-level loader
# ---------------------------------------------------------------------------

INFOTABLE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/infotable.xml"
)


def _infotable_url(filing: Filing) -> str:
    """The deterministic infotable.xml URL for a 13F filing.

    Modern (2014+) 13F-HR filings standardize on `infotable.xml` as the
    holdings document. Pre-2014 filings used varying names — out of scope
    for v1 (operator-curated funds are all modern filers)."""
    accession_no_dashes = filing.accession_number.replace("-", "")
    cik_no_zeros = filing.cik.lstrip("0") or "0"
    return INFOTABLE_URL.format(
        cik=cik_no_zeros, accession_no_dashes=accession_no_dashes,
    )


async def fetch_13f(
    client: EdgarClient,
    filing: Filing,
    *,
    manager_name: str = "",
) -> Form13FFiling:
    """Fetch + parse one 13F-HR's information table.

    Symmetric with `form4.fetch_form4` — both are free functions taking
    a client, keeping `EdgarClient` itself free of form-type knowledge."""
    if filing.form_type not in ("13F-HR", "13F-NT"):
        raise ValueError(
            f"fetch_13f requires form_type='13F-HR' or '13F-NT', got "
            f"{filing.form_type!r}"
        )
    url = _infotable_url(filing)
    response = await client.get(url)
    return parse_13f(
        response.text,
        accession_number=filing.accession_number,
        filing_date=filing.filing_date,
        manager_cik=filing.cik,
        manager_name=manager_name,
    )


_PRIOR_QUARTER_MIN_DAYS = 60
_PRIOR_QUARTER_MAX_DAYS = 110


async def _load_fund_last_two_quarters(
    client: EdgarClient,
    fund: TrackedFund,
) -> tuple[Optional[Form13FFiling], Optional[Form13FFiling]]:
    """Fetch a fund's two most-recent 13F-HRs. Returns (current, prior).
    Either may be None on graceful-degrade failure.

    `prior` is dropped if its period_of_report is not 60-110 days before
    `current`'s. A filer who skipped a quarter would otherwise produce
    phantom new/exited counters when the next-most-recent 13F is 4+
    quarters back."""
    try:
        filings = await client.list_filings(fund.cik, "13F-HR", limit=2)
    except Exception as exc:
        log.warning("EDGAR: 13F list failed for %s (%s): %s",
                    fund.name, fund.cik, exc)
        return None, None
    if not filings:
        return None, None

    async def _safe_fetch(filing: Filing) -> Optional[Form13FFiling]:
        try:
            return await fetch_13f(client, filing, manager_name=fund.name)
        except Exception as exc:
            log.warning("EDGAR: 13F fetch failed for %s (%s): %s",
                        fund.name, filing.accession_number, exc)
            return None

    if len(filings) == 1:
        current = await _safe_fetch(filings[0])
        return current, None
    current, prior = await asyncio.gather(
        _safe_fetch(filings[0]), _safe_fetch(filings[1]),
    )
    if current is not None and prior is not None:
        try:
            cur_d = date.fromisoformat(current.period_of_report)
            prior_d = date.fromisoformat(prior.period_of_report)
            days_apart = (cur_d - prior_d).days
            if not (_PRIOR_QUARTER_MIN_DAYS <= days_apart <= _PRIOR_QUARTER_MAX_DAYS):
                log.info(
                    "EDGAR 13F: %s prior period is %dd from current "
                    "(expected ~91); dropping to avoid phantom deltas",
                    fund.name, days_apart,
                )
                prior = None
        except ValueError:
            prior = None
    return current, prior


async def _resolve_issuer_match(
    client: EdgarClient,
    ticker: str,
) -> Optional[str]:
    """Derive an issuer-name substring for `ticker` from SEC's submissions
    JSON. Used as a fuzzy match against 13F infotable issuer_name fields
    when the caller doesn't supply an explicit `issuer_match`."""
    cik = await client.resolve_cik(ticker)
    if cik is None:
        return None
    try:
        from research_assistant.edgar.client import SUBMISSIONS_URL
        response = await client.get(SUBMISSIONS_URL.format(cik=cik))
        payload = response.json()
        name = payload.get("name") or ""
        # 13F infotables use uppercase issuer names; case-insensitive match
        # works regardless. Strip common corporate suffixes for a tighter
        # substring (issuer might be "NVIDIA CORP" while SEC name is
        # "NVIDIA CORPORATION"). Order longest-first so " INC." is checked
        # before " INC" — otherwise "APPLE INC." → "APPLE." (with stray
        # period) and the substring match against 13F text "APPLE INC"
        # silently misses.
        for suffix in (
            ", INC.", " CORPORATION", " INC.", " CORP", " INC", " LTD", " PLC",
        ):
            if name.upper().endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name.strip() or None
    except Exception as exc:
        log.warning("EDGAR: issuer-name resolve failed for %s: %s", ticker, exc)
        return None


async def load_institutional_ownership(
    ticker: str,
    *,
    tracked_funds: tuple[TrackedFund, ...] = DEFAULT_TRACKED_FUNDS,
    issuer_match: Optional[str] = None,
    client: Optional[EdgarClient] = None,
) -> Optional[InstitutionalOwnership]:
    """Fetch latest 2 quarters of 13F-HRs from each tracked fund and
    aggregate into a per-stock view for `ticker`.

    Args:
        ticker: target ticker (case-insensitive).
        tracked_funds: curated CIKs to query (default DEFAULT_TRACKED_FUNDS).
        issuer_match: substring to match against 13F issuer_name.
            When None, derived from SEC submissions.json's company name
            (stripped of common corporate suffixes).
        client: reuse an existing EdgarClient when called inside a loop;
            otherwise a one-shot client is created and closed.

    Returns None when:
        - issuer_match can't be derived (ticker not in SEC universe), OR
        - no tracked fund returned any holdings (all fetches failed).
    """
    owns_client = client is None
    if client is None:
        client = EdgarClient()
    try:
        match = issuer_match or await _resolve_issuer_match(client, ticker)
        if not match:
            log.info("EDGAR 13F: no issuer match for %s", ticker)
            return None

        per_fund = await asyncio.gather(
            *[_load_fund_last_two_quarters(client, fund) for fund in tracked_funds],
        )
        current_filings: list[Form13FFiling] = []
        prior_filings: list[Form13FFiling] = []
        for current, prior in per_fund:
            if current is not None:
                current_filings.append(current)
            if prior is not None:
                prior_filings.append(prior)

        if not current_filings and not prior_filings:
            log.info("EDGAR 13F: no usable filings for %s across %d tracked funds",
                     ticker, len(tracked_funds))
            return None

        return aggregate_institutional_ownership(
            current_filings, prior_filings,
            ticker=ticker, issuer_match=match,
            funds_tracked=len(tracked_funds),
        )
    finally:
        if owns_client:
            await client.close()
