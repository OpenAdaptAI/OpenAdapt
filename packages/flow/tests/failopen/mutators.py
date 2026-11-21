"""The generic mutations. Each uses only a spec's lenses and hooks.

M1 drop, M2 inflate past the limit, M3 empty, M4 probe raises, M5 missing or
null field, M6 reorder, M7 duplicate, M8 subject swap, M9 knob sweep, M10 value
perturb, M11 corpus, M12 outcome after delivery, M14 unknown code. M13 (a
trust-source swap) waits for the signer-registry vectors (FAIL_CLOSED.md,
follow-ups).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tests.failopen.harness import ABSENT, Seed, SiteSpec, safe_pass

CORPUS = Path(__file__).with_name("corpus")


class HTTP503(Exception):
    """A server that answered 503: the read did not happen."""

    status_code = 503


PROBE_ERRORS: tuple[BaseException, ...] = (
    Exception("probe failed"),
    TimeoutError("probe timed out"),
    HTTP503("503 Service Unavailable"),
)


def load_corpus(name: str) -> Any:
    return json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Mutation:
    id: str
    label: str
    evidence: Any = None
    #: ``not_pass`` (P2/P3), ``same`` (P4) or ``no_better`` (P1 only).
    expect: str = "no_better"
    #: A property that is not about one mutated evidence (P5, P7): returns a
    #: problem description, or "" when it holds.
    check: Optional[Callable[[], str]] = None
    prop: str = ""


def perturbations(value: Any) -> list[tuple[str, Any]]:
    """M10: values that look close to ``value`` but are not it."""

    out: list[tuple[str, Any]] = []
    if isinstance(value, str):
        if len(value) > 1:
            out.append(("prefix", value[: max(1, len(value) // 2)]))
        out.append(("superstring", value + " 0"))
        if value != "":
            out.append(("empty", ""))
        if value.isdigit():
            out.append(("number for string", int(value)))
    elif isinstance(value, bool):
        out.append(("int for bool", int(value)))
    elif isinstance(value, int):
        out.append(("string for number", str(value)))
        out.append(("bool for number", bool(value)))
        out.append(("negative", -1 - value))
    if value is not None:
        out.append(("null", None))
    else:
        out.append(("empty for null", ""))
    if value is not ABSENT:
        out.append(("absent", ABSENT))
    return out


def mutations(spec: SiteSpec, seed: Seed) -> Iterator[Mutation]:
    ev = seed.evidence
    items = spec.items.get(ev) if spec.items else None

    if spec.plan is not None and items:
        yield Mutation(
            "M1",
            "drop the last result, keep the plan",
            spec.items.set(ev, items[:-1]),
            "not_pass",
        )  # type: ignore[union-attr]
    if spec.inflate is not None:
        n = (spec.limit or 256) + 1
        yield Mutation(
            "M2", f"{n} results, the last one bad", spec.inflate(ev, n), "not_pass"
        )
    if spec.items is not None and not (spec.vacuous and spec.plan is None):
        empty = spec.items.set(ev, [])
        if spec.plan is not None and spec.vacuous is None:
            empty = spec.plan.set(empty, [])
        yield Mutation("M3", "no results and no plan", empty, "not_pass")
    if spec.probe_raise is not None:
        for exc in PROBE_ERRORS:
            yield Mutation(
                "M4",
                f"probe raises {type(exc).__name__}",
                spec.probe_raise(ev, exc),
                "not_pass",
            )
    if spec.fields is not None:
        fields = spec.fields.get(ev)
        for name in list(fields):
            gone = dict(fields)
            del gone[name]
            yield Mutation(
                "M5", f"field {name!r} missing", spec.fields.set(ev, gone), "not_pass"
            )
            if fields[name] is not None:
                yield Mutation(
                    "M5",
                    f"field {name!r} null",
                    spec.fields.set(ev, {**fields, name: None}),
                    "not_pass",
                )
    if items and len(items) > 1:
        rev = spec.items.set(ev, list(reversed(items)))  # type: ignore[union-attr]
        if spec.plan is not None:
            rev = spec.plan.set(rev, list(reversed(spec.plan.get(ev))))
        yield Mutation("M6", "results reordered", rev, "same")
    if items:
        yield Mutation(
            "M7", "a result repeated", spec.items.set(ev, items + [items[0]])
        )  # type: ignore[union-attr]
        if spec.bad_item is not None:
            yield Mutation(
                "M7",
                "a result repeated with a bad copy",
                spec.items.set(ev, items + [spec.bad_item(items[0])]),
                "not_pass",
            )  # type: ignore[union-attr]
    if spec.subject is not None:
        yield Mutation(
            "M8",
            "a result from another run",
            spec.subject.set(ev, "other-run"),
            "not_pass",
        )
    for lens in spec.values:
        try:
            current = lens.get(ev)
        except (KeyError, IndexError, TypeError):
            continue  # this seed has no such value
        for label, value in perturbations(current):
            if label in spec.equivalent:
                continue
            yield Mutation("M10", f"value {label}", lens.set(ev, value), "not_pass")
    if spec.corpus is not None:
        for entry in spec.corpus_entries():
            mutated = spec.corpus.apply(ev, entry)
            if spec.corpus.bad is None:
                yield Mutation(
                    "M11", f"{spec.corpus.name} {entry!r}", mutated, "not_pass"
                )
            else:
                yield Mutation(
                    "M11",
                    f"{spec.corpus.name} {entry!r}",
                    check=_corpus_check(spec, mutated, entry),
                    prop="P3",
                )
    if spec.knob is not None and seed.correct_pass:
        yield Mutation(
            "M9", f"knob {spec.knob.name}", check=_knob_check(spec, ev), prop="P5"
        )
    if spec.relabel is not None and seed.correct_pass:
        yield Mutation(
            "M9", "a scored run relabelled as not scored", spec.relabel(ev), "not_pass"
        )
    if spec.codes is not None and seed.correct_pass:
        for case in spec.codes():
            yield Mutation(
                "M14" if case.unknown else "M12",
                case.label,
                check=_code_check(spec, case),
                prop="P2" if case.unknown else "P7",
            )


def _corpus_check(spec: SiteSpec, evidence: Any, entry: Any) -> Callable[[], str]:
    def check() -> str:
        try:
            out = spec.run(evidence)
        except Exception:  # noqa: BLE001 - a refusal by raising is safe
            return ""
        assert spec.corpus is not None and spec.corpus.bad is not None
        return (
            f"accepted {entry!r} as {out!r}"
            if spec.corpus.bad(evidence, entry, out)
            else ""
        )

    return check


def _knob_check(spec: SiteSpec, evidence: Any) -> Callable[[], str]:
    def check() -> str:
        knob = spec.knob
        assert knob is not None

        def passes(value: Any) -> bool:
            try:
                return bool(spec.is_pass(knob.call(evidence, value)))
            except Exception:  # noqa: BLE001
                return False

        if not passes(knob.default):
            return ""
        stricter = [v for v in knob.sweep if not passes(v)]
        return (
            f"default {knob.name}={knob.default!r} passes where {stricter!r} does not"
            if stricter
            else ""
        )

    return check


def _code_check(spec: SiteSpec, case: Any) -> Callable[[], str]:
    def check() -> str:
        try:
            out = spec.run(case.evidence)
        except Exception:  # noqa: BLE001 - a crash fails closed
            return ""
        if case.unknown:
            return (
                f"unknown code passed as {out!r}"
                if safe_pass(spec, case.evidence)
                else ""
            )
        if case.may_have_changed and case.forbidden(out):
            return f"may have changed, reported {out!r}"
        return ""

    return check
