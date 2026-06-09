"""
Phase A — Agent D (InsiderBehaviorDetail).

Extends the aggregate `InsiderActivitySummary` with the per-insider detail
the spec calls out: 10b5-1 plan presence per filing, discretionary-S vs
scheduled-S transaction split, and officer-role to functional-area mapping.

Implementation strategy:
  - Re-uses already-fetched `list[Form4Filing]` from
    `research_assistant.edgar.form4` — no new EDGAR HTTP calls.
  - 10b5-1 detection: heuristic scan of filing XML text. Yfinance's Form 4
    schema doesn't expose a structured `10b5-1` boolean; the indicator is
    typically encoded in the free-text footnote section ("Sale executed
    pursuant to a Rule 10b5-1 trading plan adopted on..."). We resolve this
    in the loader by attaching `mentions_10b5_1` to the Form4Filing at fetch
    time (see `_classify_10b5_1`).
  - Discretionary-S vs scheduled-S split: a sale code-S is classified
    "scheduled" when the filing mentions 10b5-1, "discretionary" otherwise.
    This is the swing-trade-relevant distinction — a discretionary cluster
    is alpha, a 10b5-1 cluster is noise.
  - Officer-role functional mapping: a small keyword map over the
    `officer_title` field. Maps "CEO", "CFO", "COO" etc. to a coarse
    functional area ("c-suite", "finance", "operations", "tech", "gtm").
    The map is intentionally coarse — Stage 2's thesis writer doesn't
    need surgical fidelity, just "which functional area is selling".

Three-state semantics:
  - None        → no Form 4 fetch attempted, or filings list was None
  - empty       → fetch succeeded but no filings in window
  - populated   → render_for_prompt emits the compressed block
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from research_assistant.edgar.form4 import (
    Form4Filing,
    InsiderActivitySummary,
    _BUY_CODE,
    _SALE_CODE,
)

log = logging.getLogger(__name__)


_10b5_1_PATTERN = re.compile(
    r"\b(10b5[-\s]?1|rule\s*10b5[-\s]?1|trading\s+plan)\b",
    re.IGNORECASE,
)


# Officer-role keyword map. Order matters: more-specific titles (CFO, COO)
# match before "Chief" generics so "Chief Financial Officer" goes to
# "finance" not "c-suite". Lowercase comparison.
_FUNCTIONAL_AREA_MAP: list[tuple[str, str]] = [
    ("chief financial", "finance"),
    ("chief accounting", "finance"),
    ("treasurer", "finance"),
    ("controller", "finance"),
    ("cfo", "finance"),
    ("chief operating", "operations"),
    ("coo", "operations"),
    ("chief technology", "tech"),
    ("chief information", "tech"),
    ("cto", "tech"),
    ("cio", "tech"),
    ("chief science", "tech"),
    ("chief technical", "tech"),
    ("chief executive", "c-suite"),
    ("ceo", "c-suite"),
    ("president", "c-suite"),
    ("chief commercial", "gtm"),
    ("chief marketing", "gtm"),
    ("chief revenue", "gtm"),
    ("chief sales", "gtm"),
    ("chief business", "gtm"),
    ("cmo", "gtm"),
    ("cro", "gtm"),
    ("chief legal", "legal"),
    ("general counsel", "legal"),
    ("chief", "c-suite"),  # generic Chief fallback (last)
    ("director", "board"),
]


@dataclass
class PerInsiderDetail:
    """One insider's behavior detail over the aggregation window."""
    cik: str
    name: str
    officer_title: Optional[str]
    functional_area: str               # "c-suite" / "finance" / "tech" / "gtm" / "board" / "other"
    discretionary_sales_count: int = 0
    scheduled_sales_count: int = 0     # sales under 10b5-1 (by filing-level heuristic)
    buys_count: int = 0
    discretionary_net_dollars: float = 0.0
    scheduled_net_dollars: float = 0.0
    latest_transaction_date: Optional[str] = None

    @property
    def total_sales(self) -> int:
        return self.discretionary_sales_count + self.scheduled_sales_count


