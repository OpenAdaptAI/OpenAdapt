"""Fail when a file gains a hand-written verdict fold outside the kernel.

The same fail-open bugs kept coming back because every check folded its own
evidence: ``all(...)`` over a list that could be empty, a slice before the
tally, ``except Exception: return``, a final ``else`` that says success, a
compare against ``"VERIFIED"``. ``openadapt_flow/verdict.py`` folds once, and
a function marked ``@verdict_site`` has a spec the fail-open harness runs
(``tests/failopen``). This check counts the hand-written folds everywhere
else, per kind, and fails only when a kind's total rises above
``verdict_folds_baseline.json`` (it lists the files whose count rose).
Totals decide, so moving code into a new module is not a new fold. A count that falls never fails; lower the
baseline with ``--update-baseline`` when you like (it never raises one).

What it counts (``verdict_folds.toml`` holds the literal lists):

- ``says_success``: a success literal (``VERIFIED``, ``CONFIRMED``, ``held``,
  ...) produced in code, not compared. Only the kernel and a registered site
  may say a check held.
- ``cmp_literal``: a compare against an outcome literal.
- ``bool_fold``: ``all(...)``/``any(...)`` over verdicts, results, checks,
  effects, lines or tiers.
- ``swallow``: ``except Exception`` (or bare) whose body returns, continues
  or passes without a raise or a not-checked value.
- ``slice_count``: a ``[:N]`` slice in a statement that also counts.
- ``else_success``: a final ``else:`` that assigns a success literal.

The way to add verdict code is to build it on ``verdict.settle`` inside a
function marked ``@verdict_site`` with a spec (docs/FAIL_CLOSED.md).

Usage::

    python scripts/check_verdict_folds.py
    python scripts/check_verdict_folds.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent
ROOT = FLOW.parent.parent
CONFIG = HERE / "verdict_folds.toml"
BASELINE = HERE / "verdict_folds_baseline.json"
OUTCOMES = ROOT / "packages/types/openadapt_types/vocabulary/outcomes.json"

KINDS = (
    "says_success",
    "cmp_literal",
    "bool_fold",
    "swallow",
    "slice_count",
    "else_success",
)


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    literals = set(config["literals"]["outcome_extra"])
    if OUTCOMES.is_file():
        vocab = json.loads(OUTCOMES.read_text(encoding="utf-8"))
        for source in vocab["sources"].values():
            literals.update(source.get("values", {}))
    config["_outcomes"] = frozenset(literals)
    config["_success"] = frozenset(config["literals"]["success"])
    config["_fold_names"] = re.compile(config["bool_fold"]["names"], re.IGNORECASE)
    return config


def _site_decorator(node: ast.AST) -> Optional[str]:
    """The id of ``@verdict_site("id", ...)`` on a function, else None."""

    for dec in getattr(node, "decorator_list", []):
        if (
            isinstance(dec, ast.Call)
            and getattr(dec.func, "id", getattr(dec.func, "attr", None))
            == "verdict_site"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
        ):
            return dec.args[0].value
    return None


def decorated_sites(path: Path) -> dict[str, str]:
    """``{site id: qualified function name}`` for every ``@verdict_site``."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                site = _site_decorator(child)
                if site is not None:
                    if site in out:
                        raise ValueError(
                            f"{path}: verdict site {site!r} on both {out[site]} and {name}"
                        )
                    out[site] = name
                visit(child, name + ".")

    visit(tree, "")
    return out


def _exempt_nodes(tree: ast.AST) -> set[int]:
    """Ids of every node inside a ``@verdict_site`` function."""

    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _site_decorator(node) is not None
        ):
            skip.update(id(n) for n in ast.walk(node))
    return skip


