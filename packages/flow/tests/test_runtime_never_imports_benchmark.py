"""Product code never imports the benchmark harness.

The harness lives in the source checkout (``packages/flow/benchmark/harness``),
outside the ``openadapt_flow`` package, so neither the wheel nor a frozen build
(Desktop's engine) carries it. A product module that imports it, even lazily,
fails there. Shared names (``MODEL``, ``load_api_key``, ``SCROLL_PX_PER_UNIT``)
live in ``openadapt_flow.anthropic_defaults``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import openadapt_flow

ROOT = Path(openadapt_flow.__file__).parent

#: Modules whose job is the benchmark itself. Nothing else may import it.
ALLOWED = {
    # The benchmark CLI command loads the harness on demand from a checkout.
    "__main__.py",
}


def _is_benchmark(module: str) -> bool:
    return any(
        module == root or module.startswith(root + ".")
        for root in ("benchmark", "openadapt_flow.benchmark")
    )


def _imports_benchmark(tree: ast.AST, *, lazy_ok: bool = False) -> list[int]:
    lines: list[int] = []
    # An allowed module may import the harness only inside a function, where
    # it runs on demand from a checkout.
    nodes = (
        [
            n
            for n in ast.iter_child_nodes(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        if lazy_ok
        else ast.walk(tree)
    )
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _is_benchmark(module):
                lines.append(node.lineno)
            elif node.level == 0 and module == "openadapt_flow":
                if any(a.name == "benchmark" for a in node.names):
                    lines.append(node.lineno)
            elif node.level > 0 and _is_benchmark(module):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(_is_benchmark(alias.name) for alias in node.names):
                lines.append(node.lineno)
    return lines


def test_the_harness_is_not_inside_the_package() -> None:
    assert not (ROOT / "benchmark").exists(), (
        "benchmark harnesses belong in packages/flow/benchmark/, outside the "
        "shipped openadapt_flow package"
    )


def test_product_modules_never_import_the_benchmark_package() -> None:
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{rel}:{n}" for n in _imports_benchmark(tree, lazy_ok=rel in ALLOWED)
        ]
    assert not offenders, (
        "product code imports the benchmark harness (the wheel and a frozen "
        "build leave it out); move the shared name to "
        "openadapt_flow.anthropic_defaults or a runtime module: "
        f"{offenders}"
    )


def test_shared_anthropic_names_have_one_home() -> None:
    from benchmark.harness import agent_baseline
    from openadapt_flow import anthropic_defaults

    assert agent_baseline.MODEL is anthropic_defaults.MODEL
    assert agent_baseline.SCROLL_PX_PER_UNIT == anthropic_defaults.SCROLL_PX_PER_UNIT
    assert agent_baseline.load_api_key is anthropic_defaults.load_api_key
