"""Runtime hooks that connect the v3 transducer modules to ``SimplexTransducer``.

``SimplexTransducer`` owns one ``TransducerV3Hooks``. Each hook is a no-op until
the matching optional argument is supplied, so existing callers see no change.

Hooks:
- shell gate: classify bash/shell actions, block destructive, oracle-tampering
  and exfiltrating commands, and mark scoped mutations as commit boundaries.
- egress gate: strip out-of-scope outbound items without an extra model call,
  or repair once and keep the better of the two lists.
- done gate: oracle-file integrity, regression tests, and field read-back diff.
- stall detection: feed ``NoProgressDetector`` and escalate to a human.
- macros: record, replay and invalidate verified commit macros.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openadapt.transducer_cow_container import run_probes, synthesize_probes
from openadapt.transducer_dual_egress import EgressDecision
from openadapt.transducer_form_batch import (
    FormFieldWrite,
    FormSubmitPlan,
    evaluate_readback_against_sop,
    validate_form_plan,
)
from openadapt.transducer_macros import (
    CompiledCommitMacro,
    MacroStore,
    NoProgressDetector,
    StallRecoveryPlan,
    distill_verified_trace,
    instantiate_macro,
)
from openadapt.transducer_obligations import (
    CompiledFieldObligation,
    MicroHandoffResolution,
    MicroHandoffTicket,
    ObligationStatus,
    apply_human_resolution,
    build_micro_handoff,
    compile_obligations_from_spec,
    evaluate_readback_with_completeness,
    mark_drafted_from_action,
    merge_obligations_from_document,
)
from openadapt.transducer_shell_and_egress import (
    ImmutableOracleGuard,
    PostEditTestGate,
    classify_file_write,
    classify_shell_command,
    evaluate_outbound_items,
    is_single_turn_answer_task,
    select_best_egress,
)
from openadapt.transducer_transactions import TransactionalWorkspaceGuard

if TYPE_CHECKING:  # pragma: no cover
    from openadapt.simplex_transducer import EpisodeState, SimplexTransducer

SHELL_ACTION_TYPES = ("bash", "shell", "exec", "command")
_EDIT_ACTION_TYPES = frozenset(
    {"write", "write_file", "edit", "edit_file", "str_replace", "apply_patch"}
)
_MAX_HANDOFFS = 3


def _no_exec(cmd: str) -> tuple[int, str, str]:
    return 1, "", "no container_exec_fn configured"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _raw(action: Any) -> dict[str, Any]:
    raw = _get(action, "raw_action")
    if isinstance(raw, dict):
        return raw
    return action if isinstance(action, dict) else {}


def _type(action: Any) -> str:
    kind = _get(action, "type", "")
    return str(getattr(kind, "value", kind) or "").strip().lower()


def observation_state_prefix(observation: Any) -> str:
    """Compact screen identity: URL path plus a hash of the DOM state.

    Empty when the observation carries neither. Used in commit fingerprints so
    the same click on two different screens is not seen as a duplicate.
    """
    if observation is None:
        return ""
    url = str(_get(observation, "url", "") or "")
    path = url.split("#", 1)[0].split("?", 1)[0]
    if "://" in path:
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    raw_obs = _get(observation, "raw_observation")
    dom: Any = None
    if isinstance(raw_obs, Mapping):
        dom = raw_obs.get("dom_state_hash") or raw_obs.get("dom_elements")
    if dom is None:
        dom = _get(observation, "dom_elements")
    if not dom:
        return path
    blob = dom if isinstance(dom, str) else json.dumps(dom, sort_keys=True, default=str)
    return f"{path}#{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def _write_target(action: Any) -> tuple[str, str] | None:
    """``(path, content)`` for a write/edit-file action, else ``None``."""
    if _type(action) not in ("write_file", "edit_file"):
        return None
    raw = _raw(action)
    path = raw.get("path") or raw.get("file_path") or _get(action, "target_name")
    content = (
        raw.get("content")
        or raw.get("new_str")
        or raw.get("new_string")
        or _get(action, "text")
    )
    return str(path or ""), str(content or "")


def _rewrite_payload(action: Any, items: Sequence[str]) -> Any | None:
    """Return a copy of ``action`` whose outbound payload is ``items``."""

    def fit(old: Any) -> Any:
        return list(items) if isinstance(old, list) else " ".join(items)

    out = copy.deepcopy(action)
    raw = _get(out, "raw_action")
    holders = [raw] if isinstance(raw, dict) else []
    if isinstance(out, dict):
        holders.append(out)
    for holder in holders:
        parsed = holder.get("parsed_json")
        if isinstance(parsed, dict) and parsed.get("shared_content") is not None:
            parsed["shared_content"] = fit(parsed["shared_content"])
            return out
        if holder.get("shared_content") is not None:
            holder["shared_content"] = fit(holder["shared_content"])
            return out
    for attr in ("answer", "text"):
        if isinstance(_get(out, attr), str) and not isinstance(out, dict):
            setattr(out, attr, " ".join(items))
            return out
    return None


def _items(payload: Any) -> list[str] | None:
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        return [str(p) for p in payload]
    return None


@dataclass
class V3EpisodeState:
    """Per-``(run_id, task_id)`` state for the v3 hooks."""

    started: bool = False
    oracle_snapshot: dict[str, str] | None = None
    obligations: list[CompiledFieldObligation] = field(default_factory=list)
    detector: NoProgressDetector = field(default_factory=NoProgressDetector)
    mutated: bool = False
    regression_passed: bool = False
    egress_clean: bool = False
    handoffs: int = 0
    handoff_patches: list[dict[str, Any]] = field(default_factory=list)
    stalls: list[StallRecoveryPlan] = field(default_factory=list)
    tx_guard: TransactionalWorkspaceGuard | None = None
    intake_docs: list[str] = field(default_factory=list)
    egress_handled: bool = False
    container_baseline: dict[str, str] | None = None
    os_probes: list[Any] = field(default_factory=list)
    container_violated: bool = False


class TransducerV3Hooks:
    """Optional v3 supervision hooks bound to one ``SimplexTransducer``."""

    def __init__(
        self,
        owner: SimplexTransducer,
        *,
        macro_store: MacroStore | None = None,
        regression_test_runner: Callable[[], tuple[bool, str]] | None = None,
        protected_oracle_paths: Collection[str] = (),
        workspace_root: str = "",
        item_auditor: Callable[[str, str], bool] | None = None,
        on_stall_escalate: Callable[[StallRecoveryPlan], None] | None = None,
        micro_handoff_resolver: Callable[
            [MicroHandoffTicket], MicroHandoffResolution | None
        ]
        | None = None,
        completeness_auditor: Callable[[str, str], Mapping[str, Any]] | None = None,
        container_tx: Any = None,
        container_exec_fn: Callable[[str], tuple[int, str, str]] | None = None,
        sop_contract: Any = None,
        dual_egress: Any = None,
        compounding_handoff: Any = None,
    ) -> None:
        self.owner = owner
        self.container_tx = container_tx
        self.container_exec_fn = container_exec_fn
        self.sop_contract = sop_contract
        self.dual_egress = dual_egress
        self.compounding_handoff = compounding_handoff
        self.completeness_auditor = completeness_auditor
        self.macro_store = macro_store
        self.regression_test_runner = regression_test_runner
        self.protected_oracle_paths = tuple(protected_oracle_paths)
        self.workspace_root = workspace_root
        self.item_auditor = item_auditor
        self.on_stall_escalate = on_stall_escalate
        self.micro_handoff_resolver = micro_handoff_resolver
        if macro_store is not None:
            macro_store.isolation_mode = owner.config.isolation_mode

    # -- per-episode state ------------------------------------------------
    def ep(self, state: EpisodeState) -> V3EpisodeState:
        with state._lock:
            if state.v3_ep is None:
                state.v3_ep = V3EpisodeState()
            return state.v3_ep  # type: ignore[no-any-return]

    def _instruction(self, state: EpisodeState, task: Any) -> str:
        raw_cfg = _get(task, "raw_config")
        if isinstance(raw_cfg, Mapping) and raw_cfg.get("task_prompt"):
            return str(raw_cfg["task_prompt"])
        return (
            state.task_instruction_text
            or state.task_instruction_indexed
            or str(_get(task, "instruction", "") or "")
        )

    # -- context (every step) ---------------------------------------------
    def on_context(self, state: EpisodeState, observation: Any, task: Any) -> None:
        ep = self.ep(state)
        if not ep.started:
            self._start(state, task, ep)
        else:
            self._check_oracle_after_step(state, observation, ep)
            self._check_container_after_step(state, observation, ep)
        ep.egress_handled = self.handles_egress(observation, task)
        raw_obs = _get(observation, "raw_observation")
        doc = raw_obs.get("document_text") if isinstance(raw_obs, Mapping) else None
        if isinstance(doc, str) and doc.strip() and doc not in ep.intake_docs:
            ep.intake_docs.append(doc)
            ep.obligations = merge_obligations_from_document(ep.obligations, doc)

    def _start(self, state: EpisodeState, task: Any, ep: V3EpisodeState) -> None:
        ep.started = True
        if self.workspace_root:
            root = Path(self.workspace_root)
            if root.is_dir():
                ep.oracle_snapshot = ImmutableOracleGuard.snapshot_directory(root)
                ep.tx_guard = TransactionalWorkspaceGuard(root)
                ep.tx_guard.begin_transaction()
        if self.container_tx is not None:
            ep.container_baseline = self.container_tx.begin()
            ep.os_probes = synthesize_probes(
                self._instruction(state, task),
                baseline_files=ep.container_baseline,
                watched_roots=self.container_tx.watched_roots,
                baseline_tar=self.container_tx.baseline_tar,
                protected_patterns=self.container_tx.protected_patterns,
            )
        raw_cfg = _get(task, "raw_config")
        fields = (
            raw_cfg.get("structured_fields") if isinstance(raw_cfg, Mapping) else None
        )
        if fields or self.owner.config.enable_obligation_compiler:
            ep.obligations = compile_obligations_from_spec(
                self._instruction(state, task),
                fields if isinstance(fields, Mapping) else None,
            )

    def _check_container_after_step(
        self, state: EpisodeState, observation: Any, ep: V3EpisodeState
    ) -> None:
        """After a mutating step, undo changes to protected container paths."""
        tx = self.container_tx
        if tx is None or not ep.mutated or ep.container_baseline is None:
            return
        report = tx.inspect_step_diff()
        if not report.protected_violations:
            return
        ep.container_violated = True
        bad = list(report.protected_violations)
        restored = tx.rollback_paths(bad)
        self.owner._deliver_intervention(
            state,
            kind="TRAP_REPAIR_HINT",
            hint=(
                "TRAP_REPAIR_HINT (CONTAINER_PROTECTED_TAMPER_ROLLED_BACK): "
                f"protected paths {bad} were changed and have been restored "
                f"({restored}). Do not touch them."
            ),
            delta_note="container_protected_tamper_rolled_back",
            fast_forward_obs=observation,
        )

    def _form_gate(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        """Validate a whole ``fill_form`` plan before any field is written."""
        raw = _raw(action)
        spec = raw.get("form_plan")
        fields = spec.get("fields") if isinstance(spec, Mapping) else None
        fields = fields or raw.get("fields")
        if not isinstance(fields, Sequence) or isinstance(fields, str):
            return action
        plan = FormSubmitPlan(
            fields=[
                FormFieldWrite(
                    selector=str(f.get("selector", "")),
                    field_name=str(f.get("field_name") or f.get("name") or ""),
                    value=str(f.get("value", "")),
                    exact_code=bool(f.get("exact_code", False)),
                )
                for f in fields
                if isinstance(f, Mapping)
            ],
            submit_selector=str(
                (spec or {}).get("submit_selector", "")
                if isinstance(spec, Mapping)
                else ""
            ),
        )
        res = validate_form_plan(
            plan,
            pinned_entities=self.owner.pinned,
            required_obligations=self.ep(state).obligations,
        )
        if res.ok:
            return action
        self.owner._deliver_intervention(
            state,
            kind="TRAP_REPAIR_HINT",
            hint=(
                f"TRAP_REPAIR_HINT (FORM_PLAN_INVALID): {res.reason} "
                f"Missing: {res.missing_fields}. Mismatched: {res.mismatched_fields}. "
                f"Blank: {res.blank_fields}."
            ),
            delta_note="form_plan_invalid",
            fast_forward_obs=observation,
        )
        return self.owner._call_inner_act(observation, task, history)

    def _check_oracle_after_step(
        self, state: EpisodeState, observation: Any, ep: V3EpisodeState
    ) -> None:
        """Right after a mutating step, undo any change to test/evaluator files."""
        if not (ep.mutated and ep.oracle_snapshot and ep.tx_guard):
            return
        intact, bad = ImmutableOracleGuard.verify_intact(
            Path(self.workspace_root), ep.oracle_snapshot
        )
        if intact:
            return
        restored = ep.tx_guard.rollback_paths(bad)
        self.owner._deliver_intervention(
            state,
            kind="TRAP_REPAIR_HINT",
            hint=(
                "TRAP_REPAIR_HINT (ORACLE_TAMPER_ROLLED_BACK): test or evaluator "
                f"files {bad} were changed and have been restored ({restored}). "
                "Fix the code under test instead."
            ),
            delta_note="oracle_tamper_rolled_back",
            fast_forward_obs=observation,
        )

    def handles_egress(self, observation: Any, task: Any) -> bool:
        """True when ``egress_gate`` is the single outbound check for this step.

        That is the case when an ``item_auditor`` judges every item. Containers
        alone do not qualify: the legacy scope check also catches outlier paths
        and entities that the container-name match in ``egress_gate`` misses.
        """
        return self.item_auditor is not None or self.dual_egress is not None

    def legacy_egress_skipped(self, state: EpisodeState) -> bool:
        return self.ep(state).egress_handled

    # -- shell gate -------------------------------------------------------
    @staticmethod
    def shell_command(action: Any) -> str | None:
        if _type(action) not in SHELL_ACTION_TYPES:
            return None
        raw = _raw(action)
        for value in (_get(action, "text"), raw.get("command"), raw.get("cmd")):
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def shell_gate(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        """Block unsafe shell commands; mark scoped mutations as commits."""
        owner = self.owner
        if _type(action) == "fill_form":
            return self._form_gate(state, action, observation, task, history)
        for _ in range(owner.config.max_repairs_per_commit + 2):
            command = self.shell_command(action)
            write = _write_target(action)
            if command is None and write is None:
                return action
            if write is not None:
                result = classify_file_write(
                    write[0],
                    write[1],
                    task_instruction=self._instruction(state, task),
                    protected_oracle_paths=self.protected_oracle_paths,
                    workspace_root=self.workspace_root,
                )
            else:
                result = classify_shell_command(
                    command or "",
                    task_instruction=self._instruction(state, task),
                    protected_oracle_paths=self.protected_oracle_paths,
                    workspace_root=self.workspace_root,
                )
            if not result.blocked:
                if result.is_commit or write is not None:
                    ep = self.ep(state)
                    ep.mutated = True
                    if ep.tx_guard is not None:
                        ep.tx_guard.snapshot_step()
                    return action if write is not None else self._as_commit(action)
                return action
            kind, outcome = owner._handle_trap(
                state=state,
                action=action,
                observation=observation,
                task=task,
                history=history,
                trap_code=result.trap_code or "DESTRUCTIVE_PRIVILEGED_COMMAND",
                reason=result.reason,
                commit_index=state.next_commit_index,
            )
            if kind != "repaired":
                return outcome
            action = outcome
        return owner._abstain_action(
            state,
            trap_code="DESTRUCTIVE_PRIVILEGED_COMMAND",
            reason="Blocked shell command was re-issued after the repair budget.",
            commit_index=state.next_commit_index,
        )

    @staticmethod
    def _as_commit(action: Any) -> Any:
        marked = copy.deepcopy(action)
        raw = _raw(marked)
        if raw is not None and isinstance(_get(marked, "raw_action", marked), dict):
            raw["is_commit"] = True
        return marked

    # -- after every step -------------------------------------------------
    def after_step(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
        action: Any,
    ) -> None:
        """Track mutations and drafts; feed the stall detector."""
        ep = self.ep(state)
        kind = _type(action)
        raw = _raw(action)
        if kind in _EDIT_ACTION_TYPES or raw.get("fs_write") is True:
            ep.mutated = True
        if ep.obligations and kind in ("fill", "type", "input", "set_value"):
            text = _get(action, "text") or raw.get("value") or raw.get("text") or ""
            mark_drafted_from_action(ep.obligations, str(text))
        if self.owner._is_terminal_action(action):
            return
        raw_obs = _get(observation, "raw_observation")
        dom = raw_obs.get("dom_elements") if isinstance(raw_obs, Mapping) else None
        shot = _get(observation, "screenshot")
        plan = ep.detector.observe_step(
            str(_get(observation, "url", "") or ""),
            dom,
            shot if isinstance(shot, bytes) else None,
            {
                "action": kind,
                "selector": str(
                    raw.get("selector") or _get(action, "target_name") or ""
                ),
                "value": str(_get(action, "text") or raw.get("value") or ""),
            },
        )
        if plan is not None and plan.is_stalled:
            self._on_stall(state, observation, ep, plan)

    def _on_stall(
        self,
        state: EpisodeState,
        observation: Any,
        ep: V3EpisodeState,
        plan: StallRecoveryPlan,
    ) -> None:
        ep.stalls.append(plan)
        hint = plan.hint
        if plan.surfaced_form_candidates:
            names = [
                str(c.get("selector", "")) for c in plan.surfaced_form_candidates[:5]
            ]
            hint += f" Surfaced form controls: {', '.join(n for n in names if n)}."
        self.owner._deliver_intervention(
            state,
            kind="NO_PROGRESS_LOOP",
            hint=hint,
            delta_note=f"stall_count={plan.stall_count}",
            fast_forward_obs=observation,
        )
        if self.on_stall_escalate is not None and (
            plan.escalate_effort or plan.switch_to_vision
        ):
            self.on_stall_escalate(plan)
        if self.compounding_handoff is not None and not plan.escalate_effort:
            self._compound(state, observation, ep, plan)
        elif (
            self.micro_handoff_resolver is not None
            and not plan.escalate_effort
            and ep.handoffs < _MAX_HANDOFFS
        ):
            self._handoff(state, observation, ep, plan)

    def _compound(
        self,
        state: EpisodeState,
        observation: Any,
        ep: V3EpisodeState,
        plan: StallRecoveryPlan,
    ) -> None:
        resolver = self.micro_handoff_resolver
        if resolver is None or ep.handoffs >= _MAX_HANDOFFS:
            return
        ep.handoffs += 1

        def human(ticket: MicroHandoffTicket) -> MicroHandoffResolution:
            res = resolver(ticket)
            if res is None:
                raise LookupError("no human resolution")
            return res

        url = str(_get(observation, "url", "") or "")
        try:
            out = self.compounding_handoff.handle_stall(
                run_id=state.run_id,
                task_id=state.task_id,
                task_kind="stall",
                origin_path_pattern=url,
                reason=plan.hint,
                blocker_kind="no_progress",
                missing_obligations=[
                    o
                    for o in ep.obligations
                    if o.status is not ObligationStatus.VERIFIED
                ],
                stalled_selectors=plan.blacklisted_selectors,
                human_resolver=human,
            )
        except LookupError:
            return
        if out.autonomy == "AVC_H":
            with state._lock:
                state.human_touches += 1
        patch = out.patch or {}
        ep.handoff_patches.append(patch)
        hints = [h["selector"] for h in patch.get("selector_hints", [])]
        if out.completed:
            self.owner._deliver_intervention(
                state,
                kind="MICRO_HANDOFF_RESOLVED",
                hint=(
                    "A stored fix for this blocker applies. "
                    + (f"Use: {', '.join(hints)}." if hints else "")
                ).strip(),
                delta_note=f"autonomy={out.autonomy}",
                fast_forward_obs=observation,
            )

    def _handoff(
        self,
        state: EpisodeState,
        observation: Any,
        ep: V3EpisodeState,
        plan: StallRecoveryPlan,
    ) -> None:
        ep.handoffs += 1
        ticket = build_micro_handoff(
            run_id=state.run_id,
            task_id=state.task_id,
            reason=plan.hint,
            blocker_kind="no_progress",
            current_url=str(_get(observation, "url", "") or ""),
            missing_obligations=[
                o for o in ep.obligations if o.status is not ObligationStatus.VERIFIED
            ],
            stalled_selectors=plan.blacklisted_selectors,
        )
        assert self.micro_handoff_resolver is not None
        resolution = self.micro_handoff_resolver(ticket)
        if resolution is None:
            return
        with state._lock:
            state.human_touches += 1
        patch = apply_human_resolution(ticket, resolution)
        ep.handoff_patches.append(patch)
        if patch["applied"]:
            hints = [h["selector"] for h in patch["selector_hints"]]
            self.owner._deliver_intervention(
                state,
                kind="MICRO_HANDOFF_RESOLVED",
                hint=(
                    "A human resolved the blocker. "
                    + (f"Use: {', '.join(hints)}. " if hints else "")
                    + (
                        f"Avoid: {', '.join(patch['avoid_selectors'])}."
                        if patch["avoid_selectors"]
                        else ""
                    )
                ).strip(),
                delta_note=f"ticket={ticket.ticket_id}",
                fast_forward_obs=observation,
            )

    # -- egress gate ------------------------------------------------------
    def _containers(self, observation: Any, task: Any) -> Mapping[str, Any] | None:
        raw_obs = _get(observation, "raw_observation")
        for src in (raw_obs, observation, _get(task, "raw_config")):
            found = _get(src, "containers") if src is not None else None
            if isinstance(found, Mapping):
                return found
        return None

    def egress_gate(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
    ) -> Any:
        """Strip out-of-scope outbound items, or repair once, never losing items."""
        items = _items(self.owner._extract_outbound_payload(action))
        containers = self._containers(observation, task)
        if items is not None and self.dual_egress is not None:
            return self._dual_egress_gate(
                state, action, items, containers, observation, task
            )
        if items is None or (containers is None and self.item_auditor is None):
            return action
        ep = self.ep(state)
        with state._lock:
            anchors = list(state.required_task_anchors)
        prompt = self._instruction(state, task)
        kwargs: dict[str, Any] = {
            "task_prompt": prompt,
            "containers": containers,
            "required_anchors": anchors,
            "item_auditor": self.item_auditor,
        }
        result = evaluate_outbound_items(items, **kwargs)
        if result.action == "ALLOW":
            ep.egress_clean = True
            return action
        if result.action == "STRIP_MONOTONIC":
            rewritten = _rewrite_payload(action, result.safe_items)
            ep.egress_clean = rewritten is not None
            return rewritten if rewritten is not None else action
        if result.action == "ABSTAIN":
            return self._abstain_egress(
                state, action, observation, task, history, kwargs, result.reason
            )
        if result.action != "REPAIR_ONCE":
            return action
        self.owner._deliver_intervention(
            state,
            kind="EGRESS_REPAIR",
            hint=f"EGRESS_REPAIR: {result.reason}. Include these exactly: {result.missing_anchors}.",
            delta_note="egress_repair_once",
            fast_forward_obs=observation,
        )
        repaired = self.owner._call_inner_act(observation, task, history)
        items2 = _items(self.owner._extract_outbound_payload(repaired)) or []
        best = select_best_egress(
            items,
            items2,
            required_anchors=anchors,
            flagged_bleed_items=result.stripped_items,
        )
        base = repaired if items2 else action
        out = _rewrite_payload(base, best)
        if out is None:
            return action
        ep.egress_clean = not evaluate_outbound_items(best, **kwargs).missing_anchors
        return out

    def _dual_egress_gate(
        self,
        state: EpisodeState,
        action: Any,
        items: list[str],
        containers: Mapping[str, Any] | None,
        observation: Any,
        task: Any,
    ) -> Any:
        ep = self.ep(state)
        with state._lock:
            anchors = list(state.required_task_anchors)
        report = self.dual_egress.verify_outbound(
            items,
            task_prompt=self._instruction(state, task),
            containers=containers,
            required_anchors=anchors,
        )
        rewritten = (
            _rewrite_payload(action, report.final_items) if report.final_items else None
        )
        if rewritten is None and (not report.final_items or report.stripped_items):
            ep.egress_clean = False
            return self.owner._abstain_action(
                state,
                trap_code="CONTEXTUAL_SCOPE_BLEED",
                reason=f"Outbound egress refused: {report.reason}",
                commit_index=state.next_commit_index,
            )
        ep.egress_clean = (
            report.decision
            in (EgressDecision.ALLOW_VERIFIED, EgressDecision.STRIP_VERIFIED)
            and rewritten is not None
        )
        return rewritten if rewritten is not None else action

    def _abstain_egress(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
        kwargs: dict[str, Any],
        reason: str,
    ) -> Any:
        """No item is in scope: let the legacy scope check repair or strip, else halt."""
        owner = self.owner
        ep = self.ep(state)
        ep.egress_handled = False  # re-enable the legacy check for this one pass
        fixed = owner._check_and_repair_commit(
            state=state,
            action=action,
            observation=observation,
            task=task,
            history=history,
            commit_index=state.next_commit_index,
        )
        if _get(fixed, "is_abstained"):
            return fixed
        items = _items(owner._extract_outbound_payload(fixed)) or []
        again = evaluate_outbound_items(items, **kwargs) if items else None
        if again is not None and again.action == "ALLOW":
            ep.egress_clean = True
            return fixed
        if again is not None and again.action == "STRIP_MONOTONIC":
            rewritten = _rewrite_payload(fixed, again.safe_items)
            if rewritten is not None:
                ep.egress_clean = True
                return rewritten
        return owner._abstain_action(
            state,
            trap_code="CONTEXTUAL_SCOPE_BLEED",
            reason=f"Outbound egress blocked: {reason}.",
            commit_index=state.next_commit_index,
        )

    # -- done gate --------------------------------------------------------
    def readback(
        self, state: EpisodeState, observation: Any, task: Any
    ) -> dict[str, Any] | None:
        raw_obs = _get(observation, "raw_observation")
        text = raw_obs.get("readback_text") if isinstance(raw_obs, Mapping) else None
        if text is None and self.owner.rung2_verifier is not None:
            res = self.owner.rung2_verifier(observation, task, state)
            if isinstance(res, Mapping) and res.get("readback_text") is not None:
                return dict(res)
        if text is None:
            return None
        return {
            "readback_text": text,
            "is_editable": bool(raw_obs.get("is_editable")),
            "is_toast": bool(raw_obs.get("is_toast")),
        }

    def pre_done(
        self,
        state: EpisodeState,
        action: Any,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
        remaining_steps: int,
        commit_idx: int,
    ) -> Any | None:
        """Gate ``DONE``. Return the action to emit, or ``None`` to continue."""
        owner = self.owner
        ep = self.ep(state)
        action = self.egress_gate(state, action, observation, task, history)

        if ep.oracle_snapshot is not None:
            intact, bad = ImmutableOracleGuard.verify_intact(
                Path(self.workspace_root), ep.oracle_snapshot
            )
            if not intact:
                self._rollback_all(ep)
                return owner._abstain_action(
                    state,
                    trap_code="ORACLE_TAMPER",
                    reason=f"Test or evaluator files changed after Step 0: {bad}",
                    commit_index=commit_idx,
                )

        budget = remaining_steps >= owner.config.min_budget_for_ui_reopen
        if self.regression_test_runner is not None and ep.mutated:
            ok, summary = PostEditTestGate.verify_regression(
                self.regression_test_runner
            )
            ep.regression_passed = ok
            if not ok:
                held = self._hold(
                    state, observation, task, history, budget,
                    commit_idx + 20_000, "REGRESSION_TEST_FAILED",
                    f"HOLD_DONE (REGRESSION_TEST_FAILED): existing tests fail. Fix before DONE.\n{summary}",
                )  # fmt: skip
                if held is not None:
                    return held
                self._rollback_all(ep)
                return owner._abstain_action(
                    state,
                    trap_code="REGRESSION_TEST_FAILED",
                    reason=f"Existing tests still fail at DONE: {summary[:500]}",
                    commit_index=commit_idx,
                )

        container_ok = False
        tx = self.container_tx
        if tx is not None and self.regression_test_runner is None:
            report = tx.inspect_step_diff()
            probes = run_probes(ep.os_probes, self.container_exec_fn or _no_exec)
            if report.protected_violations or not probes.all_passed:
                ep.container_violated = bool(report.protected_violations)
                why = (
                    f"protected paths changed: {report.protected_violations}; "
                    f"failed probes: {probes.failed_details}"
                )
                held = self._hold(
                    state, observation, task, history, budget,
                    commit_idx + 30_000, "CONTAINER_POSTCONDITION_FAILED",
                    f"HOLD_DONE (CONTAINER_POSTCONDITION_FAILED): {why}",
                )  # fmt: skip
                if held is not None:
                    return held
                tx.rollback_all()
                return owner._abstain_action(
                    state,
                    trap_code="CONTAINER_POSTCONDITION_FAILED",
                    reason=f"Container check failed at DONE: {why[:500]}",
                    commit_index=commit_idx,
                )
            container_ok = True

        sop_ok = False
        if self.sop_contract is not None:
            back = self.readback(state, observation, task)
            if back is not None:
                sop_res = evaluate_readback_against_sop(
                    self.sop_contract,
                    str(back["readback_text"]),
                    is_editable=bool(back.get("is_editable")),
                    is_toast=bool(back.get("is_toast")),
                )
                if not sop_res.passed:
                    hint = f"HOLD_DONE (SOP_RULES_MISSING): {sop_res.repair_hint}"
                    held = self._hold(
                        state, observation, task, history, budget,
                        commit_idx + 40_000, "SOP_RULES_MISSING", hint,
                    )  # fmt: skip
                    if held is not None:
                        return held
                    self._release_unverified(state, commit_idx, sop_res.reason)
                    return action
                sop_ok = True

        if ep.obligations or self.completeness_auditor is not None:
            back = self.readback(state, observation, task)
            if back is not None:
                intake = "\n\n".join([self._instruction(state, task), *ep.intake_docs])
                diff = evaluate_readback_with_completeness(
                    ep.obligations,
                    str(back["readback_text"]),
                    intake_context=intake,
                    is_editable=bool(back.get("is_editable")),
                    is_toast=bool(back.get("is_toast")),
                    completeness_auditor=self.completeness_auditor,
                )
                if not diff.all_verified:
                    held = self._hold(
                        state, observation, task, history, budget,
                        commit_idx + 10_000, "FIELD_OBLIGATIONS_MISSING", diff.reason,
                    )  # fmt: skip
                    if held is not None:
                        return held
                    self._release_unverified(state, commit_idx, diff.reason)
                    return action
                self._verified(
                    state, commit_idx, 1, "persisted_state_reacquisition",
                    "All compiled field obligations found in the saved read-back.",
                )  # fmt: skip
                return action

        if sop_ok:
            self._verified(
                state, commit_idx, 1, "persisted_state_reacquisition",
                "Saved read-back satisfies every declarative SOP rule.",
            )  # fmt: skip
            return action
        if container_ok:
            self._verified(
                state, commit_idx, 2, "independent_system_of_record",
                "Container CoW diff clean and synthesized OS postcondition probes passed.",
            )  # fmt: skip
            return action
        if ep.regression_passed:
            self._verified(
                state, commit_idx, 2, "independent_system_of_record",
                "Existing regression tests pass after the edits.",
            )  # fmt: skip
            return action
        if (
            ep.tx_guard is not None
            and not ep.mutated
            and not ep.tx_guard.has_workspace_changes()
        ):
            self._verified(
                state, commit_idx, 2, "independent_system_of_record",
                "Read-only / clean workspace verified with zero unauthorized mutations.",
            )  # fmt: skip
            return action
        if ep.egress_clean and is_single_turn_answer_task(observation, action):
            self._verified(
                state, commit_idx, 0, "immediate_screen_confirmation",
                "Outbound items passed the provenance and scope check.",
            )  # fmt: skip
            return action
        return None

    def _rollback_all(self, ep: V3EpisodeState) -> None:
        if self.container_tx is not None:
            self.container_tx.rollback_all()
        if ep.tx_guard is not None:
            ep.tx_guard.rollback_all()

    def finalize_episode(
        self,
        run_id: str,
        task_id: str,
        *,
        tests_passed: bool | None = None,
    ) -> dict[str, Any]:
        """End an episode: roll back on tampered oracles or failing tests, then clean up."""
        ep = self.ep(self.owner.get_state(run_id, task_id))
        tampered: list[str] = []
        if ep.oracle_snapshot is not None:
            _, tampered = ImmutableOracleGuard.verify_intact(
                Path(self.workspace_root), ep.oracle_snapshot
            )
        if (
            tests_passed is None
            and self.regression_test_runner is not None
            and ep.mutated
        ):
            tests_passed = PostEditTestGate.verify_regression(
                self.regression_test_runner
            )[0]
        rolled_back = False
        if ep.tx_guard is not None and (tampered or tests_passed is False):
            rolled_back = ep.tx_guard.rollback_all()
        if ep.tx_guard is not None:
            ep.tx_guard.cleanup()
            ep.tx_guard = None
        if self.container_tx is not None and ep.container_baseline is not None:
            if tests_passed is False or ep.container_violated:
                rolled_back = self.container_tx.rollback_all() or rolled_back
            self.container_tx.cleanup()
            ep.container_baseline = None
        return {
            "restored": rolled_back,
            "oracle_intact": not tampered,
            "tampered_paths": list(tampered),
            "tests_passed": tests_passed,
        }

    def skip_ui_reopen(self, observation: Any, action: Any) -> bool:
        return is_single_turn_answer_task(observation, action)

    def _hold(
        self,
        state: EpisodeState,
        observation: Any,
        task: Any,
        history: Sequence[Any] | None,
        budget: bool,
        key: int,
        code: str,
        hint: str,
    ) -> Any | None:
        owner = self.owner
        used = state.repairs_by_commit.get(key, 0)
        if not (
            owner.config.enable_repair_hints
            and budget
            and used < owner.config.max_repairs_per_commit
        ):
            return None
        with state._lock:
            state.repairs_by_commit[key] = used + 1
        owner._deliver_intervention(
            state,
            kind="HOLD_DONE_REPAIR",
            hint=hint,
            delta_note=f"hold_done={code};attempt={used + 1}",
            fast_forward_obs=observation,
        )
        repaired = owner._call_inner_act(observation, task, history)
        if owner._is_terminal_action(repaired):
            # Re-gate the repaired DONE; bounded by max_repairs_per_commit.
            return owner._verify_before_done(
                state, repaired, observation, task, history
            )
        return repaired

    @staticmethod
    def _verified_outcome() -> Any:
        from openadapt.simplex_transducer import ReceiptOutcome

        return ReceiptOutcome.VERIFIED.value

    def _verified(
        self,
        state: EpisodeState,
        commit_idx: int,
        tier: Any,
        strength: str,
        reason: str,
    ) -> Any:
        self.owner._mark_verified(state, commit_idx, tier=tier)
        return self.owner._emit_receipt(
            state=state,
            outcome=self._verified_outcome(),
            oracle_tier=tier,
            observed_effect_strength=strength,
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=True,
            effect_passed=True,
            delivery_uncertain=False,
            commit_index=commit_idx,
            reason=reason,
        )

    def _release_unverified(
        self, state: EpisodeState, commit_idx: int, reason: str
    ) -> None:
        self.owner._emit_receipt(
            state=state,
            outcome="COMPLETED_UNVERIFIED",
            oracle_tier=0,
            observed_effect_strength=None,
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=False,
            effect_passed=False,
            delivery_uncertain=True,
            commit_index=commit_idx,
            reason=reason,
        )

    # -- macros -----------------------------------------------------------
    def _store(self, run_id: str) -> MacroStore | None:
        if self.macro_store is not None:
            self.macro_store.run_id = run_id
        return self.macro_store

    def record_verified_macro(
        self,
        run_id: str,
        task_id: str,
        trace: Sequence[Mapping[str, Any]],
        payload: Mapping[str, str],
        *,
        route_pattern: str = "",
        task_kind: str = "",
        require_verified: bool = True,
    ) -> CompiledCommitMacro | None:
        store = self._store(run_id)
        if store is None:
            return None
        if require_verified:
            state = self.owner.get_state(run_id, task_id)
            if (
                not state.receipts
                or state.receipts[-1].outcome != self._verified_outcome()
            ):
                return None
        macro = distill_verified_trace(trace, payload, route_pattern)
        if macro is not None:
            store.put(
                macro,
                task_id=task_id,
                origin_path_pattern=route_pattern,
                task_kind=task_kind,
            )
        return macro

    def try_instantiate_macro(
        self,
        run_id: str,
        task_id: str,
        next_payload: Mapping[str, str],
        *,
        route_pattern: str = "",
        task_kind: str = "",
    ) -> tuple[CompiledCommitMacro, list[dict[str, Any]]] | None:
        store = self._store(run_id)
        if store is None:
            return None
        macro = store.get(
            task_id=task_id, origin_path_pattern=route_pattern, task_kind=task_kind
        )
        if macro is None:
            return None
        steps = instantiate_macro(macro, next_payload)
        return (macro, steps) if steps else None

    def record_macro_verified_receipt(
        self, run_id: str, task_id: str, macro_id: str, reason: str = ""
    ) -> Any:
        """Signed ``VERIFIED`` receipt for a completion replayed from a macro."""
        state = self.owner.get_state(run_id, task_id)
        return self._verified(
            state,
            max(1, state.next_commit_index - 1),
            1,
            "persisted_state_reacquisition",
            f"Macro {macro_id} replayed and verified. {reason}".strip(),
        )

    def record_macro_outcome(self, macro_id: str, succeeded: bool) -> bool:
        if self.macro_store is None:
            return False
        return self.macro_store.record_macro_outcome(macro_id, succeeded)
