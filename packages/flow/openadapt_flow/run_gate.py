"""Fail-closed admission gate for regulated execution (``openadapt-flow run``).

``replay`` is the permissive DEMO path: it will drive a bundle against an app
with every safety control (certification, identity arming, effect verification,
encryption) left OPTIONAL, because a demo's job is to *show the mechanism*, not
to be safe. The external safety review scored that default posture 4/10 for
exactly this reason: nothing forces the controls on.

``run`` is the REGULATED path, and this module is its admission gate. It is a
PURE function of a loaded bundle plus its deployment wiring: it executes nothing
and mutates nothing. It answers one question -- *may this bundle be executed
unattended in this deployment?* -- and it FAILS CLOSED, refusing (with a
structured reason naming the failing gate) unless ALL of the following hold:

1. **Certification** -- the bundle passes a required safety policy (default
   ``clinical-write``, or ``--policy``). An uncertified bundle is refused.
2. **Identity coverage** -- every entity-sensitive / consequential action is
   IDENTITY-ARMED. An unarmed consequential action (one that would act with no
   verified target identity) refuses the run; it is never silently proceeded.
3. **Effect coverage** -- every consequential write DECLARES a system-of-record
   effect contract (and none is an unconfirmed / fabricated binding). A write
   that would be verified by the SCREEN only -- because it declares no
   system-of-record effect -- refuses the run.
4. **Approval fallback** -- every declared write effect must be independently
   verifiable in THIS deployment (a verifier configured for its substrate).
   Where independent verification is impossible (no verifier wired), the write
   is admitted ONLY under EXPLICIT operator approval; absent approval, the run
   halts.
5. **Encryption at rest** -- the bundle's ``workflow.json`` and template crops
   are sealed with AES-256-GCM. A plaintext bundle is refused. Any additional
   plaintext template / screenshot asset is a loud WARNING by default and a
   REFUSAL under ``strict_templates``.
6. **Sealed manifest + version pin** -- the bundle carries an integrity-sealed
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
from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow import crypto
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import Step, Workflow
from openadapt_flow.policy import (
    has_screen_postcondition,
    has_system_effect,
    has_unconfirmed_effect_binding,
    is_identity_applicable,
    is_identity_armed,
    step_tags,
)
from openadapt_flow.risk import classify_step_risk
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    UnverifiedWriteApproval,
    runtime_inputs_digest,
)
from openadapt_flow.traversal import iter_workflow_steps

#: Default certifying policy for a regulated run when none is configured.
DEFAULT_POLICY: str = "clinical-write"

# Gate identifiers (stable strings a caller / test can assert on).
GATE_CERTIFICATION = "certification"
GATE_IDENTITY = "identity_coverage"
GATE_EFFECT = "effect_coverage"
GATE_APPROVAL = "approval_fallback"
GATE_ENCRYPTION = "encryption"
GATE_MANIFEST = "manifest_integrity"

#: The gates, in the order the report renders them.
GATE_ORDER = (
    GATE_CERTIFICATION,
    GATE_IDENTITY,
    GATE_EFFECT,
    GATE_APPROVAL,
    GATE_ENCRYPTION,
    GATE_MANIFEST,
)

_GATE_TITLES = {
    GATE_CERTIFICATION: "Certification passed",
    GATE_IDENTITY: "Identity coverage",
    GATE_EFFECT: "Effect coverage",
    GATE_APPROVAL: "Approval fallback",
    GATE_ENCRYPTION: "Encrypted bundle",
    GATE_MANIFEST: "Sealed manifest + version pin",
}


# ---------------------------------------------------------------------------
# Consequential-action classification (fail-closed: err toward "consequential")
# ---------------------------------------------------------------------------


def is_consequential(step: Step) -> bool:
    """Whether ``step`` commits a consequential (irreversible) write.

    Fail-closed union of every signal the codebase already carries: the
    compiled ``risk`` label, the write-shaped heuristic
    (:func:`openadapt_flow.risk.classify_step_risk`), and the presence of a
    declared system-of-record effect. A step any of these flag is treated as a
    write for coverage purposes.
    """
    return (
        step.risk == "irreversible"
        or classify_step_risk(step) == "irreversible"
        or has_system_effect(step)
    )


def must_be_identity_armed(step: Step) -> bool:
    """Whether the pre-click identity check MUST be armed on ``step``.

    The entity-sensitive / consequential surface: an identity-applicable step
    (anchored click / double-click / TYPE) that either commits a write or lands
    on a specific on-screen entity (the wrong-entity surface). These are the
    steps that must never act without a verified target identity.
    """
    if not is_identity_applicable(step):
        return False
    return is_consequential(step) or "entity_navigation" in step_tags(step)


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
    gates: list[GateResult] = Field(default_factory=list)
    bundle_content_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")
    required_identity_step_ids: list[str] = Field(default_factory=list)
    effect_verifier_configured: bool = False
    api_actuator_configured: bool = False
    unverified_write_approval_granted: bool = False

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
            f"workflow {self.workflow_name!r} vs policy {self.policy_name!r}"
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
    policy_source: Optional[str] = None,
    approval_available: bool = False,
    strict_templates: bool = False,
    require_encryption: bool = True,
    pinned_content_digest: Optional[str] = None,
    pinned_compiler_version: Optional[str] = None,
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
        policy_source: Certifying policy name / path. Defaults to the
            deployment's ``policy.policy``, then to :data:`DEFAULT_POLICY`.
        approval_available: The operator has EXPLICITLY approved executing writes
            whose effects cannot be independently verified in this deployment
            (gate 4 fallback). Default False (fail closed).
        strict_templates: Treat any genuinely unsealed template / screenshot
            asset as a REFUSAL rather than a warning (gate 5). Ciphertexts
            produced by ``Workflow.save(encrypt=True)`` satisfy this gate.
        require_encryption: Require the bundle be AES-GCM encrypted at rest
            (gate 5). Default True (fail closed).
        pinned_content_digest / pinned_compiler_version: Optional version pins
            (gate 6); a supplied pin that does not match the sealed manifest
            refuses the run.
    """
    bundle = Path(bundle_dir)
    policy_name = policy_source or deployment.policy.policy or DEFAULT_POLICY
    steps = list(iter_workflow_steps(workflow))

    approval_gate = _gate_approval(
        steps, effect_verifier, api_actuator, approval_available
    )
    gates = [
        _gate_certification(workflow, policy_name),
        _gate_identity(steps),
        _gate_effect(steps),
        approval_gate,
        _gate_encryption(workflow, bundle, require_encryption, strict_templates),
        _gate_manifest(
            workflow, bundle, pinned_content_digest, pinned_compiler_version
        ),
    ]
    return RunGateReport(
        workflow_name=workflow.name,
        policy_name=policy_name,
        gates=gates,
        bundle_content_digest=(
            workflow.manifest.content_digest if workflow.manifest is not None else None
        ),
        required_identity_step_ids=[
            step.id for step in steps if must_be_identity_armed(step)
        ],
        effect_verifier_configured=effect_verifier is not None,
        api_actuator_configured=api_actuator is not None,
        unverified_write_approval_granted=(
            effect_verifier is None
            and approval_available
            and approval_gate.passed
            and any(
                is_consequential(step) and has_system_effect(step) for step in steps
            )
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


def _gate_certification(workflow: Workflow, policy_name: str) -> GateResult:
    """Gate 1: the bundle must PASS the required certifying policy."""
    from openadapt_flow.policy import evaluate_policy, load_policy

    try:
        policy = load_policy(policy_name)
    except (FileNotFoundError, ValueError) as e:
        return _result(
            GATE_CERTIFICATION,
            False,
            f"certifying policy {policy_name!r} could not be loaded: {e}",
        )
    report = evaluate_policy(workflow, policy)
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


def _gate_identity(steps: list[Step]) -> GateResult:
    """Gate 2: every entity-sensitive / consequential action is identity-armed."""
    must_arm = [s for s in steps if must_be_identity_armed(s)]
    unarmed = [s for s in must_arm if not is_identity_armed(s)]
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


def _gate_effect(steps: list[Step]) -> GateResult:
    """Gate 3: every consequential write DECLARES a (confirmed) effect contract.

    A write with no declared system-of-record effect would be verified by the
    SCREEN only; a write whose effect binding was not derivable from the demo
    (``needs_operator_confirmation``) carries a fabricated/unconfirmed contract.
    Both are bundle-level defects and refuse here (they cannot be waived by
    deployment approval -- gate 4 only covers a verifier that is missing).
    """
    writes = [s for s in steps if is_consequential(s)]
    screen_only = [s for s in writes if not has_system_effect(s)]
    unconfirmed = [s for s in writes if has_unconfirmed_effect_binding(s)]
    offenders = sorted({s.id for s in screen_only} | {s.id for s in unconfirmed})
    total = len(writes)
    if not offenders:
        return _result(
            GATE_EFFECT,
            True,
            f"{total}/{total} consequential write(s) declare a confirmed "
            "system-of-record effect contract",
        )
    parts = []
    if screen_only:
        parts.append(
            f"{len(screen_only)} would be verified by SCREEN only "
            "(no declared system-of-record effect)"
        )
    if unconfirmed:
        parts.append(
            f"{len(unconfirmed)} carry an UNCONFIRMED effect binding "
            "(not derivable from the demonstration)"
        )
    return _result(
        GATE_EFFECT,
        False,
        f"{len(offenders)}/{total} consequential write(s) lack an adequate "
        "effect contract: " + "; ".join(parts),
        offenders,
    )


def _gate_approval(
    steps: list[Step],
    effect_verifier: object | None,
    api_actuator: object | None,
    approval_available: bool,
) -> GateResult:
    """Gate 4: writes with no configured verifier need explicit approval.

    A consequential write that DECLARES an effect but has NO verifier wired for
    this deployment cannot be independently verified (its effect would go
    unchecked). It is admitted ONLY under explicit operator approval; otherwise
    the run halts. Writes that DO have a verifier need nothing here.
    """
    writes = [s for s in steps if is_consequential(s) and has_system_effect(s)]
    if effect_verifier is not None:
        return _result(
            GATE_APPROVAL,
            True,
            f"a system-of-record verifier is configured; "
            f"{len(writes)} declared write(s) are independently verified",
        )
    # No verifier: every declared write is unverifiable in this deployment.
    if not writes:
        return _result(
            GATE_APPROVAL,
            True,
            "no consequential writes require independent verification",
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
    if approval_available:
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
    return _result(
        GATE_APPROVAL,
        False,
        f"{len(writes)} consequential write(s) cannot be independently verified "
        "(no verifier configured for this deployment) and no explicit approval "
        "was provided -- halting",
        [s.id for s in writes],
    )


def build_runtime_authorization(
    workflow: Workflow,
    report: RunGateReport,
    *,
    approval_source: str = "local-cli-explicit-flag",
    params: Optional[dict[str, str]] = None,
    worklists: Optional[dict[str, list[dict[str, str]]]] = None,
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

    from openadapt_flow.bundle_validation import compute_content_digest

    recomputed = compute_content_digest(workflow, workflow.manifest.file_hashes)
    if recomputed != report.bundle_content_digest:
        raise ValueError("workflow changed after the run gate evaluated it")

    steps = list(iter_workflow_steps(workflow))
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
            if is_consequential(step) and has_system_effect(step)
        ]

    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, params, worklists),
        admitted_policy_name=report.policy_name,
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
    """Gate 5: the bundle is AES-GCM encrypted at rest (+ template coverage)."""
    if require_encryption and not workflow.encrypted:
        return _result(
            GATE_ENCRYPTION,
            False,
            "bundle workflow.json is NOT encrypted at rest (AES-256-GCM). "
            "Re-save with save(encrypt=True) / a configured OPENADAPT_BUNDLE_KEY",
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
    """Gate 6: sealed integrity manifest re-verifies + version pins match."""
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
