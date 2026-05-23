"""
EDGAR HTTP client + filing primitives (FOLLOWUPS #1).

Rate-limited async HTTP adapter for SEC EDGAR. Resolves tickers to CIKs,
lists filings by form type, fetches filing bodies as paragraph-anchored
text. Form-specific parsers (#3 Form 4, #5 13F) live in sibling modules
and reuse this client.

Anchor format (stable, citable by Defender):
    edgar:{form}:{accession}:para_{n}
  e.g. edgar:8-K:0001234567-26-000045:para_17

Rate limit: 5 req/sec (half of SEC's stated 10/sec ceiling). Sliding-window
token bucket via monotonic-clock timestamps.

User-Agent: required by SEC. Defaults to
"research-assistant william.a.sit@gmail.com"; override via env var
EDGAR_USER_AGENT.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


DEFAULT_USER_AGENT = "research-assistant william.a.sit@gmail.com"
DEFAULT_RATE_LIMIT_PER_SEC = 5.0

TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_ARCHIVE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Filing:
    """A single SEC filing index entry (metadata only — no body text)."""
    accession_number: str       # e.g. "0001234567-26-000045"
    form_type: str              # e.g. "10-K", "10-Q", "8-K"
    filing_date: str            # ISO date string
    cik: str                    # 10-digit zero-padded CIK
    primary_document: str       # filename of the main document

    @property
    def archive_url(self) -> str:
        accession_no_dashes = self.accession_number.replace("-", "")
        # The Archives path uses CIK with leading zeros stripped, but the
        # accession-number directory keeps its full 18 digits.
        cik_no_zeros = self.cik.lstrip("0") or "0"
        return FILING_ARCHIVE_URL.format(
            cik=cik_no_zeros,
            accession_no_dashes=accession_no_dashes,
            primary_doc=self.primary_document,
        )


@dataclass
class FilingText:
    """A fetched filing with body text + paragraph anchoring."""
    accession_number: str
    form_type: str
    filing_date: str
    cik: str
    paragraphs: list[str]

    def anchor(self, para_idx: int) -> str:
        return f"edgar:{self.form_type}:{self.accession_number}:para_{para_idx}"

    def search(self, needle: str, *, max_hits: int = 5) -> list[tuple[str, str]]:
        """Return (anchor, paragraph) tuples whose body contains `needle`
        (case-insensitive). Stops after `max_hits`. Used by Defender (#2)
        to verify pushback citations against fetched filing text."""
        needle_l = needle.lower()
        hits: list[tuple[str, str]] = []
        for i, p in enumerate(self.paragraphs):
            if needle_l in p.lower():
                hits.append((self.anchor(i), p))
                if len(hits) >= max_hits:
                    break
        return hits


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Sliding-window rate limiter; async-safe via internal lock.

    Tracks request timestamps in a deque. Before each acquire, ages out
    timestamps older than `window`, then sleeps until capacity is
    available if at-cap."""

    def __init__(self, max_per_sec: float, window: float = 1.0):
        self._window = window
        self._max = max_per_sec
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._drop_aged(now)
            if len(self._timestamps) >= self._max:
                sleep_for = self._window - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.monotonic()
                self._drop_aged(now)
            self._timestamps.append(now)

    def _drop_aged(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self._window:
            self._timestamps.popleft()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EdgarClient:
    """Rate-limited async HTTP client for SEC EDGAR.

    Usage:
        async with EdgarClient() as client:
            cik = await client.resolve_cik("NVDA")
            filings = await client.list_filings(cik, "10-K", limit=2)
            text = await client.fetch_filing(filings[0])
            for anchor, body in text.search("revenue"):
                ...

    Args:
        user_agent: SEC-required identifier. Defaults to env
            EDGAR_USER_AGENT or DEFAULT_USER_AGENT.
        rate_limit_per_sec: requests-per-second ceiling (default 5).
        timeout: per-request timeout in seconds (default 30).
        transport: optional httpx transport for testing
            (httpx.MockTransport).
    """

    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        rate_limit_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        ua = user_agent or os.environ.get("EDGAR_USER_AGENT") or DEFAULT_USER_AGENT
        self._user_agent = ua
        self._rate_limiter = _RateLimiter(rate_limit_per_sec)
        self._http = httpx.AsyncClient(
            headers={
                "User-Agent": ua,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            transport=transport,
        )
        self._cik_cache: Optional[dict[str, str]] = None

    async def __aenter__(self) -> "EdgarClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str) -> httpx.Response:
        await self._rate_limiter.acquire()
        response = await self._http.get(url)
        response.raise_for_status()
        return response

    async def _ensure_cik_cache(self) -> dict[str, str]:
        if self._cik_cache is not None:
            return self._cik_cache
        log.info("Loading SEC ticker → CIK index from %s", TICKER_INDEX_URL)
        response = await self._get(TICKER_INDEX_URL)
        raw = response.json()
        # company_tickers.json is keyed by integer-string indices; each value
        # is {"cik_str": int, "ticker": "AAPL", "title": "Apple Inc."}.
        cache: dict[str, str] = {}
        for entry in raw.values():
            ticker = entry.get("ticker")
            cik_int = entry.get("cik_str")
            if not ticker or cik_int is None:
                continue
            cache[ticker.upper()] = str(cik_int).zfill(10)
        self._cik_cache = cache
        return cache

    async def resolve_cik(self, ticker: str) -> Optional[str]:
        """Return 10-digit zero-padded CIK for `ticker`, or None when the
        ticker is not in the SEC universe (foreign private issuers, OTC,
        delisted)."""
        cache = await self._ensure_cik_cache()
        return cache.get(ticker.upper())

    async def list_filings(
        self,
        cik: str,
        form_type: str,
        *,
        since: Optional[str] = None,
        limit: int = 10,
    ) -> list[Filing]:
        """List recent filings of `form_type` for `cik`, newest first.

        Args:
            cik: CIK (zero-padded or not).
            form_type: exact form code — "10-K", "10-Q", "8-K", "4",
                "13F-HR". Matched case-sensitively against SEC's form
                column.
            since: optional ISO date filter; only filings with
                filing_date >= since are returned.
            limit: max number to return.
        """
        cik = cik.zfill(10)
        url = SUBMISSIONS_URL.format(cik=cik)
        response = await self._get(url)
        payload = response.json()
        recent = payload.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        docs = recent.get("primaryDocument", [])
        results: list[Filing] = []
        for acc, form, date, doc in zip(accessions, forms, dates, docs):
            if form != form_type:
                continue
            if since is not None and date < since:
                continue
            results.append(Filing(
                accession_number=acc,
                form_type=form,
                filing_date=date,
                cik=cik,
                primary_document=doc,
            ))
            if len(results) >= limit:
                break
        return results

    async def fetch_filing(self, filing: Filing) -> FilingText:
        """Fetch a filing's primary document, strip HTML, and return
        paragraph-anchored body text."""
        response = await self._get(filing.archive_url)
        body = response.text
        paragraphs = _extract_paragraphs(body, filing.primary_document)
        return FilingText(
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            filing_date=filing.filing_date,
            cik=filing.cik,
            paragraphs=paragraphs,
        )

    async def fetch_form4(self, filing: Filing):
        """Fetch a Form 4 filing and parse its structured XML.

        `parse_form4` is imported lazily to avoid a circular import:
        `form4` depends on this module's `EdgarClient` (via the
        high-level loaders), and this method depends on `form4.parse_form4`.
        Lazy import keeps the package import graph acyclic."""
        if filing.form_type != "4":
            raise ValueError(
                f"fetch_form4 requires form_type='4', got {filing.form_type!r}"
            )
        response = await self._get(filing.archive_url)
        from research_assistant.edgar.form4 import parse_form4  # circular-import guard
        return parse_form4(
            response.text,
            accession_number=filing.accession_number,
            filing_date=filing.filing_date,
        )


# ---------------------------------------------------------------------------
# HTML -> paragraph extraction
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _extract_paragraphs(raw: str, filename: str) -> list[str]:
    """Strip HTML/XBRL tags and split into paragraphs.

    .txt files: split on blank lines.
    .htm/.html: BeautifulSoup over <p>/<div> blocks, drop <script>/<style>,
    collapse whitespace, dedupe parent/child containment.
    """
    if filename.lower().endswith(".txt"):
        return [_collapse_ws(p) for p in raw.split("\n\n") if _collapse_ws(p)]

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    blocks: list[str] = []
    for el in soup.find_all(["p", "div"]):
        text = _collapse_ws(el.get_text(" ", strip=True))
        if text:
            blocks.append(text)
    if not blocks:
        all_text = _collapse_ws(soup.get_text("\n"))
        blocks = [p for p in all_text.split("\n") if p.strip()]
    return _dedupe_consecutive(blocks)


def _collapse_ws(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _dedupe_consecutive(blocks: list[str]) -> list[str]:
    """When a parent <div> and its child <p> both surface the same text,
    keep only the longer (preserves any siblings the parent rolled up)."""
    out: list[str] = []
    for b in blocks:
        if out and (b in out[-1] or out[-1] in b):
            if len(b) > len(out[-1]):
                out[-1] = b
            continue
        out.append(b)
    return out
