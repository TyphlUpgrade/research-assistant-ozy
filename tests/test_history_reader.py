"""Tests for `research_assistant.history.reader` (FOLLOWUPS #21).

The reader's job is to project both write paths (brief journal + research
ledger) into a single chronological history view. Tests cover the four
shapes: journal-only, ledger-only, both, neither — plus the parser-level
verdict extraction and prefix-stripping invariants.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    write_dossier_atomic,
)
from research_assistant.history import (
    TraceEnrichment,
    read_chain_enrichment,
    read_unified_history,
)
from research_assistant.history.reader import (
    KNOWN_VERDICTS,
    Stage2HistoryEntry,
    _extract_verdict,
    _strip_verdict_prefix,
)
from research_assistant.history.trace_reader import _chain_id_to_trace_path
from research_assistant.journal import append_stage2_note
from research_assistant.orchestrator import Stage2Note


def _seed_trace(
    traces_base: Path,
    chain_id: str,
    *,
    stage_2_conviction: float | None = None,
    stage_3_adjusted: float | None = None,
    decision_tag: str | None = None,
) -> None:
    """Write a minimal per-chain trace file with Stage 2 + Stage 3 events."""
    import json
    path = _chain_id_to_trace_path(chain_id, traces_base)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    events = []
    if stage_2_conviction is not None or decision_tag is not None:
        events.append({
            "stage_id": "stage_2_thesis",
            "chain_id": chain_id,
            "parsed": {
                "conviction_score": stage_2_conviction,
                "decision_tag": decision_tag,
            },
        })
    if stage_3_adjusted is not None:
        events.append({
            "stage_id": "stage_3_skeptic",
            "chain_id": chain_id,
            "parsed": {"adjusted_score": stage_3_adjusted},
        })
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stage2_note(
    ticker: str,
    *,
    composite: float = 0.5,
    verdict: str = "AGREE",
    bull: str = "bull anchor",
    bear: str = "bear anchor",
) -> Stage2Note:
    """Build a minimal Stage2Note for journal-write tests."""
    return Stage2Note(
        ticker=ticker,
        observation=(),
        bull_anchor=bull,
        bear_anchor=bear,
        what_would_change=(),
        conviction={"technical": 0.5, "fundamental": 0.5, "catalyst": 0.5, "regime": 0.5},
        composite_conviction=composite,
        decision_tag="RESEARCH",
        skeptic_verdict=verdict,
    )


def _seed_ledger(
    base: Path,
    ticker: str,
    chain_id: str,
    timestamp: str,
    *,
    thesis_text: str,
    skeptic_text: str,
) -> None:
    """Write one thesis+skeptic ledger pair under the given chain_id."""
    dossier = Dossier(
        symbol=ticker,
        ledger=[
            LedgerEntry(
                timestamp=timestamp, kind="thesis",
                summary=thesis_text, evidence_anchor=chain_id,
            ),
            LedgerEntry(
                timestamp=timestamp, kind="skeptic",
                summary=skeptic_text, evidence_anchor=chain_id,
            ),
        ],
    )
    write_dossier_atomic(dossier, base)


# ---------------------------------------------------------------------------
# Parser invariants
# ---------------------------------------------------------------------------

class TestVerdictExtraction:
    def test_recognizes_all_known_verdicts(self):
        for verdict in KNOWN_VERDICTS:
            summary = f"Verdict: {verdict}. The thesis pillar is dismantled."
            assert _extract_verdict(summary) == verdict

    def test_missing_prefix_returns_none(self):
        assert _extract_verdict("The thesis under-weights the dilution risk.") is None

    def test_empty_summary_returns_none(self):
        assert _extract_verdict("") is None
        assert _extract_verdict(None) is None  # type: ignore[arg-type]

    def test_unknown_verdict_word_returns_none(self):
        # Defends against future prompt drift introducing a verdict the
        # reader hasn't been taught yet — fail closed rather than emit
        # a fabricated label downstream.
        assert _extract_verdict("Verdict: INVALIDATE. Critique...") is None

    def test_leading_whitespace_tolerated(self):
        assert _extract_verdict("   Verdict: TEMPER. body") == "TEMPER"


class TestVerdictPrefixStripping:
    def test_strips_known_prefix(self):
        text = "Verdict: CHALLENGE. The thesis pillar is dismantled by X."
        assert _strip_verdict_prefix(text) == "The thesis pillar is dismantled by X."

    def test_passthrough_when_no_prefix(self):
        text = "The thesis correctly identifies the risk."
        assert _strip_verdict_prefix(text) == text

    def test_empty_string(self):
        assert _strip_verdict_prefix("") == ""

    def test_strips_with_extra_whitespace(self):
        text = "Verdict:  WEAKEN.   body text"
        assert _strip_verdict_prefix(text) == "body text"


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

class TestReadUnifiedHistory:
    def test_empty_returns_empty_list(self, tmp_path: Path):
        assert read_unified_history("FAKE", tmp_path) == []

    def test_journal_only(self, tmp_path: Path):
        note = _make_stage2_note("AAPL", composite=0.42, verdict="WEAKEN")
        append_stage2_note(note, tmp_path)

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        entry = history[0]
        assert isinstance(entry, Stage2HistoryEntry)
        assert entry.ticker == "AAPL"
        assert entry.source == "brief"
        assert entry.composite_conviction == pytest.approx(0.42)
        assert entry.skeptic_verdict == "WEAKEN"
        assert entry.decision_tag == "RESEARCH"
        assert entry.bull_anchor == "bull anchor"
        assert entry.bear_anchor == "bear anchor"
        # journal schema doesn't carry chain_id today
        assert entry.chain_id is None

    def test_ledger_only(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "AAPL",
            chain_id="20260601T120000-abc123",
            timestamp="2026-06-01T16:00:00+00:00",
            thesis_text="Apple has staged a 20% rally on services growth.",
            skeptic_text="Verdict: CHALLENGE. Services growth is decelerating.",
        )

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        entry = history[0]
        assert entry.source == "research"
        assert entry.chain_id == "20260601T120000-abc123"
        assert entry.skeptic_verdict == "CHALLENGE"
        # Composite conviction is unrecoverable from the ledger text
        assert entry.composite_conviction is None
        assert entry.bull_anchor == "Apple has staged a 20% rally on services growth."
        # Verdict prefix is stripped from the bear column
        assert entry.bear_anchor == "Services growth is decelerating."

    def test_union_orders_by_asof_then_recorded_at(self, tmp_path: Path):
        # Older ledger entry
        _seed_ledger(
            tmp_path, "AAPL",
            chain_id="20260530T100000-old",
            timestamp="2026-05-30T14:00:00+00:00",
            thesis_text="Old thesis",
            skeptic_text="Verdict: WEAKEN. Old critique.",
        )
        # Newer journal entry
        note = _make_stage2_note("AAPL", composite=0.6, verdict="AGREE")
        append_stage2_note(note, tmp_path)

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 2
        # Ledger entry has older asof (2026-05-30) so comes first.
        assert history[0].source == "research"
        assert history[1].source == "brief"

    def test_orphan_thesis_without_skeptic_still_emitted(self, tmp_path: Path):
        # An orphan can happen if a write failed between the two ledger
        # appends. The reader still surfaces the thesis so the operator
        # sees the partial read instead of silently dropping it.
        dossier = Dossier(
            symbol="AAPL",
            ledger=[
                LedgerEntry(
                    timestamp="2026-06-01T12:00:00+00:00", kind="thesis",
                    summary="Thesis only", evidence_anchor="chain-orphan",
                ),
            ],
        )
        write_dossier_atomic(dossier, tmp_path)

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        assert history[0].chain_id == "chain-orphan"
        assert history[0].skeptic_verdict is None
        assert history[0].bull_anchor == "Thesis only"
        assert history[0].bear_anchor is None

    def test_ignores_non_thesis_non_skeptic_ledger_kinds(self, tmp_path: Path):
        # `/probe` and `manual:` entries also live in the ledger but should
        # NOT appear in the Stage 2 trajectory — they're a different event
        # class.
        dossier = Dossier(
            symbol="AAPL",
            ledger=[
                LedgerEntry(
                    timestamp="2026-06-01T12:00:00+00:00", kind="probe",
                    summary="Probed: foo", evidence_anchor="probe-chain",
                ),
                LedgerEntry(
                    timestamp="2026-06-01T13:00:00+00:00", kind="manual",
                    summary="Operator note", evidence_anchor="manual-chain",
                ),
                LedgerEntry(
                    timestamp="2026-06-01T14:00:00+00:00", kind="thesis",
                    summary="Real thesis", evidence_anchor="real-chain",
                ),
                LedgerEntry(
                    timestamp="2026-06-01T14:00:00+00:00", kind="skeptic",
                    summary="Verdict: TEMPER. Real critique.",
                    evidence_anchor="real-chain",
                ),
            ],
        )
        write_dossier_atomic(dossier, tmp_path)

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        assert history[0].chain_id == "real-chain"
        assert history[0].skeptic_verdict == "TEMPER"

    def test_ticker_is_uppercased_at_read(self, tmp_path: Path):
        note = _make_stage2_note("AAPL")
        append_stage2_note(note, tmp_path)

        history = read_unified_history("aapl", tmp_path)
        assert len(history) == 1
        assert history[0].ticker == "AAPL"

    def test_skeptic_summary_without_verdict_prefix_returns_unavailable(
        self, tmp_path: Path,
    ):
        # Pre-prompt-format-update reads in the ledger don't have the
        # `Verdict: X.` header. The reader emits None for the verdict
        # rather than fabricating one — the trajectory renderer displays
        # `UNAVAILABLE` in that case.
        _seed_ledger(
            tmp_path, "AAPL",
            chain_id="early-format",
            timestamp="2026-05-15T12:00:00+00:00",
            thesis_text="Early-format thesis.",
            skeptic_text="The thesis under-weights the dilution risk.",
        )

        history = read_unified_history("AAPL", tmp_path)
        assert len(history) == 1
        assert history[0].skeptic_verdict is None
        # bear_anchor passes through unchanged when there's no prefix
        assert history[0].bear_anchor == "The thesis under-weights the dilution risk."

    def test_ledger_entry_enriched_from_trace(self, tmp_path: Path):
        chain_id = "20260601T120000-aaa111"
        _seed_ledger(
            tmp_path, "AAPL",
            chain_id=chain_id,
            timestamp="2026-06-01T16:00:00+00:00",
            thesis_text="Apple thesis.",
            skeptic_text="Verdict: CHALLENGE. Critique.",
        )
        _seed_trace(
            tmp_path / "traces", chain_id,
            stage_2_conviction=0.48,
            stage_3_adjusted=0.31,
            decision_tag="RESEARCH",
        )

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        entry = history[0]
        assert entry.composite_conviction == pytest.approx(0.31)
        assert entry.pre_skeptic_conviction == pytest.approx(0.48)
        assert entry.decision_tag == "RESEARCH"
        assert entry.skeptic_verdict == "CHALLENGE"

    def test_ledger_entry_with_missing_trace_keeps_none_fields(
        self, tmp_path: Path,
    ):
        # No trace file written — research entry should still surface,
        # just with conviction fields None (the operator sees `?`).
        _seed_ledger(
            tmp_path, "AAPL",
            chain_id="20260601T120000-bbb222",
            timestamp="2026-06-01T16:00:00+00:00",
            thesis_text="Apple thesis.",
            skeptic_text="Verdict: TEMPER. Critique.",
        )

        history = read_unified_history("AAPL", tmp_path)

        assert len(history) == 1
        entry = history[0]
        assert entry.composite_conviction is None
        assert entry.pre_skeptic_conviction is None
        assert entry.decision_tag is None
        # Verdict still extractable from ledger text
        assert entry.skeptic_verdict == "TEMPER"


class TestTraceReader:
    def test_missing_file_returns_empty_enrichment(self, tmp_path: Path):
        result = read_chain_enrichment("20260601T120000-nofile", tmp_path)
        assert result == TraceEnrichment()

    def test_malformed_chain_id_returns_empty(self, tmp_path: Path):
        # Hand-edited or pre-format ledger anchors that don't match the
        # YYYYMMDDTHHMMSS-XXXXXX shape shouldn't crash — they just yield
        # an empty enrichment.
        assert read_chain_enrichment("not-a-chain-id", tmp_path) == TraceEnrichment()
        assert read_chain_enrichment("", tmp_path) == TraceEnrichment()

    def test_full_enrichment_recovered(self, tmp_path: Path):
        chain_id = "20260601T120000-fullok"
        _seed_trace(
            tmp_path, chain_id,
            stage_2_conviction=0.55,
            stage_3_adjusted=0.40,
            decision_tag="RESEARCH",
        )

        result = read_chain_enrichment(chain_id, tmp_path)

        assert result.pre_skeptic_conviction == pytest.approx(0.55)
        assert result.adjusted_score == pytest.approx(0.40)
        assert result.decision_tag == "RESEARCH"

    def test_partial_trace_missing_stage_3(self, tmp_path: Path):
        # A chain that wrote Stage 2 but failed before Stage 3 (rare but
        # possible on a crash mid-cascade). Stage 2 fields recovered;
        # adjusted_score stays None.
        chain_id = "20260601T120000-partial"
        _seed_trace(
            tmp_path, chain_id,
            stage_2_conviction=0.55,
            stage_3_adjusted=None,
        )

        result = read_chain_enrichment(chain_id, tmp_path)

        assert result.pre_skeptic_conviction == pytest.approx(0.55)
        assert result.adjusted_score is None

    def test_trace_with_corrupt_lines_ignored(self, tmp_path: Path):
        # Half-flushed write or operator-edited trace shouldn't kill the
        # reader. Corrupt JSON lines are skipped; valid lines still read.
        chain_id = "20260601T120000-corrupt"
        path = _chain_id_to_trace_path(chain_id, tmp_path)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not-json\n"
            '{"stage_id": "stage_3_skeptic", "parsed": {"adjusted_score": 0.42}}\n'
            "{also not json\n"
        )

        result = read_chain_enrichment(chain_id, tmp_path)
        assert result.adjusted_score == pytest.approx(0.42)
