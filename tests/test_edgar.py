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
from typing import Any, Callable

import httpx
import pytest

from research_assistant.edgar import (
    DEFAULT_USER_AGENT,
    EdgarClient,
    Filing,
    FilingText,
    _extract_paragraphs,
    _RateLimiter,
)


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
