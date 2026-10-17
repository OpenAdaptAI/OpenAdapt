"""Mine qualification pins from a compiled bundle and its recording.

The compiler already records the demonstration's surface, origin, parameters,
identity evidence, and observed system-of-record deltas. This module turns
those retained facts into pin *proposals*. It does not confirm a pin, invent a
system-of-record binding, or write a qualification project.

A missing required pin is returned as ``status="missing"``. Callers HALT.
They must not guess.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.effects.effect import Effect
from openadapt_flow.traversal import iter_workflow_steps

PinSource = Literal["recording", "bundle", "workflow"]
PinStatus = Literal["proposed", "missing"]

_WRITE_ACTIONS = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.KEY,
        ActionKind.HOTKEY,
        ActionKind.TYPE,
        ActionKind.SELECT_OPTION,
    }
)
_IDENTITY_PARAM_HINTS = frozenset(
    {
        "record_id",
        "secondary_identifier",
        "account_id",
        "order_id",
        "item_id",
        "document_id",
        "case_id",
    }
)


class MinedPin(BaseModel):
    """One pin the operator must confirm, or a missing pin that HALTs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["application", "environment", "identity", "effect"]
    status: PinStatus
    source: PinSource
    summary: str = Field(min_length=1, max_length=512)
    halt_reason: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class MinedFailureCase(BaseModel):
    """One starter fault the operator can run after the pins land."""

    model_config = ConfigDict(extra="forbid")

    id: str
    case_class: Literal["break_it", "extra_field", "identity_swap"]
    kind: Literal[
        "ambiguity",
        "wrong_identity",
        "stale_identity",
        "weak_effect",
        "missing_effect",
    ]
    expected_outcome: Literal["halted"] = "halted"
    description: str = Field(min_length=1, max_length=512)
    step_id: Optional[str] = None


