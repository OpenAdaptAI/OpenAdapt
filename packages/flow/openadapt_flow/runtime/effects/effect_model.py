"""Declarations for shadow effect models; expression semantics live in transforms.

These declarations confer no write, read or receipt authority. Observation
bindings come from the reader, not from an agent's account of its actions.
"""

from __future__ import annotations

import hashlib
import json
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

    ``judged`` means complete authoritative evidence was actually evaluated.
    An unavailable read or an evaluator failure is never judged. It is not a
    confidence score and cannot be supplied by a model expression.
    """

    identity: EvaluationIdentity
    verdict: Verdict = Verdict.INDETERMINATE
    judged: bool = False
    reason: Literal[
        "satisfied", "contradiction", "evidence_unavailable", "language_refused"
    ] = "evidence_unavailable"

    @model_validator(mode="after")
    def _consistent_result(self) -> CandidateResult:
        if self.verdict == Verdict.CONFIRMED:
            if not self.judged or self.reason != "satisfied":
                raise ValueError("confirmation requires judged satisfied obligations")
        elif self.verdict == Verdict.REFUTED:
            if not self.judged or self.reason != "contradiction":
                raise ValueError("refutation requires a judged contradiction")
        elif self.judged or self.reason not in (
            "evidence_unavailable",
            "language_refused",
        ):
            raise ValueError("an incomplete candidate cannot claim a judged outcome")
        return self
