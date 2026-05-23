"""
Prompt-template loader + single-pass renderer + chain-id minter.

Extracted from `orchestrator.py` so `brief.py` (which runs Stage 0
and Stage 1 itself) stops importing single-underscore symbols across
a module boundary. Both orchestrator and brief now consume from here
as peers.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "research-v1.0.0"


def load_prompt(stem: str) -> str:
    """Load a research-v1.0.0 prompt template (text with {placeholder} slots)."""
    path = PROMPTS_DIR / f"{stem}.txt"
    return path.read_text()


def render(template: str, **subs: Any) -> str:
    """
    Substitute `{key}` placeholders in a prompt template.

    Single-pass via regex so that values containing `{other_key}` strings
    (e.g. persisted user content that survives into a future prompt's
    dossier_context block) are NOT re-scanned for further substitution.
    Only `{key}` for `key` in `subs` is substituted; other brace-wrapped
    content (e.g. JSON schema examples like `{ "ticker": "<symbol>", ... }`
    or unmatched placeholders) is preserved verbatim.
    """
    if not subs:
        return template
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in subs) + r")\}")
    return pattern.sub(lambda m: str(subs[m.group(1)]), template)


def chain_id() -> str:
    """Generate a chain ID for the cascade trace (date + epoch-ms hex)."""
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{int(now.timestamp()*1000) & 0xFFFFFF:06x}"
