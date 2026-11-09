"""Declarations for shadow effect models; expression semantics live in transforms.

These declarations confer no write, read or receipt authority. Observation
bindings come from the reader, not from an agent's account of its actions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.runtime.effects.effect import Verdict

Name = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ObservationBinding(StrictModel):
    """A flat expression name bound to a caller-owned observation path.

    Path segments select record fields only. Collection operations belong to
    the shared language, not to path resolution. No Python attribute access.
    """

    name: Name
    source: Literal["task", "before", "after"]
    path: Annotated[tuple[Identifier, ...], Field(max_length=24)] = ()


class EffectModel(StrictModel):
    """A bounded declaration, validated by the shared language before use.

    Parsing a declaration does not admit the model or establish that a
    language version/profile is supported. The consumer must check both.
    """

    schema_version: Literal["openadapt.effect-model/v1"] = "openadapt.effect-model/v1"
    kind: Name
    language_version: Identifier
    operator_profile: Identifier
    bindings: Annotated[
        tuple[ObservationBinding, ...], Field(min_length=1, max_length=256)
    ]
    obligations: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=2000)], ...],
        Field(min_length=1, max_length=64),
    ]

    @model_validator(mode="after")
    def _unique_bindings(self) -> EffectModel:
        names = [binding.name for binding in self.bindings]
        if len(names) != len(set(names)):
            raise ValueError("duplicate binding name")
        if any(name.startswith("__") for name in names):
            raise ValueError("reserved binding name")
        return self

    def content_sha256(self) -> str:
        """Identity of the declaration, without observed values or credentials."""
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class EvaluationIdentity(StrictModel):
    """Exact replay dependencies supplied by the caller's frozen plan."""

    model_sha256: Digest
    language_sha256: Digest
    language_version: Identifier
    operator_profile: Identifier
    schema_sha256: Digest
    reader_sha256: Digest
    source_sha256: Digest


class BindingUnavailable(ValueError):
    """A required binding cannot be resolved; no observed values are reported."""


