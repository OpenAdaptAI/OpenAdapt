"""Bind reviewed record checks to the step of a compiled program that saves.

A *record check* is a typed effect contract
(:class:`~openadapt_flow.runtime.effects.Effect`) on the step that writes a
record. At run time the deployment's effect verifier reads the system of
record through its own read-only session and the engine judges the contract,
so a saved entry is confirmed by the engine itself rather than by a test
setup after the fact.

The compiler binds the contracts it can derive from a demonstration (mined
effects). A GUI-only demonstration often cannot observe the system of record,
so its save step carries no contract, or a placeholder the gate refuses. This
module is the seam where an operator attaches a reviewed contract afterwards.
It is deliberately narrow:

- **It never edits the source bundle.** It copies it, binds into the copy,
  re-seals the copy's integrity manifest, verifies it, and publishes it at a
  new path that must not exist yet.
- **It only adds contracts.** It never changes a step's action, target,
  postconditions, identity contract, risk label, or review flag. Confirmed
  contracts already on the step are kept; the new ones are added to them.
  Only compiler placeholders (``needs_operator_confirmation``), which no run
  may trust, are replaced.
- **A bound contract must be able to prove this run's write.** Every bound
  step needs a change claim (``record_written`` with ``count_new_only``, or
  ``exact_new_set``), so a record that was already there cannot satisfy it.
  Every ``record_written`` / ``field_equals`` contract must select its subject
  through a run parameter, so another record cannot satisfy it either.
- **Every entered field is accounted for.** Each non-secret parameter the
  program enters must be compared by some bound contract (as a selector, a
  value, an idempotency key, or an ``exact_new_set`` member), or be listed
  under ``not_compared`` with the reason. The binding refuses otherwise.
- **Secrets stay out.** A contract that references a secret parameter is
  refused; a record check never reads a credential back.
- **The changed program needs a fresh decision.** A prior certification or
  qualification decision bound to the old contract is invalidated, exactly as
  at-rest sealing does. Nothing here certifies, qualifies, approves, or
  changes a risk classification.

The binding's provenance (the binding file's digest, the source program's
digest, and the per-field coverage) is written next to the bundle as
``record-check-binding.json``. It is informational. The contracts themselves
are inside ``workflow.json`` and covered by the sealed content digest.

The binding file schema (JSON or YAML)::

    schema: openadapt.record-check-binding/v1
    workflow_name: openimis-claim-intake
    source_content_digest: <sha256 of the compiled program, optional pin>
    steps:
      - step: step_012
        intent: "click '...'"          # optional guard: must equal the step's intent
        effects:
          - kind: record_written
            match: {code: {param: claim_no}}
            expected_count: 1
            count_new_only: true
          - kind: field_equals
            match: {code: {param: claim_no}}
            field: explanation
            value: {param: explanation}
    not_compared:
      some_param: "why this entered value is not read back"

Per-system contracts (which record, which fields) are application knowledge
and belong with the deployment, not in this repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr
from openadapt_flow.traversal import iter_workflow_steps

BINDING_SCHEMA = "openadapt.record-check-binding/v1"
PROVENANCE_SCHEMA = "openadapt.record-check-binding-provenance/v1"
PROVENANCE_FILENAME = "record-check-binding.json"

#: The reserved run-identity parameter an idempotency key may bind to.
_RESERVED_PARAMS = frozenset({"__run_id__"})

_INVALIDATION_REASON = "record-check binding changed the workflow contract"


class RecordCheckBindingError(ValueError):
    """The binding cannot be applied; every reason is listed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "record-check binding refused:\n" + "\n".join(f"  - {p}" for p in problems)
        )


class RecordCheckStep(BaseModel):
    """The contracts to bind to one step."""

    model_config = ConfigDict(extra="forbid")

    #: Exact step id in the compiled program.
    step: str = Field(min_length=1)
    #: Optional guard: when set, it must equal the step's ``intent`` exactly,
    #: so a binding written for one compilation cannot land on a different
    #: step of a recompiled program that happens to reuse the id.
    intent: Optional[str] = None
    effects: list[dict[str, Any]] = Field(min_length=1)


