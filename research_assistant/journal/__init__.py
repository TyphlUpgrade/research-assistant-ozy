"""
Append-only alert journal package.

Two write paths (creation + enrichment) and two read paths (per-day raw +
windowed LWW-collapsed). See `alerts.py` module docstring for the SCHEMA
CONTRACT.
"""
from research_assistant.journal.alerts import (
    append_alert,
    append_enriched_alert,
    read_alerts,
    read_alerts_window,
)
from research_assistant.journal.outcomes import (
    enrich_alert_with_returns,
    enrich_window,
)

__all__ = [
    "append_alert",
    "append_enriched_alert",
    "enrich_alert_with_returns",
    "enrich_window",
    "read_alerts",
    "read_alerts_window",
]
