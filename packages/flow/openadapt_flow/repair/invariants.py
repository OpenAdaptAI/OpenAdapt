"""Contract invariants a repair may never weaken (fail-closed).

A repair changes HOW a workflow locates and performs its steps. It may NEVER
silently weaken WHAT the workflow guarantees. Concretely, comparing the prior
bundle's contract with the proposed bundle's contract, none of these may get
weaker without an explicit new qualification revision:

- **identity**: a step that carried identity evidence (context band,
  structured identity, identity template, identifier crop) must keep every
  tier it had; a step's qualification identity policy must not be removed,
  its quorum lowered, or its signal set reduced.
- **effect**: no declared step effect may disappear; the qualification
  minimum effect tier must not get weaker (a higher tier number is weaker
  evidence); no effect verification policy binding may be removed or its
  tier weakened.
- **risk**: no step may downgrade ``irreversible -> reversible`` or drop
  ``consequential``; no operator risk classification may be removed,
  downgraded, or lose its operator confirmation; no prior step may vanish.
- **environment**: the qualification environment boundary (target kind,
  application, version, digest, required capabilities) must not shrink.
- **policy**: the qualification project itself, its requalification
  conditions, and its exclusions boundary must survive.

Enforcement is FAIL-CLOSED: any detected weakening, and any contract that
cannot be compared, hard-refuses with a clear message. The only path through
is an explicit new qualification revision on the proposed bundle (its
``qualification.revision`` strictly greater than the prior bundle's, chained
by ``previous_revision_sha256``), which is a separately reviewed artifact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from openadapt_flow.qualification import ActionRiskClass
from openadapt_flow.repair.candidate import ContractWeakening

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow


class RepairInvariantError(ValueError):
    """The proposed repair weakens a safety contract; promotion is refused."""


#: Ordering used to detect a risk-classification downgrade. Lower rank is
#: less protective; UNKNOWN ranks lowest so any move INTO unknown from a
#: classified action is a weakening (fail closed).
_RISK_RANK: dict[ActionRiskClass, int] = {
    ActionRiskClass.UNKNOWN: 0,
    ActionRiskClass.READ_ONLY: 1,
    ActionRiskClass.STATE_CHANGING: 2,
    ActionRiskClass.CONSEQUENTIAL: 3,
    ActionRiskClass.IRREVERSIBLE: 4,
}

#: Identity evidence tiers on an anchor, from the step's own contract.
_IDENTITY_TIERS: tuple[str, ...] = (
    "context_text",
    "structured_identity",
    "identity_template",
    "identifier_crop",
)


def _step_identity_tiers(step: "Step") -> set[str]:
    anchor = step.anchor
    if anchor is None:
        return set()
    return {tier for tier in _IDENTITY_TIERS if getattr(anchor, tier, None)}


def _step_effect_hashes(step: "Step") -> list[str]:
    hashes: list[str] = []
    for effect in step.effects:
        try:
            hashes.append(effect.contract_hash())
        except Exception as exc:
            # Fail closed: an effect whose contract cannot be digested cannot
            # be proven preserved.
            raise RepairInvariantError(
                f"step {step.id!r}: effect contract could not be digested for "
                f"comparison: {exc}"
            ) from exc
    return hashes


def _steps_by_id(workflow: "Workflow") -> dict[str, "Step"]:
    return {step.id: step for step in workflow.steps}


def _diff_steps(prior: "Workflow", proposed: "Workflow") -> list[ContractWeakening]:
    weakenings: list[ContractWeakening] = []
    prior_steps = _steps_by_id(prior)
    proposed_steps = _steps_by_id(proposed)

    for step_id, old_step in prior_steps.items():
        new_step = proposed_steps.get(step_id)
        if new_step is None:
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=(
                        f"step {step_id!r} was removed; every prior step's "
                        "contract must survive a repair"
                    ),
                )
            )
            continue

        # identity: every prior evidence tier must survive.
        old_tiers = _step_identity_tiers(old_step)
        new_tiers = _step_identity_tiers(new_step)
        lost = sorted(old_tiers - new_tiers)
        if lost:
            weakenings.append(
                ContractWeakening(
                    dimension="identity",
                    detail=(
                        f"step {step_id!r} lost identity evidence tier(s): "
                        + ", ".join(lost)
                    ),
                )
            )

        # effect: every prior declared effect must survive (by contract hash).
        old_effects = _step_effect_hashes(old_step)
        new_effects = set(_step_effect_hashes(new_step))
        missing = [h for h in old_effects if h not in new_effects]
        if missing:
            weakenings.append(
                ContractWeakening(
                    dimension="effect",
                    detail=(
                        f"step {step_id!r} dropped {len(missing)} declared "
                        "effect contract(s)"
                    ),
                )
            )

        # risk: irreversible and consequential markings must survive.
        if old_step.risk == "irreversible" and new_step.risk != "irreversible":
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=(
                        f"step {step_id!r} downgraded risk "
                        f"{old_step.risk} -> {new_step.risk}"
                    ),
                )
            )
        old_consequential = bool(getattr(old_step, "consequential", False))
        new_consequential = bool(getattr(new_step, "consequential", False))
        if old_consequential and not new_consequential:
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=f"step {step_id!r} dropped its consequential marking",
                )
            )
    return weakenings


def _diff_qualification(
    prior: "Workflow", proposed: "Workflow"
) -> list[ContractWeakening]:
    weakenings: list[ContractWeakening] = []
    old_project = prior.qualification
    new_project = proposed.qualification

    if old_project is None:
        return weakenings
    if new_project is None:
        weakenings.append(
            ContractWeakening(
                dimension="policy",
                detail="the qualification project was removed from the bundle",
            )
        )
        return weakenings

    # environment: the boundary must not shrink or silently change scope.
    old_env = old_project.environment
    new_env = new_project.environment
    for field in (
        "target_kind",
        "application",
        "application_version",
        "environment_digest",
        "runtime_version",
    ):
        if getattr(old_env, field) != getattr(new_env, field):
            weakenings.append(
                ContractWeakening(
                    dimension="environment",
                    detail=(
                        f"environment boundary field {field!r} changed; a "
                        "repair may not change the qualified environment scope"
                    ),
                )
            )
    lost_caps = sorted(
        set(old_env.required_capabilities) - set(new_env.required_capabilities)
    )
    if lost_caps:
        weakenings.append(
            ContractWeakening(
                dimension="environment",
                detail="required capabilities removed: " + ", ".join(lost_caps),
            )
        )

    # effect: the minimum tier must not get weaker (higher number = weaker).
    if int(new_project.minimum_effect_tier) > int(old_project.minimum_effect_tier):
        weakenings.append(
            ContractWeakening(
                dimension="effect",
                detail=(
                    "minimum effect tier weakened: "
                    f"{old_project.minimum_effect_tier.name} -> "
                    f"{new_project.minimum_effect_tier.name}"
                ),
            )
        )

    # effect: no verification policy binding removed or weakened.
    new_bindings = {
        (binding.step_id, binding.effect_index): binding
        for binding in new_project.effect_policies
    }
    for binding in old_project.effect_policies:
        replacement = new_bindings.get((binding.step_id, binding.effect_index))
        if replacement is None:
            weakenings.append(
                ContractWeakening(
                    dimension="effect",
                    detail=(
                        "effect verification policy removed for step "
                        f"{binding.step_id!r} effect {binding.effect_index}"
                    ),
                )
            )
        elif int(replacement.tier) > int(binding.tier):
            weakenings.append(
                ContractWeakening(
                    dimension="effect",
                    detail=(
                        "effect verification tier weakened for step "
                        f"{binding.step_id!r} effect {binding.effect_index}: "
                        f"{binding.tier.name} -> {replacement.tier.name}"
                    ),
                )
            )

    # identity: no per-step policy removed, quorum lowered, or signals reduced.
    for step_id, old_policy in old_project.identity_policies.items():
        new_policy = new_project.identity_policies.get(step_id)
        if new_policy is None:
            weakenings.append(
                ContractWeakening(
                    dimension="identity",
                    detail=f"identity policy removed for step {step_id!r}",
                )
            )
            continue
        if new_policy.enforcement != old_policy.enforcement:
            weakenings.append(
                ContractWeakening(
                    dimension="identity",
                    detail=(
                        f"identity enforcement changed for step {step_id!r}: "
                        f"{old_policy.enforcement.value} -> "
                        f"{new_policy.enforcement.value}"
                    ),
                )
            )
        if new_policy.quorum < old_policy.quorum:
            weakenings.append(
                ContractWeakening(
                    dimension="identity",
                    detail=(
                        f"identity quorum lowered for step {step_id!r}: "
                        f"{old_policy.quorum} -> {new_policy.quorum}"
                    ),
                )
            )
        old_signals = {signal.key for signal in old_policy.signals}
        new_signals = {signal.key for signal in new_policy.signals}
        lost_signals = sorted(key.value for key in old_signals - new_signals)
        if lost_signals:
            weakenings.append(
                ContractWeakening(
                    dimension="identity",
                    detail=(
                        f"identity signals removed for step {step_id!r}: "
                        + ", ".join(lost_signals)
                    ),
                )
            )

    # risk: no operator classification removed, downgraded, or de-confirmed.
    for step_id, old_class in old_project.action_classifications.items():
        new_class = new_project.action_classifications.get(step_id)
        if new_class is None:
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=f"risk classification removed for step {step_id!r}",
                )
            )
            continue
        if _RISK_RANK[new_class.classification] < _RISK_RANK[old_class.classification]:
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=(
                        f"risk classification downgraded for step {step_id!r}: "
                        f"{old_class.classification.value} -> "
                        f"{new_class.classification.value}"
                    ),
                )
            )
        if old_class.operator_confirmed and not new_class.operator_confirmed:
            weakenings.append(
                ContractWeakening(
                    dimension="risk",
                    detail=(
                        "operator confirmation dropped from the risk "
                        f"classification of step {step_id!r}"
                    ),
                )
            )

    # policy: requalification conditions must survive; exclusions cannot grow.
    old_conditions = {
        (condition.kind, condition.description)
        for condition in old_project.requalification_conditions
    }
    new_conditions = {
        (condition.kind, condition.description)
        for condition in new_project.requalification_conditions
    }
    lost_conditions = sorted(kind for kind, _ in old_conditions - new_conditions)
    if lost_conditions:
        weakenings.append(
            ContractWeakening(
                dimension="policy",
                detail=(
                    "requalification condition(s) removed: "
                    + ", ".join(lost_conditions)
                ),
            )
        )
    grown_exclusions = sorted(set(new_project.exclusions) - set(old_project.exclusions))
    if grown_exclusions:
        weakenings.append(
            ContractWeakening(
                dimension="policy",
                detail=(
                    "qualification exclusions grew: " + ", ".join(grown_exclusions)
                ),
            )
        )
    return weakenings


def check_contract_invariants(
    prior: "Workflow", proposed: "Workflow"
) -> list[ContractWeakening]:
    """Diff the two bundles' safety contracts; return every weakening found."""
    return _diff_steps(prior, proposed) + _diff_qualification(prior, proposed)


