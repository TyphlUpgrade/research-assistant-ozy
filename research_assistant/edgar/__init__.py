"""
EDGAR adapter package — rate-limited async client + per-form parsers.

Modules:
  client    — EdgarClient, _RateLimiter, Filing / FilingText, HTML extraction.
  form4     — Form 4 insider transactions + per-window aggregation + loaders.
  form13f   — Form 13F institutional holdings + per-stock aggregation.
  excerpts  — Filing-text excerpts (FOLLOWUPS #1) for /probe + Defender corpus.

Public surface re-exported here so callers can `from research_assistant.edgar
import EdgarClient, load_insider_activity` without caring about the internal
split. Names prefixed with `_` are private but re-exported for the test suite.
"""
from research_assistant.edgar.client import (
    DEFAULT_RATE_LIMIT_PER_SEC,
    DEFAULT_USER_AGENT,
    EdgarClient,
    Filing,
    FilingText,
    _extract_paragraphs,
    _RateLimiter,
)
from research_assistant.edgar.form4 import (
    INSIDER_DEFAULT_MAX_FILINGS,
    INSIDER_DEFAULT_WINDOW_DAYS,
    Form4Filing,
    Form4Owner,
    Form4Transaction,
    InsiderActivitySummary,
    OfficerActivity,
    _fmt_dollars,
    _relationship_label,
    aggregate_insider_activity,
    load_insider_activities_batch,
    load_insider_activity,
    parse_form4,
)
from research_assistant.edgar.form13f import (
    DEFAULT_TRACKED_FUNDS,
    Form13FFiling,
    Form13FHolding,
    FundPosition,
    InstitutionalOwnership,
    TrackedFund,
    aggregate_institutional_ownership,
    load_institutional_ownership,
    parse_13f,
)
from research_assistant.edgar.excerpts import (
    DEFAULT_MAX_PARAGRAPHS,
    FilingExcerpts,
    extract_keywords,
    load_filing_excerpts,
)

__all__ = [
    "DEFAULT_MAX_PARAGRAPHS",
    "DEFAULT_RATE_LIMIT_PER_SEC",
    "DEFAULT_TRACKED_FUNDS",
    "DEFAULT_USER_AGENT",
    "EdgarClient",
    "Filing",
    "FilingExcerpts",
    "FilingText",
    "Form4Filing",
    "Form4Owner",
    "Form4Transaction",
    "Form13FFiling",
    "Form13FHolding",
    "FundPosition",
    "INSIDER_DEFAULT_MAX_FILINGS",
    "INSIDER_DEFAULT_WINDOW_DAYS",
    "InsiderActivitySummary",
    "InstitutionalOwnership",
    "OfficerActivity",
    "TrackedFund",
    "aggregate_insider_activity",
    "aggregate_institutional_ownership",
    "extract_keywords",
    "load_filing_excerpts",
    "load_insider_activities_batch",
    "load_insider_activity",
    "load_institutional_ownership",
    "parse_13f",
    "parse_form4",
]
