"""Named execution profiles over OpenAdapt's existing governed runtime.

Profiles do not implement a second policy or replay path.  They select which
requirements the existing run gate must enforce, whether the shared replayer
must be durable, and how the resulting report may be described.

The low-level controls remain available for embedding and backwards
compatibility.  Production callers can choose one reviewed profile instead of
assembling a potentially contradictory collection of permissive flags.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Mapping
from urllib.parse import urlsplit

from openadapt_flow.action_evidence import (
    AUTOMATED_GUI_ACTUATIONS,
    HUMAN_ATTENDED_ACTUATIONS,
    action_evidence_error,
)
from openadapt_flow.decision_delivery import DecisionDeliveryTier
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:
    from openadapt_flow.ir import ExecutionOutcomeEnvelope, RunReport, Workflow


class ExecutionProfile(str, Enum):
    """The supported runtime postures."""

    DEMO = "demo"
    STANDARD = "standard"
    REGULATED = "regulated"


class ExecutionOutcome(str, Enum):
    """Precise result of applying a profile's evidence contract."""

    VERIFIED = "VERIFIED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    HALTED = "HALTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class ExecutionProfileContract:
    """Requirements a named profile applies to the existing runtime."""

    profile: ExecutionProfile
    production: bool
    require_certification: bool
    require_identity_coverage: bool
    require_consequential_postconditions: bool
    require_effect_contracts: bool
    minimum_effect_tier: VerificationTier | None
    require_approval_for_unverified_effects: bool
    allow_unverified_write_approval: bool
    require_encryption: bool
    strict_templates: bool
    require_durable: bool
    require_settled: bool
    default_policy: str | None
    #: The most context this profile permits a REMOTE attended-decision surface
    #: to carry. Local delivery is unaffected: the loopback console and the
    #: runner-local portal always serve
    #: :attr:`~openadapt_flow.decision_delivery.DecisionDeliveryTier.LOCAL_FULL`.
    #: Every profile currently permits ``REMOTE_CLOSED_CONTEXT`` because that
    #: tier adds only closed enums, bounded integers, and booleans — it widens
    #: what a remote operator KNOWS without widening what the envelope can
    #: REPRESENT. A future scrubbed tier would not have that property, and this
    #: field is where ``regulated`` refuses it.
    max_remote_decision_tier: DecisionDeliveryTier


