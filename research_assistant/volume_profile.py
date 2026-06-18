"""
Intraday volume profile — cached time-of-day participation baseline.

See `.omc/specs/intraday-volume-profile-v1.md`. The problem: `volume_ratio`
(data_loader) drops today's in-progress bar and is blind to the current
session; the live `volume_ratio_intraday` path fixes that for single-ticker
/research but costs an intraday fetch per ticker — too dear for the brief's
universe scan.

This module caches the slow-moving half (typical cumulative volume by
time-of-day) so the brief can compute:

    volume_ratio_intraday = quote.volume / profile.typical_through(now_ET)

using `quote.volume` (today's accumulated volume — already fetched) and zero
hot-path intraday fetches. The baseline is MEASURED (median over real prior
sessions), not projected, so catalyst-day accuracy is preserved.

Persistence mirrors the EDGAR CIK disk cache (atomic write via os.replace,
fail-open on any IO/parse error). Profiles live at
`.research/volume_profiles/<TICKER>.json`.

Public surface:
  - VolumeProfile (dataclass) + typical_through()
  - build_profile_from_bars()   (pure; testable without network)
  - intraday_ratio_from_profile()
  - profile_path / load_profile / write_profile / is_stale
  - refresh_profile()           (one fetch_bars(15m, 1mo) + build + write)
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Session geometry (ET minutes-from-midnight).
_SESSION_OPEN_MIN = 9 * 60 + 30   # 09:30
_FIRST_BUCKET_MIN = 9 * 60 + 45   # 09:45 — first grid point (>= one 15m bar in)
_SESSION_CLOSE_MIN = 16 * 60      # 16:00

# Defaults.
PROFILE_TTL_DAYS = 7            # refresh if older (calendar days; shape drifts slowly)
PROFILE_N_SESSIONS = 20        # trailing sessions in the baseline
PROFILE_MIN_SESSIONS = 5       # below this the median baseline is too thin to trust
PROFILE_BUCKET_MINUTES = 15
PROFILE_REFRESH_CAP = 8        # max lazy refreshes per brief run (brief wiring, PR 2)
_CATALYST_K = 3.0              # full-day vol >= K x median => flagged (color only)


def _bucket_grid(bucket_minutes: int = PROFILE_BUCKET_MINUTES) -> list[int]:
    """Grid of ET minute-marks: 09:45, 10:00, …, 16:00."""
    return list(range(_FIRST_BUCKET_MIN, _SESSION_CLOSE_MIN + 1, bucket_minutes))


def _min_key(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _as_minutes(now_et: Union[time, datetime]) -> int:
    t = now_et.time() if isinstance(now_et, datetime) else now_et
    return t.hour * 60 + t.minute


@dataclass
class VolumeProfile:
    """Per-ticker typical cumulative volume by ET time-of-day.

    `typical_cumulative` maps "HH:MM" grid marks to the MEDIAN over the
    trailing `n_sessions` of that session's cumulative regular-hours volume
    through that clock time. Median (not mean) so a single catalyst day in
    the window can't inflate the baseline."""
    ticker: str
    fetched_at: float
    interval: str
    n_sessions: int
    bucket_minutes: int
    typical_cumulative: dict[str, float]
    typical_full_day: float
    tz: str = "America/New_York"
    catalyst_days: list[str] = field(default_factory=list)

    def typical_through(self, now_et: Union[time, datetime]) -> Optional[float]:
        """Typical cumulative volume through `now_et` (ET).

        Floors `now_et` to the nearest grid mark at or before it. Returns
        None before the first grid mark (too little signal), and the full-day
        median at/after the close (so an after-close call degenerates to a
        clean full-day participation ratio)."""
        minutes = _as_minutes(now_et)
        if minutes < _FIRST_BUCKET_MIN:
            return None
        if minutes >= _SESSION_CLOSE_MIN:
            return self.typical_full_day if self.typical_full_day > 0 else None
        floored = (
            _FIRST_BUCKET_MIN
            + ((minutes - _FIRST_BUCKET_MIN) // self.bucket_minutes)
            * self.bucket_minutes
        )
        return self.typical_cumulative.get(_min_key(floored))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "tz": self.tz,
            "interval": self.interval,
            "n_sessions": self.n_sessions,
            "bucket_minutes": self.bucket_minutes,
            "typical_cumulative": self.typical_cumulative,
            "typical_full_day": self.typical_full_day,
            "catalyst_days": self.catalyst_days,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VolumeProfile":
        return cls(
            ticker=d["ticker"],
            fetched_at=float(d["fetched_at"]),
            interval=d.get("interval", "15m"),
            n_sessions=int(d["n_sessions"]),
            bucket_minutes=int(d.get("bucket_minutes", PROFILE_BUCKET_MINUTES)),
            typical_cumulative={k: float(v) for k, v in d["typical_cumulative"].items()},
            typical_full_day=float(d["typical_full_day"]),
            tz=d.get("tz", "America/New_York"),
            catalyst_days=list(d.get("catalyst_days", [])),
        )


def build_profile_from_bars(
    volume: pd.Series,
    *,
    ticker: str,
    interval: str = "15m",
    bucket_minutes: int = PROFILE_BUCKET_MINUTES,
    n_sessions: int = PROFILE_N_SESSIONS,
    as_of: Optional[date] = None,
    catalyst_k: float = _CATALYST_K,
) -> Optional[VolumeProfile]:
    """Build a VolumeProfile from intraday bars (pure; no network).

    `volume` is an intraday Series with a tz-aware (UTC) DatetimeIndex (e.g.
    15m bars over ~1mo). Today's (partial) session is EXCLUDED so the baseline
    is built only from completed sessions. Returns None when fewer than
    PROFILE_MIN_SESSIONS completed sessions are available.
    """
    if volume is None or len(volume) == 0:
        return None
    try:
        idx = volume.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert(_ET)
        vals = pd.to_numeric(volume.values, errors="coerce")
        et_times = [ts.time() for ts in et]
        df = pd.DataFrame(
            {
                "vol": vals,
                "date": [ts.date() for ts in et],
                "min": [t.hour * 60 + t.minute for t in et_times],
            }
        ).dropna(subset=["vol"])
        # Regular hours only.
        df = df[(df["min"] >= _SESSION_OPEN_MIN) & (df["min"] <= _SESSION_CLOSE_MIN)]
        # Exclude today's partial session from the baseline.
        today = as_of or datetime.now(_ET).date()
        df = df[df["date"] != today]
        if df.empty:
            return None
        sessions = sorted(df["date"].unique())[-n_sessions:]
        df = df[df["date"].isin(sessions)]
        grid = _bucket_grid(bucket_minutes)
        per_session: dict[Any, dict[int, float]] = {}
        totals: dict[Any, float] = {}
        for d, g in df.groupby("date"):
            totals[d] = float(g["vol"].sum())
            # Cumulative through grid mark T = bars that STARTED before T (i.e.
            # completed by T). Strict `<` excludes the bar just opening at T,
            # which hasn't accumulated its volume yet — the correct
            # participation-by-clock-time semantics.
            per_session[d] = {
                m: float(g.loc[g["min"] < m, "vol"].sum()) for m in grid
            }
        n = len(per_session)
        if n < PROFILE_MIN_SESSIONS:
            return None
        typical_cumulative = {
            _min_key(m): float(
                pd.Series([per_session[d][m] for d in per_session]).median()
            )
            for m in grid
        }
        med_total = float(pd.Series(list(totals.values())).median())
        if med_total <= 0:
            return None
        catalyst_days = sorted(
            d.isoformat() for d, t in totals.items() if t >= catalyst_k * med_total
        )
        return VolumeProfile(
            ticker=ticker.upper(),
            fetched_at=_time.time(),
            interval=interval,
            n_sessions=n,
            bucket_minutes=bucket_minutes,
            typical_cumulative=typical_cumulative,
            typical_full_day=med_total,
            catalyst_days=catalyst_days,
        )
    except (IndexError, ValueError, TypeError, AttributeError, KeyError) as exc:
        log.warning("build_profile_from_bars failed for %s: %s", ticker, exc)
        return None


def intraday_ratio_from_profile(
    profile: Optional[VolumeProfile],
    today_volume: Optional[float],
    now_et: Union[time, datetime],
) -> Optional[float]:
    """today's accumulated volume / typical-through-now. None when either side
    is unavailable so the caller degrades to `volume_ratio` (prior-close)."""
    if profile is None or today_volume is None or today_volume <= 0:
        return None
    base = profile.typical_through(now_et)
    if base is None or base <= 0:
        return None
    return round(float(today_volume) / base, 3)


# ---------------------------------------------------------------------------
# Disk cache — mirrors the EDGAR CIK cache pattern (atomic + fail-open).
# ---------------------------------------------------------------------------

def profile_path(base: Union[str, Path], ticker: str) -> Path:
    return Path(base) / "volume_profiles" / f"{ticker.upper()}.json"


def load_profile(base: Union[str, Path], ticker: str) -> Optional[VolumeProfile]:
    """Read a cached profile. None if missing or unreadable (fail-open).
    Staleness is a separate concern — see `is_stale`; a stale-but-present
    profile is still returned (the shape drifts slowly, so it's better than
    nothing when the refresh budget is exhausted)."""
    path = profile_path(base, ticker)
    try:
        if not path.exists():
            return None
        return VolumeProfile.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("load_profile failed for %s: %s", ticker, exc)
        return None


def write_profile(base: Union[str, Path], profile: VolumeProfile) -> Optional[Path]:
    """Persist atomically. Fail-open: cache-write errors are logged, not raised."""
    path = profile_path(base, profile.ticker)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(profile.to_dict()))
        os.replace(tmp, path)
        return path
    except OSError as exc:
        log.warning("write_profile failed for %s: %s", profile.ticker, exc)
        return None


def is_stale(
    profile: VolumeProfile,
    *,
    now: Optional[float] = None,
    ttl_days: int = PROFILE_TTL_DAYS,
) -> bool:
    now = now if now is not None else _time.time()
    return (now - profile.fetched_at) > ttl_days * 86400


async def refresh_profile(
    ticker: str,
    adapter: Any,
    *,
    base: Union[str, Path],
    n_sessions: int = PROFILE_N_SESSIONS,
    as_of: Optional[date] = None,
) -> Optional[VolumeProfile]:
    """Fetch 15m/1mo bars, build the profile, persist it. Returns the fresh
    profile or None on fetch/build failure (caller degrades gracefully)."""
    try:
        bars = await adapter.fetch_bars(ticker, interval="15m", period="1mo")
    except Exception as exc:
        log.warning("refresh_profile fetch failed for %s: %s", ticker, exc)
        return None
    if bars is None or "volume" not in bars:
        return None
    profile = build_profile_from_bars(
        bars["volume"], ticker=ticker, n_sessions=n_sessions, as_of=as_of
    )
    if profile is None:
        return None
    write_profile(base, profile)
    return profile