class RecordCheckBinding(BaseModel):
    """A reviewed set of record checks for one compiled program."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["openadapt.record-check-binding/v1"] = Field(alias="schema")
    #: Must equal the program's ``Workflow.name``.
    workflow_name: str = Field(min_length=1)
    #: Optional pin: the sealed content digest of the exact compiled program
    #: this binding was reviewed against.
    source_content_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")
    steps: list[RecordCheckStep] = Field(min_length=1)
    #: Entered parameters deliberately not compared, each with its reason.
    not_compared: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> "RecordCheckBinding":
        """Read a binding file (``.json``, or ``.yaml`` / ``.yml``)."""
        source = Path(path)
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() in (".yaml", ".yml"):
            import yaml

            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
        if not isinstance(raw, dict):
            raise RecordCheckBindingError(["binding file must hold one mapping"])
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise RecordCheckBindingError(
                [
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                ]
            ) from exc

    def sha256(self) -> str:
        """Canonical digest of the reviewed binding content."""
        payload = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class FieldCoverage(BaseModel):
    """How one entered parameter is accounted for."""

    param: str
    status: Literal["compared", "not_compared", "uncovered"]
    #: Where it is compared: ``<step>#<effect index>:<role>``.
    checks: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


class RecordCheckCoverage(BaseModel):
    """Entered-field coverage of a program's record checks."""

    entered: list[str]
    fields: list[FieldCoverage]

    @property
    def complete(self) -> bool:
        return all(item.status != "uncovered" for item in self.fields)

    def render(self) -> str:
        lines = []
        for item in self.fields:
            if item.status == "compared":
                shown = ", ".join(item.checks[:3])
                if len(item.checks) > 3:
                    shown += f" (+{len(item.checks) - 3} more)"
                lines.append(f"  compared      {item.param}: {shown}")
            elif item.status == "not_compared":
                lines.append(f"  not compared  {item.param}: {item.reason}")
            else:
                lines.append(f"  UNCOVERED     {item.param}")
        compared = sum(1 for item in self.fields if item.status == "compared")
        header = (
            f"record checks compare {compared} of {len(self.fields)} entered field(s)"
        )
        return "\n".join([header, *lines])


class BindingResult(BaseModel):
    """What :func:`bind_record_checks` produced."""

    bundle_dir: str
    source_content_digest: str
    content_digest: str
    binding_sha256: str
    bound_effects: dict[str, int]
    replaced_placeholders: dict[str, int]
    certification_invalidated: bool
    coverage: RecordCheckCoverage


# ---------------------------------------------------------------------------
# Parameter references
# ---------------------------------------------------------------------------


def _expr_param(expr: Optional[ValueExpr]) -> Optional[str]:
    return expr.param if expr is not None and expr.param is not None else None


def effect_parameter_roles(effect: Effect) -> list[tuple[str, str]]:
    """Return ``(param, role)`` for every run parameter ``effect`` compares."""
    found: list[tuple[str, str]] = []
    for key, expr in effect.match.items():
        name = _expr_param(expr)
        if name:
            found.append((name, f"match.{key}"))
    name = _expr_param(effect.value)
    if name:
        found.append((name, f"value.{effect.field}"))
    name = _expr_param(effect.idempotency_key)
    if name:
        found.append((name, f"idempotency_key.{effect.key_field}"))
    for position, selector in enumerate(effect.new_records):
        for key, expr in selector.items():
            name = _expr_param(expr)
            if name:
                found.append((name, f"new_records[{position}].{key}"))
    return found


def entered_parameters(workflow: Workflow) -> list[str]:
    """Non-secret parameters the program enters, in step order."""
    secret = set(workflow.secret_params)
    seen: list[str] = []
    for step in iter_workflow_steps(workflow):
        name = step.param
        if name and name not in secret and name not in seen:
            seen.append(name)
    return seen


