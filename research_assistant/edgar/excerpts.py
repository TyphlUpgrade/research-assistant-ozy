"""
Filing-text excerpts (FOLLOWUPS #1 — full closure).

Fetches a specific filing on demand and greps paragraphs by the
operator's question keywords. Each excerpt carries its stable
`edgar:{form}:{accession}:para_{n}` anchor so that:
  - the /probe Stage 2 prompt can cite specific paragraphs
  - the orchestrator can later splice the paragraph text into evidence
    anchors so Defender's citation-corpus check (no code change needed)
    resolves pushback citations like "per the 10-K".

Gate: /probe only. Raw filing text never reaches Stage 0/1/2 of the
main cascade.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from research_assistant.edgar.client import EdgarClient

log = logging.getLogger(__name__)


DEFAULT_MAX_PARAGRAPHS = 10

# Generic English stopwords + filler verbs commonly seen in operator probe
# questions ("what does the 10-K say about X?"). The keyword extractor
# drops these so we grep on content terms only.
_STOPWORDS = frozenset({
    "a", "about", "above", "all", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "below", "between", "both", "but",
    "by", "can", "check", "could", "did", "do", "does", "doing", "done",
    "down", "during", "each", "few", "find", "for", "from", "get", "give",
    "go", "going", "has", "had", "have", "having", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "if", "in", "into", "is",
    "it", "its", "itself", "just", "know", "let", "like", "look", "make",
    "many", "may", "me", "might", "more", "most", "must", "my", "myself",
    "need", "no", "nor", "not", "now", "of", "off", "on", "once", "one",
    "only", "or", "other", "our", "ours", "out", "over", "own", "per",
    "pls", "please", "refer", "say", "says", "said", "see", "she", "show",
    "shows", "should", "so", "some", "such", "tell", "than", "that",
    "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under",
    "until", "up", "use", "uses", "used", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "within", "without", "would", "yes", "you", "your", "yours",
    "yourself", "yourselves",
})


@dataclass
class FilingExcerpts:
    """Selected paragraphs from a single filing, anchored for citation.

    Each `(anchor, paragraph)` tuple is independently citable. Used by
    /probe to inject filing text into Stage 2 and by the orchestrator
    to splice paragraph text into evidence anchors at write time."""
    ticker: str
    accession_number: str
    form_type: str
    filing_date: str
    cik: str
    excerpts: list[tuple[str, str]] = field(default_factory=list)

    def by_anchor(self, anchor: str) -> Optional[str]:
        """Return the paragraph text for a given anchor string, or None."""
        for a, t in self.excerpts:
            if a == anchor:
                return t
        return None

    def render_block(self) -> str:
        """Labeled-paragraph rendering for prompt injection.

        Format:
            [edgar:10-K:0001234567-26-000045:para_42]
            Paragraph text…

            [edgar:10-K:0001234567-26-000045:para_67]
            Paragraph text…
        """
        if not self.excerpts:
            return (
                f"(no paragraphs in {self.form_type} filed "
                f"{self.filing_date} matched question keywords)"
            )
        lines: list[str] = []
        for anchor, text in self.excerpts:
            lines.append(f"[{anchor}]")
            lines.append(text)
            lines.append("")
        return "\n".join(lines).rstrip()


def extract_keywords(question: str) -> list[str]:
    """Tokenize `question`; drop stopwords and tokens shorter than 3
    chars. Returns deduplicated, lowercased content terms in question
    order."""
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", question.lower())
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


async def load_filing_excerpts(
    ticker: str,
    form: str,
    question: str,
    *,
    max_paragraphs: int = DEFAULT_MAX_PARAGRAPHS,
    client: Optional[EdgarClient] = None,
) -> Optional[FilingExcerpts]:
    """Fetch the latest filing of `form` for `ticker`, grep paragraphs
    matching any content word from `question`, return up to
    `max_paragraphs` anchored excerpts.

    Returns None when:
      - ticker is not in the SEC universe
      - no filing of `form` exists
      - any HTTP / parse failure (graceful degrade)

    Returns FilingExcerpts with `excerpts=[]` when the filing was
    fetched but no paragraphs matched any keyword (a valid signal —
    the operator should rephrase or pick a different form).
    """
    owns_client = client is None
    if client is None:
        client = EdgarClient()
    try:
        cik = await client.resolve_cik(ticker)
        if cik is None:
            log.info("EDGAR excerpts: no CIK for %s", ticker)
            return None
        filings = await client.list_filings(cik, form, limit=1)
        if not filings:
            log.info("EDGAR excerpts: no %s filings for %s", form, ticker)
            return None
        filing = filings[0]
        filing_text = await client.fetch_filing(filing)

        keywords = extract_keywords(question)
        hits: list[tuple[str, str]] = []
        seen_anchors: set[str] = set()
        if keywords:
            for kw in keywords:
                # `text.search` returns up to max_hits per call; we ask for
                # the full budget each time and dedupe across keywords.
                for anchor, para in filing_text.search(kw, max_hits=max_paragraphs):
                    if anchor in seen_anchors:
                        continue
                    seen_anchors.add(anchor)
                    hits.append((anchor, para))
                    if len(hits) >= max_paragraphs:
                        break
                if len(hits) >= max_paragraphs:
                    break

        return FilingExcerpts(
            ticker=ticker.upper(),
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            filing_date=filing.filing_date,
            cik=cik,
            excerpts=hits,
        )
    except Exception as exc:
        log.warning(
            "EDGAR excerpts: load failed for %s %s: %s", ticker, form, exc,
        )
        return None
    finally:
        if owns_client:
            await client.close()
