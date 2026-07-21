"""Local, independent verification of a leased dispatch — LOCAL GATES ARE FINAL.

The control plane can only REQUEST what local policy already permits. Before a
single GUI pixel is touched, this module re-derives every claim in the
dispatch from material the machine already holds:

1. the job kind is one this client implements;
2. no run of the same workflow is already in flight on this machine (the
   dispatch route has no idempotency key — the client serializes);
3. the dispatch has not passed its cloud-stamped expiry (agents refuse to
   START stale work — the queue mirrors that refusal server-side);
4. the authorization and the dispatch agree on the bundle content digest,
   and that digest names a bundle the OPERATOR listed in the local trust
   manifest (``runner.toml``) — ``bundle.url`` is never fetched;
5. runtime params arrived in a form this machine's posture accepts (inline
   values are refused for ``params_ref_required`` bundles; the regulated
   params-by-reference lane has no local resolver yet: refuse, don't guess)
   and every value matches its operator-pinned domain pattern;
6. the deployment profile id names a locally configured deployment.yaml that
   does not enable model-grounding egress (the evidence contract asserts
   ``screenshots_may_leave_box: false``; a profile that could make that
   assertion untrue is refused up front);
7. the embedded ``GovernedRunAuthorization`` actually FITS the loaded sealed
   bundle (digest match, recomputed semantics, identity steps, exact
   write-approval contract hashes — ``validate_workflow``, flow PR #129);
8. the runtime-inputs digest recomputed over the local bundle's effective
   params equals the authorization's binding (fail closed on drift);
9. an operator-pinned policy matches the authorization's admitted policy.

Anything unverifiable is a :class:`Refusal` with a stable machine-readable
code — reportable to the cloud as an ``authorization_refused`` halt, and
never executed. After verification a real execution STILL goes through the
full fail-closed ``openadapt-flow run`` admission gate in a child process
(:mod:`openadapt_flow.runner.commands`); this module is an additional refusal
layer, not a bypass of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from openadapt_flow.runner.config import RunnerConfig, TrustedBundle
from openadapt_flow.runner.protocol import (
    JOB_KIND_GOVERNED_RUN,
    DispatchParamsValues,
    RunnerDispatchPayload,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from openadapt_flow.ir import Workflow


class RefusalCode(str, Enum):
    """Stable identifiers for every way this client refuses a dispatch."""

    #: job_kind is not ``governed_run`` (pause/resume/etc. are not yet in the
    #: merged cloud contract; anything unknown is refused, never guessed).
    UNSUPPORTED_JOB_KIND = "unsupported_job_kind"
    #: The dispatch payload failed strict contract parsing.
    MALFORMED_DISPATCH = "malformed_dispatch"
    #: ``expires_at`` is in the past — stale attended work is never started.
    DISPATCH_EXPIRED = "dispatch_expired"
    #: Params arrived by reference (regulated lane); no local resolver in v1.
    PARAMS_REF_UNSUPPORTED = "params_ref_unsupported"
    #: The bundle is configured ``params_ref_required`` and the dispatch sent
    #: inline ``params.values`` — for the regulated posture, runtime params
    #: ARE the PHI and must never ride the dispatch wire (review PHI-3).
    PARAMS_VALUES_REFUSED = "params_values_refused"
    #: A supplied param failed (or lacked) its operator-pinned domain pattern
    #: — local policy distinguishes good params from bad ones (review S2).
    PARAM_DOMAIN_REFUSED = "param_domain_refused"
    #: A run for this workflow is already in flight on this machine; dispatch
    #: enqueue has no idempotency key, so the client serializes per workflow
    #: and refuses the duplicate (review S3).
    CONCURRENT_RUN = "concurrent_run"
    #: The bundle digest is not in the operator trust manifest. The runner
    #: NEVER fetches bundle.url — no remote code delivery.
    BUNDLE_NOT_HELD = "bundle_not_held"
    #: The payload disagrees with itself (authorization digest != bundle digest).
    DIGEST_MISMATCH = "digest_mismatch"
    #: deployment_profile_id names no locally configured profile.
    UNKNOWN_PROFILE = "unknown_profile"
    #: The named profile enables model-grounding egress, which would falsify
    #: the evidence stream's ``screenshots_may_leave_box: false`` assertion.
    EGRESS_PROFILE_REFUSED = "egress_profile_refused"
    #: The trusted bundle failed to load/decrypt locally.
    BUNDLE_LOAD_FAILED = "bundle_load_failed"
    #: ``GovernedRunAuthorization.validate_workflow`` refused the fit.
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    #: Recomputed runtime-inputs digest differs from the authorization binding.
    RUNTIME_INPUTS_MISMATCH = "runtime_inputs_mismatch"
    #: Operator-pinned policy differs from the admitted policy name.
    POLICY_MISMATCH = "policy_mismatch"


@dataclass(frozen=True)
class Refusal:
    """A refused dispatch: the code is stable, the detail is structural
    (digest prefixes / ids / counts — never free text from the payload)."""

    code: RefusalCode
    detail: str

    def reason(self) -> str:
        """The PHI-free reason string reported in the evidence stream."""
        return f"{self.code.value}: {self.detail}"[:400]


@dataclass(frozen=True)
class VerifiedDispatch:
    """Everything execution needs, derived from locally trusted material."""

    payload: RunnerDispatchPayload
    bundle: TrustedBundle
    profile_path: "Path"
    params: dict[str, str]
    workflow: "Workflow"
    #: Whole-workflow coverage counts for the terminal run_summary (computed
    #: from the sealed bundle, not from the cloud's claims).
    consequential_steps: int
    effect_covered_consequential_steps: int


def _parse_expiry(raw: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def verify_dispatch(
    payload: RunnerDispatchPayload,
    config: RunnerConfig,
    *,
    now: Optional[datetime] = None,
    active_workflow_ids: Optional[set[str]] = None,
) -> VerifiedDispatch | Refusal:
    """Independently verify ``payload`` against local trust. Never executes.

    ``active_workflow_ids`` is the caller's in-flight run registry (see
    :class:`openadapt_flow.runner.lease.WorkflowSerialization`): a dispatch
    for a workflow that already has a run in flight on this machine is
    refused, because the dispatch route has no idempotency key and a
    double-enqueue would otherwise write to the system of record twice.
    """
    if payload.job_kind != JOB_KIND_GOVERNED_RUN:
        return Refusal(
            RefusalCode.UNSUPPORTED_JOB_KIND,
            f"job_kind {payload.job_kind!r} is not implemented by this client",
        )

    if active_workflow_ids and payload.workflow_id in active_workflow_ids:
        return Refusal(
            RefusalCode.CONCURRENT_RUN,
            f"workflow {payload.workflow_id} already has a run in flight on "
            "this machine; refusing the duplicate dispatch",
        )

    expiry = _parse_expiry(payload.expires_at)
    current = now or datetime.now(timezone.utc)
    if expiry is None:
        return Refusal(
            RefusalCode.MALFORMED_DISPATCH, "expires_at is not an ISO timestamp"
        )
    if current >= expiry:
        return Refusal(
            RefusalCode.DISPATCH_EXPIRED,
            "dispatch expired before this runner could start it",
        )

    digest = payload.bundle.content_digest
    if payload.authorization.bundle_content_digest != digest:
        return Refusal(
            RefusalCode.DIGEST_MISMATCH,
            "authorization is bound to "
            f"{payload.authorization.bundle_content_digest[:16]}... but the "
            f"dispatch names bundle {digest[:16]}...",
        )

    trusted = config.bundles.get(digest)
    if trusted is None:
        return Refusal(
            RefusalCode.BUNDLE_NOT_HELD,
            f"bundle {digest[:16]}... is not in the local trust manifest; "
            "this runner never downloads bundles",
        )

    if not isinstance(payload.params, DispatchParamsValues):
        return Refusal(
            RefusalCode.PARAMS_REF_UNSUPPORTED,
            "params-by-reference (regulated lane) has no local resolver in v1",
        )
    if trusted.params_ref_required:
        return Refusal(
            RefusalCode.PARAMS_VALUES_REFUSED,
            "this bundle requires params-by-reference; inline params.values "
            "dispatches are refused on this machine",
        )
    params = dict(payload.params.values)

    if trusted.param_patterns:
        for key in sorted(params):
            pattern = trusted.param_patterns.get(key)
            if pattern is None:
                return Refusal(
                    RefusalCode.PARAM_DOMAIN_REFUSED,
                    f"param {key!r} has no operator-pinned domain pattern",
                )
            if re.fullmatch(pattern, params[key]) is None:
                return Refusal(
                    RefusalCode.PARAM_DOMAIN_REFUSED,
                    f"param {key!r} does not match its pinned domain pattern",
                )

    profile_path = config.profiles.get(payload.deployment_profile_id)
    if profile_path is None:
        return Refusal(
            RefusalCode.UNKNOWN_PROFILE,
            f"deployment profile {payload.deployment_profile_id!r} is not "
            "configured on this machine",
        )

    from openadapt_flow.deployment import load_deployment

    try:
        deployment = load_deployment(profile_path)
    except Exception as exc:  # noqa: BLE001 - any load failure refuses
        return Refusal(
            RefusalCode.UNKNOWN_PROFILE,
            f"deployment profile {payload.deployment_profile_id!r} failed to "
            f"load: {type(exc).__name__}",
        )
    if deployment.runtime.allow_model_grounding:
        return Refusal(
            RefusalCode.EGRESS_PROFILE_REFUSED,
            "profile enables model-grounding egress; the runner evidence "
            "contract requires screenshots_may_leave_box=false",
        )

    from openadapt_flow.ir import Workflow

    try:
        workflow = Workflow.load(trusted.path)
    except Exception as exc:  # noqa: BLE001 - crypto/integrity/shape: refuse
        return Refusal(
            RefusalCode.BUNDLE_LOAD_FAILED,
            f"trusted bundle failed to load: {type(exc).__name__}",
        )

    fit_refusal = payload.authorization.validate_workflow(workflow)
    if fit_refusal is not None:
        return Refusal(RefusalCode.AUTHORIZATION_MISMATCH, fit_refusal)

    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    recomputed = runtime_inputs_digest(workflow, params, None)
    if recomputed != payload.authorization.runtime_inputs_digest:
        return Refusal(
            RefusalCode.RUNTIME_INPUTS_MISMATCH,
            "locally recomputed runtime-inputs digest "
            f"{recomputed[:16]}... does not match the authorization binding "
            f"{payload.authorization.runtime_inputs_digest[:16]}...",
        )

    if (
        trusted.policy is not None
        and payload.authorization.admitted_policy_name != trusted.policy
    ):
        return Refusal(
            RefusalCode.POLICY_MISMATCH,
            f"authorization admits policy "
            f"{payload.authorization.admitted_policy_name!r} but this machine "
            f"pins {trusted.policy!r} for this bundle",
        )

    from openadapt_flow.policy import has_system_effect
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    steps = list(iter_workflow_steps(workflow))
    consequential = [step for step in steps if is_consequential(step)]
    effect_covered = [step for step in consequential if has_system_effect(step)]

    return VerifiedDispatch(
        payload=payload,
        bundle=trusted,
        profile_path=profile_path,
        params=params,
        workflow=workflow,
        consequential_steps=len(consequential),
        effect_covered_consequential_steps=len(effect_covered),
    )
