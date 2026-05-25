"""
Setup-finder screeners package.

Public surface re-exported here so callers can `from research_assistant.screeners
import SetupCandidate, run_screeners_and_journal` without caring about the
internal module split. Underscore-prefixed internals (e.g. `_REGISTRY`) are
intentionally NOT re-exported.

Plan: `.omc/plans/setup-finder-v1-implementation.md` (PR 1.1 ships the
foundation; PR 1.2 + 2.2 + 2.3 register the three v1 screeners).
"""
from research_assistant.screeners._pipeline import (
    compute_sector_performance,
    evaluate_all,
    register_screener,
    run_screeners_and_journal,
)
from research_assistant.screeners._types import (
    Screener,
    SetupCandidate,
    register_formatter,
    render_setup_line,
)

__all__ = [
    "Screener",
    "SetupCandidate",
    "compute_sector_performance",
    "evaluate_all",
    "register_formatter",
    "register_screener",
    "render_setup_line",
    "run_screeners_and_journal",
]

# Screener auto-registration — importing the module triggers its
# register_screener() call at the bottom of the file.
# PR 1.2: sector_rotation. PR 2.2: pead. PR 2.3: pre_catalyst.
from research_assistant.screeners import sector_rotation as sector_rotation  # noqa: E402, F401
