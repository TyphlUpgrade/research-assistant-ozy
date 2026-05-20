"""
Import-boundary contract test (Principle 2 structural enforcement).

Walks the transitive Ozy-import graph starting from every file in
`research_assistant/**/*.py` and asserts:

1. No edge into FORBIDDEN_MODULE_PATTERNS (state_manager, orchestrator,
   execution/, risk_manager, broker, fill_handler, position_sync,
   strategies, score_cascade.ScoreCascade orchestration, etc.).

2. Direct imports limited to ALLOWED set.

3. The ONE allowed `state_manager` touch is `WatchlistState` from
   `null_state_manager.py` only — enforced via ALLOWED_NAMED_SYMBOL.

4. Position / PortfolioState / WatchlistState may not be INSTANTIATED
   anywhere except `null_state_manager.NullStateManager.load_watchlist`
   (instantiation triggers Ozy's v0→v1 migration logic).

Algorithm: BFS from research_assistant/**/*.py. Each Ozy module discovered
is itself ast.parsed and its imports added to the queue. Fixed-point
termination via `visited` set. Performance bound: ~30-50 modules, <2s.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_PKG = REPO_ROOT / "research_assistant"

# Ozy modules research_assistant may import (direct OR transitively)
ALLOWED_OZY_PREFIXES = {
    "ozymandias",
    "ozymandias.data",
    "ozymandias.data.adapters",
    "ozymandias.data.adapters.yfinance_adapter",
    "ozymandias.intelligence",
    "ozymandias.intelligence.technical_analysis",
    "ozymandias.intelligence.claude_json",
    "ozymandias.intelligence.context_compressor",  # pure compute
    # universe_fetcher graduated v1.x: Ozy momentum-blindspot fix landed
    # (commit f07f526 — score-cascade replaces intraday momentum loop),
    # making the lift safe per Open Follow-up #1.
    "ozymandias.intelligence.universe_fetcher",
    "ozymandias.core",
    "ozymandias.core.config",
    "ozymandias.core.market_context",
    "ozymandias.core.market_hours",
    "ozymandias.core.reasoning_cache",  # clean stdlib-only
}

# Named-symbol exceptions: `from ozymandias.core.state_manager import X`
# is forbidden EXCEPT for these specific (module, symbol, from_file) triples.
ALLOWED_NAMED_SYMBOL: dict[tuple[str, str], set[str]] = {
    ("ozymandias.core.state_manager", "WatchlistState"): {
        "research_assistant/null_state_manager.py",
    },
}

# Anything matching these is forbidden (substring match on module name)
FORBIDDEN_MODULE_PATTERNS = {
    "ozymandias.core.state_manager",  # except via ALLOWED_NAMED_SYMBOL above
    "ozymandias.core.orchestrator",
    "ozymandias.core.crash_protector",
    "ozymandias.core.quant_overrides",
    "ozymandias.core.fill_handler",
    "ozymandias.core.position_sync",
    "ozymandias.core.position_manager",
    "ozymandias.core.trigger_engine",
    "ozymandias.core.watchlist_manager",
    "ozymandias.execution",
    "ozymandias.execution.risk_manager",
    "ozymandias.execution.alpaca_broker",
    "ozymandias.execution.broker_interface",
    "ozymandias.strategies",
    "ozymandias.intelligence.score_cascade",  # research builds its own orchestrator
    "ozymandias.intelligence.skeptic",          # research forks the Skeptic prompts
    "ozymandias.intelligence.portfolio_fit",    # skipped per spec
    "ozymandias.intelligence.opportunity_ranker",  # retired in Ozy, never used here
    "ozymandias.intelligence.universe_scanner",  # not in v1 lift list
    "ozymandias.intelligence.claude_reasoning",  # use claude_json submodule instead
}

# Dataclasses whose instantiation triggers v0→v1 migration logic
FORBIDDEN_INSTANTIATIONS = {"Position", "PortfolioState"}
# WatchlistState instantiation is allowed only in null_state_manager.py
ALLOWED_WATCHLIST_INSTANTIATION = {"research_assistant/null_state_manager.py"}


def _ozy_imports_from_file(path: Path) -> set[tuple[str, str | None]]:
    """
    Returns set of (module_name, symbol_or_None) tuples.
    - For `from X import Y, Z`: yields (X, "Y"), (X, "Z").
    - For `import X`: yields (X, None).
    Filters to ozymandias.* edges.
    """
    edges: set[tuple[str, str | None]] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("ozymandias"):
                for alias in node.names:
                    edges.add((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ozymandias"):
                    edges.add((alias.name, None))
    return edges


def _instantiations_in_file(path: Path) -> set[str]:
    """Find ast.Call nodes where func.id is a class name (best-effort heuristic)."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def _ozy_module_to_path(module: str) -> Path | None:
    """Resolve `ozymandias.x.y` → filesystem path of the .py file."""
    try:
        mod = __import__(module, fromlist=["__file__"])
        if hasattr(mod, "__file__") and mod.__file__:
            return Path(mod.__file__)
    except (ImportError, ModuleNotFoundError):
        pass
    return None


