#!/usr/bin/env python3
"""Check user-facing text against the OpenAdapt vocabulary registry.

Standard library only. Any repository with Python 3.10 or later can run it,
installed or copied::

    openadapt-vocab-lint terms --level business README.md
    python -m openadapt_flow.contracts.vocabulary claims src/
    python path/to/vocabulary/lint.py coverage \\
        --enum openadapt_flow.transaction:TransactionOutcome=transaction_outcome
    python path/to/vocabulary/lint.py check --config .vocab.json

Four commands:

``terms``
    A file at one surface level must not show a technical term whose
    ``min_level`` is deeper (``vocabulary.json``).
``claims``
    A sentence that claims no write happened, or that a retry is safe, must
    sit near an outcome that proves no effect. A coarse HALTED or "stopped"
    alone never proves it.
``coverage``
    Every value of an outcome enum must map to a plain result
    (``outcomes.json``), so a new engine value can't reach a person unmapped.
    ``--enum`` takes an Enum class, a ``Literal`` alias, or a Pydantic model
    field whose annotation holds Literals or Enums.
``check``
    Runs whatever a ``.vocab.json`` file asks for.

Exit status: 0 when clean (warnings allowed), 1 when there is an error finding
(or any finding with ``--strict``), 2 for a usage or registry error.

Two escape hatches. Both need a reason after ``--``:

* ``vocab-allow: <term or rule> -- <reason>`` on the same line or the line
  above silences that term (or ``no-change-claim``) there.
* Lines between ``vocab: advanced`` and ``vocab: end`` (in any comment
  syntax), and inside ``<details data-level="advanced">``, are skipped by
  ``terms``. Use them for a Technical details section.

To use it from another repository without installing this package, copy the
whole ``vocabulary`` directory (this file and the two JSON files) and run the
copied ``lint.py``. It finds the registry next to itself.
"""

from __future__ import annotations

import argparse
import ast
import enum
import fnmatch
import importlib
import json
import re
import sys
import typing
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REGISTRY_DIR = Path(__file__).resolve().parent
VOCABULARY_FILE = "vocabulary.json"
OUTCOMES_FILE = "outcomes.json"
VECTORS_FILE = "outcome-vectors.json"
VOCABULARY_SCHEMA = "openadapt.vocabulary/v1"
OUTCOMES_SCHEMA = "openadapt.plain-outcomes/v1"
VECTORS_SCHEMA = "openadapt.plain-outcome-vectors/v1"
LEVELS = ("business", "detail", "engineering")
SEVERITIES = ("error", "warn")
RESULTS = (
    "done",
    "stopped_before_change",
    "check_the_record",
    "finished_not_checked",
    "did_not_finish",
    "refused",
)
PROOFS = ("sealed", "local", "none")
FALLBACKS = ("conflict", "unrecognized", "missing", "delivery_uncertain")
#: Results whose label claims nothing changed.
NO_CHANGE_RESULTS = frozenset({"stopped_before_change", "refused"})
#: Results that can never claim nothing changed or invite a retry.
MAY_CHANGE_RESULTS = frozenset({"done", "check_the_record", "finished_not_checked"})
NO_CHANGE_CLAIM_RULE = "no-change-claim"
#: The only rows allowed to say nothing changed. Each one is an engine value
#: that, by its own contract, proves the run made no business effect (Flow
#: transaction.py, Execute v1), or a refusal before anything ran. A
#: ``.if_not_dispatched`` row applies only when the caller states the request
#: never reached the app. Adding a row here is a safety review, not a copy edit.
NO_EFFECT_PROOFS = frozenset(
    {
        "sources.transaction_outcome.HALTED_BEFORE_EFFECT",
        "sources.transaction_outcome.FAILED_PLATFORM",
        "sources.transaction_outcome.CANCELED",
        "sources.transaction_outcome.REJECTED_POLICY",
        "sources.execute_terminal_outcome.halted_before_effect",
        "sources.execute_terminal_outcome.rejected_policy",
        "sources.execute_terminal_outcome.failed_platform",
        "sources.agent_status.refused",
        "sources.agent_status.timeout.if_not_dispatched",
        "sources.agent_status.error.if_not_dispatched",
        "sources.connector_status.failed.if_not_dispatched",
    }
)

