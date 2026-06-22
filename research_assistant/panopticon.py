"""
Phase C — panopticon replay + falsifiability gate (#28).

The Stage 1.7 synthesizer ships behind `STAGE_1_7_SYNTHESIZER=on` and stays
flag-off until its flags are shown to be *right* on held-out trades. This
module is that validation step: it reads the kept/dropped flag detail the
orchestrator already persists to each `stage_1_7_synthesizer` trace event,
joins a falsifiability **decoy arm** (control tickers a contrarian flag should
NOT fire on — every flag there is a false positive), and emits a hand-grading
sheet + a PASS / FAIL / INCOMPLETE gate verdict that is the GO/NO-GO for
flipping the synthesizer default on.

No LLM calls and no network here — it only reads traces already on disk.
Forward-return annotation is optional and injected (the CLI can wire
scoreboard's cached enrichment); tests run fully offline.

Trace event shape (from trace_renderer.append_stage_event):
    {stage_id, chain_id, timestamp, symbol, parsed: {
        panopticon_degraded: bool,
        flags: {kept: [flag,...], dropped: [flag,...]},
        ...}}
where each flag is SynthesisFlag.to_verification_dict().
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

STAGE_ID = "stage_1_7_synthesizer"

# Gate defaults — a flag is only trustworthy enough to default-on if it rarely
# fires on clean controls AND grades out mostly correct on real trades.
DEFAULT_MAX_DECOY_FP_RATE = 0.10
DEFAULT_MIN_PRECISION = 0.70

# Returns provider: (ticker, iso_date) -> {"5d": float|None, ...} | None.
ReturnsProvider = Callable[[str, str], Optional[dict]]


@dataclass
class FlagRecord:
    """One kept synthesizer flag, flattened for grading."""
    ticker: str
    chain_id: str
    date: str                       # ISO date of the trace event
    severity: str
    observation_type: str
    shallow_source: str
    shallow_observation: str
    deep_source: str
    deep_observation: str
    verifier_reasoning: str
    is_decoy: bool = False
    returns: Optional[dict] = None  # {"5d":..,"10d":..,"30d":..} when enriched
    grade: Optional[str] = None     # CORRECT | WRONG | UNCLEAR (post hand-grade)

    @property
    def row_id(self) -> str:
        """Stable id for round-tripping a graded sheet back to precision."""
        return f"{self.chain_id[:14]}:{self.ticker}:{self.observation_type}"


@dataclass
class GateResult:
    status: str                     # PASS | FAIL | INCOMPLETE
    n_events: int
    n_degraded_excluded: int
    n_kept: int
    n_dropped: int
    decoy_tickers_present: list[str]
    decoy_fp_count: int             # decoy tickers with >=1 kept flag
    decoy_fp_rate: Optional[float]  # None when no decoys were in the traces
    precision: Optional[float]      # None until flags are hand-graded
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def iter_synthesizer_events(traces_base: Path) -> Iterator[dict]:
    """Yield every parsed `stage_1_7_synthesizer` event under traces_base."""
    for path in sorted(traces_base.rglob("*.jsonl")):
        try:
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("stage_id") == STAGE_ID and event.get("parsed"):
                        yield event
        except OSError:
            continue


def _event_date(event: dict) -> str:
    ts = event.get("timestamp") or ""
    return ts[:10]  # ISO YYYY-MM-DD prefix; "" if absent


def collect_flag_records(
    traces_base: Path,
    *,
    decoys: frozenset[str] = frozenset(),
    exclude_degraded: bool = True,
    returns_provider: Optional[ReturnsProvider] = None,
) -> tuple[list[FlagRecord], dict]:
    """Walk traces → (kept-flag records, stats).

    `stats` carries the gate denominators: event count, degraded-excluded
    count, dropped-flag count, and the set of tickers actually synthesized
    (so the decoy FP rate is measured over decoys that were really run, not
    the whole configured list).
    """
    decoys = frozenset(d.upper() for d in decoys)
    records: list[FlagRecord] = []
    n_events = n_degraded = n_dropped = 0
    tickers_seen: set[str] = set()

    for event in iter_synthesizer_events(traces_base):
        parsed = event["parsed"]
        ticker = (event.get("symbol") or "").upper()
        if parsed.get("panopticon_degraded"):
            n_degraded += 1
            if exclude_degraded:
                continue  # Gate 3 rule (M3): degraded events are not gradeable
        n_events += 1
        if ticker:
            tickers_seen.add(ticker)
        flags = parsed.get("flags") or {}
        n_dropped += len(flags.get("dropped") or [])
        for fl in flags.get("kept") or []:
            rec = FlagRecord(
                ticker=ticker,
                chain_id=event.get("chain_id", ""),
                date=_event_date(event),
                severity=fl.get("severity", "?"),
                observation_type=fl.get("observation_type", "?"),
                shallow_source=fl.get("shallow_source", ""),
                shallow_observation=fl.get("shallow_observation", ""),
                deep_source=fl.get("deep_source", ""),
                deep_observation=fl.get("deep_observation", ""),
                verifier_reasoning=fl.get("verifier_reasoning", ""),
                is_decoy=ticker in decoys,
            )
            if returns_provider is not None and ticker and rec.date:
                try:
                    rec.returns = returns_provider(ticker, rec.date)
                except Exception:
                    rec.returns = None
            records.append(rec)

    stats = {
        "n_events": n_events,
        "n_degraded_excluded": n_degraded if exclude_degraded else 0,
        "n_dropped": n_dropped,
        "tickers_seen": tickers_seen,
        "decoys_present": sorted(decoys & tickers_seen),
    }
    return records, stats


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def compute_gate(
    records: list[FlagRecord],
    stats: dict,
    *,
    decoys: frozenset[str] = frozenset(),
    max_decoy_fp_rate: float = DEFAULT_MAX_DECOY_FP_RATE,
    min_precision: float = DEFAULT_MIN_PRECISION,
) -> GateResult:
    """Verdict: FAIL if decoys trip too often (objective, computable now);
    INCOMPLETE if real-trade flags aren't hand-graded yet; PASS only when
    decoy FP rate is under bound AND graded precision clears the bar."""
    decoys = frozenset(d.upper() for d in decoys)
    decoys_present = stats.get("decoys_present", [])
    fp_tickers = {r.ticker for r in records if r.is_decoy}
    decoy_fp_rate = (len(fp_tickers) / len(decoys_present)) if decoys_present else None

    graded = [r for r in records if r.grade in ("CORRECT", "WRONG")]
    precision = (
        sum(r.grade == "CORRECT" for r in graded) / len(graded) if graded else None
    )

    reasons: list[str] = []
    status = "PASS"
    if decoy_fp_rate is not None and decoy_fp_rate > max_decoy_fp_rate:
        status = "FAIL"
        reasons.append(
            f"decoy false-positive rate {decoy_fp_rate:.0%} > {max_decoy_fp_rate:.0%} "
            f"({len(fp_tickers)}/{len(decoys_present)} control tickers tripped a flag)"
        )
    if decoy_fp_rate is None:
        reasons.append("no decoy tickers present in traces — falsifiability arm unrun")
    if precision is None:
        if status != "FAIL":
            status = "INCOMPLETE"
        reasons.append("kept flags not hand-graded yet — precision unknown")
    elif precision < min_precision:
        status = "FAIL"
        reasons.append(
            f"graded precision {precision:.0%} < {min_precision:.0%} "
            f"({sum(r.grade=='CORRECT' for r in graded)}/{len(graded)} flags correct)"
        )

    if status == "PASS":
        reasons.append(
            f"decoy FP {decoy_fp_rate:.0%} ≤ {max_decoy_fp_rate:.0%} and "
            f"precision {precision:.0%} ≥ {min_precision:.0%}"
        )

    return GateResult(
        status=status,
        n_events=stats.get("n_events", 0),
        n_degraded_excluded=stats.get("n_degraded_excluded", 0),
        n_kept=len(records),
        n_dropped=stats.get("n_dropped", 0),
        decoy_tickers_present=decoys_present,
        decoy_fp_count=len(fp_tickers),
        decoy_fp_rate=decoy_fp_rate,
        precision=precision,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Grading sheet (markdown, round-trippable)
# ---------------------------------------------------------------------------

_GRADE_VALUES = {"CORRECT", "WRONG", "UNCLEAR"}


def _ret_cell(returns: Optional[dict]) -> str:
    if not returns:
        return "—"
    parts = []
    for h in ("5d", "10d", "30d"):
        v = returns.get(h)
        parts.append(f"{v*100:+.0f}%" if isinstance(v, (int, float)) else "·")
    return "/".join(parts)


def render_grading_sheet(records: list[FlagRecord]) -> str:
    """One row per kept flag with a blank GRADE column for a human. The
    leading id column lets parse_graded_sheet() read grades back."""
    lines = [
        "# Panopticon Phase C — flag grading sheet",
        "",
        "Grade each kept flag in the **GRADE** column: `CORRECT` (the observed "
        "divergence really mattered for the trade), `WRONG` (noise / misleading), "
        "or `UNCLEAR`. Decoy rows are control tickers — any flag there is a "
        "false positive, grade `WRONG`. Leave other columns untouched.",
        "",
        "| id | ticker | decoy | date | sev | observation | anchors | verifier note | fwd 5/10/30 | GRADE |",
        "|----|--------|-------|------|-----|-------------|---------|---------------|-------------|-------|",
    ]
    for r in records:
        obs = f"{r.shallow_observation} → {r.deep_observation}".replace("|", "/")
        anchors = f"{r.shallow_source} / {r.deep_source}".replace("|", "/")
        note = (r.verifier_reasoning or "").replace("|", "/")
        lines.append(
            f"| {r.row_id} | {r.ticker} | {'Y' if r.is_decoy else ''} | {r.date} "
            f"| {r.severity} | {obs[:120]} | {anchors[:60]} | {note[:100]} "
            f"| {_ret_cell(r.returns)} |  |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_graded_sheet(text: str) -> dict[str, str]:
    """Read a filled grading sheet back into {row_id: GRADE}. Ignores blank
    and non-grade cells so a partially-graded sheet is fine."""
    grades: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("id", "----"):
            continue
        grade = cells[-1].upper()
        if grade in _GRADE_VALUES:
            grades[cells[0]] = grade
    return grades


def apply_grades(records: list[FlagRecord], grades: dict[str, str]) -> int:
    """Attach grades to records by row_id. Returns count applied."""
    n = 0
    for r in records:
        g = grades.get(r.row_id)
        if g:
            r.grade = g
            n += 1
    return n


def render_gate_summary(gate: GateResult) -> str:
    """Compact human verdict for the CLI."""
    lines = [
        f"Panopticon gate: {gate.status}",
        f"  events graded:   {gate.n_events}  (excluded {gate.n_degraded_excluded} degraded)",
        f"  kept flags:      {gate.n_kept}  ·  dropped (failed verify): {gate.n_dropped}",
    ]
    if gate.decoy_tickers_present:
        rate = f"{gate.decoy_fp_rate:.0%}" if gate.decoy_fp_rate is not None else "n/a"
        lines.append(
            f"  decoy arm:       {gate.decoy_fp_count}/{len(gate.decoy_tickers_present)} "
            f"controls tripped ({rate})  [{', '.join(gate.decoy_tickers_present)}]"
        )
    else:
        lines.append("  decoy arm:       no control tickers in traces")
    lines.append(
        f"  precision:       {gate.precision:.0%}" if gate.precision is not None
        else "  precision:       (awaiting hand-grades)"
    )
    for reason in gate.reasons:
        lines.append(f"  · {reason}")
    return "\n".join(lines)
