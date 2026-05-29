"""
Tests for the EDGAR client adapter (FOLLOWUPS #1).

Covers:
- CIK resolver hit/miss + case-insensitivity + lazy single-fetch cache
- list_filings filters by form type, since-date, and limit
- Filing.archive_url builds the canonical SEC Archives path
- fetch_filing extracts paragraphs from HTML, drops script/style noise
- FilingText.anchor format matches the spec edgar:{form}:{accession}:para_{n}
- FilingText.search returns hits with anchors, respects max_hits
- _extract_paragraphs handles .txt and HTML
- Rate limiter throttles past the per-second ceiling, allows initial burst
- User-Agent header: default, env override, explicit override
- Async context manager closes the underlying httpx client
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

import httpx
import pytest

from datetime import date

from research_assistant.edgar import (
    DEFAULT_USER_AGENT,
    EdgarClient,
    Filing,
    FilingText,
    Form4Filing,
    Form4Owner,
    Form4Transaction,
    InsiderActivitySummary,
    OfficerActivity,
    aggregate_insider_activity,
    fetch_form4,
    load_insider_activities_batch,
    load_insider_activity,
    parse_form4,
)
# Underscore-prefixed internals are imported from impl modules directly
# rather than re-exported from the package — keeps the public surface
# honest about what's public.
from research_assistant.edgar.client import _extract_paragraphs, _RateLimiter
from research_assistant.edgar.form4 import _form4_primary_xml_url
from research_assistant.edgar.form4 import _fmt_dollars, _relationship_label


# ---------------------------------------------------------------------------
# Fixture bodies
# ---------------------------------------------------------------------------

_TICKER_INDEX_BODY: dict[str, dict[str, Any]] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "2": {"cik_str": 1108524, "ticker": "CRM", "title": "Salesforce, Inc."},
}

_NVDA_SUBMISSIONS_BODY: dict[str, Any] = {
    "cik": "0001045810",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0001045810-26-000045",
                "0001045810-26-000040",
                "0001045810-25-000200",
                "0001045810-25-000150",
            ],
            "form": ["10-K", "8-K", "10-Q", "10-K"],
            "filingDate": ["2026-04-15", "2026-04-01", "2026-02-15", "2025-04-12"],
            "primaryDocument": [
                "nvda-20260131.htm",
                "8k-20260401.htm",
                "nvda-10q-q1.htm",
                "nvda-10k-fy25.htm",
            ],
        }
    },
}

_NVDA_10K_HTML = """<html><body>
<p>NVIDIA Corporation is a leader in accelerated computing.</p>
<div><p>Revenue for fiscal 2026 totaled $130.5 billion, a 65% increase year-over-year.</p></div>
<p>Risk factors include geopolitical tensions and competition.</p>
<script>var spy = 1;</script>
<style>body { color: red; }</style>
</body></html>"""


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
# CIK resolver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_cik_returns_padded_cik() -> None:
    handler = _make_handler({
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        cik = await client.resolve_cik("NVDA")
    assert cik == "0001045810"


@pytest.mark.asyncio
async def test_resolve_cik_case_insensitive() -> None:
    handler = _make_handler({
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.resolve_cik("nvda") == "0001045810"
        assert await client.resolve_cik("aapl") == "0000320193"


@pytest.mark.asyncio
async def test_resolve_cik_unknown_returns_none() -> None:
    handler = _make_handler({
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.resolve_cik("BOGUS") is None


@pytest.mark.asyncio
async def test_cik_cache_is_lazy_and_reused() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        await client.resolve_cik("NVDA")
        await client.resolve_cik("AAPL")
        await client.resolve_cik("BOGUS")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# list_filings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_filings_filters_by_form_type() -> None:
    handler = _make_handler({
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        filings = await client.list_filings("1045810", "10-K", limit=5)
    assert [f.form_type for f in filings] == ["10-K", "10-K"]
    assert filings[0].accession_number == "0001045810-26-000045"
    assert filings[0].cik == "0001045810"
    assert filings[0].primary_document == "nvda-20260131.htm"


@pytest.mark.asyncio
async def test_list_filings_since_filter() -> None:
    handler = _make_handler({
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        filings = await client.list_filings("1045810", "10-K", since="2026-01-01")
    assert len(filings) == 1
    assert filings[0].accession_number == "0001045810-26-000045"


@pytest.mark.asyncio
async def test_list_filings_respects_limit() -> None:
    handler = _make_handler({
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        filings = await client.list_filings("1045810", "10-K", limit=1)
    assert len(filings) == 1
    assert filings[0].accession_number == "0001045810-26-000045"


@pytest.mark.asyncio
async def test_list_filings_other_form_type() -> None:
    handler = _make_handler({
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_BODY),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        eights = await client.list_filings("1045810", "8-K")
    assert len(eights) == 1
    assert eights[0].accession_number == "0001045810-26-000040"


# ---------------------------------------------------------------------------
# Filing.archive_url
# ---------------------------------------------------------------------------

def test_filing_archive_url() -> None:
    f = Filing(
        accession_number="0001045810-26-000045",
        form_type="10-K",
        filing_date="2026-04-15",
        cik="0001045810",
        primary_document="nvda-20260131.htm",
    )
    assert f.archive_url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581026000045/nvda-20260131.htm"
    )


# ---------------------------------------------------------------------------
# fetch_filing + paragraph extraction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_filing_extracts_paragraphs() -> None:
    f = Filing(
        accession_number="0001045810-26-000045",
        form_type="10-K",
        filing_date="2026-04-15",
        cik="0001045810",
        primary_document="nvda-10k.htm",
    )
    handler = _make_handler({f.archive_url: (200, _NVDA_10K_HTML)})
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        text = await client.fetch_filing(f)
    assert text.accession_number == "0001045810-26-000045"
    assert text.form_type == "10-K"
    body = " ".join(text.paragraphs)
    assert "NVIDIA Corporation is a leader" in body
    assert "$130.5 billion" in body
    assert "Risk factors" in body
    # Script and style content must be stripped
    assert "spy = 1" not in body
    assert "color: red" not in body


def test_extract_paragraphs_strips_script_style() -> None:
    html = """<html><body>
        <p>Real paragraph.</p>
        <script>var noise = 1;</script>
        <style>p { color: red; }</style>
    </body></html>"""
    paras = _extract_paragraphs(html, "anything.htm")
    body = " ".join(paras)
    assert "Real paragraph" in body
    assert "noise" not in body
    assert "color: red" not in body


def test_extract_paragraphs_plain_text() -> None:
    txt = "First paragraph here.\n\nSecond paragraph here.\n\n\nThird."
    paras = _extract_paragraphs(txt, "doc.txt")
    assert paras == ["First paragraph here.", "Second paragraph here.", "Third."]


def test_extract_paragraphs_collapses_whitespace() -> None:
    html = "<html><body><p>line one\n\n   with    extra\twhitespace</p></body></html>"
    [para] = _extract_paragraphs(html, "x.htm")
    assert para == "line one with extra whitespace"


# ---------------------------------------------------------------------------
# FilingText.anchor / search
# ---------------------------------------------------------------------------

def test_filing_text_anchor_format_matches_spec() -> None:
    """FOLLOWUPS #1 specifies edgar:8-K:0001234567-26-000045:para_17."""
    text = FilingText(
        accession_number="0001234567-26-000045",
        form_type="8-K",
        filing_date="2026-01-01",
        cik="0001234567",
        paragraphs=["p0"] * 20,
    )
    assert text.anchor(17) == "edgar:8-K:0001234567-26-000045:para_17"
    assert text.anchor(0) == "edgar:8-K:0001234567-26-000045:para_0"