_TEXT_SUFFIXES = {".md", ".mdx", ".markdown", ".txt", ".rst"}
_HTML_SUFFIXES = {".html", ".htm"}
_SCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_LINTED_SUFFIXES = _TEXT_SUFFIXES | _HTML_SUFFIXES | _SCRIPT_SUFFIXES | {".py", ".json"}
_SKIPPED_DIRS = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".next",
    ".pytest_cache",
}
# Names hold only the characters a term id, a term, or a rule uses. That keeps
# this pattern's own source, and any code that mentions the pragma, from
# reading as a malformed pragma when a repository lints a vendored copy.
_PRAGMA = re.compile(
    r"vocab-allow:[ \t]*(?P<names>[\w ,.+'-]*?)"
    r"(?:\s+--\s+(?P<reason>\S[^\n]*?))?\s*(?:\*/|-->)?\s*$"
)
_ADVANCED_START = re.compile(r"vocab:\s*advanced\b")
_ADVANCED_END = re.compile(r"vocab:\s*end\b")
_DETAILS_START = re.compile(
    r"<details\b[^>]*data-level=[\"']advanced[\"']", re.IGNORECASE
)
_DETAILS_END = re.compile(r"</details>", re.IGNORECASE)
# The code just before a string literal that marks it as not user-facing.
_NON_PROSE_CONTEXT = re.compile(
    r"(className|class|href|src|key|id|style|import|from|require\(|data-[\w-]+)\s*[=:]?\s*\{?\s*$"
)
# The code just before a single-word literal that marks it as a visible label.
_LABEL_CONTEXT = re.compile(
    r"\b(title|label|aria-label|placeholder|alt|eyebrow|sub|heading|headline|"
    r"description|text|message|status)\s*[=:]\s*\{?\s*$"
)
_JSX_TEXT = re.compile(r">([^<>{}]*[A-Za-z][^<>{}]*)<")
_CODE_IN_TEXT = re.compile(r"[;=]|=>|\(\)|\b(const|let|return|function)\b")


class RegistryError(ValueError):
    """The registry files are missing, malformed, or break a safety rule."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.severity} [{self.rule}] {self.message}"


@dataclass(frozen=True)
class Fragment:
    """User-visible text, the 1-based line it starts on, and where it ends.

    ``end_line`` matters for text built from several source lines, such as
    adjacent Python string literals that the parser joins into one.
    """

    line: int
    text: str
    end_line: int | None = None

    @property
    def last_line(self) -> int:
        return max(self.end_line or self.line, self.line + self.text.count("\n"))


def _locate(lines: Sequence[str], fragment: Fragment, pattern: re.Pattern[str]) -> int:
    """The source line inside ``fragment`` where ``pattern`` first matches."""

    for number in range(fragment.line, fragment.last_line + 1):
        if 0 < number <= len(lines) and pattern.search(lines[number - 1]):
            return number
    return fragment.line


@dataclass(frozen=True)
class TermRule:
    term_id: str
    term: str
    pattern: re.Pattern[str]
    min_level: str
    severity: str
    say_instead: str


# ---------------------------------------------------------------- registry


def _load_json(directory: Path | None, name: str, schema: str) -> dict[str, Any]:
    path = (directory or REGISTRY_DIR) / name
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != schema:
        raise RegistryError(f"{path} is not {schema}")
    return document


def load_vocabulary(directory: Path | None = None) -> dict[str, Any]:
    """Load and validate ``vocabulary.json``."""

    vocabulary = _load_json(directory, VOCABULARY_FILE, VOCABULARY_SCHEMA)
    validate_vocabulary(vocabulary)
    return vocabulary


def load_outcome_table(directory: Path | None = None) -> dict[str, Any]:
    """Load and validate ``outcomes.json``."""

    table = _load_json(directory, OUTCOMES_FILE, OUTCOMES_SCHEMA)
    validate_outcome_table(table)
    return table


def load_registry(
    directory: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load both files and check that they agree with each other."""

    vocabulary = load_vocabulary(directory)
    table = load_outcome_table(directory)
    validate_registry_pair(vocabulary, table)
    return vocabulary, table


def load_outcome_vectors(directory: Path | None = None) -> dict[str, Any]:
    """Load ``outcome-vectors.json``, the conformance cases for other languages."""

    return _load_json(directory, VECTORS_FILE, VECTORS_SCHEMA)


def _flags(case_sensitive: object) -> int:
    if not isinstance(case_sensitive, bool):
        raise RegistryError("case_sensitive must be true or false")
    return 0 if case_sensitive else re.IGNORECASE


