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
    "swallow_wide",
    "slice_count",
    "else_success",
    "says_unchanged",
    "empty_change_set",
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
    config["_unchanged"] = frozenset(config["literals"]["unchanged"])
    config["_change_names"] = re.compile(config["change_set"]["names"], re.IGNORECASE)
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
            else:
                # Inside an ``if``, ``try`` or ``with``: same scope, same prefix.
                visit(child, prefix)

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


def _not_checked_value(value: ast.AST) -> bool:
    """True when every value the expression can produce is a not-checked one."""

    if isinstance(value, ast.IfExp):
        return _not_checked_value(value.body) and _not_checked_value(value.orelse)
    if isinstance(value, ast.Constant):
        return value.value in _NOT_CHECKED_STRINGS
    if isinstance(value, ast.Name):
        return value.id in _NOT_CHECKED_NAMES
    if isinstance(value, ast.Attribute):
        return value.attr in _NOT_CHECKED_NAMES or value.attr in _NOT_CHECKED_STRINGS
    if isinstance(value, ast.Call):
        return _not_checked_value(value.func)
    if isinstance(value, (ast.Tuple, ast.List)):
        return bool(value.elts) and _not_checked_value(value.elts[0])
    return False


def _closed_exit(node: ast.AST) -> bool:
    """True for a ``return`` that hands back a not-checked value."""

    value = getattr(node, "value", None)
    if not isinstance(node, ast.Return) or value is None:
        return False
    return _not_checked_value(value)


_BROAD = frozenset({"Exception", "BaseException"})


def _broad(handler: ast.ExceptHandler) -> bool:
    """A bare ``except``, or one naming ``Exception``/``BaseException`` by
    name, by attribute (``builtins.Exception``) or inside a tuple."""

    kind = handler.type
    if kind is None:
        return True
    kinds = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(
        (isinstance(k, ast.Name) and k.id in _BROAD)
        or (isinstance(k, ast.Attribute) and k.attr in _BROAD)
        for k in kinds
    )


def _falls_through(body: list[ast.stmt]) -> bool:
    """True when running ``body`` can reach its end (no raise, return,
    continue or break on every path). Loops, ``with``, ``try`` and ``match``
    are taken to fall through unless every branch they hold ends."""

    for stmt in body:
        if isinstance(stmt, (ast.Raise, ast.Return, ast.Continue, ast.Break)):
            return False
        if isinstance(stmt, ast.If):
            if not _falls_through(stmt.body) and not _falls_through(stmt.orelse):
                return False
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            if not _falls_through(stmt.body):
                return False
        elif isinstance(stmt, ast.Try):
            ends = not _falls_through(stmt.body + stmt.orelse) and all(
                not _falls_through(h.body) for h in stmt.handlers
            )
            if ends or (stmt.finalbody and not _falls_through(stmt.finalbody)):
                return False
    return True


def _records_not_checked(body: list[ast.stmt]) -> bool:
    """True when the handler assigns or appends a not-checked value, so a
    fall-through hands on "not checked" rather than nothing."""

    wrapper = ast.Module(body=body, type_ignores=[])
    for node in ast.walk(wrapper):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and (
            node.value is not None and _not_checked_value(node.value)
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and node.args
            and any(_not_checked_value(a) for a in node.args)
        ):
            return True
    return False


_COUNTING = frozenset({"sum", "len", "Counter", "tally", "count"})


def count_file(path: Path, config: dict[str, Any]) -> Counter[str]:
    """Per-kind counts of hand-written folds in one Python file."""

    # A file the check can't read is an error, never a file with no folds.
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _exempt_nodes(tree)
    outcomes, success = config["_outcomes"], config["_success"]
    fold_names, unchanged = config["_fold_names"], config["_unchanged"]
    change_names = config["_change_names"]
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
        if _empty_change_set(node, change_names):
            counts["empty_change_set"] += 1
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
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in unchanged
            and id(node) not in compared
            and id(node) not in typed
        ):
            counts["says_unchanged"] += 1
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("all", "any")
            and node.args
            and any(fold_names.search(n) for n in _names(node.args[0]))
        ):
            counts["bool_fold"] += 1
        elif isinstance(node, ast.ExceptHandler) and _broad(node):
            # Each way out of the handler is judged on its own: a raise on one
            # branch doesn't excuse a ``return True`` on another.
            body = ast.Module(body=node.body, type_ignores=[])
            exits = [
                n
                for n in ast.walk(body)
                if isinstance(n, (ast.Return, ast.Continue, ast.Break, ast.Pass))
            ]
            named = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in _BROAD
            )
            if named and any(
                not _closed_exit(n) for n in exits if not isinstance(n, ast.Break)
            ):
                counts["swallow"] += 1
            elif any(not _closed_exit(n) for n in exits) or (
                _falls_through(node.body) and not _records_not_checked(node.body)
            ):
                # What ``swallow`` misses: ``except Exception: logger.error(...)``
                # and carry on, so the code after the handler runs as if the
                # call had worked; a ``break``; and ``Exception`` named in a
                # tuple or by attribute.
                counts["swallow_wide"] += 1
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


