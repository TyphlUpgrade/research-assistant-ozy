"""
Per-ticker dossier persistence.

Schema (markdown + YAML frontmatter):

    ---
    schema_version: 1
    symbol: NVDA
    last_updated: "2026-05-14T16:32:00Z"
    conviction: 0.62
    ---

    ## State

    Free-form analyst narrative — current view, drivers, risks.

    ## Open Questions

    - [ ] question 1
    - [ ] question 2

    ## Ledger

    - 2026-05-14T10:42:00Z — Probe: 10-Q segment breakdown. Result: ...
    - 2026-05-12T09:15:00Z — Skeptic flagged: ...

Three update rules:
- **State** is mutable but every overwrite must be paired with a Ledger entry.
- **Open Questions** are mutable (add/close).
- **Ledger** is APPEND-ONLY — enforced by `_validate_ledger_append_only`
  called from inside `write_dossier_atomic` (every write path goes through it).

Atomic writes: `tempfile.mkstemp` → write → `os.replace` — same pattern as
Ozy's `state_manager.save_portfolio` (CLAUDE.md L194 cites this convention).

Migrator: schema_version dispatch table. v1 is current; v0→v1 migrator
demonstrates the idempotent-rewrite pattern (running migrate twice produces
byte-identical output).
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import frontmatter


_ANCHOR_SUFFIX_RE = re.compile(r"\s*\[anchor:\s*(?P<anchor>[^\]]+)\]\s*$")


CURRENT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    timestamp: str            # ISO 8601 UTC
    kind: str                 # "probe" | "skeptic" | "defender" | "user_note" | "revision"
    summary: str              # one-line description
    evidence_anchor: Optional[str] = None  # tool_call_id or source ref


@dataclass
class Dossier:
    symbol: str
    schema_version: int = CURRENT_SCHEMA_VERSION
    last_updated: str = ""
    conviction: Optional[float] = None
    state_md: str = ""                                  # narrative state section
    open_questions: list[str] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Migrators (idempotent — running twice produces byte-identical output)
# ---------------------------------------------------------------------------

def _migrate_v0_to_v1(raw_frontmatter: dict[str, Any], content: str) -> tuple[dict[str, Any], str]:
    """v0 had no `conviction` field; default to None. Content unchanged."""
    if "conviction" not in raw_frontmatter:
        raw_frontmatter["conviction"] = None
    raw_frontmatter["schema_version"] = 1
    return raw_frontmatter, content


_MIGRATORS: dict[int, Callable[[dict[str, Any], str], tuple[dict[str, Any], str]]] = {
    0: _migrate_v0_to_v1,
}


# ---------------------------------------------------------------------------
# Parsing / rendering
# ---------------------------------------------------------------------------

def _parse_sections(body: str) -> tuple[str, list[str], list[LedgerEntry]]:
    """Split body into State / Open Questions / Ledger sections."""
    sections = {"State": "", "Open Questions": "", "Ledger": ""}
    current = None
    buf: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            heading = stripped[3:].strip()
            current = heading if heading in sections else None
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    state_md = sections["State"]
    open_questions = [
        line.lstrip("- []x ").strip()
        for line in sections["Open Questions"].splitlines()
        if line.strip().startswith(("- [", "* ["))
    ]
    ledger: list[LedgerEntry] = []
    for line in sections["Ledger"].splitlines():
        s = line.strip()
        if s.startswith("- "):
            parts = s[2:].split(" — ", 1)
            if len(parts) == 2:
                ts, rest = parts
                kind_split = rest.split(":", 1)
                kind = kind_split[0].strip().lower() if len(kind_split) == 2 else "note"
                summary = kind_split[1].strip() if len(kind_split) == 2 else rest
                # Extract [anchor: X] suffix into evidence_anchor field
                anchor: Optional[str] = None
                m = _ANCHOR_SUFFIX_RE.search(summary)
                if m is not None:
                    anchor = m.group("anchor").strip()
                    summary = _ANCHOR_SUFFIX_RE.sub("", summary).rstrip()
                ledger.append(LedgerEntry(
                    timestamp=ts.strip(),
                    kind=kind,
                    summary=summary,
                    evidence_anchor=anchor,
                ))
    return state_md, open_questions, ledger


def _render(d: Dossier) -> str:
    """Render Dossier back to YAML-frontmatter markdown. Deterministic."""
    fm = {
        "schema_version": d.schema_version,
        "symbol": d.symbol,
        "last_updated": d.last_updated,
        "conviction": d.conviction,
    }
    body_parts = ["## State", "", d.state_md or "_(no analyst state yet)_", ""]
    body_parts += ["## Open Questions", ""]
    body_parts += [f"- [ ] {q}" for q in d.open_questions] or ["_(none)_"]
    body_parts += ["", "## Ledger", ""]
    for e in d.ledger:
        anchor = f" [anchor: {e.evidence_anchor}]" if e.evidence_anchor else ""
        body_parts.append(f"- {e.timestamp} — {e.kind}: {e.summary}{anchor}")
    body = "\n".join(body_parts) + "\n"
    post = frontmatter.Post(body, **fm)
    return frontmatter.dumps(post) + "\n"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_ledger_append_only(prev: list[LedgerEntry], new: list[LedgerEntry]) -> None:
    """
    Append-only invariant: `new` must start with all entries from `prev`
    in the same order, then may append additional entries. Raises ValueError
    on any mutation/deletion/reordering of historical entries.
    """
    if len(new) < len(prev):
        raise ValueError(
            f"Ledger mutation rejected: new ledger has {len(new)} entries but "
            f"prev had {len(prev)} — entries cannot be removed."
        )
    for i, (p, n) in enumerate(zip(prev, new)):
        if (p.timestamp, p.kind, p.summary, p.evidence_anchor) != (
            n.timestamp, n.kind, n.summary, n.evidence_anchor
        ):
            raise ValueError(
                f"Ledger mutation rejected at index {i}: historical entries are append-only "
                f"(prev={p}, new={n})."
            )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _dossier_path(symbol: str, base: Path) -> Path:
    return base / "tickers" / f"{symbol.upper()}.md"


def read_dossier(symbol: str, base: Path) -> Optional[Dossier]:
    """Read dossier from disk. Returns None if file doesn't exist."""
    path = _dossier_path(symbol, base)
    if not path.exists():
        return None

    post = frontmatter.load(path)
    fm = dict(post.metadata)
    body = post.content

    # Schema migration
    version = fm.get("schema_version", 0)
    while version < CURRENT_SCHEMA_VERSION:
        migrator = _MIGRATORS.get(version)
        if migrator is None:
            raise RuntimeError(
                f"No migrator for schema version {version} in {path}"
            )
        fm, body = migrator(fm, body)
        version = fm["schema_version"]

    state_md, open_questions, ledger = _parse_sections(body)
    return Dossier(
        symbol=fm["symbol"],
        schema_version=fm["schema_version"],
        last_updated=fm.get("last_updated", ""),
        conviction=fm.get("conviction"),
        state_md=state_md,
        open_questions=open_questions,
        ledger=ledger,
    )