def record_check_coverage(
    workflow: Workflow, not_compared: Optional[dict[str, str]] = None
) -> RecordCheckCoverage:
    """Report how each entered parameter is compared by the bundle's contracts.

    Only confirmed contracts count; a compiler placeholder compares nothing.
    """
    not_compared = dict(not_compared or {})
    checks: dict[str, list[str]] = {}
    for step in iter_workflow_steps(workflow):
        for index, effect in enumerate(step.effects):
            if effect.needs_operator_confirmation:
                continue
            for name, role in effect_parameter_roles(effect):
                checks.setdefault(name, []).append(f"{step.id}#{index}:{role}")
    entered = entered_parameters(workflow)
    fields = []
    for name in entered:
        if name in checks:
            fields.append(
                FieldCoverage(param=name, status="compared", checks=checks[name])
            )
        elif name in not_compared:
            fields.append(
                FieldCoverage(
                    param=name, status="not_compared", reason=not_compared[name]
                )
            )
        else:
            fields.append(FieldCoverage(param=name, status="uncovered"))
    return RecordCheckCoverage(entered=entered, fields=fields)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _parse_effects(step_binding: RecordCheckStep, problems: list[str]) -> list[Effect]:
    known = set(Effect.model_fields)
    parsed: list[Effect] = []
    for index, raw in enumerate(step_binding.effects):
        where = f"{step_binding.step} effect {index}"
        unknown = sorted(set(raw) - known)
        if unknown:
            problems.append(f"{where}: unknown field(s) {', '.join(unknown)}")
            continue
        try:
            parsed.append(Effect.model_validate(raw))
        except ValidationError as exc:
            problems.append(f"{where}: {exc.errors()[0]['msg']}")
    return parsed


def _check_effect(
    where: str,
    effect: Effect,
    *,
    params: set[str],
    secret: set[str],
    problems: list[str],
) -> None:
    if effect.needs_operator_confirmation:
        problems.append(
            f"{where}: needs_operator_confirmation is set; bind a completed "
            "contract, not a placeholder"
        )
    if effect.readback is not None:
        problems.append(
            f"{where}: an on-screen read-back is not a system-of-record check; "
            "bind it through the compiler instead"
        )
    for name, role in effect_parameter_roles(effect):
        if name in secret:
            problems.append(
                f"{where}: {role} references secret parameter {name!r}; a record "
                "check never reads a credential back"
            )
        elif name not in params and name not in _RESERVED_PARAMS:
            problems.append(
                f"{where}: {role} references {name!r}, which is not a parameter "
                "of this program"
            )
    if effect.kind in (EffectKind.RECORD_WRITTEN, EffectKind.FIELD_EQUALS):
        selector_params = [
            name
            for name in (_expr_param(expr) for expr in effect.match.values())
            if name
        ]
        if not selector_params:
            problems.append(
                f"{where}: match must select the record through a run parameter "
                "(a literal-only selector lets another record satisfy the check)"
            )
    if effect.kind is EffectKind.FIELD_EQUALS and (
        not effect.field or effect.value is None
    ):
        problems.append(f"{where}: field_equals needs field and value")


def _has_change_claim(effects: list[Effect]) -> bool:
    return any(
        (effect.kind is EffectKind.RECORD_WRITTEN and effect.count_new_only)
        or effect.kind is EffectKind.EXACT_NEW_SET
        for effect in effects
    )