def test_filing_text_search_returns_hits_with_anchors() -> None:
    text = FilingText(
        accession_number="X",
        form_type="10-K",
        filing_date="2026-01-01",
        cik="0001234567",
        paragraphs=[
            "Revenue for the year was strong.",
            "Operating margin expanded.",
            "Revenue mix shifted toward services.",
            "Free cash flow improved.",
        ],
    )
    hits = text.search("revenue")
    assert [a for a, _ in hits] == ["edgar:10-K:X:para_0", "edgar:10-K:X:para_2"]


def test_filing_text_search_respects_max_hits() -> None:
    text = FilingText(
        accession_number="X",
        form_type="10-K",
        filing_date="2026-01-01",
        cik="0001234567",
        paragraphs=["match"] * 20,
    )
    assert len(text.search("match", max_hits=3)) == 3


def test_filing_text_search_case_insensitive() -> None:
    text = FilingText(
        accession_number="X",
        form_type="10-K",
        filing_date="2026-01-01",
        cik="0001234567",
        paragraphs=["Revenue rose sharply."],
    )
    assert len(text.search("REVENUE")) == 1


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_initial_burst_immediate() -> None:
    rl = _RateLimiter(max_per_sec=5)
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        await rl.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_rate_limiter_throttles_over_capacity() -> None:
    """4 calls at 2/sec must take ≥ ~1s (window-sliding throttle)."""
    rl = _RateLimiter(max_per_sec=2)
    start = asyncio.get_event_loop().time()
    for _ in range(4):
        await rl.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.95


# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        await client.resolve_cik("NVDA")
    assert captured[0] == DEFAULT_USER_AGENT
    assert "william.a.sit@gmail.com" in captured[0]


@pytest.mark.asyncio
async def test_user_agent_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_USER_AGENT", "custom-tool ops@example.com")
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        await client.resolve_cik("NVDA")
    assert captured[0] == "custom-tool ops@example.com"


@pytest.mark.asyncio
async def test_user_agent_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_USER_AGENT", "env-value")
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)

    async with EdgarClient(
        transport=httpx.MockTransport(handler), user_agent="explicit-value"
    ) as client:
        await client.resolve_cik("NVDA")
    assert captured[0] == "explicit-value"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_closes_underlying_http() -> None:
    client = EdgarClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={}, request=r)
        ),
    )
    await client.close()
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_aexit_closes_underlying_http() -> None:
    async with EdgarClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={}, request=r)
        ),
    ) as client:
        pass
    assert client._http.is_closed


# ---------------------------------------------------------------------------
# Form 4 — FOLLOWUPS #3
# ---------------------------------------------------------------------------

_FORM4_NVDA_CEO_SALE = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-05-19</periodOfReport>
    <issuer>
        <issuerCik>0001045810</issuerCik>
        <issuerName>NVIDIA CORP</issuerName>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>1494730</rptOwnerCik>
            <rptOwnerName>HUANG JEN-HSUN</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>true</isDirector>
            <isOfficer>1</isOfficer>
            <officerTitle>President &amp; CEO</officerTitle>
            <isTenPercentOwner>0</isTenPercentOwner>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-05-19</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>120000</value></transactionShares>
                <transactionPricePerShare><value>150.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>800000</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""