def validate_vocabulary(vocabulary: dict[str, Any]) -> None:
    levels = [level.get("id") for level in vocabulary.get("levels", [])]
    if tuple(levels) != LEVELS:
        raise RegistryError(f"levels must be {LEVELS}")
    seen: set[str] = set()
    for term in vocabulary.get("terms", []):
        term_id = term.get("id")
        if not isinstance(term_id, str) or not term_id or term_id in seen:
            raise RegistryError(f"term id {term_id!r} is missing or repeated")
        seen.add(term_id)
        if not isinstance(term.get("meaning"), str) or not term["meaning"]:
            raise RegistryError(f"term {term_id} needs a one-line meaning")
        if term.get("business_term") is None and not term.get("use_instead"):
            raise RegistryError(f"term {term_id} needs a business_term or use_instead")
        if "result" in term and term["result"] not in RESULTS:
            raise RegistryError(
                f"term {term_id} names unknown result {term['result']!r}"
            )
        for variant in term.get("technical", []):
            if variant.get("min_level") not in LEVELS[1:]:
                raise RegistryError(
                    f"{term_id}/{variant.get('term')}: min_level must be detail or engineering"
                )
            if variant.get("severity") not in SEVERITIES:
                raise RegistryError(
                    f"{term_id}/{variant.get('term')}: severity must be error or warn"
                )
            try:
                re.compile(variant["pattern"], _flags(variant.get("case_sensitive")))
            except (KeyError, re.error) as exc:
                raise RegistryError(
                    f"{term_id}/{variant.get('term')}: bad pattern: {exc}"
                ) from exc
    claims = vocabulary.get("no_change_claims")
    if not isinstance(claims, dict):
        raise RegistryError("no_change_claims is missing")
    try:
        for pattern in claims["claim_patterns"]:
            re.compile(pattern, re.IGNORECASE)
        re.compile(claims["stop_context"], re.IGNORECASE)
        re.compile(claims["proof_tokens"])
    except (KeyError, TypeError, re.error) as exc:
        raise RegistryError(f"no_change_claims is malformed: {exc}") from exc
    if not isinstance(claims.get("window_lines"), int) or claims["window_lines"] < 0:
        raise RegistryError(
            "no_change_claims.window_lines must be a non-negative integer"
        )


def _check_row(where: str, row: object) -> None:
    if not isinstance(row, dict):
        raise RegistryError(f"{where} must be an object")
    result = row.get("result")
    may = row.get("may_have_changed")
    safe = row.get("safe_to_retry")
    if result not in RESULTS:
        raise RegistryError(f"{where}: unknown result {result!r}")
    if not isinstance(may, bool) or not isinstance(safe, bool):
        raise RegistryError(
            f"{where}: may_have_changed and safe_to_retry must be booleans"
        )
    if not isinstance(row.get("next_action"), str) or not row["next_action"]:
        raise RegistryError(f"{where}: next_action is required")
    # The safety rule. Only an outcome that proves no effect may say that
    # nothing changed or invite a retry.
    if safe and may:
        raise RegistryError(f"{where}: safe_to_retry needs may_have_changed=false")
    if result in NO_CHANGE_RESULTS and may:
        raise RegistryError(
            f"{where}: {result} claims nothing changed, so may_have_changed must be false"
        )
    if result in MAY_CHANGE_RESULTS and (not may or safe):
        raise RegistryError(
            f"{where}: {result} must keep may_have_changed=true and safe_to_retry=false"
        )
    if "if_not_dispatched" in row:
        _check_row(f"{where}.if_not_dispatched", row["if_not_dispatched"])