def _validate(
    workflow: Workflow, binding: RecordCheckBinding
) -> tuple[dict[str, list[Effect]], list[str]]:
    problems: list[str] = []
    if binding.workflow_name != workflow.name:
        problems.append(
            f"binding is for workflow {binding.workflow_name!r}; this program is "
            f"{workflow.name!r}"
        )
    digest = workflow.manifest.content_digest if workflow.manifest else ""
    if binding.source_content_digest and binding.source_content_digest != digest:
        problems.append(
            "binding is pinned to program digest "
            f"{binding.source_content_digest[:16]}..., this program is "
            f"{digest[:16]}..."
        )
    steps: dict[str, Any] = {}
    duplicate_ids: set[str] = set()
    for step in iter_workflow_steps(workflow):
        if step.id in steps:
            duplicate_ids.add(step.id)
        steps[step.id] = step
    params = set(workflow.params) | set(workflow.param_specs)
    secret = set(workflow.secret_params)
    bound: dict[str, list[Effect]] = {}
    for step_binding in binding.steps:
        sid = step_binding.step
        if sid in bound:
            problems.append(f"step {sid!r} is listed more than once")
            continue
        target = steps.get(sid)
        if target is None:
            problems.append(f"step {sid!r} is not in this program")
            continue
        if sid in duplicate_ids:
            problems.append(f"step id {sid!r} is not unique in this program")
            continue
        if step_binding.intent is not None and step_binding.intent != target.intent:
            problems.append(
                f"step {sid!r} intent is {target.intent!r}, binding expects "
                f"{step_binding.intent!r}"
            )
        if target.api_binding is not None and target.api_binding.kind != "web_form":
            # A web_form API step takes the save step's record check when a
            # person admits it (openadapt-flow api admit), so binding here is
            # the way to give it one.
            problems.append(
                f"step {sid!r} has an API binding; its API-path contracts are "
                "bound with that binding, not here"
            )
        effects = _parse_effects(step_binding, problems)
        for index, effect in enumerate(effects):
            _check_effect(
                f"{sid} effect {index}",
                effect,
                params=params,
                secret=secret,
                problems=problems,
            )
        if effects and not _has_change_claim(effects):
            problems.append(
                f"step {sid!r}: no change claim; add a record_written with "
                "count_new_only: true (or an exact_new_set) so a record that was "
                "already there cannot satisfy the check"
            )
        existing = {
            json.dumps(e.model_dump(mode="json"), sort_keys=True)
            for e in target.effects
            if not e.needs_operator_confirmation
        }
        for index, effect in enumerate(effects):
            if json.dumps(effect.model_dump(mode="json"), sort_keys=True) in existing:
                problems.append(
                    f"{sid} effect {index} duplicates a contract the step already has"
                )
        bound[sid] = effects
    entered = entered_parameters(workflow)
    for name, reason in binding.not_compared.items():
        if name not in entered:
            problems.append(
                f"not_compared lists {name!r}, which this program does not enter"
            )
        if not reason or not reason.strip():
            problems.append(f"not_compared[{name!r}] needs a reason")
    return bound, problems


def _apply(workflow: Workflow, bound: dict[str, list[Effect]]) -> dict[str, int]:
    replaced: dict[str, int] = {}
    for step in iter_workflow_steps(workflow):
        if step.id not in bound:
            continue
        kept = [e for e in step.effects if not e.needs_operator_confirmation]
        dropped = len(step.effects) - len(kept)
        if dropped:
            replaced[step.id] = dropped
        step.effects = kept + [e.model_copy(deep=True) for e in bound[step.id]]
    return replaced


def _bundle_key(bundle: Path) -> tuple[bool, Optional[str]]:
    """Whether ``bundle`` is sealed at rest, and the key to open it."""
    from openadapt_flow import crypto

    if not (bundle / "workflow.json.enc").is_file():
        return False, None
    try:
        return True, crypto.require_key(None)
    except crypto.MissingKeyError as exc:
        raise RecordCheckBindingError(
            [f"the bundle is encrypted and {crypto.ENV_KEY} is not set: {exc}"]
        ) from exc


def check_record_checks(
    bundle_dir: Path | str, binding: RecordCheckBinding
) -> RecordCheckCoverage:
    """Validate ``binding`` against a bundle and report coverage; write nothing.

    Raises:
        RecordCheckBindingError: with every reason the binding would be refused.
    """
    _encrypted, key = _bundle_key(Path(bundle_dir))
    workflow = Workflow.load(bundle_dir, verify_integrity=True, key=key)
    bound, problems = _validate(workflow, binding)
    if problems:
        raise RecordCheckBindingError(problems)
    _apply(workflow, bound)
    coverage = record_check_coverage(workflow, binding.not_compared)
    _check_coverage(coverage, binding, problems)
    if problems:
        raise RecordCheckBindingError(problems)
    return coverage