_CONTRACTS = {
    ExecutionProfile.DEMO: ExecutionProfileContract(
        profile=ExecutionProfile.DEMO,
        production=False,
        require_certification=False,
        require_identity_coverage=False,
        require_consequential_postconditions=False,
        require_effect_contracts=False,
        minimum_effect_tier=None,
        require_approval_for_unverified_effects=True,
        allow_unverified_write_approval=True,
        require_encryption=False,
        strict_templates=False,
        require_durable=False,
        require_settled=False,
        default_policy=None,
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
    ExecutionProfile.STANDARD: ExecutionProfileContract(
        profile=ExecutionProfile.STANDARD,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_consequential_postconditions=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=False,
        strict_templates=False,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
    ExecutionProfile.REGULATED: ExecutionProfileContract(
        profile=ExecutionProfile.REGULATED,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_consequential_postconditions=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=True,
        strict_templates=True,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
}


def resolve_execution_profile(
    value: ExecutionProfile | str | None,
    *,
    default: ExecutionProfile = ExecutionProfile.REGULATED,
) -> ExecutionProfile:
    """Resolve a profile name or fail loudly on an unknown value."""

    if value is None:
        return default
    if isinstance(value, ExecutionProfile):
        return value
    try:
        return ExecutionProfile(str(value).strip().lower())
    except ValueError as exc:
        choices = ", ".join(profile.value for profile in ExecutionProfile)
        raise ValueError(
            f"unknown execution profile {value!r}; expected one of: {choices}"
        ) from exc


def execution_profile_contract(
    value: ExecutionProfile | str,
) -> ExecutionProfileContract:
    """Return the immutable contract for ``value``."""

    return _CONTRACTS[resolve_execution_profile(value)]


def required_effect_tier(
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> VerificationTier | None:
    """Return the strongest profile/project minimum for this workflow."""

    contract = execution_profile_contract(profile)
    required = contract.minimum_effect_tier
    if not contract.production:
        return required
    project_minimum = getattr(workflow.qualification, "minimum_effect_tier", None)
    if project_minimum is not None:
        candidate = VerificationTier(project_minimum)
        if required is None or int(candidate) < int(required):
            required = candidate
    return required


def qualified_effect_requirements(
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> tuple[Any, ...]:
    """Return exact per-effect strength requirements from the qualified project.

    The global profile/project minimum remains the fallback for workflows that
    do not carry a qualification project.  Once a project assigns a stronger
    tier to one effect, that exact step/path/index contract becomes the
    production requirement instead of being weakened back to the global floor.
    """

    resolved = resolve_execution_profile(profile)
    if not execution_profile_contract(resolved).production:
        return ()
    project = workflow.qualification
    if project is None:
        return ()

    from openadapt_flow.ir import QualifiedEffectRequirement
    from openadapt_flow.policy import iter_effect_paths
    from openadapt_flow.qualification import qualification_action_requirements
    from openadapt_flow.traversal import iter_workflow_steps

    steps = {step.id: step for step in iter_workflow_steps(workflow)}
    required_actions, _required_identity = qualification_action_requirements(workflow)
    expected_keys: set[tuple[str, str, int]] = set()
    for step_id in required_actions:
        step = steps[step_id]
        for actuation_path, effects in iter_effect_paths(step):
            if not effects:
                raise ValueError(
                    "qualified action path has no effect contract to assign a tier"
                )
            expected_keys.update(
                (step.id, actuation_path, effect_index)
                for effect_index in range(len(effects))
            )
    actual_keys = {
        (binding.step_id, binding.actuation_path, binding.effect_index)
        for binding in project.effect_policies
    }
    if actual_keys != expected_keys:
        raise ValueError(
            "qualification project must assign an exact tier to every effect"
        )
    global_minimum = required_effect_tier(workflow, resolved)
    requirements: list[QualifiedEffectRequirement] = []
    for binding in sorted(
        project.effect_policies,
        key=lambda item: (item.step_id, item.actuation_path, item.effect_index),
    ):
        bound_step = steps.get(binding.step_id)
        if bound_step is None:
            raise ValueError("qualified effect requirement references an unknown step")
        paths = dict(iter_effect_paths(bound_step))
        bound_effects = paths.get(binding.actuation_path)
        if bound_effects is None or binding.effect_index >= len(bound_effects):
            raise ValueError(
                "qualified effect requirement references a missing actuation effect"
            )
        effect = bound_effects[binding.effect_index]
        if effect.contract_hash() != binding.effect_contract_hash:
            raise ValueError("qualified effect contract changed after qualification")
        required = VerificationTier(binding.tier)
        if global_minimum is not None and int(global_minimum) < int(required):
            required = global_minimum
        requirements.append(
            QualifiedEffectRequirement(
                step_id=binding.step_id,
                actuation_path=binding.actuation_path,
                effect_index=binding.effect_index,
                effect_contract_hash=binding.effect_contract_hash,
                minimum_tier=int(required),
            )
        )
    return tuple(requirements)


def _api_identity_evidence_is_exact(
    *,
    workflow: Workflow,
    step: Any,
    check: Any,
    scoped_params: Mapping[str, str],
    effects: list[Any],
) -> bool:
    """Return whether an API result matches its exact identity binding.

    API execution does not have a GUI identity observation.  Its identity
    proof is the qualified parameter binding shared by the outgoing request
    and the independently verified effect.  A bare ``status=verified`` report
    is therefore not sufficient.
    """

    binding = step.api_binding
    if binding is None or not binding.identity or check is None:
        return False
    project = workflow.qualification
    policy = project.identity_policies.get(step.id) if project is not None else None
    if policy is not None:
        from openadapt_flow.qualification_identity_evidence import (
            qualification_identity_evidence_error,
        )

        if (
            qualification_identity_evidence_error(
                policy=policy,
                check=check,
                step=step,
                actuation_path="api",
                runtime_params=scoped_params,
                recorded_params=workflow.params,
            )
            is not None
        ):
            return False
        required = policy.quorum
    else:
        # Legacy certified workflows without a QualificationProject use the
        # same one-signal minimum that the API runtime enforces.
        required = 1

    expected_signals = [item.key for item in binding.identity]
    actual_signals = [item.signal for item in check.signal_evidence]
    if actual_signals != expected_signals:
        return False
    if any(
        item.source != "api_parameter"
        or item.verdict != "verified"
        or item.evidence_class != "api_request_effect_binding"
        or item.match != "exact"
        for item in check.signal_evidence
    ):
        return False
    if (
        check.status != "verified"
        or check.mode != "signal_quorum"
        or check.coverage != 1.0
        or check.quorum_required != required
        or check.quorum_verified != len(binding.identity)
        or check.expected
        or check.observed
        or check.param is not None
    ):
        return False

    for identity in binding.identity:
        if not scoped_params.get(identity.param):
            return False
        effect_path = tuple(identity.effect_field.split("."))
        if not any(
            tuple(field.split(".")) == effect_path
            and expression.param == identity.param
            for effect in effects
            for field, expression in effect.match.items()
        ):
            return False
        token = "{" + identity.param + "}"
        for pointer in identity.request_pointers:
            pointer_parts = [
                item.replace("~1", "/").replace("~0", "~")
                for item in pointer.lstrip("/").split("/")
            ]
            if tuple(pointer_parts[1:]) != effect_path:
                return False
            if pointer_parts[0] == "url":
                path_segments = [
                    item
                    for item in urlsplit(binding.url_template).path.split("/")
                    if item
                ]
                if token not in path_segments:
                    return False
                continue
            value: Any = (
                binding.body_template if pointer_parts[0] == "body" else binding.query
            )
            for part in pointer_parts[1:]:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                elif isinstance(value, list) and part.isdigit():
                    index = int(part)
                    if index >= len(value):
                        return False
                    value = value[index]
                else:
                    return False
            if value != token:
                return False
    return True


@dataclass(frozen=True)
class _ProgramActionOccurrence:
    """One structurally proven action occurrence in a program trace."""

    state_id: str
    step: Any
    program_scope: tuple[Any, ...]
    exception_edge: bool = False
    attended_action: str | None = None


def _program_action_trace(
    workflow: Workflow,
    visited_states: list[str],
    *,
    runtime_params: Mapping[str, str] | None = None,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
    transition_evidence: list[Any] | None = None,
    exception_evidence: list[Any] | None = None,
    attended_transition_evidence: list[Any] | None = None,
    business_decision_evidence: list[Any] | None = None,
    transition_evidence_root: Path | None = None,
    transition_predicate_vision: Any | None = None,
    governed_runtime_inputs_digest: str | None = None,
    run_id_sha256: str | None = None,
    workflow_contract_digest: str | None = None,
    halted_at_step_id: str | None = None,
    reported_results: list[Any] | None = None,
) -> list[_ProgramActionOccurrence] | None:
    """Validate one complete graph walk and return its action occurrences.

    This proves entry, declared transitions, nested subflow returns, exact loop
    iteration counts, and a successful top-level terminal or declared fall-off.
    State and step identifiers must be globally unique so no weaker action
    contract can replace another one during report classification.
    """

    if not visited_states or workflow.program is None:
        return None
    from openadapt_flow.ir import (
        PredicateKind,
        ProgramExecutionScopeFrame,
        StateKind,
        predicate_contract_sha256,
    )
    from openadapt_flow.runtime.program_predicates import (
        evaluate_program_predicate,
        exact_png_size,
        predicate_template_refs,
        program_predicate_evaluator_contract_sha256,
    )

    if transition_predicate_vision is None:
        from openadapt_flow import vision as default_transition_predicate_vision

        transition_predicate_vision = default_transition_predicate_vision

    graphs = {"__program__": workflow.program, **workflow.subflows}
    state_owner: dict[str, str] = {}
    action_step_ids: set[str] = set()
    for graph_id, graph in graphs.items():
        for key, state in graph.states.items():
            if key != state.id or state.id in state_owner:
                return None
            state_owner[state.id] = graph_id
            if state.kind is StateKind.ACTION:
                if state.step is None or state.step.id in action_step_ids:
                    return None
                action_step_ids.add(state.step.id)

    cursor = 0
    evidence_cursor = 0
    exception_evidence_cursor = 0
    attended_evidence_cursor = 0
    business_evidence_cursor = 0
    expected_evidence_decision_index = 0
    actions: list[_ProgramActionOccurrence] = []
    halted_at_requested_action = False
    evaluator_contract_sha256: str | None = None
    trace_base_params = dict(runtime_params or {})
    if business_decision_evidence:
        if transition_evidence_root is None:
            return None
        from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

        manifest = CheckpointStore(transition_evidence_root).read_manifest()
        if manifest is None:
            return None
        trace_base_params = dict(manifest.params)

    class _RequestedActionHalt(Exception):
        """The retained trace ended exactly at the requested action."""

    def _rows(relation: str) -> list[dict[str, str]] | None:
        if runtime_worklists is not None and relation in runtime_worklists:
            return list(runtime_worklists[relation])
        declared = workflow.data_sources.get(relation)
        return None if declared is None else list(declared.rows)

    def _reported_guard_value(
        predicate: Any, current_params: Mapping[str, str]
    ) -> bool | None:
        """Recompute guards whose inputs are retained in the run report.

        Screen predicates depend on the exact frame observed by the runtime.
        ``visited_states`` does not retain that evidence, so those predicates
        stay unknown and the report fails closed instead of claiming an exact
        graph path. Parameter predicates and their boolean compositions are
        reproducible from ``RunReport.params``.
        """

        kind = predicate.kind
        if kind is PredicateKind.PARAM_EQUALS:
            return predicate.param is not None and str(
                current_params.get(predicate.param)
            ) == str(predicate.value)
        if kind is PredicateKind.AND:
            values = [
                _reported_guard_value(item, current_params)
                for item in predicate.operands
            ]
            if any(value is False for value in values):
                return False
            return True if all(value is True for value in values) else None
        if kind is PredicateKind.OR:
            values = [
                _reported_guard_value(item, current_params)
                for item in predicate.operands
            ]
            if any(value is True for value in values):
                return True
            return False if all(value is False for value in values) else None
        if kind is PredicateKind.NOT:
            if not predicate.operands:
                return False
            value = _reported_guard_value(predicate.operands[0], current_params)
            return None if value is None else not value
        return None

    def _guard_uses_frame(predicate: Any) -> bool:
        if predicate is None:
            return False
        if predicate.kind in {
            PredicateKind.ANCHOR_RESOLVES,
            PredicateKind.TEXT_PRESENT,
            PredicateKind.TEXT_ABSENT,
        }:
            return True
        return any(_guard_uses_frame(item) for item in predicate.operands)

    def _matching_evidence_group(
        *, graph_id: str, state: Any, scope: tuple[Any, ...]
    ) -> list[Any] | None:
        nonlocal evidence_cursor, expected_evidence_decision_index
        evidence = transition_evidence or []
        if evidence_cursor >= len(evidence):
            raise ValueError("program transition evidence omits a decision")
        first = evidence[evidence_cursor]
        if (
            first.graph_id != graph_id
            or first.state_id != state.id
            or tuple(first.program_scope) != scope
        ):
            raise ValueError("program transition evidence omits a decision")
        if first.decision_index != expected_evidence_decision_index:
            raise ValueError("program transition evidence sequence is not contiguous")
        end = evidence_cursor
        while (
            end < len(evidence) and evidence[end].decision_index == first.decision_index
        ):
            end += 1
        group = evidence[evidence_cursor:end]
        evidence_cursor = end
        expected_evidence_decision_index += 1
        if any(
            item.graph_id != graph_id
            or item.state_id != state.id
            or tuple(item.program_scope) != scope
            or item.decision_index != first.decision_index
            for item in group
        ):
            raise ValueError("transition evidence decision rows disagree")
        return group

    def _verified_inventory_bytes(ref: str, expected_sha256: str) -> bytes:
        if transition_evidence_root is None:
            raise ValueError("visual transition evidence has no local evidence root")
        root = transition_evidence_root.resolve()
        parts = PurePosixPath(ref).parts
        candidate = root.joinpath(*parts)
        cursor_path = root
        for part in parts:
            cursor_path /= part
            if cursor_path.is_symlink():
                raise ValueError("transition frame path contains a symlink")
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("transition frame evidence is missing") from exc
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("transition frame escapes the evidence root") from exc
        if candidate.is_symlink() or not path.is_file():
            raise ValueError("transition frame evidence is missing")
        import hashlib

        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError("transition frame evidence is unreadable") from exc
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("transition evidence digest does not match")
        return payload

    def _validated_evidence_target(
        *,
        graph_id: str,
        state: Any,
        scope: tuple[Any, ...],
        current_params: Mapping[str, str],
    ) -> str | None:
        nonlocal evaluator_contract_sha256
        group = _matching_evidence_group(
            graph_id=graph_id,
            state=state,
            scope=scope,
        )
        if group is None:
            return None
        indexes = [item.transition_index for item in group]
        if indexes != list(range(len(group))) or len(group) > len(state.transitions):
            raise ValueError("transition evidence does not follow declaration order")
        selected = [item for item in group if item.selected]
        if len(selected) > 1 or (selected and selected[0] is not group[-1]):
            raise ValueError("transition evidence has an invalid selected guard")
        if not selected and len(group) != len(state.transitions):
            raise ValueError("unmatched transition evidence is incomplete")
        target = selected[0].selected_target if selected else None
        if any(item.selected_target != target for item in group):
            raise ValueError("transition evidence rows disagree on the selected target")
        for item, transition in zip(group, state.transitions):
            uses_frame = _guard_uses_frame(transition.guard)
            expected_kind = (
                "unconditional"
                if transition.guard is None
                else "frame"
                if uses_frame
                else "parameters"
            )
            if (
                item.selected != item.guard_verdict
                or item.guard_contract_sha256
                != predicate_contract_sha256(transition.guard)
                or item.guard_evidence_kind != expected_kind
                or item.governed_runtime_inputs_digest != governed_runtime_inputs_digest
            ):
                raise ValueError("transition evidence contract binding differs")
            recomputed = (
                True
                if transition.guard is None
                else _reported_guard_value(transition.guard, current_params)
            )
            if recomputed is not None and item.guard_verdict != recomputed:
                raise ValueError("transition evidence guard verdict differs")
            if uses_frame:
                if evaluator_contract_sha256 is None:
                    evaluator_contract_sha256 = (
                        program_predicate_evaluator_contract_sha256(
                            transition_predicate_vision
                        )
                    )
                if (
                    item.guard_evaluator_contract_sha256 != evaluator_contract_sha256
                    or item.observed_frame_inventory_ref is None
                    or item.observed_frame_sha256 is None
                    or item.observed_viewport is None
                    or item.observed_context_inventory_ref is None
                    or item.observed_context_sha256 is None
                ):
                    raise ValueError("transition visual evaluator contract differs")
                frame = _verified_inventory_bytes(
                    item.observed_frame_inventory_ref,
                    item.observed_frame_sha256,
                )
                try:
                    observed_size = exact_png_size(frame)
                except Exception as exc:
                    raise ValueError(
                        "transition frame is not an exact PNG observation"
                    ) from exc
                context_payload = _verified_inventory_bytes(
                    item.observed_context_inventory_ref,
                    item.observed_context_sha256,
                )
                try:
                    observed_context = json.loads(context_payload)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "transition observation context is invalid"
                    ) from exc
                if observed_context != {
                    "schema_version": 1,
                    "frame_sha256": item.observed_frame_sha256,
                    "frame_size": list(observed_size),
                    "viewport": list(item.observed_viewport),
                    "evaluator_contract_sha256": (item.guard_evaluator_contract_sha256),
                }:
                    raise ValueError(
                        "transition viewport differs from its exact observation"
                    )
                expected_refs = predicate_template_refs(transition.guard)
                if (
                    tuple(asset.source_ref for asset in item.guard_assets)
                    != expected_refs
                ):
                    raise ValueError("transition guard asset inventory differs")
                retained_assets = {
                    asset.source_ref: _verified_inventory_bytes(
                        asset.inventory_ref,
                        asset.sha256,
                    )
                    for asset in item.guard_assets
                }
                assert transition.guard is not None
                recomputed_visual = evaluate_program_predicate(
                    transition.guard,
                    frame,
                    current_params,
                    vision=transition_predicate_vision,
                    viewport=item.observed_viewport,
                    asset_loader=retained_assets.get,
                )
                if item.guard_verdict != recomputed_visual:
                    raise ValueError("transition visual guard verdict differs")
        if (
            target is not None
            and target != state.transitions[group[-1].transition_index].target
        ):
            raise ValueError("transition evidence selected an undeclared target")
        return str(target) if target is not None else None

    def _validated_attended_target(
        *,
        graph_id: str,
        state: Any,
        scope: tuple[Any, ...],
        current_params: Mapping[str, str],
    ) -> tuple[bool, str | None, str | None]:
        nonlocal attended_evidence_cursor, expected_evidence_decision_index
        evidence = attended_transition_evidence or []
        if attended_evidence_cursor >= len(evidence):
            return False, None, None
        item = evidence[attended_evidence_cursor]
        if (
            item.graph_id != graph_id
            or item.state_id != state.id
            or tuple(item.program_scope) != scope
        ):
            return False, None, None
        if item.decision_index != expected_evidence_decision_index:
            raise ValueError("attended transition evidence sequence is not contiguous")
        if item.governed_runtime_inputs_digest != governed_runtime_inputs_digest:
            raise ValueError("attended transition input binding differs")
        payload = _verified_inventory_bytes(
            item.receipt_inventory_ref,
            item.receipt_sha256,
        )
        from openadapt_flow.runtime.durable.attended import (
            AttendedActionRefused,
            AttendedActionStore,
        )
        from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
        from openadapt_flow.runtime.durable.program_checkpoint import (
            GraphFrame,
            ProgramTransitionReceipt,
            bound_params_sha256,
            bundle_version,
            control_frames_hash,
        )

        if transition_evidence_root is None:
            raise ValueError("attended transition evidence has no local evidence root")
        try:
            parsed = ProgramTransitionReceipt.model_validate_json(payload)
            authenticated = AttendedActionStore(
                transition_evidence_root
            ).read_program_receipt(item.receipt_pause_id)
            manifest = CheckpointStore(transition_evidence_root).read_manifest()
        except (OSError, ValueError, AttendedActionRefused) as exc:
            raise ValueError("attended transition receipt does not verify") from exc
        if (
            authenticated != parsed
            or parsed.pause_id != item.receipt_pause_id
            or parsed.source_graph_id != graph_id
            or parsed.source_state_id != state.id
            or parsed.target_state_id != item.target_state_id
            or parsed.action != item.action
            or run_id_sha256 is None
            or hashlib.sha256(parsed.run_id.encode("utf-8")).hexdigest()
            != run_id_sha256
            or parsed.workflow_name != workflow.name
            or workflow_contract_digest is None
            or parsed.workflow_contract_sha256 != workflow_contract_digest
            or parsed.governed_runtime_inputs_digest != governed_runtime_inputs_digest
            or parsed.bound_params_sha256 != bound_params_sha256(dict(current_params))
            or manifest is None
            or manifest.run_id != parsed.run_id
            or manifest.workflow_name != parsed.workflow_name
            or manifest.params != dict(runtime_params or {})
            or (
                manifest.governed_authorization.runtime_inputs_digest
                if manifest.governed_authorization is not None
                else None
            )
            != governed_runtime_inputs_digest
        ):
            raise ValueError("attended transition receipt binding differs")
        try:
            live_bundle_version = bundle_version(manifest.bundle_dir)
        except OSError as exc:
            raise ValueError("attended transition bundle cannot be verified") from exc
        if parsed.bundle_version != live_bundle_version:
            raise ValueError("attended transition bundle binding differs")
        control_payload = _verified_inventory_bytes(
            item.control_frames_inventory_ref,
            item.control_frames_sha256,
        )
        try:
            control_frames = [
                GraphFrame.model_validate(frame)
                for frame in json.loads(control_payload)
            ]
        except (TypeError, ValueError) as exc:
            raise ValueError("attended transition control frames are invalid") from exc
        if (
            not control_frames
            or control_frames_hash(control_frames) != parsed.control_frames_hash
            or control_frames[-1].graph_id != graph_id
            or control_frames[-1].state_id != state.id
            or control_frames[-1].params != dict(current_params)
        ):
            raise ValueError("attended transition control-frame binding differs")
        retained_scope = tuple(
            ProgramExecutionScopeFrame(
                graph_id=frame.graph_id,
                loop_state_id=(
                    frame.loop.loop_state_id if frame.loop is not None else None
                ),
                relation=frame.loop.relation if frame.loop is not None else None,
                row_index=frame.loop.row_index if frame.loop is not None else None,
            )
            for frame in control_frames
        )
        if retained_scope != scope:
            raise ValueError("attended transition program scope differs")
        targets = [str(transition.target) for transition in state.transitions]
        if item.target_state_id is None:
            if targets:
                raise ValueError("attended transition omitted a declared successor")
        elif item.target_state_id not in targets:
            raise ValueError("attended transition selected an undeclared successor")
        attended_evidence_cursor += 1
        expected_evidence_decision_index += 1
        return True, item.target_state_id, item.action

    def _validated_business_decision(
        *,
        graph_id: str,
        state: Any,
        scope: tuple[Any, ...],
        current_params: dict[str, str],
    ) -> str:
        nonlocal business_evidence_cursor, expected_evidence_decision_index
        evidence = business_decision_evidence or []
        if business_evidence_cursor >= len(evidence):
            raise ValueError("business decision lacks signed human evidence")
        item = evidence[business_evidence_cursor]
        spec = state.decision
        if (
            spec is None
            or item.graph_id != graph_id
            or item.state_id != state.id
            or tuple(item.program_scope) != scope
            or item.decision_index != expected_evidence_decision_index
            or item.decision_contract_sha256 != spec.contract_sha256()
            or item.governed_runtime_inputs_digest != governed_runtime_inputs_digest
        ):
            raise ValueError("business decision evidence binding differs")
        if transition_evidence_root is None:
            raise ValueError("business decision has no local evidence root")
        from openadapt_flow.runtime.durable.business_decision import (
            BusinessDecisionRefused,
            BusinessDecisionStore,
        )
        from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
        from openadapt_flow.runtime.durable.program_checkpoint import (
            bound_params_sha256,
            bundle_version,
        )

        try:
            store = BusinessDecisionStore(transition_evidence_root)
            manifest = CheckpointStore(transition_evidence_root).read_manifest()
            if manifest is None:
                raise ValueError("business decision has no durable manifest")
            expected_bundle = bundle_version(manifest.bundle_dir)
            receipt = store.authenticate_evidence(
                item,
                workflow=workflow,
                run_id=manifest.run_id,
                expected_bundle_version=expected_bundle,
                governed_runtime_inputs_digest=governed_runtime_inputs_digest,
            )
            request = store.authenticate_request(item.request_sha256)
        except (OSError, ValueError, BusinessDecisionRefused) as exc:
            raise ValueError("business decision signature does not verify") from exc
        options = [option for option in spec.options if option.id == item.option_id]
        if len(options) != 1:
            raise ValueError("business decision chose an undeclared option")
        option = options[0]
        if (
            request.digest != item.request_digest
            or receipt.request_digest != request.digest
            or request.workflow_name != workflow.name
            or workflow_contract_digest is None
            or request.workflow_contract_sha256 != workflow_contract_digest
            or request.governed_runtime_inputs_digest != governed_runtime_inputs_digest
            or request.program_scope != scope
            or request.bound_params_sha256 != bound_params_sha256(dict(current_params))
            or run_id_sha256 is None
            or hashlib.sha256(request.run_id.encode("utf-8")).hexdigest()
            != run_id_sha256
            or manifest.run_id != request.run_id
            or manifest.workflow_name != request.workflow_name
            or request.bundle_version != expected_bundle
            or item.output_param != spec.output_param
            or item.output_value != option.value
            or item.target_state_id != option.target
        ):
            raise ValueError("business decision receipt binding differs")
        current_params[spec.output_param] = option.value
        business_evidence_cursor += 1
        expected_evidence_decision_index += 1
        return option.target

    def _expected_non_action_exception_kind(
        state: Any,
        *,
        branch_evaluated: bool,
        branch_target: str | None,
    ) -> str | None:
        """Recompute the exact non-action failure that runtime can handle."""

        if (
            state.kind is StateKind.BRANCH
            and branch_evaluated
            and branch_target is None
        ):
            return "branch_without_transition"
        if state.kind is StateKind.SUBFLOW_CALL and (
            state.subflow is None or state.subflow not in graphs
        ):
            return "missing_subflow"
        if state.kind is StateKind.LOOP and state.loop is not None:
            if state.loop.body not in graphs:
                return "missing_loop_body"
            rows = _rows(state.loop.relation)
            if rows is not None and len(rows) > state.loop.max_iterations:
                return "loop_bound_exceeded"
        return None

    def _validated_exception_target(
        *,
        graph_id: str,
        state: Any,
        scope: tuple[Any, ...],
        expected_kind: str,
        action_result: Any | None = None,
    ) -> str:
        """Authenticate one recomputable non-action exception edge."""

        nonlocal exception_evidence_cursor, expected_evidence_decision_index
        evidence = exception_evidence or []
        if exception_evidence_cursor >= len(evidence):
            raise ValueError("program exception edge lacks typed evidence")
        item = evidence[exception_evidence_cursor]
        if (
            item.graph_id != graph_id
            or item.state_id != state.id
            or tuple(item.program_scope) != scope
            or item.decision_index != expected_evidence_decision_index
            or item.target_state_id != state.on_exception
            or item.failure_kind != expected_kind
            or item.governed_runtime_inputs_digest != governed_runtime_inputs_digest
        ):
            raise ValueError("program exception evidence binding differs")
        if expected_kind == "action_failure":
            if (
                action_result is None
                or action_result.error is None
                or action_result.failure_category != "runtime_failure"
                or item.action_failure_category != "runtime_failure"
                or item.error_sha256
                != hashlib.sha256(action_result.error.encode("utf-8")).hexdigest()
            ):
                raise ValueError("program action exception cause differs")
        elif item.error_sha256 is not None or item.action_failure_category is not None:
            raise ValueError("non-action exception invents an action cause")
        exception_evidence_cursor += 1
        expected_evidence_decision_index += 1
        return str(item.target_state_id)

    def _selected_transition_target(
        state: Any,
        current_params: dict[str, str],
        *,
        graph_id: str,
        scope: tuple[Any, ...],
    ) -> tuple[str | None, str | None]:
        """Apply the runtime's ordered, first-matching transition semantics."""

        attended, attended_target, attended_action = _validated_attended_target(
            graph_id=graph_id,
            state=state,
            scope=scope,
            current_params=current_params,
        )
        if attended:
            return attended_target, attended_action
        if not state.transitions:
            return None, None
        evidence_target = _validated_evidence_target(
            graph_id=graph_id,
            state=state,
            scope=scope,
            current_params=current_params,
        )
        for transition in state.transitions:
            if transition.guard is None:
                target = str(transition.target)
                if evidence_target is not None and target != evidence_target:
                    raise ValueError(
                        "retained transition evidence chose another target"
                    )
                return target, None
            matched = _reported_guard_value(transition.guard, current_params)
            if matched is None:
                if evidence_target is None:
                    continue
                return evidence_target, None
            if matched:
                target = str(transition.target)
                if evidence_target is not None and target != evidence_target:
                    raise ValueError(
                        "retained transition evidence chose another target"
                    )
                return target, None
        if evidence_target is not None:
            raise ValueError("transition evidence selected a guard that did not match")
        return None, None

    def _next_state(
        state: Any,
        graph: Any,
        occurrence_index: int | None,
        current_params: dict[str, str],
        *,
        graph_id: str,
        scope: tuple[Any, ...],
    ) -> str | None:
        nonlocal cursor, halted_at_requested_action
        if not state.transitions and state.on_exception is None:
            attended, attended_target, attended_action = _validated_attended_target(
                graph_id=graph_id,
                state=state,
                scope=scope,
                current_params=current_params,
            )
            if attended:
                if attended_target is not None or occurrence_index is None:
                    raise ValueError("attended fall-off transition is invalid")
                previous = actions[occurrence_index]
                actions[occurrence_index] = _ProgramActionOccurrence(
                    state_id=previous.state_id,
                    step=previous.step,
                    program_scope=previous.program_scope,
                    attended_action=attended_action,
                )
            return None
        if cursor >= len(visited_states):
            raise ValueError("program trace ended before a declared successor")
        candidate = visited_states[cursor]
        branch_evaluated = state.kind is StateKind.BRANCH
        branch_target: str | None = None
        branch_attended_action: str | None = None
        if branch_evaluated:
            branch_target, branch_attended_action = _selected_transition_target(
                state,
                current_params,
                graph_id=graph_id,
                scope=scope,
            )
        expected_non_action_exception = _expected_non_action_exception_kind(
            state,
            branch_evaluated=branch_evaluated,
            branch_target=branch_target,
        )
        if expected_non_action_exception is not None:
            if state.on_exception is None:
                raise ValueError("unhandled non-action failure cannot continue")
            exception_target = _validated_exception_target(
                graph_id=graph_id,
                state=state,
                scope=scope,
                expected_kind=expected_non_action_exception,
            )
            if candidate != exception_target:
                raise ValueError("program trace bypassed its exception handler")
            return exception_target
        normal_targets = {transition.target for transition in state.transitions}
        explicit_exception_edge: bool | None = None
        if occurrence_index is not None and reported_results is not None:
            if occurrence_index >= len(reported_results):
                raise ValueError("program trace lacks its action result")
            reported = reported_results[occurrence_index]
            if reported.exception_handled:
                if reported.skipped or reported.ok:
                    raise ValueError("program exception result has an invalid shape")
                explicit_exception_edge = True
            else:
                explicit_exception_edge = False
        action_exception_target: str | None = None
        if explicit_exception_edge is True:
            if state.on_exception is None:
                raise ValueError("action exception result has no declared handler")
            action_exception_target = _validated_exception_target(
                graph_id=graph_id,
                state=state,
                scope=scope,
                expected_kind="action_failure",
                action_result=reported,
            )
        if (
            state.on_exception is not None
            and candidate == state.on_exception
            and candidate not in normal_targets
        ):
            if (
                occurrence_index is None
                or explicit_exception_edge is not True
                or action_exception_target != candidate
            ):
                raise ValueError("program exception edge lacks an exact action result")
            previous = actions[occurrence_index]
            actions[occurrence_index] = _ProgramActionOccurrence(
                state_id=previous.state_id,
                step=previous.step,
                program_scope=previous.program_scope,
                exception_edge=True,
            )
            return candidate
        if (
            state.on_exception is not None
            and candidate == state.on_exception
            and candidate in normal_targets
            and explicit_exception_edge is True
        ):
            if occurrence_index is None or action_exception_target != candidate:
                raise ValueError("non-action exception edge is ambiguous")
            previous = actions[occurrence_index]
            actions[occurrence_index] = _ProgramActionOccurrence(
                state_id=previous.state_id,
                step=previous.step,
                program_scope=previous.program_scope,
                exception_edge=True,
            )
            return candidate
        if branch_evaluated:
            selected_target, attended_action = branch_target, branch_attended_action
        else:
            selected_target, attended_action = _selected_transition_target(
                state,
                current_params,
                graph_id=graph_id,
                scope=scope,
            )
        if candidate == selected_target:
            if explicit_exception_edge is True:
                raise ValueError("exception result entered a normal edge")
            if occurrence_index is not None and attended_action is not None:
                previous = actions[occurrence_index]
                actions[occurrence_index] = _ProgramActionOccurrence(
                    state_id=previous.state_id,
                    step=previous.step,
                    program_scope=previous.program_scope,
                    attended_action=attended_action,
                )
            return candidate
        if state.on_exception is not None and candidate == state.on_exception:
            if (
                occurrence_index is None
                or explicit_exception_edge is not True
                or action_exception_target != candidate
            ):
                raise ValueError("program exception edge lacks an exact action result")
            previous = actions[occurrence_index]
            actions[occurrence_index] = _ProgramActionOccurrence(
                state_id=previous.state_id,
                step=previous.step,
                program_scope=previous.program_scope,
                exception_edge=True,
            )
            return candidate
        raise ValueError("program trace crossed an undeclared transition")

    def _consume_graph(
        graph_id: str,
        scope: tuple[Any, ...],
        *,
        depth: int,
        current_params: dict[str, str],
    ) -> None:
        nonlocal cursor, halted_at_requested_action
        if depth > 64:
            raise ValueError("program trace nesting is not bounded")
        graph = graphs.get(graph_id)
        if graph is None:
            raise ValueError("program trace references an unknown graph")
        state_id: str | None = graph.entry
        while state_id is not None:
            if cursor >= len(visited_states) or visited_states[cursor] != state_id:
                raise ValueError("program trace does not follow the graph entry")
            state = graph.states.get(state_id)
            if state is None or state_owner.get(state_id) != graph_id:
                raise ValueError("program trace references an unknown state")
            cursor += 1
            occurrence_index: int | None = None
            if state.kind is StateKind.ACTION:
                if state.step is None:
                    raise ValueError("action state has no step")
                occurrence_index = len(actions)
                actions.append(
                    _ProgramActionOccurrence(
                        state_id=state.id,
                        step=state.step,
                        program_scope=scope,
                    )
                )
                if (
                    halted_at_step_id is not None
                    and state.step.id == halted_at_step_id
                    and cursor == len(visited_states)
                ):
                    halted_at_requested_action = True
                    raise _RequestedActionHalt
            business_target: str | None = None
            if state.kind is StateKind.BUSINESS_DECISION:
                business_target = _validated_business_decision(
                    graph_id=graph_id,
                    state=state,
                    scope=scope,
                    current_params=current_params,
                )
            if state.kind is StateKind.TERMINAL:
                if (state.outcome or "success") != "success":
                    raise ValueError("successful trace reached a non-success terminal")
                return
            if state.kind is StateKind.SUBFLOW_CALL:
                if state.subflow is not None and state.subflow in graphs:
                    _consume_graph(
                        state.subflow,
                        (*scope, ProgramExecutionScopeFrame(graph_id=state.subflow)),
                        depth=depth + 1,
                        current_params=current_params,
                    )
            elif state.kind is StateKind.LOOP:
                if state.loop is None:
                    raise ValueError("loop body is missing")
                rows = _rows(state.loop.relation)
                if rows is None:
                    raise ValueError(
                        "loop worklist is unavailable or outside its bound"
                    )
                if state.loop.body in graphs and len(rows) <= state.loop.max_iterations:
                    for row_index in range(len(rows)):
                        iteration_params = {**current_params, **rows[row_index]}
                        _consume_graph(
                            state.loop.body,
                            (
                                *scope,
                                ProgramExecutionScopeFrame(
                                    graph_id=state.loop.body,
                                    loop_state_id=state.id,
                                    relation=state.loop.relation,
                                    row_index=row_index,
                                ),
                            ),
                            depth=depth + 1,
                            current_params=iteration_params,
                        )
            state_id = _next_state(
                state,
                graph,
                occurrence_index,
                current_params,
                graph_id=graph_id,
                scope=scope,
            )
            if business_target is not None and state_id != business_target:
                raise ValueError(
                    "business decision transition did not match its signed answer"
                )

    try:
        _consume_graph(
            "__program__",
            (ProgramExecutionScopeFrame(graph_id="__program__"),),
            depth=0,
            current_params=trace_base_params,
        )
    except _RequestedActionHalt:
        pass
    except ValueError:
        return None
    if evidence_cursor != len(transition_evidence or []):
        return None
    if exception_evidence_cursor != len(exception_evidence or []):
        return None
    if attended_evidence_cursor != len(attended_transition_evidence or []):
        return None
    if business_evidence_cursor != len(business_decision_evidence or []):
        return None
    if cursor != len(visited_states) or (
        halted_at_step_id is not None and not halted_at_requested_action
    ):
        return None
    if business_decision_evidence and trace_base_params != dict(runtime_params or {}):
        # Signed decision receipts are the authority for decision outputs.
        # The run report cannot claim another value and then use that value to
        # evaluate an action, identity, postcondition, or effect contract.
        return None
    return actions


def classify_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
    transition_evidence_root: Path | None = None,
    transition_predicate_vision: Any | None = None,
    _qualification_fault_target_step_id: str | None = None,
) -> ExecutionOutcome:
    """Classify a completed report without changing legacy ``success``.

    Demo success is always visibly non-production.  Standard and Regulated
    success becomes ``VERIFIED`` only when every executed consequential action
    has a confirmed effect at or above the workflow's required evidence tier.
    Therefore an approved-unverified or immediate-screen-only result can never
    be reported as ``VERIFIED`` under either production profile.
    """

    resolved = resolve_execution_profile(profile)
    execution_completed = (
        report.execution_completed
        if report.execution_completed is not None
        else report.success
    )
    fault_prefix_review = _qualification_fault_target_step_id is not None
    if _completed_compensation_actions(report) > 0:
        return ExecutionOutcome.ROLLED_BACK
    if (
        not fault_prefix_review
        and execution_completed
        and (
            report.canceled
            or report.halt is not None
            or report.terminal_outcome
            in {
                "halt",
                "escalate",
                "cancelled",
                "canceled",
            }
        )
    ):
        return ExecutionOutcome.HALTED
    if (
        not fault_prefix_review
        and execution_completed
        and report.terminal_outcome in {"failed", "failure"}
    ):
        return ExecutionOutcome.FAILED
    if any(
        result.failure_category == "runtime_failure"
        and (result.ok or not result.exception_handled)
        for result in report.results
    ):
        return ExecutionOutcome.FAILED
    if any(result.ok and result.error is not None for result in report.results):
        return ExecutionOutcome.FAILED
    if not execution_completed and not fault_prefix_review:
        refusal_step_ids = {"<authorization>", "<params>", "<profile>"}
        governed_halt = (
            report.terminal_outcome in {"halt", "escalate"}
            or any(result.safety_halt for result in report.results)
            or any(
                result.safety_refusal_evidence is not None for result in report.results
            )
            or any(
                result.failure_category in {"governed_refusal", "safety_halt"}
                for result in report.results
            )
            or any(result.step_id in refusal_step_ids for result in report.results)
        )
        return ExecutionOutcome.HALTED if governed_halt else ExecutionOutcome.FAILED

    if resolved is ExecutionProfile.DEMO:
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    if not (report.governed_authorization_id and report.governed_runtime_inputs_digest):
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    unhandled_results = [
        result
        for result in report.results
        if not result.ok and not result.skipped and not result.exception_handled
    ]
    if not fault_prefix_review and any(
        result.safety_halt
        or result.safety_refusal_evidence is not None
        or result.failure_category in {"governed_refusal", "safety_halt"}
        for result in report.results
    ):
        return ExecutionOutcome.HALTED
    if not fault_prefix_review and unhandled_results:
        return ExecutionOutcome.FAILED

    # Import lazily: run_gate imports this module for the profile contract.
    from openadapt_flow.qualification import workflow_contract_sha256
    from openadapt_flow.run_gate import is_consequential, must_be_identity_armed
    from openadapt_flow.traversal import iter_workflow_steps

    if report.workflow_contract_sha256 != workflow_contract_sha256(workflow):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    try:
        expected_qualified_requirements = qualified_effect_requirements(
            workflow, resolved
        )
    except ValueError:
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    if tuple(report.governed_qualified_effect_requirements) != (
        expected_qualified_requirements
    ):
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    qualification_review_context = bool(
        report.qualification_evidence_only
        and workflow.qualification is not None
        and report.governed_qualification_project_id
        == workflow.qualification.project_id
        and report.governed_qualification_project_revision
        == workflow.qualification.revision
        and report.governed_qualification_project_contract_sha256
        == workflow.qualification.contract_sha256()
    )
    all_steps = list(iter_workflow_steps(workflow))
    if len({step.id for step in all_steps}) != len(all_steps):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    consequential = {
        step.id
        for step in all_steps
        if is_consequential(
            step,
            workflow,
            require_current_risk_certification=not qualification_review_context,
            certifying_policy_sha256=report.governed_policy_contract_sha256,
        )
    }
    if qualification_review_context:
        from openadapt_flow.qualification import qualification_action_requirements

        _required_actions, expected_required_identity_ids = (
            qualification_action_requirements(workflow)
        )
    else:
        expected_required_identity_ids = {
            step.id
            for step in all_steps
            if must_be_identity_armed(
                step,
                workflow,
                require_current_risk_certification=True,
                certifying_policy_sha256=report.governed_policy_contract_sha256,
            )
        }
    paired_results: list[tuple[Any, Any]] = []
    identity_results = report.results
    if workflow.program is None:
        if report.program_transition_evidence:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(result.program_scope for result in report.results):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if report.program_exception_evidence:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if report.attended_program_transition_evidence:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if report.business_decision_evidence:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(result.exception_handled for result in report.results):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if fault_prefix_review:
            target_indexes = [
                index
                for index, step in enumerate(workflow.steps)
                if step.id == _qualification_fault_target_step_id
            ]
            if len(target_indexes) != 1:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            target_index = target_indexes[0]
            expected_steps = workflow.steps[: target_index + 1]
            if [result.step_id for result in report.results] != [
                step.id for step in expected_steps
            ]:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            paired_results = list(zip(report.results[:-1], expected_steps[:-1]))
            identity_results = report.results[:-1]
        else:
            if [result.step_id for result in report.results] != [
                step.id for step in workflow.steps
            ]:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            paired_results = list(zip(report.results, workflow.steps))
    else:
        if not fault_prefix_review and report.terminal_outcome != "success":
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if fault_prefix_review and report.terminal_outcome != "halt":
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        expected_action_trace = _program_action_trace(
            workflow,
            report.visited_states,
            runtime_params=report.params,
            runtime_worklists=runtime_worklists,
            transition_evidence=report.program_transition_evidence,
            exception_evidence=report.program_exception_evidence,
            attended_transition_evidence=(report.attended_program_transition_evidence),
            business_decision_evidence=report.business_decision_evidence,
            transition_evidence_root=transition_evidence_root,
            transition_predicate_vision=transition_predicate_vision,
            governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
            run_id_sha256=report.run_id_sha256,
            workflow_contract_digest=report.workflow_contract_sha256,
            halted_at_step_id=_qualification_fault_target_step_id,
            reported_results=report.results,
        )
        if expected_action_trace is None:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        action_results = report.results
        if fault_prefix_review:
            terminal_result = action_results[-1] if action_results else None
            if (
                terminal_result is None
                or terminal_result.step_id != "<terminal>"
                or terminal_result.intent != "program halt"
                or terminal_result.ok
                or not terminal_result.safety_halt
                or terminal_result.skipped
                or terminal_result.exception_handled
                or terminal_result.resolution is not None
                or terminal_result.drag_end_resolution is not None
                or terminal_result.identity is not None
                or terminal_result.input_verified is not None
                or terminal_result.input_retried
                or terminal_result.postconditions_ok is not None
                or terminal_result.interstitial_actions
                or terminal_result.effect_verified is not None
                or terminal_result.effect_approved_unverified
                or terminal_result.effect_results
                or terminal_result.effect_evidence
                or terminal_result.effect_contract_hashes
                or terminal_result.failure_category is not None
                or terminal_result.actuation is not None
                or terminal_result.program_scope
                or terminal_result.delivery_receipt is not None
                or terminal_result.delivery_attempted is not None
                or terminal_result.delivery_uncertainty is not None
                or terminal_result.safety_refusal_evidence is not None
                or terminal_result.starting_state_settled is not None
                or terminal_result.postcondition_drift_rescues
                or terminal_result.drift_oracle_calls
                or terminal_result.heal is not None
                or terminal_result.before_png is not None
                or terminal_result.after_png is not None
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            action_results = action_results[:-1]
            fault_target_result = action_results[-1] if action_results else None
            if (
                fault_target_result is None
                or fault_target_result.ok
                or not fault_target_result.safety_halt
                or fault_target_result.failure_category
                not in {"governed_refusal", "safety_halt"}
                or fault_target_result.delivery_attempted is not False
                or fault_target_result.actuation is not None
                or fault_target_result.delivery_receipt is not None
                or fault_target_result.delivery_uncertainty is not None
                or fault_target_result.effect_verified is True
                or fault_target_result.effect_approved_unverified
                or fault_target_result.input_verified is not None
                or fault_target_result.input_retried
                or fault_target_result.postconditions_ok is not None
                or fault_target_result.error is None
                or terminal_result.error != fault_target_result.error
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        if len(action_results) != len(expected_action_trace):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        for result, occurrence in zip(action_results, expected_action_trace):
            if (
                result.step_id != occurrence.step.id
                or tuple(result.program_scope) != occurrence.program_scope
                or (
                    occurrence.exception_edge
                    and (result.skipped or result.ok or not result.exception_handled)
                )
                or (not occurrence.exception_edge and result.exception_handled)
                or (
                    occurrence.attended_action == "skip"
                    and (
                        not result.skipped or result.actuation != "human_attended_skip"
                    )
                )
                or (
                    occurrence.attended_action == "continue"
                    and (result.skipped or result.actuation != "human_attended")
                )
                or (
                    occurrence.attended_action is None
                    and result.actuation in HUMAN_ATTENDED_ACTUATIONS
                )
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            paired_results.append((result, occurrence.step))
        if fault_prefix_review:
            paired_results = paired_results[:-1]
            identity_results = action_results[:-1]
    required_identity_ids = set(report.required_identity_step_ids)
    allowed_required_identity_sets = [expected_required_identity_ids]
    if fault_prefix_review:
        # A qualification campaign can bind the complete workflow identity
        # inventory, while a real pre-delivery run can retain only the identity
        # requirements admitted for its executed prefix.  Both are exact.  The
        # target refusal is proved separately and never borrows a verified row.
        allowed_required_identity_sets.append(
            expected_required_identity_ids.intersection(
                result.step_id for result in identity_results
            )
        )
    if (
        len(report.required_identity_step_ids) != len(required_identity_ids)
        or required_identity_ids not in allowed_required_identity_sets
    ):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    if any(
        result.identity is None or result.identity.status != "verified"
        for result in identity_results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    minimum = required_effect_tier(workflow, resolved)
    assert minimum is not None
    from openadapt_flow.ir import ActionKind

    def _scoped_params(result: Any) -> dict[str, str] | None:
        if workflow.program is None and result.program_scope:
            return None
        scoped = dict(report.params)
        for frame in result.program_scope:
            if frame.relation is None:
                continue
            if runtime_worklists is not None and frame.relation in runtime_worklists:
                rows = runtime_worklists[frame.relation]
            else:
                relation = workflow.data_sources.get(frame.relation)
                if relation is None:
                    return None
                rows = relation.rows
            if frame.row_index is None or frame.row_index >= len(rows):
                return None
            scoped.update(rows[frame.row_index])
        return scoped

    def _reported_parameter_predicate_value(
        predicate: Any, current_params: Mapping[str, str]
    ) -> bool | None:
        """Recompute a guard only when all of its inputs are report-bound."""

        from openadapt_flow.ir import PredicateKind

        if predicate.kind is PredicateKind.PARAM_EQUALS:
            return predicate.param is not None and str(
                current_params.get(predicate.param)
            ) == str(predicate.value)
        if predicate.kind is PredicateKind.AND:
            values = [
                _reported_parameter_predicate_value(item, current_params)
                for item in predicate.operands
            ]
            if any(value is False for value in values):
                return False
            return True if all(value is True for value in values) else None
        if predicate.kind is PredicateKind.OR:
            values = [
                _reported_parameter_predicate_value(item, current_params)
                for item in predicate.operands
            ]
            if any(value is True for value in values):
                return True
            return False if all(value is False for value in values) else None
        if predicate.kind is PredicateKind.NOT:
            if not predicate.operands:
                return False
            value = _reported_parameter_predicate_value(
                predicate.operands[0], current_params
            )
            return None if value is None else not value
        return None

    for result, step in paired_results:
        if result.skipped or result.exception_handled:
            if (
                (result.skipped and not result.ok)
                or (result.exception_handled and (result.skipped or result.ok))
                or result.delivery_attempted is not False
                or result.delivery_receipt is not None
                or result.delivery_uncertainty is not None
                or result.effect_verified is not None
                or result.effect_contract_hashes
                or result.effect_evidence
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            if result.skipped:
                scoped_params = _scoped_params(result)
                declared_guard_skip = bool(
                    scoped_params is not None
                    and step.guard is not None
                    and step.guard.on_unmet == "skip"
                    and _reported_parameter_predicate_value(
                        step.guard.predicate, scoped_params
                    )
                    is False
                )
                authenticated_attended_skip = (
                    workflow.program is not None
                    and result.actuation == "human_attended_skip"
                )
                if not (declared_guard_skip or authenticated_attended_skip):
                    return ExecutionOutcome.COMPLETED_UNVERIFIED
            continue
        if not result.ok:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        is_consequential_result = result.step_id in consequential
        scoped_params = _scoped_params(result)
        if scoped_params is None:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        evidence_error = action_evidence_error(
            step,
            result,
            params=scoped_params,
            identity_required=result.step_id in required_identity_ids,
            strict_production=(
                is_consequential_result or result.step_id in required_identity_ids
            ),
        )
        if evidence_error is not None:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        project = workflow.qualification
        identity_policy = (
            project.identity_policies.get(step.id) if project is not None else None
        )
        if result.step_id in required_identity_ids and identity_policy is not None:
            from openadapt_flow.qualification_identity_evidence import (
                qualification_identity_evidence_error,
            )

            recorded_asset_sha256 = (
                workflow.manifest.file_hashes.get(step.anchor.identifier_crop)
                if workflow.manifest is not None
                and step.anchor is not None
                and step.anchor.identifier_crop is not None
                else None
            )
            if (
                qualification_identity_evidence_error(
                    policy=identity_policy,
                    check=result.identity,
                    step=step,
                    actuation_path=("api" if result.actuation == "api" else "gui"),
                    runtime_params=scoped_params,
                    recorded_params=workflow.params,
                    evidence_root=transition_evidence_root,
                    recorded_asset_sha256=recorded_asset_sha256,
                )
                is not None
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        if step.action is ActionKind.WAIT:
            if result.actuation is not None:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        elif result.actuation is None:
            # Older local, non-consequential keyboard/mouse delivery did not
            # label its actuation path. It still carries settle and delivery
            # proof. A consequential result must name a closed path unless a
            # typed uncertain-delivery record proves that the GUI dispatch was
            # attempted. That record still needs the complete postcondition and
            # effect contract below before it can verify.
            if is_consequential_result and result.delivery_uncertainty is None:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        elif result.actuation not in {
            "api",
            *AUTOMATED_GUI_ACTUATIONS,
            *HUMAN_ATTENDED_ACTUATIONS,
        }:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        api_actuation = result.actuation == "api"
        from openadapt_flow.policy import effects_for_actuation

        effects = effects_for_actuation(step, result.actuation)
        automated_gui = result.actuation in AUTOMATED_GUI_ACTUATIONS or (
            result.actuation is None and step.action is not ActionKind.WAIT
        )
        if api_actuation:
            # The API tier bypasses the GUI resolve/settle/act/postcondition
            # path. A successful API result therefore contains only the API
            # delivery marker, API identity evidence, and independent effect
            # evidence. Reject a report that combines API effects with GUI-only
            # artifacts; the runtime cannot emit that mixed path.
            if (
                result.resolution is not None
                or result.drag_end_resolution is not None
                or result.delivery_receipt is not None
                or result.delivery_uncertainty is not None
                or result.starting_state_settled is not None
                or result.input_verified is not None
                or result.input_retried
                or result.postconditions_ok is not None
                or result.postcondition_drift_rescues
                or result.drift_oracle_calls
                or result.heal is not None
                or result.before_png is not None
                or result.after_png is not None
                or result.interstitial_actions
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            if not effects:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        if automated_gui and result.starting_state_settled is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if (
            automated_gui
            and step.action in {ActionKind.TYPE, ActionKind.SELECT_OPTION}
            and result.input_verified is not True
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if (
            automated_gui
            and step.action is not ActionKind.WAIT
            and result.delivery_attempted is not True
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if api_actuation and result.delivery_attempted is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        # An unarmed step can retain an optional identity observation.  An
        # abstain/unreadable observation means the optional ladder could not
        # decide; it must not downgrade an otherwise complete safe action.
        # A mismatch is affirmative wrong-entity evidence and always rejects
        # the outcome. Required identities were checked above and still need
        # nonvacuous verified proof.
        if result.identity is not None and result.identity.status == "mismatch":
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(
            not interstitial.ok
            or not interstitial.delivered
            or interstitial.clearance_ok is not True
            for interstitial in result.interstitial_actions
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        uncertainty = result.delivery_uncertainty
        if uncertainty is not None and not (
            uncertainty.verification_attempted
            and uncertainty.effects_confirmed is True
            and uncertainty.resolved_by_contract
            and (not step.expect or uncertainty.postconditions_confirmed is True)
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if not api_actuation and step.expect and result.postconditions_ok is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if not is_consequential_result:
            continue
        from openadapt_flow.policy import has_postcondition_contract

        if not has_postcondition_contract(
            step,
            actuation_path="api" if api_actuation else "gui",
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if result.effect_approved_unverified or result.effect_verified is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if api_actuation and not _api_identity_evidence_is_exact(
            workflow=workflow,
            step=step,
            check=result.identity,
            scoped_params=scoped_params,
            effects=effects,
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        opaque = (
            {"__run_id__": report.run_id_sha256}
            if report.run_id_sha256 is not None
            else {}
        )
        try:
            expected_hashes = Counter(
                effect.resolved_contract_hash(
                    scoped_params,
                    opaque_param_sha256=opaque,
                )
                for effect in effects
            )
        except ValueError:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if Counter(result.effect_contract_hashes) != expected_hashes:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(
            item.initial_verdict != "confirmed"
            or item.final_verdict != "confirmed"
            or item.observed_effect != "present"
            or item.reconciliation_completed
            or item.reconciliation_actions
            or item.verification_tier is None
            for item in result.effect_evidence
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        evidence_hashes = Counter(
            item.effect_contract_hash for item in result.effect_evidence
        )
        if not expected_hashes or evidence_hashes != expected_hashes:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        actuation_path = "api" if result.actuation == "api" else "gui"
        requirement_by_index = {
            item.effect_index: item
            for item in expected_qualified_requirements
            if item.step_id == step.id and item.actuation_path == actuation_path
        }
        if requirement_by_index and set(requirement_by_index) != set(
            range(len(effects))
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        required_tiers_by_hash: dict[str, list[VerificationTier]] = {}
        for index, effect in enumerate(effects):
            requirement = requirement_by_index.get(index)
            if requirement is not None and (
                requirement.effect_contract_hash != effect.contract_hash()
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            required_tier = (
                VerificationTier(requirement.minimum_tier)
                if requirement is not None
                else minimum
            )
            try:
                effect_hash = effect.resolved_contract_hash(
                    scoped_params,
                    opaque_param_sha256=opaque,
                )
            except ValueError:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            required_tiers_by_hash.setdefault(effect_hash, []).append(required_tier)
        observed_tiers_by_hash: dict[str, list[VerificationTier]] = {}
        for evidence in result.effect_evidence:
            assert evidence.verification_tier is not None
            observed_tiers_by_hash.setdefault(evidence.effect_contract_hash, []).append(
                VerificationTier(evidence.verification_tier)
            )
        for effect_hash, required_tiers in required_tiers_by_hash.items():
            observed_tiers = observed_tiers_by_hash.get(effect_hash, [])
            if len(observed_tiers) != len(required_tiers):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            required_tiers.sort(key=int)
            observed_tiers.sort(key=int)
            if any(
                not observed.satisfies(required)
                for observed, required in zip(observed_tiers, required_tiers)
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
    return ExecutionOutcome.VERIFIED


def stamp_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
    transition_evidence_root: Path | None = None,
    transition_predicate_vision: Any | None = None,
) -> ExecutionOutcome:
    """Write the profile and precise outcome into ``report``."""

    resolved = resolve_execution_profile(profile)
    report.external_network_calls = _external_network_call_state(report)
    if report.execution_completed is None:
        report.execution_completed = report.success
    report.qualification_evidence_only = bool(
        report.governed_qualification_case_id_sha256
    )
    outcome = classify_execution_outcome(
        report,
        workflow,
        resolved,
        runtime_worklists=runtime_worklists,
        transition_evidence_root=transition_evidence_root,
        transition_predicate_vision=transition_predicate_vision,
    )
    report.execution_profile = resolved.value
    report.execution_outcome = outcome.value
    report.production_eligible = bool(
        execution_profile_contract(resolved).production
        and outcome is ExecutionOutcome.VERIFIED
        and not report.qualification_evidence_only
    )
    if execution_profile_contract(resolved).production:
        report.success = outcome is ExecutionOutcome.VERIFIED
    elif outcome is ExecutionOutcome.ROLLED_BACK:
        report.success = False
    report.outcome_envelope = build_outcome_envelope(
        report,
        workflow,
        runtime_worklists=runtime_worklists,
    )
    # Section 3: refine the coarse outcome into a first-class terminal
    # transaction outcome + effect journal. Additive -- reads the fields set
    # above and never mutates them (leaf import; see openadapt_flow.transaction).
    from openadapt_flow.transaction import stamp_transaction_outcome

    stamp_transaction_outcome(report, workflow)
    return outcome


def _completed_compensation_actions(report: RunReport) -> int:
    """Count only compensations that completed and were re-verified."""

    return sum(
        evidence.reconciliation_actions
        for result in report.results
        for evidence in result.effect_evidence
        if (
            evidence.reconciliation_completed
            and evidence.reconciliation_actions > 0
            and evidence.final_verdict == "confirmed"
        )
    )


def _postcondition_verdict(
    value: bool | None,
) -> Literal["passed", "refuted", "unverifiable"]:
    if value is True:
        return "passed"
    if value is False:
        return "refuted"
    return "unverifiable"


def build_outcome_envelope(
    report: RunReport,
    workflow: Workflow,
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
) -> ExecutionOutcomeEnvelope:
    """Build the versioned PHI-free evidence summary for ``report``.

    Counts are derived from typed workflow/report fields only.  Free-text
    intents, parameters, identifiers, effect hashes, and observed values never
    enter the envelope.
    """

    from openadapt_flow.ir import (
        ActionKind,
        ExecutionOutcomeEnvelope,
        OutcomeContractCounts,
        OutcomeEvidenceClass,
        PostconditionContractEvidence,
        postcondition_contract_sha256,
        postcondition_step_contract_sha256,
    )
    from openadapt_flow.qualification import workflow_contract_sha256
    from openadapt_flow.traversal import iter_workflow_steps

    if report.execution_outcome is None:
        raise ValueError("execution outcome must be classified before enveloping")

    ordered_steps = list(iter_workflow_steps(workflow))
    steps_by_id = {step.id: step for step in ordered_steps}
    step_index_by_id = {step.id: index for index, step in enumerate(ordered_steps)}
    production = report.execution_profile in {"standard", "regulated"}

    required_authorization = 1 if production else 0
    passed_authorization = int(
        bool(
            required_authorization
            and report.governed_authorization_id
            and report.governed_runtime_inputs_digest
        )
    )

    required_identity_ids = set(report.required_identity_step_ids)
    identity_results = [
        result
        for result in report.results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ]
    required_identity = len(identity_results)
    passed_identity = sum(
        result.identity is not None and result.identity.status == "verified"
        for result in identity_results
    )

    workflow_contract = workflow_contract_sha256(workflow)
    postcondition_evidence: list[PostconditionContractEvidence] = []
    required_effects = 0
    passed_effects = 0
    evidence_classes: set[OutcomeEvidenceClass] = set()
    effect_class_by_tier: dict[int, OutcomeEvidenceClass] = {
        1: "effect_tier_1",
        2: "effect_tier_2",
        3: "effect_tier_3",
        4: "effect_tier_4",
    }
    minimum_effect_tier = (
        required_effect_tier(workflow, report.execution_profile)
        if report.execution_profile is not None
        else None
    )
    effect_requirements_valid = True
    try:
        envelope_requirements = (
            qualified_effect_requirements(workflow, report.execution_profile)
            if report.execution_profile is not None
            else ()
        )
    except ValueError:
        envelope_requirements = ()
        effect_requirements_valid = False

    def _scoped_params(result: Any) -> dict[str, str] | None:
        if workflow.program is None and result.program_scope:
            return None
        scoped = dict(report.params)
        for frame in result.program_scope:
            if frame.relation is None:
                continue
            if runtime_worklists is not None and frame.relation in runtime_worklists:
                rows = runtime_worklists[frame.relation]
            else:
                relation = workflow.data_sources.get(frame.relation)
                if relation is None:
                    return None
                rows = relation.rows
            if frame.row_index is None or frame.row_index >= len(rows):
                return None
            scoped.update(rows[frame.row_index])
        return scoped

    compensation_actions = _completed_compensation_actions(report)
    for result_index, result in enumerate(report.results):
        if result.skipped or result.exception_handled:
            continue
        step = steps_by_id.get(result.step_id)
        bound_effect_tiers: set[int] = set()
        if step is not None:
            api_actuation = result.actuation == "api"
            if not api_actuation:
                step_index = step_index_by_id[result.step_id]
                step_contract = postcondition_step_contract_sha256(
                    workflow_contract_sha256=workflow_contract,
                    step_index=step_index,
                    action_kind=step.action,
                )
                for contract_index in range(len(step.expect)):
                    postcondition_evidence.append(
                        PostconditionContractEvidence(
                            result_index=result_index,
                            workflow_contract_sha256=workflow_contract,
                            step_index=step_index,
                            step_contract_sha256=step_contract,
                            action_kind=step.action,
                            contract_kind="explicit_predicate",
                            contract_index=contract_index,
                            contract_sha256=postcondition_contract_sha256(
                                workflow_contract_sha256=workflow_contract,
                                step_contract_sha256=step_contract,
                                action_kind=step.action,
                                contract_kind="explicit_predicate",
                                contract_index=contract_index,
                            ),
                            verdict=_postcondition_verdict(result.postconditions_ok),
                        )
                    )
                if result.actuation != "human_attended" and step.action in {
                    ActionKind.TYPE,
                    ActionKind.SELECT_OPTION,
                }:
                    contract_kind: Literal["intrinsic_input_readback"] = (
                        "intrinsic_input_readback"
                    )
                    postcondition_evidence.append(
                        PostconditionContractEvidence(
                            result_index=result_index,
                            workflow_contract_sha256=workflow_contract,
                            step_index=step_index,
                            step_contract_sha256=step_contract,
                            action_kind=step.action,
                            contract_kind=contract_kind,
                            contract_index=0,
                            contract_sha256=postcondition_contract_sha256(
                                workflow_contract_sha256=workflow_contract,
                                step_contract_sha256=step_contract,
                                action_kind=step.action,
                                contract_kind=contract_kind,
                                contract_index=0,
                            ),
                            verdict=_postcondition_verdict(result.input_verified),
                        )
                    )
            from openadapt_flow.policy import effects_for_actuation

            effects = effects_for_actuation(step, result.actuation)
            required_effects += len(effects)
            if result.effect_verified is True and effects:
                scoped_params = _scoped_params(result)
                actuation_path = "api" if api_actuation else "gui"
                requirement_by_index = {
                    item.effect_index: item
                    for item in envelope_requirements
                    if item.step_id == result.step_id
                    and item.actuation_path == actuation_path
                }
                requirements_complete = effect_requirements_valid and (
                    not requirement_by_index
                    or set(requirement_by_index) == set(range(len(effects)))
                )
                required_by_hash: dict[str, list[VerificationTier]] = {}
                expected_hashes: list[str] = []
                if scoped_params is not None and requirements_complete:
                    opaque = (
                        {"__run_id__": report.run_id_sha256}
                        if report.run_id_sha256 is not None
                        else {}
                    )
                    try:
                        for index, effect in enumerate(effects):
                            requirement = requirement_by_index.get(index)
                            if requirement is not None and (
                                requirement.effect_contract_hash
                                != effect.contract_hash()
                            ):
                                raise ValueError(
                                    "qualified effect contract does not match"
                                )
                            effect_hash = effect.resolved_contract_hash(
                                scoped_params,
                                opaque_param_sha256=opaque,
                            )
                            expected_hashes.append(effect_hash)
                            required_tier = (
                                VerificationTier(requirement.minimum_tier)
                                if requirement is not None
                                else minimum_effect_tier
                                or VerificationTier.IMMEDIATE_SCREEN
                            )
                            required_by_hash.setdefault(effect_hash, []).append(
                                required_tier
                            )
                    except ValueError:
                        required_by_hash = {}
                        expected_hashes = []
                if Counter(result.effect_contract_hashes) != Counter(expected_hashes):
                    required_by_hash = {}
                observed_by_hash: dict[str, list[VerificationTier]] = {}
                evidence_shape_is_exact = len(result.effect_evidence) == len(
                    expected_hashes
                )
                for evidence in result.effect_evidence:
                    if (
                        evidence.initial_verdict == "confirmed"
                        and evidence.final_verdict == "confirmed"
                        and evidence.observed_effect == "present"
                        and not evidence.reconciliation_completed
                        and evidence.reconciliation_actions == 0
                        and evidence.verification_tier is not None
                    ):
                        observed_by_hash.setdefault(
                            evidence.effect_contract_hash, []
                        ).append(VerificationTier(evidence.verification_tier))
                    else:
                        evidence_shape_is_exact = False
                if Counter(
                    evidence.effect_contract_hash for evidence in result.effect_evidence
                ) != Counter(expected_hashes):
                    evidence_shape_is_exact = False
                if not evidence_shape_is_exact:
                    required_by_hash = {}
                for effect_hash, required_tiers in required_by_hash.items():
                    observed_tiers = observed_by_hash.get(effect_hash, [])
                    bound_effect_tiers.update(int(tier) for tier in observed_tiers)
                    if len(observed_tiers) != len(required_tiers):
                        continue
                    required_tiers.sort(key=int)
                    observed_tiers.sort(key=int)
                    for observed, required_tier in zip(observed_tiers, required_tiers):
                        if observed.satisfies(required_tier):
                            passed_effects += 1
        if result.identity is not None and result.identity.status == "verified":
            evidence_classes.add("identity")
        for evidence in result.effect_evidence:
            if evidence.verification_tier in bound_effect_tiers:
                evidence_class = effect_class_by_tier.get(evidence.verification_tier)
                if evidence_class is not None:
                    evidence_classes.add(evidence_class)
            if (
                evidence.reconciliation_completed
                and evidence.reconciliation_actions > 0
                and evidence.final_verdict == "confirmed"
            ):
                evidence_classes.add("compensation")

    required_postconditions = len(postcondition_evidence)
    passed_postconditions = sum(
        item.verdict == "passed" for item in postcondition_evidence
    )
    if passed_postconditions:
        evidence_classes.add("postcondition")

    required = OutcomeContractCounts(
        authorization=required_authorization,
        identity=required_identity,
        postcondition=required_postconditions,
        effect=required_effects,
    )
    passed = OutcomeContractCounts(
        authorization=passed_authorization,
        identity=min(passed_identity, required_identity),
        postcondition=min(passed_postconditions, required_postconditions),
        effect=min(passed_effects, required_effects),
    )
    if passed.authorization:
        evidence_classes.add("authorization")
    if report.model_calls:
        evidence_classes.add("model")

    return ExecutionOutcomeEnvelope(
        outcome=report.execution_outcome,
        profile=report.execution_profile,
        production_eligible=report.production_eligible,
        qualification_evidence_only=report.qualification_evidence_only,
        execution_completed=bool(report.execution_completed),
        required_contracts=required,
        passed_contracts=passed,
        workflow_contract_sha256=workflow_contract,
        postcondition_evidence=postcondition_evidence,
        evidence_classes=sorted(evidence_classes),
        model_calls=report.model_calls,
        external_network_calls=report.external_network_calls,
        compensation_actions=compensation_actions,
    )


def _external_network_call_state(
    report: RunReport,
) -> Literal["none", "observed", "unknown"]:
    """Report observed egress without turning absence of instrumentation into 0."""

    if report.external_network_calls == "observed" or report.model_calls > 0:
        return "observed"
    if report.execution_origin or report.execution_entry_url:
        return "observed"
    if report.execution_target_kind in {"web", "rdp", "citrix"}:
        return "observed"

    local_substrates = {
        "onscreen",
        "file",
        "document_hash",
        "snapshot",
        "test",
        "fake",
    }
    for result in report.results:
        if result.actuation == "api":
            return "observed"
        for evidence in result.effect_evidence:
            substrate = evidence.substrate.strip().lower()
            if substrate in {"rest", "fhir", "sftp", "http", "https"}:
                return "observed"
            if substrate not in local_substrates:
                return "unknown"
    # A native target says where input was delivered, not whether this process
    # or one of its integrations opened a socket. Until an explicit network
    # observer proves the negative, absence of an observed call remains unknown.
    return report.external_network_calls
