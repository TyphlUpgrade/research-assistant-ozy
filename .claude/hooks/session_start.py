#!/usr/bin/env python3
"""
SessionStart hook — soft nudge when no morning brief exists for today (ET).

Timezone discipline (CLAUDE.md L27): NEVER rely on local system clock for
date logic. Use ZoneInfo("America/New_York") for "today" semantics — the
market calendar is ET, and a user in PT opening CC at 9:01pm local would
see the wrong day-of-brief otherwise.

Nudge is non-pushy: prints to stdout (which CC surfaces as a system-reminder
context block). If the user wants the brief, they type `/brief`. If they
don't, the nudge is one line of context they ignore.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def main() -> int:
    # $CLAUDE_PROJECT_DIR is the canonical CC env variable for the open project.
    # NEVER use VS Code's ${workspaceFolder} — that's a different system.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        # Fallback to script's own location-derived project root
        project_dir = str(Path(__file__).resolve().parent.parent.parent)

    today_et = datetime.now(ET).date().isoformat()
    brief_path = Path(project_dir) / ".research" / "briefs" / f"{today_et}.json"

    if brief_path.exists():
        # Brief already generated today — silent, no nudge
        return 0

    # Non-pushy nudge: one line, no pressure
    print(
        f"📰 No morning brief generated yet for {today_et} (ET). "
        f"Run `/brief` when you're ready — or skip and use `/research <TICKER>` directly."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