def _check_coverage(
    coverage: RecordCheckCoverage, binding: RecordCheckBinding, problems: list[str]
) -> None:
    for item in coverage.fields:
        if item.status == "uncovered":
            problems.append(
                f"entered field {item.param!r} is not compared by any record check; "
                "add a contract for it or list it under not_compared with the reason"
            )
    compared = {item.param for item in coverage.fields if item.status == "compared"}
    for name in binding.not_compared:
        if name in compared:
            problems.append(
                f"not_compared lists {name!r}, but a record check compares it"
            )


def bind_record_checks(
    bundle_dir: Path | str,
    binding: RecordCheckBinding,
    out_dir: Path | str,
) -> BindingResult:
    """Bind ``binding`` into a copy of ``bundle_dir`` published at ``out_dir``.

    The source bundle is never modified. ``out_dir`` must not exist. An
    encrypted source requires ``OPENADAPT_BUNDLE_KEY`` and is re-sealed
    encrypted.

    Raises:
        RecordCheckBindingError: when the binding is refused (nothing written).
        FileExistsError: when ``out_dir`` already exists.
    """
    from openadapt_flow.bundle_sealing import (
        BundleSealingError,
        _invalidate_prior_certification,
        _publish_no_replace,
        _reject_symlinks,
    )

    source = Path(bundle_dir).resolve()
    destination = Path(out_dir).resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"output bundle path exists: {destination}")
    if destination == source or source in destination.parents:
        raise RecordCheckBindingError(["output must be outside the source bundle"])
    try:
        _reject_symlinks(source)
    except BundleSealingError as exc:
        raise RecordCheckBindingError([str(exc)]) from exc
    encrypted, key = _bundle_key(source)
    workflow = Workflow.load(source, verify_integrity=True, key=key)
    if workflow.manifest is None or not workflow.manifest.content_digest:
        raise RecordCheckBindingError(["source bundle carries no sealed manifest"])
    source_digest = workflow.manifest.content_digest
    bound, problems = _validate(workflow, binding)
    if problems:
        raise RecordCheckBindingError(problems)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.binding-", dir=destination.parent)
    )
    try:
        os.chmod(staging, 0o700)
        bundle = staging / "bundle"
        shutil.copytree(source, bundle, symlinks=False)
        (bundle / PROVENANCE_FILENAME).unlink(missing_ok=True)
        working = Workflow.load(bundle, verify_integrity=True, key=key)
        replaced = _apply(working, bound)
        coverage = record_check_coverage(working, binding.not_compared)
        _check_coverage(coverage, binding, problems)
        if problems:
            raise RecordCheckBindingError(problems)
        invalidated = (
            _invalidate_prior_certification(working, reason=_INVALIDATION_REASON)
            if working.manifest is not None
            else False
        )
        working.save(bundle, encrypt=encrypted, key=key)
        reloaded = Workflow.load(bundle, verify_integrity=True, key=key)
        assert reloaded.manifest is not None
        result = BindingResult(
            bundle_dir=str(destination),
            source_content_digest=source_digest,
            content_digest=reloaded.manifest.content_digest,
            binding_sha256=binding.sha256(),
            bound_effects={sid: len(effects) for sid, effects in bound.items()},
            replaced_placeholders=replaced,
            certification_invalidated=invalidated,
            coverage=coverage,
        )
        provenance = {
            "schema": PROVENANCE_SCHEMA,
            "binding_sha256": result.binding_sha256,
            "source_content_digest": source_digest,
            "content_digest": result.content_digest,
            "workflow_name": working.name,
            "bound_effects": result.bound_effects,
            "replaced_placeholders": replaced,
            "certification_invalidated": invalidated,
            "coverage": coverage.model_dump(mode="json"),
        }
        (bundle / PROVENANCE_FILENAME).write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            _publish_no_replace(bundle, destination)
        except BundleSealingError as exc:
            raise FileExistsError(str(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result


__all__ = [
    "BINDING_SCHEMA",
    "PROVENANCE_FILENAME",
    "BindingResult",
    "FieldCoverage",
    "RecordCheckBinding",
    "RecordCheckBindingError",
    "RecordCheckCoverage",
    "RecordCheckStep",
    "bind_record_checks",
    "check_record_checks",
    "effect_parameter_roles",
    "entered_parameters",
    "record_check_coverage",
]
