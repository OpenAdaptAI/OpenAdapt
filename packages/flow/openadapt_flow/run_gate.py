"""Fail-closed admission gate for governed execution (``openadapt-flow run``).

``replay`` is the permissive DEMO path: it will drive a bundle against an app
with every safety control (certification, identity arming, effect verification,
encryption) left OPTIONAL. Its report is explicitly non-production.

``run`` applies Demo, Standard, or Regulated requirements to this one admission
gate. It is a PURE function of a loaded bundle plus its deployment wiring: it
executes nothing and mutates nothing. It answers one question -- *may this
bundle be executed under the selected profile in this deployment?* -- and
FAILS CLOSED with a structured reason naming every failed requirement.

1. **Certification** -- the bundle passes a required safety policy (default
   ``clinical-write``, or ``--policy``). An uncertified bundle is refused.
2. **Identity coverage** -- every entity-sensitive / consequential action is
   IDENTITY-ARMED. An unarmed consequential action (one that would act with no
   verified target identity) refuses the run; it is never silently proceeded.
3. **Effect coverage** -- every consequential write DECLARES a system-of-record
   effect contract (and none is an unconfirmed / fabricated binding). A write
   that would be verified by the SCREEN only -- because it declares no
   system-of-record effect -- refuses the run.
4. **Effect execution** -- Standard and Regulated require a verifier meeting
   the workflow's declared evidence tier in THIS deployment. Demo and the
   legacy compatibility lane may use exact, bundle-bound approval, but that
   result remains unverified.
5. **Interstitial admission** -- every bundle/runtime interstitial declaration
   is schema-valid, every explicit asset is sealed in the bundle manifest, and
   the exact declaration digest is recorded for authorization binding.
6. **Encryption at rest** -- the bundle's ``workflow.json`` and template crops
   are sealed with AES-256-GCM. A plaintext bundle is refused. Any additional
   plaintext template / screenshot asset is a loud WARNING by default and a
   REFUSAL under ``strict_templates``.
7. **Sealed manifest + version pin** -- the bundle carries an integrity-sealed
   manifest whose digest re-verifies (no post-seal tampering), and any supplied
   version pin (content digest / compiler version) matches. A mismatch refuses.

The gate reuses -- never re-implements -- the existing analysis primitives:
policy certification (:func:`openadapt_flow.policy.evaluate_policy`), the
identity-arming predicates (:func:`openadapt_flow.policy.is_identity_armed`),
the effect-contract predicates
(:func:`openadapt_flow.policy.has_system_effect`), and the manifest integrity
check (:func:`openadapt_flow.bundle_validation.verify_integrity`).

Scope: this gate ENFORCES coverage; it does not SYNTHESISE missing contracts
(no effect auto-inference -- that is a separate follow-up). An inadequate bundle
is refused, not repaired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow import crypto
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.execution_profiles import (
    ExecutionProfileContract,
    qualified_effect_requirements,
    required_effect_tier,
)
from openadapt_flow.ir import (
    Interstitial,
    QualifiedEffectRequirement,
    Step,
    Workflow,
)
from openadapt_flow.policy import (
    Policy,
    all_effect_paths_covered,
    executable_actuation_paths,
    has_postcondition_contract,
    has_screen_postcondition,
    has_system_effect,
    has_unconfirmed_effect_binding,
    is_identity_applicable,
    is_identity_armed,
    iter_effect_paths,
    missing_effect_paths,
    path_requires_gui_contracts,
    policy_contract_sha256,
    project_step_safety,
    step_tags,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    UnverifiedWriteApproval,
    interstitial_declarations_digest,
    runtime_inputs_digest,
)
from openadapt_flow.traversal import iter_workflow_steps
from openadapt_flow.verification import VerificationTier, verifier_effect_tier

#: Default certifying policy for a regulated run when none is configured.
DEFAULT_POLICY: str = "clinical-write"

# Gate identifiers (stable strings a caller / test can assert on).
GATE_CERTIFICATION = "certification"
GATE_PROFILE = "execution_profile"
GATE_IDENTITY = "identity_coverage"
GATE_EFFECT = "effect_coverage"
GATE_APPROVAL = "approval_fallback"
GATE_INTERSTITIALS = "interstitial_admission"
GATE_ENCRYPTION = "encryption"
GATE_MANIFEST = "manifest_integrity"

#: The gates, in the order the report renders them.
GATE_ORDER = (
    GATE_PROFILE,
    GATE_CERTIFICATION,
    GATE_IDENTITY,
    GATE_EFFECT,
    GATE_APPROVAL,
    GATE_INTERSTITIALS,
    GATE_ENCRYPTION,
    GATE_MANIFEST,
)

_GATE_TITLES = {
    GATE_PROFILE: "Execution profile",
    GATE_CERTIFICATION: "Certification passed",
    GATE_IDENTITY: "Identity coverage",
    GATE_EFFECT: "Effect coverage",
    GATE_APPROVAL: "Approval fallback",
    GATE_INTERSTITIALS: "Interstitial admission",
    GATE_ENCRYPTION: "Encrypted bundle",
    GATE_MANIFEST: "Sealed manifest + version pin",
}


# ---------------------------------------------------------------------------
# Consequential-action classification (fail-closed: err toward "consequential")
# ---------------------------------------------------------------------------


def is_consequential(
    step: Step,
    workflow: Optional[Workflow] = None,
    *,
    require_current_risk_certification: bool = True,
    certifying_policy: Optional[Policy] = None,
    certifying_policy_sha256: Optional[str] = None,
) -> bool:
    """Whether ``step`` commits a consequential (irreversible) write.

    Fail-closed union of every signal the codebase already carries: the
    compiled ``risk`` label, the write-shaped heuristic
    (:func:`openadapt_flow.risk.classify_step_risk`), and the presence of a
    declared system-of-record effect. A step any of these flag is treated as a
    write for coverage purposes.

    Production profiles require a current passing qualification certification
    before an operator down-classification is authoritative. Legacy/no-profile
    qualification gates instead bind the typed project decision through their
    live policy decision and exact sealed manifest.
    """
    return project_step_safety(
        step,
        workflow,
        require_current_certification=require_current_risk_certification,
        certifying_policy=certifying_policy,
        certifying_policy_sha256=certifying_policy_sha256,
    ).consequential


def must_be_identity_armed(
    step: Step,
    workflow: Optional[Workflow] = None,
    *,
    require_current_risk_certification: bool = True,
    certifying_policy: Optional[Policy] = None,
    certifying_policy_sha256: Optional[str] = None,
) -> bool:
    """Whether the pre-click identity check MUST be armed on ``step``.

    Every consequential action requires identity. An action without a target
    identity contract is an identity-coverage defect, not an exemption. An
    identity-applicable entity-navigation action also requires identity.
    """
    consequential = is_consequential(
        step,
        workflow,
        require_current_risk_certification=require_current_risk_certification,
        certifying_policy=certifying_policy,
        certifying_policy_sha256=certifying_policy_sha256,
    )
    if consequential:
        return True
    if not is_identity_applicable(step):
        return False
    return "entity_navigation" in step_tags(
        step,
        workflow,
        require_current_certification=require_current_risk_certification,
        certifying_policy=certifying_policy,
        certifying_policy_sha256=certifying_policy_sha256,
    )


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


class GateResult(BaseModel):
    """The outcome of one admission gate."""

    gate: str
    title: str
    passed: bool
    #: A WARNING-only result: informational, does NOT fail the run (used for the
    #: unsealed-template notice when ``strict_templates`` is off).
    warning: bool = False
    detail: str = ""
    #: Step ids that caused a refusal (empty on a pass).
    offenders: list[str] = Field(default_factory=list)

    def render(self) -> str:
        if self.passed and self.warning:
            mark = "WARN"
        elif self.passed:
            mark = "PASS"
        else:
            mark = "REFUSE"
        line = f"  [{mark}] {self.title}: {self.detail}"
        if self.offenders:
            shown = ", ".join(self.offenders[:8])
            if len(self.offenders) > 8:
                shown += f", ... (+{len(self.offenders) - 8} more)"
            line += f"\n         steps: {shown}"
        return line


class RunGateReport(BaseModel):
    """The whole admission decision: a coverage report and a pass/refuse verdict.

    ``passed`` is True only when EVERY non-warning gate passed. A refused report
    lists which gate(s) refused and why, so the operator sees the FIRST thing to
    fix, not a generic denial.
    """

    workflow_name: str
    policy_name: str
    policy_contract_sha256: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    execution_profile: Optional[Literal["demo", "standard", "regulated"]] = None
    gates: list[GateResult] = Field(default_factory=list)
    bundle_content_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")
    required_identity_step_ids: list[str] = Field(default_factory=list)
    effect_verifier_configured: bool = False
    minimum_effect_tier: Optional[int] = Field(default=None, ge=1, le=4)
    qualified_effect_requirements: list[QualifiedEffectRequirement] = Field(
        default_factory=list
    )
    api_actuator_configured: bool = False
    unverified_write_approval_granted: bool = False
    admitted_interstitials_digest: Optional[str] = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates if not g.warning)

    @property
    def refusals(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    def gate(self, gate_id: str) -> Optional[GateResult]:
        """The result for ``gate_id`` (or None if the gate was not evaluated)."""
        for g in self.gates:
            if g.gate == gate_id:
                return g
        return None

    def render(self) -> str:
        head = (
            f"{'ADMIT' if self.passed else 'REFUSE'}: "
            f"workflow {self.workflow_name!r}"
            + (
                f" under {self.execution_profile!r} profile"
                if self.execution_profile
                else ""
            )
            + f" vs policy {self.policy_name!r}"
        )
        lines = [head]
        lines.extend(g.render() for g in self.gates)
        if not self.passed:
            names = ", ".join(g.title for g in self.refusals if not g.warning)
            lines.append(
                f"  -> RUN REFUSED (fail-closed): {names}. Nothing was executed."
            )
        else:
            lines.append("  -> ADMITTED: all fail-closed gates satisfied.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def evaluate_run_gate(
    workflow: Workflow,
    *,
    bundle_dir: Path | str,
    deployment: DeploymentConfig,
    effect_verifier: object | None,
    api_actuator: object | None = None,
    interstitials: Optional[list[Interstitial]] = None,
    policy_source: Optional[str] = None,
    approval_available: bool = False,
    strict_templates: bool = False,
    require_encryption: bool = True,
    pinned_content_digest: Optional[str] = None,
    pinned_compiler_version: Optional[str] = None,
    profile_contract: Optional[ExecutionProfileContract] = None,
    effective_durable: Optional[bool] = None,
    effective_require_settled: Optional[bool] = None,
    qualification_evidence_only: bool = False,
) -> RunGateReport:
    """Admit or refuse ``workflow`` for a regulated run in this deployment.

    Pure: reads the bundle + deployment wiring and returns a
    :class:`RunGateReport`. It executes nothing and writes nothing.

    Args:
        workflow: The loaded bundle (already decrypted, if it was encrypted --
            :meth:`Workflow.load` sets ``workflow.encrypted`` accordingly).
        bundle_dir: The bundle directory on disk (for manifest / template
            checks).
        deployment: The deployment wiring (policy, effects substrate, ...).
        effect_verifier: The verifier constructed for this deployment (None when
            ``effects.kind`` is ``none`` -- i.e. no independent write verifier).
        interstitials: Additional runtime interstitial declarations. The gate
            evaluates and records their exact digest before authorization; a
            caller cannot add a pre-step key/click action after admission.
        policy_source: Certifying policy name / path. Defaults to the
            deployment's ``policy.policy``, then to :data:`DEFAULT_POLICY`.
        approval_available: The operator has EXPLICITLY approved executing writes
            whose effects cannot be independently verified in this deployment
            (gate 4 fallback). Default False (fail closed).
        strict_templates: Treat any genuinely unsealed template / screenshot
            asset as a REFUSAL rather than a warning (gate 6). Ciphertexts
            produced by ``Workflow.save(encrypt=True)`` satisfy this gate.
        require_encryption: Require the bundle be AES-GCM encrypted at rest
            (gate 6). Default True (fail closed).
        pinned_content_digest / pinned_compiler_version: Optional version pins
            (gate 7); a supplied pin that does not match the sealed manifest
            refuses the run.
    """
    bundle = Path(bundle_dir)
    policy_name = (
        policy_source
        or deployment.policy.policy
        or (profile_contract.default_policy if profile_contract is not None else None)
        or DEFAULT_POLICY
    )
    steps = list(iter_workflow_steps(workflow))
    minimum_effect_tier = (
        required_effect_tier(workflow, profile_contract.profile)
        if profile_contract is not None
        else None
    )
    qualified_requirements: tuple[QualifiedEffectRequirement, ...] = ()
    qualified_requirements_error: str | None = None
    if profile_contract is not None:
        try:
            qualified_requirements = qualified_effect_requirements(
                workflow, profile_contract.profile
            )
        except ValueError as exc:
            qualified_requirements_error = str(exc)
    # Production admission requires the completed qualification campaign.
    # Demo and legacy qualification harnesses may bind a typed operator review
    # through this policy decision plus the exact sealed manifest.
    require_current_risk_cert = bool(
        profile_contract is not None
        and profile_contract.production
        and not qualification_evidence_only
    )
    try:
        from openadapt_flow.policy import load_policy

        certifying_policy = load_policy(policy_name)
    except (FileNotFoundError, ValueError):
        certifying_policy = None
    certifying_policy_digest = (
        policy_contract_sha256(certifying_policy)
        if certifying_policy is not None
        else None
    )

    approval_gate = _gate_approval(
        workflow,
        steps,
        effect_verifier,
        api_actuator,
        approval_available,
        minimum_effect_tier=minimum_effect_tier,
        qualified_effect_requirements=qualified_requirements,
        qualified_effect_requirements_error=qualified_requirements_error,
        require_approval=(
            profile_contract.require_approval_for_unverified_effects
            if profile_contract is not None
            else True
        ),
        allow_approval=(
            profile_contract.allow_unverified_write_approval
            if profile_contract is not None
            else True
        ),
        require_current_risk_certification=require_current_risk_cert,
        certifying_policy=certifying_policy,
    )
    interstitial_gate = _gate_interstitials(workflow, interstitials)
    gates = []
    if profile_contract is not None:
        gates.append(
            _gate_profile(
                profile_contract,
                effective_durable,
                effective_require_settled,
            )
        )
    gates.extend(
        [
            (
                _gate_certification(
                    workflow,
                    policy_name,
                    require_current_risk_certification=require_current_risk_cert,
                    certifying_policy=certifying_policy,
                )
                if profile_contract is None or profile_contract.require_certification
                else _not_required(
                    GATE_CERTIFICATION, "not required by the Demo profile"
                )
            ),
            (
                _gate_identity(
                    workflow,
                    steps,
                    require_current_risk_certification=require_current_risk_cert,
                    certifying_policy=certifying_policy,
                )
                if profile_contract is None
                or profile_contract.require_identity_coverage
                else _not_required(GATE_IDENTITY, "not required by the Demo profile")
            ),
            (
                _gate_effect(
                    workflow,
                    steps,
                    require_postconditions=(
                        profile_contract.require_consequential_postconditions
                        if profile_contract is not None
                        else False
                    ),
                    require_current_risk_certification=require_current_risk_cert,
                    certifying_policy=certifying_policy,
                )
                if profile_contract is None or profile_contract.require_effect_contracts
                else _not_required(
                    GATE_EFFECT,
                    "not required by the Demo profile; screen evidence cannot "
                    "produce a production VERIFIED outcome",
                )
            ),
            approval_gate,
            interstitial_gate,
            _gate_encryption(
                workflow,
                bundle,
                (
                    profile_contract.require_encryption
                    if profile_contract is not None
                    else require_encryption
                ),
                (
                    profile_contract.strict_templates or strict_templates
                    if profile_contract is not None
                    else strict_templates
                ),
            ),
            _gate_manifest(
                workflow, bundle, pinned_content_digest, pinned_compiler_version
            ),
        ]
    )
    required_identity = (
        [
            step.id
            for step in steps
            if must_be_identity_armed(
                step,
                workflow,
                require_current_risk_certification=require_current_risk_cert,
                certifying_policy=certifying_policy,
            )
        ]
        if profile_contract is None or profile_contract.require_identity_coverage
        else []
    )
    return RunGateReport(
        workflow_name=workflow.name,
        policy_name=policy_name,
        policy_contract_sha256=certifying_policy_digest,
        execution_profile=(
            profile_contract.profile.value if profile_contract is not None else None
        ),
        gates=gates,
        bundle_content_digest=(
            workflow.manifest.content_digest if workflow.manifest is not None else None
        ),
        required_identity_step_ids=required_identity,
        effect_verifier_configured=effect_verifier is not None,
        minimum_effect_tier=(
            int(minimum_effect_tier) if minimum_effect_tier is not None else None
        ),
        qualified_effect_requirements=list(qualified_requirements),
        api_actuator_configured=api_actuator is not None,
        unverified_write_approval_granted=(
            effect_verifier is None
            and approval_available
            and (
                profile_contract is None
                or profile_contract.allow_unverified_write_approval
            )
            and approval_gate.passed
            and any(
                is_consequential(
                    step,
                    workflow,
                    require_current_risk_certification=require_current_risk_cert,
                    certifying_policy=certifying_policy,
                )
                and has_system_effect(step)
                for step in steps
            )
        ),
        admitted_interstitials_digest=(
            interstitial_declarations_digest(workflow, interstitials)
            if interstitial_gate.passed
            else None
        ),
    )


def _result(
    gate: str,
    passed: bool,
    detail: str,
    offenders: Optional[list[str]] = None,
    *,
    warning: bool = False,
) -> GateResult:
    return GateResult(
        gate=gate,
        title=_GATE_TITLES[gate],
        passed=passed,
        warning=warning,
        detail=detail,
        offenders=offenders or [],
    )


def _not_required(gate: str, detail: str) -> GateResult:
    return _result(gate, True, detail)


def _gate_profile(
    contract: ExecutionProfileContract,
    effective_durable: Optional[bool],
    effective_require_settled: Optional[bool],
) -> GateResult:
    """Verify the effective runtime satisfies the selected named posture."""

    if contract.require_durable and effective_durable is not True:
        return _result(
            GATE_PROFILE,
            False,
            f"{contract.profile.value} requires durable execution, but the "
            "effective runtime is not durable",
        )
    if contract.require_settled and effective_require_settled is not True:
        return _result(
            GATE_PROFILE,
            False,
            f"{contract.profile.value} requires settled-state detection, but "
            "the effective runtime does not require settled frames",
        )
    durability = "required and enabled" if contract.require_durable else "optional"
    settling = "required and enabled" if contract.require_settled else "optional"
    production = "production" if contract.production else "non-production"
    return _result(
        GATE_PROFILE,
        True,
        f"{contract.profile.value} ({production}); durability {durability}; "
        f"settled-state detection {settling}",
    )


def _gate_certification(
    workflow: Workflow,
    policy_name: str,
    *,
    require_current_risk_certification: bool,
    certifying_policy: Optional[Policy],
) -> GateResult:
    """Gate 1: the bundle must PASS the required certifying policy."""
    from openadapt_flow.policy import evaluate_policy, load_policy

    try:
        policy = certifying_policy or load_policy(policy_name)
    except (FileNotFoundError, ValueError) as e:
        return _result(
            GATE_CERTIFICATION,
            False,
            f"certifying policy {policy_name!r} could not be loaded: {e}",
        )
    report = evaluate_policy(
        workflow,
        policy,
        require_current_risk_certification=require_current_risk_certification,
    )
    if report.passed:
        return _result(
            GATE_CERTIFICATION,
            True,
            f"certified under {policy_name!r} ({report.n_steps} steps, 0 violations)",
        )
    offenders = [v.step_id for v in report.violations if v.step_id]
    return _result(
        GATE_CERTIFICATION,
        False,
        f"bundle is NOT certified under {policy_name!r}: "
        f"{len(report.violations)} policy violation(s) "
        f"(e.g. {report.violations[0].rule}: {report.violations[0].reason})",
        offenders,
    )


def _gate_identity(
    workflow: Workflow,
    steps: list[Step],
    *,
    require_current_risk_certification: bool,
    certifying_policy: Optional[Policy],
) -> GateResult:
    """Gate 2: every entity-sensitive / consequential action is identity-armed."""
    must_arm = [
        step
        for step in steps
        if must_be_identity_armed(
            step,
            workflow,
            require_current_risk_certification=require_current_risk_certification,
            certifying_policy=certifying_policy,
        )
    ]
    unarmed = []
    for step in must_arm:
        paths = executable_actuation_paths(step)
        if ("gui" in paths and not is_identity_armed(step)) or (
            "api" in paths
            and (step.api_binding is None or not step.api_binding.identity)
        ):
            unarmed.append(step)
    total = len(must_arm)
    if not unarmed:
        return _result(
            GATE_IDENTITY,
            True,
            f"{total}/{total} entity-sensitive/consequential action(s) identity-armed",
        )
    return _result(
        GATE_IDENTITY,
        False,
        f"{len(unarmed)}/{total} entity-sensitive/consequential action(s) are "
        "UNARMED -- would act with no verified target identity",
        [s.id for s in unarmed],
    )


def _gate_effect(
    workflow: Workflow,
    steps: list[Step],
    *,
    require_postconditions: bool,
    require_current_risk_certification: bool,
    certifying_policy: Optional[Policy],
) -> GateResult:
    """Gate 3: every consequential write declares outcome contracts.

    A write with no declared system-of-record effect would be verified by the
    SCREEN only; a write whose effect binding was not derivable from the demo
    (``needs_operator_confirmation``) carries a fabricated/unconfirmed contract.
    Production profiles also require an immediate postcondition contract. These
    are bundle-level defects and cannot be waived by deployment approval.
    """
    writes = [
        step
        for step in steps
        if is_consequential(
            step,
            workflow,
            require_current_risk_certification=require_current_risk_certification,
            certifying_policy=certifying_policy,
        )
    ]
    # Each executable path needs its own exact effect contract. API-only
    # bindings omit GUI from the canonical path projection.
    screen_only = [s for s in writes if not all_effect_paths_covered(s)]
    unconfirmed = [s for s in writes if has_unconfirmed_effect_binding(s)]
    missing_postconditions = (
        [
            s
            for s in writes
            if path_requires_gui_contracts(s) and not has_postcondition_contract(s)
        ]
        if require_postconditions
        else []
    )
    offenders = sorted(
        {s.id for s in screen_only}
        | {s.id for s in unconfirmed}
        | {s.id for s in missing_postconditions}
    )
    total = len(writes)
    if not offenders:
        return _result(
            GATE_EFFECT,
            True,
            f"{total}/{total} consequential write(s) declare the required "
            "postcondition and confirmed system-of-record effect contracts",
        )
    parts = []
    if screen_only:
        path_count = sum(len(missing_effect_paths(step)) for step in screen_only)
        missing_names = sorted(
            {
                path.upper()
                for step in screen_only
                for path in missing_effect_paths(step)
            }
        )
        parts.append(
            f"{path_count} executable path(s) across {len(screen_only)} step(s) "
            f"({', '.join(missing_names)}) would be verified by SCREEN only"
        )
    if unconfirmed:
        parts.append(
            f"{len(unconfirmed)} carry an UNCONFIRMED effect binding "
            "(not derivable from the demonstration)"
        )
    if missing_postconditions:
        parts.append(
            f"{len(missing_postconditions)} have no immediate postcondition contract"
        )
    return _result(
        GATE_EFFECT,
        False,
        f"{len(offenders)}/{total} consequential write(s) lack an adequate "
        "effect contract: " + "; ".join(parts),
        offenders,
    )


def _gate_approval(
    workflow: Workflow,
    steps: list[Step],
    effect_verifier: object | None,
    api_actuator: object | None,
    approval_available: bool,
    *,
    minimum_effect_tier: Optional[VerificationTier] = None,
    qualified_effect_requirements: tuple[QualifiedEffectRequirement, ...] = (),
    qualified_effect_requirements_error: str | None = None,
    require_approval: bool = True,
    allow_approval: bool = True,
    require_current_risk_certification: bool,
    certifying_policy: Optional[Policy],
) -> GateResult:
    """Gate 4: writes with no configured verifier need explicit approval.

    A consequential write that DECLARES an effect but has NO verifier wired for
    this deployment cannot be verified (its effect would go
    unchecked). It is admitted ONLY under explicit operator approval; otherwise
    the run halts. Writes that DO have a verifier need nothing here.
    """
    if qualified_effect_requirements_error is not None:
        return _result(
            GATE_APPROVAL,
            False,
            "qualified effect requirements are invalid: "
            f"{qualified_effect_requirements_error}",
        )
    requirement_by_ref = {
        (item.step_id, item.actuation_path, item.effect_index): item
        for item in qualified_effect_requirements
    }
    writes = [
        step
        for step in steps
        if is_consequential(
            step,
            workflow,
            require_current_risk_certification=require_current_risk_certification,
            certifying_policy=certifying_policy,
        )
        and has_system_effect(step)
    ]
    if effect_verifier is not None:
        weak: list[str] = []
        untyped: list[str] = []
        observed: list[VerificationTier] = []
        required_observed: list[VerificationTier] = []
        for step in writes:
            for path, effects in iter_effect_paths(step):
                for index, effect in enumerate(effects):
                    tier = verifier_effect_tier(effect_verifier, effect)
                    requirement = requirement_by_ref.get((step.id, path, index))
                    required_tier = (
                        VerificationTier(requirement.minimum_tier)
                        if requirement is not None
                        else minimum_effect_tier
                    )
                    if tier is None:
                        untyped.append(step.id)
                    elif required_tier is not None and not tier.satisfies(
                        required_tier
                    ):
                        weak.append(step.id)
                        observed.append(tier)
                        required_observed.append(required_tier)
        if untyped and (minimum_effect_tier is not None or requirement_by_ref):
            return _result(
                GATE_APPROVAL,
                False,
                f"{len(set(untyped))} consequential write(s) use a verifier "
                "that does not declare a machine-readable evidence tier",
                sorted(set(untyped)),
            )
        if weak:
            tiers = ", ".join(
                sorted({tier.name.lower().replace("_", "-") for tier in observed})
            )
            required_names = ", ".join(
                sorted(
                    {tier.name.lower().replace("_", "-") for tier in required_observed}
                )
            )
            return _result(
                GATE_APPROVAL,
                False,
                f"{len(set(weak))} consequential write(s) have {tiers} evidence; "
                f"their qualified effects require {required_names}",
                sorted(set(weak)),
            )
        return _result(
            GATE_APPROVAL,
            True,
            f"a verifier satisfying the required evidence tier is configured; "
            f"{len(writes)} declared write(s) are covered",
        )
    # No verifier: every declared write is unverifiable in this deployment.
    if not writes:
        return _result(
            GATE_APPROVAL,
            True,
            "no consequential writes require effect verification",
        )
    direct_api_writes = [
        step
        for step in writes
        if api_actuator is not None and step.api_binding is not None
    ]
    if direct_api_writes:
        return _result(
            GATE_APPROVAL,
            False,
            f"{len(direct_api_writes)} direct API write(s) have no verifier; "
            "operator approval cannot replace the API tier's independent outcome "
            "check",
            [step.id for step in direct_api_writes],
        )
    if minimum_effect_tier is not None:
        return _result(
            GATE_APPROVAL,
            False,
            f"{len(writes)} consequential write(s) lack a verifier at the "
            "evidence tier required by this execution profile; an approval "
            "cannot convert unverified completion into VERIFIED",
            [step.id for step in writes],
        )
    if approval_available and not allow_approval:
        return _result(
            GATE_APPROVAL,
            False,
            "this execution profile does not permit unverified-write approval",
            [step.id for step in writes],
        )
    if approval_available and allow_approval:
        vacuous = [step for step in writes if not has_screen_postcondition(step)]
        if vacuous:
            return _result(
                GATE_APPROVAL,
                False,
                f"{len(vacuous)} approved-unverified GUI write(s) have no "
                "screen postcondition floor",
                [step.id for step in vacuous],
            )
        return _result(
            GATE_APPROVAL,
            True,
            f"NO verifier configured, but {len(writes)} unverifiable write(s) "
            "were EXPLICITLY approved by the operator (approval fallback)",
            [s.id for s in writes],
        )
    if not require_approval:
        return _result(
            GATE_APPROVAL,
            True,
            f"{len(writes)} consequential write(s) have no verifier; Demo may "
            "execute, but the result cannot be production VERIFIED",
            [s.id for s in writes],
        )
    return _result(
        GATE_APPROVAL,
        False,
        f"{len(writes)} consequential write(s) cannot be independently verified "
        "(no verifier configured for this deployment) and no explicit approval "
        "was provided -- halting",
        [s.id for s in writes],
    )


def _gate_interstitials(
    workflow: Workflow,
    runtime_interstitials: Optional[list[Interstitial]],
) -> GateResult:
    """Admit the exact declarative pre-step action surface.

    Schema validation enforces affirmative visual detection, Escape-only or a
    structurally/template-anchored click, explicit reversible/non-consequential
    risk, and visual clearance. Any explicitly referenced asset must already be
    present in the workflow's sealed manifest; runtime declarations may reuse a
    sealed bundle asset but cannot smuggle in an unreviewed file after the gate.
    """

    declarations = [*workflow.interstitials, *(runtime_interstitials or [])]
    validated: list[Interstitial] = []
    for declaration in declarations:
        try:
            validated.append(
                Interstitial.model_validate(declaration.model_dump(mode="python"))
            )
        except Exception as exc:
            return _result(
                GATE_INTERSTITIALS,
                False,
                "interstitial declaration is invalid and cannot be admitted "
                f"({type(exc).__name__})",
                [getattr(declaration, "name", "<invalid>")],
            )

    if not validated:
        return _result(
            GATE_INTERSTITIALS,
            True,
            "no automatic interstitial actions declared",
        )

    from openadapt_flow.bundle_validation import interstitial_asset_paths

    sealed_assets = (
        set(workflow.manifest.file_hashes) if workflow.manifest is not None else set()
    )
    missing = sorted(interstitial_asset_paths(validated) - sealed_assets)
    if missing:
        return _result(
            GATE_INTERSTITIALS,
            False,
            f"{len(missing)} interstitial asset(s) are not sealed in the bundle "
            "manifest",
            missing,
        )

    digest = interstitial_declarations_digest(workflow, runtime_interstitials)
    automatic = sum(
        declaration.dismiss_key is not None or declaration.dismiss_anchor is not None
        for declaration in validated
    )
    blocking = len(validated) - automatic
    return _result(
        GATE_INTERSTITIALS,
        True,
        f"admitted {automatic} automatic and {blocking} blocking declaration(s) "
        f"under digest {digest[:16]}...",
    )


def build_runtime_authorization(
    workflow: Workflow,
    report: RunGateReport,
    *,
    approval_source: str = "local-cli-explicit-flag",
    params: Optional[dict[str, str]] = None,
    worklists: Optional[dict[str, list[dict[str, str]]]] = None,
    interstitials: Optional[list[Interstitial]] = None,
) -> GovernedRunAuthorization:
    """Bind a successful admission decision to the exact sealed workflow.

    The returned capability is passed in-memory to :class:`Replayer`.  It
    enforces two facts that admission alone cannot: identity-required steps
    must receive an affirmative live verdict, and an approved unverifiable GUI
    write must be the exact step/effect contract the operator admitted.
    """
    if not report.passed:
        raise ValueError("cannot authorize a workflow that failed the run gate")
    if workflow.manifest is None or not workflow.manifest.content_digest:
        raise ValueError("cannot authorize an unsealed workflow")
    if report.bundle_content_digest != workflow.manifest.content_digest:
        raise ValueError("run gate report belongs to a different workflow")
    if report.policy_contract_sha256 is None:
        raise ValueError("run gate report has no exact admitted policy digest")

    from openadapt_flow.bundle_validation import compute_content_digest

    recomputed = compute_content_digest(workflow, workflow.manifest.file_hashes)
    if recomputed != report.bundle_content_digest:
        raise ValueError("workflow changed after the run gate evaluated it")

    declarations_digest = interstitial_declarations_digest(workflow, interstitials)
    if report.admitted_interstitials_digest is None:
        if workflow.interstitials or interstitials:
            raise ValueError(
                "run gate report did not admit the interstitial declarations"
            )
    elif report.admitted_interstitials_digest != declarations_digest:
        raise ValueError(
            "interstitial declarations changed after the run gate evaluated them"
        )

    steps = list(iter_workflow_steps(workflow))
    require_current_risk_cert = report.execution_profile in {
        "standard",
        "regulated",
    }
    approvals: list[UnverifiedWriteApproval] = []
    if report.unverified_write_approval_granted:
        approvals = [
            UnverifiedWriteApproval(
                step_id=step.id,
                effect_contract_hashes=tuple(
                    effect.contract_hash() for effect in step.effects
                ),
            )
            for step in steps
            if is_consequential(
                step,
                workflow,
                require_current_risk_certification=require_current_risk_cert,
                certifying_policy_sha256=report.policy_contract_sha256,
            )
            # This capability authorizes only an unverifiable GUI write.  API
            # effects belong to the independently verified API actuation path
            # and must never create (or be conflated with) a GUI approval.
            and bool(step.effects)
        ]

    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(
            workflow,
            params,
            worklists,
            interstitials=interstitials,
        ),
        admitted_policy_name=report.policy_name,
        admitted_policy_contract_sha256=report.policy_contract_sha256,
        execution_profile=report.execution_profile,
        minimum_effect_tier=report.minimum_effect_tier,
        qualified_effect_requirements=tuple(report.qualified_effect_requirements),
        required_identity_step_ids=tuple(report.required_identity_step_ids),
        unverified_write_approvals=tuple(approvals),
        approval_source=approval_source,
    )


def _template_asset_encryption(
    workflow: Workflow, bundle: Path
) -> tuple[list[str], list[str], list[str]]:
    """Return logical assets, cleartext leaks, and uncovered declared assets."""

    assets: set[str] = set()
    unsealed: set[str] = set()
    uncovered: set[str] = set()
    manifest = workflow.manifest
    declared = set(manifest.file_hashes) if manifest is not None else set()
    if manifest is not None:
        assets.update(declared)
    tdir = bundle / "templates"
    if tdir.is_dir():
        for p in tdir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(bundle).as_posix()
            if rel.endswith(".enc"):
                logical = rel.removesuffix(".enc")
                assets.add(logical)
                if not crypto.is_encrypted(p.read_bytes()):
                    unsealed.add(rel)
            else:
                assets.add(rel)
                unsealed.add(rel)
    if workflow.encrypted:
        authenticated = workflow.decrypted_templates()
        for rel in declared:
            ciphertext = bundle / f"{rel}.enc"
            if (
                not ciphertext.is_file()
                or not crypto.is_encrypted(ciphertext.read_bytes())
                or rel not in authenticated
            ):
                uncovered.add(rel)
    return sorted(assets), sorted(unsealed), sorted(uncovered)


def _gate_encryption(
    workflow: Workflow,
    bundle: Path,
    require_encryption: bool,
    strict_templates: bool,
) -> GateResult:
    """Gate 6: the bundle is AES-GCM encrypted at rest (+ template coverage)."""
    if require_encryption and not workflow.encrypted:
        return _result(
            GATE_ENCRYPTION,
            False,
            "bundle workflow.json is NOT encrypted at rest (AES-256-GCM). "
            "Set OPENADAPT_BUNDLE_KEY, seal a new candidate with "
            "`openadapt-flow seal SOURCE --out DEST`, then certify that exact "
            "sealed destination",
        )
    templates, unsealed, uncovered = _template_asset_encryption(workflow, bundle)
    enc_note = "encrypted" if workflow.encrypted else "plaintext (not required)"
    if not templates:
        return _result(
            GATE_ENCRYPTION,
            True,
            f"workflow.json {enc_note}; no template/screenshot assets present",
        )
    if workflow.encrypted and uncovered:
        return _result(
            GATE_ENCRYPTION,
            False,
            f"workflow.json encrypted, but {len(uncovered)} declared template/"
            "screenshot asset(s) lack authenticated ciphertext coverage",
            uncovered,
        )
    if workflow.encrypted and unsealed:
        return _result(
            GATE_ENCRYPTION,
            False,
            f"workflow.json encrypted, but {len(unsealed)} plaintext template/"
            "screenshot asset(s) remain on disk; mixed encrypted/plaintext "
            "bundles are refused",
            unsealed,
        )
    if not unsealed:
        return _result(
            GATE_ENCRYPTION,
            True,
            f"workflow.json {enc_note}; {len(templates)} template/screenshot "
            "asset(s) encrypted at rest",
        )
    if strict_templates:
        return _result(
            GATE_ENCRYPTION,
            False,
            f"workflow.json {enc_note}, but {len(unsealed)} template/screenshot "
            "asset(s) are UNSEALED (plaintext at rest) and --strict-templates "
            "is set",
            unsealed,
        )
    return _result(
        GATE_ENCRYPTION,
        True,
        f"workflow.json {enc_note}; WARNING: {len(unsealed)} template/"
        "screenshot asset(s) are unsealed (plaintext at rest) -- protect via "
        "disk encryption or run with --strict-templates to refuse",
        unsealed,
        warning=True,
    )


def _gate_manifest(
    workflow: Workflow,
    bundle: Path,
    pinned_content_digest: Optional[str],
    pinned_compiler_version: Optional[str],
) -> GateResult:
    """Gate 7: sealed integrity manifest re-verifies + version pins match."""
    from openadapt_flow.bundle_validation import (
        BundleIntegrityError,
        verify_integrity,
    )

    manifest = workflow.manifest
    if manifest is None or not manifest.content_digest:
        return _result(
            GATE_MANIFEST,
            False,
            "bundle carries no integrity-sealed manifest (no content digest) "
            "-- cannot verify provenance or version-pin it",
        )
    try:
        verify_integrity(
            workflow,
            bundle,
            manifest,
            decrypted_assets=(
                workflow.decrypted_templates() if workflow.encrypted else None
            ),
        )
    except BundleIntegrityError as e:
        return _result(
            GATE_MANIFEST,
            False,
            f"manifest integrity FAILED (bundle modified after sealing): {e}",
        )
    if pinned_content_digest and pinned_content_digest != manifest.content_digest:
        return _result(
            GATE_MANIFEST,
            False,
            "bundle content digest does not match the pinned digest "
            f"(pinned {pinned_content_digest[:16]}..., bundle "
            f"{manifest.content_digest[:16]}...)",
        )
    version = manifest.provenance.compiler_version
    if pinned_compiler_version and pinned_compiler_version != version:
        return _result(
            GATE_MANIFEST,
            False,
            f"bundle compiler version {version!r} does not match the pinned "
            f"version {pinned_compiler_version!r}",
        )
    pin_note = ""
    if pinned_content_digest or pinned_compiler_version:
        pin_note = " (version pin matches)"
    return _result(
        GATE_MANIFEST,
        True,
        f"integrity-sealed manifest re-verified "
        f"(digest {manifest.content_digest[:16]}..., compiler "
        f"{version or 'unstamped'}){pin_note}",
    )
