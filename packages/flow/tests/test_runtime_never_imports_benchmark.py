"""Product code never imports ``openadapt_flow.benchmark``.

A frozen build (Desktop's engine) leaves the benchmark package out, so a
product module that imports it, even lazily, fails there. Shared names
(``MODEL``, ``load_api_key``, ``SCROLL_PX_PER_UNIT``) live in
``openadapt_flow.anthropic_defaults``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import openadapt_flow

ROOT = Path(openadapt_flow.__file__).parent

#: Modules whose job is the benchmark itself. Nothing else may import it.
ALLOWED = {
    # The benchmark CLI command loads the harness on demand.
    "__main__.py",
    # The competitor-drift harness reuses the benchmark's fault scenarios.
    "instrument/competitor_drift.py",
}


def _imports_benchmark(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (
                module == "openadapt_flow.benchmark"
                or module.startswith("openadapt_flow.benchmark.")
            ):
                lines.append(node.lineno)
            elif node.level == 0 and module == "openadapt_flow":
                if any(a.name == "benchmark" for a in node.names):
                    lines.append(node.lineno)
            elif node.level > 0 and (
                module == "benchmark" or module.startswith("benchmark.")
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "openadapt_flow.benchmark" or alias.name.startswith(
                    "openadapt_flow.benchmark."
                ):
                    lines.append(node.lineno)
    return lines


def test_product_modules_never_import_the_benchmark_package() -> None:
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("benchmark/") or rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{rel}:{n}" for n in _imports_benchmark(tree)]
    assert not offenders, (
        "product code imports openadapt_flow.benchmark (a frozen build leaves "
        "it out); move the shared name to openadapt_flow.anthropic_defaults "
        f"or a runtime module: {offenders}"
    )


def test_shared_anthropic_names_have_one_home() -> None:
    from openadapt_flow import anthropic_defaults
    from openadapt_flow.benchmark import agent_baseline

    assert agent_baseline.MODEL is anthropic_defaults.MODEL
    assert agent_baseline.SCROLL_PX_PER_UNIT == anthropic_defaults.SCROLL_PX_PER_UNIT
    assert agent_baseline.load_api_key is anthropic_defaults.load_api_key