class MinedQualificationPins(BaseModel):
    """Compiler-side proposal inputs. Not a qualification project."""

    model_config = ConfigDict(extra="forbid")

    pins: list[MinedPin]
    failure_cases: list[MinedFailureCase]
    recording_present: bool
    has_parameters: bool


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def origin_from_app_url(app_url: Optional[str]) -> Optional[str]:
    """Return a bounded HTTP(S) origin, or None when the URL is not usable."""

    if not app_url or not isinstance(app_url, str):
        return None
    try:
        parts = urlsplit(app_url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    rendered_host = (
        f"[{hostname.lower().rstrip('.')}]"
        if ":" in hostname
        else hostname.lower().rstrip(".")
    )
    origin = f"{scheme}://{rendered_host}"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        origin += f":{port}"
    return origin if len(origin) <= 320 else None


def load_recording_meta(recording_dir: Path | str | None) -> Optional[dict[str, Any]]:
    """Load ``meta.json`` when a recording directory is supplied."""

    if recording_dir is None:
        return None
    path = Path(recording_dir) / "meta.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_steps(workflow: Workflow) -> list[Step]:
    steps: list[Step] = []
    for step in iter_workflow_steps(workflow):
        effects = list(step.effects)
        if step.api_binding is not None:
            effects.extend(step.api_binding.effects)
        if step.risk == "irreversible" or effects or step.action in _WRITE_ACTIONS:
            steps.append(step)
    return steps


def _real_effects(step: Step) -> list[tuple[str, int, Effect]]:
    found: list[tuple[str, int, Effect]] = []
    for index, effect in enumerate(step.effects):
        if not effect.needs_operator_confirmation:
            found.append(("gui", index, effect))
    if step.api_binding is not None:
        for index, effect in enumerate(step.api_binding.effects):
            if not effect.needs_operator_confirmation:
                found.append(("api", index, effect))
    return found


def _placeholder_or_missing_write(step: Step) -> bool:
    effects = list(step.effects)
    if step.api_binding is not None:
        effects.extend(step.api_binding.effects)
    if step.risk != "irreversible" and not effects:
        return False
    if not effects:
        return True
    return any(effect.needs_operator_confirmation for effect in effects)


def _identity_param_names(workflow: Workflow, step: Step) -> list[str]:
    names: set[str] = set()
    for name in workflow.params:
        if name in _IDENTITY_PARAM_HINTS:
            names.add(name)
    template = None
    if step.anchor is not None:
        template = step.anchor.identity_template
    if template is not None:
        names.update(template.structured_params)
        names.update(template.context_params)
    return sorted(name for name in names if name in workflow.params)


def mine_application_pin(
    workflow: Workflow, meta: Optional[dict[str, Any]]
) -> MinedPin:
    """Propose application identity from the recording origin or bundle surface."""

    app_url = None if meta is None else meta.get("app_url")
    origin = origin_from_app_url(app_url if isinstance(app_url, str) else None)
    surface = workflow.surface or (
        str(meta.get("surface")) if meta and meta.get("surface") else None
    )
    version = None
    if meta is not None and isinstance(meta.get("application_version"), str):
        version = meta["application_version"].strip() or None
    application = None
    if meta is not None and isinstance(meta.get("application"), str):
        application = meta["application"].strip() or None
    if application is None and origin is not None:
        host = urlsplit(origin).hostname or "recorded-application"
        application = host
    if application is None and surface:
        application = f"{surface}-application"
    target_kind = (
        surface
        if surface
        in {
            "web",
            "windows",
            "macos",
            "linux",
            "rdp",
            "citrix",
        }
        else None
    )
    if target_kind is None and origin is not None:
        target_kind = "web"
    if application is None or target_kind is None:
        return MinedPin(
            kind="application",
            status="missing",
            source="recording" if meta is not None else "bundle",
            summary="application identity is not in the recording or bundle",
            halt_reason=(
                "application pin is missing: the recording has no app URL "
                "or surface, and the bundle has no surface binding"
            ),
        )
    identity = origin
    if identity is None and target_kind != "web":
        identity = application
    if identity is None:
        return MinedPin(
            kind="application",
            status="missing",
            source="recording" if meta is not None else "bundle",
            summary="application identity is not a bounded origin or native id",
            halt_reason=(
                "application pin is missing: web recordings need an HTTP(S) "
                "origin; native recordings need an application id"
            ),
        )
    return MinedPin(
        kind="application",
        status="proposed",
        source="recording" if meta is not None else "bundle",
        summary=f"{application} on {target_kind}",
        payload={
            "target_kind": target_kind,
            "application": application[:256],
            "application_identity": identity[:320],
            "application_version": (version or "recorded")[:128],
        },
    )


def mine_environment_pin(
    workflow: Workflow,
    meta: Optional[dict[str, Any]],
    *,
    runtime_version: str,
    application: Optional[MinedPin],
) -> MinedPin:
    """Fingerprint the recorded environment. Do not invent a live observer."""

    if application is None or application.status == "missing":
        return MinedPin(
            kind="environment",
            status="missing",
            source="recording" if meta is not None else "bundle",
            summary="environment fingerprint needs an application pin first",
            halt_reason="environment pin is missing because application is missing",
        )
    viewport = None
    if meta is not None and isinstance(meta.get("viewport"), list):
        viewport = meta["viewport"]
    elif workflow.viewport is not None:
        viewport = list(workflow.viewport)
    payload = {
        "target_kind": application.payload["target_kind"],
        "application_identity": application.payload["application_identity"],
        "application_version": application.payload["application_version"],
        "runtime_version": runtime_version,
        "viewport": viewport,
        "surface": workflow.surface,
        "execution_mode": workflow.execution_mode,
    }
    digest = _sha256_json(
        {
            "schema": "openadapt.qualification-environment-fingerprint/v1",
            **payload,
        }
    )
    capabilities = ["pixel_observation"]
    if any(_real_effects(step) for step in iter_workflow_steps(workflow)):
        capabilities.append("effect_verification")
    return MinedPin(
        kind="environment",
        status="proposed",
        source="recording" if meta is not None else "workflow",
        summary=f"environment digest {digest[:12]}",
        payload={
            "environment_digest": digest,
            "runtime_version": runtime_version[:64],
            "required_capabilities": capabilities,
            "fingerprint": payload,
        },
    )


def mine_identity_pin(workflow: Workflow) -> MinedPin:
    """Propose identity-gate fields from retained evidence. Do not guess."""

    writes = [
        step
        for step in _write_steps(workflow)
        if step.risk == "irreversible"
        or _real_effects(step)
        or _placeholder_or_missing_write(step)
    ]
    if not writes:
        return MinedPin(
            kind="identity",
            status="proposed",
            source="workflow",
            summary="no write step needs an identity gate",
            payload={"policies": []},
        )
    policies: list[dict[str, Any]] = []
    missing: list[str] = []
    for step in writes:
        if step.identity_armed:
            policies.append(
                {
                    "step_id": step.id,
                    "enforcement": "canonical_ladder",
                }
            )
            continue
        identity_params = _identity_param_names(workflow, step)
        structured = (
            step.anchor.structured_identity if step.anchor is not None else None
        )
        if identity_params and structured:
            param = identity_params[0]
            policies.append(
                {
                    "step_id": step.id,
                    "enforcement": "signal_quorum",
                    "quorum": 1,
                    "signals": [
                        {
                            "key": "record_id",
                            "source": "structured",
                            "extract_pattern": r"^(?P<value>.+)$",
                            "params": [param],
                        }
                    ],
                }
            )
            continue
        missing.append(step.id)
    if missing:
        return MinedPin(
            kind="identity",
            status="missing",
            source="workflow",
            summary="identity gate fields are not retained on a write step",
            halt_reason=(
                "identity pin is missing: write step(s) "
                + ", ".join(missing)
                + " have no armed identity ladder and no structured "
                "identity bound to a workflow parameter"
            ),
            payload={"missing_step_ids": missing},
        )
    return MinedPin(
        kind="identity",
        status="proposed",
        source="workflow",
        summary=f"{len(policies)} identity gate(s) from retained evidence",
        payload={"policies": policies},
    )


def mine_effect_pin(workflow: Workflow) -> MinedPin:
    """Propose effect oracles from declared or observed writes.

    A consequential write with no system-of-record effect, or only a
    placeholder that still needs an operator binding, HALTs.
    """

    writes = [
        step
        for step in iter_workflow_steps(workflow)
        if step.risk == "irreversible" or _placeholder_or_missing_write(step)
    ]
    if not writes:
        # No irreversible write: still harvest any declared real effects.
        bindings: list[dict[str, Any]] = []
        for step in iter_workflow_steps(workflow):
            for path, index, effect in _real_effects(step):
                bindings.append(
                    {
                        "step_id": step.id,
                        "effect_index": index,
                        "actuation_path": path,
                        "tier": 1,
                        "kind": effect.kind.value,
                    }
                )
        return MinedPin(
            kind="effect",
            status="proposed",
            source="workflow",
            summary=(
                f"{len(bindings)} declared effect(s)"
                if bindings
                else "no irreversible write in this bundle"
            ),
            payload={"effects": bindings},
        )
    missing: list[str] = []
    bindings = []
    for step in writes:
        real = _real_effects(step)
        if not real:
            missing.append(step.id)
            continue
        for path, index, effect in real:
            bindings.append(
                {
                    "step_id": step.id,
                    "effect_index": index,
                    "actuation_path": path,
                    "tier": 1,
                    "kind": effect.kind.value,
                }
            )
    if missing:
        return MinedPin(
            kind="effect",
            status="missing",
            source="workflow",
            summary="system-of-record oracle is missing on a write step",
            halt_reason=(
                "effect pin is missing: write step(s) "
                + ", ".join(missing)
                + " have no observed system-of-record delta and no declared "
                "effect; refusing to invent a binding"
            ),
            payload={"missing_step_ids": missing},
        )
    return MinedPin(
        kind="effect",
        status="proposed",
        source="workflow",
        summary=f"{len(bindings)} system-of-record effect(s)",
        payload={"effects": bindings},
    )


def mine_failure_cases(workflow: Workflow) -> list[MinedFailureCase]:
    """Fill the starter campaign table from the demonstration.

    Always includes the ``--break-it`` class (optimistic write: screen claims
    success, system of record does not). Adds identity-swap or extra-field
    when the recording/bundle has parameters.
    """

    write_step = next(
        (
            step
            for step in iter_workflow_steps(workflow)
            if step.risk == "irreversible" or _real_effects(step)
        ),
        None,
    )
    step_id = write_step.id if write_step is not None else None
    cases = [
        MinedFailureCase(
            id="fault-break-it",
            case_class="break_it",
            kind="missing_effect",
            description=(
                "Same certified bundle against a backend that paints success "
                "after rejecting the write. Independent SoR read must HALT."
            ),
            step_id=step_id,
        )
    ]
    params = dict(workflow.params)
    identity_params: list[str] = []
    if write_step is not None:
        identity_params = _identity_param_names(workflow, write_step)
    if identity_params:
        cases.append(
            MinedFailureCase(
                id="fault-identity-swap",
                case_class="identity_swap",
                kind="wrong_identity",
                description=(
                    "Replay with a swapped identity parameter so the gate "
                    "refuses the wrong record instead of writing it."
                ),
                step_id=step_id,
            )
        )
    elif params:
        cases.append(
            MinedFailureCase(
                id="fault-extra-field",
                case_class="extra_field",
                kind="ambiguity",
                description=(
                    "Replay with an extra field the demonstration did not "
                    "type. The program must HALT rather than guess."
                ),
                step_id=step_id,
            )
        )
    return cases


def mine_qualification_pins(
    workflow: Workflow,
    *,
    recording_dir: Path | str | None = None,
    runtime_version: str,
) -> MinedQualificationPins:
    """Return compiler-mined pins and the starter failure matrix."""

    meta = load_recording_meta(recording_dir)
    application = mine_application_pin(workflow, meta)
    environment = mine_environment_pin(
        workflow,
        meta,
        runtime_version=runtime_version,
        application=application,
    )
    identity = mine_identity_pin(workflow)
    effect = mine_effect_pin(workflow)
    return MinedQualificationPins(
        pins=[application, environment, identity, effect],
        failure_cases=mine_failure_cases(workflow),
        recording_present=meta is not None,
        has_parameters=bool(workflow.params),
    )
