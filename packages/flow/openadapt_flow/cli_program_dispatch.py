"""CLI handoffs for the shared verified program-dispatch authority."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

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
