"""Phase A deterministic substrate — market positioning.

Options-market and analyst-revision signal surfaces. Used by Stage 2 thesis
prompt as the {options_positioning_block} and {analyst_revisions_block}
substrate. See `.omc/specs/research-deep-substrate-v1.md` Phase A.
"""
from research_assistant.positioning.options import (
    OptionsPositioning,
    load_options_positioning,
)
from research_assistant.positioning.revisions import (
    AnalystRevisions,
    load_analyst_revisions,
)

__all__ = [
    "AnalystRevisions",
    "OptionsPositioning",
    "load_analyst_revisions",
    "load_options_positioning",
]
