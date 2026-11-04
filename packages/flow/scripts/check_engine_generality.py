"""Fail when the engine names a specific application.

The engine (``openadapt_flow/``) has no code written for one application. What
one application needs lives in data: ``app_profiles/*.yaml``, generated
contracts, deployment configs, or test fixtures and benchmark harnesses outside
the package. ``generality.toml`` beside this script holds the denylist of
application names and app-specific identifiers, and the few modules allowed to
name OpenAdapt's own demo apps.

A comment line may name an application as an example when it carries an
explicit ``# example:`` marker. Every other hit fails, unless it is in the
baseline: the hits that were already there when the check went live
(``docs/GENERALITY_AUDIT.md``). The baseline only shrinks. A file whose count
for a term rises fails; a file whose count falls must have its baseline entry
lowered in the same change (``--update-baseline`` writes it).

Usage::

    python scripts/check_engine_generality.py
    python scripts/check_engine_generality.py --update-baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent
ENGINE = FLOW / "openadapt_flow"
CONFIG = HERE / "generality.toml"
BASELINE = HERE / "generality_baseline.json"

#: Engine files that hold text: code and the data shipped with it.
SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".yaml", ".yml", ".toml", ".txt"}

#: The marker opens the comment: ``# example: ...`` or ``#: example: ...``.
EXAMPLE_MARKER = re.compile(r"#:?\s*example:", re.IGNORECASE)

#: A lower-case letter or digit followed by a capital: where a camelCase word
#: splits, so ``isOpenEMR`` reads as ``is Open EMR`` for the name patterns.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _term_patterns(config: dict) -> dict[str, tuple[re.Pattern[str], bool]]:
    """``{term: (pattern, is_name)}``; names also match inside camelCase words."""
    terms: dict[str, tuple[re.Pattern[str], bool]] = {}
    for section in ("names", "identifiers"):
        for term, pattern in config[section].items():
            terms[term] = (re.compile(pattern, re.IGNORECASE), section == "names")
    return terms


def _allowed(rel: str, term: str, config: dict) -> bool:
    for prefix in config["allow"]["paths"]:
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return True
    for prefix, terms in config["allow"].get("terms_in", {}).items():
        if (
            rel == prefix or rel.startswith(prefix.rstrip("/") + "/")
        ) and term in terms:
            return True
    return False


def scan(
    engine: Path = ENGINE, config: dict | None = None
) -> dict[str, dict[str, list[int]]]:
    """Return ``{relative path: {term: [line numbers]}}`` for every hit."""
    config = config if config is not None else load_config()
    terms = _term_patterns(config)
    hits: dict[str, dict[str, list[int]]] = {}
    for path in sorted(engine.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(engine).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if EXAMPLE_MARKER.match(stripped):
                continue
            split = _CAMEL_BOUNDARY.sub(" ", line)
            for term, (pattern, is_name) in terms.items():
                found = pattern.search(split if is_name else line)
                if found and not _allowed(rel, term, config):
                    hits.setdefault(rel, {}).setdefault(term, []).append(number)
    return hits


def counts(hits: dict[str, dict[str, list[int]]]) -> dict[str, dict[str, int]]:
    return {
        rel: {term: len(lines) for term, lines in sorted(by_term.items())}
        for rel, by_term in sorted(hits.items())
    }


def compare(
    hits: dict[str, dict[str, list[int]]], baseline: dict[str, dict[str, int]]
) -> tuple[list[str], list[str]]:
    """Return (new hits, stale baseline entries)."""
    new: list[str] = []
    stale: list[str] = []
    seen = Counter()
    for rel, by_term in hits.items():
        for term, lines in by_term.items():
            allowed = baseline.get(rel, {}).get(term, 0)
            seen[(rel, term)] = len(lines)
            if len(lines) > allowed:
                where = ", ".join(str(n) for n in lines)
                new.append(
                    f"openadapt_flow/{rel}: {term!r} on line(s) {where} "
                    f"({len(lines)} found, {allowed} in the baseline)"
                )
    for rel, by_term in baseline.items():
        for term, allowed in by_term.items():
            if seen[(rel, term)] < allowed:
                stale.append(
                    f"openadapt_flow/{rel}: {term!r} baseline {allowed}, "
                    f"now {seen[(rel, term)]}"
                )
    return new, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Lower the baseline to the current hits (it never grows)",
    )
    args = parser.parse_args(argv)

    hits = scan()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    new, stale = compare(hits, baseline)

    if args.update_baseline:
        if new:
            print("New hits cannot enter the baseline:", file=sys.stderr)
            print("\n".join(f"  {line}" for line in new), file=sys.stderr)
            return 1
        BASELINE.write_text(json.dumps(counts(hits), indent=2) + "\n", encoding="utf-8")
        print(f"Baseline lowered: {BASELINE.relative_to(FLOW)}")
        return 0

    if new:
        print(
            "The engine names a specific application. Put what one app needs "
            "in data (app_profiles/*.yaml, a generated contract, a deployment "
            "config) or in a fixture outside openadapt_flow/; see "
            "CONTRIBUTING.md, 'App knowledge lives in data'. A comment that "
            "only gives an example may carry '# example:'.",
            file=sys.stderr,
        )
        print("\n".join(f"  {line}" for line in new), file=sys.stderr)
    if stale:
        print(
            "The baseline is higher than the engine now; lower it with "
            "`python scripts/check_engine_generality.py --update-baseline`:",
            file=sys.stderr,
        )
        print("\n".join(f"  {line}" for line in stale), file=sys.stderr)
    if new or stale:
        return 1
    total = sum(n for by_term in baseline.values() for n in by_term.values())
    print(f"Engine generality OK ({total} baseline hits left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
