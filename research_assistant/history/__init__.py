"""
Unified history reader — projects brief and research Stage 2 events into a
common shape (FOLLOWUPS #21).

`/brief` writes Stage 2 + inline Skeptic to the journal at
`.research/stage2/<TKR>.jsonl`. `/research` writes Stage 2 thesis + Stage 3
Skeptic critique to the dossier ledger at `.research/tickers/<TKR>.md`.
Operator-facing history views need the chronological union of both streams.
"""
from research_assistant.history.reader import (
    Stage2HistoryEntry,
    read_unified_history,
)
from research_assistant.history.trace_reader import (
    TraceEnrichment,
    read_chain_enrichment,
)
from research_assistant.history.views import (
    CohortGrid,
    build_cohort_grid,
    enumerate_tickers,
    filter_verdicts,
    history_brief,
    render_cohort_grid,
    render_history_brief,
    render_verdicts_table,
)

__all__ = [
    "CohortGrid",
    "Stage2HistoryEntry",
    "TraceEnrichment",
    "build_cohort_grid",
    "enumerate_tickers",
    "filter_verdicts",
    "history_brief",
    "read_chain_enrichment",
    "read_unified_history",
    "render_cohort_grid",
    "render_history_brief",
    "render_verdicts_table",
]
