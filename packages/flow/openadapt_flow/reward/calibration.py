"""Exact false-accept bounds for a synthetic-scope reward certificate.

A certificate's ``epsilon`` is a bound on the probability that the checker
accepts an episode it should have refused. The seed does not invent that
number. It runs the checker over ``n`` synthetic trials, one fault per trial
chosen by a seeded generator, counts the false accepts, and reports the exact
one-sided Clopper-Pearson upper bound at the stated confidence.

The bound is exact: it uses the binomial tail directly, not a normal
approximation. For zero failures it reduces to ``1 - alpha ** (1 / n)``.

**The corpus comes from the contract under calibration.** A fixed corpus
measures nothing. Feed a store of triage rows to a contract about radiology
rows and every trial refutes for a reason that has nothing to do with the
fault, the false-accept count is zero because the checker rejects everything,
and the bound is the best number the method can produce while bounding no
real behaviour. So :func:`corpus_from_effects` reads the contract's own
required and forbidden effects and builds the records they describe, and
:func:`extradup_trials` refuses to report a bound unless a clean store built
that way actually earns ``VERIFIED``.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from openadapt_types.process_capability import _digest_payload
from openadapt_types.reward import RewardOutcomeV1

from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr


class CalibrationRefused(ValueError):
    """The corpus cannot exercise the contract, so no bound may be issued."""


def binomial_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), computed with exact coefficients."""

    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    total = 0.0
    for i in range(k + 1):
        total += math.comb(n, i) * (p**i) * ((1.0 - p) ** (n - i))
    return min(1.0, total)


def clopper_pearson_upper(
    failures: int, trials: int, *, confidence: float = 0.95
) -> float:
    """One-sided exact upper bound on a binomial proportion.

    The smallest ``p`` with ``P(X <= failures | trials, p) <= 1 - confidence``.
    """

    if trials <= 0:
        raise ValueError("trials must be positive")
    if not 0 <= failures <= trials:
        raise ValueError("failures must lie in [0, trials]")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must lie in (0, 1)")
    alpha = 1.0 - confidence
    if failures == trials:
        return 1.0
    if failures == 0:
        return 1.0 - alpha ** (1.0 / trials)
    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if binomial_cdf(failures, trials, mid) > alpha:
            low = mid
        else:
            high = mid
    return high


#: Every fault a trial may plant. ``wrong_subject`` is the write that landed
#: on somebody else: the record the contract requires exists, correct in every
#: field, under another subject's identity. ``RewardOutcomeV1.WRONG_EFFECT``
#: names that mode ("a terminal effect that differs from the required one"),
#: so a bound that never planted it did not bound what the receipt claims.
FAULT_CLASSES: tuple[str, ...] = (
    "extra_record",
    "duplicate_record",
    "missing_record",
    "wrong_field",
    "wrong_subject",
    "forbidden_present",
)

#: The suffix a ``wrong_field`` trial appends to one declared literal.
_WRONG_FIELD_SUFFIX = "__calibration_wrong"


@dataclass(frozen=True)
class CorpusRecipe:
    """The records a calibration corpus plants, read off one contract.

    ``intended`` are the records a clean episode leaves behind: one merged
    template per record the required effects describe, repeated as many times
    as the contract requires it. ``forbidden`` are the records the contract
    forbids. Both are templates: a field value is either ``{"literal": ...}``
    or ``{"param": ...}``, and a ``param`` resolves against the trial's
    subject identity, exactly as the judge resolves it.

    ``identity_fields`` are the row-id fields an ``exact_new_set`` effect
    reads. Every planted record carries them, and ``id``, so newness can be
    enumerated.
    """

    identity_keys: tuple[str, ...]
    intended: tuple[dict[str, dict[str, str]], ...]
    forbidden: tuple[dict[str, dict[str, str]], ...]
    identity_fields: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        """The canonical form the corpus digest is taken over."""

        return {
            "corpus": "openadapt.reward-derived-corpus/v1",
            "identity_keys": list(self.identity_keys),
            "intended": [dict(record) for record in self.intended],
            "forbidden": [dict(record) for record in self.forbidden],
            "identity_fields": list(self.identity_fields),
        }

    @property
    def applicable_faults(self) -> tuple[str, ...]:
        """The fault classes this contract can actually be perturbed by.

        A contract that forbids nothing has no ``forbidden_present`` mode to
        false-accept, and a contract whose required records carry no literal
        field beyond the subject has no ``wrong_field`` mode. Those classes
        are vacuous here rather than untested, and the result names the set
        that was sampled so a reader is not left to assume.
        """

        applicable = ["missing_record", "wrong_subject"]
        if self.intended:
            applicable[:0] = ["extra_record", "duplicate_record"]
            if self._mutable_field() is not None:
                applicable.append("wrong_field")
        if self.forbidden:
            applicable.append("forbidden_present")
        return tuple(sorted(set(applicable)))

    def _mutable_field(self) -> Optional[tuple[int, str]]:
        """The first declared literal a ``wrong_field`` trial may spoil."""

        for position, record in enumerate(self.intended):
            for field in sorted(record):
                if field in self.identity_keys or field in self.identity_fields:
                    continue
                if field == "id":
                    continue
                if "literal" in record[field]:
                    return position, field
        return None

    def records(
        self, identity: Mapping[str, str], *, first_id: int
    ) -> list[dict[str, Any]]:
        """Resolve ``intended`` against one subject, with distinct row ids."""

        return _resolve_all(self.intended, identity, self.identity_fields, first_id)

    def forbidden_records(
        self, identity: Mapping[str, str], *, first_id: int
    ) -> list[dict[str, Any]]:
        return _resolve_all(self.forbidden, identity, self.identity_fields, first_id)


