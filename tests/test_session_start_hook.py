"""
Tests for the SessionStart hook script — timezone discipline + nudge behavior.

Critical assertion (CLAUDE.md L27 + Critic iter2 BLOCKER 8): the hook
must use ZoneInfo("America/New_York") for "today" date logic, NOT
local-system-clock `datetime.date.today()`.

Spawn the script as a subprocess so the test sees the same behavior CC
would (after `command: "python3 ...session_start.py"`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


HOOK_SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "session_start.py"


def _run_hook(project_dir: Path) -> tuple[int, str]:
    """Invoke the hook with CLAUDE_PROJECT_DIR set to a tmp project."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=env, capture_output=True, text=True,
    )
    return result.returncode, result.stdout


def test_nudge_fires_when_no_brief_today(tmp_path: Path) -> None:
    """No briefs/<today-ET>.json file → nudge is printed."""
    (tmp_path / ".research" / "briefs").mkdir(parents=True, exist_ok=True)
    rc, stdout = _run_hook(tmp_path)
    assert rc == 0
    assert "No morning brief" in stdout
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    assert today_et in stdout, "Nudge should cite today's ET date specifically"


def test_no_nudge_when_brief_exists_today(tmp_path: Path) -> None:
    """briefs/<today-ET>.json exists → silent."""
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    brief = tmp_path / ".research" / "briefs" / f"{today_et}.json"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text('{"world_state": {}}')

    rc, stdout = _run_hook(tmp_path)
    assert rc == 0
    assert stdout.strip() == "", f"Expected silence; got: {stdout!r}"


def test_uses_et_not_local_timezone(tmp_path: Path) -> None:
    """
    Place a brief stamped with YESTERDAY's local date. If hook used local
    clock, that brief might satisfy 'today' on a timezone boundary day.
    But hook uses ET, so unless ET 'today' happens to equal local
    yesterday, the nudge should still fire. This is a probabilistic check
    but combined with the assertion that today_et appears in the nudge,
    it gives reasonable coverage.
    """
    yesterday_local = (datetime.now().date().toordinal() - 1)
    from datetime import date
    yesterday_str = date.fromordinal(yesterday_local).isoformat()
    brief = tmp_path / ".research" / "briefs" / f"{yesterday_str}.json"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text('{"world_state": {}}')

    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    rc, stdout = _run_hook(tmp_path)
    # If ET 'today' happens to equal yesterday-local (rare timezone edge),
    # the brief is valid and no nudge fires. Otherwise nudge fires.
    if today_et == yesterday_str:
        assert stdout.strip() == ""
    else:
        assert "No morning brief" in stdout
        assert today_et in stdout
