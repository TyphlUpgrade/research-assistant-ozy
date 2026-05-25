"""
Tests for `alerts review` CLI subcommand (PR 1.3).

Covers:
- Basic per-screener tally output (3 alerts, one screener)
- Empty window renders "(no alerts in window)"
- --screener filter restricts to one screener
- --export-csv writes a flat CSV
- --window accepts Nd shapes (7d / 30d / 90d)
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from research_assistant import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_alerts(base: Path, asof: str, rows: list[dict]) -> Path:
    """Write a day's worth of alert JSONL rows for testing."""
    path = base / "alerts" / f"{asof}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def _row(
    *, ticker: str, screener: str, asof: str, return_30d: float | None = None,
    entry_price: float = 100.0,
) -> dict:
    return {
        "schema_version": 1,
        "ticker": ticker,
        "screener": screener,
        "asof": asof,
        "entry_price": entry_price,
        "evidence": {"hit": True},
        "created_at": "2026-05-25T00:00:00+00:00",
        "return_7d": None,
        "return_30d": return_30d,
        "return_90d": None,
        "enriched_at": "2026-05-25T01:00:00+00:00" if return_30d is not None else None,
    }


class _NoopAdapter:
    """Stub yfinance adapter so enrich_window in _cmd_alerts is a no-op."""
    def __init__(self) -> None:
        self.research_base = None

    async def fetch_price_at(self, ticker: str, target):  # noqa: D401
        return None


@pytest.fixture(autouse=True)
def _stub_yfinance(monkeypatch):
    """Replace YFinanceAdapter with a no-op so the test never touches network."""
    import ozymandias.data.adapters.yfinance_adapter as yf_mod
    monkeypatch.setattr(yf_mod, "YFinanceAdapter", _NoopAdapter)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_review_basic_output(tmp_path: Path, capsys) -> None:
    asof = date.today().isoformat()
    _write_alerts(tmp_path, asof, [
        _row(ticker="XLK", screener="sector_rotation", asof=asof, return_30d=0.05),
        _row(ticker="XLF", screener="sector_rotation", asof=asof, return_30d=0.02),
        _row(ticker="XLE", screener="sector_rotation", asof=asof, return_30d=-0.01),
    ])
    args = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "30d",
    ])
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "30d",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sector_rotation" in out
    assert "count=3" in out


def test_review_empty_window(tmp_path: Path, capsys) -> None:
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "30d",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no alerts in window)" in out


def test_review_screener_filter(tmp_path: Path, capsys) -> None:
    asof = date.today().isoformat()
    _write_alerts(tmp_path, asof, [
        _row(ticker="XLK", screener="sector_rotation", asof=asof, return_30d=0.05),
        _row(ticker="NVDA", screener="pead", asof=asof, return_30d=0.10),
    ])
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "30d",
        "--screener", "sector_rotation",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sector_rotation" in out
    # The filter restricts the *rendered* output to one screener; the other
    # screener's tally line must NOT appear.
    assert "pead" not in out


def test_review_export_csv(tmp_path: Path, capsys) -> None:
    asof = date.today().isoformat()
    _write_alerts(tmp_path, asof, [
        _row(ticker="XLK", screener="sector_rotation", asof=asof, return_30d=0.05),
        _row(ticker="XLF", screener="sector_rotation", asof=asof, return_30d=0.02),
    ])
    csv_path = tmp_path / "out" / "alerts.csv"
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "30d",
        "--export-csv", str(csv_path),
    ])
    assert rc == 0
    assert csv_path.exists()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"XLK", "XLF"}
    # Stable column set the operator's spreadsheet will care about
    expected_cols = {
        "asof", "ticker", "screener", "entry_price",
        "return_7d", "return_30d", "return_90d",
        "enriched_at", "created_at",
    }
    assert expected_cols.issubset(set(rows[0].keys()))


@pytest.mark.parametrize("window", ["7d", "30d", "90d"])
def test_review_window_arg_format(tmp_path: Path, window: str, capsys) -> None:
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", window,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    days = int(window.rstrip("d"))
    assert f"({days}d)" in out


def test_review_window_arg_rejects_bad_format(tmp_path: Path, capsys) -> None:
    rc = cli.main([
        "--base", str(tmp_path),
        "alerts", "review", "--window", "lifetime",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Invalid --window" in err
