"""The run authority of an admitted agent-compiled program on a managed runner.

A governed authorization template summarises a passing qualification
campaign (fault cases, identity policies, trusted-runner case results), and
``Workflow.load`` refuses any template it cannot rebuild from that sealed
certification. A program compiled from an agent run (``openadapt wrap``,
``jit``) has no campaign. Its authority is its signed admission
(``admission.json``): a named person, a registered admitter key or an agent
under a signed policy, an attended canary on the target, and an independent
read-back of every write (:mod:`openadapt_flow.jit.lifecycle`). Every local
run of such a program already rests on that admission
(``lifecycle.ensure_runnable``).

This module carries the same admission to a managed Standard run without
inventing a second envelope:

* :func:`run_shape` is computed once at publish from Flow's own run gate. It
  holds the value-free fields a hosted :class:`GovernedRunAuthorization`
  needs (policy, profile, effect tier and requirements, identity steps) and
  the admission's SHA-256. It grants nothing: the runner re-derives every
  field locally and refuses a dispatch that disagrees.
* :func:`managed_admission_refusal` is what ``openadapt-flow run
  --managed-dispatch-file`` checks instead of a qualification admission when
  the dispatch carries none: Standard only, an admission that verifies
  against the trusted admission key directory (the runner stages the key the
  organization registered), and an ``approval_source`` that names this exact
  admission.
* :func:`readback_effects` is the read-back verifier the run uses: the
  ``verify.yaml`` bundled at publish, which the cloud also bound.

Regulated runs are refused: they need the qualification campaign.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "APPROVAL_PREFIX",
    "READBACK_PATH",
    "RUN_SHAPE_SCHEMA",
    "HostedAuthorityError",
    "admission_sha256",
    "approval_source",
    "is_admitted_program",
    "managed_admission_refusal",
    "readback_effects",
    "run_shape",
    "shape_matches_authorization",
]

RUN_SHAPE_SCHEMA = "openadapt.admitted-program-run-shape/v1"
#: ``approval_source`` of a hosted run whose authority is a program admission.
APPROVAL_PREFIX = "hosted:program-admission:"
#: Where the publish step puts the confirmed read-back check in the bundle.
READBACK_PATH = "readback/verify.yaml"
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class HostedAuthorityError(ValueError):
    """The admitted program cannot run under a hosted admission authority."""


def _admission_file(bundle_dir: Path | str) -> Path:
    from openadapt_flow.jit.lifecycle import ADMISSION_FILE

    return Path(bundle_dir) / ADMISSION_FILE


def is_admitted_program(bundle_dir: Path | str) -> bool:
    """True when the bundle carries an admission manifest and a read-back file.

    Only a shape check: :func:`managed_admission_refusal` verifies them.
    """

    bundle = Path(bundle_dir)
    admission = _admission_file(bundle)
    readback = bundle / READBACK_PATH
    return (
        admission.is_file()
        and not admission.is_symlink()
        and readback.is_file()
        and not readback.is_symlink()
    )


def admission_sha256(bundle_dir: Path | str) -> str:
    """SHA-256 of the exact ``admission.json`` bytes in the bundle."""

    path = _admission_file(bundle_dir)
    if path.is_symlink() or not path.is_file():
        raise HostedAuthorityError("the bundle carries no admission manifest")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_source(admission_digest: str) -> str:
    """The hosted ``approval_source`` that names one exact admission."""

    if not _SHA256.fullmatch(admission_digest):
        raise HostedAuthorityError("the admission digest is invalid")
    return APPROVAL_PREFIX + admission_digest


def readback_effects(bundle_dir: Path | str) -> Any:
    """The bundled ``verify.yaml``'s verifier, as a deployment effects section."""

    from openadapt_flow.verify_run import load_verify_config

    path = Path(bundle_dir) / READBACK_PATH
    if path.is_symlink() or not path.is_file():
        raise HostedAuthorityError("the bundle carries no read-back check")
    try:
        return load_verify_config(path).verifier
    except Exception as exc:  # noqa: BLE001 - any loader refusal is a refusal
        raise HostedAuthorityError(
            "the bundled read-back check is not a valid verify-run configuration"
        ) from exc