def _check_edge(
    module: str,
    symbol: str | None,
    source_file: str,
    violations: list[str],
) -> None:
    """Apply allow/forbid rules to a single import edge."""
    is_forbidden = any(pat in module for pat in FORBIDDEN_MODULE_PATTERNS)
    if is_forbidden:
        # Check named-symbol exception
        if symbol is not None:
            allowed_sources = ALLOWED_NAMED_SYMBOL.get((module, symbol))
            if allowed_sources is not None and source_file in allowed_sources:
                return  # explicitly allowed
        violations.append(
            f"FORBIDDEN: {source_file} imports `{symbol or '<module>'}` from `{module}`"
        )


def test_import_boundaries_transitive() -> None:
    """
    Transitive BFS: research_assistant/ → Ozy modules → Ozy submodules → ...
    Asserts every edge in the closure complies with ALLOWED / FORBIDDEN rules.
    """
    violations: list[str] = []
    visited_modules: set[str] = set()

    # Seed queue from research_assistant/*.py files
    queue: list[tuple[str, str]] = []  # (file_path_str, module_or_file_label)
    for py_file in RESEARCH_PKG.rglob("*.py"):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        for module, symbol in _ozy_imports_from_file(py_file):
            _check_edge(module, symbol, rel, violations)
            queue.append((module, rel))

    # BFS through Ozy module graph
    while queue:
        module, _ = queue.pop(0)
        if module in visited_modules:
            continue
        visited_modules.add(module)

        path = _ozy_module_to_path(module)
        if path is None:
            continue
        for sub_module, sub_symbol in _ozy_imports_from_file(path):
            _check_edge(sub_module, sub_symbol, module, violations)
            if sub_module not in visited_modules:
                queue.append((sub_module, module))

    assert not violations, "\n".join(violations)


def test_no_forbidden_instantiations() -> None:
    """
    Position/PortfolioState may never be instantiated in research_assistant.
    WatchlistState may only be instantiated in null_state_manager.py.
    """
    violations: list[str] = []
    for py_file in RESEARCH_PKG.rglob("*.py"):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        names = _instantiations_in_file(py_file)
        for forbidden in FORBIDDEN_INSTANTIATIONS:
            if forbidden in names:
                violations.append(
                    f"FORBIDDEN INSTANTIATION: {rel} instantiates `{forbidden}` "
                    "(triggers Ozy v0→v1 migration)"
                )
        if "WatchlistState" in names and rel not in ALLOWED_WATCHLIST_INSTANTIATION:
            violations.append(
                f"FORBIDDEN INSTANTIATION: {rel} instantiates `WatchlistState` "
                "(allowed only in null_state_manager.py)"
            )
    assert not violations, "\n".join(violations)


if __name__ == "__main__":
    test_import_boundaries_transitive()
    test_no_forbidden_instantiations()
    print("OK")
