"""One fold for every verdict: obligations fixed first, then settled.

Reviews kept finding the same fail-open bugs one pull request at a time. A
check passed on an empty set, on the first N of more than N writes, on a read
that raised, on a result for a different run, or on an outcome code nobody
listed. Each was fixed where it was found. This module fixes the classes once
(``docs/FAIL_CLOSED.md`` names them C1 to C7):

- :class:`Obligations` is the set of things that must be checked. It comes
  from the plan (what the run wrote), never from what a check happened to
  return, and it may be empty.
- :class:`Result` is one check of one :class:`Subject`. ``HELD`` can only be
  made from a complete :class:`Observed` value (:meth:`Result.held`) or from
  an allow-listed upstream code (:meth:`Result.from_code`). A read that raised
  or found nothing gives ``NOT_CHECKED``; it never defaults to a pass.
- :func:`settle` matches results to obligations. An obligation with no result,
  a result nobody asked for, a result bound to a different run, two results
  that disagree, or more obligations than the limit all keep the settlement
  from passing. Nothing is truncated, and an empty set never passes.
- :func:`from_code` maps an outside code to a status by allow-list: a code
  nobody listed is ``NOT_CHECKED``.
- :func:`trust_label` says who signed something only from a verified trust
  source; any other signature is "not verified".

Stdlib only, so it can move to ``openadapt_types`` unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from fractions import Fraction
from typing import Optional, Union

__all__ = [
    "Errored",
    "FailClosed",
    "Missing",
    "Observation",
    "Observed",
    "Obligations",
    "Result",
    "Settlement",
    "Status",
    "Subject",
    "TrustSource",
    "binding_mismatches",
    "from_code",
    "observe",
    "require_bound",
    "settle",
    "trust_label",
]


class Status(str, Enum):
    """The three values a check can have. There is no fourth."""

    HELD = "held"
    REFUTED = "refuted"
    NOT_CHECKED = "not_checked"


# -- observations ----------------------------------------------------------------


@dataclass(frozen=True)
class Observed:
    """A value that was read. ``complete=False`` when the read was partial
    (a short page, a capped query): it can refute, never hold."""

    value: object
    complete: bool = True


@dataclass(frozen=True)
class Missing:
    """The read found nothing to judge (no row, no file, no key)."""

    reason: str


@dataclass(frozen=True)
class Errored:
    """The read raised. Only the exception's type and a short reason are kept."""

    exc_type: str
    reason: str


Observation = Union[Observed, Missing, Errored]


class FailClosed(Exception):
    """Raised by a probe to stop the run. :func:`observe` never swallows it."""


def observe(
    fn: Callable[[], object],
    *,
    benign: Optional[Mapping[type[BaseException], Observation]] = None,
) -> Observation:
    """Run a probe and turn what happened into an :data:`Observation`.

    A returned :data:`Observation` passes through; any other value becomes
    :class:`Observed`. :class:`FailClosed` re-raises. An exception listed in
    ``benign`` maps to its declared observation (a 404 that means "no such
    row" is ``Missing``, never a value). Any other ``Exception`` becomes
    :class:`Errored`. It never returns a default value.
    """

    try:
        value = fn()
    except FailClosed:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes Errored
        for kind, mapped in (benign or {}).items():
            if isinstance(exc, kind):
                return mapped
        return Errored(type(exc).__name__, str(exc)[:200])
    if isinstance(value, (Observed, Missing, Errored)):
        return value
    return Observed(value)


# -- subjects and binding ---------------------------------------------------------

#: The subject fields that bind a result to one run. When an obligation sets
#: one, a result must carry the same value.
BINDING_FIELDS = ("run_id", "nonce", "digest")


@dataclass(frozen=True)
class Subject:
    """What one check is about, and which run it belongs to.

    ``key_tuple`` names the thing (system, surface, table, key, field): the
    table, key and field are bound together, so a decoy table holding the
    same key is a different subject. ``run_id``, ``nonce`` and ``digest`` bind
    a result to the run that produced it (:func:`binding_mismatches`).
    """

    run_id: str = ""
    nonce: str = ""
    system: str = ""
    surface: str = ""
    table: Optional[str] = None
    key: Optional[str] = None
    field: Optional[str] = None
    digest: Optional[str] = None

    def key_tuple(
        self,
    ) -> tuple[str, str, Optional[str], Optional[str], Optional[str]]:
        return (self.system, self.surface, self.table, self.key, self.field)

    def bound(
        self,
        *,
        run_id: Optional[str] = None,
        nonce: Optional[str] = None,
        digest: Optional[str] = None,
    ) -> "Subject":
        """A copy bound to a run, a nonce and a digest (unset ones are kept)."""

        return replace(
            self,
            run_id=self.run_id if run_id is None else run_id,
            nonce=self.nonce if nonce is None else nonce,
            digest=self.digest if digest is None else digest,
        )