_FORM4_NVDA_CFO_BUY_AND_GRANT = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-04-10</periodOfReport>
    <issuer>
        <issuerCik>0001045810</issuerCik>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>2000001</rptOwnerCik>
            <rptOwnerName>KRESS COLETTE</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>false</isDirector>
            <isOfficer>true</isOfficer>
            <officerTitle>EVP &amp; CFO</officerTitle>
            <isTenPercentOwner>false</isTenPercentOwner>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-04-10</value></transactionDate>
            <transactionCoding>
                <transactionCode>P</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>140.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-04-10</value></transactionDate>
            <transactionCoding>
                <transactionCode>A</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>5000</value></transactionShares>
                <transactionPricePerShare><value>0</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <derivativeTable>
        <derivativeTransaction>
            <securityTitle><value>Restricted Stock Unit</value></securityTitle>
            <transactionDate><value>2026-04-10</value></transactionDate>
            <transactionCoding>
                <transactionCode>M</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>2000</value></transactionShares>
                <transactionPricePerShare><value>0</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </derivativeTransaction>
    </derivativeTable>
</ownershipDocument>"""

_FORM4_OLD_PRE_WINDOW = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2025-01-15</periodOfReport>
    <issuer>
        <issuerCik>0001045810</issuerCik>
        <issuerTradingSymbol>NVDA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>3000001</rptOwnerCik>
            <rptOwnerName>OLD INSIDER</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>true</isDirector>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2025-01-15</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>999999</value></transactionShares>
                <transactionPricePerShare><value>100.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


# --- parser ----------------------------------------------------------------

def test_parse_form4_extracts_issuer_and_owner() -> None:
    f = parse_form4(
        _FORM4_NVDA_CEO_SALE,
        accession_number="0001045810-26-000045",
        filing_date="2026-05-19",
    )
    assert f.issuer_cik == "0001045810"
    assert f.issuer_ticker == "NVDA"
    assert f.period_of_report == "2026-05-19"
    [owner] = f.owners
    assert owner.cik == "0001494730"   # zero-padded to 10 digits
    assert owner.name == "HUANG JEN-HSUN"
    assert owner.is_director is True
    assert owner.is_officer is True
    assert owner.officer_title == "President & CEO"   # HTML entity decoded
    assert owner.is_ten_percent_owner is False


def test_parse_form4_extracts_single_transaction() -> None:
    f = parse_form4(
        _FORM4_NVDA_CEO_SALE,
        accession_number="0001045810-26-000045",
        filing_date="2026-05-19",
    )
    [t] = f.non_derivative
    assert t.date == "2026-05-19"
    assert t.code == "S"
    assert t.shares == 120_000
    assert t.price_per_share == 150.00
    assert t.acquired_disposed == "D"
    assert t.security_title == "Common Stock"
    assert t.post_transaction_shares == 800_000
    assert t.is_derivative is False


def test_parse_form4_net_dollars_signed() -> None:
    """Disposition (D) yields negative net_dollars; acquisition (A) positive.
    $0-price entries contribute $0 (no yfinance fallback per scope decision)."""
    sale = Form4Transaction(
        date="2026-05-19", code="S", shares=120_000, price_per_share=150.00,
        acquired_disposed="D", security_title="Common Stock",
    )
    assert sale.net_dollars == pytest.approx(-18_000_000)

    buy = Form4Transaction(
        date="2026-04-10", code="P", shares=10_000, price_per_share=140.00,
        acquired_disposed="A", security_title="Common Stock",
    )
    assert buy.net_dollars == pytest.approx(1_400_000)

    grant = Form4Transaction(
        date="2026-04-10", code="A", shares=5_000, price_per_share=0.0,
        acquired_disposed="A", security_title="Common Stock",
    )
    assert grant.net_dollars == 0.0


def test_parse_form4_handles_derivative_table() -> None:
    f = parse_form4(
        _FORM4_NVDA_CFO_BUY_AND_GRANT,
        accession_number="0001045810-26-000040",
        filing_date="2026-04-10",
    )
    assert len(f.non_derivative) == 2
    assert len(f.derivative) == 1
    deriv = f.derivative[0]
    assert deriv.code == "M"
    assert deriv.shares == 2000
    assert deriv.is_derivative is True


def test_parse_form4_handles_multiple_owners() -> None:
    """Joint Form 4 filings can list multiple reportingOwner blocks."""
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-05-01</periodOfReport>
    <issuer><issuerCik>1</issuerCik><issuerTradingSymbol>X</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>11</rptOwnerCik><rptOwnerName>FIRST</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isOfficer>true</isOfficer></reportingOwnerRelationship>
    </reportingOwner>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>22</rptOwnerCik><rptOwnerName>SECOND</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>true</isDirector></reportingOwnerRelationship>
    </reportingOwner>
</ownershipDocument>"""
    f = parse_form4(xml, accession_number="A", filing_date="2026-05-01")
    assert [o.name for o in f.owners] == ["FIRST", "SECOND"]
    # Aggregation attributes transactions to primary_owner
    assert f.primary_owner is not None
    assert f.primary_owner.name == "FIRST"


def test_parse_form4_invalid_xml_raises() -> None:
    with pytest.raises(ValueError, match="Form 4 XML parse failed"):
        parse_form4("not valid xml <>", accession_number="A", filing_date="2026-01-01")


def test_parse_form4_filing_with_no_transactions() -> None:
    """A Form 4 with no transactions (e.g. relationship-only update) still
    parses; non_derivative and derivative are empty lists."""
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-05-01</periodOfReport>
    <issuer><issuerCik>1</issuerCik><issuerTradingSymbol>X</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>1</rptOwnerCik><rptOwnerName>X</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>true</isDirector></reportingOwnerRelationship>
    </reportingOwner>
