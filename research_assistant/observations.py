"""
Per-ticker observations stream (FOLLOWUPS #1 — write phase).

Append-only JSONL log at `<base>/tickers/<SYMBOL>/observations.jsonl`,
one event per line. Written by `/brief` (one event per surviving item)
and `/research` (one event per cascade run). Future kinds will include
`/probe` once that skill lands (FOLLOWUPS #2).

Schema is documented in FOLLOWUPS.md #1. Required keys are stable; extra
keys are forward-compatible (a reader that doesn't know about
`flagged_risks` will still parse the event).

Atomicity: single-line writes through O_APPEND are atomic on POSIX
filesystems for writes ≤ PIPE_BUF (typically 4 KB). A serialized
Observation is well under that. Multi-line writes would need a file
lock; we don't do those.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1


@dataclass
class Observation:
    ts: str
    kind: str
    symbol: str
    chain_id: str
    thesis: str
    schema_version: int = SCHEMA_VERSION
    conviction: Optional[float] = None
    regime: Optional[str] = None
    drivers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    flagged_risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # anchors round-trip through JSON; downstream consumers (#5 read-phase,
        # #7 derived views) call `a.get("claim", ...)`. A bare-string anchor
        # would TypeError silently at read time. Catch the contract violation
        # at write time so the failure happens at the producer, not the consumer.
        if not all(isinstance(a, dict) for a in self.anchors):
            raise TypeError(
                f"Observation.anchors must be list[dict]; got: {self.anchors!r}"
            )


_VALID_KINDS = frozenset({"brief", "research", "probe"})
_KNOWN_FIELDS = frozenset(f.name for f in dataclasses.fields(Observation))


def _observations_path(symbol: str, base: Path) -> Path:
    return base / "tickers" / symbol / "observations.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_observation(obs: Observation, base: Path) -> None:
    """Append one event to the per-ticker stream, creating the ticker
    directory if it doesn't exist. Validates kind; everything else is
    trusted (callers construct Observations from already-validated
    cascade output)."""
    if obs.kind not in _VALID_KINDS:
        raise ValueError(f"unknown observation kind: {obs.kind!r}")
    path = _observations_path(obs.symbol, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dataclasses.asdict(obs), separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def read_observations(
    symbol: str,
    base: Path,
    *,
    limit: Optional[int] = None,
) -> list[Observation]:
    """Return events for `symbol`, oldest first. `limit`, if given,
    returns the most-recent N (still oldest-first within the slice).
    Returns [] if the file doesn't exist yet."""
    path = _observations_path(symbol, base)
    if not path.exists():
        return []
    events: list[Observation] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # Tolerate a single corrupted record without losing the rest
                # of the history. Mirrors trace JSONL reader behavior in
                # cli._load_anchors_from_chain.
                continue
            events.append(Observation(**{k: v for k, v in payload.items() if k in _KNOWN_FIELDS}))
    if limit is not None and limit < len(events):
        events = events[-limit:]
    return events
