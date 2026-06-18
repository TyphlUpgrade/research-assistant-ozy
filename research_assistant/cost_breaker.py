"""
Panopticon daily cost circuit breaker (FOLLOWUPS #28 / spec critic C3).

A per-ET-day, per-base-dir cumulative cap on *panopticon* LLM spend
(Stage 1.7 synthesizer + role-bound verifier; the Phase E 8-K classifier
will join later). When the day's cumulative panopticon spend exceeds
`PANOPTICON_DAILY_CAP_USD` (default $10), the panopticon degrades
gracefully — the synthesizer is skipped and per-flag verification falls
back to anchor-existence-only — rather than silently burning budget on
idle days. The cap governs ONLY the panopticon surface; Stage 2 thesis
and Stage 3 skeptic are the core /research product and are never capped.

Design notes (from the architect design for this task):
  - The ledger is one JSON file per ET-day under
    `<base>/panopticon_spend/<YYYY-MM-DD>.json`. Each `/research` is a
    fresh process, so a *daily* cap requires cross-invocation
    persistence — hence a file, not an in-memory counter.
  - Read-modify-write is serialized with an `fcntl.flock` advisory lock
    (the proven pattern generalized from `dossier_io.dossier_lock`) and
    persisted with the `tempfile.mkstemp` + `os.replace` atomic-write
    pattern so a crash mid-write can't corrupt the ledger. ACCOUNTING is
    exact (the cumulative total can't be lost). ENFORCEMENT is best-effort:
    `is_capped` and `add_spend` take the lock separately (check-then-act),
    so two concurrent /research processes can each pass the gate before
    either records its spend and overshoot the cap by up to one in-flight
    event per process. Acceptable for a soft backstop with cents-per-event
    cost; the alternative (a single held lock spanning the whole call)
    would serialize all concurrent panopticon work.
  - The breaker is an operator-protection BACKSTOP, not a correctness
    invariant: every failure path FAILS OPEN (cap not enforced, warning
    logged) so a flaky filesystem can never take down /research.
  - The cap is per-`base`, so tests with a tmp base are fully isolated.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

import fcntl

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DEFAULT_CAP_USD = 10.0


def cost_cap_usd() -> float:
    """The configured daily cap. `PANOPTICON_DAILY_CAP_USD` overrides the
    $10 default; a malformed value falls back to the default (fail-open)."""
    raw = os.environ.get("PANOPTICON_DAILY_CAP_USD")
    if raw is None:
        return DEFAULT_CAP_USD
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning(
            "PANOPTICON_DAILY_CAP_USD=%r is not a number; using default $%.2f",
            raw, DEFAULT_CAP_USD,
        )
        return DEFAULT_CAP_USD


def current_et_date() -> str:
    """Today's ET date as YYYY-MM-DD — the ledger's day bucket. Matches
    the ET-day pattern used for the brief cache (cli.py)."""
    return datetime.now(ET).date().isoformat()


def _ledger_path(base: Path, date_et: str) -> Path:
    return base / "panopticon_spend" / f"{date_et}.json"


@contextmanager
def _spend_ledger_lock(ledger_path: Path) -> Iterator[None]:
    """Exclusive advisory lock over a sidecar `<ledger>.lock` file for the
    read-modify-write cycle. Generalized from `dossier_io.dossier_lock`
    (locking a separate file avoids interaction with the atomic rename)."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_ledger(ledger_path: Path, date_et: str) -> dict:
    """Load the day's ledger, or a fresh zeroed one. A corrupt/truncated
    ledger reads as zero-spend (fail-open) rather than crashing /research."""
    if not ledger_path.exists():
        return {
            "date_et": date_et,
            "panopticon_spend_usd": 0.0,
            "research_count": 0,
            "degraded_count": 0,
            "updated_at": "",
        }
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        # Defensive: coerce the spend field to float, default 0 on garbage.
        data["panopticon_spend_usd"] = float(
            data.get("panopticon_spend_usd", 0.0) or 0.0
        )
        return data
    except Exception as exc:
        log.warning(
            "panopticon spend ledger %s unreadable (%s); treating as $0 (fail-open)",
            ledger_path, exc,
        )
        return {
            "date_et": date_et,
            "panopticon_spend_usd": 0.0,
            "research_count": 0,
            "degraded_count": 0,
            "updated_at": "",
        }


def _write_ledger_atomic(ledger_path: Path, data: dict) -> None:
    """Atomic write via mkstemp + os.replace (pattern from
    dossier_io.write_dossier_atomic); orphan tmp cleaned on failure."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".spend.", suffix=".json.tmp", dir=str(ledger_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, ledger_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def is_capped(base: Path, *, cap: Optional[float] = None,
              date_et: Optional[str] = None) -> bool:
    """True when today's cumulative panopticon spend has reached the cap.

    Fails OPEN (returns False) on any error — the breaker must never take
    down /research."""
    cap = cost_cap_usd() if cap is None else cap
    date_et = date_et or current_et_date()
    ledger_path = _ledger_path(base, date_et)
    try:
        with _spend_ledger_lock(ledger_path):
            ledger = _read_ledger(ledger_path, date_et)
        return ledger["panopticon_spend_usd"] >= cap
    except Exception as exc:  # pragma: no cover - fail-open backstop
        log.warning("cost-cap check failed (%s); failing open (uncapped)", exc)
        return False


def add_spend(base: Path, usd: float, *, date_et: Optional[str] = None,
              was_degraded: bool = False, count_research: bool = False) -> None:
    """Add `usd` to today's panopticon spend ledger under the lock.

    `count_research=True` increments the per-day research counter exactly
    once per /research call (pass it only on the FIRST add of a call).
    `was_degraded=True` increments the degraded-research counter. Fails
    OPEN (no-op + warning) on any error."""
    date_et = date_et or current_et_date()
    ledger_path = _ledger_path(base, date_et)
    try:
        if usd < 0:
            # A negative cost is a caller bug; clamp it so it can't lower
            # the cumulative total and defeat the cap, but say so.
            log.debug("negative panopticon spend %r clamped to 0", usd)
        with _spend_ledger_lock(ledger_path):
            ledger = _read_ledger(ledger_path, date_et)
            ledger["panopticon_spend_usd"] = round(
                ledger["panopticon_spend_usd"] + max(0.0, usd), 6
            )
            if count_research:
                ledger["research_count"] = int(ledger.get("research_count", 0)) + 1
                if was_degraded:
                    ledger["degraded_count"] = (
                        int(ledger.get("degraded_count", 0)) + 1
                    )
            ledger["date_et"] = date_et
            ledger["updated_at"] = datetime.now(ET).isoformat()
            _write_ledger_atomic(ledger_path, ledger)
    except Exception as exc:  # pragma: no cover - fail-open backstop
        log.warning("cost-cap add_spend failed (%s); ledger not updated", exc)


def degrade_summary(base: Path) -> tuple[int, int]:
    """Operator surface: (cost-cap-hit days, cumulative degraded /research
    events) across all per-day ledger files. (0, 0) if the panopticon
    spend dir doesn't exist (feature never used)."""
    spend_dir = base / "panopticon_spend"
    if not spend_dir.exists():
        return (0, 0)
    days = 0
    events = 0
    for f in spend_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            degraded = int(data.get("degraded_count", 0))
            if degraded > 0:
                days += 1
            events += degraded
        except Exception:
            continue
    return (days, events)