</ownershipDocument>"""
    f = parse_form4(xml, accession_number="A", filing_date="2026-05-01")
    assert f.non_derivative == []
    assert f.derivative == []


# --- fetch_form4 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_form4_parses_xml() -> None:
    filing = Filing(
        accession_number="0001045810-26-000045",
        form_type="4",
        filing_date="2026-05-19",
        cik="0001045810",
        primary_document="wf-form4_xxx.xml",
    )
    handler = _make_handler({
        _form4_primary_xml_url(filing): (200, _FORM4_NVDA_CEO_SALE),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        f = await fetch_form4(client, filing)
    assert f.issuer_ticker == "NVDA"
    assert f.primary_owner.officer_title == "President & CEO"


@pytest.mark.asyncio
async def test_fetch_form4_bypasses_xslt_display_url() -> None:
    """EDGAR returns primary_document='xslF345X06/ownership.xml' (the XSLT
    HTML-rendered view, which ET cannot parse). fetch_form4 must hit the
    plain '.../ownership.xml' URL instead."""
    filing = Filing(
        accession_number="0001045810-26-000045",
        form_type="4",
        filing_date="2026-05-19",
        cik="0001045810",
        primary_document="xslF345X06/ownership.xml",
    )
    plain_url = (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581026000045/ownership.xml"
    )
    xslt_url = (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581026000045/xslF345X06/ownership.xml"
    )
    # Plain XML at the canonical URL; HTML garbage at the XSLT URL. If the
    # fetcher hits the XSLT URL it gets unparseable HTML and raises.
    handler = _make_handler({
        plain_url: (200, _FORM4_NVDA_CEO_SALE),
        xslt_url: (200, "<html><head><style>x</style></head><body></body></html>"),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        f = await fetch_form4(client, filing)
    assert f.issuer_ticker == "NVDA"


@pytest.mark.asyncio
async def test_fetch_form4_bypasses_xslt_with_vendor_filename() -> None:
    """Some filers (e.g. T1 Energy) use EDGAR vendor names like
    'marketforms-73189.xml' instead of the standard 'ownership.xml'. The
    fetcher must strip only the 'xsl*/' prefix and preserve the filename,
    not hardcode 'ownership.xml'."""
    filing = Filing(
        accession_number="0001213900-26-058482",
        form_type="4",
        filing_date="2026-05-19",
        cik="0001992243",
        primary_document="xslF345X06/marketforms-73189.xml",
    )
    plain_url = (
        "https://www.sec.gov/Archives/edgar/data/1992243/"
        "000121390026058482/marketforms-73189.xml"
    )
    xslt_url = (
        "https://www.sec.gov/Archives/edgar/data/1992243/"
        "000121390026058482/xslF345X06/marketforms-73189.xml"
    )
    handler = _make_handler({
        plain_url: (200, _FORM4_NVDA_CEO_SALE),
        xslt_url: (200, "<html><head><style>x</style></head><body></body></html>"),
    })
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        f = await fetch_form4(client, filing)
    assert f.issuer_ticker == "NVDA"  # body is reused fixture; ticker comes from XML


@pytest.mark.asyncio
async def test_fetch_form4_rejects_wrong_form_type() -> None:
    filing = Filing(
        accession_number="X",
        form_type="10-K",
        filing_date="2026-01-01",
        cik="0001045810",
        primary_document="x.htm",
    )
    async with EdgarClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=""))
    ) as client:
        with pytest.raises(ValueError, match="fetch_form4 requires form_type='4'"):
            await fetch_form4(client, filing)


# --- aggregation -----------------------------------------------------------

def _parse(xml: str, *, accession: str, filing_date: str) -> Form4Filing:
    return parse_form4(xml, accession_number=accession, filing_date=filing_date)


def test_aggregate_counts_buys_and_sales() -> None:
    sale = _parse(_FORM4_NVDA_CEO_SALE, accession="A1", filing_date="2026-05-19")
    buy = _parse(_FORM4_NVDA_CFO_BUY_AND_GRANT, accession="A2", filing_date="2026-04-10")
    s = aggregate_insider_activity([sale, buy], as_of=date(2026, 5, 22))
    assert s.total_filings == 2
    assert s.sales_count == 1
    assert s.buys_count == 1
    # CEO: -$18M; CFO: +$1.4M; grant contributes $0
    assert s.net_dollars == pytest.approx(-18_000_000 + 1_400_000)
    assert s.latest_transaction_date == "2026-05-19"


def test_aggregate_code_mix_separates_derivative() -> None:
    f = _parse(_FORM4_NVDA_CFO_BUY_AND_GRANT, accession="A", filing_date="2026-04-10")
    s = aggregate_insider_activity([f], as_of=date(2026, 5, 22))
    assert s.code_mix == {"P": 1, "A": 1}
    assert s.deriv_code_mix == {"M": 1}


def test_aggregate_by_officer_sorted_by_abs_dollars() -> None:
    sale = _parse(_FORM4_NVDA_CEO_SALE, accession="A1", filing_date="2026-05-19")
    buy = _parse(_FORM4_NVDA_CFO_BUY_AND_GRANT, accession="A2", filing_date="2026-04-10")
    s = aggregate_insider_activity([sale, buy], as_of=date(2026, 5, 22))
    assert len(s.by_officer) == 2
    # CEO -$18M dominates CFO +$1.4M
    assert s.by_officer[0].name == "HUANG JEN-HSUN"
    assert s.by_officer[0].net_dollars == pytest.approx(-18_000_000)
    assert s.by_officer[1].name == "KRESS COLETTE"


def test_aggregate_window_filters_old_filings() -> None:
    """Filings whose period_of_report falls outside the window are dropped
    from the transaction-window aggregate. The old filing's filing_date
    (2025-01-15) also falls outside the disclosure window, so it doesn't
    contribute there either."""
    in_window = _parse(_FORM4_NVDA_CEO_SALE, accession="A1", filing_date="2026-05-19")
    old = _parse(_FORM4_OLD_PRE_WINDOW, accession="A0", filing_date="2025-01-15")
    s = aggregate_insider_activity([in_window, old], window_days=90, as_of=date(2026, 5, 22))
    assert s.total_filings == 1
    assert s.sales_count == 1   # the OLD 999999-share sale must NOT appear
    assert s.net_dollars == pytest.approx(-18_000_000)
    assert s.disclosed_filings_count == 1
    assert s.late_disclosure_count == 0
    assert s.latest_disclosure_date == "2026-05-19"


def test_aggregate_empty_list_returns_zero_summary() -> None:
    s = aggregate_insider_activity([], as_of=date(2026, 5, 22))
    assert s.total_filings == 0
    assert s.buys_count == 0
    assert s.sales_count == 0
    assert s.net_dollars == 0.0
    assert s.code_mix == {}
    assert s.by_officer == []
    assert s.latest_transaction_date is None
    assert s.disclosed_filings_count == 0
    assert s.late_disclosure_count == 0
    assert s.late_disclosure_officers == 0
    assert s.latest_disclosure_date is None


def test_aggregate_surfaces_late_disclosure_cluster() -> None:
    """A burst of insiders late-filing in the window for pre-window trades
    is itself signal. The TE case: 4 distinct officers filed Form 4s in
    May 2026 for transactions dated Jan 2026 (outside the 90d window).
    Even though total_filings is 0 for the trade window, the late-
    disclosure metrics surface the behavioral cluster."""
    late_owner_a = _parse(
        _FORM4_OLD_PRE_WINDOW, accession="LATE_A", filing_date="2026-05-11",
    )
    late_owner_b = _parse(
        _FORM4_OLD_PRE_WINDOW, accession="LATE_B", filing_date="2026-05-12",
    )
    # Force distinct owner CIKs by mutating after parse — the fixture shares
    # one owner CIK, but real late-disclosure clusters are by definition
    # multi-officer; we test that the distinct-officer count is right.
    late_owner_b.owners[0].cik = "0009999998"
    late_owner_c = _parse(
        _FORM4_OLD_PRE_WINDOW, accession="LATE_C", filing_date="2026-05-12",
    )
    late_owner_c.owners[0].cik = "0009999997"
    s = aggregate_insider_activity(
        [late_owner_a, late_owner_b, late_owner_c],
        window_days=90, as_of=date(2026, 5, 22),
    )
    assert s.total_filings == 0   # nothing transacted in window
    assert s.disclosed_filings_count == 3
    assert s.late_disclosure_count == 3
    assert s.late_disclosure_officers == 3
    assert s.latest_disclosure_date == "2026-05-12"


def test_aggregate_form4_with_no_owner_still_counts_transactions() -> None:
    """Regression: a Form 4 with an empty <reportingOwner> previously
    `continue`d in the aggregation loop, silently dropping ALL its
    transactions from top-line buys/sales/net_dollars/code_mix in
    addition to the (correct) by-officer attribution gating."""
    sale = Form4Transaction(
        date="2026-05-19", code="S", shares=10_000, price_per_share=150.00,
        acquired_disposed="D", security_title="Common Stock",
    )
    orphan = Form4Filing(
        accession_number="A-NOWNER", filing_date="2026-05-19",
        period_of_report="2026-05-19", issuer_cik="0001045810",
        issuer_ticker="NVDA", owners=[],   # ← no reporting owner
        non_derivative=[sale],
    )
    s = aggregate_insider_activity([orphan], as_of=date(2026, 5, 22))
    # Top-line counters MUST reflect the transaction
    assert s.total_filings == 1
    assert s.sales_count == 1
    assert s.net_dollars == pytest.approx(-1_500_000)
    assert s.code_mix == {"S": 1}
    assert s.latest_transaction_date == "2026-05-19"
    # by_officer is correctly empty — there's no owner to attribute to
    assert s.by_officer == []


def test_aggregate_discretionary_excludes_f_code() -> None:
    """FOLLOWUPS #17: net_dollars sums ALL non-derivative disposals (incl.
    code-F tax-withholding on vesting), but discretionary_net_dollars counts
    only open-market P/S — so comp mechanics don't masquerade as distribution."""
    owner = Form4Owner(
        cik="0000000001", name="CEO EXAMPLE", is_officer=True, officer_title="CEO",
    )
    filing = Form4Filing(
        accession_number="A1", filing_date="2026-05-20",
        period_of_report="2026-05-20", issuer_cik="0000000010",
        issuer_ticker="MRVL", owners=[owner],
        non_derivative=[
            Form4Transaction(
                date="2026-05-20", code="S", shares=10_000, price_per_share=200.0,
                acquired_disposed="D", security_title="Common Stock",
            ),
            Form4Transaction(
                date="2026-05-20", code="F", shares=100_000, price_per_share=200.0,
                acquired_disposed="D", security_title="Common Stock",
            ),
        ],
    )
    s = aggregate_insider_activity([filing], as_of=date(2026, 5, 22))
    assert s.net_dollars == pytest.approx(-22_000_000)               # S + F disposals
    assert s.discretionary_net_dollars == pytest.approx(-2_000_000)  # only the S sale
    assert s.sales_count == 1                                        # F is not an S
    assert s.code_mix == {"S": 1, "F": 1}


