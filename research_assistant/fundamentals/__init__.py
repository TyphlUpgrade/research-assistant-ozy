"""Phase A deterministic substrate — fundamentals.

Multi-year KPI extraction + earnings calendar from yfinance. Used by Stage 2
thesis prompt as the {fundamentals_block} and {earnings_calendar_block}
substrate. See `.omc/specs/research-deep-substrate-v1.md` Phase A.
"""
from research_assistant.fundamentals.kpis import (
    EarningsCalendar,
    KPISummary,
    load_earnings_calendar,
    load_kpi_summary,
)

__all__ = [
    "EarningsCalendar",
    "KPISummary",
    "load_earnings_calendar",
    "load_kpi_summary",
]