def _empty_change_set(node: ast.AST, names: "re.Pattern[str]") -> bool:
    """A "nothing else changed" test written by hand: ``not changes``,
    ``len(changes) == 0``, ``changes == []`` or ``not any(changes)`` over a
    set of changes, collateral, diffs or deletes. The set may be a partial
    read, so emptiness is not evidence that nothing changed; the kernel's
    ``Result.nothing_else`` holds only on a complete read."""

    def about_changes(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Call) and getattr(expr.func, "id", "") in (
            "any",
            "len",
            "bool",
        ):
            return bool(expr.args) and about_changes(expr.args[0])
        if isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return any(about_changes(g.iter) for g in expr.generators)
        if isinstance(expr, ast.Name):
            return bool(names.fullmatch(expr.id))
        if isinstance(expr, ast.Attribute):
            return bool(names.fullmatch(expr.attr))
        return False

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return about_changes(node.operand)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.Eq, ast.Is))
    ):
        left, right = node.left, node.comparators[0]
        empty = (
            (isinstance(right, ast.Constant) and right.value == 0)
            or (isinstance(right, (ast.List, ast.Tuple, ast.Set)) and not right.elts)
            or (
                isinstance(right, ast.Call)
                and getattr(right.func, "id", "") in ("set", "list", "tuple", "dict")
                and not right.args
            )
        )
        return empty and about_changes(left)
    return False


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
    """The baseline lowered to what is found, per kind; a kind's total never
    rises. A kind whose total fell (or held) takes the counts found, so a fold
    that moved files moves its baseline entry too. A kind whose total rose
    keeps its old entries."""

    now, allowed = totals(found), totals(baseline)
    source = {k: (found if now[k] <= allowed[k] else baseline) for k in KINDS}
    out: dict[str, dict[str, int]] = {}
    for kind in KINDS:
        for rel, kinds in source[kind].items():
            if kinds.get(kind, 0):
                out.setdefault(rel, {})[kind] = kinds[kind]
    return {rel: dict(sorted(k.items())) for rel, k in sorted(out.items())}


_SPEC_KINDS = frozenset({"site", "owned", "selftest"})


def spec_ids(source: str) -> set[str]:
    """The site ids a spec file registers: an ``id=`` keyword, or a string
    passed to the call given to ``register(...)`` (a spec factory). Any other
    quoted word in the file (``"held"``, ``"C1"``) is not an id."""

    ids: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "id"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                ids.add(kw.value.value)
        if getattr(node.func, "id", "") == "register" and node.args:
            inner = node.args[0]
            if isinstance(inner, ast.Call):
                ids.update(
                    a.value
                    for a in inner.args
                    if isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                    and a.value not in _SPEC_KINDS
                )
    return ids


def unregistered_sites(root: Path = ROOT) -> list[str]:
    """Decorated sites whose id appears in no spec under tests/failopen/sites.

    The meta-test (tests/failopen/test_registry_complete.py) checks the same
    thing and runs the specs, but only when the Flow tests run; this runs on
    every change, so a launcher or scripts/ci site can't skip its spec.
    """

    config = load_config()
    specs = root / "packages/flow/tests/failopen/sites"
    ids = {i for f in specs.glob("*.py") for i in spec_ids(f.read_text("utf-8"))}
    missing = []
    for path in scope_files(config, root):
        for site, name in decorated_sites(path).items():
            if site not in ids:
                missing.append(f"{path.relative_to(root).as_posix()}: {name} ({site})")
    return missing


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
    unspecced = unregistered_sites()
    if unspecced:
        print(
            "A @verdict_site has no spec under packages/flow/tests/failopen/sites "
            "(docs/FAIL_CLOSED.md, 'Register a site'):",
            file=sys.stderr,
        )
        print("\n".join(f"  {line}" for line in unspecced), file=sys.stderr)
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
    if grown or unspecced:
        return 1
    total = sum(n for kinds in baseline.values() for n in kinds.values())
    print(f"Verdict folds OK ({total} baseline folds left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
