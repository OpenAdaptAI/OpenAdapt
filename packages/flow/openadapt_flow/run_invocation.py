"""Fresh local runtime attribution; this evidence grants no execution authority.

The producer derives the descriptor from parsed deployment sections and the
resolved backend it actually uses. A public challenge is not a signature, a
qualification, or protection from a malicious process with the same OS identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport


class InvocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["openadapt.local-invocation/v1"] = "openadapt.local-invocation/v1"
    challenge: StrictStr = Field(
        pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64
    )
    descriptor_sha256: StrictStr = Field(
        pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64
    )
    profile: Literal["standard"] = "standard"


def challenge_value(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invocation challenge must be exactly 64 lowercase hex digits")
    return value


def producer_supported(args: Any, cfg: Any, workflow: Any = None) -> bool:
    """One serial, ordinary local run; refuse unsupported producers before input."""
    if getattr(args, "profile", None) != "standard":
        return False
    if not getattr(args, "config_sha256", None) or not getattr(args, "config", None):
        return False
    if any(
        getattr(args, name, None)
        for name in (
            "self_repair",
            "repair_agent",
            "dry_run",
            "explain",
            "resume",
            "resume_from",
            "resume_program",
            "idempotency_key",
            "managed_dispatch_file",
            "_qualification_case_execution",
        )
    ):
        return False
    if getattr(args, "workers", None) is not None:
        return False
    if cfg.worklist.accounts or cfg.worklist.readback_window or cfg.surfaces:
        return False
    if (
        cfg.repair.agent
        and cfg.repair.auto
        and not getattr(args, "no_auto_repair", False)
    ):
        return False
    if workflow is not None and getattr(workflow, "surfaces", None):
        return False
    return True


def derive_context(
    challenge: str,
    *,
    cfg: Any,
    backend: Any,
    effects: Any,
    actuation: Any,
    profile: str,
) -> InvocationContext:
    """Hash actual configuration, never resolved credentials or an expected hash.

    Model data contains credential references, not resolved credential values.
    No credential resolver, verifier/provider object, environment or run parameter
    value is inspected here. Runtime inputs keep their existing separate digest.
    """
    if profile != "standard":
        raise ValueError("this local invocation producer supports Standard only")
    value = {
        "version": "openadapt.effective-invocation-descriptor/v1",
        "profile": profile,
        "deployment": cfg.model_dump(mode="json"),
        "resolved_backend": backend.model_dump(mode="json"),
        "effective_effects": effects.model_dump(mode="json"),
        "effective_actuation": actuation.model_dump(mode="json"),
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("invocation descriptor exceeds its bound")
    return InvocationContext(
        challenge=challenge_value(challenge),
        descriptor_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def prepare_producer_context(
    args: Any, cfg: Any, backend: Any, workflow: Any, effects: Any, actuation: Any
) -> None:
    challenge = getattr(args, "invocation_challenge", None)
    if challenge is None:
        return
    if not producer_supported(args, cfg, workflow):
        raise ValueError("local invocation producer does not support this run")
    profile = getattr(args, "_execution_profile", None)
    if not isinstance(profile, str):
        raise ValueError("actual runtime profile is unavailable")
    args._run_invocation_context = derive_context(
        challenge,
        cfg=cfg,
        backend=backend,
        effects=effects,
        actuation=actuation,
        profile=profile,
    )


def serialize_report_compatible(report: Any, handler: Any) -> dict[str, Any]:
    data = handler(report)
    if not report.observed_surface_environments:
        data.pop("observed_surface_environments", None)
    if report.invocation_context is None:
        data.pop("invocation_context", None)
    return data


def validate_report_profile(report: Any) -> None:
    if (
        report.invocation_context is not None
        and report.invocation_context.profile != report.execution_profile
    ):
        raise ValueError("invocation profile does not match the actual runtime report")


def initial_run_report(
    replayer: Any,
    workflow: Any,
    *,
    durable_run: Any = None,
    invocation_context: InvocationContext | None = None,
    **fields: Any,
) -> RunReport:
    """Create the report inside the real producer, before any persistence.

    Existing runtime identities and profile are observed, never replaced by the
    caller's challenge. Authentication and qualification are separate contracts.
    """
    from datetime import datetime, timezone

    from openadapt_flow.bundle_validation import compute_parameter_schema_digest
    from openadapt_flow.ir import RunReport
    from openadapt_flow.qualification import workflow_contract_sha256

    return RunReport(
        invocation_context=invocation_context,
        workflow_name=workflow.name,
        started_at=durable_run.started_at
        if durable_run is not None
        else datetime.now(timezone.utc).isoformat(),
        execution_profile=(
            replayer.governed_authorization.execution_profile
            if replayer.governed_authorization is not None
            else "demo"
        ),
        recorded_surface=workflow.surface,
        bundle_content_digest=workflow.manifest.content_digest
        if workflow.manifest
        else None,
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        source_recording_sha256=workflow.manifest.provenance.source_recording_sha256
        if workflow.manifest
        else None,
        parameter_schema_sha256=compute_parameter_schema_digest(workflow),
        run_id_sha256=hashlib.sha256(replayer._run_id.encode("utf-8")).hexdigest(),
        **fields,
    )


def invocation_kwargs(
    context: InvocationContext | None,
) -> dict[str, InvocationContext]:
    """Keep legacy call signatures untouched when no correlation was requested."""
    return {} if context is None else {"invocation_context": context}