def _rows(table: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every result row in the table, with where it lives."""

    for source, spec in table["sources"].items():
        for value, row in spec["values"].items():
            yield f"sources.{source}.{value}", row
            if "if_not_dispatched" in row:
                yield (
                    f"sources.{source}.{value}.if_not_dispatched",
                    row["if_not_dispatched"],
                )
    for name, row in table["fallbacks"].items():
        yield f"fallbacks.{name}", row


def validate_outcome_table(table: dict[str, Any]) -> None:
    results = table.get("results")
    if not isinstance(results, dict) or tuple(results) != RESULTS:
        raise RegistryError(f"results must list {RESULTS} in order")
    for result, spec in results.items():
        if (
            not isinstance(spec, dict)
            or not spec.get("label")
            or not spec.get("meaning")
        ):
            raise RegistryError(f"results.{result} needs a label and a meaning")
    if tuple(table.get("proof", {})) != PROOFS:
        raise RegistryError(f"proof must define {PROOFS} in order")
    sources = table.get("sources")
    precedence = table.get("precedence")
    tiers = table.get("tiers")
    if not isinstance(sources, dict) or not isinstance(precedence, list):
        raise RegistryError("sources and precedence are required")
    if set(precedence) != set(sources) or len(precedence) != len(sources):
        raise RegistryError("precedence must name every source once")
    if not isinstance(tiers, list) or not all(
        isinstance(tier, list) and tier for tier in tiers
    ):
        raise RegistryError("tiers must be a list of non-empty lists")
    flat = [source for tier in tiers for source in tier]
    if flat != precedence:
        raise RegistryError("tiers must name every source once, in precedence order")
    for source, spec in sources.items():
        values = spec.get("values") if isinstance(spec, dict) else None
        if not isinstance(values, dict) or not values:
            raise RegistryError(f"sources.{source}.values is required")
        if not isinstance(spec.get("meaning"), str) or not spec["meaning"]:
            raise RegistryError(f"sources.{source} needs a meaning")
    fallbacks = table.get("fallbacks")
    if not isinstance(fallbacks, dict) or tuple(sorted(fallbacks)) != tuple(
        sorted(FALLBACKS)
    ):
        raise RegistryError(f"fallbacks must define {FALLBACKS}")
    for name, row in fallbacks.items():
        _check_row(f"fallbacks.{name}", row)
        if row["result"] != "check_the_record":
            raise RegistryError(
                f"fallbacks.{name} must fail closed to check_the_record"
            )
    for where, row in _rows(table):
        _check_row(where, row)
        if not row["may_have_changed"] and where not in NO_EFFECT_PROOFS:
            raise RegistryError(
                f"{where} says nothing changed, but that value doesn't prove no effect"
            )
    coarse = set(sources.get("execution_outcome", {}).get("values", {}))
    for value, row in sources.get("transaction_outcome", {}).get("values", {}).items():
        allowed = row.get("execution_outcomes")
        if not isinstance(allowed, list) or not allowed or not set(allowed) <= coarse:
            raise RegistryError(
                f"sources.transaction_outcome.{value}.execution_outcomes is invalid"
            )
    for kind in ("in_progress", "not_an_outcome"):
        for source, values in table.get(kind, {}).items():
            if source not in sources:
                raise RegistryError(f"{kind}.{source} is not a source")
            overlap = set(values) & set(sources[source]["values"])
            if overlap:
                raise RegistryError(
                    f"{kind}.{source} repeats mapped values {sorted(overlap)}"
                )
    # Execute states carry the progress labels. Every other source's
    # in-progress value names the Execute state it reads as, so all surfaces
    # share three progress labels.
    progress_states = table.get("in_progress", {}).get("execute_state", {})
    for source, states in table.get("in_progress", {}).items():
        for value, spec in states.items():
            if not isinstance(spec, dict):
                raise RegistryError(f"in_progress.{source}.{value} must be an object")
            if source == "execute_state":
                if not spec.get("label") or not spec.get("next_action"):
                    raise RegistryError(
                        f"in_progress.{source}.{value} needs a label and a next_action"
                    )
            elif spec.get("state") not in progress_states:
                raise RegistryError(
                    f"in_progress.{source}.{value} must name an in_progress.execute_state entry as its state"
                )
    for emitter in table.get("emitters", []):
        if emitter.get("source") not in sources:
            raise RegistryError(
                f"emitter {emitter.get('symbol')!r} names unknown source"
            )
        if (
            not emitter.get("repository")
            or not emitter.get("symbol")
            or not emitter.get("read_at")
        ):
            raise RegistryError(
                "every emitter needs a repository, a symbol, and read_at"
            )
        if not isinstance(emitter.get("values"), list) or not emitter["values"]:
            raise RegistryError(f"emitter {emitter['symbol']!r} needs its values")


def validate_registry_pair(vocabulary: dict[str, Any], table: dict[str, Any]) -> None:
    """The two files must use one label per result, and the words must hold.

    * A vocabulary term that names a result uses that result's label, and every
      result has such a term.
    * A technical term that is itself an outcome value, such as ``HALTED``,
      sits under a result that value reads as on its own. Otherwise the lint
      would suggest plain words that claim more than the value proves.
    * A row that may have changed something never carries a no-change claim.
    """

    reads_as: dict[str, set[str]] = {}
    for spec in table["sources"].values():
        for value, row in spec["values"].items():
            reads_as.setdefault(value, set()).add(row["result"])
    for term in vocabulary["terms"]:
        result = term.get("result")
        for variant in term.get("technical", []):
            results = reads_as.get(variant["term"])
            if result is not None and results is not None and result not in results:
                raise RegistryError(
                    f"{term['id']}/{variant['term']}: {variant['term']} on its own reads as "
                    f"{sorted(results)}, not {result}"
                )

    named: dict[str, str] = {}
    for term in vocabulary["terms"]:
        result = term.get("result")
        if result is None:
            continue
        if result in named:
            raise RegistryError(f"result {result} has two vocabulary terms")
        named[result] = term["business_term"]
    for result in RESULTS:
        label = table["results"][result]["label"]
        if named.get(result) != label:
            raise RegistryError(
                f"vocabulary.json calls {result} {named.get(result)!r}, outcomes.json calls it {label!r}"
            )
    claims = [
        re.compile(p, re.IGNORECASE)
        for p in vocabulary["no_change_claims"]["claim_patterns"]
    ]
    for where, row in _rows(table):
        if not row["may_have_changed"]:
            continue
        for claim in claims:
            match = claim.search(row["next_action"])
            if match:
                raise RegistryError(
                    f"{where}: may have changed something but next_action says {match.group(0)!r}"
                )


def known_values(table: dict[str, Any], source: str) -> set[str]:
    """Every value of ``source`` the table accounts for, results or not."""

    if source not in table["sources"]:
        raise KeyError(source)
    return (
        set(table["sources"][source]["values"])
        | set(table.get("in_progress", {}).get(source, {}))
        | set(table.get("not_an_outcome", {}).get(source, []))
    )


def term_rules(vocabulary: dict[str, Any], level: str) -> list[TermRule]:
    """Technical terms a file at ``level`` must not show."""

    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    depth = LEVELS.index(level)
    rules: list[TermRule] = []
    for term in vocabulary["terms"]:
        say = term.get("business_term") or term.get("use_instead")
        for variant in term.get("technical", []):
            if LEVELS.index(variant["min_level"]) <= depth:
                continue
            rules.append(
                TermRule(
                    term_id=term["id"],
                    term=variant["term"],
                    pattern=re.compile(
                        variant["pattern"], _flags(variant["case_sensitive"])
                    ),
                    min_level=variant["min_level"],
                    severity=variant["severity"],
                    say_instead=say,
                )
            )
    return rules


# -------------------------------------------------------------- extraction


def _blank(text: str) -> str:
    """Keep the newlines of ``text`` and nothing else, so line numbers hold."""

    return "\n" * text.count("\n")


def _markdown_fragments(lines: Sequence[str]) -> Iterator[Fragment]:
    fence = ""
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            if not fence:
                fence = marker.group(1)[:3]
            elif stripped.startswith(fence):
                fence = ""
            continue
        if fence:
            continue
        text = re.sub(r"`[^`]*`", " ", raw)
        text = re.sub(r"\]\([^)]*\)", "]", text)
        text = re.sub(r"<!--.*?-->", " ", text)
        text = re.sub(r"https?://\S+", " ", text)
        yield Fragment(number, text)


def _html_fragments(text: str) -> Iterator[Fragment]:
    text = re.sub(
        r"(?is)<(script|style)\b.*?</\1\s*>", lambda m: _blank(m.group(0)), text
    )
    text = re.sub(r"(?s)<!--.*?-->", lambda m: _blank(m.group(0)), text)
    text = re.sub(r"(?s)<[^>]+>", lambda m: " " + _blank(m.group(0)), text)
    for number, line in enumerate(text.split("\n"), 1):
        yield Fragment(number, line)


def _python_fragments(text: str) -> Iterator[Fragment]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        yield from (Fragment(n, line) for n, line in enumerate(text.split("\n"), 1))
        return
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and re.search(r"\s", node.value.strip())
        ):
            yield Fragment(node.lineno, node.value, getattr(node, "end_lineno", None))


def _json_fragments(text: str) -> Iterator[Fragment]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        yield from (Fragment(n, line) for n, line in enumerate(text.split("\n"), 1))
        return

    def strings(value: object) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)

    cursor = 0
    for value in strings(document):
        if not re.search(r"\s", value.strip()):
            continue
        encoded = json.dumps(value)[1:-1]
        offset = text.find(encoded, cursor)
        if offset < 0:
            offset = text.find(encoded)
        else:
            cursor = offset + len(encoded)
        line = text.count("\n", 0, offset) + 1 if offset >= 0 else 1
        yield Fragment(line, value)


def _scan_script(text: str) -> tuple[list[tuple[int, str, str]], str]:
    """Split JS or TS into string literals and code with literals blanked.

    A heuristic tokenizer, not a parser. It's good enough to find prose and to
    keep identifiers, imports, and class names out of the term check.
    """

    literals: list[tuple[int, str, str]] = []
    code: list[str] = []
    tail = ""
    i, n, line = 0, len(text), 1
    while i < n:
        char = text[i]
        pair = text[i : i + 2]
        if pair == "//":
            end = text.find("\n", i)
            end = n if end == -1 else end
            code.append(" " * (end - i))
            i = end
            continue
        if pair == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            segment = text[i:end]
            code.append(re.sub(r"[^\n]", " ", segment))
            line += segment.count("\n")
            i = end
            continue
        if char in "'\"`":
            j, buffer = i + 1, []
            while j < n:
                current = text[j]
                if current == "\\" and j + 1 < n:
                    buffer.append(text[j + 1])
                    j += 2
                    continue
                if current == char or (current == "\n" and char != "`"):
                    break
                if char == "`" and text[j : j + 2] == "${":
                    depth, k = 0, j + 1
                    while k < n:
                        depth += {"{": 1, "}": -1}.get(text[k], 0)
                        if depth == 0:
                            break
                        k += 1
                    buffer.append(" ")
                    j = k + 1
                    continue
                buffer.append(current)
                j += 1
            segment = text[i : j + 1]
            literals.append((line, "".join(buffer), tail))
            code.append(re.sub(r"[^\n]", " ", segment))
            tail = ""
            line += segment.count("\n")
            i = j + 1
            continue
        code.append(char)
        tail = (tail + char)[-48:]
        if char == "\n":
            line += 1
        i += 1
    return literals, "".join(code)


def _script_fragments(text: str) -> Iterator[Fragment]:
    literals, code = _scan_script(text)
    for line, value, tail in literals:
        if _NON_PROSE_CONTEXT.search(tail):
            continue
        if re.search(r"\s", value.strip()) or (
            re.fullmatch(r"[A-Za-z][A-Za-z-]*", value) and _LABEL_CONTEXT.search(tail)
        ):
            yield Fragment(line, value)
    for match in _JSX_TEXT.finditer(code):
        value = match.group(1)
        if value.strip() and not _CODE_IN_TEXT.search(value):
            yield Fragment(code.count("\n", 0, match.start(1)) + 1, value)


def extract_fragments(path: Path, text: str) -> list[Fragment]:
    """The user-visible text in one file, by type."""

    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return list(_markdown_fragments(text.split("\n")))
    if suffix in _HTML_SUFFIXES:
        return list(_html_fragments(text))
    if suffix == ".py":
        return list(_python_fragments(text))
    if suffix == ".json":
        return list(_json_fragments(text))
    if suffix in _SCRIPT_SUFFIXES:
        return list(_script_fragments(text))
    return [Fragment(n, line) for n, line in enumerate(text.split("\n"), 1)]


def _advanced_lines(lines: Sequence[str]) -> set[int]:
    skipped: set[int] = set()
    inside = False
    for number, line in enumerate(lines, 1):
        if _ADVANCED_START.search(line) or _DETAILS_START.search(line):
            inside = True
        if inside:
            skipped.add(number)
        if inside and (_ADVANCED_END.search(line) or _DETAILS_END.search(line)):
            inside = False
    return skipped


def _pragmas(
    path: str, lines: Sequence[str]
) -> tuple[dict[int, set[str]], list[Finding]]:
    allowed: dict[int, set[str]] = {}
    problems: list[Finding] = []
    for number, line in enumerate(lines, 1):
        match = _PRAGMA.search(line)
        if not match:
            continue
        names = {
            name.strip().lower()
            for name in match.group("names").split(",")
            if name.strip()
        }
        if not names or not match.group("reason"):
            problems.append(
                Finding(
                    path,
                    number,
                    "pragma",
                    "error",
                    "vocab-allow needs a term and a reason: vocab-allow: <term> -- <reason>",
                )
            )
            continue
        for target in (number, number + 1):
            allowed.setdefault(target, set()).update(names)
    return allowed, problems


def _allowed(allowed: dict[int, set[str]], line: int, *names: str) -> bool:
    here = allowed.get(line, set())
    return any(name.lower() in here for name in names)


def iter_files(paths: Iterable[Path], exclude: Sequence[str] = ()) -> Iterator[Path]:
    """Expand directories into lintable files, skipping vendored trees."""

    for path in paths:
        if path.is_dir():
            candidates: Iterable[Path] = sorted(
                p
                for p in path.rglob("*")
                if p.is_file()
                and p.suffix.lower() in _LINTED_SUFFIXES
                and not _SKIPPED_DIRS.intersection(p.relative_to(path).parts)
            )
        else:
            candidates = [path]
        for candidate in candidates:
            posix = candidate.as_posix()
            if any(fnmatch.fnmatch(posix, pattern) for pattern in exclude):
                continue
            yield candidate


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ------------------------------------------------------------------- rules


def check_terms(
    paths: Iterable[Path],
    level: str,
    vocabulary: dict[str, Any],
    *,
    exclude: Sequence[str] = (),
) -> list[Finding]:
    """Flag technical terms that are too deep for ``level``.

    One stretch of text gets one finding: when two terms match overlapping
    text (for example "governed repair" and "governed"), the longer one wins.
    """

    rules = term_rules(vocabulary, level)
    findings: list[Finding] = []
    for path in iter_files(paths, exclude):
        text = _read(path)
        if text is None:
            findings.append(
                Finding(str(path), 0, "read", "error", "cannot read this file as UTF-8")
            )
            continue
        lines = text.split("\n")
        allowed, problems = _pragmas(str(path), lines)
        findings.extend(problems)
        skipped = _advanced_lines(lines)
        for fragment in extract_fragments(path, text):
            hits = [
                (match, rule)
                for rule in rules
                for match in rule.pattern.finditer(fragment.text)
            ]
            hits.sort(
                key=lambda hit: (hit[0].start(), -(hit[0].end() - hit[0].start()))
            )
            taken: list[tuple[int, int]] = []
            reported: set[tuple[str, str]] = set()
            for match, rule in hits:
                span = match.span()
                if any(start <= span[0] and span[1] <= end for start, end in taken):
                    continue
                taken.append(span)
                line = _locate(
                    lines,
                    fragment,
                    re.compile(re.escape(match.group(0)), rule.pattern.flags),
                )
                if line in skipped or (rule.term_id, rule.term) in reported:
                    continue
                if _allowed(allowed, line, rule.term_id, rule.term) or _allowed(
                    allowed, fragment.line, rule.term_id, rule.term
                ):
                    continue
                reported.add((rule.term_id, rule.term))
                findings.append(
                    Finding(
                        str(path),
                        line,
                        f"term:{rule.term_id}",
                        rule.severity,
                        f"{match.group(0)!r} is {rule.min_level}-level wording on a {level} surface. "
                        f"Say: {rule.say_instead}",
                    )
                )
    return findings


def check_claims(
    paths: Iterable[Path],
    vocabulary: dict[str, Any],
    *,
    exclude: Sequence[str] = (),
) -> list[Finding]:
    """Flag no-change claims near a halt or stop that no outcome proves."""

    spec = vocabulary["no_change_claims"]
    claims = [re.compile(p, re.IGNORECASE) for p in spec["claim_patterns"]]
    stop = re.compile(spec["stop_context"], re.IGNORECASE)
    proof = re.compile(spec["proof_tokens"])
    window = spec["window_lines"]
    findings: list[Finding] = []
    for path in iter_files(paths, exclude):
        text = _read(path)
        if text is None:
            findings.append(
                Finding(str(path), 0, "read", "error", "cannot read this file as UTF-8")
            )
            continue
        lines = text.split("\n")
        allowed, problems = _pragmas(str(path), lines)
        findings.extend(problems)
        for fragment in extract_fragments(path, text):
            match = next((m for c in claims if (m := c.search(fragment.text))), None)
            if match is None:
                continue
            line = _locate(
                lines, fragment, re.compile(re.escape(match.group(0)), re.IGNORECASE)
            )
            if _allowed(allowed, line, NO_CHANGE_CLAIM_RULE) or _allowed(
                allowed, fragment.line, NO_CHANGE_CLAIM_RULE
            ):
                continue
            nearby = "\n".join(
                lines[max(0, fragment.line - 1 - window) : fragment.last_line + window]
            )
            if stop.search(nearby) and not proof.search(nearby):
                findings.append(
                    Finding(
                        str(path),
                        line,
                        NO_CHANGE_CLAIM_RULE,
                        "error",
                        f"{match.group(0)!r} next to a halt or stop with no outcome that proves no effect. "
                        "Derive the sentence from transaction_outcome or the Execute terminal outcome "
                        "(openadapt_flow.contracts.plain_results), never from HALTED alone.",
                    )
                )
    return findings


def _values_of(annotation: object) -> list[str]:
    """String values a Literal, an Enum, or a union of them can take."""

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return [str(member.value) for member in annotation]
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        values: list[str] = []
        for arg in typing.get_args(annotation):
            values.extend(_values_of(arg) if isinstance(arg, enum.Enum) else [str(arg)])
        return values
    if isinstance(annotation, enum.Enum):
        return [str(annotation.value)]
    if origin is not None:
        values = []
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            values.extend(_values_of(arg))
        return values
    return []


def _import_enum_values(spec: str) -> tuple[str, list[str]]:
    target, _, source = spec.partition("=")
    module_name, _, attribute = target.partition(":")
    if not (module_name and attribute and source):
        raise ValueError(f"--enum takes MODULE:ATTRIBUTE=SOURCE, not {spec!r}")
    obj: object = importlib.import_module(module_name)
    parts = attribute.split(".")
    for index, part in enumerate(parts):
        try:
            obj = getattr(obj, part)
            continue
        except AttributeError:
            fields = getattr(obj, "model_fields", None)
            if index == len(parts) - 1 and isinstance(fields, dict) and part in fields:
                obj = fields[part].annotation
                continue
            raise
    values = _values_of(obj)
    if not values:
        raise ValueError(
            f"{target} is not an Enum, a Literal, or a field that holds them"
        )
    return source, list(dict.fromkeys(values))


def check_coverage(
    table: dict[str, Any],
    *,
    enums: Sequence[str] = (),
    values: dict[str, Sequence[str]] | None = None,
    partial: bool = False,
) -> list[Finding]:
    """Every emitted value must map to a plain result or a progress label.

    Without ``partial``, a value the table maps but the emitter doesn't have
    is a warning, because the row may be stale. Pass ``partial=True`` for an
    emitter that only ever sends some of a source's values.
    """

    findings: list[Finding] = []
    checks: list[tuple[str, str, list[str]]] = []
    for spec in enums:
        source, members = _import_enum_values(spec)
        checks.append((spec, source, members))
    for source, listed in (values or {}).items():
        checks.append((f"--values {source}", source, list(listed)))
    for where, source, members in checks:
        try:
            known = known_values(table, source)
        except KeyError:
            raise ValueError(
                f"{where}: unknown source {source!r}; known sources: {sorted(table['sources'])}"
            ) from None
        for value in members:
            if value not in known:
                findings.append(
                    Finding(
                        where,
                        0,
                        "coverage",
                        "error",
                        f"{source}={value!r} has no plain result. Add a row to outcomes.json before this value ships.",
                    )
                )
        for value in [] if partial else sorted(known - set(members)):
            findings.append(
                Finding(
                    where,
                    0,
                    "coverage",
                    "warn",
                    f"outcomes.json maps {source}={value!r}, which this emitter doesn't have.",
                )
            )
    return findings


def _parse_values(items: Sequence[str]) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for item in items:
        source, _, listed = item.partition("=")
        if not source or not listed:
            raise ValueError(f"--values takes SOURCE=V1,V2, not {item!r}")
        parsed.setdefault(source, []).extend(
            v.strip() for v in listed.split(",") if v.strip()
        )
    return parsed


def _glob(root: Path, patterns: Sequence[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError(f"config pattern {pattern!r} matches nothing under {root}")
        found.extend(matches)
    return found


def check_config(
    config_path: Path, vocabulary: dict[str, Any], table: dict[str, Any]
) -> list[Finding]:
    """Run every rule a ``.vocab.json`` file asks for.

    Shape::

        {"levels": {"business": ["README.md", "site/pages/*.tsx"],
                    "detail": ["docs/guides/**/*.md"]},
         "claims": ["src", "docs"],
         "exclude": ["*/vendor/*"],
         "coverage": {"enums": ["pkg.mod:Outcome=transaction_outcome"],
                      "values": {"agent_status": ["success", "halt"]},
                      "partial": false}}
    """

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config_path} is not JSON: {exc}") from exc
    root = config_path.resolve().parent
    exclude = list(config.get("exclude", []))
    findings: list[Finding] = []
    for level, patterns in config.get("levels", {}).items():
        if level not in LEVELS[:2]:
            raise ValueError(
                f"{config_path}: level {level!r} must be business or detail"
            )
        findings.extend(
            check_terms(_glob(root, patterns), level, vocabulary, exclude=exclude)
        )
    if config.get("claims"):
        findings.extend(
            check_claims(_glob(root, config["claims"]), vocabulary, exclude=exclude)
        )
    coverage = config.get("coverage", {})
    if coverage:
        findings.extend(
            check_coverage(
                table,
                enums=coverage.get("enums", []),
                values=coverage.get("values"),
                partial=bool(coverage.get("partial", False)),
            )
        )
    return findings


# --------------------------------------------------------------------- CLI


def _report(findings: Sequence[Finding], output: str, strict: bool) -> int:
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    if output == "json":
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(f"{errors} error(s), {warnings} warning(s)", file=sys.stderr)
    return 1 if errors or (strict and warnings) else 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--registry", type=Path, help="directory with vocabulary.json and outcomes.json"
    )
    common.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    common.add_argument(
        "--format", choices=("text", "json"), default="text", dest="output"
    )
    common.add_argument(
        "--exclude", action="append", default=[], help="glob of paths to skip"
    )

    parser = argparse.ArgumentParser(
        prog="openadapt-vocab-lint",
        description="Check user-facing text against the OpenAdapt vocabulary registry.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    terms = commands.add_parser(
        "terms", parents=[common], help="technical terms too deep for a surface level"
    )
    terms.add_argument("--level", choices=LEVELS[:2], required=True)
    terms.add_argument("paths", nargs="+", type=Path)
    claims = commands.add_parser(
        "claims", parents=[common], help="no-change claims a halt alone can't support"
    )
    claims.add_argument("paths", nargs="+", type=Path)
    coverage = commands.add_parser(
        "coverage", parents=[common], help="every outcome value maps to a plain result"
    )
    coverage.add_argument(
        "--enum", action="append", default=[], metavar="MODULE:ATTRIBUTE=SOURCE"
    )
    coverage.add_argument(
        "--values", action="append", default=[], metavar="SOURCE=V1,V2"
    )
    coverage.add_argument(
        "--partial",
        action="store_true",
        help="the emitters send only some values, so don't warn about rows they lack",
    )
    check = commands.add_parser(
        "check", parents=[common], help="run what a .vocab.json file asks for"
    )
    check.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "coverage" and not args.enum and not args.values:
        parser.error("coverage needs --enum or --values")
    try:
        vocabulary, table = load_registry(args.registry)
        if args.command == "terms":
            findings = check_terms(
                args.paths, args.level, vocabulary, exclude=args.exclude
            )
        elif args.command == "claims":
            findings = check_claims(args.paths, vocabulary, exclude=args.exclude)
        elif args.command == "coverage":
            findings = check_coverage(
                table,
                enums=args.enum,
                values=_parse_values(args.values),
                partial=args.partial,
            )
        else:
            findings = check_config(args.config, vocabulary, table)
    except (RegistryError, ValueError, ImportError, AttributeError, OSError) as exc:
        print(f"openadapt-vocab-lint: {exc}", file=sys.stderr)
        return 2
    return _report(findings, args.output, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