def _strings(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for elt in node.elts:
            yield from _strings(elt)


def _names(node: ast.AST) -> Iterator[str]:
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr


_NOT_CHECKED_NAMES = frozenset(
    {"Missing", "Errored", "not_checked", "NOT_CHECKED", "FailClosed"}
)
_NOT_CHECKED_STRINGS = frozenset(
    {"not_checked", "NOT_CHECKED", "INDETERMINATE", "unchecked", "unknown", "UNKNOWN"}
)


def _closed_exit(node: ast.AST) -> bool:
    """True for a ``return`` that hands back a not-checked value."""

    value = getattr(node, "value", None)
    if not isinstance(node, ast.Return) or value is None:
        return False
    return any(n in _NOT_CHECKED_NAMES for n in _names(value)) or any(
        isinstance(c, ast.Constant) and c.value in _NOT_CHECKED_STRINGS
        for c in ast.walk(value)
    )


_COUNTING = frozenset({"sum", "len", "Counter", "tally", "count"})


def count_file(path: Path, config: dict[str, Any]) -> Counter[str]:
    """Per-kind counts of hand-written folds in one Python file."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return Counter()
    skip = _exempt_nodes(tree)
    outcomes, success = config["_outcomes"], config["_success"]
    fold_names = config["_fold_names"]
    counts: Counter[str] = Counter()

    compared: set[int] = set()
    typed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            compared.update(id(n) for n in ast.walk(node))
        elif (
            isinstance(node, ast.Subscript)
            and getattr(node.value, "id", "") == "Literal"
        ):
            typed.update(id(n) for n in ast.walk(node))

    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Compare):
            values = [node.left, *node.comparators]
            if any(s in outcomes or s in success for v in values for s in _strings(v)):
                counts["cmp_literal"] += 1
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in success
            and id(node) not in compared
            and id(node) not in typed
        ):
            counts["says_success"] += 1
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("all", "any")
            and node.args
            and any(fold_names.search(n) for n in _names(node.args[0]))
        ):
            counts["bool_fold"] += 1
        elif isinstance(node, ast.ExceptHandler):
            broad = node.type is None or (
                isinstance(node.type, ast.Name)
                and node.type.id in ("Exception", "BaseException")
            )
            # Each way out of the handler is judged on its own: a raise on one
            # branch doesn't excuse a ``return True`` on another.
            body = ast.Module(body=node.body, type_ignores=[])
            if broad and any(
                not _closed_exit(n)
                for n in ast.walk(body)
                if isinstance(n, (ast.Return, ast.Continue, ast.Pass))
            ):
                counts["swallow"] += 1
        elif isinstance(node, ast.If) and node.orelse:
            tail = node.orelse
            if not (len(tail) == 1 and isinstance(tail[0], ast.If)):
                for stmt in tail:
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and any(
                        s in success for s in _strings(stmt.value or ast.Constant(None))
                    ):
                        counts["else_success"] += 1
    for stmt in statements:
        if id(stmt) in skip or isinstance(
            stmt,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.If,
                ast.For,
                ast.While,
                ast.With,
                ast.Try,
            ),
        ):
            continue
        sliced = any(
            isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Slice)
            and n.slice.lower is None
            and n.slice.upper is not None
            for n in ast.walk(stmt)
        )
        if sliced and any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", "")) in _COUNTING
            for n in ast.walk(stmt)
        ):
            counts["slice_count"] += 1
    return counts


def scope_files(config: dict[str, Any], root: Path = ROOT) -> Iterator[Path]:
    exempt = set(config["scope"]["exempt"])
    for pattern in config["scope"]["include"]:
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if path.is_file() and "__pycache__" not in path.parts and rel not in exempt:
                yield path


def scan(
    config: Optional[dict[str, Any]] = None, root: Path = ROOT
) -> dict[str, dict[str, int]]:
    config = config if config is not None else load_config()
    out: dict[str, dict[str, int]] = {}
    for path in scope_files(config, root):
        counts = count_file(path, config)
        if counts:
            out[path.relative_to(root).as_posix()] = {
                k: counts[k] for k in KINDS if counts[k]
            }
    return out


def totals(counts: dict[str, dict[str, int]]) -> Counter[str]:
    out: Counter[str] = Counter()
    for kinds in counts.values():
        out.update(kinds)
    return out


def compare(
    found: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Every kind whose total rose above the baseline's, with the files whose
    count rose. Totals, not per-file counts, decide: moving a fold to a new
    module (the decomposition plan splits large files) is not a new fold."""

    grown = []
    now, allowed = totals(found), totals(baseline)
    for kind in KINDS:
        if now[kind] > allowed[kind]:
            files = [
                f"{rel} {kinds[kind]} (baseline {baseline.get(rel, {}).get(kind, 0)})"
                for rel, kinds in sorted(found.items())
                if kinds.get(kind, 0) > baseline.get(rel, {}).get(kind, 0)
            ]
            grown.append(
                f"{kind}: {now[kind]} (baseline {allowed[kind]}); rose in "
                + ("; ".join(files) or "no single file")
            )
    return grown


def lowered(
    found: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline lowered to what is found; never raised."""

    out: dict[str, dict[str, int]] = {}
    for rel, kinds in baseline.items():
        for kind, allowed in kinds.items():
            n = min(allowed, found.get(rel, {}).get(kind, 0))
            if n:
                out.setdefault(rel, {})[kind] = n
    return dict(sorted(out.items()))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0], allow_abbrev=False
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Lower the baseline to the current counts (it never rises)",
    )
    args = parser.parse_args(argv)
    found = scan()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    grown = compare(found, baseline)
    if args.update_baseline:
        BASELINE.write_text(
            json.dumps(lowered(found, baseline), indent=2) + "\n", encoding="utf-8"
        )
        print(f"Baseline lowered: {BASELINE.relative_to(FLOW)}")
    if grown:
        print(
            "New hand-written verdict folds. Build the verdict on "
            "openadapt_flow.verdict.settle inside a function marked "
            "@verdict_site, and add its spec under tests/failopen/sites "
            "(docs/FAIL_CLOSED.md):",
            file=sys.stderr,
        )
        print("\n".join(f"  {line}" for line in grown), file=sys.stderr)
        return 1
    total = sum(n for kinds in baseline.values() for n in kinds.values())
    print(f"Verdict folds OK ({total} baseline folds left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