def write_dossier_atomic(d: Dossier, base: Path) -> None:
    """
    Atomic write with hoisted ledger-append-only validator.

    Every write path goes through this function. Validator runs BEFORE the
    tmp file is created, so a rejected write never produces orphan tmp files.

    Pattern: tempfile.mkstemp in same dir → write → os.replace (atomic on
    POSIX). Matches Ozy's `state_manager.save_portfolio` convention.
    """
    path = _dossier_path(d.symbol, base)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Hoisted invariant: validate ledger append-only against prior state
    prev = read_dossier(d.symbol, base)
    if prev is not None:
        _validate_ledger_append_only(prev.ledger, d.ledger)

    # Stamp last_updated if caller didn't
    if not d.last_updated:
        d.last_updated = datetime.now(timezone.utc).isoformat()

    content = _render(d)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{d.symbol}.", suffix=".md.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup orphan tmp on any failure
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def append_ledger_entry(symbol: str, entry: LedgerEntry, base: Path) -> None:
    """Convenience: read → append → atomic-write. Guaranteed append-only."""
    d = read_dossier(symbol, base)
    if d is None:
        d = Dossier(symbol=symbol.upper())
    d.ledger.append(entry)
    d.last_updated = datetime.now(timezone.utc).isoformat()
    write_dossier_atomic(d, base)
