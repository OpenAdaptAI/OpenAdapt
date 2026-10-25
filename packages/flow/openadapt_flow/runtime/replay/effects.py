# mypy: disable-error-code="empty-body"
"""Effect checks and read-back, including pipelined read-back: did the system of record change as declared.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_EffectChecksHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional  # noqa: F401

from openadapt_flow.ir import EffectVerificationEvidence, HaltObservation, StepResult
from openadapt_flow.qualification_faults import effect_verifier_input_sha256
from openadapt_flow.runtime.authorization import runtime_param_text
from openadapt_flow.runtime.effects import EffectState, reconcile_or_escalate
from openadapt_flow.runtime.effects.adapter import _probe_adapter_connection
from openadapt_flow.runtime.effects.adapter import (
    verifier_identity as effect_verifier_identity,
)
from openadapt_flow.runtime.replay._shared import (
    _MAX_SERVER_RESPONSES,
    _effect_read_wording,
    _pipeline_safe_verifier,
    effect_endpoint_scope,
    effect_field_checks,
)
from openadapt_flow.runtime.step_timing import timed_span
from openadapt_flow.verification import (
    EffectContractMutationError,
    verifier_effect_tier,
    verify_effect_without_mutation,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal, Mapping

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import Region, RunReport, Step, Workflow
    from openadapt_flow.qualification_faults import (
        FaultMutationReceipt,
        QualificationFaultGate,
    )
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        RuntimeParamScalar,
    )
    from openadapt_flow.runtime.effects import Effect, EffectVerifier
    from openadapt_flow.runtime.readback_pipeline import (
        PipelinedReadback,
        ReadbackPipeline,
    )


class _EffectChecksHost:
    _durable_continuation_guard: Optional[Any]
    _pipelined_readback: Optional["PipelinedReadback"]
    _qualification_fault_context: Any
    _qualification_fault_mutations: list[FaultMutationReceipt]
    _readback_verifier: Any
    _require_current_fault_driver_binding: Any
    _run_id: str

    def _settle_durable_readback(
        self, entry: Any, error: Optional[str]
    ) -> Optional[str]: ...
    def _stamp_approval(self, report: RunReport) -> None: ...
    def _stamp_execution_outcome(
        self, report: RunReport, workflow: Workflow, run_dir: Path
    ) -> None: ...

    _validate_fault_mutation: Any
    backend: Backend
    checkpoint_key: Optional[str]
    durable: bool
    effect_compensator: Optional[Any]
    effect_verifier: Optional[EffectVerifier]
    governed_authorization: Optional[GovernedRunAuthorization]
    readback_pipeline: Optional["ReadbackPipeline"]


if TYPE_CHECKING:
    _Base = _EffectChecksHost
else:
    _Base = object


class EffectChecks(_Base):
    """Effect checks and read-back, including pipelined read-back: did the system of record change as declared."""

    def _may_pipeline_readback(
        self, verifier: Any, step: Optional[Step] = None
    ) -> bool:
        """Whether this effect's read-back may run on the side thread.

        A durable run defers the record check only when the durable pause
        can hold it (docs/THROUGHPUT.md): a linear run, an encrypted
        checkpoint store, and the item's last step, so nothing else in the
        item waits on the check.
        """

        pipeline = getattr(self, "readback_pipeline", None)
        if pipeline is None:
            return False
        reason = None
        if getattr(self, "_program_durable", None) is not None:
            reason = "durable program runs verify inline"
        elif self.durable and self._durable_continuation_guard is not None:
            reason = "a resumed durable run verifies inline"
        elif self.durable and not self.checkpoint_key:
            reason = (
                "a durable run without an encrypted checkpoint store verifies "
                "inline (the deferred check keeps record rows)"
            )
        elif self.durable and (
            step is None or step.id != getattr(self, "_linear_final_step_id", None)
        ):
            reason = (
                "a durable run defers only its last step's record check; this "
                "one verifies inline"
            )
        elif getattr(self, "idempotency_ledger", None) is not None:
            reason = "idempotency-ledger runs verify inline"
        elif getattr(self, "_pipelined_readback", None) is not None:
            reason = "an earlier read-back in this item is still pending"
        else:
            target = verifier if verifier is not None else self.effect_verifier
            if not _pipeline_safe_verifier(target):
                reason = (
                    "only REST, FHIR, GraphQL and SQL read-backs run alongside "
                    "the next item; this one verifies inline"
                )
        pipeline.inline_reason = reason
        return reason is None

    def _defer_readback(
        self,
        step: Step,
        before: EffectState,
        result: StepResult,
        *,
        effects: Optional[list["Effect"]],
        verifier: Any,
        actuation: Literal["gui", "api"] = "gui",
    ) -> None:
        assert self.readback_pipeline is not None
        scratch = StepResult(step_id=step.id, intent=step.intent, ok=True)
        # A durable run's check starts only once the durable pause holds it,
        # so a crash in between can't leave a sent save without its pause.
        # Whatever waits on the check also starts it.
        start = threading.Event()
        if not self.durable:
            start.set()

        def verify() -> Optional[str]:
            start.wait()
            return self._verify_effects(
                step, before, scratch, effects=effects, verifier=verifier
            )

        entry = self.readback_pipeline.submit(step.id, verify)
        entry.start = start  # type: ignore[attr-defined]
        entry.scratch = scratch  # type: ignore[attr-defined]
        entry.result = result  # type: ignore[attr-defined]
        entry.intent = step.intent  # type: ignore[attr-defined]
        # What a durable pause needs to re-run this check after a crash.
        entry.step = step  # type: ignore[attr-defined]
        entry.before = before  # type: ignore[attr-defined]
        entry.actuation = actuation  # type: ignore[attr-defined]
        entry.durable_run = None  # type: ignore[attr-defined]
        result.effect_verified = None
        result.effect_results.append("[pipelined] system-of-record read-back pending")
        self._pipelined_readback = entry

    def _join_pipelined_readback(self, *, keep: bool = False) -> Optional[str]:
        """Wait for a pending read-back, apply it to its step, return its error."""

        entry = getattr(self, "_pipelined_readback", None)
        if entry is None:
            return None
        start = getattr(entry, "start", None)
        if start is not None:
            start.set()
        try:
            error = entry.verdict.result()
        except Exception as exc:  # noqa: BLE001 - a raising verifier fails closed
            error = f"read-back raised {type(exc).__name__}; reconciliation is required"
        self._copy_pipelined_evidence(entry, error)
        if not keep:
            self._pipelined_readback = None
        return error

    def _copy_pipelined_evidence(self, entry: Any, error: Optional[str]) -> None:
        result: StepResult = entry.result
        scratch: StepResult = entry.scratch
        result.effect_results = [
            r for r in result.effect_results if not r.startswith("[pipelined]")
        ]
        result.effect_results.extend(scratch.effect_results)
        result.effect_evidence.extend(scratch.effect_evidence)
        result.effect_field_checks.extend(scratch.effect_field_checks)
        result.effect_verified = scratch.effect_verified
        if scratch.failure_category is not None:
            result.failure_category = scratch.failure_category
        if error is not None:
            result.ok = False
            result.error = error
            result.safety_halt = True
            if result.failure_category is None:
                result.failure_category = (
                    "governed_refusal"
                    if self.governed_authorization is not None
                    else "safety_halt"
                )
        else:
            result.safety_halt = result.safety_halt or scratch.safety_halt

    def _apply_pipelined_verdict(
        self,
        report: RunReport,
        error: Optional[str],
        *,
        entry: Optional["PipelinedReadback"] = None,
    ) -> None:
        if error is None:
            return
        report.success = False
        report.execution_completed = False
        report.terminal_outcome = "halt"
        report.results.append(
            StepResult(
                step_id="<terminal>",
                intent="program halt",
                ok=False,
                error=error,
                safety_halt=True,
            )
        )
        if entry is None:
            entry = getattr(self, "_pipelined_readback", None)
        report.halt = HaltObservation(
            state_id=getattr(entry, "step_id", "") or "",
            intent=getattr(entry, "intent", "") or "",
            reason=error,
            outcome="halt",
            # The screen now belongs to the next item; nothing is probed.
            observed_texts=[],
            completed_intents=[
                r.intent for r in report.results if r.ok and not r.skipped
            ],
        )

    def _settle_pipelined_readback(
        self,
        report: RunReport,
        workflow: Workflow,
        run_dir: Path,
        entry: "PipelinedReadback",
        error: Optional[str],
        *,
        decide_approval: bool = True,
    ) -> RunReport:
        """Apply a finished deferred read-back to the saved item report.

        ``decide_approval`` is False when the read-back's deadline already
        decided the item's approval; a late verdict doesn't change it.
        """

        try:
            self._copy_pipelined_evidence(entry, error)
            error = self._settle_durable_readback(entry, error)
            if error is not None:
                self._apply_pipelined_verdict(report, error, entry=entry)
            else:
                # The execution completed; it was held unsuccessful only until
                # the read-back confirmed. Re-derive the outcome from that
                # evidence.
                report.execution_completed = True
                report.success = True
            self._stamp_execution_outcome(report, workflow, run_dir)
        finally:
            # The held approval decision is always made, even when settling
            # fails: the report then still says unsuccessful, so the item goes
            # to a person, and it never stays held open for the next item.
            if decide_approval:
                self._stamp_approval(report)
        report.save(run_dir)
        return report

    def recheck_deferred_readback(
        self,
        workflow: Workflow,
        deferred: Any,
        *,
        params: Mapping[str, RuntimeParamScalar],
        run_id: str,
    ) -> StepResult:
        """Re-run a durable pause's deferred record check; send nothing.

        The check runs against the snapshot of the system of record taken
        before the save, with this replayer's system-of-record verifier.
        Returns the save's result with the verdict applied: ``ok`` when the
        record check confirmed the save.
        """

        from openadapt_flow.policy import effects_for_actuation
        from openadapt_flow.runtime.durable.approval import StateDiverged

        step = workflow.steps[deferred.step_index]
        if step.id != deferred.step_id:
            raise StateDiverged(
                "the deferred record check names a different step than the bundle"
            )
        verifier = self.effect_verifier
        if verifier is None or not _pipeline_safe_verifier(verifier):
            raise StateDiverged(
                "re-running a deferred record check needs the run's REST, FHIR, "
                "GraphQL or SQL record checker"
            )
        self._run_id = run_id
        effects = self._resolve_effects(
            list(effects_for_actuation(step, deferred.actuation)), params
        )
        before = EffectState.model_validate(deferred.before)
        result = deferred.result.model_copy(deep=True)
        scratch = StepResult(step_id=step.id, intent=step.intent, ok=True)
        try:
            error = self._verify_effects(
                step, before, scratch, effects=effects, verifier=verifier
            )
        except Exception as exc:  # noqa: BLE001 - a raising verifier fails closed
            error = f"read-back raised {type(exc).__name__}; reconciliation is required"
        self._copy_pipelined_evidence(
            SimpleNamespace(result=result, scratch=scratch), error
        )
        return result

    def _apply_effect_fault_mutation(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        actuation_path: Literal["gui", "api"],
        effect_verifier: Any,
        effects: tuple[Any, ...],
    ) -> Any:
        if self._qualification_fault_mutations:
            return effect_verifier
        before_sha256 = effect_verifier_input_sha256(effect_verifier, effects)
        context = self._qualification_fault_context(
            gate=gate,
            step=step,
            actuation_path=actuation_path,
            before_input_sha256=before_sha256,
            effect_verifier=effect_verifier,
            effects=effects,
        )
        if context is None:
            return effect_verifier
        driver = self._require_current_fault_driver_binding()
        mutation = driver.mutate(context)
        if mutation is None:
            return effect_verifier
        if not mutation.replace_effect_verifier:
            raise RuntimeError("qualification_effect_fault_did_not_replace_verifier")
        after_sha256 = effect_verifier_input_sha256(
            mutation.effect_verifier,
            effects,
        )
        self._validate_fault_mutation(
            context,
            mutation.receipt,
            after_input_sha256=after_sha256,
        )
        return mutation.effect_verifier

    def _resolve_effects(
        self, effects: list["Effect"], params: Mapping[str, RuntimeParamScalar]
    ) -> list["Effect"]:
        """Bind each effect's ``ValueExpr`` contract to THIS run's params (P0-3).

        Mirrors how an ``ApiBinding``'s ``{param}`` templates are filled at
        actuation time: the effect a PARAMETERIZED workflow verifies must
        describe the record IT WROTE THIS RUN, not the demonstration's. The
        reserved ``__run_id__`` param exposes the stable-per-run identity so an
        idempotency key can be bound per-run. A pure-literal (v1) effect is
        returned value-identical -- ``resolve`` is a no-op for it.
        """
        namespace = {
            **{name: runtime_param_text(value) for name, value in params.items()},
            "__run_id__": self._run_id,
        }
        run_id_sha256 = hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()
        return [
            effect.resolve(
                namespace,
                opaque_param_sha256={"__run_id__": run_id_sha256},
            )
            for effect in effects
        ]

    @staticmethod
    def _capture_effect_pre_state(verifier: Any, effects: list["Effect"]) -> Any:
        """Capture the pre-action baseline for resolved effect contracts.

        Multi-candidate deployments select an adapter per resolved effect.  A
        normal verifier keeps the established single snapshot behavior.
        """
        capture_for_effects = getattr(verifier, "capture_pre_state_for_effects", None)
        if callable(capture_for_effects):
            return capture_for_effects(effects)
        if getattr(verifier, "_openadapt_requires_preflight", False):
            probe = _probe_adapter_connection(verifier)
            if not probe.ok:
                raise ValueError(
                    "the selected effect verifier failed its read-only connection "
                    f"preflight ({probe.substrate}: {probe.reason})"
                )
        return verifier.capture_pre_state()

    def _keep_undo_prior_state(
        self,
        workflow: Workflow,
        step: Step,
        before: Any,
        effects: Optional[list["Effect"]],
        params: Mapping[str, Any],
    ) -> None:
        """Keep what a locating or restoring undo of ``step`` needs.

        Reads only the pre-action baseline already captured; never raises and
        never changes the step's result (``after_stop.keep_prior_state``).
        """
        run_dir = getattr(self, "_undo_prior_run_dir", None)
        if run_dir is None or not workflow.undo_steps or not effects:
            return
        try:
            state = self._effect_pre_state_for(before, effects[0])
        except Exception:  # noqa: BLE001 - bookkeeping never stops a run
            return
        if getattr(state, "reachable", None) is not True:
            return
        from openadapt_flow.after_stop import keep_prior_state

        keep_prior_state(
            workflow, step, list(getattr(state, "records", []) or []), params, run_dir
        )

    @staticmethod
    def _effect_pre_state_for(before: Any, effect: "Effect") -> Any:
        """Return one selected effect baseline, or a normal shared baseline."""
        selected = getattr(before, "for_effect", None)
        if callable(selected):
            return selected(effect).state
        return before

    @staticmethod
    def _effect_binding_for(
        verifier: Any, before: Any, effect: "Effect"
    ) -> tuple[Any, Any, Optional[Any], str]:
        """Return the exact adapter, state, tier, and identity bound to an effect."""

        selected = getattr(before, "for_effect", None)
        if callable(selected):
            binding = selected(effect)
            return (
                binding.verifier,
                binding.state,
                binding.tier,
                binding.verifier_identity,
            )
        return (
            verifier,
            before,
            verifier_effect_tier(verifier, effect),
            effect_verifier_identity(verifier),
        )

    @staticmethod
    def _candidate_binding_refusal(
        before: Any, effects: list["Effect"]
    ) -> Optional[str]:
        """Refuse a candidate whose identity or tier changed after selection."""

        selected = getattr(before, "for_effect", None)
        if not callable(selected):
            return None
        for effect in effects:
            binding = selected(effect)
            current_tier = verifier_effect_tier(binding.verifier, effect)
            current_identity = effect_verifier_identity(binding.verifier)
            if (
                current_tier != binding.tier
                or current_identity != binding.verifier_identity
            ):
                return (
                    "the selected effect verifier identity or evidence tier changed "
                    "after pre-action binding; refusing to use a different proof"
                )
        return None

    @staticmethod
    def _verify_current_effect(
        verifier: Any, effect: "Effect", current: EffectState
    ) -> Any:
        """Use an adapter's current-state path without inventing a new baseline."""

        candidate = effect.model_copy(deep=True)
        original = candidate.model_dump(mode="json")
        callback = getattr(verifier, "verify_current_state", None)
        if callable(callback):
            verdict = callback(candidate, current)
        else:
            from openadapt_flow.runtime.effects._common import judge_records

            # An effect whose CLAIM is a delta (``exact_new_set``) cannot be
            # judged against a synthesized empty baseline: every record in
            # scope would read as an addition, so the verdict would be
            # fabricated rather than proved. Mark the synthetic baseline
            # UNREACHABLE for those effects and let the judge return its own
            # structured INDETERMINATE refusal. Every pre-existing kind keeps
            # the readable empty baseline and is judged exactly as before.
            needs_baseline = bool(candidate.requires_baseline)
            detail: dict[str, Any] = {"current_state_readback": True}
            if needs_baseline:
                detail["baseline_unavailable_for_delta"] = True
            baseline = EffectState(
                substrate=current.substrate,
                reachable=not needs_baseline,
                records=[],
                detail=detail,
            )
            verdict = judge_records(
                candidate,
                baseline,
                current.records if current.reachable is True else None,
                substrate=current.substrate,
            )
        if candidate.model_dump(mode="json") != original:
            raise EffectContractMutationError(
                "effect verifier changed the resolved effect contract"
            )
        return verdict

    @classmethod
    def _required_effect_pre_state_unreadable(
        cls, verifier: Any, before: Any, effects: list["Effect"]
    ) -> bool:
        """Return true only when a selected effect needs, but lacks, a baseline.

        Different-path on-screen read-back proves persistence after input and
        deliberately has no readable pre-action state.  Delta, duplicate, and
        collateral checks still require their selected verifier baseline.
        """
        requirement = getattr(verifier, "requires_readable_pre_state_for", None)
        selected = getattr(before, "for_effect", None)
        missing_reachability = object()
        for effect in effects:
            if callable(selected):
                required = bool(selected(effect).requires_readable_pre_state)
            else:
                required = (
                    bool(requirement(effect))
                    if callable(requirement)
                    else bool(effect.requires_baseline or effect.forbid_collateral_loss)
                )
            state = cls._effect_pre_state_for(before, effect)
            # Legacy in-process verifiers may retain an opaque pre-state for
            # their own verify implementation. Their established verification
            # path decides whether it is usable. If a legacy state exposes
            # reachability, it must state exactly True; None, falsey values,
            # and truthy non-bools are not evidence that its baseline is
            # readable. Candidate deployments are stricter: their verifier
            # accepts only EffectState, so a missing reachability value cannot
            # bypass its pre-actuation guard.
            reachable = getattr(state, "reachable", missing_reachability)
            if (
                required
                and reachable is not missing_reachability
                and reachable is not True
            ):
                return True
        return False

    def _profile_effect_tier_refusal(
        self,
        workflow: Workflow,
        step: Step,
        actuation_path: Literal["gui", "api"],
        effects: list["Effect"],
        verifier: object,
    ) -> Optional[str]:
        """Recheck effect strength immediately before governed actuation."""

        authorization = self.governed_authorization
        if authorization is None or authorization.execution_profile is None:
            return None
        from openadapt_flow.execution_profiles import required_effect_tier
        from openadapt_flow.verification import VerificationTier

        minimum = (
            VerificationTier(authorization.minimum_effect_tier)
            if authorization.minimum_effect_tier is not None
            else required_effect_tier(workflow, authorization.execution_profile)
        )
        if minimum is None:
            return None
        requirements = authorization.effect_requirements(step.id, actuation_path)
        if requirements:
            from openadapt_flow.policy import effects_for_actuation

            declared = effects_for_actuation(
                step, "api" if actuation_path == "api" else "guarded_coordinate"
            )
            if len(requirements) != len(declared) or len(effects) != len(declared):
                return (
                    "Qualified effect requirements changed after admission — "
                    "refusing to actuate; run aborted"
                )
            for index, requirement in enumerate(requirements):
                if (
                    requirement.effect_index != index
                    or requirement.effect_contract_hash
                    != declared[index].contract_hash()
                ):
                    return (
                        "Qualified effect contract changed after admission — "
                        "refusing to actuate; run aborted"
                    )
        weak = []
        for index, effect in enumerate(effects):
            required = (
                VerificationTier(requirements[index].minimum_tier)
                if requirements
                else minimum
            )
            actual = verifier_effect_tier(verifier, effect)
            if actual is None or not actual.satisfies(required):
                weak.append("untyped" if actual is None else actual.name.lower())
        if not weak:
            return None
        observed = ", ".join(sorted(set(weak)))
        return (
            "Effect verifier strength changed after admission: "
            f"{observed} evidence cannot satisfy the required "
            "qualified effect tier — refusing to actuate; run aborted"
        )

    def _all_default_readback(self, effects: list["Effect"]) -> bool:
        """Whether EVERY declared effect is an auto-derived DIFFERENT-PATH
        on-screen read-back that may be verified out of the box with no
        connector (measured false-CONFIRM ~0). A same-surface read-back or any
        structured system-of-record effect makes this False — those still
        require a configured verifier (fail-safe)."""
        from openadapt_flow.runtime.effects.onscreen import is_default_readback_effect

        return bool(effects) and all(is_default_readback_effect(e) for e in effects)

    def _auto_readback_verifier(self) -> Any:
        """The backend-bound on-screen read-back verifier for the no-connector
        default path (built once, reused across steps)."""
        verifier = getattr(self, "_readback_verifier", None)
        if verifier is None:
            from openadapt_flow.runtime.effects.onscreen import (
                OnScreenReadbackVerifier,
            )

            verifier = OnScreenReadbackVerifier(backend=self.backend)
            self._readback_verifier = verifier
        return verifier

    def _server_response_mark(self) -> Optional[int]:
        """Where the backend's server-answer log stands, or None when the
        backend doesn't see server answers (every non-browser backend)."""

        mark = getattr(self.backend, "server_response_mark", None)
        if not callable(mark):
            return None
        try:
            return int(mark())
        except Exception:  # noqa: BLE001 - evidence only, never a failure
            return None

    def _collect_server_responses(self, result: StepResult, mark: int) -> None:
        since = getattr(self.backend, "server_responses_since", None)
        if not callable(since):
            return
        try:
            responses = list(since(mark))
        except Exception:  # noqa: BLE001 - evidence only, never a failure
            return
        result.server_responses = responses[:_MAX_SERVER_RESPONSES]

    @timed_span("readback")
    def _verify_effects(
        self,
        step: Step,
        before: EffectState,
        result: StepResult,
        effects: Optional[list["Effect"]] = None,
        verifier: Any = None,
    ) -> Optional[str]:
        """Verify each declared Effect against the system of record; HALT on
        any non-CONFIRMED verdict.

        Runs AFTER the action and its screen postconditions (for a GUI write),
        or immediately after an API actuation. For each effect the configured
        verifier rules CONFIRMED / REFUTED / INDETERMINATE against the REAL
        record (an API/DB read, never the screen). A CONFIRMED effect proceeds;
        a non-CONFIRMED effect halts, except that an IRREVERSIBLE effect is
        first routed through ``reconcile_or_escalate`` -- a duplicate the
        configured compensator can undo is RECONCILED (proceed), everything else
        ESCALATEs (halt). Every verdict is recorded on ``result.effect_results``
        for the audit trail. Makes ZERO model calls (the $0 runtime guarantee).

        Args:
            step: The step being verified (for the audit/error text).
            before: The pre-action snapshot of the system of record.
            result: The step result to record verdicts on.
            effects: The effects to verify; defaults to ``step.effects`` (the
                GUI path). The API tier passes ``api_binding.effects`` only.

        Returns an error string (HALT) or None (all effects confirmed or
        reconciled).
        """
        # ``verifier`` is the configured one by default, but the caller may pass
        # the auto-wired backend-bound read-back verifier (the no-connector
        # default path) for a step whose effects are different-path read-backs.
        if verifier is None:
            verifier = self.effect_verifier
        assert verifier is not None  # guaranteed by the caller
        if effects is None:
            effects = step.effects
        for effect in effects:
            # A compiler-mined PLACEHOLDER effect (binding not derivable from
            # the demonstration — see compiler.effect_mining) carries a
            # sentinel selector, NOT a real one. Never verify it against the
            # system of record: that would either falsely refute or, worse,
            # silently pass. Fail-safe HALT until an operator completes the
            # binding and clears the flag — same posture as the identity gate.
            if effect.needs_operator_confirmation:
                result.effect_verified = False
                result.effect_results.append(
                    f"[unbound] {effect.kind.value}: NEEDS OPERATOR "
                    "CONFIRMATION — placeholder effect not bound to the system "
                    "of record (compiler could not derive it); HALT"
                )
                return (
                    f"System-of-record effect for step '{step.id}' "
                    f"({step.intent}) is an unconfirmed PLACEHOLDER — the "
                    "compiler flagged its binding as app-specific and it must "
                    "be completed by an operator before this consequential "
                    "write can run — run aborted"
                )
            try:
                verdict = verify_effect_without_mutation(verifier, effect, before)
                scope = effect_endpoint_scope(verifier)
                selected_pre_state = getattr(before, "for_effect", None)
                if callable(selected_pre_state):
                    # Candidate selection and its evidence strength are pinned
                    # before input. Do not recompute the tier after delivery:
                    # a stateful plugin must not change the receipt contract.
                    binding = selected_pre_state(effect)
                    tier = binding.tier
                    identity = binding.verifier_identity
                    scope = effect_endpoint_scope(binding.verifier)
                    if (
                        verifier_effect_tier(binding.verifier, effect) != tier
                        or effect_verifier_identity(binding.verifier) != identity
                    ):
                        result.effect_verified = False
                        result.safety_halt = True
                        result.failure_category = "governed_refusal"
                        result.effect_results.append(
                            "selected effect verifier identity or evidence tier "
                            "changed after input; HALT"
                        )
                        return (
                            f"Effect verifier binding changed after input for step "
                            f"'{step.id}' — refusing evidence from a different "
                            "adapter or tier; reconciliation is required"
                        )
                else:
                    tier = verifier_effect_tier(verifier, effect)
                    identity = effect_verifier_identity(verifier)
            except EffectContractMutationError:
                result.effect_verified = False
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                result.effect_results.append(
                    "effect verifier changed the resolved effect contract; HALT"
                )
                return (
                    f"Effect verifier changed the resolved contract for step "
                    f"'{step.id}' — refusing mutable verification evidence; "
                    "run aborted"
                )
            result.effect_field_checks.extend(
                effect_field_checks(effect, verdict, tier)
            )
            if verdict.confirmed:
                result.effect_evidence.append(
                    EffectVerificationEvidence(
                        effect_contract_hash=effect.contract_hash(),
                        substrate=verdict.substrate,
                        verifier_identity=identity,
                        verification_tier=(int(tier) if tier is not None else None),
                        initial_verdict=verdict.verdict.value,
                        final_verdict=verdict.verdict.value,
                        observed_effect=verdict.observed_effect,
                        endpoint_scope=scope,
                    )
                )
                result.effect_results.append(
                    f"[{verdict.substrate}] {effect.kind.value}: CONFIRMED"
                    + (f" — {verdict.reason}" if verdict.reason else "")
                )
                continue

            # Non-CONFIRMED: never proceed on an unverified/contradicted write.
            # An irreversible effect gets one reconcile-or-escalate pass (a
            # compensable duplicate is fixable; missing / partial / collateral
            # loss / INDETERMINATE always escalate). A reversible effect just
            # halts. Either way the run stops -- this is the same
            # refuse-rather-than-guess posture as the identity gate.
            if effect.risk == "irreversible":
                comp = reconcile_or_escalate(
                    effect,
                    verdict,
                    verifier=verifier,
                    before=before,
                    compensator=self.effect_compensator,
                )
                if comp.proceed:
                    final_verdict = comp.final_verdict or verdict
                    result.effect_evidence.append(
                        EffectVerificationEvidence(
                            effect_contract_hash=effect.contract_hash(),
                            substrate=final_verdict.substrate,
                            verifier_identity=identity,
                            verification_tier=(int(tier) if tier is not None else None),
                            initial_verdict=verdict.verdict.value,
                            final_verdict=final_verdict.verdict.value,
                            observed_effect=final_verdict.observed_effect,
                            endpoint_scope=scope,
                            reconciliation_completed=True,
                            reconciliation_actions=comp.actions_taken,
                        )
                    )
                    result.effect_results.append(
                        f"[{verdict.substrate}] {effect.kind.value}: "
                        f"{verdict.verdict.value.upper()} → RECONCILED "
                        f"({comp.actions_taken} action(s)) — {comp.reason}"
                    )
                    continue
                result.effect_verified = False
                final_verdict = comp.final_verdict or verdict
                result.effect_evidence.append(
                    EffectVerificationEvidence(
                        effect_contract_hash=effect.contract_hash(),
                        substrate=final_verdict.substrate,
                        verifier_identity=identity,
                        verification_tier=(int(tier) if tier is not None else None),
                        initial_verdict=verdict.verdict.value,
                        final_verdict=final_verdict.verdict.value,
                        observed_effect=final_verdict.observed_effect,
                        endpoint_scope=scope,
                        reconciliation_actions=comp.actions_taken,
                        deferred_check=verdict.deferred_check,
                    )
                )
                result.effect_results.append(
                    f"[{verdict.substrate}] {effect.kind.value}: "
                    f"{verdict.verdict.value.upper()} → "
                    f"{comp.outcome.value.upper()} — {comp.reason}"
                )
                prefix, read = _effect_read_wording(tier)
                return (
                    f"{prefix} HALTED step "
                    f"'{step.id}' ({step.intent}): {effect.kind.value} "
                    f"{verdict.verdict.value} against the {verdict.substrate} "
                    f"{read} and could not be reconciled "
                    f"({comp.outcome.value}) — "
                    f"{comp.escalation or comp.reason} — run aborted"
                )

            result.effect_verified = False
            result.effect_evidence.append(
                EffectVerificationEvidence(
                    effect_contract_hash=effect.contract_hash(),
                    substrate=verdict.substrate,
                    verifier_identity=identity,
                    verification_tier=(int(tier) if tier is not None else None),
                    initial_verdict=verdict.verdict.value,
                    final_verdict=verdict.verdict.value,
                    observed_effect=verdict.observed_effect,
                    endpoint_scope=scope,
                    deferred_check=verdict.deferred_check,
                )
            )
            result.effect_results.append(
                f"[{verdict.substrate}] {effect.kind.value}: "
                f"{verdict.verdict.value.upper()} — {verdict.reason}"
            )
            prefix, read = _effect_read_wording(tier)
            return (
                f"{prefix} HALTED step "
                f"'{step.id}' ({step.intent}): {effect.kind.value} "
                f"{verdict.verdict.value} against the {verdict.substrate} "
                f"{read} (the screen indicated completion but the "
                f"declared outcome is contradicted or unverifiable) — "
                f"{verdict.reason} — run aborted"
            )

        result.effect_verified = True
        return None

    @staticmethod
    def _mapped_selection_readback_region(
        recorded_target: Region,
        recorded_readback: Region,
        live_target: Region,
        frame_png: bytes,
    ) -> Optional[Region]:
        """Map the recorded readback band onto a fresh live target.

        Absolute recording coordinates never drive verification. The mapping
        preserves the band's target-relative offsets while allowing the
        already-qualified target geometry to translate or scale modestly, then
        clamps it to the exact final frame.
        """

        from openadapt_flow.vision import imgops

        frame = imgops.decode_bgr(frame_png)
        if frame is None:
            return None
        frame_h, frame_w = frame.shape[:2]
        target_x, target_y, target_w, target_h = recorded_target
        read_x, read_y, read_w, read_h = recorded_readback
        live_x, live_y, live_w, live_h = live_target
        if min(target_w, target_h, read_w, read_h, live_w, live_h) <= 0:
            return None
        scale_x = live_w / target_w
        scale_y = live_h / target_h
        mapped_x = round(live_x + (read_x - target_x) * scale_x)
        mapped_y = round(live_y + (read_y - target_y) * scale_y)
        mapped_w = max(1, round(read_w * scale_x))
        mapped_h = max(1, round(read_h * scale_y))
        left = max(0, mapped_x)
        top = max(0, mapped_y)
        right = min(frame_w, mapped_x + mapped_w)
        bottom = min(frame_h, mapped_y + mapped_h)
        if right <= left or bottom <= top:
            return None
        return (left, top, right - left, bottom - top)
