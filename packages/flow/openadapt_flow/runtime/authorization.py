"""Run-bound authorization for governed deployment execution.

The permissive ``replay`` path does not use this object.  ``run`` creates one
only after every admission gate passes, then hands it to the shared replayer.
Binding the decision to the sealed bundle and exact effect contracts prevents
an approval intended for one workflow from becoming a reusable bypass.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import Step, Workflow
from openadapt_flow.traversal import iter_workflow_steps

_CONSUMED_IDS: set[str] = set()
_CONSUMED_LOCK = threading.Lock()


def effective_runtime_params(
    workflow: Workflow, supplied: dict[str, str] | None
) -> dict[str, str]:
    """Resolve defaults exactly as :meth:`Replayer.run` does."""
    merged = dict(workflow.params)
    for name, spec in workflow.param_specs.items():
        if spec.example is not None:
            merged.setdefault(name, spec.example)
    merged.update(supplied or {})
    return merged


def runtime_inputs_digest(
    workflow: Workflow,
    params: dict[str, str] | None,
    worklists: dict[str, list[dict[str, str]]] | None,
) -> str:
    """Hash the exact effective runtime inputs without persisting their values."""
    payload = {
        "params": effective_runtime_params(workflow, params),
        "worklists": worklists or {},
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UnverifiedWriteApproval(BaseModel):
    """Approval for one GUI step whose effects lack an independent verifier."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    effect_contract_hashes: tuple[str, ...] = Field(min_length=1)


class GovernedRunAuthorization(BaseModel):
    """Ephemeral capability carrying admission decisions into replay.

    ``approval_source`` is deliberately descriptive, not an authentication
    claim.  The local CLI can prove that its explicit flag was supplied, but it
    cannot identify a human.  Hosted callers can replace the source with their
    authenticated approval reference when they construct the capability.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    bundle_content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    runtime_inputs_digest: str = Field(pattern="^[a-f0-9]{64}$")
    admitted_policy_name: str
    required_identity_step_ids: tuple[str, ...] = Field(default_factory=tuple)
    unverified_write_approvals: tuple[UnverifiedWriteApproval, ...] = Field(
        default_factory=tuple
    )
    approval_source: str = "local-cli-explicit-flag"

    def validate_workflow(self, workflow: Workflow) -> str | None:
        """Return a refusal reason when this capability does not fit ``workflow``."""
        actual_digest = (
            workflow.manifest.content_digest if workflow.manifest is not None else None
        )
        if actual_digest != self.bundle_content_digest:
            return (
                "governed run authorization is bound to bundle digest "
                f"{self.bundle_content_digest[:16]}..., but the loaded bundle is "
                f"{(actual_digest or 'unsealed')[:16]}..."
            )
        if workflow.manifest is None:
            return "governed run authorization requires a sealed manifest"

        from openadapt_flow.bundle_validation import compute_content_digest

        recomputed = compute_content_digest(workflow, workflow.manifest.file_hashes)
        if recomputed != self.bundle_content_digest:
            return (
                "governed run authorization no longer matches the current "
                "in-memory workflow semantics"
            )

        steps = {step.id: step for step in iter_workflow_steps(workflow)}
        missing_identity = sorted(
            set(self.required_identity_step_ids).difference(steps)
        )
        if missing_identity:
            return (
                "governed run authorization requires unknown identity step(s): "
                + ", ".join(missing_identity)
            )

        seen: set[str] = set()
        for approval in self.unverified_write_approvals:
            if approval.step_id in seen:
                return (
                    "governed run authorization contains duplicate write approval "
                    f"for step {approval.step_id!r}"
                )
            seen.add(approval.step_id)
            step = steps.get(approval.step_id)
            if step is None:
                return (
                    "governed run authorization approves unknown write step "
                    f"{approval.step_id!r}"
                )
            expected = sorted(effect.contract_hash() for effect in step.effects)
            if sorted(approval.effect_contract_hashes) != expected:
                return (
                    "governed run authorization effect contract mismatch for step "
                    f"{approval.step_id!r}"
                )
        return None

    def validate_execution(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path | str,
        params: dict[str, str] | None,
        worklists: dict[str, list[dict[str, str]]] | None,
        continuation: bool = False,
    ) -> str | None:
        """Validate semantics, sealed assets, inputs, and single-use status."""
        refusal, _assets = self.validate_execution_snapshot(
            workflow,
            bundle_dir=bundle_dir,
            params=params,
            worklists=worklists,
            continuation=continuation,
        )
        return refusal

    def validate_execution_snapshot(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path | str,
        params: dict[str, str] | None,
        worklists: dict[str, list[dict[str, str]]] | None,
        continuation: bool = False,
    ) -> tuple[str | None, dict[str, bytes]]:
        """Validate once and return the exact sealed bytes execution may use."""
        refusal = self.validate_workflow(workflow)
        if refusal is not None:
            return refusal, {}
        assert workflow.manifest is not None

        from openadapt_flow.bundle_validation import (
            BundleIntegrityError,
            verify_integrity,
        )

        try:
            verify_integrity(
                workflow,
                bundle_dir,
                workflow.manifest,
                decrypted_assets=(
                    workflow.decrypted_templates() if workflow.encrypted else None
                ),
            )
        except BundleIntegrityError as exc:
            return f"governed run authorization bundle integrity failed: {exc}", {}

        assets: dict[str, bytes] = {}
        decrypted = workflow.decrypted_templates() if workflow.encrypted else None
        try:
            for rel, expected in workflow.manifest.file_hashes.items():
                data = (
                    decrypted.get(rel)
                    if decrypted is not None
                    else (Path(bundle_dir) / rel).read_bytes()
                )
                if data is None or hashlib.sha256(data).hexdigest() != expected:
                    return (
                        f"governed run authorization asset {rel!r} changed "
                        "while its verified snapshot was created",
                        {},
                    )
                assets[rel] = data
        except OSError as exc:
            return f"governed run authorization could not snapshot assets: {exc}", {}

        actual_inputs = runtime_inputs_digest(workflow, params, worklists)
        if actual_inputs != self.runtime_inputs_digest:
            return (
                "governed run authorization is bound to different runtime "
                "parameters or worklists",
                {},
            )

        if not continuation:
            with _CONSUMED_LOCK:
                if self.authorization_id in _CONSUMED_IDS:
                    return (
                        "governed run authorization was already consumed by a "
                        "different execution",
                        {},
                    )
                _CONSUMED_IDS.add(self.authorization_id)
        return None, assets

    def requires_verified_identity(self, step_id: str) -> bool:
        return step_id in self.required_identity_step_ids

    def approves_unverified_write(self, step: Step) -> bool:
        """Whether this capability exactly approves ``step``'s current effects."""
        expected = sorted(effect.contract_hash() for effect in step.effects)
        return any(
            approval.step_id == step.id
            and sorted(approval.effect_contract_hashes) == expected
            for approval in self.unverified_write_approvals
        )
