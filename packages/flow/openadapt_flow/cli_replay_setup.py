"""Deployment wiring and non-authorizing replay preparation for the CLI."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Callable, Mapping

if TYPE_CHECKING:
    from openadapt_flow.deployment import (
        ActuationConfig,
        BackendConfig,
        DeploymentConfig,
        EffectsConfig,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.authorization import RuntimeParamScalar


def deployment_runtime(
    args: argparse.Namespace,
    params: Mapping[str, RuntimeParamScalar] | None,
    *,
    sections: Callable[
        [],
        Callable[
            [argparse.Namespace],
            tuple[DeploymentConfig, EffectsConfig, ActuationConfig],
        ],
    ],
):
    """Resolve the deployment wiring for a replay/run from ``--config`` + flags.

    Returns ``(cfg, effect_verifier, api_actuator, durable, allow_egress)``.
    A ``--config`` deployment YAML supplies the full surface (records paths,
    FHIR search params, ...); direct flags override the common fields. With
    neither, everything is default: no verifier, no actuator, non-durable, and
    egress only if ``--allow-model-grounding`` was passed (fully back-compatible).

    ``params`` (the governed ``--params-file`` / ``--param`` values) binds an
    effect-verifier config's explicit ``{param: ...}`` references
    (``effects.path_params`` / ``search_param_exprs`` / ``sql_query_params``)
    at construction — see ``docs/EFFECT_KIT.md``. A config with no references
    ignores it.
    """
    from openadapt_flow.deployment import build_api_actuator, build_effect_verifier
    from openadapt_flow.runtime.authorization import runtime_params_for_gui

    cfg, effects, actuation = sections()(args)
    try:
        # Custom verifier factories may create a readback session. A dry-run
        # binds only the declared contracts and never constructs that runner.
        effect_verifier = (
            None
            if getattr(args, "stop_before_effect", False)
            else build_effect_verifier(
                effects,
                params=runtime_params_for_gui(params or {}),
            )
        )
        api_actuator = (
            None
            if getattr(args, "stop_before_effect", False)
            else build_api_actuator(actuation)
        )
    except ValueError as e:
        raise SystemExit(str(e))

    durable = bool(cfg.runtime.durable or getattr(args, "durable", False))
    selected_profile = getattr(args, "_execution_profile", None)
    if selected_profile is not None:
        from openadapt_flow.execution_profiles import execution_profile_contract

        durable = bool(
            durable or execution_profile_contract(selected_profile).require_durable
        )
    allow_egress = bool(
        cfg.runtime.allow_model_grounding
        or getattr(args, "allow_model_grounding", False)
    )
    if getattr(args, "local_grounding", False):
        cfg.runtime = cfg.runtime.model_copy(update={"local_grounding": True})
    return cfg, effect_verifier, api_actuator, durable, allow_egress


def dry_run_replay_refusal(
    args: argparse.Namespace,
    cfg: DeploymentConfig,
    backend_cfg: BackendConfig,
    workflow: Workflow,
    durable: bool,
    qualification_case: object,
    worklists: object,
    surface_cfgs: object,
    normalize_kind: Callable[[str], str],
) -> int | None:
    from openadapt_flow.cli_worklist_pool import requested_pool
    from openadapt_flow.ir import ActionKind

    unsupported = (
        durable
        or qualification_case is not None
        or worklists
        or workflow.program is not None
        or workflow.subflows
        or workflow.surfaces
        or surface_cfgs
        or requested_pool(args, cfg) is not None
        or any(step.action is ActionKind.SWITCH_SURFACE for step in workflow.steps)
        or (cfg.rollback is not None and cfg.rollback.enabled)
        # Creating a network RDP connection can switch/log in a desktop
        # session before the input gate exists; its effects are unproved.
        or bool(backend_cfg.rdp_host)
        or (
            normalize_kind(backend_cfg.kind) == "web"
            and not backend_cfg.browser_cdp_endpoint
        )
    )
    if unsupported:
        print(
            "Check-it REFUSED: dry-run requires a linear, single-surface replay "
            "without navigation, pooling, rollback or durable continuation. "
            "Web runs must attach to the already-open screen. Nothing was executed."
        )
        return 2
    return None
