"""
Isolation contract test — Principle 3 structural enforcement.

Asserts that research-repo `.claude/` does not bleed into other CC instances:
- Ozy v3's `.claude/` does NOT reference research_assistant / defender paths
- Global `~/.claude/` does NOT host research-assistant skills/agents
- Research repo's `settings.json` uses `$CLAUDE_PROJECT_DIR` (not absolute
  paths or fictional VS Code `${workspaceFolder}`)

Per Critic iter1 AC8: the iter1 plan's `claude --headless --cwd /tmp
--list-skills` test was using fictional CC CLI flags. The iter2 fix
(adopted here) is filesystem-only structural assertions — CI-runnable, no
live CC instance required. Live-CC verification is deferred to manual
smoke during release.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


RESEARCH_REPO = Path(__file__).resolve().parent.parent
OZY_REPO = Path("/home/typhlupgrade/.local/share/ozy-bot-v3")
GLOBAL_CLAUDE = Path.home() / ".claude"


def test_ozy_claude_does_not_reference_research_repo() -> None:
    """
    Open CC in Ozy → must not see any research-assistant skills/agents/hooks.
    Assertion: no file under ozy/.claude/ references the research-repo path,
    the `research_assistant` package, or the `defender` agent name.
    """
    ozy_claude = OZY_REPO / ".claude"
    if not ozy_claude.exists():
        pytest.skip("Ozy .claude/ not present — vacuously isolated")

    forbidden_substrings = [
        str(RESEARCH_REPO),
        "research_assistant",
        "/.research/",
        "defender",  # the agent name — would collide if leaked globally
    ]
    violations: list[str] = []
    for f in ozy_claude.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix in (".lock", ".pyc"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for s in forbidden_substrings:
            if s in text:
                violations.append(f"{f}: contains forbidden substring '{s}'")
    assert not violations, "Ozy .claude/ bleeds into research-repo:\n" + "\n".join(violations)


def test_global_claude_has_no_research_repo_skills() -> None:
    """
    Open CC in any other project → must not see research-assistant skills/agents.
    Assertion: ~/.claude/{skills,agents}/ does not contain research-assistant
    files. (OMC plugin cache is separate and allowed.)
    """
    if not GLOBAL_CLAUDE.exists():
        pytest.skip("No global ~/.claude/ — vacuously isolated")

    forbidden_names = {"research.md", "brief.md", "trace.md", "defender.md"}
    violations: list[str] = []
    for sub in ("skills", "agents"):
        d = GLOBAL_CLAUDE / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.name in forbidden_names:
                violations.append(f"Global {sub}/ contains research-repo file: {f}")
    assert not violations, (
        "Research-repo files leaked into global ~/.claude/:\n" + "\n".join(violations)
    )


def test_research_settings_uses_claude_project_dir() -> None:
    """
    research-repo .claude/settings.json hook command must use $CLAUDE_PROJECT_DIR
    (the canonical CC variable) and NOT VS Code's ${workspaceFolder} (Critic
    iter2 BLOCKER 2) nor absolute paths into other repos.
    """
    settings_path = RESEARCH_REPO / ".claude" / "settings.json"
    assert settings_path.exists(), "research-repo settings.json missing"

    settings = json.loads(settings_path.read_text())

    # Check the LIVE config values (not _comment / documentation strings).
    # A previous version of this test checked raw text and caught its own docs.
    def _collect_command_strings(node) -> list[str]:
        """Walk the settings tree, returning every 'command' string value."""
        commands: list[str] = []
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "command" and isinstance(v, str):
                    commands.append(v)
                else:
                    commands.extend(_collect_command_strings(v))
        elif isinstance(node, list):
            for item in node:
                commands.extend(_collect_command_strings(item))
        return commands

    live_commands = _collect_command_strings(settings)
    for cmd in live_commands:
        assert "${workspaceFolder}" not in cmd, (
            f"Live hook command uses fictional ${{workspaceFolder}} — Critic iter2 BLOCKER 2 regression. "
            f"Command: {cmd}"
        )

    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])
    assert session_start, "SessionStart hook missing from settings.json"
    # Verify real CC schema shape (Critic iter2 BLOCKER 2)
    assert "matcher" in session_start[0], "Hook entry missing 'matcher' field (real CC schema)"
    assert "hooks" in session_start[0], "Hook entry missing 'hooks' array (real CC schema)"
    inner = session_start[0]["hooks"][0]
    assert inner.get("type") == "command", "Inner hook 'type' must be 'command'"
    assert isinstance(inner.get("command"), str), "Inner 'command' must be a STRING (not list)"
    # And the command uses $CLAUDE_PROJECT_DIR
    assert "$CLAUDE_PROJECT_DIR" in inner["command"], (
        "Hook command must reference $CLAUDE_PROJECT_DIR (canonical CC env var)"
    )


def test_research_settings_no_absolute_external_paths() -> None:
    """
    Defense in depth: settings.json should not hard-code paths into OTHER
    repos (Ozy etc.) — that would make the research repo non-portable and
    create cross-repo coupling at the CC level.
    """
    settings_path = RESEARCH_REPO / ".claude" / "settings.json"
    text = settings_path.read_text()
    # Hard-coded Ozy path is forbidden (Ozy modules are imported via pip
    # editable install, not via direct .claude/settings.json references)
    assert str(OZY_REPO) not in text, (
        f"settings.json hard-codes Ozy path {OZY_REPO} — couples repos at CC layer"
    )


def test_no_global_session_start_pointing_at_research_repo() -> None:
    """
    Global ~/.claude/settings.json must not have SessionStart hooks pointing at
    research-repo paths (which would fire on every CC startup anywhere).
    """
    global_settings = GLOBAL_CLAUDE / "settings.json"
    if not global_settings.exists():
        pytest.skip("No global settings.json")
    try:
        text = global_settings.read_text()
    except Exception:
        pytest.skip("Cannot read global settings.json")
    assert "research-assistant" not in text and "research_assistant" not in text, (
        "Global ~/.claude/settings.json references research-assistant — leaks into all CC sessions"
    )