def _has_explicit_new_revision(
    prior: "Workflow", proposed: "Workflow"
) -> tuple[bool, Optional[str]]:
    """Whether the proposed bundle carries an explicit NEW qualification
    revision that supersedes the prior bundle's (revision strictly advanced
    and chained). Returns ``(allowed, reason_if_not)``."""
    old_project = prior.qualification
    new_project = proposed.qualification
    if old_project is None or new_project is None:
        return False, (
            "no qualification project is present to carry a reviewed revision"
        )
    if new_project.revision <= old_project.revision:
        return False, (
            f"proposed qualification revision {new_project.revision} does not "
            f"supersede prior revision {old_project.revision}"
        )
    if new_project.previous_revision_sha256 is None:
        return False, (
            "proposed qualification revision is not chained to a previous "
            "revision digest"
        )
    return True, None


def enforce_contract_invariants(
    prior: "Workflow", proposed: "Workflow"
) -> list[ContractWeakening]:
    """Hard-refuse (fail closed) any contract weakening without a new revision.

    Returns the (possibly empty) list of weakenings when the repair is
    admissible: either no contract field weakened, or every weakening is
    covered by an explicit new qualification revision on the proposed bundle.

    Raises:
        RepairInvariantError: The proposed repair weakens the identity,
            effect, risk, environment, or policy contract and the proposed
            bundle does not carry an explicit new qualification revision.
    """
    try:
        weakenings = check_contract_invariants(prior, proposed)
    except RepairInvariantError:
        raise
    except Exception as exc:
        # Fail closed: an incomparable contract is a refusal, not a pass.
        raise RepairInvariantError(
            f"contract comparison failed; refusing the repair (fail closed): {exc}"
        ) from exc
    if not weakenings:
        return []
    allowed, reason = _has_explicit_new_revision(prior, proposed)
    if allowed:
        return weakenings
    details = "; ".join(
        f"[{weakening.dimension}] {weakening.detail}" for weakening in weakenings
    )
    raise RepairInvariantError(
        "repair refused (fail closed): the proposed bundle weakens the safety "
        f"contract without an explicit new qualification revision: {details}. "
        f"({reason}.) A repair may change how a step is performed, never "
        "silently weaken identity, effect, risk, environment, or policy "
        "requirements; record and review a new qualification revision first."
    )
