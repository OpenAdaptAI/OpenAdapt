"""Output ownership and per-step state for a fresh local durable run."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.runtime.durable.first_local_delivery import (
    FirstLocalDeliveryHeld,
    local_delivery_owner,
)

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Step, StepResult, Workflow
    from openadapt_flow.runtime.authorization import RuntimeParamScalar
    from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint
    from openadapt_flow.runtime.effects.effect import Effect
    from openadapt_flow.runtime.replayer import Replayer


@dataclass
class FirstLocalDeliveryStep:
    durable_run: Any
    index: int
    step: Step
    params: dict[str, RuntimeParamScalar]
    pause_digest: str = ""
    actuation: Optional[Literal["gui", "api"]] = None
    held: bool = False


def mark_first_local_delivery_held(result: StepResult, reason: str) -> None:
    result.ok = False
    result.safety_halt = True
    result.failure_category = "safety_halt"
    result.error = reason


def placeholder_effect_refusal(
    step: Step, effect: Effect, result: StepResult
) -> Optional[str]:
    # A compiler-mined placeholder carries a sentinel selector, not a real one.
    # Refuse before using it as evidence until an operator completes the binding.
    if not effect.needs_operator_confirmation:
        return None
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


def first_local_effect_refusal(
    step: Step, result: StepResult, effects: list[Effect]
) -> bool:
    if not effects:
        result.error = (
            "first local consequential delivery requires an effect contract "
            "for the selected actuation path; refusing input"
        )
    else:
        for effect in effects:
            refusal = placeholder_effect_refusal(step, effect, result)
            if refusal is not None:
                result.error = refusal
                break
        else:
            return False
    result.safety_halt = True
    return True


def arm_first_local_delivery(
    replayer: Replayer,
    step: Step,
    params: Mapping[str, RuntimeParamScalar],
    *,
    actuation: Literal["gui", "api"],
) -> Optional[str]:
    local = replayer._first_local_delivery_step
    if local is None:
        return None
    try:
        if (
            local.held
            or step.id != local.step.id
            or params != local.params
            or any(
                type(value) is not type(local.params[name])
                for name, value in params.items()
            )
        ):
            raise FirstLocalDeliveryHeld("the current step no longer owns delivery")
        if local.pause_digest:
            if (
                local.actuation != actuation
                or local.durable_run.pause_digest() != local.pause_digest
            ):
                raise FirstLocalDeliveryHeld("the armed step's pause changed")
        else:
            local.pause_digest = local.durable_run.arm_first_local_delivery(
                local.index, local.step, local.params, actuation=actuation
            )
            local.actuation = actuation
    except FirstLocalDeliveryHeld as exc:
        local.held = True
        return str(exc)
    return None


def run_with_output(
    replayer: Replayer,
    workflow: Workflow,
    *,
    bundle_dir: Path,
    run_dir: Path,
    save_healed_to: Optional[Path],
    resume_from: Optional[int],
    resume_program: Optional[ProgramCheckpoint],
    **kwargs: Any,
) -> RunReport:
    """Keep an owned local leg fenced through its sealed output finalization."""
    from openadapt_flow import at_rest
    from openadapt_flow.runtime.preparation import validate_dry_run_entry

    validate_dry_run_entry(
        replayer,
        workflow,
        resume_from=resume_from,
        resume_program=resume_program,
        worklists=kwargs.get("worklists"),
    )
    bundle_path = Path(bundle_dir)
    bundle_sealed = _sealed.is_sealed(bundle_path) or (
        _sealed.active_writer(bundle_path) is not None
    )
    # A sealed bundle always runs into a sealed run directory, whatever
    # the mode or opt-out: encrypted input never yields plaintext output.
    # Strict mode seals the run of a plaintext bundle too. Otherwise the
    # plaintext run directory is named on stderr and in the ledger
    # (docs/phi_at_rest.md).
    seal_run = at_rest.strict() or bundle_sealed
    if save_healed_to is not None and seal_run:
        # The healed copy is written as a plaintext bundle: hold it back
        # before anything runs rather than decrypt a sealed bundle to disk.
        raise at_rest.AtRestRefused(
            f"Not writing a healed bundle at {save_healed_to}: it would be a "
            "plaintext copy of an encrypted run. Nothing was executed."
        )
    run_path = Path(run_dir)
    passphrase = None
    if seal_run and _sealed.active_writer(run_path) is None:
        passphrase = _sealed.passphrase_for("bundle")
        if not passphrase:
            raise _sealed.SealedSetKeyError(
                _sealed.missing_key_message(bundle_path, "bundle")
            )
        unsupported = replayer._sealed_run_unsupported(workflow)
        if unsupported is not None:
            # Refuse before anything is actuated or written: these stores
            # keep their own plaintext state files in the run directory.
            raise _sealed.SealedSetError(
                f"a sealed run keeps {unsupported} outside the sealed set, so "
                "it is held back. Nothing was executed. Run a plaintext bundle "
                "outside OPENADAPT_FLOW_AT_REST=strict to use it (recorded)."
            )
    first_local_delivery = (
        replayer.durable
        and replayer.delivery_authority_kind == "customer_local"
        and replayer.managed_dispatch_binding is None
        and workflow.program is None
        and resume_from is None
        and resume_program is None
        and replayer._durable_resume_admission is None
        and replayer._durable_continuation_guard is None
    )
    kwargs.update(
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        save_healed_to=save_healed_to,
        resume_from=resume_from,
        resume_program=resume_program,
        first_local_delivery=first_local_delivery,
    )
    with ExitStack() as ownership:
        if first_local_delivery:
            ownership.enter_context(local_delivery_owner(run_path))
        if passphrase is None:
            if _sealed.active_writer(run_path) is None:
                if not at_rest.plaintext_allowed("a run directory"):
                    at_rest.unsealed_write("a run directory", run_dir)
                if replayer.checkpoint_key is None:
                    # Durable checkpoints can still be encrypted here.
                    from openadapt_flow import crypto as _crypto

                    replayer.checkpoint_key = _crypto.resolve_key(None)
            return replayer._run_unsealed(workflow, **kwargs)
        if replayer.checkpoint_key is None:
            # A sealed bundle's run keeps its durable state under the same key.
            replayer.checkpoint_key = passphrase
        writer = _sealed.SealedSetWriter(
            run_path, "run", passphrase, resume=_sealed.is_sealed(run_path)
        )
        _sealed.register_writer(writer)
        try:
            report = replayer._run_unsealed(workflow, **kwargs)
        finally:
            _sealed.unregister_writer(writer)
        writer.finalize(adopt_external=True)
        return report