def test_stage_2_block_top_filters_pure_vesting_officer() -> None:
    """FOLLOWUPS #17 (per-officer): an officer whose entire window activity is
    vesting (code-F) has discretionary_net_dollars=0 and must NOT appear in the
    'top' distributors line, even if their full net_dollars dominates."""
    ceo = Form4Owner(
        cik="0000000001", name="CEO", is_officer=True, officer_title="CEO",
    )
    coo = Form4Owner(
        cik="0000000002", name="COO", is_officer=True, officer_title="COO",
    )
    ceo_filing = Form4Filing(
        accession_number="A1", filing_date="2026-05-20",
        period_of_report="2026-05-20", issuer_cik="0000000010",
        issuer_ticker="MRVL", owners=[ceo],
        non_derivative=[
            Form4Transaction(
                date="2026-05-20", code="S", shares=10_000, price_per_share=200.0,
                acquired_disposed="D", security_title="Common Stock",
            ),
        ],
    )
    coo_filing = Form4Filing(
        accession_number="A2", filing_date="2026-05-20",
        period_of_report="2026-05-20", issuer_cik="0000000010",
        issuer_ticker="MRVL", owners=[coo],
        non_derivative=[
            Form4Transaction(
                date="2026-05-20", code="F", shares=100_000, price_per_share=200.0,
                acquired_disposed="D", security_title="Common Stock",
            ),
        ],
    )
    s = aggregate_insider_activity(
        [ceo_filing, coo_filing], as_of=date(2026, 5, 22),
    )
    coo_oa = next(o for o in s.by_officer if o.name == "COO")
    ceo_oa = next(o for o in s.by_officer if o.name == "CEO")
    # COO: -$20M all-F vesting but zero discretionary.
    assert coo_oa.net_dollars == pytest.approx(-20_000_000)
    assert coo_oa.discretionary_net_dollars == 0
    # CEO: -$2M discretionary S-code sale.
    assert ceo_oa.discretionary_net_dollars == pytest.approx(-2_000_000)
    top_line = next(
        ln for ln in s.stage_2_block().splitlines() if ln.startswith("top:")
    )
    assert "CEO -$2.0M" in top_line
    assert "COO" not in top_line  # pure-vesting officer filtered out