@dataclass
class InsiderBehaviorDetail:
    """Per-insider extension of the aggregate insider summary.

    The aggregate `InsiderActivitySummary` answers "what was the net flow
    on this ticker?" This dataclass answers "WHO did it, under what
    program, and from which functional area?" The Stage 2 thesis writer
    uses this to distinguish a coordinated discretionary distribution
    (alpha-bearing) from a pre-scheduled 10b5-1 rotation (noise).
    """
    symbol: str
    window_days: int
    window_start: str
    window_end: str
    by_insider: list[PerInsiderDetail] = field(default_factory=list)
    # Aggregate roll-ups across all insiders
    discretionary_sales_count: int = 0
    scheduled_sales_count: int = 0
    discretionary_net_dollars: float = 0.0
    scheduled_net_dollars: float = 0.0
    # Functional-area breakdown — sum of discretionary_net_dollars per area
    functional_area_net_dollars: dict[str, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.by_insider

    def stage_2_block(self) -> str:
        lines: list[str] = []

        # Headline: discretionary vs scheduled split
        if self.discretionary_sales_count or self.scheduled_sales_count:
            lines.append(
                f"sales split last {self.window_days}d: "
                f"discretionary {self.discretionary_sales_count} "
                f"({_fmt_dollars(self.discretionary_net_dollars)}) / "
                f"scheduled-10b5-1 {self.scheduled_sales_count} "
                f"({_fmt_dollars(self.scheduled_net_dollars)})"
            )

        # Functional area lean — only render areas with material activity.
        if self.functional_area_net_dollars:
            ordered = sorted(
                self.functional_area_net_dollars.items(),
                key=lambda kv: -abs(kv[1]),
            )
            parts = [
                f"{area} {_fmt_dollars(net)}"
                for area, net in ordered if abs(net) >= 1e5
            ]
            if parts:
                lines.append("functional lean (discretionary): " + ", ".join(parts))

        # Per-insider top 3 by discretionary net $ magnitude
        ordered_insiders = sorted(
            self.by_insider,
            key=lambda p: -abs(p.discretionary_net_dollars),
        )[:3]
        for p in ordered_insiders:
            if p.discretionary_net_dollars == 0 and p.scheduled_sales_count == 0:
                continue
            program = []
            if p.discretionary_sales_count:
                program.append(
                    f"{p.discretionary_sales_count} discretionary "
                    f"{_fmt_dollars(p.discretionary_net_dollars)}"
                )
            if p.scheduled_sales_count:
                program.append(
                    f"{p.scheduled_sales_count} 10b5-1 "
                    f"{_fmt_dollars(p.scheduled_net_dollars)}"
                )
            if p.buys_count:
                program.append(f"{p.buys_count} buys")
            role = p.officer_title or p.functional_area
            lines.append(
                f"- {p.name} ({role}, {p.functional_area}): "
                + "; ".join(program)
            )

        return "\n".join(lines) if lines else "(no insider activity in window)"

    @classmethod
    def render_for_prompt(cls, detail: Optional["InsiderBehaviorDetail"]) -> str:
        if detail is None:
            return (
                "(insider behavior detail unavailable — Form 4 fetch failed)"
            )
        if detail.is_empty:
            return (
                f"(no insider activity last {detail.window_days}d)"
            )
        return detail.stage_2_block()


# ---------------------------------------------------------------------------
# Build path
# ---------------------------------------------------------------------------

def build_insider_behavior_detail(
    filings: list[Form4Filing],
    *,
    symbol: str,
    window_days: int = 90,
    as_of: Optional[date] = None,
) -> InsiderBehaviorDetail:
    """Build InsiderBehaviorDetail from already-fetched Form 4 filings.

    The 10b5-1 classification reads `Form4Filing.mentions_10b5_1` if
    present (set at fetch time when the loader scans filing footnotes).
    When the attribute is missing (older parse path), the filing is
    treated as discretionary — conservative default that surfaces
    distribution as alpha-relevant rather than silently mark-scheduling
    it as noise.
    """
    as_of = as_of or date.today()
    window_start = (as_of - timedelta(days=window_days)).isoformat()
    window_end = as_of.isoformat()

    detail = InsiderBehaviorDetail(
        symbol=symbol.upper(),
        window_days=window_days,
        window_start=window_start,
        window_end=window_end,
    )

    per_cik: dict[str, PerInsiderDetail] = {}

    for f in filings:
        anchor = f.period_of_report or f.filing_date
        if not anchor or not (window_start <= anchor <= window_end):
            continue
        owner = f.primary_owner
        if owner is None:
            continue
        # Default to discretionary when the attribute isn't set — preserves
        # backward-compat with Form4Filing instances that pre-date the
        # mentions_10b5_1 wiring.
        is_scheduled = bool(getattr(f, "mentions_10b5_1", False))

        title = owner.officer_title
        functional = _classify_functional_area(title, owner)
        rec = per_cik.setdefault(
            owner.cik,
            PerInsiderDetail(
                cik=owner.cik,
                name=owner.name,
                officer_title=title,
                functional_area=functional,
            ),
        )

        for t in f.non_derivative:
            if not t.code:
                continue
            tx_value = t.net_dollars
            if t.code == _BUY_CODE:
                rec.buys_count += 1
            elif t.code == _SALE_CODE:
                if is_scheduled:
                    rec.scheduled_sales_count += 1
                    rec.scheduled_net_dollars += tx_value
                    detail.scheduled_sales_count += 1
                    detail.scheduled_net_dollars += tx_value
                else:
                    rec.discretionary_sales_count += 1
                    rec.discretionary_net_dollars += tx_value
                    detail.discretionary_sales_count += 1
                    detail.discretionary_net_dollars += tx_value
                    detail.functional_area_net_dollars[functional] = (
                        detail.functional_area_net_dollars.get(functional, 0.0)
                        + tx_value
                    )
            # Other codes (A grants, M exercises, F withholding) intentionally
            # not bucketed — they're noise for the behavior-detail surface.
            if t.date and (
                rec.latest_transaction_date is None
                or t.date > rec.latest_transaction_date
            ):
                rec.latest_transaction_date = t.date

    detail.by_insider = list(per_cik.values())
    return detail


def derive_from_summary(
    summary: Optional[InsiderActivitySummary],
    filings: Optional[list[Form4Filing]],
    *,
    symbol: str,
    as_of: Optional[date] = None,
) -> Optional[InsiderBehaviorDetail]:
    """Convenience: build detail from a (summary, filings) pair.

    Returns None when both inputs are None (fetch failure carries through).
    Returns an empty-but-populated InsiderBehaviorDetail when summary
    exists but filings is None/empty — keeps the three-state semantics
    consistent with the other Phase A surfaces.
    """
    if summary is None and not filings:
        return None
    if filings is None:
        filings = []
    window_days = summary.window_days if summary else 90
    return build_insider_behavior_detail(
        filings, symbol=symbol, window_days=window_days, as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_functional_area(title: Optional[str], owner) -> str:
    """Map an officer title to a coarse functional area.

    Falls back through: officer_title keyword match → directorship → "other".
    """
    if title:
        t = title.lower()
        for kw, area in _FUNCTIONAL_AREA_MAP:
            if kw in t:
                return area
    if getattr(owner, "is_director", False):
        return "board"
    if getattr(owner, "is_ten_percent_owner", False):
        return "10%-owner"
    return "other"


def detect_10b5_1(xml_text: str) -> bool:
    """Heuristic 10b5-1 detection.

    Form 4 schema doesn't encode 10b5-1 as a structured boolean; it lives
    in the free-text footnote section. Pattern is broad ("10b5-1" or
    "Rule 10b5-1" or "trading plan") to catch the major encoding variants.
    False positives are rare in practice — Form 4 footnotes are short.
    """
    if not xml_text:
        return False
    return bool(_10b5_1_PATTERN.search(xml_text))


def _fmt_dollars(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.0f}"
