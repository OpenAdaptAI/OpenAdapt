"""The fail-open harness: one set of mutations for every verdict site.

A site is a function that turns evidence into a verdict. Its spec says how to
call it, what a pass is, and where in the evidence the per-obligation
results, the plan, the required fields, the observed values, the run binding
and the outcome codes live (``Lens`` hooks). The mutators in
:mod:`tests.failopen.mutators` use only those hooks, so one spec, written once
per site, buys every mutation. :func:`run_site` applies them and checks the
properties:

- P0: each seed's verdict is the one the spec says is correct.
- P1: a mutation never turns a not-pass into a pass.
- P2: on a pass seed, dropping a result (M1), inflating past the limit with
  the last one refuted (M2), emptying (M3), a probe that raises (M4), a
  missing or null field (M5), a conflicting duplicate (M7), a perturbed value
  (M10) and an unknown outcome code (M14) must not pass.
- P3: a result bound to another run (M8) and a hostile corpus entry (M11)
  must not pass.
- P4: reordering the results (M6) changes nothing.
- P5: the default of a knob is the strictest value of its sweep (M9).
- P7: an outcome that may have changed something never maps to one that says
  nothing changed, or to a plain "failed" (M12).

An uncaught exception from the site counts as not-pass: a check that crashes
fails closed. ``docs/FAIL_CLOSED.md`` explains the classes behind each
mutation.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

#: Mutator ids whose mutated evidence must not pass, by property.
P2 = frozenset({"M1", "M2", "M3", "M4", "M5", "M7", "M10", "M14"})
P3 = frozenset({"M8", "M11"})
#: Mutators P1 applies to: each one only removes or corrupts evidence. A
#: value perturbation (M10), a rebuilt set (M2) or a dropped result (M1, which
#: can drop the one bad result) can repair a failing seed, so those are held
#: to P2/P3 only.
P1_IDS = frozenset({"M3", "M4", "M5", "M7", "M8"})


@dataclass(frozen=True)
class Lens:
    """Read and replace one part of the evidence. ``put`` gets a deep copy."""

    get: Callable[[Any], Any]
    put: Callable[[Any, Any], None]

    def set(self, evidence: Any, value: Any) -> Any:
        out = copy.deepcopy(evidence)
        self.put(out, value)
        return out


def key(name: str, *path: Any) -> Lens:
    """A lens on ``evidence[path...][name]``."""

    def parent(ev: Any) -> Any:
        for p in path:
            ev = ev[p]
        return ev

    def put(ev: Any, value: Any) -> None:
        if value is ABSENT:
            parent(ev).pop(name, None)
        else:
            parent(ev)[name] = value

    return Lens(lambda ev: parent(ev).get(name, ABSENT), put)


class _Absent:
    def __repr__(self) -> str:
        return "<absent>"

    def __deepcopy__(self, memo: dict[int, Any]) -> "_Absent":
        return self


ABSENT: Any = _Absent()  # a key lens deletes the key when given this


@dataclass(frozen=True)
class Seed:
    evidence: Any
    correct_pass: bool
    note: str = ""


@dataclass(frozen=True)
class CodeCase:
    """One outcome code for P7/M14: the evidence that carries it, whether the
    code says a write may have landed, and which outputs would claim it did
    not. ``unknown=True`` marks a code no vocabulary lists (M14)."""

    evidence: Any
    may_have_changed: bool
    forbidden: Callable[[Any], bool]
    label: str
    unknown: bool = False


@dataclass(frozen=True)
class Knob:
    """M9: ``call(evidence, value)`` runs the site with the knob at ``value``."""

    name: str
    default: Any
    sweep: Sequence[Any]
    call: Callable[[Any, Any], Any]


@dataclass(frozen=True)
class CorpusCase:
    """M11: ``apply`` puts one corpus entry into the evidence; ``bad`` says
    whether the output accepted it unsafely (the generic not-pass test is
    replaced by this when given)."""

    name: str
    apply: Callable[[Any, Any], Any]
    bad: Optional[Callable[[Any, Any, Any], bool]] = None


@dataclass
class SiteSpec:
    id: str
    classes: str
    #: ``module:qualname`` of the function under test.
    target: str
    run: Callable[[Any], Any]
    is_pass: Callable[[Any], bool]
    seeds: Callable[[], list[Seed]]
    #: The per-obligation results (a list).
    items: Optional[Lens] = None
    #: The plan's obligations, when they are separate from the results: M1
    #: drops a result and keeps the plan.
    plan: Optional[Lens] = None
    #: A refuted (or not-checked) copy of one result item.
    bad_item: Optional[Callable[[Any], Any]] = None
    #: M2: build evidence with ``n`` results, the last one bad.
    inflate: Optional[Callable[[Any, int], Any]] = None
    limit: Optional[int] = None
    #: M5: a dict of fields that must all be present and non-null.
    fields: Optional[Lens] = None
    #: M10: observed values to perturb.
    values: tuple[Lens, ...] = ()
    #: M10 perturbation labels this site may read as the same value (a
    #: version id 2 and "2"); the reason goes in ``notes``.
    equivalent: frozenset[str] = frozenset()
    #: Set (to the reason) when an empty set correctly passes: M3 then
    #: empties the results only, or is skipped when there is no plan.
    vacuous: Optional[str] = None
    #: M4: evidence whose probe raises ``exc``.
    probe_raise: Optional[Callable[[Any, BaseException], Any]] = None
    #: M8: the run binding (run id, nonce or digest) of a result.
    subject: Optional[Lens] = None
    corpus: Optional[CorpusCase] = None
    corpus_entries: Callable[[], list[Any]] = lambda: []
    codes: Optional[Callable[[], list[CodeCase]]] = None
    knob: Optional[Knob] = None
    #: P1 extra: the same evidence with one run relabelled as not scored.
    relabel: Optional[Callable[[Any], Any]] = None
    #: ``site`` (a decorated function on main), ``owned`` (a site on main an
    #: open pull request owns; decorated when the owner adopts) or
    #: ``selftest`` (vendored history).
    kind: str = "site"
    #: For an ``owned`` site: why it is still fail-open, and who owns the fix.
    xfail: Optional[str] = None
    #: For an ``owned`` site: exactly the failures it has today (``Failure.key``).
    #: Any other failure, or one of these going away, fails the test.
    known: frozenset[str] = frozenset()
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure:
    site: str
    mutator: str
    prop: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.mutator} {self.prop}: {self.detail}"

    def __str__(self) -> str:
        return f"{self.site} {self.key}"


REGISTRY: dict[str, SiteSpec] = {}


def register(
    spec: SiteSpec, xfail: Optional[str] = None, known: Sequence[str] = ()
) -> SiteSpec:
    if spec.id in REGISTRY:
        raise ValueError(f"spec {spec.id!r} registered twice")
    if xfail is not None:
        spec.xfail = xfail
        spec.known = frozenset(known)
    REGISTRY[spec.id] = spec
    return spec


def safe_pass(spec: SiteSpec, evidence: Any) -> bool:
    try:
        out = spec.run(evidence)
    except Exception:  # noqa: BLE001 - a crash is a not-pass
        return False
    return bool(spec.is_pass(out))


def run_site(spec: SiteSpec, only: Optional[set[str]] = None) -> list[Failure]:
    """Every property failure for ``spec``; empty means the site holds."""

    from tests.failopen import mutators

    failures: list[Failure] = []
    seeds = spec.seeds()
    if not any(seed.correct_pass for seed in seeds):
        failures.append(Failure(spec.id, "seed", "P0", "no pass seed to mutate"))
    ran = 0
    for seed in seeds:
        got = safe_pass(spec, seed.evidence)
        if got != seed.correct_pass:
            failures.append(
                Failure(spec.id, "seed", "P0", f"{seed.note or 'seed'}: pass={got}")
            )
            continue
        for m in mutators.mutations(spec, seed):
            if only is not None and m.id not in only:
                continue
            ran += 1
            if m.check is not None:
                problem = m.check()
                if problem:
                    failures.append(Failure(spec.id, m.id, m.prop, problem))
                continue
            after = safe_pass(spec, m.evidence)
            if not got and after and m.id in P1_IDS:
                failures.append(
                    Failure(spec.id, m.id, "P1", f"{m.label}: a not-pass became a pass")
                )
            elif got and m.expect == "not_pass" and after:
                prop = "P3" if m.id in P3 else "P2"
                failures.append(
                    Failure(spec.id, m.id, prop, f"{m.label}: still passes")
                )
            elif m.expect == "same" and after != got:
                failures.append(Failure(spec.id, m.id, "P4", f"{m.label}: changed"))
    if not ran and only is None:
        failures.append(Failure(spec.id, "spec", "P0", "no mutation applies"))
    return failures


# -- helpers for specs -------------------------------------------------------------


def browser_origin(base: str, location: str) -> Optional[str]:
    """The origin a browser navigates to for ``Location: location`` on
    ``base``, as the WHATWG URL parser reads it for http(s): tabs and newlines
    removed, a backslash read as a slash, any run of two or more slashes
    starting an authority, userinfo dropped. None when it can't be read."""

    loc = location.strip(" \x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f")
    loc = "".join(ch for ch in loc if ch not in "\t\n\r").replace("\\", "/")
    scheme, _, rest = base.partition("://")
    host = rest.split("/", 1)[0]
    lower = loc.lower()
    for s in ("http:", "https:"):
        if lower.startswith(s):
            body = loc[len(s) :]
            if s[:-1] == scheme and not body.startswith("/"):
                return f"{scheme}://{host}"  # "http:path" is relative to a http base
            scheme, loc = s[:-1], "//" + body.lstrip("/")
            break
    else:
        if ":" in loc.split("/", 1)[0] and not loc.startswith("/"):
            return None  # another scheme: not a same-origin navigation
    if loc.startswith("//"):
        authority = loc.lstrip("/")
        for stop in "/?#":
            authority = authority.split(stop, 1)[0]
        authority = authority.rsplit("@", 1)[-1].lower()
        if not authority:
            return None
        if authority.endswith(":80") and scheme == "http":
            authority = authority[:-3]
        if authority.endswith(":443") and scheme == "https":
            authority = authority[:-4]
        return f"{scheme}://{authority}"
    return f"{scheme}://{host}"


def root_list() -> Lens:
    """A lens on evidence that is itself the list of results."""

    def put(ev: Any, value: Any) -> None:
        ev[:] = value

    return Lens(lambda ev: ev, put)


def index(lens: Lens, i: int) -> Lens:
    """A lens on item ``i`` of the list ``lens`` reads."""

    def put(ev: Any, value: Any) -> None:
        lens.get(ev)[i] = value

    return Lens(lambda ev: lens.get(ev)[i], put)


def load_all() -> dict[str, SiteSpec]:
    """Import every spec module (``sites/*`` and ``selftest/specs``)."""

    import importlib
    import pkgutil

    from tests.failopen import sites

    for info in pkgutil.iter_modules(sites.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"tests.failopen.sites.{info.name}")
    importlib.import_module("tests.failopen.selftest.specs")
    return REGISTRY