def run_shape(
    bundle_dir: Path | str,
    workflow: Any,
    *,
    policy_source: str,
) -> dict[str, Any]:
    """The value-free Standard run shape of an admitted program.

    Computed with the run gate the managed runner evaluates for the same
    bundle and policy, so the fields agree with the authorization the runner
    builds locally. Values, identities and credentials are never in it.
    """

    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.run_gate import evaluate_run_gate

    bundle = Path(bundle_dir)
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not digest:
        raise HostedAuthorityError("the bundle carries no content digest")
    profile = execution_profile_contract(ExecutionProfile.STANDARD)
    report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=DeploymentConfig(policy=PolicySection(policy=policy_source)),
        effect_verifier=None,
        policy_source=policy_source,
        require_encryption=False,
        profile_contract=profile,
        effective_durable=profile.require_durable,
        effective_require_settled=profile.require_settled,
    )
    if (
        report.execution_profile != ExecutionProfile.STANDARD.value
        or report.policy_contract_sha256 is None
        or report.minimum_effect_tier is None
        or report.bundle_content_digest != digest
    ):
        raise HostedAuthorityError(
            "the run gate did not produce a Standard run shape for this bundle"
        )
    return {
        "schema_version": RUN_SHAPE_SCHEMA,
        "bundle_content_digest": digest,
        "admission_sha256": admission_sha256(bundle),
        "admitted_policy_name": report.policy_name,
        "admitted_policy_contract_sha256": report.policy_contract_sha256,
        "execution_profile": report.execution_profile,
        "minimum_effect_tier": report.minimum_effect_tier,
        "qualified_effect_requirements": [
            item.model_dump(mode="json")
            for item in report.qualified_effect_requirements
        ],
        # Run-gate order, which the runner's local authorization keeps.
        "required_identity_step_ids": list(report.required_identity_step_ids),
    }


def managed_admission_refusal(
    bundle_dir: Path | str,
    workflow: Any,
    authorization: Any,
    *,
    profile: Optional[str],
    key_dir: Optional[Path] = None,
) -> Optional[str]:
    """Why a managed dispatch cannot rest on the program's admission, or None.

    ``key_dir`` is the trusted admission key directory (default
    :func:`openadapt_flow.jit.lifecycle.default_key_dir`); on a managed runner
    it holds only the public key the organization registered.
    """

    from openadapt_flow.jit.lifecycle import (
        AdmissionError,
        is_jit_bundle,
        verify_admission,
    )

    bundle = Path(bundle_dir)
    if profile != "standard":
        return "an admitted program runs hosted only under the Standard profile"
    if authorization is None:
        return "the managed dispatch carries no run authorization"
    if getattr(authorization, "qualification_admission", None) is not None:
        return "the dispatch carries a qualification admission"
    if getattr(authorization, "execution_profile", None) != "standard":
        return "the run authorization is not for the Standard profile"
    if not is_jit_bundle(bundle, workflow) or not is_admitted_program(bundle):
        return "the bundle is not an admitted program with a read-back check"
    try:
        verify_admission(bundle, key_dir=key_dir)
        digest = admission_sha256(bundle)
        readback_effects(bundle)
    except (AdmissionError, HostedAuthorityError):
        return "the program's admission or read-back check does not verify"
    if getattr(authorization, "approval_source", None) != approval_source(digest):
        return "the run authorization does not name this program's admission"
    return None


def shape_matches_authorization(shape: Mapping[str, Any], authorization: Any) -> bool:
    """Whether a published run shape agrees with a run authorization."""

    requirements = [
        item.model_dump(mode="json")
        for item in getattr(authorization, "qualified_effect_requirements", ())
    ]
    return bool(
        shape.get("schema_version") == RUN_SHAPE_SCHEMA
        and shape.get("bundle_content_digest")
        == getattr(authorization, "bundle_content_digest", None)
        and shape.get("admitted_policy_name")
        == getattr(authorization, "admitted_policy_name", None)
        and shape.get("admitted_policy_contract_sha256")
        == getattr(authorization, "admitted_policy_contract_sha256", None)
        and shape.get("execution_profile")
        == getattr(authorization, "execution_profile", None)
        and shape.get("minimum_effect_tier")
        == getattr(authorization, "minimum_effect_tier", None)
        and shape.get("qualified_effect_requirements") == requirements
        and list(shape.get("required_identity_step_ids") or [])
        == list(getattr(authorization, "required_identity_step_ids", ()))
        and isinstance(shape.get("admission_sha256"), str)
        and getattr(authorization, "approval_source", None)
        == APPROVAL_PREFIX + str(shape.get("admission_sha256"))
    )
