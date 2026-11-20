"""CLI handoffs for the shared verified program-dispatch authority."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.jit.dispatch_authority import verify_program_dispatch
from openadapt_flow.jit.hosted_authority import (
    HostedAuthorityError,
    is_admitted_program,
    readback_effects,
)


def stage_admitted_program_readback(
    args: argparse.Namespace, bundle: Path
) -> int | None:
    """Stage only the admission-bound verifier of a managed program.

    The caller must first decide that the dispatch rests on the program's
    signed admission, with no qualification admission or authority file.
    """
    if not getattr(args, "managed_dispatch_file", None) or not is_admitted_program(
        bundle
    ):
        return None
    try:
        args._admitted_program_effects = readback_effects(bundle)
    except HostedAuthorityError:
        print(
            "run REFUSED: the admitted program's bundled read-back check is invalid. Nothing was executed."
        )
        return 2
    return None


def bind_verified_program_run(
    args: argparse.Namespace,
    *,
    bundle: Path,
    workflow: Any,
    params: Any,
    worklists: Any,
    policy_source: str,
    effect_verifier: Any,
    api_actuator: Any,
) -> int | None:
    """Bind after the existing runner-local approval mode was resolved."""
    try:
        args._managed_dispatch_binding = verify_program_dispatch(
            args._managed_dispatch_binding,
            bundle_dir=bundle,
            workflow=workflow,
            params=params,
            worklists=worklists,
            policy_source=policy_source,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
        )
    except (HostedAuthorityError, ValueError, OSError):
        print(
            "run REFUSED: this managed program dispatch could not be verified. Nothing was executed."
        )
        return 2
    return None


def restore_verified_program_dispatch(
    binding: Any,
    manifest: Any,
    workflow: Any,
    *,
    policy_source: str,
    effect_verifier: Any,
    api_actuator: Any,
) -> tuple[Any, Any]:
    """Reverify the exact retained program and current keys for continuation."""
    from openadapt_flow.deployment import build_effect_verifier

    retained = manifest.governed_authorization
    if not (
        binding is not None
        and retained is not None
        and retained.qualification_admission is None
        and retained.production_qualification_admission_id is None
        and retained.approval_source.startswith("hosted:program-admission:")
    ):
        return binding, effect_verifier
    verifier = build_effect_verifier(
        readback_effects(Path(manifest.bundle_dir)), params=manifest.params
    )
    verified = verify_program_dispatch(
        binding,
        bundle_dir=manifest.bundle_dir,
        workflow=workflow,
        params=manifest.params,
        worklists=manifest.worklists,
        policy_source=policy_source,
        effect_verifier=verifier,
        api_actuator=api_actuator,
    )
    return verified, verifier


def refuse_managed_qualification(
    authorization: object,
    workflow: object | None = None,
) -> int | None:
    """Verify the signed admission already bound on a Cloud authorization."""

    from openadapt_flow.production_qualification import (
        managed_qualification_edge_refusal,
    )

    admission = getattr(authorization, "qualification_admission", None)
    if admission is None:
        print(
            "run REFUSED: Standard and Regulated actuation requires a signed "
            "qualification admission. Nothing was executed."
        )
        return 2
    refusal = managed_qualification_edge_refusal(authorization, workflow)
    if refusal is not None:
        print(
            "run REFUSED: the Production qualification authority is invalid, "
            "expired, revoked, or does not match this exact run. Nothing was "
            "executed."
        )
        return 2
    return None


def resolve_run_approval_mode(
    args: argparse.Namespace, cfg: Any, profile_value: Any, *, admitted_mode: Any
):
    """The approval mode a ``run`` uses, or ``None`` for an ungated run.

    A direct ``--approval-mode`` wins over ``runtime.approval_mode``. Every
    run is gated by default: a missing or unknown value means ``each_item``,
    so a person approves each save whatever the profile. The only way to turn
    the gate off is to name ``not_gated``, and the report records that choice.
    """
    from openadapt_flow.approval_mode.model import resolve_approval_mode

    requested = getattr(args, "approval_mode", None)
    if requested is None:
        requested = getattr(getattr(cfg, "runtime", None), "approval_mode", None)
    if requested is None:
        requested = admitted_mode(args)
    return resolve_approval_mode(requested)


def admitted_approval_mode(args: argparse.Namespace) -> Optional[str]:
    """The approval mode the bundle's admission gives it, or ``None``.

    An accepted ``openadapt-flow admit`` sets ``problems_only`` for that exact
    bundle. A flag or ``runtime.approval_mode`` still wins. A bundle changed
    since admission, or an agent's admission whose person-signed grant no
    longer verifies, falls back to the default (a person approves each save)
    and says why.
    """

    bundle = getattr(args, "bundle", None)
    if not bundle:
        return None
    from openadapt_flow.admit_pipeline import admitted_approval_mode

    admitted = admitted_approval_mode(Path(bundle))
    if admitted.refusal is not None:
        print(
            f"admission approval mode not used ({admitted.refusal}): a person "
            "approves each save."
        )
        return None
    if admitted.approval_mode is not None and admitted.admitted_by is not None:
        print(
            f"Admitted by {admitted.admitted_by.label()}: this workflow uses "
            f"{admitted.approval_mode}."
        )
    return admitted.approval_mode


def approval_wait_seconds(args: argparse.Namespace) -> float:
    from openadapt_flow.approval_mode.item_approval import (
        DEFAULT_APPROVAL_WAIT_S,
        MAX_APPROVAL_WAIT_S,
    )

    raw = getattr(args, "approval_wait", None)
    if raw is None:
        return DEFAULT_APPROVAL_WAIT_S
    return max(0.0, min(float(raw), MAX_APPROVAL_WAIT_S))


def bind_approval_mode(args: argparse.Namespace) -> Optional[int]:
    """Bind the resolved approval mode into the run's authorization.

    The mode is then part of the authorization the run checks at every input
    edge and retains for a resume. A Cloud dispatch keeps its binding digest:
    the mode is a runner-local field.
    """
    mode = getattr(args, "_approval_mode", None)
    authorization = getattr(args, "_governed_run_authorization", None)
    if mode is None or authorization is None:
        return None
    existing = getattr(authorization, "approval_mode", None)
    if existing is not None and existing != mode.value:
        print(
            f"run REFUSED: this run's authorization names the {existing} approval "
            f"mode, not {mode.value}. Nothing was executed."
        )
        return 2
    bound = authorization.model_copy(update={"approval_mode": mode.value})
    binding = getattr(args, "_managed_dispatch_binding", None)
    if binding is not None:
        from openadapt_flow.runner.dispatch_envelope import (
            ManagedDispatchEnvelopeError,
            with_runner_local_authorization,
        )

        try:
            args._managed_dispatch_binding = with_runner_local_authorization(
                binding, bound
            )
        except ManagedDispatchEnvelopeError:
            print(
                "run REFUSED: managed dispatch binding is invalid. Nothing was "
                "executed."
            )
            return 2
    args._governed_run_authorization = bound
    labels = {
        "each_item": "a person approves each save",
        "problems_only": "only problems go to a person",
        "not_gated": "no per-item pause",
    }
    print(f"Approval mode: {mode.value} ({labels[mode.value]}).")
    return None