# --- summary rendering -----------------------------------------------------

def test_stage_1_line_matches_spec_format() -> None:
    """FOLLOWUPS #3 example:
    'insider net flow last 90d: -$42M / 4 sales / 0 buys'."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=4, buys_count=0, sales_count=4,
        net_dollars=-42_000_000, discretionary_net_dollars=-42_000_000,
        code_mix={"S": 4}, deriv_code_mix={},
        by_officer=[], latest_transaction_date="2026-05-19",
    )
    assert s.stage_1_line() == "insider net flow last 90d: -$42.0M / 4 sales / 0 buys"


def test_stage_2_block_renders_three_lines() -> None:
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=2, buys_count=1, sales_count=1,
        net_dollars=-16_600_000, discretionary_net_dollars=-16_600_000,
        code_mix={"S": 1, "P": 1, "A": 1},
        deriv_code_mix={"M": 1},
        by_officer=[
            OfficerActivity(
                cik="11", name="HUANG", relationship="President & CEO",
                sales_count=1, net_shares=-120_000, net_dollars=-18_000_000,
                discretionary_net_dollars=-18_000_000,
                latest_transaction_date="2026-05-19",
            ),
            OfficerActivity(
                cik="22", name="KRESS", relationship="EVP & CFO",
                buys_count=1, net_shares=10_000, net_dollars=1_400_000,
                discretionary_net_dollars=1_400_000,
                latest_transaction_date="2026-04-10",
            ),
        ],
        latest_transaction_date="2026-05-19",
    )
    out = s.stage_2_block()
    lines = out.split("\n")
    assert lines[0] == "1 sales / 1 buys last 90d, net -$16.6M, latest 2026-05-19"
    assert lines[1].startswith("codes: ")
    assert "S×1" in lines[1] and "P×1" in lines[1] and "A×1" in lines[1]
    assert lines[2] == "top: President & CEO -$18.0M; EVP & CFO $1.4M"


def test_stage_2_block_surfaces_discretionary_vs_total_split() -> None:
    """FOLLOWUPS #17: when non-discretionary disposals materially inflate the
    all-disposals figure, the head shows discretionary as 'net' and annotates
    the total so the Skeptic reasons on real selling, not comp mechanics."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=3, buys_count=0, sales_count=13,
        net_dollars=-148_900_000, discretionary_net_dollars=-24_300_000,
        code_mix={"M": 25, "F": 25, "S": 13}, deriv_code_mix={},
        by_officer=[], latest_transaction_date="2026-05-22",
    )
    head = s.stage_2_block().splitlines()[0]
    # New format: "discretionary net <X> (vs <Y> total disposals incl. vesting/tax)"
    # so the Skeptic sees the figure is already filtered, not an unknown split.
    assert "discretionary net -$24.3M" in head
    assert "vs -$148.9M total disposals" in head
    assert "incl. vesting/tax" in head


