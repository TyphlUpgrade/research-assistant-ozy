"""Phase A deterministic substrate — deep-read surfaces.

Today: Agent D (InsiderBehaviorDetail) extending the existing aggregate
InsiderActivitySummary with per-insider 10b5-1 status, discretionary-S
vs scheduled-S split, and officer-role functional mapping.

Phase D will add Agent A (deep filing reader) here once gated by Phase C's
falsifiability harness. See `.omc/specs/research-deep-substrate-v1.md`.
"""
from research_assistant.deep_reads.agent_d import (
    InsiderBehaviorDetail,
    PerInsiderDetail,
    build_insider_behavior_detail,
)

__all__ = [
    "InsiderBehaviorDetail",
    "PerInsiderDetail",
    "build_insider_behavior_detail",
]
