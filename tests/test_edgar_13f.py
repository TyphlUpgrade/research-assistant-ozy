"""
Tests for the 13F institutional-holdings layer (FOLLOWUPS #5).

Covers:
- parse_13f: namespace stripping, SH vs PRN, value scaling pre-/post-2023.
- _quarter_end_for_filing_date / _value_multiplier helpers.
- aggregate_institutional_ownership: per-stock flip, new/exited diff,
  sorting by value, consolidation of multi-class entries per manager.
- InstitutionalOwnership.stage_2_line rendering.
- load_institutional_ownership end-to-end with MockTransport: per-fund
  index resolution, issuer_match auto-derivation from submissions JSON,
  graceful degrade on per-fund failure.
"""
from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from research_assistant.edgar import (
    DEFAULT_TRACKED_FUNDS,
    EdgarClient,
    Form13FFiling,
    Form13FHolding,
    FundPosition,
    InstitutionalOwnership,
    TrackedFund,
    aggregate_institutional_ownership,
    load_institutional_ownership,
    parse_13f,
)
from research_assistant.edgar.form13f import (
    _load_fund_last_two_quarters,
    _quarter_end_for_filing_date,
    _resolve_issuer_match,
    _value_multiplier,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_INFOTABLE_NVDA_AAPL_2026Q1 = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip>
    <value>1800000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>12000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>2500000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>13000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""

_INFOTABLE_NVDA_VANGUARD_2026Q1 = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip>
    <value>1400000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>9000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""

_INFOTABLE_PRE_2023_THOUSANDS = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip>
    <value>1800000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>12000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""

_INFOTABLE_PRINCIPAL_AMOUNT = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>UST BOND 4.5</nameOfIssuer>
    <titleOfClass>NOTE</titleOfClass>
    <cusip>912828ZZ7</cusip>
    <value>50000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>50000000</sshPrnamt>
      <sshPrnamtType>PRN</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""


def _make_handler(routes: dict[str, tuple[int, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(body, (dict, list)):
                    return httpx.Response(status, json=body, request=request)
                return httpx.Response(status, text=body, request=request)
        return httpx.Response(404, text=f"unmatched url {request.url}", request=request)
    return handler


# ---------------------------------------------------------------------------
# Period inference + value scaling helpers
# ---------------------------------------------------------------------------

def test_quarter_end_for_filing_date() -> None:
    """13F-HRs are due 45 days after quarter end; filing_date → quarter."""
    assert _quarter_end_for_filing_date("2026-05-15") == "2026-03-31"
    assert _quarter_end_for_filing_date("2026-08-14") == "2026-06-30"
    assert _quarter_end_for_filing_date("2026-11-14") == "2026-09-30"
    assert _quarter_end_for_filing_date("2027-02-14") == "2026-12-31"


def test_quarter_end_for_invalid_date_returns_empty() -> None:
    assert _quarter_end_for_filing_date("not-a-date") == ""
    assert _quarter_end_for_filing_date("") == ""


def test_value_multiplier_pre_2023_is_thousands() -> None:
    assert _value_multiplier("2022-12-31") == 1000
    assert _value_multiplier("2020-06-30") == 1000


def test_value_multiplier_post_2023_is_whole_dollars() -> None:
    assert _value_multiplier("2023-03-31") == 1
    assert _value_multiplier("2026-06-30") == 1


# ---------------------------------------------------------------------------
# parse_13f
# ---------------------------------------------------------------------------

def test_parse_13f_strips_namespace_and_extracts_holdings() -> None:
    f = parse_13f(
        _INFOTABLE_NVDA_AAPL_2026Q1,
        accession_number="0001364742-26-000005",
        filing_date="2026-05-15",
        manager_cik="1364742",
        manager_name="BlackRock",
    )
    assert f.manager_cik == "0001364742"
    assert f.manager_name == "BlackRock"
    assert f.period_of_report == "2026-03-31"
    assert len(f.holdings) == 2
    nvda, aapl = f.holdings
    assert nvda.issuer_name == "NVIDIA CORP"
    assert nvda.cusip == "67066G104"
    assert nvda.value_usd == 1_800_000_000
    assert nvda.shares == 12_000_000
    assert nvda.shares_or_principal_type == "SH"
    assert nvda.investment_discretion == "SOLE"
    assert aapl.issuer_name == "APPLE INC"


def test_parse_13f_pre_2023_scales_value_to_dollars() -> None:
    """Pre-2023 schema reported value in $thousands. Parser normalizes."""
    f = parse_13f(
        _INFOTABLE_PRE_2023_THOUSANDS,
        accession_number="A1",
        filing_date="2022-05-15",   # → period 2022-03-31, multiplier 1000
        manager_cik="1364742",
        manager_name="BlackRock",
    )
    assert f.period_of_report == "2022-03-31"
    # 1_800_000 in source × 1000 = 1.8B dollars (matches post-2023 schema)
    assert f.holdings[0].value_usd == 1_800_000_000


def test_parse_13f_handles_principal_amount_type() -> None:
    """sshPrnamtType=PRN means a bond/note position, not common stock."""
    f = parse_13f(
        _INFOTABLE_PRINCIPAL_AMOUNT,
        accession_number="A1",
        filing_date="2026-05-15",
        manager_cik="1364742",
        manager_name="BlackRock",
    )
    [holding] = f.holdings
    assert holding.shares_or_principal_type == "PRN"
    assert holding.shares == 50_000_000


def test_parse_13f_invalid_xml_raises_value_error() -> None:
    with pytest.raises(ValueError, match="13F XML parse failed"):
        parse_13f(
            "not valid xml <>",
            accession_number="A", filing_date="2026-05-15",
            manager_cik="1", manager_name="Test",
        )


def test_parse_13f_empty_table() -> None:
    xml = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
</informationTable>"""
    f = parse_13f(
        xml, accession_number="A", filing_date="2026-05-15",
        manager_cik="1", manager_name="Test",
    )
    assert f.holdings == []


# ---------------------------------------------------------------------------
# aggregate_institutional_ownership
# ---------------------------------------------------------------------------

def _make_filing(
    *, manager_cik: str, manager_name: str, period: str,
    holdings: list[Form13FHolding],
) -> Form13FFiling:
    return Form13FFiling(
        accession_number=f"A-{manager_cik}-{period}",
        filing_date="2026-05-15", period_of_report=period,
        manager_cik=manager_cik, manager_name=manager_name,
        holdings=holdings,
    )


def _nvda_holding(*, shares: float = 1_000_000, value: float = 150_000_000) -> Form13FHolding:
    return Form13FHolding(
        cusip="67066G104", issuer_name="NVIDIA CORP", title_of_class="COM",
        value_usd=value, shares=shares, shares_or_principal_type="SH",
        investment_discretion="SOLE",
    )


def _aapl_holding(*, shares: float = 1_000_000, value: float = 200_000_000) -> Form13FHolding:
    return Form13FHolding(
        cusip="037833100", issuer_name="APPLE INC", title_of_class="COM",
        value_usd=value, shares=shares, shares_or_principal_type="SH",
        investment_discretion="SOLE",
    )


def test_aggregate_basic_per_stock_flip() -> None:
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31",
                     holdings=[_nvda_holding(shares=12e6, value=1.8e9), _aapl_holding()]),
        _make_filing(manager_cik="02", manager_name="Vanguard",
                     period="2026-03-31",
                     holdings=[_nvda_holding(shares=9e6, value=1.4e9)]),
    ]
    prior = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2025-12-31",
                     holdings=[_nvda_holding(shares=11e6, value=1.5e9)]),
        _make_filing(manager_cik="02", manager_name="Vanguard",
                     period="2025-12-31",
                     holdings=[_nvda_holding(shares=9e6, value=1.3e9)]),
    ]
    s = aggregate_institutional_ownership(
        current, prior, ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.ticker == "NVDA"
    assert s.period == "2026-03-31"
    assert s.prior_period == "2025-12-31"
    assert s.funds_holding == 2
    assert s.funds_holding_prior == 2
    assert s.new_positions == 0
    assert s.exited_positions == 0
    assert s.total_shares == 21e6
    assert s.total_value_usd == pytest.approx(3.2e9)
    # Sorted by value desc
    assert s.positions[0].manager_name == "BlackRock"
    assert s.positions[1].manager_name == "Vanguard"


def test_aggregate_counts_new_and_exited_positions() -> None:
    """A fund that newly initiated and another that exited should each
    flow into their respective counters."""
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31", holdings=[_nvda_holding()]),
        _make_filing(manager_cik="03", manager_name="State Street",
                     period="2026-03-31", holdings=[_nvda_holding()]),  # NEW
    ]
    prior = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2025-12-31", holdings=[_nvda_holding()]),
        _make_filing(manager_cik="02", manager_name="Vanguard",
                     period="2025-12-31", holdings=[_nvda_holding()]),  # EXITED
    ]
    s = aggregate_institutional_ownership(
        current, prior, ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.new_positions == 1
    assert s.exited_positions == 1


def test_aggregate_filters_by_issuer_match() -> None:
    """Holdings not matching the issuer substring are ignored."""
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31",
                     holdings=[_nvda_holding(), _aapl_holding()]),
    ]
    s = aggregate_institutional_ownership(
        [], [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_holding == 0  # no current filings
    s = aggregate_institutional_ownership(
        current, [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_holding == 1
    assert s.total_value_usd == 150_000_000  # only NVDA, not AAPL


def test_aggregate_excludes_zero_share_holdings() -> None:
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31",
                     holdings=[_nvda_holding(shares=0, value=0)]),
    ]
    s = aggregate_institutional_ownership(
        current, [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_holding == 0


def test_aggregate_consolidates_multi_class_entries() -> None:
    """A manager holding both COM and CL A of the same issuer should
    surface as one consolidated position."""
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31", holdings=[
            Form13FHolding(cusip="67066G104", issuer_name="NVIDIA CORP",
                           title_of_class="COM", value_usd=1e9, shares=6e6,
                           shares_or_principal_type="SH",
                           investment_discretion="SOLE"),
            Form13FHolding(cusip="67066G203", issuer_name="NVIDIA CORP",
                           title_of_class="CL A", value_usd=8e8, shares=4e6,
                           shares_or_principal_type="SH",
                           investment_discretion="SOLE"),
        ]),
    ]
    s = aggregate_institutional_ownership(
        current, [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_holding == 1
    assert s.positions[0].shares == 10e6
    assert s.positions[0].value_usd == pytest.approx(1.8e9)


def test_aggregate_empty_inputs_returns_zero_summary() -> None:
    s = aggregate_institutional_ownership(
        [], [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_holding == 0
    assert s.funds_holding_prior == 0
    assert s.total_value_usd == 0


# ---------------------------------------------------------------------------
# stage_2_line rendering
# ---------------------------------------------------------------------------

def test_stage_2_line_with_top_positions() -> None:
    s = InstitutionalOwnership(
        ticker="NVDA", issuer_match="NVIDIA",
        period="2026-03-31", prior_period="2025-12-31",
        funds_tracked=5, funds_holding=2, funds_holding_prior=1,
        new_positions=1, exited_positions=0,
        total_shares=21e6, total_value_usd=3.2e9,
        positions=[
            FundPosition(manager_cik="01", manager_name="BlackRock",
                         shares=12e6, value_usd=1.8e9, title_of_class="COM"),
            FundPosition(manager_cik="02", manager_name="Vanguard",
                         shares=9e6, value_usd=1.4e9, title_of_class="COM"),
        ],
    )
    line = s.stage_2_line()
    assert "2 of 5 tracked funds hold" in line
    assert "+1 new, -0 exited" in line
    assert "$3.2B" in line
    assert "21.0M shares" in line
    assert "BlackRock $1.8B" in line
    assert "Vanguard $1.4B" in line


def test_stage_2_line_without_prior_omits_delta() -> None:
    s = InstitutionalOwnership(
        ticker="NVDA", issuer_match="NVIDIA",
        period="2026-03-31", prior_period=None,
        funds_tracked=5, funds_holding=2, funds_holding_prior=0,
        new_positions=0, exited_positions=0,
        total_shares=21e6, total_value_usd=3.2e9,
        positions=[],
    )
    line = s.stage_2_line()
    assert "+0 new" not in line
    assert "exited" not in line


# ---------------------------------------------------------------------------
# load_institutional_ownership end-to-end
# ---------------------------------------------------------------------------

_TICKER_INDEX = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Corp"},
}

_NVDA_SUBMISSIONS = {
    "cik": "0001045810",
    "name": "NVIDIA Corporation",
    "filings": {"recent": {"accessionNumber": [], "form": [],
                            "filingDate": [], "primaryDocument": []}},
}

_BLACKROCK_SUBMISSIONS = {
    "cik": "0001364742",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0001364742-26-000005",
                "0001364742-25-000080",
            ],
            "form": ["13F-HR", "13F-HR"],
            "filingDate": ["2026-05-15", "2026-02-14"],
            "primaryDocument": ["primary_doc.xml", "primary_doc.xml"],
        }
    },
}

_VANGUARD_SUBMISSIONS = {
    "cik": "0000102909",
    "filings": {
        "recent": {
            "accessionNumber": ["0000102909-26-000003"],
            "form": ["13F-HR"],
            "filingDate": ["2026-05-15"],
            "primaryDocument": ["primary_doc.xml"],
        }
    },
}

# Prior-quarter (2025-12-31): BlackRock held NVDA, Vanguard didn't file
_INFOTABLE_BLACKROCK_2025Q4 = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip>
    <value>1500000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>11000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""


def _archive_url(cik: str, accession: str) -> str:
    cik_no_zeros = cik.lstrip("0") or "0"
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
        f"{acc_no_dashes}/infotable.xml"
    )


@pytest.mark.asyncio
async def test_resolve_issuer_match_strips_inc_dot_cleanly() -> None:
    """Regression: suffix-strip order put ' INC' before ' INC.' so
    'APPLE INC.' previously became 'APPLE.' (trailing period broke the
    substring match against 13F infotables that print 'APPLE INC').
    Verified directly against _resolve_issuer_match to avoid the
    loader's `no tracked_funds` short-circuit."""
    aapl_submissions = {
        "cik": "0000320193", "name": "Apple Inc.",
        "filings": {"recent": {"accessionNumber": [], "form": [],
                                "filingDate": [], "primaryDocument": []}},
    }
    aapl_index = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, aapl_index),
        "https://data.sec.gov/submissions/CIK0000320193.json": (200, aapl_submissions),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        match = await _resolve_issuer_match(client, "AAPL")
    # Strip lowercases to "apple inc." → ", inc." doesn't match (no comma
    # before " inc."), then " inc." matches → strip leaves "apple" (or
    # "Apple" preserving case). Critically: NO trailing period.
    assert match is not None
    assert not match.endswith(".")
    assert match.lower() == "apple"


@pytest.mark.asyncio
async def test_resolve_issuer_match_strips_corp_suffixes() -> None:
    """Common corporate suffixes all strip cleanly."""
    submissions_cases = [
        ("NVIDIA CORPORATION", "NVIDIA"),
        ("NVIDIA CORP", "NVIDIA"),
        ("APPLE INC.", "APPLE"),
        ("APPLE INC", "APPLE"),
        ("ARM HOLDINGS PLC", "ARM HOLDINGS"),
        ("UNILEVER PLC", "UNILEVER"),
    ]
    for name, expected in submissions_cases:
        idx = {"0": {"cik_str": 1, "ticker": "X", "title": name}}
        subs = {
            "cik": "0000000001", "name": name,
            "filings": {"recent": {"accessionNumber": [], "form": [],
                                    "filingDate": [], "primaryDocument": []}},
        }
        routes = {
            "https://www.sec.gov/files/company_tickers.json": (200, idx),
            "https://data.sec.gov/submissions/CIK0000000001.json": (200, subs),
        }
        handler = _make_handler(routes)
        async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
            match = await _resolve_issuer_match(client, "X")
        assert match == expected, f"{name!r} → {match!r}, expected {expected!r}"


def test_aggregate_funds_tracked_uses_passed_denominator() -> None:
    """Regression: aggregate previously collapsed funds_tracked to
    max(len(current), len(prior)) — after per-fund filtering this
    became "N of N" instead of "N of universe_size"."""
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31", holdings=[_nvda_holding()]),
    ]
    s = aggregate_institutional_ownership(
        current, [], ticker="NVDA", issuer_match="NVIDIA",
        funds_tracked=20,
    )
    assert s.funds_tracked == 20
    assert s.funds_holding == 1
    assert "1 of 20 tracked funds" in s.stage_2_line()


def test_aggregate_funds_tracked_default_back_compat() -> None:
    """Existing callers that don't pass funds_tracked keep the old
    max(current, prior) heuristic — preserves backward compat."""
    current = [
        _make_filing(manager_cik="01", manager_name="BlackRock",
                     period="2026-03-31", holdings=[_nvda_holding()]),
    ]
    s = aggregate_institutional_ownership(
        current, [], ticker="NVDA", issuer_match="NVIDIA",
    )
    assert s.funds_tracked == 1


@pytest.mark.asyncio
async def test_load_institutional_ownership_end_to_end() -> None:
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS),
        "https://data.sec.gov/submissions/CIK0001364742.json": (200, _BLACKROCK_SUBMISSIONS),
        "https://data.sec.gov/submissions/CIK0000102909.json": (200, _VANGUARD_SUBMISSIONS),
        _archive_url("0001364742", "0001364742-26-000005"): (200, _INFOTABLE_NVDA_AAPL_2026Q1),
        _archive_url("0001364742", "0001364742-25-000080"): (200, _INFOTABLE_BLACKROCK_2025Q4),
        _archive_url("0000102909", "0000102909-26-000003"): (200, _INFOTABLE_NVDA_VANGUARD_2026Q1),
    }
    handler = _make_handler(routes)
    funds = (
        TrackedFund(cik="0001364742", name="BlackRock"),
        TrackedFund(cik="0000102909", name="Vanguard"),
    )
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        s = await load_institutional_ownership(
            "NVDA", tracked_funds=funds, client=client,
        )
    assert s is not None
    assert s.ticker == "NVDA"
    assert s.issuer_match == "NVIDIA"   # derived from submissions name, "CORPORATION" stripped
    assert s.funds_holding == 2          # BlackRock + Vanguard current quarter
    assert s.funds_holding_prior == 1    # BlackRock had a prior; Vanguard didn't
    assert s.new_positions == 1          # Vanguard initiated
    assert s.exited_positions == 0
    # Position order: BlackRock $1.8B > Vanguard $1.4B
    assert s.positions[0].manager_name == "BlackRock"
    assert s.positions[0].value_usd == 1.8e9


