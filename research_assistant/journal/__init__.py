"""
Append-only alert journal package.

Two write paths (creation + enrichment) and two read paths (per-day raw +
windowed LWW-collapsed). See `alerts.py` module docstring for the SCHEMA
CONTRACT.

Stage 2 note journal (PR 2A.4) lives in `stage2_notes.py` — one file per
ticker, append-only, no dedup; see that module's docstring for its SCHEMA
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
from research_assistant.journal.stage2_notes import (
    append_stage2_note,
    read_stage2_full_history,
    read_stage2_history,
)

__all__ = [
    "append_alert",
    "append_enriched_alert",
    "append_stage2_note",
    "enrich_alert_with_returns",
    "enrich_window",
    "read_alerts",
    "read_alerts_window",
    "read_stage2_full_history",
    "read_stage2_history",
]
