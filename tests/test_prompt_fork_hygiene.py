"""
Prompt-fork hygiene test (Principle 4: Prompt-fork hygiene over divergence).

For each prompt in `prompts/research-v1.0.0/`, assert:

1. **Header presence**: required fields (PROMPT_VERSION, STAGE, MODEL_TIER,
   DERIVED_FROM, DELTAS) are present.

2. **SHA reachability**: the SHA cited in `DERIVED_FROM` is reachable via
   `git rev-parse` in the Ozy repo. Catches stale/invented hashes that
   regex-only validation would miss (Critic iter1 #11).

3. **Source-path existence at SHA**: the cited source file existed in the Ozy
   tree at the cited SHA. Catches typos in the source path.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts" / "research-v1.0.0"
OZY_REPO = Path("/home/typhlupgrade/.local/share/ozy-bot-v3")

_DERIVED_FROM_RE = re.compile(
    r"^DERIVED_FROM:\s*(?P<path>\S+)\s*@\s*(?P<sha>[a-f0-9]{7,40})\s*$",
    re.MULTILINE,
)

# Clean-room prompts that don't fork from Ozy declare themselves via this
# sentinel. Format: `DERIVED_FROM: clean_room (<in-repo spec or rationale>)`.
# The hygiene tests skip SHA reachability + source-path checks for these
# entries — they ARE the new code path, no upstream to track. The header
# presence + DELTAS-equivalent still apply (we keep a record of why the
# prompt exists).
_CLEAN_ROOM_PREFIX = "DERIVED_FROM: clean_room"

REQUIRED_HEADER_FIELDS = ("PROMPT_VERSION", "STAGE", "MODEL_TIER", "DERIVED_FROM", "DELTAS")
CLEAN_ROOM_REQUIRED_HEADER_FIELDS = (
    "PROMPT_VERSION", "STAGE", "MODEL_TIER", "DERIVED_FROM",
)


def _is_clean_room(content: str) -> bool:
    """True when the prompt declares itself as clean-room (no Ozy fork)."""
    for line in content.splitlines():
        if line.strip().startswith(_CLEAN_ROOM_PREFIX):
            return True
    return False


def _prompt_files() -> list[Path]:
    """Return all research-prompt .txt files."""
    return sorted(p for p in PROMPTS_DIR.glob("*.txt"))


@pytest.mark.parametrize("prompt_path", _prompt_files(), ids=lambda p: p.name)
def test_required_headers_present(prompt_path: Path) -> None:
    content = prompt_path.read_text()
    required = (
        CLEAN_ROOM_REQUIRED_HEADER_FIELDS
        if _is_clean_room(content)
        else REQUIRED_HEADER_FIELDS
    )
    missing = [field for field in required if f"{field}:" not in content]
    assert not missing, f"{prompt_path.name} missing headers: {missing}"


@pytest.mark.parametrize("prompt_path", _prompt_files(), ids=lambda p: p.name)
def test_derivation_sha_reachable(prompt_path: Path) -> None:
    """Critic iter1 #11: cited SHA must exist in Ozy git history.

    Clean-room prompts (DERIVED_FROM: clean_room ...) are exempt — there
    is no upstream SHA to verify."""
    content = prompt_path.read_text()
    if _is_clean_room(content):
        pytest.skip("clean-room prompt — no upstream SHA")
    m = _DERIVED_FROM_RE.search(content)
    assert m is not None, f"{prompt_path.name}: DERIVED_FROM line not parseable"

    sha = m.group("sha")
    result = subprocess.run(
        ["git", "-C", str(OZY_REPO), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{prompt_path.name}: cited SHA {sha} not reachable in Ozy repo.\n"
        f"git output: {result.stderr.strip()}"
    )


@pytest.mark.parametrize("prompt_path", _prompt_files(), ids=lambda p: p.name)
def test_derivation_source_existed_at_sha(prompt_path: Path) -> None:
    """The cited source path must have existed in Ozy at the cited SHA.

    Clean-room prompts (DERIVED_FROM: clean_room ...) are exempt — there
    is no upstream path to verify."""
    content = prompt_path.read_text()
    if _is_clean_room(content):
        pytest.skip("clean-room prompt — no upstream source path")
    m = _DERIVED_FROM_RE.search(content)
    assert m is not None

    source_path = m.group("path")
    sha = m.group("sha")
    result = subprocess.run(
        ["git", "-C", str(OZY_REPO), "cat-file", "-e", f"{sha}:{source_path}"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{prompt_path.name}: source path '{source_path}' did not exist "
        f"in Ozy at SHA {sha}.\nstderr: {result.stderr.strip()}"
    )