@pytest.mark.asyncio
async def test_load_institutional_ownership_unknown_ticker_returns_none() -> None:
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        s = await load_institutional_ownership(
            "BOGUS", tracked_funds=(TrackedFund(cik="0001", name="X"),),
            client=client,
        )
    assert s is None


@pytest.mark.asyncio
async def test_load_institutional_ownership_explicit_issuer_match_skips_resolution() -> None:
    """When caller supplies issuer_match, the ticker→submissions resolve
    step is skipped entirely (operator override path)."""
    routes = {
        "https://data.sec.gov/submissions/CIK0001364742.json": (200, _BLACKROCK_SUBMISSIONS),
        _archive_url("0001364742", "0001364742-26-000005"): (200, _INFOTABLE_NVDA_AAPL_2026Q1),
        _archive_url("0001364742", "0001364742-25-000080"): (200, _INFOTABLE_BLACKROCK_2025Q4),
    }
    handler = _make_handler(routes)
    funds = (TrackedFund(cik="0001364742", name="BlackRock"),)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        s = await load_institutional_ownership(
            "NVDA", tracked_funds=funds, issuer_match="NVIDIA",
            client=client,
        )
    assert s is not None
    assert s.issuer_match == "NVIDIA"


@pytest.mark.asyncio
async def test_load_institutional_ownership_per_fund_failure_graceful() -> None:
    """When one fund's submissions / infotable fetch fails (e.g. 404),
    other funds still contribute. Don't propagate the per-fund failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers.json"):
            return httpx.Response(200, json=_TICKER_INDEX, request=request)
        if url.startswith("https://data.sec.gov/submissions/CIK0001045810.json"):
            return httpx.Response(200, json=_NVDA_SUBMISSIONS, request=request)
        if url.startswith("https://data.sec.gov/submissions/CIK0001364742.json"):
            return httpx.Response(200, json=_BLACKROCK_SUBMISSIONS, request=request)
        if url.startswith("https://data.sec.gov/submissions/CIK0000102909.json"):
            return httpx.Response(500, text="server error", request=request)  # FAIL
        if url == _archive_url("0001364742", "0001364742-26-000005"):
            return httpx.Response(200, text=_INFOTABLE_NVDA_AAPL_2026Q1, request=request)
        if url == _archive_url("0001364742", "0001364742-25-000080"):
            return httpx.Response(200, text=_INFOTABLE_BLACKROCK_2025Q4, request=request)
        return httpx.Response(404, request=request)

    funds = (
        TrackedFund(cik="0001364742", name="BlackRock"),
        TrackedFund(cik="0000102909", name="Vanguard"),  # will fail
    )
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        s = await load_institutional_ownership("NVDA", tracked_funds=funds, client=client)
    assert s is not None
    assert s.funds_holding == 1   # only BlackRock survived


# ---------------------------------------------------------------------------
# Defaults sanity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_fund_last_two_quarters_drops_stale_prior() -> None:
    """A filer who skipped a quarter has a "second most recent" 13F that
    is 4+ quarters back. Without the freshness check, that ancient
    filing would become the comparison base, producing phantom
    new/exited counters. The guard drops priors > 110 days from
    current."""
    submissions = {
        "cik": "0001364742",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0001364742-26-000005",   # current: filed 2026-05-15 → Q1
                    "0001364742-25-000080",   # "prior": filed 2025-05-15 → Q1 2025 (4 quarters back)
                ],
                "form": ["13F-HR", "13F-HR"],
                "filingDate": ["2026-05-15", "2025-05-15"],
                "primaryDocument": ["primary_doc.xml", "primary_doc.xml"],
            }
        },
    }
    routes = {
        "https://data.sec.gov/submissions/CIK0001364742.json": (200, submissions),
        _archive_url("0001364742", "0001364742-26-000005"):
            (200, _INFOTABLE_NVDA_AAPL_2026Q1),
        _archive_url("0001364742", "0001364742-25-000080"):
            (200, _INFOTABLE_BLACKROCK_2025Q4),   # body irrelevant; the period date is
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        current, prior = await _load_fund_last_two_quarters(
            client, TrackedFund(cik="0001364742", name="BlackRock"),
        )
    assert current is not None
    # current period = 2026-03-31; "prior" period = 2025-03-31 = 365d apart → dropped
    assert prior is None


@pytest.mark.asyncio
async def test_load_fund_last_two_quarters_keeps_adjacent_prior() -> None:
    """Healthy filer with consecutive quarters: prior must be retained."""
    submissions = {
        "cik": "0001364742",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0001364742-26-000005",   # current: filed 2026-05-15 → Q1 2026
                    "0001364742-26-000001",   # prior: filed 2026-02-14 → Q4 2025 (~91d apart)
                ],
                "form": ["13F-HR", "13F-HR"],
                "filingDate": ["2026-05-15", "2026-02-14"],
                "primaryDocument": ["primary_doc.xml", "primary_doc.xml"],
            }
        },
    }
    routes = {
        "https://data.sec.gov/submissions/CIK0001364742.json": (200, submissions),
        _archive_url("0001364742", "0001364742-26-000005"):
            (200, _INFOTABLE_NVDA_AAPL_2026Q1),
        _archive_url("0001364742", "0001364742-26-000001"):
            (200, _INFOTABLE_BLACKROCK_2025Q4),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        current, prior = await _load_fund_last_two_quarters(
            client, TrackedFund(cik="0001364742", name="BlackRock"),
        )
    assert current is not None
    assert prior is not None


def test_default_tracked_funds_has_entries() -> None:
    """Starter list should be non-empty so the loader does something
    out of the box even without caller configuration."""
    assert len(DEFAULT_TRACKED_FUNDS) >= 3
    for fund in DEFAULT_TRACKED_FUNDS:
        assert len(fund.cik) == 10
        assert fund.cik.isdigit()
        assert fund.name