def corpus_from_effects(
    required: Sequence[Effect],
    forbidden: Sequence[Effect],
    identity_keys: Sequence[str],
) -> CorpusRecipe:
    """Read the records a contract describes off its own effects."""

    identity_fields = tuple(
        sorted(
            {
                effect.identity_field
                for effect in (*required, *forbidden)
                if effect.kind is EffectKind.EXACT_NEW_SET
            }
        )
    )
    return CorpusRecipe(
        identity_keys=tuple(sorted(identity_keys)),
        intended=tuple(_templates(required)),
        forbidden=tuple(_templates(forbidden)),
        identity_fields=identity_fields,
    )


def corpus_digest(corpus: CorpusRecipe) -> str:
    """The digest a certificate must name for this contract's corpus.

    Derived, so a certificate cannot name a corpus it was not calibrated on
    and a bundle cannot keep a stale digest after its effects change.
    """

    return _digest_payload(corpus.as_payload())


def corpus_digest_for(
    required: Sequence[Effect],
    forbidden: Sequence[Effect],
    identity_keys: Sequence[str],
) -> str:
    return corpus_digest(corpus_from_effects(required, forbidden, identity_keys))


def faulted_store(
    fault: str,
    identity: Mapping[str, str],
    corpus: CorpusRecipe,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One trial's pre-state and post-state, carrying exactly one fault.

    The pre-state holds rows belonging to other subjects and nothing of this
    subject's, so a ``count_new_only`` effect can attribute what it finds. The
    post-state adds the corpus records, spoiled by ``fault``.
    """

    other = _other_subject(identity, rng)
    before = _resolve_all(
        corpus.intended, other, corpus.identity_fields, 100 + rng.randrange(0, 400)
    )
    clean = corpus.records(identity, first_id=1)

    if fault == "missing_record":
        return before, list(before)
    if fault == "wrong_subject":
        # The record landed, correct in every field, on somebody else.
        return before, [*before, *corpus.records(other, first_id=901)]
    if fault == "extra_record":
        extra = corpus.records(identity, first_id=501)[:1]
        return before, [*before, *clean, *extra]
    if fault == "duplicate_record":
        # The same row twice, id and all: a re-delivered write, not a
        # second distinct record.
        return before, [*before, *clean, *clean[:1]]
    if fault == "wrong_field":
        spoiled = corpus._mutable_field()
        if spoiled is None:
            raise CalibrationRefused(
                "this contract declares no literal field a wrong_field trial "
                "could spoil"
            )
        position, field = spoiled
        current = [dict(record) for record in clean]
        current[position][field] = f"{current[position][field]}{_WRONG_FIELD_SUFFIX}"
        return before, [*before, *current]
    if fault == "forbidden_present":
        if not corpus.forbidden:
            raise CalibrationRefused("this contract forbids nothing")
        return (
            before,
            [*before, *clean, *corpus.forbidden_records(identity, first_id=701)],
        )
    raise ValueError(f"unknown fault class {fault!r}")


@dataclass(frozen=True)
class CalibrationResult:
    """What the seed ran and what it found."""

    trials: int
    false_accepts: int
    confidence: float
    epsilon: float
    generator_seed: int
    corpus_digest: str
    fault_classes: tuple[str, ...]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "calibration_trials": self.trials,
            "calibration_false_accepts": self.false_accepts,
            "calibration_confidence": self.confidence,
            "calibration_generator_seed": self.generator_seed,
            "calibration_method": "clopper_pearson_one_sided_exact",
            "calibration_corpus_digest": self.corpus_digest,
            "calibration_fault_classes": list(self.fault_classes),
        }


#: ``checker(before, current, identity)`` -> the outcome the worker assigns
#: when the store held ``before`` at the start of the episode and holds
#: ``current`` at the end.
Checker = Callable[
    [Sequence[dict[str, Any]], Sequence[dict[str, Any]], dict[str, str]],
    RewardOutcomeV1,
]


def extradup_trials(
    checker: Checker,
    corpus: CorpusRecipe,
    *,
    trials: int,
    generator_seed: int,
    confidence: float = 0.95,
    corpus_digest: str,
) -> CalibrationResult:
    """Run the checker over faulted stores and bound its false-accept rate.

    A false accept is ``VERIFIED`` on a faulted store.

    Refuses before it counts anything when the corpus does not exercise the
    contract: a clean store built from the contract's own effects must earn
    ``VERIFIED``, and at least one fault class must be applicable. Without
    the first, every trial refutes for a reason the fault did not cause and
    zero false accepts means only that the checker rejects everything.
    """

    applicable = corpus.applicable_faults
    if not applicable:
        raise CalibrationRefused(
            "no fault class applies to this contract, so its false-accept "
            "rate cannot be sampled and no certificate may be issued"
        )
    control_identity = _trial_identity(corpus.identity_keys, "control")
    control_before, control_current = _clean_store(corpus, control_identity)
    control = checker(control_before, control_current, control_identity)
    if control is not RewardOutcomeV1.VERIFIED:
        raise CalibrationRefused(
            "a clean store built from this contract's own required effects "
            f"judges {control.value}, not verified, so the corpus does not "
            "exercise the contract and a zero false-accept count would bound "
            "nothing. Check that the required effects describe records the "
            "same read can hold."
        )

    rng = random.Random(generator_seed)
    false_accepts = 0
    for index in range(trials):
        fault = applicable[rng.randrange(len(applicable))]
        identity = _trial_identity(corpus.identity_keys, f"{index:04d}")
        before, current = faulted_store(fault, identity, corpus, rng)
        if checker(before, current, identity) is RewardOutcomeV1.VERIFIED:
            false_accepts += 1
    return CalibrationResult(
        trials=trials,
        false_accepts=false_accepts,
        confidence=confidence,
        epsilon=clopper_pearson_upper(false_accepts, trials, confidence=confidence),
        generator_seed=generator_seed,
        corpus_digest=corpus_digest,
        fault_classes=applicable,
    )


# -- deriving the records a contract describes -------------------------------


def _expr_template(expr: ValueExpr) -> dict[str, str]:
    if expr.param is not None:
        return {"param": expr.param}
    return {"literal": str(expr.literal)}


def _selector_template(
    selector: Mapping[str, ValueExpr],
) -> dict[str, dict[str, str]]:
    return {field: _expr_template(expr) for field, expr in selector.items()}


def _key_of(template: Mapping[str, dict[str, str]]) -> str:
    return json.dumps(template, sort_keys=True)


def _templates(effects: Iterable[Effect]) -> list[dict[str, dict[str, str]]]:
    """One merged record template per record the effects describe.

    Effects that select the same record contribute to one template: a
    ``field_equals`` read-back of a row a ``record_written`` effect also
    requires describes ONE row, not two, and planting two would make the
    clean store refute on cardinality.
    """

    merged: dict[str, dict[str, dict[str, str]]] = {}
    counts: dict[str, int] = {}
    order: list[str] = []

    def add(template: dict[str, dict[str, str]], key_template: Any, count: int) -> None:
        key = _key_of(key_template)
        if key not in merged:
            merged[key] = {}
            counts[key] = 0
            order.append(key)
        merged[key].update(template)
        counts[key] = max(counts[key], count)

    for effect in effects:
        match = _selector_template(effect.match)
        if effect.kind is EffectKind.EXACT_NEW_SET:
            for selector in effect.new_records:
                member = {**match, **_selector_template(selector)}
                add(member, member, 1)
            continue
        if effect.expected_count <= 0:
            # An absence claim plants nothing; a clean store satisfies it.
            continue
        template = dict(match)
        if effect.idempotency_key is not None:
            template[effect.key_field] = _expr_template(effect.idempotency_key)
        if effect.kind is EffectKind.FIELD_EQUALS:
            if effect.field and effect.value is not None:
                template[effect.field] = _expr_template(effect.value)
            add(template, match, 1)
            continue
        add(template, match, effect.expected_count)

    records: list[dict[str, dict[str, str]]] = []
    for key in order:
        records.extend(dict(merged[key]) for _ in range(counts[key]))
    return records


def _resolve_all(
    templates: Sequence[Mapping[str, dict[str, str]]],
    identity: Mapping[str, str],
    identity_fields: Sequence[str],
    first_id: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for offset, template in enumerate(templates):
        row_id = first_id + offset
        record: dict[str, Any] = {"id": row_id}
        for field in identity_fields:
            record[field] = row_id
        for field, expr in template.items():
            if "param" in expr:
                record[field] = identity.get(expr["param"], "")
            else:
                record[field] = expr["literal"]
        records.append(record)
    return records


def _clean_store(
    corpus: CorpusRecipe, identity: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The control trial: nothing wrong, so the checker must say verified."""

    return [], corpus.records(identity, first_id=1)


def _trial_identity(identity_keys: Sequence[str], tag: str) -> dict[str, str]:
    return {key: f"{key}-cal-{tag}" for key in identity_keys}


def _other_subject(identity: Mapping[str, str], rng: random.Random) -> dict[str, str]:
    suffix = rng.randrange(10_000)
    return {key: f"{key}-other-{suffix:04d}" for key in identity}