def test_stage_1_line_appends_late_disclosure_clause() -> None:
    """When late-disclosure activity is present, stage_1_line surfaces it
    as a `· N late-disclosed (M officers)` suffix so Stage 1 can weight
    the cluster signal alongside the transaction-window totals."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=4, buys_count=0, sales_count=4,
        net_dollars=-42_000_000, discretionary_net_dollars=-42_000_000,
        code_mix={"S": 4}, deriv_code_mix={},
        by_officer=[], latest_transaction_date="2026-05-19",
        disclosed_filings_count=10, late_disclosure_count=6,
        late_disclosure_officers=5, latest_disclosure_date="2026-05-18",
    )
    assert s.stage_1_line() == (
        "insider net flow last 90d: -$42.0M / 4 sales / 0 buys"
        " · 6 late-disclosed (5 officers)"
    )


def test_stage_2_block_appends_disclosure_line_when_late() -> None:
    """stage_2_block adds a `disclosure: ...` line when late-disclosure
    filings exist, so /research and /probe surface the cluster pattern."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=1, buys_count=0, sales_count=1,
        net_dollars=-500_000, discretionary_net_dollars=-500_000,
        code_mix={"S": 1}, deriv_code_mix={},
        by_officer=[
            OfficerActivity(
                cik="11", name="CFO", relationship="Chief Financial Officer",
                sales_count=1, net_dollars=-500_000, discretionary_net_dollars=-500_000,
            ),
        ],
        latest_transaction_date="2026-05-06",
        disclosed_filings_count=10, late_disclosure_count=6,
        late_disclosure_officers=5, latest_disclosure_date="2026-05-18",
    )
    out = s.stage_2_block()
    assert out.splitlines()[-1] == (
        "disclosure: 10 filed in window "
        "(6 late for pre-window trades, 5 officers)"
    )


def test_render_for_prompt_surfaces_late_only_disclosure() -> None:
    """A summary with zero transaction-window activity but non-zero
    disclosure-window activity must NOT degrade to the `(no Form 4
    filings…)` empty message — the late-disclosure cluster is the signal."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=0, buys_count=0, sales_count=0, net_dollars=0.0,
        code_mix={}, deriv_code_mix={}, by_officer=[],
        latest_transaction_date=None,
        disclosed_filings_count=3, late_disclosure_count=3,
        late_disclosure_officers=3, latest_disclosure_date="2026-05-12",
    )
    out = InsiderActivitySummary.render_for_prompt(s)
    assert "no Form 4 filings" not in out
    assert "3 late for pre-window trades" in out


def test_stage_2_block_omits_top_when_no_dollar_moves() -> None:
    """If all officers have $0 net flow (only grants), the 'top: ...' line
    is omitted to avoid a noisy '$0' tail."""
    s = InsiderActivitySummary(
        window_days=90, window_start="2026-02-21", window_end="2026-05-22",
        total_filings=1, buys_count=0, sales_count=0, net_dollars=0.0,
        code_mix={"A": 1}, deriv_code_mix={},
        by_officer=[OfficerActivity(cik="11", name="X", relationship="Director")],
        latest_transaction_date=None,
    )
    out = s.stage_2_block()
    assert "top:" not in out


# --- format helpers --------------------------------------------------------

def test_fmt_dollars_scales() -> None:
    assert _fmt_dollars(1_200_000_000) == "$1.2B"
    assert _fmt_dollars(42_000_000) == "$42.0M"
    assert _fmt_dollars(-42_000_000) == "-$42.0M"
    assert _fmt_dollars(850_000) == "$850K"
    assert _fmt_dollars(-850_000) == "-$850K"
    assert _fmt_dollars(200) == "$200"
    assert _fmt_dollars(0) == "$0"


def test_relationship_label_priority() -> None:
    """officer_title > Officer > Director > 10% Owner > Insider."""
    ceo = Form4Owner(cik="1", name="X", is_director=True, is_officer=True, officer_title="CEO")
    assert _relationship_label(ceo) == "CEO"

    officer_no_title = Form4Owner(cik="1", name="X", is_officer=True)
    assert _relationship_label(officer_no_title) == "Officer"

    director = Form4Owner(cik="1", name="X", is_director=True)
    assert _relationship_label(director) == "Director"

    ten_pct = Form4Owner(cik="1", name="X", is_ten_percent_owner=True)
    assert _relationship_label(ten_pct) == "10% Owner"

    nothing = Form4Owner(cik="1", name="X")
    assert _relationship_label(nothing) == "Insider"


# ---------------------------------------------------------------------------
# load_insider_activity — high-level loader
# ---------------------------------------------------------------------------

def _form4_routes(
    *,
    ticker_index: dict = _TICKER_INDEX_BODY,
    submissions_cik: str = "0001045810",
    submissions: Optional[dict] = None,
    xml_bodies: Optional[dict[str, str]] = None,
) -> dict:
    """Build a {url-prefix: (status, body)} route map for a typical
    load_insider_activity flow: ticker index → submissions JSON → N
    Form 4 XML fetches."""
    routes: dict = {
        "https://www.sec.gov/files/company_tickers.json": (200, ticker_index),
    }
    if submissions is not None:
        routes[f"https://data.sec.gov/submissions/CIK{submissions_cik}.json"] = (
            200, submissions,
        )
    for url, body in (xml_bodies or {}).items():
        routes[url] = (200, body)
    return routes


def _make_form4_submissions(accessions_to_docs: list[tuple[str, str, str]]) -> dict:
    """Build a submissions JSON payload with N Form 4 filings.
    accessions_to_docs: list of (accession, filing_date, primary_document)."""
    return {
        "cik": "0001045810",
        "filings": {
            "recent": {
                "accessionNumber": [a for a, _, _ in accessions_to_docs],
                "form": ["4"] * len(accessions_to_docs),
                "filingDate": [d for _, d, _ in accessions_to_docs],
                "primaryDocument": [doc for _, _, doc in accessions_to_docs],
            }
        },
    }


@pytest.mark.asyncio
async def test_load_insider_activity_aggregates_filings() -> None:
    filings = [
        ("0001045810-26-000045", "2026-05-19", "form4_ceo.xml"),
        ("0001045810-26-000040", "2026-04-10", "form4_cfo.xml"),
    ]
    submissions = _make_form4_submissions(filings)
    f_ceo = Filing(
        accession_number=filings[0][0], form_type="4", filing_date=filings[0][1],
        cik="0001045810", primary_document=filings[0][2],
    )
    f_cfo = Filing(
        accession_number=filings[1][0], form_type="4", filing_date=filings[1][1],
        cik="0001045810", primary_document=filings[1][2],
    )
    routes = _form4_routes(
        submissions=submissions,
        xml_bodies={
            _form4_primary_xml_url(f_ceo): _FORM4_NVDA_CEO_SALE,
            _form4_primary_xml_url(f_cfo): _FORM4_NVDA_CFO_BUY_AND_GRANT,
        },
    )
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        summary = await load_insider_activity(
            "NVDA", client=client, as_of=date(2026, 5, 22),
        )
    assert summary is not None
    assert summary.total_filings == 2
    assert summary.sales_count == 1
    assert summary.buys_count == 1
    assert summary.net_dollars == pytest.approx(-18_000_000 + 1_400_000)


@pytest.mark.asyncio
async def test_load_insider_activity_unknown_ticker_returns_none() -> None:
    routes = _form4_routes()
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_insider_activity("BOGUS", client=client)
    assert result is None


@pytest.mark.asyncio
async def test_load_insider_activity_network_failure_returns_none() -> None:
    """Graceful degrade: HTTP 500 from EDGAR must not propagate to caller."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error", request=request)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_insider_activity("NVDA", client=client)
    assert result is None


