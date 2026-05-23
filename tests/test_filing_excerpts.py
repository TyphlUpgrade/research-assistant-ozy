"""
Tests for the FOLLOWUPS #1 closure: /probe full-text filing path +
orchestrator anchor enrichment + Defender corpus integration.

Covers:
- extract_keywords filters stopwords and short tokens.
- load_filing_excerpts: happy path, unknown ticker → None, no-keyword
  question → empty excerpts, max_paragraphs cap, multi-keyword dedupe.
- _format_filing_excerpts_block: None / empty / populated.
- _enrich_anchors_with_filing_text: matches edgar:<form>:<acc>:para_N
  exactly, splices para_text, leaves other anchors intact.
- End-to-end: probe_ticker writes enriched anchors → defender-check
  via _flatten_anchors_to_corpus picks up para_text → pushback that
  cites a token IN the paragraph text does NOT fire Defender.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import httpx
import pytest

from research_assistant.edgar import (
    EdgarClient,
    FilingExcerpts,
    extract_keywords,
    load_filing_excerpts,
)
from research_assistant.dossier_io import Dossier, write_dossier_atomic
from research_assistant.orchestrator import (
    _enrich_anchors_with_filing_text,
    _flatten_anchors_to_corpus,
    probe_ticker,
    should_invoke_defender,
)

# The three-state prompt-block rendering for filing excerpts now lives
# as a classmethod on FilingExcerpts; alias keeps the existing test
# names readable.
_format_filing_excerpts_block = FilingExcerpts.render_for_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TICKER_INDEX = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Corp"},
}

_NVDA_SUBMISSIONS_10K = {
    "cik": "0001045810",
    "filings": {
        "recent": {
            "accessionNumber": ["0001045810-26-000045"],
            "form": ["10-K"],
            "filingDate": ["2026-04-15"],
            "primaryDocument": ["nvda-20260131.htm"],
        }
    },
}

# Three paragraphs: one talks about competition (matches), one about
# revenue specifics (also matches when keyword "revenue" appears), one
# generic boilerplate that nothing should match.
_NVDA_10K_HTML = """<html><body>
<p>Competition in the accelerated computing market remains intense, with
AMD MI300 and Intel Gaudi 3 each targeting our data-center customers.</p>
<p>Data center segment revenue grew 27% year-over-year to $130.5 billion,
driven by Hopper-Blackwell transition and CSP backlog.</p>
<p>This Annual Report contains forward-looking statements within the
meaning of Section 27A of the Securities Act of 1933.</p>
</body></html>"""


def _make_handler(routes: dict[str, tuple[int, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(body, (dict, list)):
                    return httpx.Response(status, json=body, request=request)
                return httpx.Response(status, text=body, request=request)
        return httpx.Response(404, text=f"unmatched {request.url}", request=request)
    return handler


def _archive_url(cik: str, accession: str, primary_doc: str) -> str:
    cik_no_zeros = cik.lstrip("0") or "0"
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
        f"{acc_no_dashes}/{primary_doc}"
    )


# ---------------------------------------------------------------------------
# extract_keywords
# ---------------------------------------------------------------------------

def test_extract_keywords_drops_stopwords() -> None:
    kw = extract_keywords("What does the 10-K say about competition?")
    # "10-k" gets tokenized but the regex requires the FIRST char to be
    # a letter — so "10-k" is rejected. Stopwords "what does the say about"
    # are filtered. Only "competition" survives.
    assert kw == ["competition"]


def test_extract_keywords_drops_short_tokens() -> None:
    """Tokens shorter than 3 chars after the leading letter are dropped."""
    kw = extract_keywords("hi a bb the segment")
    assert "hi" not in kw
    assert "bb" not in kw
    assert "segment" in kw


def test_extract_keywords_deduplicates_and_preserves_order() -> None:
    kw = extract_keywords("competition COMPETITION segment competition")
    assert kw == ["competition", "segment"]


def test_extract_keywords_empty_when_only_stopwords() -> None:
    assert extract_keywords("what does it say about that") == []


# ---------------------------------------------------------------------------
# load_filing_excerpts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_filing_excerpts_greps_paragraphs_by_keyword() -> None:
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_10K),
        _archive_url("0001045810", "0001045810-26-000045", "nvda-20260131.htm"):
            (200, _NVDA_10K_HTML),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        excerpts = await load_filing_excerpts(
            "NVDA", "10-K", "competition in segment", client=client,
        )
    assert excerpts is not None
    assert excerpts.ticker == "NVDA"
    assert excerpts.accession_number == "0001045810-26-000045"
    assert excerpts.form_type == "10-K"
    # "competition" matches para 0; "segment" matches para 1
    anchors = [a for a, _ in excerpts.excerpts]
    assert "edgar:10-K:0001045810-26-000045:para_0" in anchors
    assert "edgar:10-K:0001045810-26-000045:para_1" in anchors
    # Boilerplate paragraph 2 has no match
    assert "edgar:10-K:0001045810-26-000045:para_2" not in anchors


@pytest.mark.asyncio
async def test_load_filing_excerpts_unknown_ticker_returns_none() -> None:
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_filing_excerpts(
            "BOGUS", "10-K", "competition", client=client,
        )
    assert result is None


@pytest.mark.asyncio
async def test_load_filing_excerpts_no_filings_returns_none() -> None:
    empty_submissions = {
        "cik": "0001045810",
        "filings": {"recent": {"accessionNumber": [], "form": [],
                                "filingDate": [], "primaryDocument": []}},
    }
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, empty_submissions),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_filing_excerpts(
            "NVDA", "10-K", "competition", client=client,
        )
    assert result is None


@pytest.mark.asyncio
async def test_load_filing_excerpts_keyword_misses_returns_empty_excerpts() -> None:
    """Filing fetched but no keyword matched: returns FilingExcerpts with
    empty list (not None) so the operator sees "no matches" rather than
    "EDGAR fetch failed"."""
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_10K),
        _archive_url("0001045810", "0001045810-26-000045", "nvda-20260131.htm"):
            (200, _NVDA_10K_HTML),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_filing_excerpts(
            "NVDA", "10-K", "zebrastriped quaxotic", client=client,
        )
    assert result is not None
    assert result.excerpts == []


@pytest.mark.asyncio
async def test_load_filing_excerpts_respects_max_paragraphs_cap() -> None:
    big_html = "<html><body>" + "".join(
        f"<p>Paragraph {i} mentions revenue and segment specifics.</p>"
        for i in range(30)
    ) + "</body></html>"
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (200, _TICKER_INDEX),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, _NVDA_SUBMISSIONS_10K),
        _archive_url("0001045810", "0001045810-26-000045", "nvda-20260131.htm"):
            (200, big_html),
    }
    handler = _make_handler(routes)
    async with EdgarClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_filing_excerpts(
            "NVDA", "10-K", "revenue segment", client=client, max_paragraphs=5,
        )
    assert result is not None
    assert len(result.excerpts) == 5


# ---------------------------------------------------------------------------
# _format_filing_excerpts_block
# ---------------------------------------------------------------------------

def test_format_excerpts_block_none_signals_not_requested() -> None:
    out = _format_filing_excerpts_block(None)
    assert "not requested" in out.lower() or "no filing excerpts" in out.lower()
    assert "--filing" in out


def test_format_excerpts_block_empty_signals_no_keyword_matches() -> None:
    fe = FilingExcerpts(
        ticker="NVDA", accession_number="A", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810", excerpts=[],
    )
    out = _format_filing_excerpts_block(fe)
    assert "no paragraphs" in out.lower()
    assert "10-K" in out


def test_format_excerpts_block_populated_renders_anchors_and_text() -> None:
    fe = FilingExcerpts(
        ticker="NVDA", accession_number="ACC", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810",
        excerpts=[
            ("edgar:10-K:ACC:para_0", "first paragraph"),
            ("edgar:10-K:ACC:para_5", "fifth paragraph"),
        ],
    )
    out = _format_filing_excerpts_block(fe)
    assert "[edgar:10-K:ACC:para_0]" in out
    assert "first paragraph" in out
    assert "[edgar:10-K:ACC:para_5]" in out
    assert "fifth paragraph" in out


# ---------------------------------------------------------------------------
# _enrich_anchors_with_filing_text
# ---------------------------------------------------------------------------

def test_enrich_anchors_splices_para_text_for_matching_source() -> None:
    fe = FilingExcerpts(
        ticker="NVDA", accession_number="ACC", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810",
        excerpts=[
            ("edgar:10-K:ACC:para_42", "Data center revenue grew 27% YoY."),
        ],
    )
    anchors = [
        {"claim": "DC revenue +27%", "source": "edgar:10-K:ACC:para_42"},
        {"claim": "other", "source": "yfinance:fetch_bars:NVDA"},
    ]
    enriched = _enrich_anchors_with_filing_text(anchors, fe)
    assert enriched[0]["para_text"] == "Data center revenue grew 27% YoY."
    assert "para_text" not in enriched[1]
    # Originals not mutated
    assert "para_text" not in anchors[0]


def test_enrich_anchors_passes_through_when_no_match() -> None:
    fe = FilingExcerpts(
        ticker="NVDA", accession_number="ACC", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810", excerpts=[],
    )
    anchors = [
        {"claim": "x", "source": "edgar:10-K:ACC:para_999"},  # no match in fe
        {"claim": "y", "source": "edgar:8-K:OTHER:para_1"},   # wrong filing
    ]
    enriched = _enrich_anchors_with_filing_text(anchors, fe)
    assert all("para_text" not in a for a in enriched)


def test_enrich_anchors_none_filing_excerpts_passes_through() -> None:
    anchors = [{"claim": "x", "source": "edgar:10-K:ACC:para_1"}]
    enriched = _enrich_anchors_with_filing_text(anchors, None)
    assert enriched == anchors


def test_enrich_anchors_skips_non_dict_entries() -> None:
    fe = FilingExcerpts(
        ticker="NVDA", accession_number="ACC", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810",
        excerpts=[("edgar:10-K:ACC:para_1", "txt")],
    )
    anchors = ["bare-string-anchor", {"claim": "x", "source": "edgar:10-K:ACC:para_1"}]
    enriched = _enrich_anchors_with_filing_text(anchors, fe)
    assert enriched[0] == "bare-string-anchor"
    assert enriched[1]["para_text"] == "txt"


# ---------------------------------------------------------------------------
# End-to-end: probe → enriched anchors → Defender corpus check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_writes_enriched_anchors_and_defender_resolves(tmp_path: Path) -> None:
    """The full FOLLOWUPS #1 spec path:
      1. /probe with --filing fetches a filing
      2. Probe answer cites edgar:<form>:<acc>:para_N
      3. Orchestrator splices para_text into the anchor
      4. defender-check loads the enriched anchors from disk
      5. User pushback citing a token IN para_text does NOT fire Defender
    """
    write_dossier_atomic(Dossier(symbol="NVDA", state_md="prior thesis"), tmp_path)

    fe = FilingExcerpts(
        ticker="NVDA", accession_number="ACC", form_type="10-K",
        filing_date="2026-04-15", cik="0001045810",
        excerpts=[
            ("edgar:10-K:ACC:para_42", "Data center segment revenue grew 27% YoY to $130.5 billion."),
        ],
    )

    async def fake_probe(client, ws, td, h, dc, q,
                         insider_activity=None, institutional_ownership=None,
                         filing_excerpts=None):
        # Probe cites the exact anchor returned by load_filing_excerpts
        return {
            "ticker": "NVDA", "answer": "DC revenue grew 27%.",
            "evidence_anchors": [
                {"claim": "DC revenue +27%", "source": "edgar:10-K:ACC:para_42"},
            ],
            "closes_questions": [], "new_open_questions": [],
        }, None

    with patch("research_assistant.orchestrator._stage_2_probe", fake_probe):
        result = await probe_ticker(
            "NVDA", "what does the 10-K say about competition?",
            world_state={}, ticker_data={"price": 150.0}, headlines=[],
            base=tmp_path, filing_excerpts=fe,
        )

    # Probe-side: enriched anchor stored back in evidence_anchors
    enriched = result.evidence_anchors[0]
    assert enriched["source"] == "edgar:10-K:ACC:para_42"
    assert "para_text" in enriched
    assert "27%" in enriched["para_text"]

    # Defender-side: corpus blob now contains the paragraph text. A
    # pushback citing "27%" — present in para_text — does NOT fire.
    corpus = _flatten_anchors_to_corpus(result.evidence_anchors)
    assert "27%" in corpus
    assert "$130.5 billion" in corpus.lower() or "130.5 billion" in corpus.lower()

    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — 27% growth is not sustainable.",
        prior_evidence_anchors=result.evidence_anchors,
    ) is False

    # Inverted: pushback citing a FAKE figure not in para_text still fires.
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — the 9.9% YoY decline contradicts your read.",
        prior_evidence_anchors=result.evidence_anchors,
    ) is True