def bind_observations(
    model: EffectModel,
    *,
    task: dict[str, object],
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    """Resolve all declared field paths without coercion or expression evaluation.

    The shared language must validate and freeze the returned values before
    evaluating them. This step neither establishes coverage/freshness nor
    substitutes for the language's type checker. Snapshots are plain data;
    custom mapping/attribute access is never invoked.
    """

    sources = {"task": task, "before": before, "after": after}
    if any(type(source) is not dict for source in sources.values()):
        raise BindingUnavailable("snapshots must be plain records")
    bound: dict[str, object] = {}
    for binding in model.bindings:
        value: object = sources[binding.source]
        for segment in binding.path:
            if type(value) is not dict or segment not in value:
                raise BindingUnavailable("required observation path is absent")
            value = value[segment]
        bound[binding.name] = value
    return bound


class CandidateResult(StrictModel):
    """A shadow result, with no canonical episode, ledger or receipt handle.

    Predicate truth is a prediction of the generated declaration. It does not
    establish that the declaration expresses the requested write. In particular,
    syntactic input references cannot establish that semantic relationship.
    A positive prediction therefore remains unqualified and indeterminate.
    A judged contradiction refutes the declared predicates conservatively;
    this result never modifies the canonical reader's verdict or receipts.
    """

    identity: EvaluationIdentity
    verdict: Verdict = Verdict.INDETERMINATE
    judged: bool = False
    prediction_scope: Literal["unqualified_model_prediction"] = (
        "unqualified_model_prediction"
    )
    predicate_outcomes: Annotated[
        tuple[Literal["true", "false", "unknown"], ...], Field(max_length=64)
    ] = ()
    reason: Literal[
        "satisfied",
        "contradiction",
        "evidence_unavailable",
        "language_refused",
        "prior_refutation",
        "model_unqualified",
    ] = "evidence_unavailable"

    @model_validator(mode="after")
    def _consistent_result(self) -> CandidateResult:
        if self.reason == "model_unqualified" and (
            not self.predicate_outcomes
            or any(outcome != "true" for outcome in self.predicate_outcomes)
        ):
            raise ValueError("positive prediction requires actual true outcomes")
        if self.verdict == Verdict.CONFIRMED:
            raise ValueError("model prediction cannot confirm an effect")
        elif self.verdict == Verdict.REFUTED:
            if self.reason == "prior_refutation" and not self.judged:
                return self
            if not self.judged or self.reason != "contradiction":
                raise ValueError("refutation requires a judged contradiction")
        elif self.judged or self.reason not in (
            "evidence_unavailable",
            "language_refused",
            "model_unqualified",
        ):
            raise ValueError("an incomplete candidate cannot claim a judged outcome")
        return self


class ObservationPath(StrictModel):
    source: Literal["task", "before", "after"]
    path: Annotated[tuple[Identifier, ...], Field(max_length=24)] = ()


class ObservationRequirements(StrictModel):
    """Reader scope chosen independently of the generated model."""

    paths: Annotated[tuple[ObservationPath, ...], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def _before_and_after(self) -> ObservationRequirements:
        if not {"before", "after"} <= {path.source for path in self.paths}:
            raise ValueError("reader scope requires before and after observations")
        if len(self.paths) != len(set(self.paths)):
            raise ValueError("duplicate observation requirement")
        return self


class CapturedObservations(StrictModel):
    """Stable snapshots and facts supplied by the trusted independent reader.

    ``fresh`` requires the reader's native version/consistency evidence; elapsed
    time alone is insufficient. These facts are not model outputs or executor
    claims. The caller owns snapshot stability and authentication of the reader.
    """

    task: dict[str, object]
    before: dict[str, object]
    after: dict[str, object]
    source_sha256: Digest
    schema_sha256: Digest
    reader_sha256: Digest
    covered: Annotated[tuple[ObservationPath, ...], Field(max_length=256)] = ()
    complete: bool = False
    fresh: bool = False
    freshness_ref: Identifier | None = None
    prior_refuted: bool = False


def evaluate_effect_model(
    model: EffectModel,
    observations: CapturedObservations,
    requirements: ObservationRequirements,
    identity: EvaluationIdentity,
) -> CandidateResult:
    """Evaluate a shadow candidate using the shared language only.

    Neither model declarations nor expressions can replace reader obligations.
    The result holds no episode, ledger or signed-receipt mutation capability.
    """

    if observations.prior_refuted:
        return CandidateResult(
            identity=identity, verdict=Verdict.REFUTED, reason="prior_refutation"
        )
    unavailable = CandidateResult(identity=identity)
    if (
        not observations.complete
        or not observations.fresh
        or observations.freshness_ref is None
        or observations.source_sha256 != identity.source_sha256
        or observations.schema_sha256 != identity.schema_sha256
        or observations.reader_sha256 != identity.reader_sha256
    ):
        return unavailable
    required = set(requirements.paths)
    declared = {
        ObservationPath(source=binding.source, path=binding.path)
        for binding in model.bindings
    }
    if not required <= set(observations.covered) or not required <= declared:
        return unavailable
    try:
        from openadapt_flow import transforms

        language_digest = hashlib.sha256(
            Path(transforms.__file__).read_bytes()
        ).hexdigest()
        if (
            model.content_sha256() != identity.model_sha256
            or model.language_version != identity.language_version
            or model.language_version != transforms.LANGUAGE_VERSION
            or model.operator_profile != identity.operator_profile
            or model.operator_profile != transforms.EFFECT_PROFILE
            or language_digest != identity.language_sha256
        ):
            return CandidateResult(identity=identity, reason="language_refused")
        bindings = bind_observations(
            model,
            task=observations.task,
            before=observations.before,
            after=observations.after,
        )
        results = transforms.evaluate_effect_predicates(model.obligations, bindings)
        if len(results) != len(model.obligations):
            return CandidateResult(identity=identity, reason="language_refused")
        outcomes: tuple[Literal["true", "false", "unknown"], ...] = tuple(
            result.outcome.value for result in results
        )
        if "unknown" in outcomes:
            return CandidateResult(
                identity=identity,
                reason="language_refused",
                predicate_outcomes=outcomes,
            )
        used = {name for result in results for name in result.inputs}
        used_paths = {
            ObservationPath(source=binding.source, path=binding.path)
            for binding in model.bindings
            if binding.name in used
        }
        # Retain the observation-input coverage guard. Name occurrence is not
        # semantic intent use, and cannot qualify a positive model prediction.
        if not required <= used_paths:
            return CandidateResult(identity=identity, predicate_outcomes=outcomes)
        if any(result.outcome == "false" for result in results):
            return CandidateResult(
                identity=identity,
                verdict=Verdict.REFUTED,
                judged=True,
                reason="contradiction",
                predicate_outcomes=outcomes,
            )
        if not all(result.outcome == "true" for result in results):
            return CandidateResult(identity=identity, reason="language_refused")
        return CandidateResult(
            identity=identity,
            reason="model_unqualified",
            predicate_outcomes=outcomes,
        )
    except Exception:  # noqa: BLE001 - a shadow failure never changes canonical state
        return CandidateResult(identity=identity, reason="language_refused")
