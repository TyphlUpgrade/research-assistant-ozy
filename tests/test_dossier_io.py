"""
Smoke tests for dossier_io.py:
- Roundtrip: write → read returns equal Dossier
- Ledger append-only: removing/modifying entries raises ValueError
- Atomic write: orphan tmp cleanup on validator failure
- Migrator idempotency: migrating twice produces byte-identical output
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    append_ledger_entry,
    read_dossier,
    write_dossier_atomic,
    _migrate_v0_to_v1,
    _validate_ledger_append_only,
)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path


def test_roundtrip_write_read(base: Path) -> None:
    d = Dossier(
        symbol="NVDA",
        conviction=0.62,
        state_md="Data-center growth +18% QoQ.",
        open_questions=["Hopper→Blackwell transition smoothness?"],
        ledger=[
            LedgerEntry(
                timestamp="2026-05-14T10:42:00Z",
                kind="probe",
                summary="10-Q segment breakdown",
                evidence_anchor="tool_call_abc123",
            ),
        ],
    )
    write_dossier_atomic(d, base)
    loaded = read_dossier("NVDA", base)
    assert loaded is not None
    assert loaded.symbol == "NVDA"
    assert loaded.conviction == 0.62
    assert "Data-center growth" in loaded.state_md
    assert len(loaded.open_questions) == 1
    assert len(loaded.ledger) == 1
    assert loaded.ledger[0].evidence_anchor == "tool_call_abc123"


def test_ledger_append_only_rejects_mutation(base: Path) -> None:
    d1 = Dossier(
        symbol="TSLA",
        ledger=[
            LedgerEntry(timestamp="2026-05-14T10:00:00Z", kind="probe", summary="initial"),
            LedgerEntry(timestamp="2026-05-14T11:00:00Z", kind="skeptic", summary="second"),
        ],
    )
    write_dossier_atomic(d1, base)

    # Attempt to remove a ledger entry
    d2 = Dossier(symbol="TSLA", ledger=[d1.ledger[0]])  # dropped the second entry
    with pytest.raises(ValueError, match="cannot be removed"):
        write_dossier_atomic(d2, base)

    # Attempt to mutate a historical entry
    mutated = LedgerEntry(timestamp="2026-05-14T10:00:00Z", kind="probe", summary="EDITED")
    d3 = Dossier(symbol="TSLA", ledger=[mutated, d1.ledger[1]])
    with pytest.raises(ValueError, match="append-only"):
        write_dossier_atomic(d3, base)


def test_atomic_write_no_orphan_tmp_on_validator_failure(base: Path) -> None:
    d1 = Dossier(
        symbol="AAPL",
        ledger=[LedgerEntry(timestamp="2026-05-14T10:00:00Z", kind="probe", summary="x")],
    )
    write_dossier_atomic(d1, base)

    # Attempt invalid write — validator should reject BEFORE tmp file is created
    d2 = Dossier(symbol="AAPL", ledger=[])  # removed entries
    with pytest.raises(ValueError):
        write_dossier_atomic(d2, base)

    # Assert no orphan .AAPL.*.md.tmp files in tickers/
    tickers_dir = base / "tickers"
    orphans = list(tickers_dir.glob(".AAPL.*.md.tmp"))
    assert orphans == [], f"Orphan tmp files: {orphans}"


def test_migrator_v0_to_v1_idempotent() -> None:
    """Running the migrator twice produces byte-identical output."""
    raw_fm: dict = {"schema_version": 0, "symbol": "MSFT"}
    body = "## State\n\nstuff\n"
    fm1, body1 = _migrate_v0_to_v1(raw_fm.copy(), body)
    # Second run with already-migrated input should be inert
    fm2, body2 = _migrate_v0_to_v1(fm1.copy(), body1)
    assert fm1 == fm2
    assert body1 == body2
    assert fm1["schema_version"] == 1
    assert fm1["conviction"] is None


def test_append_ledger_entry_convenience(base: Path) -> None:
    append_ledger_entry(
        "GOOGL",
        LedgerEntry(timestamp="2026-05-14T12:00:00Z", kind="probe", summary="first probe"),
        base,
    )
    append_ledger_entry(
        "GOOGL",
        LedgerEntry(timestamp="2026-05-14T13:00:00Z", kind="skeptic", summary="risk flagged"),
        base,
    )
    d = read_dossier("GOOGL", base)
    assert d is not None
    assert len(d.ledger) == 2
    assert d.ledger[0].summary == "first probe"
    assert d.ledger[1].summary == "risk flagged"


def test_validator_allows_pure_append() -> None:
    """Direct test of the validator function."""
    prev = [
        LedgerEntry(timestamp="t1", kind="probe", summary="a"),
        LedgerEntry(timestamp="t2", kind="skeptic", summary="b"),
    ]
    new = prev + [LedgerEntry(timestamp="t3", kind="probe", summary="c")]
    # Should not raise
    _validate_ledger_append_only(prev, new)