def binding_mismatches(expected: Subject, got: Subject) -> tuple[str, ...]:
    """The binding fields ``expected`` sets that ``got`` does not match.

    A field ``expected`` leaves empty binds nothing. A field it sets must be
    equal in ``got``; an empty one in ``got`` is a mismatch, not a wildcard.
    """

    out = []
    for name in BINDING_FIELDS:
        want = getattr(expected, name)
        if want and getattr(got, name) != want:
            out.append(name)
    return tuple(out)


def require_bound(expected: Subject, got: Subject) -> None:
    """Raise :class:`FailClosed` unless ``got`` is bound like ``expected``."""

    wrong = binding_mismatches(expected, got)
    if wrong:
        raise FailClosed(f"bound to a different run: {', '.join(wrong)}")


# -- results ----------------------------------------------------------------------

#: Only this module's constructors hold it, so ``Result(..., Status.HELD, ...)``
#: written anywhere else raises.
_JUDGED = object()


@dataclass(frozen=True)
class Result:
    """One check of one subject. Build it with the class methods."""

    subject: Subject
    status: Status
    reason: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, Status):
            raise TypeError(f"status must be a Status, not {self.status!r}")
        if self.status is Status.HELD and self._token is not _JUDGED:
            raise TypeError(
                "HELD comes only from Result.held or Result.from_code; "
                "see docs/FAIL_CLOSED.md"
            )

    @classmethod
    def held(cls, subject: Subject, obs: Observation, reason: str = "") -> "Result":
        """HELD, from a complete observed value only."""

        if not isinstance(obs, Observed) or obs.complete is not True:
            raise TypeError("HELD needs a complete Observed value")
        return cls(subject, Status.HELD, reason or "held", _JUDGED)

    @classmethod
    def refuted(cls, subject: Subject, obs: Observation, reason: str) -> "Result":
        """REFUTED, from an observed value (a partial read can refute)."""

        if not isinstance(obs, Observed):
            raise TypeError("REFUTED needs an Observed value")
        return cls(subject, Status.REFUTED, reason)

    @classmethod
    def not_checked(cls, subject: Subject, reason: str) -> "Result":
        return cls(subject, Status.NOT_CHECKED, reason)

    @classmethod
    def from_observation(
        cls,
        subject: Subject,
        obs: Observation,
        judge: Callable[[object], tuple[Status, str]],
    ) -> "Result":
        """Judge an observation. ``judge`` sees only a complete value; a
        missing, errored or partial read is ``NOT_CHECKED`` without a call."""

        if isinstance(obs, Missing):
            return cls.not_checked(subject, f"missing: {obs.reason}")
        if isinstance(obs, Errored):
            return cls.not_checked(subject, f"errored: {obs.exc_type}")
        if not obs.complete:
            return cls.not_checked(subject, "partial read")
        status, reason = judge(obs.value)
        if status is Status.HELD:
            return cls.held(subject, obs, reason)
        if status is Status.REFUTED:
            return cls.refuted(subject, obs, reason)
        return cls.not_checked(subject, reason)

    @classmethod
    def from_code(
        cls,
        subject: Subject,
        code: object,
        *,
        held: frozenset[str],
        refuted: frozenset[str],
        reason: str = "",
    ) -> "Result":
        """A result from an upstream judge's code, by allow-list (:func:`from_code`)."""

        status = from_code(code, held=held, refuted=refuted)
        text = reason or f"code {code!r}"
        if status is Status.HELD:
            return cls(subject, status, text, _JUDGED)
        return cls(subject, status, text)


def from_code(code: object, *, held: frozenset[str], refuted: frozenset[str]) -> Status:
    """Map an outside code to a status. Any code not listed is ``NOT_CHECKED``.

    An enum member maps by its value. The two lists may not overlap.
    """

    if held & refuted:
        raise ValueError(f"a code is both held and refuted: {sorted(held & refuted)}")
    if isinstance(code, Enum):
        code = code.value
    if not isinstance(code, str):
        return Status.NOT_CHECKED
    if code in held:
        return Status.HELD
    if code in refuted:
        return Status.REFUTED
    return Status.NOT_CHECKED


# -- obligations and settlement ---------------------------------------------------


@dataclass(frozen=True)
class Obligations:
    """What must be checked, fixed before any check runs."""

    items: tuple[Subject, ...]
    source: str

    @classmethod
    def fix(cls, items: Iterable[Subject], *, source: str) -> "Obligations":
        """Dedupe by :meth:`Subject.key_tuple`, keeping the first. Empty is
        allowed; it never settles as a pass."""

        seen: set[tuple[object, ...]] = set()
        kept = []
        for item in items:
            if item.key_tuple() not in seen:
                seen.add(item.key_tuple())
                kept.append(item)
        return cls(tuple(kept), source)