@pytest.mark.asyncio
async def test_load_insider_activity_respects_max_filings_cap() -> None:
    """When submissions has more filings than max_filings, only the cap
    is fetched."""
    many = [
        (f"0001045810-26-{i:06d}", "2026-05-01", f"form4_{i}.xml")
        for i in range(10)
    ]
    submissions = _make_form4_submissions(many)
    fetch_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers.json"):
            return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)
        if url.startswith("https://data.sec.gov/submissions/"):
            return httpx.Response(200, json=submissions, request=request)
        if url.startswith("https://www.sec.gov/Archives/"):
            fetch_calls.append(url)
            return httpx.Response(200, text=_FORM4_NVDA_CEO_SALE, request=request)
        return httpx.Response(404, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        await load_insider_activity(
            "NVDA", max_filings=3, client=client, as_of=date(2026, 5, 22),
        )
    assert len(fetch_calls) == 3


@pytest.mark.asyncio
async def test_load_insider_activity_no_filings_returns_zero_summary() -> None:
    """Ticker in SEC universe but no Form 4 filings in window → empty
    summary (distinct from None which means EDGAR fetch failed)."""
    submissions = _make_form4_submissions([])
    routes = _form4_routes(submissions=submissions)
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        summary = await load_insider_activity(
            "NVDA", client=client, as_of=date(2026, 5, 22),
        )
    assert summary is not None
    assert summary.total_filings == 0
    assert summary.window_days == 90


@pytest.mark.asyncio
async def test_load_insider_activities_batch_amortizes_ticker_index() -> None:
    """Three tickers should share one company_tickers.json fetch
    (lazy CIK cache amortizes across calls on the shared client)."""
    submissions = _make_form4_submissions([])
    ticker_index_hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ticker_index_hits
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers.json"):
            ticker_index_hits += 1
            return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)
        if url.startswith("https://data.sec.gov/submissions/"):
            return httpx.Response(200, json=submissions, request=request)
        return httpx.Response(404, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_insider_activities_batch(
            ["NVDA", "AAPL", "CRM"], client=client, as_of=date(2026, 5, 22),
        )
    assert ticker_index_hits == 1
    assert set(result.keys()) == {"NVDA", "AAPL", "CRM"}
    for sym, summary in result.items():
        assert summary is not None
        assert summary.total_filings == 0


@pytest.mark.asyncio
async def test_load_insider_activities_batch_per_ticker_failure_isolated() -> None:
    """One bogus ticker → None in the dict; other tickers still populate."""
    submissions = _make_form4_submissions([])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers.json"):
            return httpx.Response(200, json=_TICKER_INDEX_BODY, request=request)
        if url.startswith("https://data.sec.gov/submissions/"):
            return httpx.Response(200, json=submissions, request=request)
        return httpx.Response(404, request=request)

    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_insider_activities_batch(
            ["NVDA", "BOGUS"], client=client, as_of=date(2026, 5, 22),
        )
    assert result["BOGUS"] is None
    assert result["NVDA"] is not None
    assert result["NVDA"].total_filings == 0


@pytest.mark.asyncio
async def test_load_insider_activity_skips_parse_failures() -> None:
    """If one Form 4 XML is malformed, the loader logs and continues —
    other filings still contribute to the aggregate."""
    filings = [
        ("0001045810-26-000045", "2026-05-19", "good.xml"),
        ("0001045810-26-000040", "2026-04-10", "bad.xml"),
    ]
    submissions = _make_form4_submissions(filings)
    good = Filing(
        accession_number=filings[0][0], form_type="4", filing_date=filings[0][1],
        cik="0001045810", primary_document=filings[0][2],
    )
    bad = Filing(
        accession_number=filings[1][0], form_type="4", filing_date=filings[1][1],
        cik="0001045810", primary_document=filings[1][2],
    )
    routes = _form4_routes(
        submissions=submissions,
        xml_bodies={
            _form4_primary_xml_url(good): _FORM4_NVDA_CEO_SALE,
            _form4_primary_xml_url(bad): "not valid xml <>",
        },
    )
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        summary = await load_insider_activity(
            "NVDA", client=client, as_of=date(2026, 5, 22),
        )
    assert summary is not None
    assert summary.total_filings == 1
    assert summary.sales_count == 1
