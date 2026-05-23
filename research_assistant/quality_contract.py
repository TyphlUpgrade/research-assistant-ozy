"""
Quality-contract floor checks (FOLLOWUPS #2).

Lightweight regex-only checks that gate Stage 2 thesis output against
the depth axis of the quality contract. These are the v1 floor; the
v1.x evaluator-LLM upgrade (FOLLOWUPS #10) layers structured grading
on top.

Public surface: `passes_depth_floor(text, window=80)`.
"""
from __future__ import annotations

import re


# Depth signals — any filing / transcript / segment-data reference.
# A match alone is NOT sufficient: see `passes_depth_floor` for the
# substance co-occurrence rule that closes the bare-citation gap.
_DEPTH_PATTERNS = re.compile(
    r"(S-1|10-Q|10-K|10K|8-K|"                    # filings
    r"earnings\s+call|transcript|"                # transcripts
    r"segment|revenue\s+by|"                      # segment breakdown
    r"guidance|forward\s+P/?E|"                   # forward-looking metrics
    r"risk\s+factor|management's\s+discussion)",  # filing-content markers
    re.IGNORECASE,
)

# Substance signals — concrete facts that would back a depth citation.
# At least one of these must appear within `window` chars of a depth
# match for the thesis to pass.
_SUBSTANCE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*%|"               # 18% / 3.5%
    r"\$\d+(?:[.,]\d+)?\s*[BMK]?|"      # $5 / $5.2M / $500K
    r"\b\d{4}-\d{2}-\d{2}\b|"           # ISO date
    r"\bQ[1-4]\b|"                      # Q3 (temporal anchor; pairs with year via context)
    r"\b[1-4]Q\d{2,4}\b|"               # 3Q24 / 2Q2025
    r"\b[1-4]H\d{2,4}\b|"               # 1H25
    r"\bFY\d{2,4}\b|"                   # FY25
    r"\b\d+\s*bps\b|"                   # 200 bps
    r"\b\d+(?:\.\d+)?x\b|"              # 1.5x / 22x
    r"(?<![\d-])\b\d{3,}(?:,\d{3})*\b(?!-[A-Z])",  # 3+ digit numbers,
                                                    # excluding "10-K" / "8-K"
                                                    # filing-name digits
    re.IGNORECASE,
)

DEFAULT_SUBSTANCE_WINDOW = 80


def passes_depth_floor(text: str, *, window: int = DEFAULT_SUBSTANCE_WINDOW) -> bool:
    """A thesis passes the depth floor when at least one filing /
    transcript / segment reference is accompanied by a substance signal
    (number, %, $, date, quarter) within `window` characters.

    Suppresses bare citations like *"See the 10-K for risk factors."* —
    the depth term is present but no concrete fact backs it. Closes the
    known weakness documented at the original test marker.
    """
    if not text:
        return False
    n = len(text)
    for match in _DEPTH_PATTERNS.finditer(text):
        start, end = match.span()
        slice_start = max(0, start - window)
        slice_end = min(n, end + window)
        if _SUBSTANCE_RE.search(text[slice_start:slice_end]):
            return True
    return False
