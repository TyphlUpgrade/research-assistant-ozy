"""
Tests for volume_profile.py — pure builder/lookup math + disk cache round-trip.

No live yfinance: synthetic 15m bars (UTC tz-aware) feed the builder, mirroring
the data_loader test conventions.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from research_assistant.volume_profile import (
    VolumeProfile,
    build_profile_from_bars,
    intraday_ratio_from_profile,
    is_stale,
    load_profile,
    profile_path,
    refresh_profile,
    write_profile,
)

_ET = ZoneInfo("America/New_York")


def _intraday_volume(
    sessions: dict[date, dict[str, float]],
) -> pd.Series:
    """Build a 15m volume Series (UTC tz-aware) from a {date: {"HH:MM ET": vol}}
    map. June 2026 is EDT (UTC-4), so ET hour + 4 == UTC hour."""
    stamps: list[pd.Timestamp] = []
    vols: list[float] = []
    for d, bars in sessions.items():
        for hhmm, vol in bars.items():
            h, m = (int(x) for x in hhmm.split(":"))
            stamps.append(pd.Timestamp(f"{d.isoformat()}T{h + 4:02d}:{m:02d}:00", tz="UTC"))
            vols.append(vol)
    return pd.Series(vols, index=pd.DatetimeIndex(stamps))


def _uniform_session(per_bar: float) -> dict[str, float]:
    """A full regular session of 15m bars, constant volume per bar. Bars are
    labelled by start time, so the last regular bar opens at 15:45 (covers
    15:45–16:00); there is no 16:00 bar. 26 bars total."""
    out: dict[str, float] = {}
    minute = 9 * 60 + 30
    while minute <= 15 * 60 + 45:
        out[f"{minute // 60:02d}:{minute % 60:02d}"] = per_bar
        minute += 15
    return out


# ---------------------------------------------------------------------------
# build_profile_from_bars
# ---------------------------------------------------------------------------

def test_build_profile_medians_and_excludes_today() -> None:
    # 6 prior sessions, 100/bar; "today" is a partial high-volume session that
    # must be EXCLUDED from the baseline.
    prior = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10),
             date(2026, 6, 11), date(2026, 6, 12), date(2026, 6, 15)]
    sessions = {d: _uniform_session(100.0) for d in prior}
    sessions[date(2026, 6, 16)] = {"09:30": 9999.0, "09:45": 9999.0}  # today, partial
    v = _intraday_volume(sessions)

    p = build_profile_from_bars(v, ticker="sndk", as_of=date(2026, 6, 16))
    assert p is not None
    assert p.ticker == "SNDK"
    assert p.n_sessions == 6  # today excluded
    # Through 09:45 ET = only the 09:30 bar (started before 09:45) → 100.
    assert p.typical_cumulative["09:45"] == 100.0
    # Through 10:00 = 09:30 + 09:45 bars (both started before 10:00) → 200.
    assert p.typical_cumulative["10:00"] == 200.0
    # Full day = 26 bars (09:30..15:45 by start time) × 100.
    assert p.typical_full_day == 2600.0


def test_build_profile_catalyst_day_flagged_not_excluded() -> None:
    prior = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10),
             date(2026, 6, 11), date(2026, 6, 12)]
    sessions = {d: _uniform_session(100.0) for d in prior}
    # One historic catalyst day at 5x volume — flagged, but median is robust.
    sessions[date(2026, 6, 5)] = _uniform_session(500.0)
    v = _intraday_volume(sessions)

    p = build_profile_from_bars(v, ticker="X", as_of=date(2026, 6, 16))
    assert p is not None
    assert "2026-06-05" in p.catalyst_days
    # Median over 6 sessions (five at 100, one at 500) is unmoved at the 100 level.
    assert p.typical_full_day == 2600.0


def test_build_profile_none_when_too_few_sessions() -> None:
    sessions = {d: _uniform_session(100.0)
                for d in [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]}
    v = _intraday_volume(sessions)
    assert build_profile_from_bars(v, ticker="X", as_of=date(2026, 6, 16)) is None


def test_build_profile_empty_returns_none() -> None:
    assert build_profile_from_bars(pd.Series([], dtype=float), ticker="X") is None


# ---------------------------------------------------------------------------
# typical_through + intraday_ratio_from_profile
# ---------------------------------------------------------------------------

def _profile() -> VolumeProfile:
    return VolumeProfile(
        ticker="X", fetched_at=1_750_000_000.0, interval="15m",
        n_sessions=20, bucket_minutes=15,
        typical_cumulative={"09:45": 100.0, "10:00": 200.0, "15:45": 2600.0},
        typical_full_day=2700.0,
    )


def test_typical_through_floors_to_grid() -> None:
    p = _profile()
    # 10:07 ET floors to the 10:00 grid mark.
    assert p.typical_through(time(10, 7)) == 200.0
    assert p.typical_through(time(10, 0)) == 200.0


def test_typical_through_pre_open_is_none() -> None:
    assert _profile().typical_through(time(9, 31)) is None


def test_typical_through_after_close_uses_full_day() -> None:
    assert _profile().typical_through(time(16, 30)) == 2700.0


def test_typical_through_accepts_datetime() -> None:
    dt = datetime(2026, 6, 16, 10, 5, tzinfo=_ET)
    assert _profile().typical_through(dt) == 200.0


def test_intraday_ratio_basic() -> None:
    p = _profile()
    # 400 traded by 10:00 vs typical 200 → 2.0.
    assert intraday_ratio_from_profile(p, 400.0, time(10, 0)) == 2.0


def test_intraday_ratio_none_paths() -> None:
    p = _profile()
    assert intraday_ratio_from_profile(None, 400.0, time(10, 0)) is None
    assert intraday_ratio_from_profile(p, 0.0, time(10, 0)) is None
    assert intraday_ratio_from_profile(p, 400.0, time(9, 0)) is None  # pre-open base None


# ---------------------------------------------------------------------------
# disk cache round-trip + staleness
# ---------------------------------------------------------------------------

def test_write_load_round_trip(tmp_path) -> None:
    p = _profile()
    path = write_profile(tmp_path, p)
    assert path == profile_path(tmp_path, "X")
    loaded = load_profile(tmp_path, "X")
    assert loaded is not None
    assert loaded.ticker == "X"
    assert loaded.typical_full_day == 2700.0
    assert loaded.typical_cumulative["10:00"] == 200.0


def test_load_missing_returns_none(tmp_path) -> None:
    assert load_profile(tmp_path, "NOPE") is None


def test_load_corrupt_returns_none(tmp_path) -> None:
    path = profile_path(tmp_path, "BAD")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert load_profile(tmp_path, "BAD") is None


def test_is_stale_ttl() -> None:
    p = _profile()  # fetched_at = 1_750_000_000
    fresh = 1_750_000_000.0 + 86400  # 1 day later
    old = 1_750_000_000.0 + 86400 * 10  # 10 days later
    assert is_stale(p, now=fresh, ttl_days=7) is False
    assert is_stale(p, now=old, ttl_days=7) is True


# ---------------------------------------------------------------------------
# refresh_profile (mocked adapter)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_profile_writes_to_disk(tmp_path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    prior = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10),
             date(2026, 6, 11), date(2026, 6, 12)]
    v = _intraday_volume({d: _uniform_session(100.0) for d in prior})
    bars = pd.DataFrame({"volume": v.values}, index=v.index)
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=bars)

    p = await refresh_profile("X", adapter, base=tmp_path, as_of=date(2026, 6, 16))
    assert p is not None
    assert load_profile(tmp_path, "X") is not None


@pytest.mark.asyncio
async def test_refresh_profile_fetch_failure_returns_none(tmp_path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(side_effect=RuntimeError("boom"))
    p = await refresh_profile("X", adapter, base=tmp_path, as_of=date(2026, 6, 16))
    assert p is None
