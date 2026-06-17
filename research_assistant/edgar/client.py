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
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


DEFAULT_USER_AGENT = "research-assistant william.a.sit@gmail.com"
DEFAULT_RATE_LIMIT_PER_SEC = 5.0

# SEC throttling is transient; retry 429/503 a few times with exponential
# backoff (honoring Retry-After when present) instead of dropping the data.
DEFAULT_MAX_RETRIES = 3
RETRYABLE_STATUS = frozenset({429, 503})

# The ticker→CIK index changes rarely; persist it to disk so the ~1MB file
# is fetched at most once per TTL window across all CLI invocations, not
# once per process. Override the directory via EDGAR_CACHE_DIR.
CIK_DISK_CACHE_FILENAME = "research_assistant_cik_index.json"
CIK_DISK_CACHE_TTL_SEC = 24 * 60 * 60

TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_ARCHIVE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}"
)


def _default_cik_cache_path() -> Path:
    """Disk-cache location for the ticker→CIK index. Honors EDGAR_CACHE_DIR,
    else falls back to a stable file in the system temp dir."""
    base = os.environ.get("EDGAR_CACHE_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    return root / CIK_DISK_CACHE_FILENAME


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
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = 0.5,
        use_disk_cache: Optional[bool] = None,
        cik_cache_path: Optional[Path] = None,
    ):
        ua = user_agent or os.environ.get("EDGAR_USER_AGENT") or DEFAULT_USER_AGENT
        self._user_agent = ua
        self._rate_limiter = _RateLimiter(rate_limit_per_sec)
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._http = httpx.AsyncClient(
            headers={
                "User-Agent": ua,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            transport=transport,
        )
        self._cik_cache: Optional[dict[str, str]] = None
        # Guards the check-and-set in _ensure_cik_cache: without it, N
        # coroutines launched concurrently (e.g. the brief's 30-ticker
        # gather) each see an empty cache and stampede the index download.
        self._cik_lock = asyncio.Lock()
        # Disk cache defaults on in production, off when a transport is
        # injected (tests) so suites stay hermetic and don't share state
        # through a temp file. Explicit use_disk_cache overrides.
        self._use_disk_cache = (transport is None) if use_disk_cache is None else use_disk_cache
        self._cik_cache_path = cik_cache_path or _default_cik_cache_path()

    async def __aenter__(self) -> "EdgarClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def get(self, url: str) -> httpx.Response:
        """Rate-limited GET with retry on transient SEC throttling. Public
        primitive — form-specific loaders (form4, form13f, excerpts) call
        this directly rather than going through form-aware client methods,
        which keeps `EdgarClient` free of form-type knowledge.

        On 429/503, retries up to `max_retries` times with exponential
        backoff (honoring a numeric Retry-After header when present). This
        prevents a transient throttle from silently dropping 13F/Form-4
        data — the caller's degrade path then only triggers on genuine,
        persistent failures."""
        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.acquire()
            response = await self._http.get(url)
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                delay = self._retry_delay(response, attempt)
                log.warning(
                    "EDGAR %s on %s — retry %d/%d after %.2fs",
                    response.status_code, url, attempt + 1, self._max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        # Unreachable: the loop either returns or raises on the final attempt.
        raise RuntimeError("get() retry loop exited without returning")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Seconds to wait before the next retry. Prefer a numeric
        Retry-After header; otherwise exponential backoff."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass  # HTTP-date form not supported; fall back to backoff
        return self._retry_backoff_base * (2 ** attempt)

    async def _ensure_cik_cache(self) -> dict[str, str]:
        if self._cik_cache is not None:
            return self._cik_cache
        async with self._cik_lock:
            # Re-check: a coroutine that was waiting on the lock while
            # another populated the cache must not refetch.
            if self._cik_cache is not None:
                return self._cik_cache
            disk = self._read_cik_disk_cache()
            if disk is not None:
                self._cik_cache = disk
                return disk
            log.info("Loading SEC ticker → CIK index from %s", TICKER_INDEX_URL)
            response = await self.get(TICKER_INDEX_URL)
            raw = response.json()
            cache = self._parse_cik_index(raw)
            self._cik_cache = cache
            self._write_cik_disk_cache(cache)
            return cache

    @staticmethod
    def _parse_cik_index(raw: dict) -> dict[str, str]:
        # company_tickers.json is keyed by integer-string indices; each value
        # is {"cik_str": int, "ticker": "AAPL", "title": "Apple Inc."}.
        cache: dict[str, str] = {}
        for entry in raw.values():
            ticker = entry.get("ticker")
            cik_int = entry.get("cik_str")
            if not ticker or cik_int is None:
                continue
            cache[ticker.upper()] = str(cik_int).zfill(10)
        return cache

    def _read_cik_disk_cache(self) -> Optional[dict[str, str]]:
        """Return the cached index if present and within TTL, else None.
        Fail-open: any read/parse error returns None so we refetch."""
        if not self._use_disk_cache:
            return None
        try:
            age = time.time() - self._cik_cache_path.stat().st_mtime
            if age >= CIK_DISK_CACHE_TTL_SEC:
                return None
            data = json.loads(self._cik_cache_path.read_text())
            index = data.get("index")
            if isinstance(index, dict) and index:
                log.info("Loaded ticker → CIK index from disk cache %s", self._cik_cache_path)
                return {str(k): str(v) for k, v in index.items()}
        except (OSError, ValueError):
            return None
        return None

    def _write_cik_disk_cache(self, cache: dict[str, str]) -> None:
        """Persist the index atomically. Fail-open: cache-write errors are
        logged and swallowed — a missing disk cache only costs a refetch."""
        if not self._use_disk_cache:
            return
        try:
            self._cik_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"fetched_at": time.time(), "index": cache})
            tmp = self._cik_cache_path.with_suffix(
                self._cik_cache_path.suffix + f".{os.getpid()}.tmp"
            )
            tmp.write_text(payload)
            os.replace(tmp, self._cik_cache_path)
        except OSError as exc:
            log.warning("Could not write CIK disk cache %s: %s", self._cik_cache_path, exc)

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
        response = await self.get(url)
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
        paragraph-anchored body text.

        Form-specific parsers (Form 4, 13F) live as free functions in
        their respective sibling modules (`form4.fetch_form4`,
        `form13f.fetch_13f`) so this client stays free of form-type
        knowledge."""
        response = await self.get(filing.archive_url)
        body = response.text
        paragraphs = _extract_paragraphs(body, filing.primary_document)
        return FilingText(
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            filing_date=filing.filing_date,
            cik=filing.cik,
            paragraphs=paragraphs,
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