@dataclass(frozen=True)
class Settlement:
    """Every obligation lands in exactly one of held, refuted, not_checked,
    missing, mismatched or conflicting. ``unexpected`` holds results that
    match no obligation."""

    total: int
    held: tuple[Subject, ...]
    refuted: tuple[Subject, ...]
    not_checked: tuple[Subject, ...]
    missing: tuple[Subject, ...]
    unexpected: tuple[Result, ...]
    mismatched: tuple[Subject, ...]
    conflicting: tuple[Subject, ...]
    over_limit: bool

    @property
    def passes(self) -> bool:
        return (
            self.total > 0
            and len(self.held) == self.total
            and not self.refuted
            and not self.not_checked
            and not self.missing
            and not self.unexpected
            and not self.mismatched
            and not self.conflicting
            and not self.over_limit
        )

    def _unsettled(self) -> bool:
        return bool(
            self.total == 0
            or self.not_checked
            or self.missing
            or self.unexpected
            or self.mismatched
            or self.conflicting
            or self.over_limit
        )

    def label(self, *, precedence: tuple[Status, ...]) -> Status:
        """``HELD`` only when :attr:`passes`. Otherwise the first status in
        ``precedence`` that applies (each site states its own order), else
        ``NOT_CHECKED``."""

        if self.passes:
            return Status.HELD
        for status in precedence:
            if status is Status.REFUTED and self.refuted:
                return Status.REFUTED
            if status is Status.NOT_CHECKED and self._unsettled():
                return Status.NOT_CHECKED
        return Status.NOT_CHECKED

    def rate(self, of: Status) -> Optional[Fraction]:
        """Share of the planned total, never of what happened to be scored.
        ``NOT_CHECKED`` counts missing, mismatched and conflicting too."""

        if self.total == 0:
            return None
        if of is Status.HELD:
            n = len(self.held)
        elif of is Status.REFUTED:
            n = len(self.refuted)
        else:
            n = (
                len(self.not_checked)
                + len(self.missing)
                + len(self.mismatched)
                + len(self.conflicting)
            )
        return Fraction(n, self.total)


def settle(
    obligations: Obligations,
    results: Iterable[Result],
    *,
    limit: Optional[int] = None,
) -> Settlement:
    """Match results to obligations by subject key. See :class:`Settlement`."""

    by_key: dict[tuple[object, ...], list[Result]] = {}
    for result in results:
        by_key.setdefault(result.subject.key_tuple(), []).append(result)
    wanted = {o.key_tuple() for o in obligations.items}
    buckets: dict[str, list[Subject]] = {
        k: [] for k in ("held", "refuted", "not_checked", "missing", "mismatched")
    }
    conflicting: list[Subject] = []
    for item in obligations.items:
        got = by_key.get(item.key_tuple(), [])
        if not got:
            buckets["missing"].append(item)
        elif any(binding_mismatches(item, r.subject) for r in got):
            buckets["mismatched"].append(item)
        elif len({r.status for r in got}) > 1:
            conflicting.append(item)
        else:
            buckets[got[0].status.value].append(item)
    unexpected = tuple(r for key, rs in by_key.items() if key not in wanted for r in rs)
    total = len(obligations.items)
    return Settlement(
        total=total,
        held=tuple(buckets["held"]),
        refuted=tuple(buckets["refuted"]),
        not_checked=tuple(buckets["not_checked"]),
        missing=tuple(buckets["missing"]),
        unexpected=unexpected,
        mismatched=tuple(buckets["mismatched"]),
        conflicting=tuple(conflicting),
        over_limit=limit is not None and total > limit,
    )


# -- trust sources ----------------------------------------------------------------


class TrustSource(str, Enum):
    """Where the key that verified a signature came from."""

    #: This computer's own signing key, read from its key store.
    LOCAL_KEY = "local_key"
    #: A key pinned in a signer registry the verifier trusts.
    REGISTERED = "registered"
    #: Only a key the envelope carries itself: proves integrity, not who.
    EMBEDDED = "embedded"
    #: Not verified, or the source is not known.
    UNVERIFIED = "unverified"


_TRUST_LABELS = {
    TrustSource.LOCAL_KEY: "signed on this computer",
    TrustSource.REGISTERED: "signed by a registered signer",
    TrustSource.EMBEDDED: "signature valid, signer not known",
}


def trust_label(source: object, *, verified: bool) -> str:
    """The label for a signature. Only a verified, known source names a signer;
    anything else (an unknown source, a string, a failed check) is
    "not verified"."""

    if verified is not True or not isinstance(source, TrustSource):
        return "not verified"
    return _TRUST_LABELS.get(source, "not verified")
