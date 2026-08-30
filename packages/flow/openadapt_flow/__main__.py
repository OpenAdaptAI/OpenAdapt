"""openadapt-flow CLI.

Subcommands (thin wrappers over the module APIs; sibling modules are
imported lazily inside each handler so ``--help`` always works):

- ``record`` — open a headed browser on your OWN app (``--url``) and
  record what you do into the format ``compile`` consumes.
- ``demo-record`` — serve MockMed locally and record the canonical demo.
- ``compile`` — compile a recording directory into a workflow bundle.
- ``induce`` — induce a parameterized PROGRAM bundle from MULTIPLE recordings
  (multi-trace induction); refuses (nonzero exit) when intent is
  underdetermined rather than guessing a branch, and prints a ``record-next:``
  worklist of missing demonstrations.
- ``for-each`` — author a DATA-DRIVEN LOOP bundle: wrap a single-demonstration
  bundle's linear body in a LOOP that runs once per record of a worklist
  (CSV/JSON), binding each record's columns to the workflow's parameters.
  Emits a program:true bundle; fails loudly on a column/parameter mismatch.
- ``compose`` — author a parent sequencer from named, already-compiled child
  bundles plus a handoff contract. Each child keeps its recorded surface.
  ``certify`` / ``run`` execute the parent. The launcher form is
  ``openadapt flow compose``.
- ``process`` — author a ProcessContract parent over independently admitted
  capabilities (each child a ``QualificationAdmissionEnvelope``). Not compose.
  ``certify`` / ``run`` execute the parent; ``replay`` refuses it. The
  launcher form is ``openadapt flow process``.
- ``replay`` — replay a bundle; serves the bundled MockMed demo app when no
  ``--url`` is given (with optional ``--drift`` to demonstrate healing).
  ``--worklist`` drives a program's loop over a CLI-supplied relation; effect
  verification and API actuation are wired from ``--config`` / flags.
- ``run`` — execute a bundle under a deployment config (``--config``): the
  same replay path, wired for a real deployment (backend / effects / actuation
  / durable runtime / policy) instead of the demo.
- ``resume`` — resume a durably-paused run from its last verified checkpoint.
- ``teach`` runs self-serve HALT -> LEARN: resolve a halted run from a fix
  demonstration (induce + gate + validate the correction, promote only a
  verified revision), writing an updated bundle. Refuses bad fixes.
- ``approve`` — mark a durably-paused run's pending escalation approved.
- ``bench`` — replay a bundle N times against MockMed and aggregate.
- ``visualize`` — SEE what a demonstration compiled into: emit a program-graph
  view of a bundle or a composed parent (child bundles, --after DAG,
  effect-bound handoffs) as self-contained HTML, Mermaid, or the shared JSON
  graph spec that the cloud and desktop surfaces render.
- ``seal`` — copy a bundle to a new path and atomically seal its workflow and
  template evidence with ``OPENADAPT_BUNDLE_KEY``.
- ``lint`` — report a bundle's coverage gaps (advice; exit code by severity).
- ``certify`` — enforce a safety policy on a bundle (refuse it if it fails).
- ``qualify`` — create and edit the versioned qualification project, import
  customer-controlled case results, explain refusals, and persist certification.
  ``qualify propose`` (also ``propose-qualification``) fills application,
  environment, identity, and effect pins from the demo; ``qualify accept``
  runs the proposed oracle against a ``--break-it`` fault before it can
  succeed; refusing a pin HALTs.
- ``scaffold-verifier`` — draft (never approve) an effect-oracle contract
  (``effect_contract.yaml``) from a recording or bundle's write-shaped steps;
  refuses demonstrations with no consequential step.
- ``explain`` — plain-language, read-only read of a completed run directory:
  what happened, why the outcome is the safe one, and the next suggested
  command.
- ``console`` — serve the localhost-only operator console (a read-first web
  UI over bundles / runs / skill libraries; requires the ``console`` extra).
- ``emit-skill`` — emit an Agent Skills folder for a bundle.
- ``emit-mcp`` — emit a standalone MCP ``server.py`` for a bundle.
- ``connector`` — the BYOC (bring-your-own-cloud) outbound-pull daemon:
  ``enroll`` this machine with OpenAdapt Cloud, then ``run`` governed jobs
  LOCALLY inside the customer perimeter (PHI stays on this side). See
  ``docs/BYOC_CONNECTOR.md`` and :mod:`openadapt_flow.connector`.

A single ``deployment.yaml`` (``--config``; see
``docs/deployment.example.yaml`` and :mod:`openadapt_flow.deployment`)
configures backend / actuation / effects / runtime / policy for ``record`` /
``compile`` / ``certify`` / ``replay`` / ``run`` / ``resume``.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    Literal,
    Mapping,
    Optional,
    Sequence,
    cast,
)
from urllib.parse import urlsplit
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import ExecutionTargetKind, RunReport
    from openadapt_flow.runtime.authorization import RuntimeParamScalar
    from openadapt_flow.tutorial import BreakItResult

_VIEWPORT = {"width": 1280, "height": 800}


def _parse_params(pairs: Sequence[str] | None) -> dict[str, str]:
    """Parse repeated ``--param k=v`` flags into a dict."""
    params: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects k=v, got {pair!r}")
        key, value = pair.split("=", 1)
        params[key] = value
    return params


def _parse_identifier_region_arg(
    values: Sequence[str], *, backend: str
) -> tuple[int, int, int, int] | None:
    """Parse a desktop ``record --identifier X,Y,W,H`` region (fail-loud).

    A pixel capture has no field identity, so ``--identifier`` on a desktop
    backend takes the record-identifying REGION itself, once. A field-name
    argument (the web syntax) or a repeated/malformed region is refused
    rather than silently recording an unmarked session.
    """
    if not values:
        return None
    if len(values) > 1:
        raise SystemExit(
            f"record --backend {backend}: --identifier takes ONE region "
            f"(X,Y,W,H) on the pixel/desktop substrate, got {len(values)}."
        )
    parts = values[0].split(",")
    try:
        region = tuple(int(p.strip()) for p in parts)
    except ValueError:
        region = ()
    if len(region) != 4 or region[2] <= 0 or region[3] <= 0:
        raise SystemExit(
            f"record --backend {backend}: --identifier expects X,Y,W,H "
            "(recording pixels, positive size) on the pixel/desktop "
            f"substrate — field names only apply to --backend web. Got "
            f"{values[0]!r}."
        )
    return (region[0], region[1], region[2], region[3])


_RECORD_TARGET_FLAGS: tuple[tuple[str, str], ...] = (
    ("agent_url", "--agent-url"),
    ("macos_app", "--macos-app"),
    ("macos_window_title", "--macos-window-title"),
    ("linux_app", "--linux-app"),
    ("linux_window_title", "--linux-window-title"),
    ("linux_allow_physical_input", "--linux-allow-physical-input"),
    ("rdp_host", "--rdp-host"),
    ("rdp_window", "--rdp-window"),
    ("rdp_window_title", "--rdp-window-title"),
    ("rdp_readiness_text", "--rdp-readiness-text"),
)


def _record_flag_is_set(args: argparse.Namespace, attr: str) -> bool:
    value = getattr(args, attr, None)
    if isinstance(value, bool):
        return value
    return value is not None


def _reject_unbound_record_target_flags(args: argparse.Namespace, backend: str) -> None:
    """Refuse target flags that cannot affect this capture session.

    The backend flags are shared with replay/run.  A record command must not
    accept one unless it either scopes the live Capture session or is retained
    by the existing recording schema as a replay binding.  Otherwise the CLI
    would appear to record the named app/host while actually recording an
    unrelated local desktop.
    """
    allowed: dict[str, set[str]] = {
        "web": set(),
        "windows": set(),
        "macos": {"macos_app", "macos_window_title"},
        "linux": set(),
        "rdp": {"rdp_window", "rdp_window_title", "rdp_readiness_text"},
        "citrix": {"rdp_window", "rdp_window_title", "rdp_readiness_text"},
    }
    for attr, flag in _RECORD_TARGET_FLAGS:
        if not _record_flag_is_set(args, attr) or attr in allowed[backend]:
            continue
        if backend == "windows" and attr == "agent_url":
            reason = (
                "the local Capture session cannot bind to a WAA endpoint. "
                "Scope the local recording with --window/--window-title, then "
                "pass --agent-url to replay or run"
            )
        elif backend == "linux" and attr in {
            "linux_app",
            "linux_window_title",
            "linux_allow_physical_input",
        }:
            reason = (
                "the current Capture component has no Linux window-scoping "
                "primitive. Record the local Linux desktop without this flag, "
                "then pass it to replay or run"
            )
        elif backend == "rdp" and attr == "rdp_host":
            reason = (
                "Capture cannot connect to a network RDP endpoint. Record "
                "inside the remote session, or scope a local client with "
                "--window/--rdp-window, then pass --rdp-host to replay or run"
            )
        else:
            reason = f"this target flag does not apply to the {backend} recorder"
        raise SystemExit(
            f"record --backend {backend}: {flag} was not applied: {reason}. "
            "Nothing was recorded."
        )


def _merge_record_window_selector(
    generic_value: Optional[str],
    target_value: Optional[str],
    *,
    generic_flag: str,
    target_flag: str,
    backend: str,
) -> Optional[str]:
    """Merge generic and surface-specific Capture selectors without guessing."""
    if generic_value is None:
        return target_value
    if target_value is None:
        return generic_value
    if generic_value.strip().casefold() != target_value.strip().casefold():
        raise SystemExit(
            f"record --backend {backend}: {generic_flag} and {target_flag} "
            "name different capture targets. Give one target, or give the "
            "same value to both. Nothing was recorded."
        )
    return generic_value


def _resolve_record_capture_window(
    args: argparse.Namespace, backend: str
) -> Optional[dict[str, Optional[str]]]:
    """Resolve the exact window that the local Capture session will record."""
    _reject_unbound_record_target_flags(args, backend)

    owner = getattr(args, "window", None)
    title = getattr(args, "window_title", None)
    if backend == "linux" and (owner is not None or title is not None):
        raise SystemExit(
            "record --backend linux: --window/--window-title cannot be applied "
            "because the current Capture component has no Linux "
            "window-scoping primitive. Record the local Linux desktop without "
            "these flags. Nothing was recorded."
        )
    if backend == "macos":
        owner = _merge_record_window_selector(
            owner,
            getattr(args, "macos_app", None),
            generic_flag="--window",
            target_flag="--macos-app",
            backend=backend,
        )
        title = _merge_record_window_selector(
            title,
            getattr(args, "macos_window_title", None),
            generic_flag="--window-title",
            target_flag="--macos-window-title",
            backend=backend,
        )
    elif backend in ("rdp", "citrix"):
        # The capture selector is a local owner/title substring. The replay
        # selector can be an exact process identity (for example ``wfica32``),
        # so the two values can legitimately differ. A dedicated --window
        # value controls Capture; otherwise the replay selector also scopes it.
        owner = owner or getattr(args, "rdp_window", None)
        title = title or getattr(args, "rdp_window_title", None)
    if owner is None and title is None:
        return None
    return {"owner": owner, "title": title}


def _replay_params(
    pairs: Sequence[str] | None,
    params_file: str | None = None,
) -> dict[str, "RuntimeParamScalar"]:
    """Load replay bindings without requiring sensitive values in argv.

    ``--params-file`` is intended for managed runners: the file can be staged
    inside the per-run boundary while process listings contain only its path.
    Explicit ``--param`` flags remain supported and override file values.
    """
    import json

    from openadapt_flow.runtime.authorization import is_runtime_param_scalar

    params: dict[str, RuntimeParamScalar] = {}
    if params_file:
        path = Path(params_file)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--params-file could not be read as JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit("--params-file must contain one JSON object")
        if len(raw) > 100:
            raise SystemExit("--params-file may contain at most 100 parameters")
        for key, value in raw.items():
            if not isinstance(key, str) or not key:
                raise SystemExit("--params-file keys must be non-empty strings")
            if not is_runtime_param_scalar(value):
                raise SystemExit(f"--params-file value for {key!r} must be a scalar")
            params[key] = value
    params.update(_parse_params(pairs))
    return params


def _with_drift(url: str, drift: str | None) -> str:
    """Append a ``?drift=...`` query to a MockMed base URL."""
    if not drift:
        return url
    return f"{url.rstrip('/')}/?drift={drift}"


def _load_worklist_file(path: Path) -> list[dict[str, str]]:
    """Load a CLI worklist file (``.csv`` or ``.json``) into param rows.

    CSV: the header row names the parameters; each subsequent row is one loop
    iteration's bindings. JSON: either a list of ``{param: value}`` row objects,
    or a single ``{param: value}`` object (one row). Every value is coerced to a
    string (the IR's worklist rows are ``dict[str, str]``).
    """
    import csv
    import json

    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"--worklist file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise SystemExit(
                f"--worklist JSON {path} must be a list of row objects (or one "
                "row object)"
            )
        return [{str(k): str(v) for k, v in row.items()} for row in data]
    if suffix == ".csv":
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            return [
                {str(k): str(v) for k, v in row.items() if k is not None}
                for row in reader
            ]
    raise SystemExit(f"--worklist file {path} must be .csv or .json (got {suffix!r})")


def _resolve_worklists(
    specs: Sequence[str] | None, workflow
) -> dict[str, list[dict[str, str]]]:
    """Turn ``--worklist`` specs into run-time worklists keyed by relation name.

    Each spec is ``RELATION=path`` (bind the file to that loop relation) or a
    bare ``path`` (bind to the workflow's SOLE loop relation; an error if the
    program has zero or several). Program-mode only — a linear bundle ignores
    worklists, so passing one there is refused loudly.
    """
    if not specs:
        return {}
    if workflow.program is None:
        raise SystemExit(
            "--worklist applies only to a PROGRAM bundle (with a loop over a "
            "relation); this bundle is linear."
        )
    relations = sorted(workflow.data_sources.keys())
    worklists: dict[str, list[dict[str, str]]] = {}
    for spec in specs:
        if "=" in spec:
            name, _, raw = spec.partition("=")
            name = name.strip()
        else:
            if len(relations) != 1:
                raise SystemExit(
                    "bare --worklist <file> needs exactly one loop relation to "
                    f"bind to; this program declares {relations or 'none'}. Use "
                    "--worklist RELATION=<file>."
                )
            name, raw = relations[0], spec
        if relations and name not in relations:
            raise SystemExit(
                f"--worklist relation {name!r} is not one of this program's "
                f"relations {relations}"
            )
        worklists[name] = _load_worklist_file(Path(raw))
    return worklists


def _deployment_sections(args: argparse.Namespace):
    """Snapshot deployment config plus direct effects/actuation overrides."""
    from openadapt_flow.deployment import DeploymentConfig, load_deployment

    cfg = (
        load_deployment(args.config)
        if getattr(args, "config", None)
        else DeploymentConfig()
    )

    effects = cfg.effects
    if getattr(args, "effects_kind", None):
        effects = effects.model_copy(update={"kind": args.effects_kind})
    if getattr(args, "effects_base_url", None):
        effects = effects.model_copy(update={"base_url": args.effects_base_url})
    if getattr(args, "effects_root", None):
        effects = effects.model_copy(update={"root": args.effects_root})

    actuation = cfg.actuation
    if getattr(args, "api_base_url", None):
        actuation = actuation.model_copy(
            update={"api": True, "base_url": args.api_base_url}
        )
    elif getattr(args, "api_actuator", False):
        actuation = actuation.model_copy(update={"api": True})
    return cfg, effects, actuation


def _deployment_runtime(
    args: argparse.Namespace,
    params: Mapping[str, "RuntimeParamScalar"] | None = None,
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

    cfg, effects, actuation = _deployment_sections(args)
    try:
        effect_verifier = build_effect_verifier(
            effects,
            params=runtime_params_for_gui(params or {}),
        )
        api_actuator = build_api_actuator(actuation)
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
    return cfg, effect_verifier, api_actuator, durable, allow_egress


def _resolve_backend_config(args: argparse.Namespace, cfg, workflow=None):
    """Merge the ``--backend`` family of CLI flags over ``cfg.backend``.

    A deployment ``--config`` supplies the backend section; direct flags
    (``--backend`` / ``--agent-url`` / ``--macos-app`` / ``--linux-app`` /
    ``--rdp-host`` / ``--rdp-window`` / ``--rdp-window-title`` /
    ``--rdp-readiness-text``) override individual fields, exactly as the
    effects/actuation flags override their sections. A compiled window-scoped
    RDP/Citrix workflow may contribute the exact target recorded with the
    demonstration; explicit config/flags remain authoritative.
    """
    backend = cfg.backend
    hints = getattr(workflow, "backend_hints", None)
    configured = set(getattr(backend, "model_fields_set", set()))
    explicit_kind = getattr(args, "backend", None)
    configured_kind = backend.kind if "kind" in configured else None
    selected_kind = explicit_kind or configured_kind
    # Surface binding (Section 5): a surface-bound workflow supplies its own
    # target when neither a flag nor the config selects one. There is no
    # implicit browser default for a bound bundle; the browser is chosen only
    # when the workflow was actually recorded on it.
    bound_surface = getattr(workflow, "surface", None)
    if selected_kind is None and hints is None and bound_surface is not None:
        backend = backend.model_copy(update={"kind": bound_surface})
    if hints is not None and (
        selected_kind is None or _report_backend_kind(selected_kind) == hints.backend
    ):
        updates: dict[str, object] = {}
        if selected_kind is None:
            updates["kind"] = hints.backend
        for field in ("rdp_window", "rdp_window_title", "rdp_readiness_text"):
            if field not in configured:
                value = getattr(hints, field)
                if value is not None:
                    updates[field] = value
        if updates:
            backend = backend.model_copy(update=updates)
    if getattr(args, "backend", None):
        backend = backend.model_copy(update={"kind": args.backend})
    if getattr(args, "agent_url", None):
        backend = backend.model_copy(update={"agent_url": args.agent_url})
    if getattr(args, "macos_app", None):
        backend = backend.model_copy(update={"macos_app": args.macos_app})
    if getattr(args, "macos_window_title", None):
        backend = backend.model_copy(
            update={"macos_window_title": args.macos_window_title}
        )
    if getattr(args, "linux_app", None):
        backend = backend.model_copy(update={"linux_app": args.linux_app})
    if getattr(args, "linux_window_title", None):
        backend = backend.model_copy(
            update={"linux_window_title": args.linux_window_title}
        )
    if getattr(args, "linux_allow_physical_input", False):
        backend = backend.model_copy(update={"linux_allow_physical_input": True})
    if getattr(args, "rdp_host", None):
        backend = backend.model_copy(update={"rdp_host": args.rdp_host})
    if getattr(args, "rdp_window", None):
        backend = backend.model_copy(update={"rdp_window": args.rdp_window})
    if getattr(args, "rdp_window_title", None):
        backend = backend.model_copy(update={"rdp_window_title": args.rdp_window_title})
    if getattr(args, "rdp_readiness_text", None):
        backend = backend.model_copy(
            update={"rdp_readiness_text": args.rdp_readiness_text}
        )
    return backend


def _surface_selection_gate(
    args: argparse.Namespace, cfg, workflow, *, operation: str
) -> Optional[int]:
    """Enforce explicit surface selection (Section 5) before backend resolve.

    Returns an exit code to refuse with, or None to proceed. Production
    profiles (Standard/Regulated) require an explicit target: a ``--backend``
    flag, a configured ``backend.kind``, or a surface-bound workflow. The
    Demo/permissive posture may default to the browser (or the operator's
    last-used Demo target from the CLI state file) but says so visibly.
    Idempotent across the ``run`` -> ``_cmd_replay`` delegation.
    """
    if getattr(args, "_surface_selection_done", False):
        return None
    args._surface_selection_done = True
    from openadapt_flow.surface_selection import (
        demo_default_notice,
        explicit_surface_refusal,
        load_last_surface,
        store_last_surface,
    )

    profile = getattr(args, "_execution_profile", None)
    user_backend = getattr(args, "backend", None)
    explicitly_selected = bool(
        user_backend
        or "kind" in set(getattr(cfg.backend, "model_fields_set", set()))
        or getattr(workflow, "backend_hints", None) is not None
        or getattr(workflow, "surface", None) is not None
    )
    if profile == "demo" and user_backend:
        # The last-used target is a Demo-only CLI convenience, persisted in
        # the per-user state file and NEVER written into a workflow bundle.
        store_last_surface(_report_backend_kind(user_backend))
    if explicitly_selected:
        return None
    if profile in ("standard", "regulated"):
        print(explicit_surface_refusal(operation, profile))
        return 2
    last = load_last_surface() if profile == "demo" else None
    if last is not None:
        args.backend = last
    print(demo_default_notice(last or "web", from_last_used=last is not None))
    return None


def _enforce_surface_binding(
    args: argparse.Namespace, workflow, backend_cfg, *, operation: str
) -> Optional[int]:
    """Refuse a cross-surface run unless explicitly overridden (Section 5).

    A workflow recorded/qualified on one surface must not silently run on
    another. ``--allow-surface-override`` proceeds anyway and records itself
    in the run report (``surface_override``) as compatibility evidence; the
    executed override is threaded via ``args._surface_override``. Idempotent
    across the ``run`` -> ``_cmd_replay`` delegation.
    """
    if getattr(args, "_surface_binding_done", False):
        return None
    args._surface_binding_done = True
    recorded = getattr(workflow, "surface", None)
    if recorded is None:
        return None
    requested = _report_backend_kind(backend_cfg.kind)
    if requested == recorded:
        return None
    from openadapt_flow.surface_selection import (
        surface_mismatch_refusal,
        surface_override_notice,
    )

    if not getattr(args, "allow_surface_override", False):
        print(
            surface_mismatch_refusal(
                operation,
                recorded=recorded,
                requested=requested,
                execution_mode=getattr(workflow, "execution_mode", None),
            )
        )
        return 2
    args._surface_override = True
    print(surface_override_notice(recorded, requested))
    return None


def _stamp_recording_surface(recording_dir: Path, surface: str) -> None:
    """Stamp the recorded surface into ``meta.json`` (additive, Section 5).

    The compiler binds the compiled bundle to this exact surface. Best-effort
    by design: a recording written by an older converter without ``meta.json``
    simply compiles to a legacy, surface-unbound bundle.

    A recorder that stamps the surface itself before it publishes the
    recording leaves nothing to do here. This function then writes nothing, so
    a complete recording is never modified after its atomic publish.
    """
    import json

    meta_path = Path(recording_dir) / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return
    if meta.get("surface") == surface:
        return
    meta["surface"] = surface
    meta_path.write_text(json.dumps(meta, indent=2))


def _report_backend_kind(kind: object) -> "ExecutionTargetKind":
    """Return the closed hosted-summary token for a resolved backend kind.

    Execution accepts ``remote-display`` / ``remote_display`` as RDP aliases.
    Hosted summaries use a closed enum, so fold those aliases without
    collapsing the distinct Citrix product token to generic RDP.
    """
    token = str(kind).strip().lower()
    if token in ("remote-display", "remote_display"):
        return "rdp"
    if token not in {"web", "windows", "macos", "linux", "rdp", "citrix"}:
        raise ValueError(f"unsupported execution target kind: {kind!r}")
    return cast("ExecutionTargetKind", token)


def _refuse_missing_citrix_readiness(backend_cfg: object, *, operation: str) -> bool:
    """Refuse governed Citrix actuation without a nonblank frame marker."""
    kind = str(getattr(backend_cfg, "kind", "")).strip().lower()
    marker = str(getattr(backend_cfg, "rdp_readiness_text", "") or "").strip()
    if kind != "citrix" or marker:
        return False
    print(
        f"{operation} REFUSED: governed Citrix execution requires a stable "
        "current-frame readiness marker before any action. Set "
        "backend.rdp_readiness_text in --config or pass "
        "--rdp-readiness-text. Nothing was executed."
    )
    return True


_SAFE_BUNDLE_LOAD_GUIDANCE = (
    "Verify the bundle path and integrity; for a sealed bundle, set "
    "OPENADAPT_BUNDLE_KEY."
)


def _configured_replayer(
    backend,
    *,
    workflow,
    allow_egress: bool,
    effect_verifier,
    api_actuator,
    durable: bool,
    use_structural: bool,
    pixel_verify_enabled: bool = False,
    governed_authorization=None,
    delivery_authority_kind: Literal[
        "customer_local", "cloud_runner"
    ] = "customer_local",
    remote_delivery_run_id: Optional[str] = None,
    managed_dispatch_binding=None,
    production_qualification_guard=None,
    qualification_campaign_guard=None,
    runtime_config=None,
    checkpoint_key: Optional[str] = None,
):
    """Wire the grounding, verification, and actuation layers into a Replayer.

    Backend-agnostic: the on-prem VLM appliance (opt-in, egress-guarded), the
    operator-selected model grounder (opt-in, PHI-allowlisted), the OCR
    grounding rung, and the deployment wiring (effect verifier / API actuator /
    durable runtime) are identical for browser, Windows, macOS, and
    RDP/remote-display sessions. The caller owns the backend lifecycle.
    """
    from openadapt_flow import crypto
    from openadapt_flow.deployment import build_replayer

    profile_name = getattr(governed_authorization, "execution_profile", None)
    if checkpoint_key is None and profile_name is not None:
        from openadapt_flow.execution_profiles import execution_profile_contract

        contract = execution_profile_contract(profile_name)
        if contract.production and (
            contract.require_encryption or bool(getattr(workflow, "encrypted", False))
        ):
            checkpoint_key = crypto.require_key(None)

    return build_replayer(
        backend,
        allow_egress=allow_egress,
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        durable=durable,
        use_structural=use_structural,
        pixel_verify_enabled=pixel_verify_enabled,
        governed_authorization=governed_authorization,
        delivery_authority_kind=delivery_authority_kind,
        remote_delivery_run_id=remote_delivery_run_id,
        managed_dispatch_binding=managed_dispatch_binding,
        production_qualification_guard=production_qualification_guard,
        qualification_campaign_guard=qualification_campaign_guard,
        runtime_config=runtime_config,
        checkpoint_key=checkpoint_key,
    )


def _build_and_run_replayer(
    backend,
    *,
    workflow,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    bundle: Path,
    run_dir: Path,
    save_healed_to: Optional[Path],
    allow_egress: bool,
    effect_verifier,
    api_actuator,
    durable: bool,
    use_structural: bool,
    pixel_verify_enabled: bool = False,
    governed_authorization=None,
    delivery_authority_kind: Literal[
        "customer_local", "cloud_runner"
    ] = "customer_local",
    remote_delivery_run_id: Optional[str] = None,
    managed_dispatch_binding=None,
    production_qualification_guard=None,
    qualification_campaign_guard=None,
    runtime_config=None,
    execution_target_kind: Optional["ExecutionTargetKind"] = None,
    surface_override: bool = False,
    execution_origin: Optional[str] = None,
    execution_entry_url: Optional[str] = None,
    run_id: Optional[str] = None,
):
    """Build the shared Replayer configuration and execute one workflow."""
    return _configured_replayer(
        backend,
        workflow=workflow,
        allow_egress=allow_egress,
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        durable=durable,
        use_structural=use_structural,
        pixel_verify_enabled=pixel_verify_enabled,
        governed_authorization=governed_authorization,
        delivery_authority_kind=delivery_authority_kind,
        remote_delivery_run_id=remote_delivery_run_id,
        managed_dispatch_binding=managed_dispatch_binding,
        production_qualification_guard=production_qualification_guard,
        qualification_campaign_guard=qualification_campaign_guard,
        runtime_config=runtime_config,
    ).run(
        workflow,
        params=params,
        worklists=worklists,
        bundle_dir=bundle,
        run_dir=run_dir,
        save_healed_to=save_healed_to,
        execution_target_kind=execution_target_kind,
        surface_override=surface_override,
        execution_origin=execution_origin,
        execution_entry_url=execution_entry_url,
        run_id=(
            remote_delivery_run_id
            if delivery_authority_kind == "cloud_runner"
            else run_id
        ),
    )


def _replay_outcome_epilogue(
    report,
    outcome: str,
    run_dir: Path,
) -> Optional[str]:
    """Three-line epilogue after a non-VERIFIED replay (presentation only).

    What happened / why this is the safe behavior / the exact next command.
    Fail-closed semantics are untouched: this only explains an ending that
    already happened.
    """
    from openadapt_flow.tutorial import outcome_epilogue_lines

    halt = getattr(report, "halt", None)
    halted_step = next(
        (
            result.step_id
            for result in report.results
            if not result.skipped
            and (
                result.effect_verified is False
                or result.safety_halt
                or (
                    result.identity is not None and result.identity.status == "mismatch"
                )
            )
        ),
        None,
    )
    if outcome == "HALTED":
        where = f" at step `{halted_step}`" if halted_step else ""
        what = f"the run stopped{where} and ended {outcome}" + (
            f" ({halt.reason})" if halt is not None and halt.reason else ""
        )
        why_safe = (
            "the engine halts instead of acting on unproven state; nothing "
            "further was executed once the check failed"
        )
        return "\n".join(
            outcome_epilogue_lines(
                what=what,
                why_safe=why_safe,
                next_command=f"openadapt-flow explain {run_dir}",
            )
        )
    if outcome == "COMPLETED_UNVERIFIED":
        return "\n".join(
            outcome_epilogue_lines(
                what=(
                    "every executed step finished on screen, but nothing "
                    f"independently proved the writes landed ({outcome})"
                ),
                why_safe=("screen-only completion can never claim success under Flow"),
                next_command=(
                    "openadapt-flow scaffold-verifier <recording-or-bundle>  "
                    "# draft an oracle, wire effects:, re-run"
                ),
            )
        )
    return "\n".join(
        outcome_epilogue_lines(
            what=f"the run ended {outcome} and reported the failure loudly",
            why_safe="a failure is never guessed into a success",
            next_command=f"openadapt-flow explain {run_dir}",
        )
    )


def _finish_replay(
    run_dir: Path,
    report,
    args: Optional[argparse.Namespace] = None,
    *,
    backend_kind: Optional[str] = None,
) -> int:
    """Render the run report, print the outcome, and map it to an exit code."""
    from openadapt_flow.report import render_run_report

    report_md = render_run_report(run_dir)
    outcome = getattr(report, "execution_outcome", None) or (
        "success" if report.success else "FAILED"
    )
    print(f"Replay {outcome}: {report_md}")
    if report.screenshots_may_leave_box:
        print(
            "NOTE: a model-grounding component was wired for this run — "
            "screenshots could have left the box (see REPORT.md)."
        )
    if outcome not in {"VERIFIED", "success"}:
        epilogue = _replay_outcome_epilogue(report, outcome, run_dir)
        if epilogue:
            print(f"\n{epilogue}")
    _maybe_report_break(run_dir, report)
    _maybe_report_run(run_dir, report, args, backend_kind=backend_kind)
    _maybe_attest_run(run_dir, report, args)
    if getattr(report, "execution_profile", None) in {"standard", "regulated"}:
        return 0 if outcome == "VERIFIED" else 1
    return 0 if report.success else 1


def _replay_desktop(
    args: argparse.Namespace,
    backend_cfg,
    *,
    workflow,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    bundle: Path,
    run_dir: Path,
    allow_egress: bool,
    effect_verifier,
    api_actuator,
    durable: bool,
    pixel_verify_enabled: bool = False,
    governed_authorization=None,
    delivery_authority_kind: Literal[
        "customer_local", "cloud_runner"
    ] = "customer_local",
    remote_delivery_run_id: Optional[str] = None,
    managed_dispatch_binding=None,
    production_qualification_guard=None,
    qualification_campaign_guard=None,
    runtime_config=None,
    run_id: Optional[str] = None,
) -> int:
    """Replay against a non-browser native/remote backend built by the factory.

    No Playwright browser, no bundled MockMed, no session video — those are
    web-only. ``--drift`` (a MockMed teaching aid) is refused. The backend is
    built from the resolved ``BackendConfig`` and the shared replayer wiring runs
    exactly as it does for the web path.
    """
    from openadapt_flow.backends.factory import build_backend

    if args.drift:
        raise SystemExit(
            "--drift only demonstrates healing on the bundled MockMed web demo; "
            f"it does not apply to the {backend_cfg.kind!r} backend."
        )
    try:
        backend = build_backend(backend_cfg)
    except ValueError as e:
        raise SystemExit(str(e))

    try:
        report = _build_and_run_replayer(
            backend,
            workflow=workflow,
            params=params,
            worklists=worklists,
            bundle=bundle,
            run_dir=run_dir,
            save_healed_to=(Path(args.save_healed_to) if args.save_healed_to else None),
            allow_egress=allow_egress,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=durable,
            # No MockMed drift here, so the deterministic structural rung is
            # preferred exactly as in a non-drift web replay.
            use_structural=True,
            pixel_verify_enabled=pixel_verify_enabled,
            governed_authorization=governed_authorization,
            delivery_authority_kind=delivery_authority_kind,
            remote_delivery_run_id=remote_delivery_run_id,
            managed_dispatch_binding=managed_dispatch_binding,
            production_qualification_guard=production_qualification_guard,
            qualification_campaign_guard=qualification_campaign_guard,
            runtime_config=runtime_config,
            execution_target_kind=_report_backend_kind(backend_cfg.kind),
            surface_override=bool(getattr(args, "_surface_override", False)),
            run_id=run_id,
        )
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()  # RDP transports hold a live socket; browsers/agents don't
    return _finish_replay(
        run_dir, report, args, backend_kind=_report_backend_kind(backend_cfg.kind)
    )


def _cmd_record(args: argparse.Namespace) -> int:
    # The interactive (web) recorder installs in-page DOM listeners against a
    # headed Playwright page. The DESKTOP recorder (--backend windows) captures
    # the operator's native OS input over the win_agent contract; both emit the
    # SAME compile-ready recording format. Selection is fail-loud (a missing
    # target for the chosen backend raises rather than silently record web).
    #
    # Surface-selection neutrality (Section 5): a production posture
    # (--profile standard/regulated) requires an explicit --backend; only the
    # Demo/permissive posture may default to the browser, visibly. The
    # last-used target is a Demo-only convenience from the CLI state file.
    from openadapt_flow.surface_selection import (
        demo_default_notice,
        explicit_surface_refusal,
        load_last_surface,
        store_last_surface,
    )

    profile = getattr(args, "profile", None)
    backend = getattr(args, "backend", None)
    if backend is None:
        if profile in ("standard", "regulated"):
            print(explicit_surface_refusal("record", profile))
            return 2
        last = load_last_surface() if profile == "demo" else None
        backend = last or "web"
        print(demo_default_notice(backend, from_last_used=last is not None))
    elif profile == "demo":
        store_last_surface(_report_backend_kind(backend))
    browser_attach_requested = bool(
        getattr(args, "browser_cdp_endpoint", None)
        or getattr(args, "browser_page_url", None)
    )
    if backend in ("windows", "macos", "linux", "rdp", "citrix"):
        if browser_attach_requested:
            raise SystemExit(
                "record: --browser-cdp-endpoint and --browser-page-url apply "
                "only to --backend web"
            )
        return _cmd_record_desktop(args, backend)

    if (
        getattr(args, "window", None)
        or getattr(args, "window_title", None)
        or getattr(args, "rdp_window", None)
        or getattr(args, "rdp_window_title", None)
        or getattr(args, "rdp_readiness_text", None)
    ):
        # --window scopes a pixel/desktop capture to one on-screen window; the
        # web recorder drives a Playwright page and has no such notion. Refuse
        # rather than silently ignore the operator's requested scope.
        raise SystemExit(
            "record: --window/--window-title and --rdp-window/"
            "--rdp-window-title/--rdp-readiness-text apply only to the desktop "
            "backends (--backend windows/macos/linux/rdp/citrix); --backend web "
            "records the Playwright page given by --url."
        )
    _reject_unbound_record_target_flags(args, "web")

    if not args.url:
        raise SystemExit(
            "record --backend web requires --url (the app to record against)."
        )
    if getattr(args, "browser_page_url", None) and not getattr(
        args, "browser_cdp_endpoint", None
    ):
        raise SystemExit("record: --browser-page-url requires --browser-cdp-endpoint")
    if getattr(args, "browser_cdp_endpoint", None) and args.headless:
        raise SystemExit(
            "record: --headless cannot be combined with "
            "--browser-cdp-endpoint; the attached browser controls its own "
            "display mode"
        )

    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        record_interactive,
    )

    try:
        out = record_interactive(
            args.url,
            Path(args.out),
            secret_fields=tuple(args.secret or ()),
            param_fields=tuple(args.param or ()),
            identifier_fields=tuple(getattr(args, "identifier", None) or ()),
            headless=args.headless,
            cdp_endpoint=getattr(args, "browser_cdp_endpoint", None),
            browser_page_url=getattr(args, "browser_page_url", None),
            surface="web",
        )
    except BrowserAttachError as exc:
        raise SystemExit(f"record: browser attachment refused: {exc}") from exc
    _stamp_recording_surface(out, "web")
    print(f"Recording written to {out}")
    secrets = sorted(args.secret or ())
    if secrets:
        print(
            "Secret field(s) recorded (no value stored, and each value is "
            "redacted from the recorded URL, title, label, and structural "
            "text): "
            + ", ".join(secrets)
            + ". At replay, export "
            + ", ".join(f"OPENADAPT_FLOW_SECRET_{name.upper()}" for name in secrets)
        )
    for notice in _recording_privacy_notices(out):
        print(notice)
    return 0


def _recording_privacy_notices(recording_dir: Path) -> list[str]:
    """Report exactly what the recorder had to withhold, or nothing.

    The claim that a secret value is not stored is the claim the operator uses
    to decide whether a recording is safe to keep or to share. Where the
    recorder had to drop evidence to keep that claim true, say so here.
    """
    import json

    try:
        meta = json.loads((Path(recording_dir) / "meta.json").read_text())
    except (OSError, ValueError):
        return []
    notices: list[str] = []
    raw_withheld = meta.get("structural_text_withheld")
    if raw_withheld:
        explained = {
            "secret-value-left-its-document": (
                "a declared secret value left its document (for example a form "
                "submit that reflects the value into the next URL), and the new "
                "document builds a page closure that never saw that value"
            ),
            "reflected-text-changed-after-a-secret-value": (
                "the page URL or title changed after a declared secret field "
                "held a value, so Flow could not prove the new text was not a "
                "reflection of that value"
            ),
            "title-changed-after-a-secret-value": (
                "the page title changed after a declared secret field held a "
                "value. A title has no structure to reduce, so Flow withholds "
                "it rather than guess whether it reflects the value"
            ),
            "declared-value-in-url": (
                "the page URL held a declared secret value that no parameter "
                "name identified, so structure alone could not remove it"
            ),
            "declared-value-in-title": ("the page title held a declared secret value"),
            "url-cannot-be-parsed": (
                "the page reported a URL that is not an HTTP or HTTPS URL, so "
                "Flow could not reduce it by structure"
            ),
            "opaque-secret-boundary": (
                "a declared secret field sits behind a closed shadow root, "
                "which exposes its value to no check at all"
            ),
        }
        for reason in [
            part.strip() for part in str(raw_withheld).split(",") if part.strip()
        ]:
            notices.append(
                "Flow withheld the page URL and title because "
                f"{explained.get(reason, reason)}. Those actions carry an "
                "origin-only URL and an empty title. Flow reports page text "
                "exactly or not at all and never rewrites it, so the recording "
                "holds no secret value and no altered text."
            )
    dropped = meta.get("url_dropped_params")
    if isinstance(dropped, list) and dropped:
        names = sorted(
            {str(entry.get("name")) for entry in dropped if isinstance(entry, dict)}
        )
        notices.append(
            "Flow removed the value of these URL parameters and kept their "
            f"names: {', '.join(names)}. A parameter named after a declared "
            "secret field loses its value in every recorded URL, whatever the "
            "value is. The rest of each URL -- origin, path and other "
            "parameters -- is exact."
        )
    if meta.get("application_placed_secret_in_url") or meta.get(
        "application_placed_secret_in_title"
    ):
        notices.append(
            "WARNING: the application put a declared secret value into its own "
            "page URL or title. Flow withheld that text, but this is an "
            "application defect that exists with or without Flow: OWASP lists "
            "browser history, server logs, proxies, CDNs and the Referer "
            "header as places such a value is already exposed. Report it to "
            "the application owner."
        )
    withheld_identity = meta.get("identity_withheld_events")
    if isinstance(withheld_identity, int) and withheld_identity > 0:
        notices.append(
            f"{withheld_identity} action(s) carry no DOM selector: the element "
            "identity contained a declared secret value, so Flow refused to "
            "record it. Replay uses the remaining identity tiers for those "
            "actions."
        )
    return notices


def _cmd_record_desktop(args: argparse.Namespace, backend: str) -> int:
    """Record a live desktop demonstration for a native/pixel desktop backend.

    Reuses the tested capture stack: an ``openadapt-capture`` session captures
    the operator's real demonstration, then
    :func:`openadapt_flow.adapters.capture.convert_capture` emits the same
    compile-ready recording format the browser recorder produces — closing
    ``record -> compile -> replay`` on the desktop substrate through the CLI.

    The recording is substrate-agnostic (pixel frames + coordinates); the
    ``--backend`` selects intent and REPLAY wiring. For ``rdp`` (a remote
    display painted in a client WINDOW), capture must happen in the SAME pixel
    space the rdp backend replays in — record inside the remote session (or
    full-screen the client) so coordinates align; a cross-machine coordinate
    remap is a documented follow-up (docs/desktop/RECORDING.md).
    """
    if getattr(args, "url", None) is not None:
        raise SystemExit(
            f"record --backend {backend}: --url applies only to --backend web. "
            "Nothing was recorded."
        )
    if getattr(args, "headless", False):
        raise SystemExit(
            f"record --backend {backend}: --headless applies only to --backend "
            "web. Nothing was recorded."
        )

    # Resolve and validate every target flag before importing or starting
    # Capture. Surface-specific flags either scope this exact recording (macOS
    # and local RDP/Citrix windows), enter the existing remote-display binding
    # schema, or fail loud. None can be accepted and then ignored.
    window = _resolve_record_capture_window(args, backend)

    if args.secret:
        # Field-level secret redaction relies on DOM field geometry (the
        # browser recorder blacks out the field rect). A pixel/desktop capture
        # has no such geometry, so we refuse rather than persist an unredacted
        # secret frame — a silent PHI leak. Deferred (docs/desktop/RECORDING.md).
        raise SystemExit(
            f"record --backend {backend}: --secret is not yet supported on the "
            "pixel/desktop substrate (no field geometry to redact the typed "
            "value from the captured frames). Use a masked/password field, or "
            "see docs/desktop/RECORDING.md for the deferred design."
        )

    # On the desktop substrate there is no field identity, so a parameter is
    # keyed by its demonstrated VALUE: --param NAME=VALUE (mirrors
    # convert_capture / the replay --param contract).
    params = _parse_params(args.param)

    # Record-identifying region: a pixel capture has no field identity, so
    # --identifier takes the region itself (X,Y,W,H, recording pixels), marked
    # once per recording. Fail loud on field-name syntax rather than silently
    # record an unmarked session.
    identifier_region = _parse_identifier_region_arg(
        getattr(args, "identifier", None) or (), backend=backend
    )

    from openadapt_flow.desktop_record import record_desktop_capture

    task = args.task or f"openadapt-flow {backend} recording"
    out = record_desktop_capture(
        Path(args.out),
        task_description=task,
        params=params,
        identifier_region=identifier_region,
        window=window,
        backend_kind=backend if backend in ("rdp", "citrix") else None,
        replay_window=getattr(args, "rdp_window", None),
        replay_window_title=getattr(args, "rdp_window_title", None),
        readiness_text=getattr(args, "rdp_readiness_text", None),
    )
    _stamp_recording_surface(out, backend)
    print(f"Recording written to {out}")
    if window is not None:
        print(
            "Window-scoped recording completed (frames are that window's own "
            "pixels; sensitive target identity is stored only in local "
            "recording metadata for the pixel replay surface)."
        )
    if params:
        print(
            "Recorded parameter(s): "
            + ", ".join(f"{k}={v!r}" for k, v in params.items())
            + ". Override at replay with --param NAME=VALUE."
        )
    print(
        "Compile it:  openadapt-flow compile "
        f"{out} --out <bundle> --name <workflow>\n"
        f"Then replay: openadapt-flow replay <bundle> --backend {backend} …"
    )
    return 0


def _cmd_demo_record(args: argparse.Namespace) -> int:
    from openadapt_flow.demo_driver import record_triage_demo
    from openadapt_flow.mockmed.server import serve

    url, stop = serve(port=0)
    try:
        url = _with_drift(url, args.drift)
        out = record_triage_demo(
            url,
            Path(args.out),
            note_text=args.note_text,
            param_name=args.param_name,
            headed=args.headed,
            record_video_dir=args.record_video,
        )
        print(f"Recording written to {out}")
    finally:
        stop()
    return 0


def _cmd_tutorial(args: argparse.Namespace) -> int:
    """Run the bundled tutorial end to end and emit its local receipt.

    The free path, composed: record -> compile -> certify -> governed run under
    the Standard profile with independent effect verification -> receipt.
    Unlike ``replay`` (Demo profile, which can only ever report
    ``COMPLETED_UNVERIFIED``), this path carries the evidence the Standard
    profile requires, so the run terminates ``VERIFIED`` because the write was
    confirmed in a system of record -- never because a gate was relaxed.
    """
    from datetime import datetime, timezone

    from openadapt_flow.tutorial import (
        GUIDED_PRESENTATION_DELAY_S,
        TUTORIAL_WORKFLOW_NAME,
        TutorialError,
        _next_steps_block,
        run_tutorial,
        tutorial_epilogue,
    )

    out = (
        Path(args.out)
        if args.out
        else Path("tutorials")
        / ("tutorial-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    )
    interactive_record = bool(args.interactive_record or args.guided)
    presentation_delay_s = args.presentation_delay
    if presentation_delay_s is None:
        presentation_delay_s = GUIDED_PRESENTATION_DELAY_S if args.guided else 0.0
    try:
        result = run_tutorial(
            out,
            headed=bool(args.headed or args.guided),
            name=args.name or TUTORIAL_WORKFLOW_NAME,
            emit_receipt=not args.no_receipt,
            interactive_record=interactive_record,
            presentation_delay_s=presentation_delay_s,
            echo=print,
            break_it=args.break_it,
        )
    except TutorialError as e:
        print(f"\nTutorial REFUSED: {e}")
        return 2

    if result.break_it is not None:
        print("\n--- clean run: the certified bundle, against an honest backend ---")
    print(f"\n{result.execution_outcome}: {result.run_dir / 'REPORT.md'}")
    print(f"  transaction     {result.transaction_outcome}")
    metering_class = "billable" if result.transaction_billable else "not billable"
    local_charge = (
        "reported"
        if result.reported_to_metering
        else "this local tutorial was not reported or charged"
    )
    print(f"  metering class  {metering_class} ({local_charge})")
    print(f"  profile         {result.execution_profile}")
    print(f"  model calls     {result.model_calls}")
    print(
        f"  effects         {result.effects_confirmed}/{result.effects_required} "
        f"confirmed at evidence tier {result.effect_tier} "
        "(independent system of record)"
    )
    print(f"  bundle digest   {result.bundle_digest}")
    if result.receipt_paths:
        print(f"\nShareable receipt: {result.receipt_paths['png']}")
        print(f"                   {result.receipt_paths['json']}")

    if result.break_it is not None:
        _print_break_it_narrative(result.break_it)
    elif result.execution_outcome == "VERIFIED":
        print(
            "\nNext: rerun this same bundle against a backend that lies -- and "
            "watch the engine halt:\n  openadapt-flow tutorial --break-it"
        )
        print(f"\n{_next_steps_block()}")
    else:
        # Presentation-only epilogue for the non-VERIFIED endings: what
        # happened, why this is the safe behavior, the exact next command.
        print("\n" + "\n".join(tutorial_epilogue(result)))
    if result.execution_outcome != "VERIFIED":
        return 1
    return 0


def _print_break_it_narrative(broken: "BreakItResult") -> None:
    """Tell the caught-fault story from the halted run's own evidence.

    Every fact printed here was read back from the broken run's report or the
    fault server's ground-truth store by ``run_tutorial``; nothing is scripted.
    """
    claim = (
        "every on-screen check passed -- the app painted its success banner"
        if broken.screen_claimed_success
        else "the app's success banner did not appear"
    )
    if broken.screen_claim_text:
        claim += f'\n                       (observed on screen: "{broken.screen_claim_text}")'
    print("\n--- break-it: the same certified bundle, against a backend that lies ---")
    print(f"\n  Injected fault:      {broken.fault!r} -- the server rejects the write")
    print("                       AFTER the app reports success")
    print(f"  The screen claimed:  {claim}")
    print(
        "  The verifier found:  "
        f"{broken.effects_refuted}/{broken.effects_required} declared effect(s) "
        "REFUTED by an independent\n"
        "                       read of the system of record, which holds "
        f"{broken.system_of_record_records} record(s)"
    )
    print(
        f"  The engine did:      {broken.execution_outcome} at the consequential "
        "step instead of claiming\n"
        f"                       success (transaction: {broken.transaction_outcome}, "
        f"billable: {'yes' if broken.transaction_billable else 'no'})"
    )
    print(
        "\n  The screen said success. The system of record said otherwise. "
        "The engine\n  believed the system of record."
    )
    print(f"\n  Engine's own words:  {broken.halt_reason}")
    print(
        "\nCaught-fault evidence (local report, NOT a success receipt):\n"
        f"  {broken.report_path}"
    )
    print(
        "\nNo shareable receipt for the halted run: only VERIFIED runs may use "
        "the\nsuccess rail. The halt itself is the demonstration."
    )
    print(
        "\nNext: record your own workflow:\n"
        "  openadapt-flow record --backend web --url <your app>"
    )


def _cmd_compile(args: argparse.Namespace) -> int:
    import json

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler import param_confirm as pc

    workflow = compile_recording(Path(args.recording), Path(args.out), name=args.name)
    print(
        f"Compiled {len(workflow.steps)} steps into {args.out} "
        f"(workflow: {workflow.name!r})"
    )

    # One-shot confirm pass for flagged field-label parameter proposals.
    # Fail-closed: with no decision channel (no flags, no TTY) every proposal
    # stays a demonstrated constant and the bundle above is final.
    if args.params_from and args.accept_params:
        raise SystemExit("use --params-from or --accept-params, not both")
    proposals = pc.load_proposals(Path(args.out))
    if not proposals:
        return 0
    decisions: list[pc.ParamDecision] = []
    if args.params_from:
        decisions_json = json.loads(Path(args.params_from).read_text())
        if not isinstance(decisions_json, dict):
            raise SystemExit("--params-from: file must be a JSON object")
        decisions = pc.decisions_from_file(proposals, decisions_json)
    elif args.accept_params:
        names = [n.strip() for n in args.accept_params.split(",") if n.strip()]
        decisions = pc.decisions_from_accept_list(proposals, names)
    elif sys.stdin.isatty() and sys.stdout.isatty() and not args.no_confirm_params:
        decisions = pc.decisions_interactive(proposals)
    else:
        print(
            f"{len(proposals)} field-label parameter proposal(s) left "
            "unconfirmed (kept as demonstrated constants). Review "
            f"{Path(args.out) / pc.PROPOSALS_FILENAME} and re-run with "
            "--accept-params or --params-from to confirm."
        )
        return 0
    if not decisions:
        print("No parameter proposals confirmed; bundle unchanged.")
        return 0
    pc.apply_decisions(
        Path(args.recording), Path(args.out), name=args.name, decisions=decisions
    )
    for d in decisions:
        kind = "secret parameter" if d.secret else "parameter"
        print(f"  confirmed {kind} {d.name!r} ({d.step_id})")
        if d.secret:
            print(
                f"    note: supply OPENADAPT_FLOW_SECRET_{d.name.upper()} at "
                "replay; the bundle carries no literal, but the RECORDING "
                "still does (re-record with --secret for full at-rest "
                "secrecy)."
            )
    print(f"Recompiled with {len(decisions)} confirmed parameter(s).")
    return 0


def _cmd_induce(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.induction import induce_program, validate_held_out
    from openadapt_flow.ir import Workflow

    # Accept both RECORDING directories (compiled via the single-trace
    # bootstrap by induce_program) and already-compiled BUNDLE directories
    # (a dir containing workflow.json -> loaded as a Workflow). Detecting the
    # bundle case CLI-side keeps induce usable on artifacts the operator
    # already has, without touching the library API.
    traces: list = []
    for d in args.recording:
        path = Path(d)
        if (path / "workflow.json").is_file():
            traces.append(Workflow.load(path))
        else:
            traces.append(path)

    result = induce_program(traces)
    print(result.render())

    if args.held_out and len(traces) >= 2:
        print(validate_held_out(traces).render())

    if not result.certified or result.workflow is None:
        # Refuse rather than guess: surface the uncertainties honestly and exit
        # nonzero so a CI / deploy gate refuses the underdetermined program.
        print(
            "\nNOT CERTIFIED — no program bundle written. See record-next: for "
            "the missing demonstration(s)."
        )
        return 2

    workflow = result.workflow
    if args.name:
        workflow = workflow.model_copy(update={"name": args.name})
    out = Path(args.out)
    workflow.save(out)
    print(
        f"\nCERTIFIED — induced program bundle written to {out} "
        f"(workflow: {workflow.name!r}, "
        f"{len(result.param_specs)} param(s), "
        f"{len(result.column_decisions)} column decision(s))."
    )
    return 0


def _cmd_for_each(args: argparse.Namespace) -> int:
    import shutil

    from openadapt_flow.compiler.loop_authoring import (
        LoopAuthoringError,
        author_data_driven_loop,
    )
    from openadapt_flow.ir import Workflow

    src = Path(args.bundle)
    if (
        not (src / "workflow.json").is_file()
        and not (src / "workflow.json.enc").is_file()
    ):
        raise SystemExit(f"{src} is not a workflow bundle (no workflow.json)")
    body = Workflow.load(src)

    records = _load_worklist_file(Path(args.records))

    # Parse --map col=param entries into an explicit column -> param mapping.
    column_map: dict[str, str] = {}
    for raw in args.map or []:
        if "=" not in raw:
            raise SystemExit(f"--map expects COLUMN=PARAM, got {raw!r}")
        col, param = raw.split("=", 1)
        col, param = col.strip(), param.strip()
        if not col or not param:
            raise SystemExit(f"--map expects COLUMN=PARAM, got {raw!r}")
        if col in column_map:
            raise SystemExit(f"--map column {col!r} given more than once")
        column_map[col] = param

    try:
        looped = author_data_driven_loop(
            body,
            records,
            column_map=column_map or None,
            relation=args.relation,
            max_iterations=args.max_iterations,
            loop_var=args.loop_var or "",
            name=args.name,
        )
    except LoopAuthoringError as exc:
        # Fail loudly on any param/column mismatch -- never emit a bundle.
        raise SystemExit(str(exc))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Copy the demonstrated body's templates so the looped bundle is a complete,
    # self-verifying artifact (the loop body reuses the body's anchors/crops).
    src_templates = src / "templates"
    if src_templates.is_dir():
        shutil.copytree(src_templates, out / "templates", dirs_exist_ok=True)
    looped.save(out)

    relation = args.relation
    rel = looped.data_sources[relation]
    print(
        f"Authored data-driven LOOP bundle at {out} (workflow: "
        f"{looped.name!r}): {len(rel.rows)} record(s) over relation "
        f"{relation!r}, body = {len(body.steps)} demonstrated step(s), "
        f"program: true."
    )
    return 0


def _cmd_compose(args: argparse.Namespace) -> int:
    from openadapt_flow.cli_compose import cmd_compose

    return cmd_compose(args)


def _cmd_process(args: argparse.Namespace) -> int:
    from openadapt_flow.cli_process import cmd_process

    return cmd_process(args)


def _default_run_dir() -> Path:
    """Timestamped default run directory under ``runs/``."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path("runs") / f"replay-{stamp}"


def _cmd_replay(args: argparse.Namespace) -> int:
    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.cli_compose import refuse_replay_composition
    from openadapt_flow.cli_process import refuse_replay_process
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    refuse_replay_composition(bundle)
    refuse_replay_process(bundle)
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir()
    workflow = Workflow.load(bundle)
    qualification_case = getattr(args, "_qualification_case_execution", None)
    params = (
        qualification_case["params"]
        if qualification_case is not None
        else _replay_params(args.param, getattr(args, "params_file", None))
    )

    # Deployment wiring (from --config and/or direct flags): a system-of-record
    # EffectVerifier, an ApiActuator, durable-runtime, and the egress opt-in.
    # All default to off, so an unconfigured replay behaves exactly as before.
    (
        cfg,
        effect_verifier,
        api_actuator,
        durable,
        allow_egress,
    ) = _deployment_runtime(args, params=params)
    # Qualification cases are always Standard and must keep their durable
    # evidence/recovery contract through this second deployment resolution.
    # ``_cmd_run`` already checked this; retain it here so a future replay
    # wiring change cannot silently construct a non-durable Replayer.
    if qualification_case is not None:
        durable = True
    worklists = (
        qualification_case["worklists"]
        if qualification_case is not None
        else _resolve_worklists(getattr(args, "worklist", None), workflow)
    )

    # Backend selection (web/native/remote, overriding --config). A
    # non-web backend drives a native desktop / RDP / remote-display session with
    # no browser: delegate to the desktop path.
    #
    # Section 5: production profiles refuse an implicit target; the permissive
    # posture defaults visibly. A surface-bound workflow then supplies its own
    # target and refuses to run cross-surface without a recorded override.
    operation = "run" if getattr(args, "command", None) == "run" else "replay"
    refused = _surface_selection_gate(args, cfg, workflow, operation=operation)
    if refused is not None:
        return refused
    backend_cfg = _resolve_backend_config(args, cfg, workflow)
    refused = _enforce_surface_binding(args, workflow, backend_cfg, operation=operation)
    if refused is not None:
        return refused
    # Opt-in attest pre-state snapshot: the earliest point where the run is
    # ADMITTED (every gate above passed) and the run directory is final, but
    # NO actuation has begun on either the desktop or the web path below.
    _maybe_attest_pre_state(run_dir, args)
    if _normalize_kind(backend_cfg.kind) != "web":
        return _replay_desktop(
            args,
            backend_cfg,
            workflow=workflow,
            params=params,
            worklists=worklists,
            bundle=bundle,
            run_dir=run_dir,
            allow_egress=allow_egress,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=durable,
            pixel_verify_enabled=cfg.runtime.pixel_verify_enabled,
            governed_authorization=getattr(args, "_governed_run_authorization", None),
            delivery_authority_kind=getattr(
                args, "_delivery_authority_kind", "customer_local"
            ),
            remote_delivery_run_id=getattr(args, "_remote_delivery_run_id", None),
            managed_dispatch_binding=getattr(args, "_managed_dispatch_binding", None),
            production_qualification_guard=getattr(
                args, "_production_qualification_guard", None
            ),
            qualification_campaign_guard=getattr(
                args, "_qualification_campaign_guard", None
            ),
            runtime_config=cfg.runtime,
            run_id=getattr(args, "_qualification_run_id", None),
        )

    headed = args.headed or cfg.backend.headed
    url = args.url or cfg.backend.url
    if url and args.drift:
        raise SystemExit(
            "--drift only applies to the bundled MockMed demo app; "
            "omit --url to use it (drift your own app for real)."
        )

    stop = None
    if url is None:
        from openadapt_flow.mockmed.server import serve

        url, stop = serve(port=0)
        url = _with_drift(url, args.drift)
        drift_note = f" (drift: {args.drift})" if args.drift else ""
        print(f"No --url given; replaying against bundled MockMed{drift_note}")

    video_dir = getattr(args, "record_video", None)
    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            # OPT-IN session video (default off): a recorded replay lives in a
            # context so Playwright can attach the recorder; None keeps the old
            # direct-page path with zero effect.
            context = None
            if video_dir is not None:
                context = browser.new_context(
                    viewport=_VIEWPORT,
                    record_video_dir=video_dir,
                    record_video_size=_VIEWPORT,
                )
                page = context.new_page()
            else:
                page = browser.new_page(viewport=_VIEWPORT)
            page.goto(url)
            try:
                # The browser backend is built through the same factory as the
                # desktop backends; the grounding / identity / deployment wiring
                # and the run are shared (see _build_and_run_replayer). ``--drift``
                # (a MockMed teaching aid) forces the visual floor so the healing
                # ladder is exercised instead of the structural rung resolving it.
                report = _build_and_run_replayer(
                    build_backend(backend_cfg, page=page),
                    workflow=workflow,
                    params=params,
                    worklists=worklists,
                    bundle=bundle,
                    run_dir=run_dir,
                    save_healed_to=(
                        Path(args.save_healed_to) if args.save_healed_to else None
                    ),
                    allow_egress=allow_egress,
                    effect_verifier=effect_verifier,
                    api_actuator=api_actuator,
                    durable=durable,
                    use_structural=not bool(args.drift),
                    pixel_verify_enabled=cfg.runtime.pixel_verify_enabled,
                    governed_authorization=getattr(
                        args, "_governed_run_authorization", None
                    ),
                    delivery_authority_kind=getattr(
                        args, "_delivery_authority_kind", "customer_local"
                    ),
                    remote_delivery_run_id=getattr(
                        args, "_remote_delivery_run_id", None
                    ),
                    managed_dispatch_binding=getattr(
                        args, "_managed_dispatch_binding", None
                    ),
                    production_qualification_guard=getattr(
                        args, "_production_qualification_guard", None
                    ),
                    qualification_campaign_guard=getattr(
                        args, "_qualification_campaign_guard", None
                    ),
                    runtime_config=cfg.runtime,
                    execution_target_kind="web",
                    surface_override=bool(getattr(args, "_surface_override", False)),
                    execution_origin=(
                        f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
                    ),
                    execution_entry_url=url,
                    run_id=getattr(args, "_qualification_run_id", None),
                )
            finally:
                video_path = None
                if context is not None:
                    try:
                        video_path = page.video.path() if page.video else None
                    except Exception:
                        video_path = None
                    context.close()  # flush the recorded video to disk
                browser.close()
                if video_path is not None:
                    print(f"Session video written to {video_path}")
    finally:
        if stop is not None:
            stop()

    return _finish_replay(
        run_dir, report, args, backend_kind=_report_backend_kind(backend_cfg.kind)
    )


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute a bundle under a named deployment profile -- FAIL-CLOSED.

    ``run`` selects Demo, Standard, or Regulated over the same admission gate,
    authorization, and replayer. An omitted profile retains the pre-profile
    compatibility contract. On any refusal it prints the coverage report naming
    the failing gate and exits nonzero WITHOUT executing.
    ``--dry-run`` / ``--explain`` print the coverage report and stop before
    execution. Once admitted, it delegates to the shared executor (the same
    backend / effect / actuation / durable runtime as ``replay``), with
    ``--drift`` (a MockMed-only teaching aid) forced off.
    """
    from openadapt_flow.composition import is_composition_artifact
    from openadapt_flow.execution_profiles import (
        execution_profile_contract,
        resolve_execution_profile,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.production_qualification import (
        ProductionQualificationAuthorityError,
        ProductionQualificationGuard,
    )
    from openadapt_flow.qualification_campaign_authority import (
        QualificationCampaignAuthorityError,
        QualificationCampaignGuard,
    )
    from openadapt_flow.run_gate import (
        build_qualification_case_authorization,
        build_runtime_authorization,
        evaluate_run_gate,
    )
    from openadapt_flow.runner.dispatch_envelope import (
        ManagedDispatchEnvelopeError,
        read_managed_dispatch_envelope,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    bundle = Path(args.bundle)
    if is_composition_artifact(bundle):
        from openadapt_flow.cli_compose import cmd_run_composition

        return cmd_run_composition(args, run_child=_cmd_run)
    from openadapt_flow.admitted_composition import is_process_contract_artifact

    if is_process_contract_artifact(bundle):
        from openadapt_flow.cli_process import cmd_run_process

        return cmd_run_process(args, run_child=_cmd_run)
    # Load the bundle first (decrypting if encrypted -- the key comes from
    # --config/env via OPENADAPT_BUNDLE_KEY); a missing/wrong key fails LOUDLY.
    try:
        workflow = Workflow.load(bundle)
    except Exception:  # crypto / integrity / structural errors -> fail closed
        # Never echo a provider/Pydantic error here. A malformed local bundle
        # can contain PHI-bearing target metadata and validation errors may
        # repeat the rejected input verbatim.
        print(
            "run REFUSED: bundle could not be loaded safely. "
            f"{_SAFE_BUNDLE_LOAD_GUIDANCE} Nothing was executed."
        )
        return 2

    qualification_case = getattr(args, "_qualification_case_execution", None)
    gate_params = (
        qualification_case["params"]
        if qualification_case is not None
        else _replay_params(args.param, getattr(args, "params_file", None))
    )
    cfg, effect_verifier, api_actuator, configured_durable, _egress = (
        _deployment_runtime(args, params=gate_params)
    )
    profile_name = getattr(args, "profile", None) or cfg.runtime.profile
    selected_profile = None
    profile = None
    if profile_name is not None:
        try:
            selected_profile = resolve_execution_profile(profile_name)
        except ValueError as exc:
            print(f"run REFUSED: {exc}. Nothing was executed.")
            return 2
        profile = execution_profile_contract(selected_profile)
        args._execution_profile = selected_profile.value
    if qualification_case is not None and selected_profile is None:
        print(
            "run REFUSED: qualification evidence requires the Standard profile. "
            "Nothing was executed."
        )
        return 2
    if qualification_case is not None and selected_profile.value != "standard":
        print(
            "run REFUSED: qualification evidence can run only under the Standard "
            "profile. Nothing was executed."
        )
        return 2
    effective_durable = bool(
        configured_durable or (profile is not None and profile.require_durable)
    )
    effective_require_settled = bool(
        cfg.runtime.require_settled or (profile is not None and profile.require_settled)
    )
    if (
        profile is not None
        and profile.require_encryption
        and bool(getattr(args, "allow_unencrypted", False))
    ):
        assert selected_profile is not None
        print(
            f"run REFUSED: the {selected_profile.value} profile requires "
            "encrypted bundles; --allow-unencrypted cannot weaken a named "
            "profile. Select Standard or Demo only when the qualified storage "
            "boundary protects plaintext artifacts. Nothing was executed."
        )
        return 2
    # Section 5: a production profile requires an explicit execution surface
    # BEFORE authorization; the Demo posture may default visibly. A
    # surface-bound bundle then refuses a cross-surface run without the
    # report-recorded override.
    refused = _surface_selection_gate(args, cfg, workflow, operation="run")
    if refused is not None:
        return refused
    backend_cfg = _resolve_backend_config(args, cfg, workflow)
    refused = _enforce_surface_binding(args, workflow, backend_cfg, operation="run")
    if refused is not None:
        return refused
    if _refuse_missing_citrix_readiness(backend_cfg, operation="run"):
        return 2
    policy_source = args.policy or cfg.policy.policy

    report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=cfg,
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        policy_source=policy_source,
        approval_available=bool(getattr(args, "approve_unverified_writes", False)),
        strict_templates=bool(getattr(args, "strict_templates", False)),
        require_encryption=not bool(getattr(args, "allow_unencrypted", False)),
        pinned_content_digest=getattr(args, "pin_digest", None),
        pinned_compiler_version=getattr(args, "pin_version", None),
        profile_contract=profile,
        effective_durable=effective_durable if profile is not None else None,
        effective_require_settled=(
            effective_require_settled if profile is not None else None
        ),
        qualification_evidence_only=qualification_case is not None,
    )
    print(report.render())

    if not report.passed:
        # The local admission gate is always first. A dispatch envelope cannot
        # turn a locally refused bundle into an admitted one.
        return 2

    dispatch_file = getattr(args, "managed_dispatch_file", None)
    if qualification_case is not None and dispatch_file:
        print(
            "run REFUSED: a qualification evidence run cannot accept a managed "
            "dispatch. Nothing was executed."
        )
        return 2
    runtime_params = _replay_params(args.param, getattr(args, "params_file", None))
    runtime_worklists = (
        qualification_case["worklists"]
        if qualification_case is not None
        else _resolve_worklists(getattr(args, "worklist", None), workflow)
    )
    managed_binding = None
    authorization = None
    local_authorization = None
    if dispatch_file:
        try:
            managed_binding = read_managed_dispatch_envelope(Path(dispatch_file))
            authorization = managed_binding.authorization
        except ManagedDispatchEnvelopeError:
            print(
                "run REFUSED: managed dispatch binding is invalid. Nothing was executed."
            )
            return 2
        local_authorization = build_runtime_authorization(
            workflow,
            report,
            params=runtime_params,
            worklists=runtime_worklists,
        )
        safety_fields = (
            "bundle_content_digest",
            "runtime_inputs_digest",
            "admitted_policy_name",
            "admitted_policy_contract_sha256",
            "execution_profile",
            "minimum_effect_tier",
            "qualified_effect_requirements",
            "required_identity_step_ids",
            "unverified_write_approvals",
        )
        if (
            selected_profile is None
            or selected_profile.value not in {"standard", "regulated"}
            or workflow.manifest is None
            or any(
                getattr(authorization, field) != getattr(local_authorization, field)
                for field in safety_fields
            )
            or (
                authorization.bundle_content_digest != workflow.manifest.content_digest
                or authorization.validate_workflow(workflow) is not None
                or runtime_inputs_digest(workflow, runtime_params, runtime_worklists)
                != authorization.runtime_inputs_digest
            )
        ):
            print(
                "run REFUSED: managed dispatch does not match this exact run. Nothing was executed."
            )
            return 2

    if getattr(args, "dry_run", False) or getattr(args, "explain", False):
        # Report-only paths retain the complete local gate and, when supplied,
        # the exact managed-dispatch check above. They do not require an
        # actuation authority because they never cross an input edge.
        return 0 if report.passed else 2

    production_guard = None
    campaign_guard = None
    campaign_permit_binding = None
    if qualification_case is not None:
        authority_file = getattr(args, "qualification_campaign_authority_file", None)
        if authority_file:
            try:
                campaign_guard = QualificationCampaignGuard(
                    authority_file,
                    workflow=workflow,
                    case_id=qualification_case["case"].id,
                    input_digest=runtime_inputs_digest(
                        workflow, gate_params, runtime_worklists
                    ),
                    campaign_id=qualification_case["campaign_id"],
                    run_id=qualification_case["run_id"],
                )
                campaign_permit_binding = campaign_guard.authorization_binding(workflow)
            except QualificationCampaignAuthorityError:
                print(
                    "run REFUSED: the non-production qualification campaign permit "
                    "is invalid, expired, consumed, or does not match this exact "
                    "trial. Nothing was executed."
                )
                return 2
            args._qualification_campaign_guard = campaign_guard
        if campaign_permit_binding is not None and local_authorization is not None:
            local_authorization = local_authorization.model_copy(
                update=campaign_permit_binding
            )
    authority_file = getattr(args, "qualification_authority_file", None)
    if authority_file:
        try:
            production_guard = ProductionQualificationGuard(
                authority_file,
                remote_permit_revalidation=bool(dispatch_file),
            )
            production_binding = production_guard.authorization_binding(workflow)
        except ProductionQualificationAuthorityError:
            print(
                "run REFUSED: the Production qualification authority is invalid, "
                "expired, revoked, or does not match this exact run. Nothing was "
                "executed."
            )
            return 2
        args._production_qualification_guard = production_guard
        if local_authorization is None:
            local_authorization = build_runtime_authorization(
                workflow,
                report,
                params=runtime_params,
                worklists=runtime_worklists,
            )
        local_authorization = local_authorization.model_copy(update=production_binding)

    if dispatch_file:
        assert managed_binding is not None
        assert authorization is not None
        assert local_authorization is not None
        production_fields = tuple(
            field
            for field in local_authorization.model_fields
            if field.startswith("production_qualification_")
        )
        if any(
            getattr(authorization, field) != getattr(local_authorization, field)
            for field in production_fields
        ):
            print(
                "run REFUSED: managed dispatch does not bind the active Production "
                "qualification authority. Nothing was executed."
            )
            return 2
        args._governed_run_authorization = authorization
        args._managed_dispatch_binding = managed_binding
        args._delivery_authority_kind = "cloud_runner"
        args._remote_delivery_run_id = managed_binding.run_id

    if qualification_case is not None:
        runtime_worklists = qualification_case["worklists"]
        try:
            authorization = build_qualification_case_authorization(
                workflow,
                report,
                case_id=qualification_case["case"].id,
                params=gate_params,
                worklists=runtime_worklists,
                campaign_id=qualification_case["campaign_id"],
                run_id=qualification_case["run_id"],
                campaign_permit_binding=campaign_permit_binding,
            )
        except ValueError:
            print(
                "run REFUSED: qualification evidence does not match this exact "
                "case. Nothing was executed."
            )
            return 2
        args._governed_run_authorization = authorization
        args._delivery_authority_kind = "customer_local"
        args._remote_delivery_run_id = None
        args._qualification_run_id = qualification_case["run_id"]

    if not dispatch_file and qualification_case is None:
        args._governed_run_authorization = local_authorization or (
            build_runtime_authorization(
                workflow,
                report,
                params=runtime_params,
                worklists=runtime_worklists,
            )
        )
        args._delivery_authority_kind = "customer_local"
        args._remote_delivery_run_id = None

    if qualification_case is not None and not _claim_qualification_case_attempt(args):
        return 2

    # Admitted. A deployment run is not the drift-demo; force it off and delegate
    # to the shared replay executor (which reads all deployment wiring itself).
    args.drift = None
    return _cmd_replay(args)


def _cmd_resume(args: argparse.Namespace) -> int:
    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable import resume, resume_point
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    run_dir = Path(args.run_dir)
    # Encrypted runs (OPENADAPT_BUNDLE_KEY set) need the key to read the pause;
    # unset => None => plaintext, unchanged.
    ckpt_key = crypto.resolve_key(None)
    store = CheckpointStore(run_dir, key=ckpt_key)
    pending = store.read_pending()
    if pending is None:
        print(
            f"No pending escalation at {run_dir} — nothing to resume "
            "(a run only durably pauses when executed with a durable "
            "deployment; see --config runtime.durable / --durable)."
        )
        return 1
    if args.require_approval and pending.status != "approved":
        print(
            f"Pending escalation at {run_dir} is {pending.status!r}, not "
            "'approved'. Approval is required before resume:\n"
            f"    openadapt flow approve {run_dir}"
        )
        return 3

    manifest = store.read_manifest()
    if manifest is None:
        print(
            "Resume REFUSED: the durable run manifest is missing, so the "
            "paused workflow cannot be identified safely. Nothing was executed."
        )
        return 3
    managed_binding = None
    if manifest.delivery_authority_kind == "cloud_runner":
        from openadapt_flow.runner.dispatch_envelope import (
            ManagedDispatchEnvelopeError,
            read_managed_dispatch_envelope,
        )

        try:
            managed_binding = read_managed_dispatch_envelope(
                Path(getattr(args, "managed_dispatch_file", ""))
            )
        except (ManagedDispatchEnvelopeError, TypeError, ValueError):
            print(
                "Resume REFUSED: managed dispatch binding is invalid. Nothing was executed."
            )
            return 3
        if (
            managed_binding.run_id != manifest.remote_delivery_run_id
            or managed_binding.authorization != manifest.governed_authorization
            or managed_binding.binding_sha256
            != manifest.managed_dispatch_binding_sha256
        ):
            print(
                "Resume REFUSED: managed dispatch does not match this run. Nothing was executed."
            )
            return 3
    from openadapt_flow.ir import Workflow

    try:
        workflow = Workflow.load(Path(manifest.bundle_dir), key=ckpt_key)
    except Exception:  # crypto / integrity / structural errors -> fail closed
        # Do not echo the exception: a malformed local backend hint can contain
        # a PHI-bearing title and Pydantic/provider errors may repeat inputs.
        print(
            "Resume REFUSED: the paused workflow bundle could not be loaded "
            f"safely. {_SAFE_BUNDLE_LOAD_GUIDANCE} Nothing was executed."
        )
        return 3

    production_guard = None
    campaign_guard = None
    retained_authorization = manifest.governed_authorization
    if (
        retained_authorization is not None
        and retained_authorization.production_qualification_admission_id is not None
    ):
        from openadapt_flow.production_qualification import (
            ProductionQualificationAuthorityError,
            ProductionQualificationGuard,
        )

        authority_file = getattr(args, "qualification_authority_file", None)
        if not authority_file:
            print(
                "Resume REFUSED: Production actuation requires the private v2 "
                "qualification authority again. Nothing was executed."
            )
            return 3
        try:
            production_guard = ProductionQualificationGuard(
                authority_file,
                remote_permit_revalidation=(
                    manifest.delivery_authority_kind == "cloud_runner"
                ),
            )
            production_binding = production_guard.authorization_binding(workflow)
        except ProductionQualificationAuthorityError:
            print(
                "Resume REFUSED: the Production qualification authority is invalid, "
                "expired, revoked, or does not match this exact run. Nothing was "
                "executed."
            )
            return 3
        if any(
            getattr(retained_authorization, field, None) != value
            for field, value in production_binding.items()
        ):
            print(
                "Resume REFUSED: the Production qualification authority differs "
                "from the retained run. Nothing was executed."
            )
            return 3
        args._production_qualification_guard = production_guard

    if (
        retained_authorization is not None
        and retained_authorization.qualification_campaign_permit_id is not None
    ):
        from openadapt_flow.qualification_campaign_authority import (
            QualificationCampaignAuthorityError,
            QualificationCampaignGuard,
        )

        authority_file = getattr(args, "qualification_campaign_authority_file", None)
        if not authority_file:
            print(
                "Resume REFUSED: qualification actuation requires the private "
                "signed non-production campaign authority again. Nothing was "
                "executed."
            )
            return 3
        try:
            campaign_guard = QualificationCampaignGuard(
                authority_file,
                workflow=workflow,
                case_id=retained_authorization.qualification_case_id,
                input_digest=retained_authorization.runtime_inputs_digest,
            )
            campaign_refusal = campaign_guard.authorization_refusal(
                workflow, retained_authorization
            )
        except QualificationCampaignAuthorityError:
            campaign_refusal = "qualification campaign authority is invalid"
        if campaign_refusal is not None:
            print(
                "Resume REFUSED: the non-production qualification campaign "
                "permit is invalid, expired, consumed, or differs from the "
                "retained run. Nothing was executed."
            )
            return 3
        args._qualification_campaign_guard = campaign_guard

    # A GUI automation cannot be resumed without a LIVE backend/vision, so build
    # a fresh Replayer here (deployment wiring from --config) and hand it to the
    # durable resume entrypoint, which re-binds params from the run manifest.
    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime.durable.approval import ResumeRefused

    (
        cfg,
        effect_verifier,
        api_actuator,
        _durable,
        allow_egress,
    ) = _deployment_runtime(
        args,
        params=_replay_params(
            getattr(args, "param", None), getattr(args, "params_file", None)
        ),
    )
    if manifest.delivery_authority_kind == "cloud_runner" and allow_egress:
        print(
            "Resume REFUSED: this managed runner doesn't permit model grounding "
            "because its evidence contract requires "
            "screenshots_may_leave_box=false. Nothing was executed."
        )
        return 3

    # Route the resumed run through the SAME backend factory as replay/run
    # (--backend / --agent-url / --rdp-host over --config), so a resume drives
    # the bundle's real substrate rather than always the browser. The default
    # (web / no flag) reproduces the historical Playwright path below exactly.
    backend_cfg = _resolve_backend_config(args, cfg, workflow)
    if _refuse_missing_citrix_readiness(backend_cfg, operation="Resume"):
        return 2

    where = (
        f"state '{pending.step_id}'"
        if pending.program
        else f"step {pending.step_index} '{pending.step_id}' "
        f"(from index {resume_point(run_dir, key=ckpt_key)})"
    )
    print(
        f"Resuming {run_dir} at {where}: {pending.category}. "
        "Already-verified work is NOT re-run."
    )

    def _resume_with(backend: "Backend") -> "RunReport":
        replayer = _configured_replayer(
            backend,
            workflow=workflow,
            allow_egress=allow_egress,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=True,  # resume forces durability so it can pause again
            use_structural=True,
            pixel_verify_enabled=cfg.runtime.pixel_verify_enabled,
            governed_authorization=retained_authorization,
            managed_dispatch_binding=managed_binding,
            production_qualification_guard=production_guard,
            qualification_campaign_guard=campaign_guard,
            runtime_config=cfg.runtime,
            checkpoint_key=ckpt_key,
        )
        return resume(
            run_dir,
            replayer,
            key=ckpt_key,
            execution_target_kind=_report_backend_kind(backend_cfg.kind),
        )

    try:
        if _normalize_kind(backend_cfg.kind) == "web":
            from openadapt_flow._browser_setup import ensure_chromium_installed

            url = args.url or cfg.backend.url
            if url is None:
                raise SystemExit(
                    "resume needs the target app URL to rebuild a live backend — "
                    "pass --url or set backend.url in --config."
                )
            headed = args.headed or cfg.backend.headed
            ensure_chromium_installed()
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headed)
                page = browser.new_page(viewport=_VIEWPORT)
                page.goto(url)
                try:
                    report = _resume_with(build_backend(backend_cfg, page=page))
                finally:
                    browser.close()
        else:
            # Desktop/native/remote: no browser, no --url; the factory builds
            # the native backend from the resolved config (fail-loud on a missing
            # required field). RDP transports hold a live socket — close them.
            try:
                backend = build_backend(backend_cfg)
            except ValueError as e:
                raise SystemExit(str(e))
            try:
                report = _resume_with(backend)
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
    except ResumeRefused as refused:
        # P0-5: the library REFUSED the resume (no valid approval, an expired
        # pause, a changed bundle, or a diverged app state) — never a silent
        # proceed. Approve first:  openadapt-flow approve <run_dir>
        print(f"Resume REFUSED: {refused}")
        return 3

    report_md = render_run_report(run_dir)
    execution_outcome = getattr(report, "execution_outcome", None)
    outcome = execution_outcome or ("success" if report.success else "FAILED")
    print(f"Resume {outcome}: {report_md}")
    _maybe_report_break(run_dir, report)
    _maybe_report_run(
        run_dir,
        report,
        args,
        backend_kind=_report_backend_kind(backend_cfg.kind),
    )
    _maybe_attest_run(run_dir, report, args)
    if getattr(report, "execution_profile", None) in {"standard", "regulated"}:
        return 0 if outcome == "VERIFIED" else 1
    return 0 if report.success else 1


def _cmd_approve(args: argparse.Namespace) -> int:
    """Record an AUTHENTICATED approval for a durably-paused run (P0-5).

    Writes an :class:`ApprovalRecord` (approver identity / timestamp / chosen
    resolution / bundle-version hash) to ``run_dir/approval.json`` — the artifact
    the durable ``resume`` entrypoint now ENFORCES (a resume with no valid
    approval is refused). Also flips the pending escalation's ``status`` to
    ``approved`` for the audit trail / back-compat.
    """
    import getpass

    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable.approval import (
        ResumeRefused,
        enforce_resume_authorization,
        issue_resume_approval,
    )
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

    run_dir = Path(args.run_dir)
    store = CheckpointStore(run_dir, key=crypto.resolve_key(None))
    pending = store.read_pending()
    if pending is None:
        print(f"No pending escalation at {run_dir} — nothing to approve.")
        return 1
    manifest = store.read_manifest()
    if manifest is None or not manifest.run_id:
        print(
            "The exact durable run manifest is unavailable. Start a fresh run "
            "instead of approving an unbound pause."
        )
        return 1
    authorize_uncertain_retry = bool(getattr(args, "authorize_uncertain_retry", False))
    if pending.delivery_uncertainty is not None and not authorize_uncertain_retry:
        print(
            "This step may already have actuated. A normal resume cannot repeat "
            "it. Reconcile and independently verify the outcome through the "
            "attended completion path. If reconciliation proves a retry is "
            "necessary, rerun approve with --authorize-uncertain-retry."
        )
        return 1
    try:
        store.validate_namespace(manifest)
    except ResumeRefused as exc:
        print(f"Approval refused: {exc}")
        return 1
    if pending.status == "rejected":
        print("This durable pause was rejected and cannot be approved or resumed.")
        return 1

    # The approver identity defaults to the invoking OS user (a resume with a
    # blank approver is refused by the durable library); --approver overrides.
    approver = args.approver or getpass.getuser()
    try:
        bundle_ver = bundle_version(manifest.bundle_dir)
    except OSError:
        print(
            "The retained bundle is unavailable. Restore it before approving "
            "this pause."
        )
        return 1
    existing_approval = store.read_approval()
    if pending.status == "approved" and existing_approval is not None:
        try:
            enforce_resume_authorization(
                pending,
                existing_approval,
                bundle_version=bundle_ver,
                run_id=manifest.run_id,
                workflow_name=manifest.workflow_name,
                run_dir=run_dir,
            )
        except ResumeRefused:
            pass
        else:
            print(f"Pending escalation at {run_dir} is already approved.")
            return 0
    resolution = args.resolution or (
        pending.proposed_options[0] if pending.proposed_options else "approved"
    )
    approval = issue_resume_approval(
        pending,
        approver=approver,
        resolution=resolution,
        bundle_version=bundle_ver,
        workflow_name=pending.workflow_name,
        run_id=manifest.run_id,
        run_dir=run_dir,
        authorize_uncertain_retry=authorize_uncertain_retry,
    )
    # Bind the exact authority and pause-state transition in one filesystem
    # transaction. A concurrent resume cannot replace the retained approver or
    # leave approval.json describing authority that the live attempt did not use.
    store.commit_approval_transition(
        expected_pending=pending,
        approval=approval,
        target_status="approved",
    )
    print(
        f"Approved pending escalation at {run_dir} by {approver!r} "
        f"(step {pending.step_index} '{pending.step_id}': {pending.category}).\n"
        f"Resume it with:  openadapt-flow resume {run_dir}"
    )
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from contextlib import contextmanager

    from openadapt_flow.bench import run_bench
    from openadapt_flow.mockmed.server import serve
    from openadapt_flow.report import render_bench_report

    url, stop = serve(port=0)
    target_url = _with_drift(url, args.drift)

    @contextmanager
    def backend_factory():
        from openadapt_flow._browser_setup import ensure_chromium_installed
        from openadapt_flow.backends.playwright_backend import (
            PlaywrightBackend,
        )

        ensure_chromium_installed()
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport=_VIEWPORT)
            page.goto(target_url)
            try:
                yield PlaywrightBackend(page)
            finally:
                browser.close()

    run_root = Path(args.run_root)
    try:
        result = run_bench(
            Path(args.bundle),
            backend_factory,
            args.n,
            params=_parse_params(args.param),
            run_root=run_root,
        )
    finally:
        stop()

    report_md = render_bench_report(run_root / "bench.json", run_root / "BENCH.md")
    print(
        f"Bench: {result['success_count']}/{result['n']} succeeded "
        f"(p50 {result['total_ms_p50']:.0f} ms) — {report_md}"
    )
    return 0 if result["success_count"] == result["n"] else 1


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from openadapt_flow.benchmark.run_benchmark import run_benchmark

    results = run_benchmark(
        Path(args.out),
        n_compiled=args.n_compiled,
        n_agent=args.n_agent,
        note_text=args.note_text,
        headed=args.headed,
    )
    compiled = results["arms"]["compiled"]
    agent = results["arms"]["agent"]
    print(
        f"compiled: {compiled['success_count']}/{compiled['n']} "
        f"(p50 {compiled['wall_s_p50']:.1f}s, $0/run) | "
        f"agent: {agent['success_count']}/{agent['n']} "
        f"(p50 {agent['wall_s_p50']:.1f}s, "
        f"${agent['cost_usd_per_run']:.4f}/run)"
    )
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import SEVERITY_ORDER, lint_workflow

    workflow = Workflow.load(Path(args.bundle))
    report = lint_workflow(workflow)
    print(report.render())
    # Exit code by max severity: nonzero once anything reaches `error`
    # (an unarmed or vacuous IRREVERSIBLE step). `--strict` also fails on warn.
    threshold = "warn" if args.strict else "error"
    fail = SEVERITY_ORDER[report.max_severity] >= SEVERITY_ORDER[threshold]
    if report.findings and fail:
        _print_lint_epilogue(args.bundle, threshold)
        return 1
    return 0


def _print_lint_epilogue(bundle: str, threshold: str) -> None:
    """Three-line epilogue after a failing lint (presentation only)."""
    from openadapt_flow.tutorial import outcome_epilogue_lines

    lines = outcome_epilogue_lines(
        what=(
            f"lint found coverage gaps at or above the '{threshold}' severity "
            f"in {bundle}"
        ),
        why_safe=(
            "gaps are reported instead of silently running unguarded or "
            "unverifiable steps"
        ),
        next_command=f"openadapt-flow certify {bundle} --policy <policy>",
    )
    print("\n" + "\n".join(lines))


def _cmd_scaffold_verifier(args: argparse.Namespace) -> int:
    """Draft (never approve) an effect-oracle contract from retained evidence."""
    from openadapt_flow.scaffold_verifier import NEXT_COMMANDS_TEMPLATE, write_draft

    out, count = write_draft(
        Path(args.source), Path(args.out) if getattr(args, "out", None) else None
    )
    print(NEXT_COMMANDS_TEMPLATE.format(out=out, count=count))
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    """Print a plain-language, read-only summary of one completed run."""
    from openadapt_flow.scaffold_verifier import explain_run

    print(explain_run(Path(args.run_dir)))
    return 0


def _cmd_visualize(args: argparse.Namespace) -> int:
    from openadapt_flow.admitted_composition import is_process_contract_artifact
    from openadapt_flow.composition import CompositionError, is_composition_artifact
    from openadapt_flow.ir import Workflow
    from openadapt_flow.visualize import (
        PresentationProfile,
        build_composition_graph,
        build_program_graph,
        project_program_graph,
        render_html,
        render_mermaid,
    )

    path = Path(args.bundle)
    if is_process_contract_artifact(path):
        return _cmd_visualize_process(args)
    if is_composition_artifact(path):
        try:
            spec = build_composition_graph(path)
        except CompositionError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        spec = build_program_graph(Workflow.load(path))
    spec = project_program_graph(spec, PresentationProfile(args.profile))
    fmt = args.format
    if fmt == "json":
        output = spec.model_dump_json(indent=2)
    elif fmt == "mermaid":
        output = render_mermaid(spec)
    else:  # html
        output = render_html(spec)

    if args.out:
        out_path = Path(args.out)
        if out_path.parent != Path(""):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        b = spec.bundle
        if b.is_composition:
            handoffs = sum(1 for edge in spec.edges if edge.kind.value == "handoff")
            print(
                f"Wrote {fmt} visualization of {b.name} "
                f"({b.child_count} children, {handoffs} handoff(s)) to {out_path}"
            )
        else:
            print(
                f"Wrote {fmt} visualization of {b.name} "
                f"({b.action_count} steps, {b.identity_armed_count} identity gates, "
                f"{b.irreversible_count} irreversible, {b.halt_point_count} halt "
                f"point(s)) to {out_path}"
            )
    else:
        print(output)
    return 0


def _cmd_visualize_process(args: argparse.Namespace) -> int:
    from openadapt_flow.visualize.admitted_composition import (
        build_process_graph,
        dumps_process_graph,
        render_process_html,
        render_process_mermaid,
    )

    spec = build_process_graph(Path(args.bundle))
    fmt = args.format
    if fmt == "json":
        output = dumps_process_graph(spec)
    elif fmt == "mermaid":
        output = render_process_mermaid(spec)
    else:
        output = render_process_html(spec)
    if args.out:
        out_path = Path(args.out)
        if out_path.parent != Path(""):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        admitted = sum(1 for node in spec.nodes if node.kind == "admitted_capability")
        handoffs = sum(1 for edge in spec.edges if edge.kind == "handoff")
        print(
            f"Wrote {fmt} visualization of {spec.name} "
            f"({admitted} admitted capabilities, {handoffs} handoff(s)) to {out_path}"
        )
    else:
        print(output)
    return 0


def _cmd_certify(args: argparse.Namespace) -> int:
    from openadapt_flow.composition import is_composition_artifact
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy

    bundle = Path(args.bundle)
    if is_composition_artifact(bundle):
        from openadapt_flow.cli_compose import cmd_certify_composition

        return cmd_certify_composition(args)
    from openadapt_flow.admitted_composition import is_process_contract_artifact

    if is_process_contract_artifact(bundle):
        from openadapt_flow.cli_process import cmd_certify_process

        return cmd_certify_process(args)
    workflow = Workflow.load(bundle)
    # Policy source: explicit --policy, else the deployment config's policy
    # section (so one deployment.yaml certifies AND runs the bundle).
    policy_source = args.policy
    if policy_source is None and getattr(args, "config", None):
        from openadapt_flow.deployment import load_deployment

        policy_source = load_deployment(args.config).policy.policy
    if policy_source is None:
        raise SystemExit(
            "certify needs a policy: pass --policy <name-or-path> or set "
            "policy.policy in --config."
        )
    try:
        policy = load_policy(policy_source)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))
    report = evaluate_policy(workflow, policy)
    print(report.render())
    # A failing certification exits nonzero so CI / deploy gates refuse the
    # bundle — the whole point of making "runnable" distinct from "certified".
    return 0 if report.passed else 2


def _cmd_seal(args: argparse.Namespace) -> int:
    from openadapt_flow.bundle_sealing import BundleSealingError, seal_bundle

    try:
        sealed = seal_bundle(Path(args.source), Path(args.out))
    except BundleSealingError as exc:
        print(f"seal REFUSED: {exc}")
        return 2
    print(f"Sealed bundle: {sealed.path}")
    print(f"Content digest: sha256:{sealed.content_digest}")
    if sealed.certification_invalidated:
        print(
            "Prior certification invalidated: certify the sealed destination "
            "before deployment."
        )
    return 0


def _qualification_workflow(args: argparse.Namespace):
    from openadapt_flow.ir import Workflow

    return Workflow.load(Path(args.bundle))


def _qualification_policy(args: argparse.Namespace):
    from openadapt_flow.policy import load_policy

    try:
        return load_policy(args.policy)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _parse_qualification_signal(
    raw: str,
    *,
    identifier_region: tuple[int, int, int, int] | None,
    signal_regions: dict[str, tuple[int, int, int, int]] | None = None,
    signal_params: dict[str, list[str]] | None = None,
    signal_extracts: dict[str, str] | None = None,
    signal_expecteds: dict[str, str] | None = None,
):
    from openadapt_flow.qualification import (
        IdentityEvidenceSource,
        IdentityMatchMode,
        IdentityNormalizer,
        IdentitySignalPolicy,
    )

    if "=" not in raw:
        raise SystemExit("--signal expects KEY=SOURCE:MODE[:NORMALIZER,NORMALIZER]")
    key, spec = raw.split("=", 1)
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise SystemExit("--signal expects KEY=SOURCE:MODE[:NORMALIZER,NORMALIZER]")
    try:
        source = IdentityEvidenceSource(parts[0])
        match = IdentityMatchMode(parts[1])
        normalizers = (
            [IdentityNormalizer(value) for value in parts[2].split(",") if value]
            if len(parts) == 3
            else []
        )
        explicit_region = (signal_regions or {}).get(key)
        return IdentitySignalPolicy(
            key=key,
            source=source,
            match=match,
            normalizers=normalizers,
            region=(
                explicit_region
                if explicit_region is not None
                else identifier_region
                if source is IdentityEvidenceSource.IDENTIFIER_REGION
                else None
            ),
            params=(signal_params or {}).get(key, []),
            extract_pattern=(signal_extracts or {}).get(key),
            expected_value=(signal_expecteds or {}).get(key),
        )
    except ValueError as exc:
        raise SystemExit(f"invalid --signal {raw!r}: {exc}") from exc


def _qualification_report(args: argparse.Namespace):
    from openadapt_flow.qualification import evaluate_qualification

    workflow = _qualification_workflow(args)
    policy = _qualification_policy(args) if getattr(args, "policy", None) else None
    evidence_root = getattr(args, "evidence_root", None)
    return workflow, evaluate_qualification(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
    )


def _read_qualification_case_inputs(path: Path, *, workflow):
    """Read only the canonical local input artifact for one case attempt."""

    import os
    import stat

    from openadapt_flow.runtime.authorization import (
        parse_runtime_inputs_bytes,
        runtime_inputs_bytes,
    )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(
            "qualification case inputs could not be opened safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 1024 * 1024
            or (
                os.name != "nt"
                and (
                    not hasattr(os, "geteuid")
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                )
            )
        ):
            raise ValueError("qualification case inputs are not a private regular file")
        raw = os.read(descriptor, 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    if len(raw) > 1024 * 1024:
        raise ValueError("qualification case inputs are too large")
    params, worklists = parse_runtime_inputs_bytes(raw, workflow=workflow)
    if runtime_inputs_bytes(workflow, params, worklists) != raw:
        raise ValueError("qualification case inputs are not canonical")
    return params, worklists, raw


def _claim_qualification_case_attempt(args: argparse.Namespace) -> bool:
    """Persist one exact case attempt before it can reach an actuator."""

    import hashlib
    import json
    import os
    import stat

    def private_directory(path: Path, *, migrate_owned_root: bool = False) -> Path:
        """Open a private directory without trusting a pathname after inspection."""
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            initial = os.lstat(path)
        except OSError as exc:
            raise ValueError(
                "qualification attempt ledger could not be inspected"
            ) from exc
        if stat.S_ISLNK(initial.st_mode) or not stat.S_ISDIR(initial.st_mode):
            raise ValueError("qualification attempt ledger is not a private directory")
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError(
                "qualification attempt ledger is not a private directory"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != initial.st_dev
                or metadata.st_ino != initial.st_ino
            ):
                raise ValueError(
                    "qualification attempt ledger changed while it was inspected"
                )
            if os.name != "nt":
                if not hasattr(os, "geteuid") or metadata.st_uid != os.geteuid():
                    raise ValueError(
                        "qualification attempt ledger is not owned by this user"
                    )
                if migrate_owned_root and stat.S_IMODE(metadata.st_mode) != 0o700:
                    os.fchmod(descriptor, 0o700)
                    metadata = os.fstat(descriptor)
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise ValueError(
                        "qualification attempt ledger is not a private directory"
                    )
        finally:
            os.close(descriptor)
        try:
            final = os.lstat(path)
        except OSError as exc:
            raise ValueError(
                "qualification attempt ledger could not be inspected"
            ) from exc
        if (
            stat.S_ISLNK(final.st_mode)
            or not stat.S_ISDIR(final.st_mode)
            or final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
        ):
            raise ValueError(
                "qualification attempt ledger changed while it was inspected"
            )
        return path

    case = args._qualification_case_execution["case"]
    workflow = args._qualification_case_execution["workflow"]
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir()
    args.run_dir = str(run_dir)
    project = workflow.qualification
    manifest = workflow.manifest
    assert project is not None and manifest is not None
    identity = {
        "schema": "openadapt.qualification-attempt/v1",
        "bundle_content_digest": manifest.content_digest,
        "project_id": project.project_id,
        "project_revision": project.revision,
        "project_contract_sha256": project.contract_sha256(),
        "campaign_id_sha256": args._qualification_case_execution["campaign_id_sha256"],
        "case_id": case.id,
        "case_input_sha256": args._qualification_case_execution["input_sha256"],
        "run_id_sha256": args._qualification_case_execution["run_id_sha256"],
    }
    payload = {
        **identity,
        "run_dir_sha256": hashlib.sha256(
            str(run_dir.resolve()).encode("utf-8")
        ).hexdigest(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    attempt_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    state_root = Path(os.environ.get("OPENADAPT_HOME", Path.home() / ".openadapt"))
    try:
        ledger_dir = private_directory(
            private_directory(state_root, migrate_owned_root=True)
            / "qualification-attempts"
        )
    except ValueError:
        print(
            "run REFUSED: qualification attempt ledger is not available safely. "
            "Nothing was executed."
        )
        return False
    marker = ledger_dir / f"{attempt_key}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError:
        print(
            "run REFUSED: this qualification case attempt already started in this "
            "local qualification ledger; "
            "reconcile its retained run evidence before another attempt. "
            "Nothing was executed."
        )
        return False
    except OSError:
        print(
            "run REFUSED: qualification case attempt could not be claimed safely. "
            "Nothing was executed."
        )
        return False
    try:
        os.write(descriptor, canonical)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory = os.open(ledger_dir, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        print(
            "run REFUSED: qualification attempt ledger could not be persisted safely. "
            "Nothing was executed."
        )
        return False
    return True


def _cmd_qualify_run_case(args: argparse.Namespace) -> int:
    """Execute one sealed, current qualification case with Flow-owned authority."""

    from openadapt_flow.qualification import (
        qualification_campaign_id_sha256,
        qualification_run_id_sha256,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    try:
        workflow = _qualification_workflow(args)
        if workflow.manifest is None or workflow.qualification is None:
            raise ValueError(
                "qualification case execution requires a sealed project bundle"
            )
        case = next(
            (item for item in workflow.qualification.cases if item.id == args.case_id),
            None,
        )
        if case is None:
            raise ValueError("qualification case is not declared by this project")
        if case.kind.value != "representative":
            raise ValueError(
                "fault qualification cases require the configured fault-driver campaign"
            )
        if not case.action_targets or case.runtime_input_sha256 is None:
            raise ValueError(
                "qualification case has no exact approved action/input scope"
            )
        params, worklists, _raw_inputs = _read_qualification_case_inputs(
            Path(args.inputs), workflow=workflow
        )
        input_sha256 = runtime_inputs_digest(workflow, params, worklists)
        if input_sha256 != case.runtime_input_sha256:
            raise ValueError("qualification case inputs do not match the approved case")
        campaign_id_sha256 = qualification_campaign_id_sha256(args.campaign_id)
        run_id_sha256 = qualification_run_id_sha256(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"qualification run-case REFUSED: {exc}. Nothing was executed.")
        return 2

    args._qualification_case_execution = {
        "workflow": workflow,
        "case": case,
        "params": params,
        "worklists": worklists,
        "input_sha256": input_sha256,
        "campaign_id": args.campaign_id,
        "campaign_id_sha256": campaign_id_sha256,
        "run_id": args.run_id,
        "run_id_sha256": run_id_sha256,
    }
    args.profile = "standard"
    args.policy = None
    args.param = None
    args.params_file = None
    args.worklist = None
    args.managed_dispatch_file = None
    args.allow_unencrypted = False
    args.approve_unverified_writes = False
    args.strict_templates = False
    args.pin_digest = workflow.manifest.content_digest
    args.pin_version = None
    args.drift = None
    args.dry_run = bool(getattr(args, "dry_run", False))
    if args.dry_run:
        return _cmd_run(args)
    return _cmd_run(args)


def _cmd_qualify(args: argparse.Namespace) -> int:
    import json

    from pydantic import TypeAdapter

    from openadapt_flow.ir import BusinessDecisionSpec, StateKind
    from openadapt_flow.judgment_cases import JudgmentCaseSetV1
    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        IdentityEnforcement,
        IdentityPolicy,
        QualificationCase,
        QualificationCaseKind,
        QualificationCaseResult,
        QualificationOutcome,
        QualifiedEntityLabel,
        RequalificationCondition,
        VerificationTier,
        add_case,
        add_requalification_condition,
        certify_project,
        entity_label_options,
        evaluate_judgment_case_qualification,
        init_project,
        list_entity_labels,
        project_schema,
        record_case_results,
        remove_entity_label,
        save_qualified_workflow,
        set_action_classification,
        set_business_decision,
        set_effect_policy,
        set_entity_label,
        set_identity_policy,
        set_judgment_cases,
        set_trusted_runner_key,
    )

    verb = args.qualify_cmd
    if verb == "run-case":
        return _cmd_qualify_run_case(args)
    if verb == "schema":
        print(json.dumps(project_schema(), indent=2, sort_keys=True))
        return 0
    if verb in {"propose", "accept", "admit-local", "from-demo"}:
        return _cmd_qualify_proposal(args)

    workflow = _qualification_workflow(args)

    if verb == "business-decision":
        if not args.check:
            if not args.input:
                raise SystemExit("--input is required unless --check is used")
            try:
                payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("authoring input must be an object")
                allowed = {
                    "schema_version",
                    "graph_id",
                    "state_id",
                    "insert_before_state_id",
                    "decision",
                }
                required = {"schema_version", "graph_id", "state_id", "decision"}
                if set(payload) - allowed or required - set(payload):
                    raise ValueError("authoring input has an unknown or missing field")
                if (
                    payload["schema_version"]
                    != "openadapt.business-decision-authoring/v1"
                ):
                    raise ValueError("authoring schema is not supported")
                graph_id = payload["graph_id"]
                state_id = payload["state_id"]
                insert_before = payload.get("insert_before_state_id")
                if not isinstance(graph_id, str) or not isinstance(state_id, str):
                    raise ValueError("graph_id and state_id must be strings")
                if insert_before is not None and not isinstance(insert_before, str):
                    raise ValueError("insert_before_state_id must be a string or null")
                decision = BusinessDecisionSpec.model_validate(payload["decision"])
            except (OSError, ValueError) as exc:
                raise SystemExit(
                    f"invalid business decision authoring input: {exc}"
                ) from exc
            workflow = set_business_decision(
                workflow,
                graph_id=graph_id,
                state_id=state_id,
                decision=decision,
                insert_before_state_id=insert_before,
            )
            save_qualified_workflow(workflow, args.bundle)

        decisions = []
        graphs = [("__program__", workflow.program), *workflow.subflows.items()]
        for graph_id, graph in graphs:
            if graph is None:
                continue
            for state_id, state in sorted(graph.states.items()):
                if (
                    state.kind is not StateKind.BUSINESS_DECISION
                    or state.decision is None
                ):
                    continue
                decisions.append(
                    {
                        "graph_id": graph_id,
                        "state_id": state_id,
                        "contract_digest": f"sha256:{state.decision.contract_sha256()}",
                        "output_param": state.decision.output_param,
                        "option_ids": [option.id for option in state.decision.options],
                        "successor_state_ids": [
                            option.target for option in state.decision.options
                        ],
                    }
                )
        print(
            json.dumps(
                {
                    "schema_version": "openadapt.business-decision-authoring-report/v1",
                    "decisions": decisions,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if verb == "judgment-cases":
        if args.check:
            report = evaluate_judgment_case_qualification(workflow)
        else:
            if not args.input:
                raise SystemExit("--input is required unless --check is used")
            try:
                payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
                case_set = JudgmentCaseSetV1.model_validate(payload)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"invalid judgment case set: {exc}") from exc
            set_judgment_cases(workflow, schemas=case_set.schemas, cases=case_set.cases)
            save_qualified_workflow(workflow, args.bundle)
            report = evaluate_judgment_case_qualification(workflow)
        print(report.model_dump_json(indent=2))
        return 0 if report.passed else 2

    if verb == "label":
        if args.label_cmd == "list":
            print(
                json.dumps(
                    [
                        label.model_dump(mode="json")
                        for label in list_entity_labels(workflow)
                    ],
                    indent=2,
                )
            )
            return 0
        changed = False
        if args.label_cmd == "set":
            reviewed_fallback = next(
                (
                    option["fallback"]
                    for option in entity_label_options()
                    if option["label"] == args.label
                ),
                None,
            )
            if (
                reviewed_fallback is not None
                and args.fallback is not None
                and args.fallback != reviewed_fallback
            ):
                raise SystemExit(
                    f"{args.label!r} uses the reviewed fallback {reviewed_fallback!r}"
                )
            fallback = reviewed_fallback or args.fallback or "record"
            try:
                label = QualifiedEntityLabel(
                    step_id=args.step, label=args.label, fallback=fallback
                )
            except ValueError as exc:
                raise SystemExit(f"invalid entity label: {exc}") from exc
            assert workflow.qualification is not None
            changed = workflow.qualification.entity_labels.get(args.step) != label
            set_entity_label(workflow, label)
        elif args.label_cmd == "remove":
            assert workflow.qualification is not None
            changed = args.step in workflow.qualification.entity_labels
            remove_entity_label(workflow, args.step)
        else:  # pragma: no cover - argparse requires a known command.
            raise SystemExit(f"unknown qualification label command {args.label_cmd!r}")
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        if changed:
            print(
                "Certification invalidated. Run `openadapt-flow qualify certify "
                "<bundle> --evidence-root <path>` before production V2 tasks.",
                file=sys.stderr,
            )
        return 0

    if verb == "init":
        environment = EnvironmentBoundary(
            target_kind=args.target,
            application=args.application,
            application_version=args.application_version,
            environment_digest=args.environment_digest,
            runtime_version=args.runtime_version or _package_version(),
            required_capabilities=args.require_capability,
        )
        init_project(
            workflow,
            environment=environment,
            minimum_effect_tier=VerificationTier(args.minimum_tier),
            replace=args.replace,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb in {"inspect", "explain", "report"}:
        policy = _qualification_policy(args) if getattr(args, "policy", None) else None
        from openadapt_flow.qualification import evaluate_qualification

        report = evaluate_qualification(
            workflow,
            policy=policy,
            evidence_root=args.evidence_root,
        )
        if args.json:
            payload = {
                "project": (
                    workflow.qualification.model_dump(mode="json")
                    if workflow.qualification
                    else None
                ),
                "report": report.model_dump(mode="json"),
                "entity_label_options": entity_label_options(),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(report.render())
        return 0 if report.passed else 2

    if verb == "set-identity":
        from openadapt_flow.traversal import iter_workflow_steps

        steps = {step.id: step for step in iter_workflow_steps(workflow)}
        step = steps.get(args.step)
        if step is None:
            raise SystemExit(f"unknown step id {args.step!r}")
        region = step.anchor.identifier_region if step.anchor is not None else None
        if args.canonical_ladder:
            if args.signal or args.quorum is not None:
                raise SystemExit(
                    "--canonical-ladder cannot be combined with --signal/--quorum"
                )
            identity_policy = IdentityPolicy(
                step_id=args.step,
                enforcement=IdentityEnforcement.CANONICAL_LADDER,
            )
        else:
            if not args.signal or args.quorum is None:
                raise SystemExit(
                    "signal-quorum identity requires --signal and --quorum"
                )
            signal_regions: dict[str, tuple[int, int, int, int]] = {}
            for raw_region in args.signal_region:
                try:
                    key, encoded = raw_region.split("=", 1)
                    values = [int(item) for item in encoded.split(",")]
                    if len(values) != 4:
                        raise ValueError
                    if key in signal_regions:
                        raise SystemExit(f"--signal-region repeats signal key {key!r}")
                    signal_regions[key] = (
                        values[0],
                        values[1],
                        values[2],
                        values[3],
                    )
                except ValueError as exc:
                    raise SystemExit(
                        "--signal-region expects KEY=x,y,width,height"
                    ) from exc
            signal_params: dict[str, list[str]] = {}
            for raw_param in args.signal_param:
                try:
                    key, name = raw_param.split("=", 1)
                except ValueError as exc:
                    raise SystemExit("--signal-param expects KEY=PARAM") from exc
                signal_params.setdefault(key, []).append(name)
            signal_extracts: dict[str, str] = {}
            for raw_extract in args.signal_extract:
                key, separator, pattern = raw_extract.partition("=")
                if not separator or not key or not pattern:
                    raise SystemExit("--signal-extract expects KEY=REGEX")
                if key in signal_extracts:
                    raise SystemExit(f"--signal-extract repeats signal key {key!r}")
                signal_extracts[key] = pattern
            signal_expecteds: dict[str, str] = {}
            for raw_expected in args.signal_expected:
                key, separator, expected = raw_expected.partition("=")
                if not separator or not key or not expected:
                    raise SystemExit("--signal-expected expects KEY=VALUE")
                if key in signal_expecteds:
                    raise SystemExit(f"--signal-expected repeats signal key {key!r}")
                signal_expecteds[key] = expected
            signal_keys = {raw.split("=", 1)[0] for raw in args.signal if "=" in raw}
            unknown_options = sorted(
                (
                    set(signal_regions)
                    | set(signal_params)
                    | set(signal_extracts)
                    | set(signal_expecteds)
                ).difference(signal_keys)
            )
            if unknown_options:
                raise SystemExit(
                    "--signal-region/--signal-param/--signal-extract/"
                    "--signal-expected references unknown signal "
                    "key(s): " + ", ".join(unknown_options)
                )
            signals = [
                _parse_qualification_signal(
                    raw,
                    identifier_region=region,
                    signal_regions=signal_regions,
                    signal_params=signal_params,
                    signal_extracts=signal_extracts,
                    signal_expecteds=signal_expecteds,
                )
                for raw in args.signal
            ]
            identity_policy = IdentityPolicy(
                step_id=args.step,
                enforcement=IdentityEnforcement.SIGNAL_QUORUM,
                signals=signals,
                quorum=args.quorum,
            )
        set_identity_policy(
            workflow,
            identity_policy,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "set-risk":
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=args.step,
                classification=ActionRiskClass(args.classification),
                explanation=args.explanation,
                operator_confirmed=True,
            ),
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "trust-runner":
        set_trusted_runner_key(
            workflow,
            key_id=args.key_id,
            public_key_base64=args.public_key,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "set-effect":
        set_effect_policy(
            workflow,
            step_id=args.step,
            effect_index=args.effect_index,
            tier=VerificationTier(args.tier),
            actuation_path=args.path,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "add-case":
        add_case(
            workflow,
            QualificationCase(
                id=args.case_id,
                kind=QualificationCaseKind(args.kind),
                description=args.description,
                input_ref=args.input_ref,
                expected_outcome=QualificationOutcome(args.expected_outcome),
                required=not args.optional,
            ),
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "add-requalification":
        add_requalification_condition(
            workflow,
            RequalificationCondition(
                kind=args.kind,
                description=args.description,
            ),
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "run":
        try:
            raw = json.loads(Path(args.results).read_text(encoding="utf-8"))
            adapter = TypeAdapter(list[QualificationCaseResult])
            results = adapter.validate_python(raw if isinstance(raw, list) else [raw])
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid qualification results: {exc}") from exc
        record_case_results(
            workflow,
            results,
            evidence_root=args.evidence_root,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(workflow.qualification.model_dump_json(indent=2))
        return 0

    if verb == "certify":
        policy = _qualification_policy(args)
        report = certify_project(
            workflow,
            policy=policy,
            evidence_root=args.evidence_root,
            execution_profile=args.profile,
        )
        save_qualified_workflow(workflow, args.bundle)
        print(report.model_dump_json(indent=2) if args.json else report.render())
        return 0 if report.passed else 2

    raise SystemExit(f"unknown qualify command {verb!r}")


def _cmd_qualify_proposal(args: argparse.Namespace) -> int:
    """Propose, accept, or locally admit pins mined from the compiled demo."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification_proposal import (
        QualificationProposalError,
        accept_proposal,
        admit_local_dev,
        emit_proposal_json,
        load_proposal,
        propose_qualification,
        proposer_from_name,
        refuse_pin,
        save_accepted_bundle,
    )

    verb = args.qualify_cmd
    bundle = Path(args.bundle)
    try:
        workflow = Workflow.load(bundle)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"could not load bundle: {exc}") from exc

    if verb == "propose":
        try:
            proposer = proposer_from_name(getattr(args, "proposer", "off"))
        except QualificationProposalError as exc:
            raise SystemExit(str(exc)) from exc
        proposal = propose_qualification(
            workflow,
            recording_dir=getattr(args, "recording", None),
            policy_pack=getattr(args, "policy_pack", "community"),
            proposer=proposer,
        )
        print(emit_proposal_json(proposal, getattr(args, "out", None)))
        if proposal.status == "halted":
            print(
                proposal.halt_reason or "qualification proposal HALTed",
                file=sys.stderr,
            )
            return 2
        return 0

    if verb == "from-demo":
        try:
            proposer = proposer_from_name(getattr(args, "proposer", "off"))
        except QualificationProposalError as exc:
            raise SystemExit(str(exc)) from exc
        proposal = propose_qualification(
            workflow,
            recording_dir=args.recording,
            policy_pack=args.policy_pack,
            proposer=proposer,
        )
        if proposal.status == "halted":
            print(emit_proposal_json(proposal, getattr(args, "out", None)))
            print(
                proposal.halt_reason or "qualification proposal HALTed",
                file=sys.stderr,
            )
            return 2
        try:
            accepted = accept_proposal(workflow, proposal)
            local = None
            if args.admit_local:
                local = admit_local_dev(workflow, accepted, bundle_dir=bundle)
            save_accepted_bundle(
                workflow,
                bundle,
                proposal=accepted,
                local_admission=local,
            )
        except QualificationProposalError as exc:
            raise SystemExit(str(exc)) from exc
        print(emit_proposal_json(accepted, getattr(args, "out", None)))
        return 0

    if verb == "accept":
        try:
            proposal = load_proposal(args.proposal)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid qualification proposal: {exc}") from exc
        refused = getattr(args, "refuse_pin", None)
        if refused:
            halted = refuse_pin(proposal, refused)
            print(emit_proposal_json(halted, getattr(args, "out", None)))
            print(halted.halt_reason, file=sys.stderr)
            return 2
        try:
            accepted = accept_proposal(workflow, proposal, replace=args.replace)
            local = None
            if args.admit_local:
                local = admit_local_dev(workflow, accepted, bundle_dir=bundle)
            save_accepted_bundle(
                workflow,
                bundle,
                proposal=accepted,
                local_admission=local,
            )
        except QualificationProposalError as exc:
            raise SystemExit(str(exc)) from exc
        print(emit_proposal_json(accepted, getattr(args, "out", None)))
        return 0

    if verb == "admit-local":
        try:
            proposal = load_proposal(args.proposal)
            local = admit_local_dev(workflow, proposal, bundle_dir=bundle)
        except QualificationProposalError as exc:
            raise SystemExit(str(exc)) from exc
        out = (
            Path(args.out)
            if args.out
            else bundle / "qualification-admission.local-dev.json"
        )
        out.write_text(local.model_dump_json(indent=2), encoding="utf-8")
        print(local.model_dump_json(indent=2))
        return 0

    raise SystemExit(f"unknown qualify command {verb!r}")


def _cmd_propose_qualification(args: argparse.Namespace) -> int:
    """Top-level alias for ``qualify propose``."""

    args.qualify_cmd = "propose"
    return _cmd_qualify_proposal(args)


def _cmd_disambiguate(args: argparse.Namespace) -> int:
    import json

    from openadapt_flow.compiler.disambiguation import (
        apply_answers,
        detect_ambiguities,
    )
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    workflow = Workflow.load(bundle)
    questions = detect_ambiguities(workflow)

    if not questions:
        print("No ambiguities detected; the demo is fully specified.")
        return 0

    answers: dict[str, str] = {}
    if args.answers:
        answers = json.loads(Path(args.answers).read_text())
    elif args.interactive:
        # Thin interactive wrapper -- prompts a human, then calls the same API
        # the tests drive directly. The core stays non-interactive.
        for q in questions:
            print(f"\n{q.prompt}")
            for opt in q.options:
                print(f"  ({opt.key}) {opt.label}")
            tag = "" if q.consequential else f" [default: {q.default_key}]"
            reply = input(f"Answer for {q.id}{tag}: ").strip()
            if reply:
                answers[q.id] = reply
    else:
        # Non-interactive listing: surface the questions and exit nonzero if
        # any is a consequential (must-answer) ambiguity.
        for q in questions:
            flag = " (CONSEQUENTIAL)" if q.consequential else ""
            print(f"\n[{q.kind.value}] {q.id}{flag}\n  {q.prompt}")
            for opt in q.options:
                print(f"  ({opt.key}) {opt.label}")
        consequential = any(q.consequential for q in questions)
        print(
            f"\n{len(questions)} question(s) detected. Re-run with "
            "--interactive or --answers to resolve."
        )
        return 2 if consequential else 0

    result = apply_answers(workflow, answers)
    print(result.render())
    if args.write:
        result.workflow.save(bundle)
        print(f"Resolved workflow written to {bundle}")
    return 0 if result.certified else 2


def _cmd_emit_skill(args: argparse.Namespace) -> int:
    from openadapt_flow.emit.skill import emit_skill

    skill_dir = emit_skill(Path(args.bundle), Path(args.out))
    print(f"Skill written to {skill_dir}")
    return 0


def _cmd_emit_mcp(args: argparse.Namespace) -> int:
    from openadapt_flow.emit.mcp_tool import emit_mcp_server

    server_path = emit_mcp_server(Path(args.bundle), Path(args.out))
    print(f"MCP server written to {server_path}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    """Validate an ingest token against the hosted control plane and store the
    host in config and the token in the OS keychain when saving is enabled.

    Token resolution: ``--token`` -> ``OPENADAPT_INGEST_TOKEN`` env ->
    OS keychain -> existing config migration token. Mint a
    token in the dashboard at ``<host>/dashboard/settings/ingest``.
    """
    from openadapt_flow.hosted import HostedError, login

    try:
        result = login(
            token=args.token,
            host=args.host,
            save=not args.no_save,
            allow_plaintext_token=args.allow_plaintext_token,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"login failed: {e}")
        return 1
    print(f"Logged in to {result['host']} (token validated).")
    if result.get("config_path"):
        if result.get("token_storage") == "keyring":
            print(
                f"Token saved to {result['config_path']}; non-secret host saved in config."
            )
        else:
            print(
                f"Host + token saved to {result['config_path']} (mode 0600).\n"
                "WARNING: plaintext storage was explicitly enabled. Prefer the "
                "OS keychain or OPENADAPT_INGEST_TOKEN."
            )
    print(f"Manage tokens at {result['settings_url']}")
    return 0


def _cmd_connect(args: argparse.Namespace) -> int:
    """Claim one browser-created pairing and store it in the OS keychain."""
    from openadapt_flow.hosted import (
        HostedError,
        connect,
        parse_connect_uri,
    )

    pairing = args.pairing
    host = args.host
    destination_kind = args.destination_kind
    try:
        if args.uri:
            if host or destination_kind or args.trusted_host:
                raise HostedError(
                    "--uri cannot be combined with --host, --destination-kind, "
                    "or --trusted-host"
                )
            request = parse_connect_uri(args.uri)
            pairing = request["pairing"]
            host = request["host"]
            destination_kind = request.get("destination_kind")
        result = connect(
            pairing,
            host=host,
            device_name=args.device_name,
            destination_kind=destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"connect failed: {e}")
        return 1
    print(
        f"Connected local OpenAdapt to {result['host']} as "
        f"{result['device_name']} (credential saved in the OS keychain)."
    )
    print(f"Manage or revoke this connection at {result['settings_url']}")
    return 0


_PUSH_JSON_SCHEMA = "openadapt.push-result/v1"
_PUSH_RUNTIME_ATTESTATION_SCHEMAS = frozenset(
    {
        "openadapt.runtime-validation/v1",
        "openadapt.runtime-validation/v2",
        "openadapt.runtime-validation/v3",
    }
)


def _push_json_base(status: str) -> dict[str, Any]:
    """Return the complete V1 shape; phase-specific fields stay explicit nulls."""
    return {
        "schema": _PUSH_JSON_SCHEMA,
        "status": status,
        "workflow_id": None,
        "artifact_ingest_id": None,
        "review": None,
        "attestation": None,
        "binding": {
            "kind": None,
            "source_tree_sha256": None,
            "derivative_tree_sha256": None,
            "approved_archive_sha256": None,
            "artifact_sha256": None,
            "bundle_sha256": None,
            "source_recording_sha256": None,
            "sanitization_policy": None,
            "certification_policy": None,
            "certification_evidence_sha256": None,
            "governed_authorization_template_sha256": None,
            "parameter_schema_sha256": None,
            "attested_run_report_sha256": None,
            "resolves_run_id": None,
            "organization_id": None,
            "bundle_version_id": None,
            "bundle_version": None,
            "runtime_validation_id": None,
        },
        "next_action": None,
        "dashboard_url": None,
        "delivery": {"attempted": False, "certainty": "not_attempted"},
        "error": None,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return str(parsed) == value and parsed.version in {1, 2, 3, 4, 5, 6, 7, 8}
    except ValueError:
        return False


def _is_runtime_attestation_schema(value: Any) -> bool:
    return isinstance(value, str) and value in _PUSH_RUNTIME_ATTESTATION_SCHEMAS


def _required_sha256(value: Any, *, field: str) -> str:
    if not _is_sha256(value):
        raise ValueError(f"invalid {field}")
    return value


def _push_json_failure(
    *, uncertain: bool = False, context: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    status = "delivery_uncertain" if uncertain else "failed"
    document = _push_json_base(status)
    if uncertain:
        document["next_action"] = "reconcile"
        document["delivery"] = {"attempted": True, "certainty": "unknown"}
        code = "delivery_uncertain"
        message = (
            "The upload did not return a trustworthy delivery result. Reconcile "
            "the artifact in the hosted control plane before any retry."
        )
    else:
        document["delivery"] = {
            "attempted": None,
            "certainty": "not_accepted",
        }
        code = "push_failed"
        message = "The artifact was not accepted for ingest."
    document["error"] = {"code": code, "message": message[:500]}
    if uncertain and context:
        local = context.get("local_binding")
        sanitization = context.get("sanitization")
        artifact = (
            sanitization.get("artifact") if isinstance(sanitization, dict) else None
        )
        if isinstance(local, dict) and isinstance(artifact, dict):
            binding = document["binding"]
            values = {
                "kind": artifact.get("kind"),
                "source_tree_sha256": local.get("source_tree_sha256"),
                "derivative_tree_sha256": local.get("derivative_tree_sha256"),
                "approved_archive_sha256": local.get("approved_archive_sha256"),
                "artifact_sha256": artifact.get("sha256"),
                "sanitization_policy": local.get("sanitization_policy"),
                "resolves_run_id": context.get("resolves_run_id"),
            }
            if (
                values["kind"] in ("recording", "bundle")
                and all(
                    _is_sha256(values[key])
                    for key in (
                        "source_tree_sha256",
                        "derivative_tree_sha256",
                        "approved_archive_sha256",
                        "artifact_sha256",
                    )
                )
                and values["approved_archive_sha256"] == values["artifact_sha256"]
                and isinstance(values["sanitization_policy"], str)
                and (
                    values["resolves_run_id"] is None
                    or _is_uuid(values["resolves_run_id"])
                )
            ):
                binding.update(values)
                review_id = context.get("review_id")
                if _is_sha256(review_id):
                    document["review"] = {
                        "id": review_id,
                        "scope": "local_non_authoritative",
                        "sanitized_path": None,
                        "action": None,
                        "original_path": None,
                    }
                attestation = context.get("attestation_binding")
                if isinstance(attestation, dict):
                    challenge_id = attestation.get("challenge_id")
                    attestation_schema = attestation.get("schema")
                    bundle_binding = {
                        "bundle_sha256": attestation.get("bundle_sha256"),
                        "source_recording_sha256": attestation.get(
                            "source_recording_sha256"
                        ),
                        "certification_policy": attestation.get("policy"),
                        "certification_evidence_sha256": attestation.get(
                            "policy_evidence_sha256"
                        ),
                        "governed_authorization_template_sha256": attestation.get(
                            "governed_authorization_template_sha256"
                        ),
                        "parameter_schema_sha256": attestation.get(
                            "parameter_schema_sha256"
                        ),
                        "attested_run_report_sha256": attestation.get(
                            "run_report_sha256"
                        ),
                    }
                    template_sha = bundle_binding[
                        "governed_authorization_template_sha256"
                    ]
                    has_complete_binding = (
                        isinstance(challenge_id, str)
                        and 1 <= len(challenge_id) <= 200
                        and _is_runtime_attestation_schema(attestation_schema)
                        and bundle_binding["bundle_sha256"] == values["artifact_sha256"]
                        and all(
                            _is_sha256(bundle_binding[key])
                            for key in (
                                "bundle_sha256",
                                "source_recording_sha256",
                                "certification_evidence_sha256",
                                "parameter_schema_sha256",
                                "attested_run_report_sha256",
                            )
                        )
                        and isinstance(bundle_binding["certification_policy"], str)
                        and bool(bundle_binding["certification_policy"])
                        and (
                            (
                                attestation_schema == "openadapt.runtime-validation/v3"
                                and _is_sha256(template_sha)
                            )
                            or (
                                attestation_schema
                                in {
                                    "openadapt.runtime-validation/v1",
                                    "openadapt.runtime-validation/v2",
                                }
                                and template_sha is None
                            )
                        )
                    )
                    if has_complete_binding:
                        document["attestation"] = {
                            "id": challenge_id,
                            "schema": attestation_schema,
                        }
                        binding.update(bundle_binding)
    return document


def _push_json_invalid_response(context: dict[str, Any]) -> dict[str, Any]:
    document = _push_json_failure(uncertain=True, context=context)
    document["error"]["message"] = (
        "The server response did not prove an exact accepted ingest. "
        "Reconcile the artifact in the hosted control plane before any retry."
    )
    return document


def _push_json_result(result: dict[str, Any]) -> dict[str, Any]:
    """Build a stable Desktop contract from verified local and server evidence."""
    local_binding = result.get("local_binding")
    if not isinstance(local_binding, dict):
        raise ValueError("missing local binding")
    source_sha = _required_sha256(
        local_binding.get("source_tree_sha256"), field="source tree binding"
    )
    derivative_sha = _required_sha256(
        local_binding.get("derivative_tree_sha256"),
        field="derivative tree binding",
    )
    sanitization_policy = local_binding.get("sanitization_policy")
    if not isinstance(sanitization_policy, str) or not sanitization_policy:
        raise ValueError("missing sanitization policy")
    review_id = _required_sha256(result.get("review_id"), field="review id")

    if result.get("pending_review") is True and result.get("uploaded") is False:
        kind = result.get("kind") or "recording"
        if kind not in ("recording", "bundle"):
            raise ValueError("invalid review kind")
        sanitized_path = result.get("sanitized_path")
        review_action = result.get("review_action")
        original_path = result.get("original_path")
        if not isinstance(sanitized_path, str) or not sanitized_path:
            raise ValueError("missing sanitized review path")
        if review_action != "review_sanitized":
            raise ValueError("missing review action")
        if not isinstance(original_path, str) or not original_path:
            raise ValueError("missing original review path")
        document = _push_json_base("paused_for_review")
        document["review"] = {
            "id": review_id,
            "scope": "local_non_authoritative",
            "sanitized_path": sanitized_path,
            "action": review_action,
            "original_path": original_path,
        }
        document["binding"].update(
            {
                "kind": kind,
                "source_tree_sha256": source_sha,
                "derivative_tree_sha256": derivative_sha,
                "sanitization_policy": sanitization_policy,
            }
        )
        document["next_action"] = "review_local"
        return document

    if result.get("uploaded") is not True:
        raise ValueError("missing upload acknowledgment")
    sanitization = result.get("sanitization")
    artifact = sanitization.get("artifact") if isinstance(sanitization, dict) else None
    if not isinstance(artifact, dict):
        raise ValueError("missing sanitization binding")
    kind = artifact.get("kind")
    if kind not in ("recording", "bundle") or result.get("kind") != kind:
        raise ValueError("ingest kind mismatch")
    approved_sha = _required_sha256(
        local_binding.get("approved_archive_sha256"),
        field="approved archive binding",
    )
    artifact_sha = _required_sha256(
        artifact.get("sha256"), field="local artifact binding"
    )
    if approved_sha != artifact_sha or result.get("artifact_sha256") != artifact_sha:
        raise ValueError("server artifact binding mismatch")
    artifact_ingest_id = result.get("artifact_ingest_id")
    if not _is_uuid(artifact_ingest_id):
        raise ValueError("missing server artifact ingest id")

    document = _push_json_base("accepted_for_ingest")
    document["artifact_ingest_id"] = artifact_ingest_id
    document["review"] = {
        "id": review_id,
        "scope": "local_non_authoritative",
        "sanitized_path": None,
        "action": None,
        "original_path": None,
    }
    document["binding"].update(
        {
            "kind": kind,
            "source_tree_sha256": source_sha,
            "derivative_tree_sha256": derivative_sha,
            "approved_archive_sha256": approved_sha,
            "artifact_sha256": artifact_sha,
            "sanitization_policy": sanitization_policy,
        }
    )
    document["delivery"] = {"attempted": True, "certainty": "accepted"}

    if kind == "recording":
        if result.get("workflow_id") is not None:
            raise ValueError("recording ingest must not activate a workflow")
        server_status = result.get("status")
        if server_status == "needs_parameterization":
            document["next_action"] = "parameterize"
        elif server_status == "needs_runtime_validation":
            document["next_action"] = "validate_runtime"
        else:
            raise ValueError("recording ingest has no governed next action")
        return document

    workflow_id = result.get("workflow_id")
    if not _is_uuid(workflow_id):
        raise ValueError("bundle ingest has no workflow id")
    requested_workflow_id = result.get("requested_workflow_id")
    if requested_workflow_id is not None and not _is_uuid(requested_workflow_id):
        raise ValueError("invalid requested workflow binding")
    if requested_workflow_id is not None and workflow_id != requested_workflow_id:
        raise ValueError("accepted workflow does not match the requested workflow")
    if result.get("status") != "accepted":
        raise ValueError("bundle ingest is not accepted")
    version = result.get("version")
    if not isinstance(version, dict):
        raise ValueError("bundle ingest has no retained version binding")
    bundle_version_id = version.get("id")
    organization_id = version.get("org_id")
    version_workflow_id = version.get("workflow_id")
    version_artifact_sha = version.get("artifact_sha256")
    runtime_validation_id = version.get("runtime_validation_id")
    version_resolves_run_id = version.get("promoted_from_run_id")
    version_number = version.get("version")
    if not _is_uuid(bundle_version_id):
        raise ValueError("invalid retained bundle version id")
    if not _is_uuid(organization_id):
        raise ValueError("invalid retained organization binding")
    if version_workflow_id != workflow_id:
        raise ValueError("retained bundle version workflow mismatch")
    if version_artifact_sha != artifact_sha:
        raise ValueError("retained bundle version artifact mismatch")
    if not _is_uuid(runtime_validation_id):
        raise ValueError("invalid retained runtime validation binding")
    if not isinstance(version_number, int) or isinstance(version_number, bool):
        raise ValueError("invalid retained bundle version")
    if version_number < 1:
        raise ValueError("invalid retained bundle version")
    attestation = result.get("attestation_binding")
    if not isinstance(attestation, dict):
        raise ValueError("bundle ingest has no attestation binding")
    challenge_id = attestation.get("challenge_id")
    schema = attestation.get("schema")
    policy = attestation.get("policy")
    if not isinstance(challenge_id, str) or not 1 <= len(challenge_id) <= 200:
        raise ValueError("invalid attestation id")
    if not _is_runtime_attestation_schema(schema):
        raise ValueError("invalid attestation schema")
    if not isinstance(policy, str) or not policy:
        raise ValueError("invalid certification policy")
    bundle_sha = _required_sha256(
        attestation.get("bundle_sha256"), field="bundle binding"
    )
    if bundle_sha != approved_sha:
        raise ValueError("attested bundle does not match approved archive")
    policy_evidence_sha = _required_sha256(
        attestation.get("policy_evidence_sha256"),
        field="policy evidence binding",
    )
    parameter_schema_sha = _required_sha256(
        attestation.get("parameter_schema_sha256"),
        field="parameter schema binding",
    )
    run_report_sha = _required_sha256(
        attestation.get("run_report_sha256"), field="run report binding"
    )
    source_recording_sha = _required_sha256(
        attestation.get("source_recording_sha256"),
        field="attested source recording binding",
    )
    template_sha = attestation.get("governed_authorization_template_sha256")
    if template_sha is not None:
        template_sha = _required_sha256(
            template_sha, field="governed authorization template binding"
        )
    if schema == "openadapt.runtime-validation/v3" and template_sha is None:
        raise ValueError("v3 attestation has no governed authorization template")
    if (
        schema
        in {
            "openadapt.runtime-validation/v1",
            "openadapt.runtime-validation/v2",
        }
        and template_sha is not None
    ):
        raise ValueError("legacy attestation has an unexpected template binding")
    resolves_run_id = result.get("resolves_run_id")
    if resolves_run_id is not None and not _is_uuid(resolves_run_id):
        raise ValueError("invalid resolved run binding")
    if version_resolves_run_id != resolves_run_id:
        raise ValueError("retained resolved run binding mismatch")
    document["workflow_id"] = workflow_id
    document["attestation"] = {"id": challenge_id, "schema": schema}
    document["binding"].update(
        {
            "bundle_sha256": bundle_sha,
            "source_recording_sha256": source_recording_sha,
            "certification_policy": policy,
            "certification_evidence_sha256": policy_evidence_sha,
            "governed_authorization_template_sha256": template_sha,
            "parameter_schema_sha256": parameter_schema_sha,
            "attested_run_report_sha256": run_report_sha,
            "resolves_run_id": resolves_run_id,
            "organization_id": organization_id,
            "bundle_version_id": bundle_version_id,
            "bundle_version": version_number,
            "runtime_validation_id": runtime_validation_id,
        }
    )
    dashboard_url = result.get("dashboard_url")
    destination_host = result.get("destination_host")
    if not isinstance(dashboard_url, str) or not isinstance(destination_host, str):
        raise ValueError("missing trusted dashboard binding")
    dashboard = urlsplit(dashboard_url)
    destination = urlsplit(destination_host)
    if dashboard[:2] != destination[:2] or (
        dashboard.path != f"/dashboard/workflows/{workflow_id}"
        or dashboard.query
        or dashboard.fragment
    ):
        raise ValueError("dashboard origin mismatch")
    document["dashboard_url"] = dashboard_url
    document["next_action"] = "open_dashboard"
    return document


def _print_push_json(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def _cmd_push(args: argparse.Namespace) -> int:
    """Upload the exact approved sanitized archive to ``/api/ingest``.

    ``PATH`` defaults to the most-recent recording directory. Raw input creates
    a derivative and pauses for review; approved input sends the exact frozen
    archive and prints the server-assigned workflow id/dashboard URL.
    """
    from openadapt_flow.hosted import HostedDeliveryUncertain, HostedError, push

    try:
        result = push(
            args.path,
            kind=args.kind,
            name=args.name,
            workflow_id=args.workflow_id,
            resolves_run_id=args.resolves_run_id,
            host=args.host,
            token=args.token,
            deployment_kind=args.deployment_kind,
            attest_non_phi=args.attest_non_phi,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
            sanitized_out=args.sanitized_out,
            auto_approve=args.auto_approve,
            validation_attestation=args.validation_attestation,
        )
    except HostedDeliveryUncertain as e:
        if args.json:
            _print_push_json(_push_json_failure(uncertain=True, context=e.context))
            return 1
        reconciliation = _push_json_failure(uncertain=True, context=e.context)
        artifact_sha256 = reconciliation["binding"]["artifact_sha256"]
        print("Push delivery is uncertain. The server can have received the artifact.")
        if artifact_sha256 is not None:
            print(f"Artifact SHA-256: {artifact_sha256}")
        print(
            "Do not retry this upload. Reconcile the artifact in the hosted "
            "control plane first."
        )
        return 1
    except HostedError as e:
        if args.json:
            _print_push_json(_push_json_failure())
            return 1
        print(f"push failed: {e}")
        return 1
    if args.json:
        try:
            document = _push_json_result(result)
        except ValueError:
            _print_push_json(_push_json_invalid_response(result))
            return 1
        _print_push_json(document)
        return 0
    if result.get("pending_review"):
        print(f"Sanitized derivative created at {result['sanitized_path']}.")
        print(
            "Upload paused for local review; the original was not modified or uploaded."
        )
        print(f"Review original: {result['original_path']}")
        print(f"Sanitized derivative: {result['sanitized_path']}")
        print(
            "Review locally: openadapt-flow review-sanitized "
            f"{result['sanitized_path']} --original {result['original_path']}"
        )
        return 0
    kind = result.get("kind")
    server_status = result.get("status", "?")
    if result.get("workflow_id"):
        version = result.get("version")
        version_number = version.get("version") if isinstance(version, dict) else None
        detail = (
            f"name={result.get('workflow_name')!r}, kind={kind}, status={server_status}"
        )
        if version_number is not None:
            detail += f", version={version_number}"
        print(f"Pushed. workflow_id={result['workflow_id']} ({detail}).")
    else:
        print(
            f"Pushed. artifact_ingest_id={result.get('artifact_ingest_id')} "
            f"(kind={kind}, status={server_status})."
        )
    if result.get("dashboard_url"):
        print(f"Dashboard: {result['dashboard_url']}")
    return 0


def _cmd_validate_hosted(args: argparse.Namespace) -> int:
    """Create a challenge-bound operator runtime-validation attestation."""
    import json

    from openadapt_flow.runtime_validation import (
        RuntimeValidationError,
        create_runtime_validation_attestation,
        save_runtime_validation_attestation,
    )

    try:
        compiler_config = (
            json.loads(Path(args.compiler_config).read_text(encoding="utf-8"))
            if args.compiler_config
            else None
        )
        if compiler_config is not None and not isinstance(compiler_config, dict):
            raise RuntimeValidationError("Compiler config must be a JSON object")
        attestation = create_runtime_validation_attestation(
            recording_derivative=Path(args.recording),
            bundle_derivative=Path(args.bundle),
            run_dir=Path(args.run_dir),
            policy_source=args.policy,
            risk_class=args.risk_class,
            environment=args.environment,
            target_kind=args.target_kind,
            target_url=args.target_url,
            allowed_hosts=args.allowed_host,
            compiler_config=compiler_config,
            host=args.host,
            token=args.token,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
        output = save_runtime_validation_attestation(attestation, Path(args.out))
    except (OSError, json.JSONDecodeError, RuntimeValidationError) as exc:
        print(f"validate-hosted failed: {exc}")
        return 1
    print(f"Runtime-validation attestation written to {output}.")
    print(
        "This is a challenge-bound operator attestation, not independent "
        "certification. Upload it once with `push --validation-attestation`."
    )
    return 0


def _cmd_sanitize(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, sanitize_artifact

    try:
        manifest = sanitize_artifact(
            Path(args.path),
            Path(args.out),
            kind=args.kind,
            redactions_file=Path(args.redactions) if args.redactions else None,
            overwrite=args.overwrite,
        )
    except SanitizationError as e:
        print(f"sanitize failed: {e}")
        return 1
    print(
        f"Sanitized {manifest['processed_file_count']} file(s) into {args.out}; "
        f"execution semantics: {manifest['execution_semantics']}."
    )
    print(
        "Review locally: openadapt-flow review-sanitized "
        f"{args.out} --original {args.path}"
    )
    return 0


def _cmd_review_sanitized(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, serve_review

    try:
        serve_review(
            Path(args.original),
            Path(args.path),
            port=args.port,
            open_browser=not args.no_open,
        )
    except SanitizationError as e:
        print(f"review failed: {e}")
        return 1
    return 0


def _cmd_approve_sanitized(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, approve_derivative

    try:
        approval = approve_derivative(
            Path(args.path), source=Path(args.original), reviewer=args.reviewer
        )
    except SanitizationError as e:
        print(f"approval failed: {e}")
        return 1
    print(
        "Approved immutable archive "
        f"sha256={approval['approved_derivative_sha256']} "
        f"size={approval['approved_archive_size_bytes']} bytes."
    )
    return 0


def _cmd_materialize_approved(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import (
        SanitizationError,
        materialize_approved_derivative,
    )

    try:
        approval = materialize_approved_derivative(
            Path(args.archive),
            approval=Path(args.approval),
            destination=Path(args.out),
            expected_archive_sha256=args.expect_archive_sha256,
        )
    except SanitizationError as e:
        print(f"materialize failed: {e}")
        return 1
    print(
        f"Materialized the approved derivative into {args.out}; "
        f"reviewer={approval['reviewer']} "
        f"sha256={approval['approved_derivative_sha256']}."
    )
    return 0


def _cmd_report_break(args: argparse.Namespace) -> int:
    """Emit a PHI-free break diagnostic from a halted run's ``report.json``.

    Reads ``run_dir/report.json`` (``RunReport.halt`` / ``HaltObservation``) —
    halt is read from the report, NOT a process exit code — then builds a
    closed-schema summary from bounded counts and enums only. Free text,
    screenshots, record values, and report/effect hashes never enter the
    request. The recording never leaves the machine.
    """
    from openadapt_flow.hosted import HostedError, report_break

    try:
        result = report_break(
            args.run_dir,
            workflow_id=args.workflow_id,
            host=args.host,
            token=args.token,
            deployment_kind=args.deployment_kind,
            org_id=args.org_id,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"report-break failed: {e}")
        return 1
    if not result.get("emitted"):
        if result.get("local_only"):
            print(f"Break kept LOCAL-ONLY: {result.get('reason')}")
        else:
            print(f"Nothing emitted: {result.get('reason')}")
        return 0
    print(
        f"Break reported (run_id={result.get('run_id')}, "
        f"halt_id={result.get('halt_id')}, status={result.get('status')})."
    )
    if result.get("teach_url"):
        print(f"Teach: {result['teach_url']}")
    return 0


def _cmd_report_run(args: argparse.Namespace) -> int:
    """Emit a PHI-free SUCCESS summary from a completed run's ``report.json``.

    The success counterpart of ``report-break`` (same endpoint, same paired
    ingest credential, same fail-closed PHI boundary): posts a schema-minimal
    summary — validated enums and bounded counts/durations, never record values
    or resolved-effect fingerprints — to ``/api/runs/ingest-report`` so the
    control plane can show locally reported runs. The recording never leaves
    the machine, and self-reported rows are not a billing meter.
    """
    if getattr(args, "receipt", None) is not None:
        return _emit_local_receipt(args)

    from openadapt_flow.hosted import HostedError, report_run

    try:
        result = report_run(
            args.run_dir,
            workflow_id=args.workflow_id,
            host=args.host,
            token=args.token,
            deployment_kind=args.deployment_kind,
            org_id=args.org_id,
            backend=args.backend,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"report-run failed: {e}")
        return 1
    if not result.get("emitted"):
        if result.get("local_only"):
            print(f"Run summary kept LOCAL-ONLY: {result.get('reason')}")
        else:
            print(f"Nothing emitted: {result.get('reason')}")
        return 0
    duplicate = " (already reported)" if result.get("duplicate") else ""
    print(
        f"Run summary reported (run_id={result.get('run_id')}, "
        f"status={result.get('status')}){duplicate}."
    )
    return 0


def _emit_local_receipt(args: argparse.Namespace) -> int:
    """Write a LOCAL, allow-listed run receipt. No network, ever.

    Only a complete governed VERIFIED run may claim the success rail, so a
    legacy unclassified or ``COMPLETED_UNVERIFIED`` run emits nothing. The
    free tutorial can now REACH VERIFIED with real effect evidence, so the
    artifact this gate protects is finally producible without a hosted account.

    The receipt is built additively from the closed allow-list in
    ``openadapt_flow.receipt``; the rich local ``REPORT.md`` -- screenshots,
    typed values, OCR identity text, parameters -- is never the source.
    """
    from openadapt_flow.ir import RunReport
    from openadapt_flow.receipt import ReceiptError, build_receipt, write_receipt

    run_dir = Path(args.run_dir)
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        print(f"report-run failed: no report.json in {run_dir} — nothing to report.")
        return 1
    try:
        report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("report-run failed: report.json is unreadable or invalid")
        return 1

    success_rail_eligible = report.success and report.execution_outcome == "VERIFIED"
    if not success_rail_eligible:
        print(
            "Nothing emitted: run is not VERIFIED; only complete governed "
            "VERIFIED successes may use the success rail"
        )
        return 0

    if not args.production:
        print(
            "report-run REFUSED: --production is required because a saved "
            "report cannot prove synthetic provenance. Run `openadapt-flow "
            "tutorial` to emit the bundled reference receipt directly."
        )
        return 2
    try:
        receipt = build_receipt(report)
    except ReceiptError as e:
        print(f"report-run failed: {e}")
        return 1
    paths = write_receipt(receipt, Path(args.receipt))
    print("Receipt written (LOCAL ONLY, nothing was uploaded):")
    for path in (paths["png"], paths["json"], paths["markdown"]):
        print(f"  {path}")
    print(
        "\nEvery byte that would leave this machine is in receipt.json. "
        "Publishing is a separate, explicit step."
    )
    print(
        "This receipt is marked `production`. Review it, then use the "
        "sanitize / review-sanitized / approve-sanitized flow before it "
        "leaves your trust boundary."
    )
    return 0


def _maybe_report_break(run_dir: Path, report) -> None:
    """Opt-in post-run hook: emit a break diagnostic when a run halts.

    Off by default and fully best-effort — it only fires when BOTH
    ``OPENADAPT_FLOW_HOSTED_WORKFLOW_ID`` is set (the hosted workflow id this
    bundle maps to) and the run carries a halt. Any failure is swallowed so the
    hook NEVER changes the run's outcome or exit code (WRAP-not-rewrite).
    """
    import os

    workflow_id = os.environ.get("OPENADAPT_FLOW_HOSTED_WORKFLOW_ID")
    attention_outcomes = {
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    }
    if not workflow_id or (
        getattr(report, "halt", None) is None
        and getattr(report, "execution_outcome", None) not in attention_outcomes
    ):
        return
    try:
        from openadapt_flow.hosted import report_break

        result = report_break(
            run_dir,
            workflow_id=workflow_id,
            deployment_kind=os.environ.get("OPENADAPT_FLOW_DEPLOYMENT_KIND", "cloud"),
            org_id=os.environ.get("OPENADAPT_FLOW_ORG_ID"),
        )
        if result.get("emitted"):
            print(f"Break reported to hosted control plane (workflow {workflow_id}).")
    except Exception as e:  # noqa: BLE001 — a diagnostic hook must never fail a run
        print(f"(break report skipped: {e})")


def _maybe_report_run(
    run_dir: Path,
    report,
    args=None,
    *,
    backend_kind: Optional[str] = None,
) -> None:
    """Opt-in post-run hook: emit a PHI-free VERIFIED summary (the L0 rail).

    NEVER auto-uploads: it fires only when the operator explicitly opted in —
    ``run --report`` on this invocation, or ``OPENADAPT_FLOW_REPORT_RUN=1``
    in the environment/config — and the run SUCCEEDED. Fully best-effort:
    any failure is swallowed so the hook never changes the run's outcome or
    exit code (mirrors ``_maybe_report_break``).
    """
    import os

    opted_in = bool(getattr(args, "report", False)) or os.environ.get(
        "OPENADAPT_FLOW_REPORT_RUN", ""
    ).lower() in ("1", "true", "yes")
    execution_outcome = getattr(report, "execution_outcome", None)
    success_rail_eligible = bool(getattr(report, "success", False)) and (
        execution_outcome is None or execution_outcome == "VERIFIED"
    )
    if not opted_in or not success_rail_eligible:
        return
    try:
        from openadapt_flow.hosted import report_run

        result = report_run(
            run_dir,
            workflow_id=os.environ.get("OPENADAPT_FLOW_HOSTED_WORKFLOW_ID"),
            # Only the closed backend token crosses this PHI-free rail. Window
            # owner/title/readiness hints remain local bundle configuration.
            backend=backend_kind or getattr(args, "backend", None),
        )
        if result.get("emitted"):
            print(
                "Run summary reported to hosted control plane "
                f"(run_id={result.get('run_id')})."
            )
        elif result.get("local_only"):
            print(f"(run summary kept LOCAL-ONLY: {result.get('reason')})")
    except Exception as e:  # noqa: BLE001 — a reporting hook must never fail a run
        print(f"(run summary report skipped: {e})")


def _maybe_attest_pre_state(run_dir: Path, args=None) -> None:
    """Opt-in pre-actuation hook: snapshot the system of record for attest.

    Delegates to :mod:`openadapt_flow.attest_bridge` (a silent no-op without
    ``--attest-contract`` / ``OPENADAPT_FLOW_ATTEST_CONTRACT``). Wrapped so
    even an import failure cannot affect the run (WRAP-not-rewrite, mirrors
    ``_maybe_report_break``).
    """
    try:
        from openadapt_flow.attest_bridge import maybe_capture_pre_state

        maybe_capture_pre_state(run_dir, args)
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest pre-state capture skipped: {e})")


def _maybe_attest_run(run_dir: Path, report, args=None) -> None:
    """Opt-in post-run hook: verify the claimed effect via openadapt-attest.

    Delegates to :mod:`openadapt_flow.attest_bridge`, which lazy-imports the
    separate ``openadapt_attest`` sidecar and degrades to one printed line
    when it is not installed. Wrapped so no exception can ever change the
    run's outcome, report, or exit code (mirrors ``_maybe_report_run``).
    """
    try:
        from openadapt_flow.attest_bridge import maybe_attest_run

        maybe_attest_run(run_dir, report, args)
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest receipt skipped: {e})")


def _cmd_teach(args: argparse.Namespace) -> int:
    """Self-serve HALT -> LEARN -> RESOLVE for a halted run + a fix demo.

    Drives the governed halt->learn loop (induce the operator resolution as a
    guarded exception branch, gate it, validate it on held-out coverage) and
    writes an UPDATED bundle ONLY when it promotes. On a governed refusal
    (underdetermined or unsafe correction) nothing is written, the base bundle
    stays halting, and this exits nonzero.
    """
    from openadapt_flow.learning.teach import TeachError, teach

    try:
        result = teach(
            Path(args.run_dir),
            Path(args.fix),
            Path(args.out),
            bundle=Path(args.bundle),
            skill_id=args.skill_id,
            library_dir=Path(args.library) if args.library else None,
        )
    except TeachError as e:
        print(f"teach cannot run: {e}")
        return 2

    print(result.summary())
    if result.promoted:
        print(
            "\nLEARNED. Re-run the updated bundle and the workflow no longer "
            f"halts on this situation:\n    openadapt-flow replay {args.out}"
        )
        return 0
    print(
        "\nREFUSED (governed): the correction was underdetermined or would "
        "weaken a safety invariant, so nothing was promoted and the base bundle "
        "is unchanged (it still halts here). Supply a clearer or safer fix."
    )
    return 1


def _cmd_repair(args: argparse.Namespace) -> int:
    """Governed repair promotion lifecycle (see docs/REPAIR_LIFECYCLE.md).

    candidate -> reviewed -> replay_passed -> fault_passed -> approved ->
    staged -> canary -> active, with hard fail-closed gates between states,
    human-only approval, atomic hash-verified activation, a bounded
    self-halting canary, and one-command rollback.
    """
    from openadapt_flow.repair.cli import run_repair_command

    return run_repair_command(args)


def _add_backend_flags(p: argparse.ArgumentParser) -> None:
    """Add the backend-selector flags (``--backend`` + targets) to a subparser.

    These override the ``backend`` section of a deployment ``--config``. Default
    (``web`` / no flag) reproduces the historical browser behavior byte-for-byte.
    """
    p.add_argument(
        "--backend",
        choices=["web", "windows", "macos", "linux", "rdp", "citrix"],
        default=None,
        help=(
            "Backend to drive: 'web' (default; Playwright/Chromium), 'windows' "
            "(native Windows via the WAA HTTP agent at replay), 'macos' (one "
            "native Mac app window), 'linux' (one exact AT-SPI app window at "
            "replay), 'rdp' (pixel-only network or local remote desktop), or "
            "'citrix' (the local "
            "Citrix Workspace window; its owner defaults by host OS and a "
            "configured rdp_window may override it). Overrides backend.kind "
            "from --config."
        ),
    )
    p.add_argument(
        "--agent-url",
        default=None,
        metavar="URL",
        help=(
            "Base URL of the in-guest Windows (WAA) agent for --backend windows "
            "(e.g. http://localhost:5001). Replay/run only: desktop record "
            "refuses this flag because local Capture cannot bind to a WAA "
            "endpoint. Overrides backend.agent_url."
        ),
    )
    p.add_argument(
        "--macos-app",
        default=None,
        metavar="APP",
        help=(
            "Owner application for --backend macos (e.g. TextEdit). During "
            "record this scopes Capture to that local app window; during "
            "replay/run it overrides backend.macos_app."
        ),
    )
    p.add_argument(
        "--macos-window-title",
        default=None,
        metavar="TITLE",
        help=(
            "Window-title substring for --backend macos. Ambiguous matches "
            "are refused. During record this scopes Capture; during replay/run "
            "it overrides backend.macos_window_title."
        ),
    )
    p.add_argument(
        "--linux-app",
        default=None,
        metavar="APP",
        help=(
            "Exact AT-SPI application name for --backend linux (e.g. gedit). "
            "Replay/run only: the current Capture path records the local Linux "
            "desktop and refuses this flag. Overrides backend.linux_app."
        ),
    )
    p.add_argument(
        "--linux-window-title",
        default=None,
        metavar="TITLE",
        help=(
            "Exact top-level window title for --backend linux. Zero or "
            "multiple matches are refused. Replay/run only: the current "
            "Capture path records the local Linux desktop and refuses this "
            "flag. Overrides backend.linux_window_title."
        ),
    )
    p.add_argument(
        "--linux-allow-physical-input",
        action="store_true",
        help=(
            "Explicitly allow window-bound X11 pointer/keyboard fallback for "
            "--backend linux replay/run when native AT-SPI actuation is "
            "unavailable. Record refuses this replay-only flag."
        ),
    )
    p.add_argument(
        "--rdp-host",
        default=None,
        metavar="HOST",
        help=(
            "RDP host/IP for --backend rdp (network RDP via FreeRDP). Overrides "
            "backend.rdp_host. Replay/run only: record cannot connect local "
            "Capture to this endpoint. Record inside the remote session, or "
            "capture a local client with --window/--rdp-window."
        ),
    )
    p.add_argument(
        "--rdp-window",
        default=None,
        metavar="OWNER",
        help=(
            "Exact local remote-display window owner/process for --backend "
            "rdp or citrix. On Windows this is the process basename (for "
            "example wfica32); on macOS it is the app owner (for example "
            "'Citrix Viewer'). During record it also scopes local Capture to "
            "that window; during replay/run it overrides backend.rdp_window."
        ),
    )
    p.add_argument(
        "--rdp-window-title",
        default=None,
        metavar="TITLE",
        help=(
            "Exact local remote-display window title used to disambiguate "
            "multiple matching RDP/Citrix client windows. During record it "
            "also scopes local Capture; during replay/run it overrides "
            "backend.rdp_window_title."
        ),
    )
    p.add_argument(
        "--rdp-readiness-text",
        default=None,
        metavar="TEXT",
        help=(
            "Stable text that must be visible in the current remote frame "
            "before input. Required for governed Citrix `run`; overrides "
            "backend.rdp_readiness_text."
        ),
    )
    p.add_argument(
        "--allow-surface-override",
        action="store_true",
        help=(
            "Explicitly permit executing a workflow on a DIFFERENT surface "
            "than the one it was recorded/qualified on (Workflow.surface). "
            "Off by default: a cross-surface run is refused. The override is "
            "recorded in the run report (surface_override) as compatibility "
            "evidence; it is never silent."
        ),
    )


def _add_deployment_flags(
    p: argparse.ArgumentParser, *, worklist: bool = False
) -> None:
    """Add the shared deployment-wiring flags (config + effects + actuation +
    durable, optionally a worklist) to a replay-family subparser."""
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help=(
            "Deployment config YAML wiring backend / actuation / effects / "
            "runtime / policy (see docs/deployment.example.yaml). Direct flags "
            "below override individual fields."
        ),
    )
    p.add_argument(
        "--effects-kind",
        choices=["none", "rest", "fhir", "sql", "file", "document-hash"],
        default=None,
        help=(
            "System-of-record EffectVerifier to wire so consequential writes "
            "are verified against the real record (not the screen). The sql/"
            "file kinds need their config fields (sql_query, root, ...) from "
            "a --config deployment YAML"
        ),
    )
    p.add_argument(
        "--effects-base-url",
        default=None,
        help="Base URL for the rest / fhir effect verifier",
    )
    p.add_argument(
        "--effects-root",
        default=None,
        help="Document-store root for the document-hash effect verifier",
    )
    p.add_argument(
        "--api-actuator",
        action="store_true",
        help=(
            "Wire the API/tool actuation tier: a step carrying an ApiBinding is "
            "performed via the API (deterministic, $0) and confirmed by the "
            "effect verifier, skipping the GUI"
        ),
    )
    p.add_argument(
        "--api-base-url",
        default=None,
        help="Base URL for the API actuator (implies --api-actuator)",
    )
    p.add_argument(
        "--durable",
        action="store_true",
        help=(
            "Enable the Tier-3 durable runtime: checkpoint each verified step "
            "and durably PAUSE on halt, so the run is resumable via `resume` "
            "(never re-performing a confirmed write)"
        ),
    )
    if worklist:
        p.add_argument(
            "--worklist",
            action="append",
            metavar="[RELATION=]FILE",
            help=(
                "CSV/JSON worklist of parameter rows driving a PROGRAM bundle's "
                "loop over a relation (repeatable). 'RELATION=FILE' binds the "
                "file to that relation; a bare 'FILE' binds to the program's "
                "sole loop relation."
            ),
        )


def _add_attest_flags(p: argparse.ArgumentParser) -> None:
    """Add the opt-in openadapt-attest proof-sidecar flags to a replay-family
    subparser. Attest is a SEPARATE, privately distributed package; without it
    these flags print a notice and do nothing (see docs/ATTEST_BRIDGE.md)."""
    p.add_argument(
        "--attest-contract",
        default=None,
        metavar="PATH",
        help=(
            "OPT-IN: effect-contract YAML for the openadapt-attest proof "
            "sidecar. After the run it verifies the claimed effect against "
            "the system of record and writes a SIGNED receipt "
            "(attest_receipt.json) into the run directory. Never changes the "
            "run outcome or exit code. Env fallback: "
            "OPENADAPT_FLOW_ATTEST_CONTRACT"
        ),
    )
    p.add_argument(
        "--attest-sign-key",
        default=None,
        metavar="PATH",
        help=(
            "Signing key for the attest receipt (env fallback: "
            "OPENADAPT_FLOW_ATTEST_SIGN_KEY)"
        ),
    )
    p.add_argument(
        "--attest-audit-log",
        default=None,
        metavar="PATH",
        help=(
            "Append-only audit log the attest sidecar writes to (env "
            "fallback: OPENADAPT_FLOW_ATTEST_AUDIT_LOG)"
        ),
    )
    p.add_argument(
        "--attest-pre-state",
        default=None,
        metavar="PATH",
        help=(
            "Existing pre-actuation system-of-record snapshot for delta "
            "checks. When omitted, replay captures one into the run "
            "directory (attest_pre_state.json) before actuation begins (env "
            "fallback: OPENADAPT_FLOW_ATTEST_PRE_STATE)"
        ),
    )


def _package_version() -> str:
    """The installed ``openadapt-flow`` distribution version.

    Falls back to the source tree's ``openadapt_flow.__version__`` when the
    package is not installed as a distribution (e.g. run from a checkout).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("openadapt-flow")
    except PackageNotFoundError:
        from openadapt_flow import __version__

        return __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="openadapt-flow",
        description=(
            "Record a workflow once, compile it into a deterministic "
            "vision-anchored script, replay it locally, and use bounded "
            "re-resolution or governed repair when the interface drifts."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "record",
        help=(
            "Record YOUR workflow interactively: a headed browser "
            "(--backend web --url), or the operator's real local desktop input "
            "through openadapt-capture (--backend windows/macos/linux/rdp/citrix)"
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help="URL of the app to record against (required for --backend web)",
    )
    p.add_argument(
        "--browser-cdp-endpoint",
        default=None,
        metavar="URL",
        help=(
            "Attach the web recorder to an already-running local Chromium "
            "browser through its loopback DevTools endpoint (for example, "
            "http://127.0.0.1:9222). The recorder selects a tab on the "
            "--url origin and does not launch, navigate, or close the browser."
        ),
    )
    p.add_argument(
        "--browser-page-url",
        default=None,
        metavar="URL",
        help=(
            "Exact current URL of the existing tab to record. Use this with "
            "--browser-cdp-endpoint when more than one open tab has the "
            "--url origin."
        ),
    )
    p.add_argument("--out", required=True, help="Recording output directory")
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="FIELD",
        help=(
            "Mark a typed field (by name or id) as a SECRET; its value is "
            "never persisted and is injected at replay from "
            "OPENADAPT_FLOW_SECRET_<FIELD>. input[type=password] is always "
            "treated as secret. Repeatable."
        ),
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="FIELD",
        help=(
            "Record a typed value as a PARAMETER; its demonstrated value "
            "becomes the default, overridable at replay with --param. For "
            "--backend web, FIELD is the field name/id. For --backend "
            "windows/macos/linux/rdp/citrix (capture has no field identity), use "
            "NAME=VALUE — the typed value equal to VALUE is marked as parameter "
            "NAME. Repeatable."
        ),
    )
    p.add_argument(
        "--identifier",
        action="append",
        default=[],
        metavar="FIELD|X,Y,W,H",
        help=(
            "Mark the RECORD-IDENTIFYING region (patient banner / MRN field) "
            "so the compiler crops its pixels (anchor.identifier_crop) and "
            "the pixel-compare identity tier arms on remote-display/pixel "
            "replays (Citrix/RDP). For --backend web, FIELD is the field "
            "name/id (repeatable; the first marked field present at each "
            "click contributes its rect). For --backend "
            "windows/macos/linux/rdp/citrix (a pixel capture has no field "
            "identity), give the region once as X,Y,W,H in recording "
            "pixels. Unmarked recordings still get automatic crops on "
            "identity-armed pixel steps (from the OCR identity band)."
        ),
    )
    p.add_argument(
        "--task",
        default=None,
        help=(
            "Task description for a desktop "
            "(--backend windows/macos/linux/rdp/citrix) capture session "
            "(stored in the recording metadata)."
        ),
    )
    p.add_argument(
        "--window",
        default=None,
        metavar="OWNER",
        help=(
            "Scope a desktop capture "
            "(--backend windows/macos/linux/rdp/citrix) to ONE "
            "window, recorded in that window's OWN pixel space (owner-app "
            "substring, e.g. --window Parallels / --window 'Citrix Workspace'). "
            "Closes the coordinate-space gap with the pixel (rdp) replay "
            "surface. Combine with --window-title to disambiguate. "
            "Supported on macOS and Windows hosts; refused elsewhere. "
            "Ignored (rejected) for --backend web."
        ),
    )
    p.add_argument(
        "--window-title",
        default=None,
        metavar="SUBSTRING",
        help=(
            "Title substring to disambiguate the --window target when the owner "
            "app has multiple windows (case-insensitive; may be used with or "
            "without --window). The resolved title is stored in the LOCAL "
            "recording metadata only."
        ),
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless (scripted/CI recording)",
    )
    p.add_argument(
        "--profile",
        choices=["demo", "standard", "regulated"],
        default=None,
        help=(
            "Recording posture (Section 5). standard/regulated REQUIRE an "
            "explicit --backend (no implicit browser default in production). "
            "demo may omit --backend: it defaults to the browser (or your "
            "last-used demo target) and prints a visible notice. Omitted: "
            "the permissive pre-profile default (browser, with notice)."
        ),
    )
    _add_backend_flags(p)
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser(
        "demo-record",
        help="Serve MockMed and record the canonical triage demo",
    )
    p.add_argument("--out", required=True, help="Recording output directory")
    p.add_argument(
        "--note-text",
        default="Follow-up in 2 weeks; BP recheck.",
        help="Note text typed during the demo (recorded as a parameter)",
    )
    p.add_argument("--param-name", default="note", help="Parameter name for the note")
    p.add_argument("--drift", default=None, help="Comma-separated MockMed drift modes")
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help=(
            "OPT-IN: capture a WebM video of the recording session into DIR "
            "(default: off; no effect on the recording written to --out)"
        ),
    )
    p.set_defaults(func=_cmd_demo_record)

    p = sub.add_parser(
        "tutorial",
        help=(
            "Run the bundled tutorial end to end against a real local system "
            "of record: record, compile, certify, govern, and VERIFY the write "
            "out of band, then write a shareable local receipt"
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Directory for the tutorial's recording, bundle, and run "
            "(default: tutorials/tutorial-<UTC timestamp> under the current "
            "directory)"
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        help="Workflow name for the compiled bundle (default: local-quickstart)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--guided",
        action="store_true",
        help=(
            "Presentation mode: record the demonstration yourself, then watch "
            "a visibly paced governed replay"
        ),
    )
    p.add_argument(
        "--interactive-record",
        action="store_true",
        help=(
            "Perform the tutorial demonstration yourself in a headed browser; "
            "the browser closes after the saved record is observed"
        ),
    )
    p.add_argument(
        "--presentation-delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Tutorial-only pause before each scripted recording action and "
            "replay step (0-5 seconds; guided default: 1)"
        ),
    )
    p.add_argument(
        "--no-receipt",
        action="store_true",
        help="Skip writing the local receipt (the run and its report are unchanged)",
    )
    p.add_argument(
        "--break-it",
        action="store_true",
        dest="break_it",
        help=(
            "After the clean VERIFIED run, rerun the SAME certified bundle "
            "against a backend that silently rejects the write AFTER the app "
            "paints its success banner -- and watch the engine HALT instead of "
            "believing the screen. The halted run's evidence lands in "
            "<out>/run-broken/REPORT.md"
        ),
    )
    p.set_defaults(func=_cmd_tutorial)

    p = sub.add_parser("compile", help="Compile a recording into a workflow bundle")
    p.add_argument("recording", help="Recording directory")
    p.add_argument("--out", required=True, help="Output bundle directory")
    p.add_argument("--name", required=True, help="Workflow name")
    p.add_argument(
        "--accept-params",
        default=None,
        metavar="NAME1,NAME2",
        help=(
            "Non-interactive confirm pass: accept these flagged field-label "
            "parameter proposals as-is (comma-separated proposed names; see "
            "param_proposals.json in the bundle). Proposals not listed stay "
            "demonstrated constants. Unknown names fail loud."
        ),
    )
    p.add_argument(
        "--params-from",
        default=None,
        metavar="FILE",
        help=(
            "Non-interactive confirm pass: JSON decision file mapping each "
            'proposed name to {"action": "confirm"|"rename"|"secret"|'
            '"constant", "name": "<new name, for rename>"}. Unlisted '
            "proposals stay constants (fail-closed)."
        ),
    )
    p.add_argument(
        "--no-confirm-params",
        action="store_true",
        help=(
            "Skip the interactive parameter review even on a TTY; flagged "
            "proposals stay demonstrated constants."
        ),
    )
    p.set_defaults(func=_cmd_compile)

    p = sub.add_parser(
        "induce",
        help=(
            "Induce a parameterized PROGRAM bundle from MULTIPLE recordings "
            "(multi-trace induction: infer params / loops / branches). REFUSES "
            "(nonzero exit, no bundle) when intent is underdetermined, printing "
            "a record-next worklist of missing demonstrations"
        ),
    )
    p.add_argument(
        "recording",
        nargs="+",
        help=(
            "Two or more recording directories (or already-compiled bundle "
            "directories) of the SAME task"
        ),
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output program-bundle directory (written only when CERTIFIED)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Name for the induced workflow (default: 'induced-program')",
    )
    p.add_argument(
        "--held-out",
        action="store_true",
        help=(
            "Also run leave-one-out held-out validation and print the per-fold "
            "reproduction scores (needs >= 2 traces)"
        ),
    )
    p.set_defaults(func=_cmd_induce)

    p = sub.add_parser(
        "for-each",
        help=(
            "Author a DATA-DRIVEN LOOP bundle: wrap an existing single-"
            "demonstration bundle's linear body in a LOOP that runs once per "
            "record of a worklist (CSV/JSON), binding each record's columns to "
            "the workflow's parameters. Emits a program:true bundle the runtime "
            "executes bounded, $0, identity-gated and effect-verified per record"
        ),
    )
    p.add_argument(
        "bundle",
        help="Existing single-demonstration bundle directory (the linear body)",
    )
    p.add_argument(
        "--records",
        required=True,
        help=(
            "Worklist file (.csv header names columns, or .json list of row "
            "objects); one record = one loop iteration"
        ),
    )
    p.add_argument("--out", required=True, help="Output program-bundle directory")
    p.add_argument(
        "--map",
        action="append",
        metavar="COLUMN=PARAM",
        help=(
            "Map a worklist COLUMN to a workflow PARAM (repeatable). Omit to map "
            "each column to the parameter of the same name. Every column must "
            "map to a known non-secret parameter or authoring FAILS LOUDLY."
        ),
    )
    p.add_argument(
        "--relation",
        default="worklist",
        help="Name of the emitted loop relation (default: 'worklist')",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=1000,
        help=(
            "Hard fail-safe bound on iterations (default 1000); a worklist "
            "longer than this is refused at authoring time and HALTs at run time"
        ),
    )
    p.add_argument(
        "--loop-var",
        default=None,
        help="Optional human label for the loop variable (reports only)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Name for the looped workflow (default: '<body>-for-each')",
    )
    p.set_defaults(func=_cmd_for_each)

    p = sub.add_parser(
        "compose",
        help=(
            "Author a parent sequencer from named compiled child bundles and "
            "a handoff contract. Each child stays bound to its recorded "
            "surface. certify/run the output directory."
        ),
    )
    p.add_argument(
        "--child",
        action="append",
        metavar="NAME=PATH",
        required=True,
        help="Named compiled child bundle (repeat; at least two)",
    )
    p.add_argument(
        "--handoff",
        action="append",
        metavar="FROM.source=TO.target",
        help=(
            "Verified fact copied from a predecessor effect-bound parameter "
            "into a successor parameter. Repeatable. Missing evidence HALTs."
        ),
    )
    p.add_argument(
        "--after",
        action="append",
        metavar="NAME=PRED[,PRED]",
        help=(
            "Explicit DAG predecessors for NAME. Omit to run children in --child order."
        ),
    )
    p.add_argument(
        "--allow-halt",
        action="append",
        metavar="NAME=OUTCOME",
        help=(
            "Let NAME start when a predecessor ended OUTCOME instead of "
            "VERIFIED (HALTED, FAILED, ...). VERIFIED is always allowed."
        ),
    )
    p.add_argument("--out", required=True, help="Output composition directory")
    p.add_argument("--name", default=None, help="Composition name")
    p.set_defaults(func=_cmd_compose)

    p = sub.add_parser(
        "process",
        help=(
            "Author a ProcessContract parent from independently admitted "
            "capabilities. Each --child needs a --admission envelope. "
            "certify/run the output directory; replay refuses it."
        ),
    )
    p.add_argument(
        "--child",
        action="append",
        metavar="NAME=BUNDLE",
        required=True,
        help="Named admitted child bundle pointer (repeat; at least two)",
    )
    p.add_argument(
        "--admission",
        action="append",
        metavar="NAME=ENVELOPE",
        help=(
            "QualificationAdmissionEnvelope for NAME. A compiled recording "
            "without an envelope is not an admitted capability."
        ),
    )
    p.add_argument(
        "--handoff",
        action="append",
        metavar="FROM.source=TO.target",
        help=(
            "Verified fact copied from a predecessor effect-bound parameter "
            "into a successor parameter. Repeatable. Missing evidence HALTs."
        ),
    )
    p.add_argument(
        "--after",
        action="append",
        metavar="NAME=PRED[,PRED]",
        help=(
            "Explicit DAG predecessors for NAME. Omit to run children in --child order."
        ),
    )
    p.add_argument(
        "--allow-halt",
        action="append",
        metavar="NAME=OUTCOME",
        help=(
            "Let the parent absorb NAME ending OUTCOME instead of VERIFIED "
            "and continue to successors without minting facts from NAME."
        ),
    )
    p.add_argument(
        "--input",
        action="append",
        metavar="NAME",
        help="Parent-level parameter name (values arrive at run time)",
    )
    p.add_argument("--out", required=True, help="Output process-contract directory")
    p.add_argument("--name", default=None, help="Process contract name")
    p.set_defaults(func=_cmd_process)

    p = sub.add_parser(
        "replay",
        help=(
            "Replay a bundle (serves the bundled MockMed demo app when "
            "no --url is given)"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--url",
        default=None,
        help=("URL of the target app (default: serve the bundled MockMed demo app)"),
    )
    p.add_argument(
        "--drift",
        default=None,
        help=(
            "Comma-separated MockMed drift modes (theme,move,rename,modal) "
            "to demonstrate bounded drift resolution; only valid without --url"
        ),
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help=(
            "Run output directory "
            "(default: runs/replay-<UTC timestamp> under the current "
            "directory)"
        ),
    )
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument(
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution"
        ),
    )
    p.add_argument(
        "--allow-model-grounding",
        action="store_true",
        help=(
            "EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model "
            "grounder / identity-VLM / state-verifier (a paid API or an on-prem "
            "VLM appliance via OPENADAPT_FLOW_VLM_URL). Screenshots may leave "
            "the box. Off by default: replay is fully local with zero outbound "
            "calls."
        ),
    )
    p.add_argument(
        "--save-healed-to",
        default=None,
        help="Write the healed bundle to this directory",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help=(
            "OPT-IN: capture a WebM video of the replay session into DIR "
            "(default: off; no effect on the run directory or report)"
        ),
    )
    _add_backend_flags(p)
    _add_deployment_flags(p, worklist=True)
    _add_attest_flags(p)
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser(
        "run",
        help=(
            "Execute a bundle under a deployment config (--config): the replay "
            "path wired for a real deployment (backend / effects / actuation / "
            "durable / policy) instead of the demo"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--profile",
        choices=["demo", "standard", "regulated"],
        default=None,
        help=(
            "Named execution posture (or runtime.profile from --config). Demo "
            "is explicitly non-production; Standard "
            "requires certification, durability, identity, and independently "
            "verified consequential effects. Regulated additionally requires "
            "encrypted bundles, strictly sealed evidence assets, and encrypted "
            "durable checkpoints in a customer-controlled deployment. Omit only "
            "for compatibility with a pre-profile deployment."
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help="Target app URL (default: backend.url from --config)",
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help="Run output directory (default: runs/replay-<UTC timestamp>)",
    )
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument(
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution"
        ),
    )
    p.add_argument(
        "--allow-model-grounding",
        action="store_true",
        help=(
            "EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model "
            "component (also settable via runtime.allow_model_grounding)"
        ),
    )
    p.add_argument(
        "--save-healed-to",
        default=None,
        help="Write the healed bundle to this directory",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video", default=None, metavar="DIR", help=argparse.SUPPRESS
    )
    _add_backend_flags(p)
    _add_deployment_flags(p, worklist=True)
    _add_attest_flags(p)
    # Fail-closed admission-gate controls (see openadapt_flow.run_gate).
    p.add_argument(
        "--policy",
        default=None,
        metavar="NAME-OR-PATH",
        help=(
            "Certifying policy the bundle must PASS to run (default: the "
            "deployment config's policy, else 'clinical-write')"
        ),
    )
    p.add_argument(
        "--approve-unverified-writes",
        action="store_true",
        help=(
            "APPROVAL FALLBACK: explicitly approve executing writes whose "
            "effects cannot be independently verified in this deployment (no "
            "verifier configured). Without it such a bundle is refused"
        ),
    )
    p.add_argument(
        "--strict-templates",
        action="store_true",
        help=(
            "Refuse (not just warn) when template/screenshot assets are unsealed "
            "(plaintext at rest)"
        ),
    )
    p.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help=(
            "Escape hatch: permit running a bundle whose workflow.json is NOT "
            "encrypted at rest (disables the encryption gate). Discouraged"
        ),
    )
    p.add_argument(
        "--pin-digest",
        default=None,
        metavar="SHA256",
        help="Refuse unless the bundle's sealed content digest equals this",
    )
    p.add_argument(
        "--pin-version",
        default=None,
        metavar="VERSION",
        help="Refuse unless the bundle's compiler version equals this",
    )
    p.add_argument("--managed-dispatch-file", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--qualification-trust",
        default=None,
        metavar="PATH",
        help=(
            "Qualification signer trust registry for a process-contract parent. "
            "Also OPENADAPT_QUALIFICATION_TRUST or qualification-trust.json "
            "beside process-contract.json"
        ),
    )
    p.add_argument(
        "--execute-url",
        default=None,
        metavar="URL",
        help=(
            "HTTPS Execute v1 base URL for process children "
            "(OPENADAPT_EXECUTE_URL). Local process run uses governed run "
            "unless this and a bearer token are set"
        ),
    )
    p.add_argument(
        "--execute-token",
        default=None,
        metavar="TOKEN",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--execute-environment-id",
        default=None,
        metavar="ID",
        help=(
            "Execute environment_id for process children "
            "(OPENADAPT_EXECUTE_ENVIRONMENT_ID)"
        ),
    )
    p.add_argument(
        "--qualification-authority-file",
        default=None,
        metavar="PATH",
        help=(
            "Private owner-only v2 Production qualification authority; when "
            "supplied it is verified and re-read at every input edge"
        ),
    )
    p.add_argument(
        "--dry-run",
        "--explain",
        dest="dry_run",
        action="store_true",
        help="Print the fail-closed coverage report and exit WITHOUT executing",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help=(
            "OPT-IN: after a SUCCESSFUL run, emit the PHI-free run summary to "
            "the paired hosted control plane (see `report-run`). Off by "
            "default — nothing is ever uploaded without this flag (or "
            "OPENADAPT_FLOW_REPORT_RUN=1)"
        ),
    )
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser(
        "resume",
        help=(
            "Resume a durably-paused run from its last verified checkpoint "
            "(never re-running an already-confirmed write)"
        ),
    )
    p.add_argument("run_dir", help="The paused run directory (holds checkpoints)")
    p.add_argument("--managed-dispatch-file", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--qualification-authority-file",
        default=None,
        metavar="PATH",
        help=(
            "Private owner-only v2 Production qualification authority for the "
            "resumed input edge"
        ),
    )
    p.add_argument(
        "--qualification-campaign-authority-file",
        default=None,
        metavar="PATH",
        help=(
            "Private signed non-production campaign authority for a resumed "
            "qualification input edge"
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help="Target app URL to rebuild a live backend (default: backend.url)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--require-approval",
        action="store_true",
        help=(
            "Refuse to resume unless the pending escalation is 'approved' "
            "(see `approve`)"
        ),
    )
    p.add_argument(
        "--report",
        action="store_true",
        help=(
            "After a successful resume, send the PHI-minimal local completion "
            "summary to the paired control plane (also enabled by "
            "OPENADAPT_FLOW_REPORT_RUN=1)"
        ),
    )
    # A deployment whose effect verifier binds run parameters
    # (effects.path_params / search_param_exprs / sql_query_params) needs the
    # SAME params to rebuild the verifier on resume — without them the
    # construction fails loud and the resume refuses. Mirror replay/run.
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help=(
            "Parameter substitution (repeatable); required again on resume "
            "when the effect-verifier config binds run parameters"
        ),
    )
    p.add_argument(
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution (see --param)"
        ),
    )
    _add_backend_flags(p)
    _add_deployment_flags(p)
    _add_attest_flags(p)
    p.set_defaults(func=_cmd_resume)

    p = sub.add_parser(
        "approve",
        help=(
            "Record an authenticated approval (approver / resolution / bundle "
            "version) authorizing a durably-paused run to resume"
        ),
    )
    p.add_argument("run_dir", help="The paused run directory (holds the escalation)")
    p.add_argument(
        "--approver",
        default=None,
        help=(
            "Approver identity recorded on the approval (defaults to the "
            "invoking OS user; a blank identity is refused at resume)"
        ),
    )
    p.add_argument(
        "--resolution",
        default=None,
        help=(
            "The chosen resolution (defaults to the pause's first proposed "
            "option) — recorded for the audit trail"
        ),
    )
    p.add_argument(
        "--authorize-uncertain-retry",
        action="store_true",
        help=(
            "After reconciling an uncertain delivery, explicitly authorize one "
            "retry of the possibly dispatched step (ordinary approval refuses)"
        ),
    )
    p.set_defaults(func=_cmd_approve)

    p = sub.add_parser(
        "bench", help="Replay a bundle N times against MockMed and aggregate"
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--n", type=int, default=3, help="Number of iterations")
    p.add_argument(
        "--drift",
        default=None,
        help="Comma-separated drift modes forwarded to the MockMed URL",
    )
    p.add_argument("--run-root", required=True, help="Directory for per-iteration runs")
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.set_defaults(func=_cmd_bench)

    p = sub.add_parser(
        "benchmark",
        help=(
            "Benchmark compiled replay vs. a Claude computer-use agent on "
            "the MockMed triage task (agent arm needs an Anthropic API key "
            "and costs real money)"
        ),
    )
    p.add_argument(
        "--n-compiled",
        type=int,
        default=100,
        help="Compiled-replay iterations",
    )
    p.add_argument("--n-agent", type=int, default=20, help="Agent iterations")
    p.add_argument(
        "--out",
        default="benchmark/",
        help="Output directory for results.json / BENCHMARK.md / chart",
    )
    p.add_argument(
        "--note-text",
        default="Follow-up in 2 weeks; BP recheck.",
        help="Note text both arms enter",
    )
    p.add_argument("--headed", action="store_true", help="Run the browsers headed")
    p.set_defaults(func=_cmd_benchmark)

    p = sub.add_parser(
        "lint",
        help=(
            "Report a bundle's coverage gaps (unarmed clicks, vacuous "
            "postconditions, under-classified risk); exits nonzero by severity"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero on warnings too (default: only on errors)",
    )
    p.set_defaults(func=_cmd_lint)

    p = sub.add_parser(
        "scaffold-verifier",
        help=(
            "Draft an effect-oracle contract (effect_contract.yaml) from a "
            "recording or bundle's write-shaped steps. The output is a DRAFT "
            "requiring human edit; refuses demonstrations with no "
            "consequential (write) step"
        ),
    )
    p.add_argument(
        "source",
        help="Recording directory OR workflow bundle directory",
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        metavar="DIR",
        help=(
            "Directory for the drafted effect_contract.yaml (default: beside the input)"
        ),
    )
    p.set_defaults(func=_cmd_scaffold_verifier)

    p = sub.add_parser(
        "explain",
        help=(
            "Plain-language read of a completed run dir: what happened, why "
            "the outcome is the safe one, and the next suggested command "
            "(read-only)"
        ),
    )
    p.add_argument(
        "run_dir",
        help="Completed run directory (holds report.json)",
    )
    p.set_defaults(func=_cmd_explain)

    p = sub.add_parser(
        "visualize",
        help=(
            "See what a demonstration compiled INTO: emit a program-graph "
            "view of a bundle or a composed parent (child bundles, --after "
            "DAG, effect-bound handoffs) as self-contained HTML, Mermaid, "
            "or the shared JSON graph spec"
        ),
    )
    p.add_argument(
        "bundle",
        help="Workflow bundle directory, or a composed parent directory",
    )
    p.add_argument(
        "--format",
        choices=("html", "mermaid", "json"),
        default="html",
        help=(
            "html: self-contained, offline-openable page (default); "
            "mermaid: flowchart source for Markdown/docs; "
            "json: the shared program-graph spec every surface renders"
        ),
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="Write to this file instead of stdout (parent dirs are created)",
    )
    p.add_argument(
        "--profile",
        choices=(
            "operator-local",
            "remote-safe",
            "public-synthetic",
            "sanitized-derivative",
        ),
        default="operator-local",
        help=(
            "Data boundary for the rendered view. Non-local profiles remove "
            "recorded text, values, selectors, URLs, free-text predicates, "
            "and local provenance. Projection is not source sanitization."
        ),
    )
    p.set_defaults(func=_cmd_visualize)

    p = sub.add_parser(
        "seal",
        help=(
            "Copy a bundle to a new path, encrypt its workflow and template "
            "evidence with OPENADAPT_BUNDLE_KEY, verify integrity, and publish "
            "it atomically"
        ),
    )
    p.add_argument("source", help="Existing workflow bundle directory")
    p.add_argument(
        "-o",
        "--out",
        required=True,
        help="New destination directory (must not already exist)",
    )
    p.set_defaults(func=_cmd_seal)

    p = sub.add_parser(
        "certify",
        help=(
            "Enforce a policy on a bundle (exits nonzero + reports if it "
            "fails); makes 'runnable' distinct from 'certified safe'"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--policy",
        default=None,
        help=(
            "Policy YAML path, or a built-in name (permissive, clinical-write). "
            "Defaults to policy.policy from --config."
        ),
    )
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help="Deployment config YAML to read the policy from when --policy is omitted",
    )
    p.set_defaults(func=_cmd_certify)

    p = sub.add_parser(
        "qualify",
        help=(
            "Create, edit, test, explain, and certify the versioned "
            "qualification project sealed into a workflow bundle"
        ),
    )
    qsub = p.add_subparsers(dest="qualify_cmd", required=True)

    q = qsub.add_parser("schema", help="Print the qualification-project JSON Schema")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "label",
        help="Optionally set, remove, or list presentation-only entity labels",
    )
    label_sub = q.add_subparsers(dest="label_cmd", required=True)
    label = label_sub.add_parser(
        "set",
        help="Set an optional presentation label for one qualified step",
    )
    label.add_argument("bundle", help="Workflow bundle directory")
    label.add_argument("--step", required=True, help="Exact qualified workflow step ID")
    label.add_argument(
        "--label",
        required=True,
        help=(
            "Local class label, for example: insurance claim. Reviewed labels "
            "can cross a remote boundary; other labels use the neutral fallback."
        ),
    )
    label.add_argument(
        "--fallback",
        choices=("record", "item"),
        default=None,
        help=(
            "Neutral remote label for a custom class (default: record). "
            "Reviewed labels use their canonical fallback."
        ),
    )
    label.set_defaults(func=_cmd_qualify)
    label = label_sub.add_parser("remove", help="Remove a step entity label")
    label.add_argument("bundle", help="Workflow bundle directory")
    label.add_argument("--step", required=True, help="Exact qualified workflow step ID")
    label.set_defaults(func=_cmd_qualify)
    label = label_sub.add_parser("list", help="List qualification-owned entity labels")
    label.add_argument("bundle", help="Workflow bundle directory")
    label.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser("init", help="Initialize a bundle's qualification project")
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument(
        "--target",
        required=True,
        choices=("web", "windows", "macos", "linux", "rdp", "citrix"),
    )
    q.add_argument("--application", required=True)
    q.add_argument("--application-version", required=True)
    q.add_argument("--environment-digest", required=True)
    q.add_argument(
        "--runtime-version",
        default=None,
        help="Exact qualified runtime version (default: installed Flow version)",
    )
    q.add_argument("--require-capability", action="append", default=[])
    q.add_argument("--minimum-tier", type=int, choices=(1, 2, 3, 4), default=3)
    q.add_argument("--replace", action="store_true")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "propose",
        help=(
            "Draft application, environment, identity, and effect pins from "
            "the compiled demo. Missing pins HALT; nothing is guessed. "
            "Runs the proposed oracle against a --break-it fault."
        ),
    )
    q.add_argument("bundle", help="Compiled workflow bundle directory")
    q.add_argument(
        "--recording",
        default=None,
        help="Recording directory (meta.json) used to mine application and environment pins",
    )
    q.add_argument(
        "--policy-pack",
        choices=("community", "cloud", "regulated"),
        default="community",
        help="Which shipped pack to bind (default: community)",
    )
    q.add_argument(
        "--proposer",
        choices=("off", "llm"),
        default="off",
        help=(
            "Optional compile-time sketch proposer. Off by default. "
            "Suggestions are flagged and still face the oracle gates."
        ),
    )
    q.add_argument("--out", default=None, help="Write the draft JSON to this path")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "accept",
        help=(
            "Confirm every proposed pin in one command after the proposed "
            "oracle catches a --break-it fault. Refusing a pin HALTs."
        ),
    )
    q.add_argument("bundle", help="Compiled workflow bundle directory")
    q.add_argument(
        "--proposal", required=True, help="Draft qualification-proposal JSON"
    )
    q.add_argument(
        "--refuse-pin",
        choices=("application", "environment", "identity", "effect"),
        default=None,
        help="Refuse one pin instead of accepting. The command HALTs.",
    )
    q.add_argument(
        "--admit-local",
        action="store_true",
        help="Sign a MockMed/local-dev admission that production trust maps refuse",
    )
    q.add_argument("--replace", action="store_true", help="Replace an existing project")
    q.add_argument("--out", default=None, help="Write the accepted proposal JSON")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "admit-local",
        help=(
            "Sign a local-dev admission for an accepted proposal. "
            "The signer cannot enter a production trust map."
        ),
    )
    q.add_argument("bundle", help="Compiled workflow bundle directory")
    q.add_argument(
        "--proposal", required=True, help="Accepted qualification-proposal JSON"
    )
    q.add_argument("--out", default=None, help="Write the local-dev admission JSON")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "from-demo",
        help=(
            "Propose pins from the demo, accept them, and optionally sign a "
            "local-dev admission. After record and compile."
        ),
    )
    q.add_argument("bundle", help="Compiled workflow bundle directory")
    q.add_argument(
        "--recording",
        required=True,
        help="Recording directory that produced the bundle",
    )
    q.add_argument(
        "--policy-pack",
        choices=("community", "cloud", "regulated"),
        default="community",
    )
    q.add_argument(
        "--admit-local",
        action="store_true",
        help="Sign a MockMed/local-dev admission that production trust maps refuse",
    )
    q.add_argument(
        "--proposer",
        choices=("off", "llm"),
        default="off",
        help="Optional compile-time sketch proposer. Off by default.",
    )
    q.add_argument("--out", default=None, help="Write the accepted proposal JSON")
    q.set_defaults(func=_cmd_qualify)

    for verb in ("inspect", "explain", "report"):
        q = qsub.add_parser(verb, help=f"{verb.title()} qualification coverage")
        q.add_argument("bundle", help="Workflow bundle directory")
        q.add_argument("--policy", default=None)
        q.add_argument("--evidence-root", default=None)
        q.add_argument("--json", action="store_true")
        q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "set-identity",
        help="Set exact/normalized identity fields and their quorum",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--step", required=True)
    q.add_argument("--canonical-ladder", action="store_true")
    q.add_argument(
        "--signal",
        action="append",
        metavar="KEY=SOURCE:MODE[:NORMALIZER,...]",
    )
    q.add_argument(
        "--signal-region",
        action="append",
        default=[],
        metavar="KEY=X,Y,W,H",
        help="Explicit qualified pixel region for a context/identifier signal",
    )
    q.add_argument(
        "--signal-param",
        action="append",
        default=[],
        metavar="KEY=PARAM",
        help="Bind a complete workflow parameter value to an identity signal",
    )
    q.add_argument(
        "--signal-extract",
        action="append",
        default=[],
        metavar="KEY=REGEX",
        help=(
            "Extract one named (?P<value>...) field from a structured or "
            "captured-context signal"
        ),
    )
    q.add_argument(
        "--signal-expected",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Bind a qualified PHI-free expected value to an "
            "application/session/workflow-state signal"
        ),
    )
    q.add_argument("--quorum", type=int)
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "set-risk",
        help="Confirm an action's reviewed business-risk classification",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--step", required=True)
    q.add_argument(
        "--classification",
        required=True,
        choices=("read_only", "state_changing", "consequential", "irreversible"),
    )
    q.add_argument("--explanation", required=True)
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "trust-runner",
        help="Trust an Ed25519 qualification-runner public key",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--key-id", required=True)
    q.add_argument("--public-key", required=True, help="Raw 32-byte key in base64")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "set-effect",
        help="Assign verification-strength tier to an existing effect",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--step", required=True)
    q.add_argument("--effect-index", type=int, required=True)
    q.add_argument(
        "--path",
        choices=("gui", "api"),
        default="gui",
        help="Actuation path whose effect is being qualified (default: gui)",
    )
    q.add_argument("--tier", type=int, choices=(1, 2, 3, 4), required=True)
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "business-decision",
        help=(
            "Add, update, or inspect one reviewed finite business decision "
            "without editing the workflow manifest"
        ),
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument(
        "--input",
        metavar="JSON",
        help=(
            "Local openadapt.business-decision-authoring/v1 JSON file "
            "(required without --check)"
        ),
    )
    q.add_argument(
        "--check",
        action="store_true",
        help="List the bundle's current typed decisions without changing it",
    )
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "judgment-cases",
        help=(
            "Store or check reviewed local judgment cases; this command never "
            "synthesizes an executable rule from examples"
        ),
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument(
        "--input",
        metavar="JSON",
        help="Local openadapt.judgment-case-set/v1 JSON file (required without --check)",
    )
    q.add_argument(
        "--check",
        action="store_true",
        help="Evaluate the bundle's current stored cases without changing it",
    )
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser("add-case", help="Add a representative or fault case")
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--case-id", required=True)
    q.add_argument(
        "--kind",
        required=True,
        choices=(
            "representative",
            "ambiguity",
            "wrong_identity",
            "stale_identity",
            "weak_effect",
            "missing_effect",
        ),
    )
    q.add_argument(
        "--expected-outcome",
        required=True,
        choices=(
            "verified",
            "completed_unverified",
            "halted",
            "failed",
            "rolled_back",
        ),
    )
    q.add_argument("--description", default="")
    q.add_argument("--input-ref", default=None)
    q.add_argument("--optional", action="store_true")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "run",
        help=(
            "Import case results produced by the Desktop/local/customer-controlled "
            "qualification runner"
        ),
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--results", required=True, metavar="JSON")
    q.add_argument("--evidence-root", required=True)
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "run-case",
        help=(
            "Run one current representative qualification case with Flow-owned "
            "Standard authorization"
        ),
    )
    q.add_argument("bundle", help="Sealed workflow bundle directory")
    q.add_argument("--case-id", required=True, help="Declared representative case")
    q.add_argument(
        "--inputs",
        required=True,
        metavar="JSON",
        help=(
            "Private canonical runtime-input artifact from Desktop "
            "(mode 0600; params and worklists only)"
        ),
    )
    q.add_argument("--campaign-id", required=True, help="Local campaign identity")
    q.add_argument("--run-id", required=True, help="One local case-attempt identity")
    q.add_argument(
        "--qualification-campaign-authority-file",
        default=None,
        metavar="PATH",
        help=(
            "Private owner-only signed non-production authority for this exact "
            "trial; required once the run actuates under a campaign permit"
        ),
    )
    q.add_argument(
        "--run-dir", required=True, help="New durable case-attempt directory"
    )
    q.add_argument("--url", default=None, help="Target app URL")
    q.add_argument("--headed", action="store_true", help="Run browser headed")
    q.add_argument("--dry-run", action="store_true", help="Validate without input")
    q.add_argument("--save-healed-to", default=None, help=argparse.SUPPRESS)
    _add_backend_flags(q)
    _add_deployment_flags(q)
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "add-requalification",
        help="Add a condition that requires this workflow to be requalified",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument(
        "--kind",
        required=True,
        choices=(
            "workflow_changed",
            "application_version_changed",
            "environment_changed",
            "identity_policy_changed",
            "effect_policy_changed",
            "runtime_version_changed",
            "expiry",
            "operator_requested",
        ),
    )
    q.add_argument("--description", default="")
    q.set_defaults(func=_cmd_qualify)

    q = qsub.add_parser(
        "certify",
        help="Persist a combined qualification and existing-policy decision",
    )
    q.add_argument("bundle", help="Workflow bundle directory")
    q.add_argument("--policy", default="clinical-write")
    q.add_argument(
        "--profile",
        choices=("standard", "regulated"),
        default="standard",
        help="Production execution profile bound into the emitted template",
    )
    q.add_argument("--evidence-root", required=True)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_qualify)

    p = sub.add_parser(
        "propose-qualification",
        help=(
            "Draft a production-shaped qualification contract from a compiled "
            "demo. Alias of `qualify propose`."
        ),
    )
    p.add_argument("bundle", help="Compiled workflow bundle directory")
    p.add_argument(
        "--recording",
        default=None,
        help="Recording directory (meta.json) used to mine application and environment pins",
    )
    p.add_argument(
        "--policy-pack",
        choices=("community", "cloud", "regulated"),
        default="community",
        help="Which shipped pack to bind (default: community)",
    )
    p.add_argument(
        "--proposer",
        choices=("off", "llm"),
        default="off",
        help="Optional compile-time sketch proposer. Off by default.",
    )
    p.add_argument("--out", default=None, help="Write the draft JSON to this path")
    p.set_defaults(func=_cmd_propose_qualification)

    p = sub.add_parser(
        "disambiguate",
        help=(
            "Surface compile-time multiple-choice questions for an ambiguous "
            "demo and apply the answers as guards/params (ask, don't guess)"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for each question on the terminal",
    )
    p.add_argument(
        "--answers",
        help="JSON file mapping question id -> chosen option key",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Save the resolved workflow back into the bundle",
    )
    p.set_defaults(func=_cmd_disambiguate)

    p = sub.add_parser("emit-skill", help="Emit an Agent Skills folder for a bundle")
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--out", required=True, help="Parent directory for the skill folder")
    p.set_defaults(func=_cmd_emit_skill)

    p = sub.add_parser("emit-mcp", help="Emit a standalone MCP server.py for a bundle")
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--out", required=True, help="Path for the generated server.py")
    p.set_defaults(func=_cmd_emit_mcp)

    p = sub.add_parser(
        "teach",
        help=(
            "Self-serve HALT -> LEARN: resolve a halted run from a fix "
            "demonstration. Induces the correction as a guarded exception "
            "branch, gates + validates it, and writes an updated bundle ONLY "
            "if it passes (governed refusal otherwise; nonzero exit)"
        ),
    )
    p.add_argument(
        "run_dir",
        help="The HALTED run directory (holds report.json with a halt)",
    )
    p.add_argument(
        "--fix",
        required=True,
        help=(
            "The fix demonstration: a RECORDING directory of the resolution "
            "(record ONLY the corrective actions, e.g. dismiss the dialog), or "
            "a .json correction spec (scripted / CI: resolution_steps, optional "
            "tail_intents / facts / params)"
        ),
    )
    p.add_argument(
        "--bundle",
        required=True,
        help="The base bundle that halted (seeds the skill's active version)",
    )
    p.add_argument(
        "--out",
        required=True,
        help=(
            "Output directory for the UPDATED bundle (written only when the "
            "correction is promoted)"
        ),
    )
    p.add_argument(
        "--skill-id",
        default=None,
        help="Skill id in the library (default: the run's workflow name)",
    )
    p.add_argument(
        "--library",
        default=None,
        help=(
            "Directory for the versioned skill library that keeps the "
            "promotion lineage (default: <out>.skills)"
        ),
    )
    p.set_defaults(func=_cmd_teach)

    p = sub.add_parser(
        "repair",
        help=(
            "Governed repair promotion lifecycle: register a proposed (healed "
            "or taught) bundle as a CANDIDATE, review its diff, run the replay "
            "and fault campaigns, approve (human), stage, canary, and roll "
            "back. A proposed bundle NEVER becomes active without this "
            "lifecycle. See docs/REPAIR_LIFECYCLE.md"
        ),
    )
    rsub = p.add_subparsers(dest="repair_cmd", required=True)

    def _repair_store_flag(rp: argparse.ArgumentParser) -> None:
        rp.add_argument(
            "--store",
            default=None,
            help=(
                "Repair store directory (candidates, staged bundles by hash, "
                "and the atomic ACTIVE.json pointer). Default: repair-store/"
            ),
        )

    r = rsub.add_parser(
        "register",
        help=(
            "Register a (prior, proposed) bundle pair as a repair candidate; "
            "refuses (fail closed) any contract weakening without a new "
            "qualification revision"
        ),
    )
    r.add_argument("proposed", help="The proposed (healed / taught) bundle")
    r.add_argument(
        "--prior",
        default=None,
        help=(
            "The prior bundle the proposal derives from (omit to import the "
            "detached repair/candidate.json the engine wrote into the "
            "proposed bundle)"
        ),
    )
    r.add_argument(
        "--source",
        choices=["heal", "teach", "manual", "model_suggestion"],
        default="manual",
        help="Where the proposal came from (default: manual)",
    )
    r.add_argument(
        "--evidence",
        default=None,
        help=(
            "Run directory holding the failure evidence (heals/<step>/ "
            "frames); required later by the campaigns"
        ),
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser("list", help="List candidates and their lifecycle states")
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "show", help="Show a candidate's reviewable diff, campaigns, and state"
    )
    r.add_argument("candidate_id")
    r.add_argument(
        "--json", action="store_true", help="Also print the full candidate record"
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser("review", help="Record a human review of the candidate's diff")
    r.add_argument("candidate_id")
    r.add_argument(
        "--reviewed-by",
        default=None,
        help="Reviewer identity (default: the invoking OS user)",
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "campaign",
        help=(
            "Run a campaign against the candidate's evidence frames: 'replay' "
            "(healthy drift battery must all pass) or 'fault' (ambiguity / "
            "wrong-entity / stale-target / dialog / verifier-failure frames "
            "must all be REFUSED)"
        ),
    )
    r.add_argument("candidate_id")
    r.add_argument("--kind", choices=["replay", "fault"], required=True)
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "approve",
        help=(
            "HUMAN approval gating promotion; refuses unless the diff was "
            "reviewed and BOTH campaigns passed. Binds the exact bundle hashes"
        ),
    )
    r.add_argument("candidate_id")
    r.add_argument(
        "--approved-by",
        default=None,
        help=(
            "Approver identity recorded on the approval (default: the "
            "invoking OS user; REQUIRED with --non-interactive)"
        ),
    )
    r.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Skip the confirmation prompt (automation). Requires an explicit "
            "--approved-by human identity"
        ),
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "stage",
        help=(
            "Copy BOTH bundles into the store by content hash and re-verify "
            "them byte-exact (prior too, so rollback is always local)"
        ),
    )
    r.add_argument("candidate_id")
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "canary",
        help=(
            "Atomically activate the staged bundle in CANARY mode: a bounded "
            "first-N-runs window with per-run verification before full active"
        ),
    )
    r.add_argument("candidate_id")
    r.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Canary window size (default: the candidate's configured window)",
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "canary-record",
        help=(
            "Record one canary run from its run directory; any "
            "silent-incorrect or verification regression auto-reverts to the "
            "prior bundle"
        ),
    )
    r.add_argument("candidate_id")
    r.add_argument("--run-dir", required=True, help="The completed run directory")
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "rollback",
        help=(
            "One-command rollback: restore the prior bundle hash as the active pointer"
        ),
    )
    r.add_argument(
        "--candidate-id",
        default=None,
        help="Candidate to roll back (default: the currently active one)",
    )
    r.add_argument(
        "--by",
        default=None,
        help="Operator identity recorded on the rollback (default: OS user)",
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    r = rsub.add_parser(
        "status", help="Show the atomic active-bundle pointer and its lineage"
    )
    _repair_store_flag(r)
    r.set_defaults(func=_cmd_repair)

    p = sub.add_parser(
        "connect",
        help=(
            "Claim a one-time browser pairing and store the workspace credential "
            "in the OS keychain"
        ),
    )
    pairing_source = p.add_mutually_exclusive_group(required=True)
    pairing_source.add_argument(
        "--pairing",
        default=None,
        help="Five-minute one-time pairing code from Cloud settings",
    )
    pairing_source.add_argument(
        "--uri",
        default=None,
        help="Exact openadapt://connect deep link supplied by the desktop app",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Cloud origin (default: https://app.openadapt.ai)",
    )
    p.add_argument(
        "--device-name",
        default=None,
        help="Name shown for this computer (default: local hostname)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the pairing destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.set_defaults(func=_cmd_connect)

    p = sub.add_parser(
        "login",
        help=(
            "Validate an ingest token against the hosted control plane and "
            "store it in the OS keychain"
        ),
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (oai_ingest_…). Falls back to OPENADAPT_INGEST_TOKEN, "
            "then OS keychain, then an existing config migration token. "
            "Mint one at <host>/dashboard/settings/ingest."
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the token destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="Validate only; do not store the host or token",
    )
    p.add_argument(
        "--allow-plaintext-token",
        action="store_true",
        help=(
            "Explicitly allow mode-0600 config token storage when no OS keychain "
            "is available (insecure fallback)"
        ),
    )
    p.set_defaults(func=_cmd_login)

    p = sub.add_parser(
        "sanitize",
        help="Create a verified PHI-scrubbed derivative without modifying the original",
    )
    p.add_argument("path", help="Original recording or bundle directory")
    p.add_argument("--out", required=True, help="New sanitized derivative directory")
    p.add_argument(
        "--kind", choices=["recording", "bundle"], required=True, help="Artifact type"
    )
    p.add_argument(
        "--redactions",
        default=None,
        help="Optional local JSON file with additional text/image redactions",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing derivative (never modifies the original)",
    )
    p.set_defaults(func=_cmd_sanitize)

    p = sub.add_parser(
        "review-sanitized",
        help="Review original vs sanitized content in a loopback-only local viewer",
    )
    p.add_argument("path", help="Sanitized derivative directory")
    p.add_argument("--original", required=True, help="Original artifact directory")
    p.add_argument(
        "--port", type=int, default=0, help="Loopback port (default: random)"
    )
    p.add_argument(
        "--no-open", action="store_true", help="Print the viewer URL without opening it"
    )
    p.set_defaults(func=_cmd_review_sanitized)

    p = sub.add_parser(
        "materialize-approved",
        help="Rebuild an approved derivative from its archive and approval record",
        description=(
            "Approval binds the archive's own SHA-256, so the archive cannot "
            "carry it and an archive unzipped elsewhere reads as unapproved. "
            "This places the reviewer's existing approval record beside the "
            "extracted bytes and then runs the unchanged approval gate. It "
            "approves nothing."
        ),
    )
    p.add_argument(
        "--archive", required=True, help="Approved immutable archive (.approved.zip)"
    )
    p.add_argument(
        "--approval",
        required=True,
        help=(
            "Existing approval record: either the derivative's "
            ".openadapt-approval.json or the openadapt.sanitization/v1 ingest "
            "envelope a deployment persisted for it"
        ),
    )
    p.add_argument(
        "--out", required=True, help="New, empty derivative directory to materialize"
    )
    p.add_argument(
        "--expect-archive-sha256",
        default=None,
        help=(
            "Pin the archive to a digest the caller already trusts, checked "
            "independently of the approval record"
        ),
    )
    p.set_defaults(func=_cmd_materialize_approved)

    p = sub.add_parser(
        "approve-sanitized",
        help="Approve and freeze the exact reviewed derivative as an immutable archive",
    )
    p.add_argument("path", help="Sanitized derivative directory")
    p.add_argument("--original", required=True, help="Original artifact directory")
    p.add_argument(
        "--reviewer", required=True, help="Reviewer identity for the audit record"
    )
    p.set_defaults(func=_cmd_approve_sanitized)

    p = sub.add_parser(
        "validate-hosted",
        help=(
            "Bind strict lint, policy certification, and a successful local "
            "replay to an expiring Cloud challenge and exact approved artifacts. "
            "Standard and Regulated activation also binds the sealed governed "
            "authorization template."
        ),
    )
    p.add_argument(
        "--recording",
        required=True,
        help="Approved sanitized recording derivative used to compile the bundle",
    )
    p.add_argument(
        "--bundle",
        required=True,
        help="Approved sanitized bundle derivative whose exact archive will upload",
    )
    p.add_argument(
        "--run-dir",
        required=True,
        help="Successful governed replay directory containing report.json",
    )
    p.add_argument(
        "--policy",
        required=True,
        help="Named or file-backed policy that the bundle must pass",
    )
    p.add_argument(
        "--risk-class",
        required=True,
        choices=["low", "consequential"],
        help=(
            "Compiled workflow risk class: low for reversible-only workflows, "
            "consequential when any step is irreversible; must match the bundle"
        ),
    )
    p.add_argument(
        "--environment",
        required=True,
        help=(
            "Non-PHI validation environment identifier; only its SHA-256 is uploaded"
        ),
    )
    p.add_argument(
        "--target-kind",
        choices=["web", "windows", "macos", "linux", "rdp", "citrix"],
        default=None,
        help=(
            "Optional expected replay substrate. The signed value is derived "
            "from report.json; a supplied value must match it exactly."
        ),
    )
    p.add_argument(
        "--target-url",
        default=None,
        help=(
            "Web only: exact non-PHI HTTPS entry URL used by the validated "
            "browser workflow; required for web and refused for native/remote"
        ),
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help=(
            "Additional exact hostname the hosted browser may reach (repeatable); "
            "the target hostname is included automatically"
        ),
    )
    p.add_argument(
        "--compiler-config",
        default=None,
        help="Optional JSON object describing the compile configuration",
    )
    p.add_argument("--out", required=True, help="Attestation JSON output path")
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: configured host or https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the challenge destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Ingest token used to acquire and sign the one-time challenge",
    )
    p.set_defaults(func=_cmd_validate_hosted)

    p = sub.add_parser(
        "push",
        help=(
            "Upload the exact approved sanitized archive to /api/ingest; "
            "raw input is sanitized locally and paused for review"
        ),
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help=(
            "Recording (or bundle) directory to push. Default: the most-recent "
            "recording directory found under the current directory."
        ),
    )
    p.add_argument(
        "--kind",
        choices=["recording", "bundle"],
        default="recording",
        help="What the directory is (default: recording)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Workflow name (the server auto-suggests one otherwise)",
    )
    p.add_argument(
        "--workflow-id",
        default=None,
        help=(
            "Existing hosted workflow UUID to receive this validated bundle as "
            "a new active version (bundle uploads only)"
        ),
    )
    p.add_argument(
        "--deployment-kind",
        choices=["cloud", "byoc", "regulated"],
        default=None,
        help=(
            "Execution deployment lane (independent of destination trust; default: "
            "OPENADAPT_FLOW_DEPLOYMENT_KIND env, then config.toml "
            "deployment_lane, else cloud). All lanes may upload only a verified "
            "sanitized derivative."
        ),
    )
    p.add_argument(
        "--attest-non-phi",
        action="store_true",
        help=(
            "Deprecated and refused: declarations no longer bypass sanitization, "
            "review, or exact-hash approval."
        ),
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help=(
            "Trust class for the upload endpoint. app.openadapt.ai is recognized "
            "automatically; customer-managed endpoints also require --trusted-host."
        ),
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin, e.g. https://control.example (repeatable)",
    )
    p.add_argument(
        "--sanitized-out",
        default=None,
        help="Where to create the derivative when PATH is raw (default: OPENADAPT_HOME)",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        default=None,
        help=(
            "Administrator policy approval for fully covered, stable derivatives; "
            "human review is the default."
        ),
    )
    p.add_argument(
        "--validation-attestation",
        default=None,
        help=("Challenge-bound runtime-validation JSON required for runnable bundles"),
    )
    p.add_argument(
        "--resolves-run-id",
        default=None,
        help=(
            "Halted run UUID resolved by this validated replacement; requires "
            "--kind bundle and --workflow-id"
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (default: OPENADAPT_INGEST_TOKEN env, OS keychain, "
            "then an existing config migration token)"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit one stable openadapt.push-result/v1 JSON object for Desktop "
            "and other local controllers"
        ),
    )
    p.set_defaults(func=_cmd_push)

    p = sub.add_parser(
        "report-break",
        help=(
            "Emit a PHI-free break diagnostic from a halted run's report.json "
            "to /api/runs/ingest-report (the recording stays local)"
        ),
    )
    p.add_argument("run_dir", help="The halted run directory (holds report.json)")
    p.add_argument(
        "--workflow-id",
        required=True,
        help="The hosted workflow id this run belongs to (from `push`/dashboard)",
    )
    p.add_argument(
        "--deployment-kind",
        choices=["cloud", "byoc"],
        default="cloud",
        help=(
            "Backward-compatible hint; v2 provenance is derived server-side "
            "and this value is not sent"
        ),
    )
    p.add_argument(
        "--org-id",
        default=None,
        help=(
            "Backward-compatible hint; token ownership is derived server-side "
            "and this value is not sent"
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the break-report destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (default: OPENADAPT_INGEST_TOKEN env, OS keychain, "
            "then an existing config migration token)"
        ),
    )
    p.set_defaults(func=_cmd_report_break)

    p = sub.add_parser(
        "report-run",
        help=(
            "Emit a PHI-free SUCCESS summary from a completed run's "
            "report.json to /api/runs/ingest-report (bounded counts, duration, "
            "and exact pushed-bundle identity — the recording stays local)"
        ),
    )
    p.add_argument("run_dir", help="The completed run directory (holds report.json)")
    p.add_argument(
        "--workflow-id",
        default=None,
        help=(
            "Hosted workflow id (from `push`/dashboard). When omitted the "
            "summary binds by the exact pushed bundle content digest"
        ),
    )
    p.add_argument(
        "--deployment-kind",
        choices=["local", "cloud", "byoc"],
        default="local",
        help=(
            "Deprecated compatibility input from Flow 1.18.0; accepted but "
            "ignored because authenticated server provenance is authoritative"
        ),
    )
    p.add_argument(
        "--backend",
        choices=["web", "windows", "macos", "linux", "rdp", "citrix"],
        default=None,
        help=(
            "Backend/substrate this run executed on "
            "(web/windows/macos/linux/rdp/citrix)"
        ),
    )
    p.add_argument(
        "--org-id",
        default=None,
        help=(
            "Deprecated compatibility input from Flow 1.18.0; accepted but "
            "ignored because token ownership is authoritative"
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the run-summary destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (default: OPENADAPT_INGEST_TOKEN env, OS keychain, "
            "then an existing config migration token)"
        ),
    )
    p.add_argument(
        "--receipt",
        default=None,
        metavar="DIR",
        help=(
            "LOCAL ONLY: write a shareable run receipt (receipt.png / .json / "
            ".md) into DIR instead of contacting any host. The receipt is "
            "generated from a closed allow-list -- closed enums, counts, "
            "digests, versions -- and can carry no screenshot, OCR text, typed "
            "value, parameter, URL, hostname, coordinate, or free-form text"
        ),
    )
    p.add_argument(
        "--production",
        action="store_true",
        help=(
            "--receipt only: explicitly mark this as a real production run. "
            "A production receipt must go "
            "through sanitize/approve before it leaves your trust boundary"
        ),
    )
    p.set_defaults(func=_cmd_report_run)

    p = sub.add_parser(
        "console",
        help=(
            "Serve the operator console: a localhost-only web UI over compiled "
            "bundles, run reports/halt evidence, and skill-library lineage. "
            "Read-only unless --allow-actions; needs `pip install "
            "'openadapt-flow[console]'`"
        ),
    )
    p.add_argument(
        "--bundles",
        default=".",
        help=(
            "Directory scanned (2 levels deep) for workflow bundle "
            "directories (default: current directory)"
        ),
    )
    p.add_argument(
        "--runs",
        default="runs",
        help="Directory scanned for run directories (default: runs/)",
    )
    p.add_argument(
        "--skills",
        default=None,
        help=(
            "Extra directory scanned for skill libraries (skills.json); the "
            "bundles directory is always scanned too"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=7863,
        help="Port on 127.0.0.1 (the bind address is loopback-only, always)",
    )
    p.add_argument(
        "--allow-actions",
        action="store_true",
        help=(
            "Enable executing governed actions (approve / resume / certify / "
            "promote / rollback) from the UI after an explicit confirm that "
            "shows the exact command. Default: read-only — the UI renders the "
            "command for the operator to copy instead"
        ),
    )
    p.add_argument(
        "--attend",
        action="store_true",
        help=(
            "Open the authenticated local Needs Attention queue first. It is "
            "read-only unless --allow-actions is also supplied; attended "
            "mutations additionally require an engine-issued pause capability "
            "and qualified deployment service"
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help=(
            "Live browser URL for attended verification/continuation. Required "
            "for --backend web unless backend.url is set in --config."
        ),
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Keep the attended browser visible for the operator. The web "
            "attended-action path requires a headed session."
        ),
    )
    p.add_argument(
        "--allow-model-grounding",
        action="store_true",
        help=(
            "EGRESS OPT-IN: permit configured off-box model components during "
            "fresh attended verification and deterministic continuation"
        ),
    )
    p.add_argument(
        "--remote-decisions",
        action="store_true",
        help=(
            "Publish open attended pauses to the hosted decision surface over "
            "an OUTBOUND-ONLY connection, so a paired phone can answer them "
            "from anywhere without any inbound port, certificate, or reverse "
            "proxy. Requires --attend --allow-actions, a --config whose "
            "human_decisions.remote is enabled with an exact tenant and runner, "
            "and OPENADAPT_RUNNER_TOKEN. Only the PHI-free signed task and the "
            "closed halt context cross the wire; screenshots never leave"
        ),
    )
    p.add_argument(
        "--remote-decision-host",
        default=None,
        metavar="URL",
        help=(
            "Hosted control-plane origin for --remote-decisions "
            "(default: the configured hosted host, https://app.openadapt.ai)"
        ),
    )
    _add_backend_flags(p)
    _add_deployment_flags(p)
    p.set_defaults(func=_cmd_console)

    p = sub.add_parser(
        "business-decisions",
        help="Customer-runner typed-decision relay; it never resumes or acts.",
    )
    bdsub = p.add_subparsers(dest="business_decisions_cmd", required=True)
    ps = bdsub.add_parser("serve", help="Serve the outbound-only non-actuating relay")
    ps.add_argument("--runs", required=True, help="Customer-local durable run root")
    ps.add_argument(
        "--runner-config", default=None, help="Operator-authored runner.toml"
    )
    ps.add_argument(
        "--profile", required=True, help="Named deployment profile from runner.toml"
    )
    ps.add_argument(
        "--cloud-origin",
        default=None,
        metavar="URL",
        help="Outbound HTTPS control-plane origin",
    )
    ps.add_argument(
        "--poll-wait-seconds",
        type=float,
        default=25.0,
        help="Cloud long-poll wait (0 through 25; default: 25)",
    )
    ps.add_argument("--once", action="store_true", help="Run one relay cycle and exit")
    ps.set_defaults(func=_cmd_business_decisions_serve)

    # --- connector: the BYOC (bring-your-own-cloud) outbound-pull daemon -------
    p = sub.add_parser(
        "connector",
        help=(
            "BYOC connector: enroll this machine with OpenAdapt Cloud and run "
            "governed jobs LOCALLY (customer owns the data boundary; PHI stays "
            "on this side)"
        ),
    )
    csub = p.add_subparsers(dest="connector_cmd", required=True)

    pe = csub.add_parser(
        "enroll",
        help="Enroll against a mock/development control plane and persist its token",
    )
    pe.add_argument(
        "--control-plane-url", help="Control plane base URL (or CONTROL_PLANE_URL)"
    )
    pe.add_argument(
        "--enrollment-secret", help="Org enrollment secret (or BYOC_ENROLLMENT_SECRET)"
    )
    pe.add_argument("--org-id", help="Org id to enroll under (or BYOC_ORG_ID)")
    pe.add_argument("--name", help="Connector name (or BYOC_CONNECTOR_NAME)")
    pe.add_argument(
        "--profile", help="Path to the deployment.yaml governed runs use as --config"
    )
    pe.add_argument(
        "--policy", help="Pinned admitted policy name (else the deployment config's)"
    )
    pe.add_argument("--storage-backend", help="Customer storage backend (local)")
    pe.add_argument(
        "--storage-root",
        help="Customer storage root dir (a full-disk-encrypted volume)",
    )
    pe.set_defaults(func=_cmd_connector)

    pr = csub.add_parser(
        "run",
        help="Poll the control plane and run governed jobs until interrupted",
    )
    pr.add_argument(
        "--control-plane-url", help="Control plane base URL (or CONTROL_PLANE_URL)"
    )
    pr.add_argument("--token", help="Per-connector token (or BYOC_CONNECTOR_TOKEN)")
    pr.add_argument("--org-id", help="Org id (or BYOC_ORG_ID)")
    pr.add_argument(
        "--profile", help="Path to the deployment.yaml governed runs use as --config"
    )
    pr.add_argument(
        "--policy", help="Pinned admitted policy name (else the deployment config's)"
    )
    pr.add_argument("--storage-backend", help="Customer storage backend (local)")
    pr.add_argument("--storage-root", help="Customer storage root dir")
    pr.add_argument(
        "--poll-wait", type=int, help="Long-poll wait seconds (or BYOC_POLL_WAIT_S)"
    )
    pr.add_argument(
        "--once", action="store_true", help="Poll once then exit (cron-style)"
    )
    pr.set_defaults(func=_cmd_connector)

    return parser


@contextmanager
def _attended_service_from_args(args: argparse.Namespace) -> Iterator[Any]:
    """Resolve CLI overrides into the public persistent attended service."""
    if not (args.attend and args.allow_actions):
        yield None
        return
    if not (getattr(args, "config", None) or getattr(args, "backend", None)):
        raise SystemExit(
            "console --attend --allow-actions requires an explicit --config "
            "or --backend target; refusing to attach mutations to a demo or "
            "implicit default"
        )

    from openadapt_flow.backends.factory import _normalize_kind
    from openadapt_flow.runtime.durable.attended_service import (
        AttendedActionService,
    )

    try:
        cfg, effects_cfg, actuation_cfg = _deployment_sections(args)
        backend_cfg = _resolve_backend_config(args, cfg)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    allow_egress = bool(
        cfg.runtime.allow_model_grounding
        or getattr(args, "allow_model_grounding", False)
    )

    if _normalize_kind(backend_cfg.kind) == "web":
        target_url = getattr(args, "url", None) or backend_cfg.url
        if not target_url:
            raise SystemExit(
                "attended web actions require --url or backend.url in --config"
            )
        headed = bool(getattr(args, "headed", False) or backend_cfg.headed)
        if not headed:
            raise SystemExit(
                "attended web actions require a visible live session; pass "
                "--headed or set backend.headed: true"
            )
        backend_cfg = backend_cfg.model_copy(
            update={"url": target_url, "headed": headed}
        )

    deployment = cfg.model_copy(
        update={
            "backend": backend_cfg,
            "effects": effects_cfg,
            "actuation": actuation_cfg,
            "runtime": cfg.runtime.model_copy(
                update={"allow_model_grounding": allow_egress}
            ),
        }
    )
    try:
        with AttendedActionService(deployment) as service:
            yield service
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _decision_supervisor_from_args(
    args: argparse.Namespace, attended_service: Any
) -> Any:
    """Build the outbound decision lane, or fail loudly. Never returns silently.

    Every refusal here is a ``SystemExit`` rather than a disabled feature. An
    operator who asked for ``--remote-decisions`` and got a loopback-only
    console instead would believe a phone can answer a halt when nothing is
    listening for one, which is the exact "looks like it works" failure this
    lane must not have.
    """
    if not getattr(args, "remote_decisions", False):
        return None
    if not (args.attend and args.allow_actions):
        raise SystemExit(
            "--remote-decisions requires --attend --allow-actions: a remote "
            "answer is executed through the same governed attended path as a "
            "local one, and that path is not available in a read-only console"
        )
    if attended_service is None:  # pragma: no cover - guarded by the check above
        raise SystemExit("--remote-decisions requires an attended action service")

    from openadapt_flow.console.decision_relay import (
        DecisionRelay,
        HttpxRelayTransport,
        RelayRefused,
        resolve_runner_token,
    )
    from openadapt_flow.console.decision_supervisor import (
        DecisionSupervisor,
        DecisionSupervisorThread,
    )
    from openadapt_flow.hosted import HostedError, resolve_host

    try:
        cfg, _effects_cfg, _actuation_cfg = _deployment_sections(args)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    remote = cfg.human_decisions.remote
    if not remote.enabled:
        raise SystemExit(
            "--remote-decisions requires human_decisions.remote.enabled in "
            "--config, with the exact tenant_id and runner_id the control "
            "plane issued for this machine"
        )
    try:
        token = resolve_runner_token()
        origin = resolve_host(getattr(args, "remote_decision_host", None))
        relay = DecisionRelay(
            HttpxRelayTransport(origin, token), token=token, deployment=cfg
        )
    except (RelayRefused, HostedError) as exc:
        raise SystemExit(str(exc)) from exc
    supervisor = DecisionSupervisor(
        args.runs, relay=relay, deployment=cfg, executor=attended_service
    )
    print(
        f"  remote decisions: outbound-only to {origin} "
        f"(tier {remote.context_tier}; no inbound port, no certificate)"
    )
    return DecisionSupervisorThread(supervisor)


def _cmd_console(args: argparse.Namespace) -> int:
    # find_spec first so "extra not installed" is distinguishable from "the
    # console package itself is broken" (a wiring bug must surface, not hide
    # behind an install hint).
    from importlib.util import find_spec

    missing = [m for m in ("fastapi", "uvicorn") if find_spec(m) is None]
    if missing:
        raise SystemExit(
            f"the operator console needs {', '.join(missing)} — install the "
            "console extra:  pip install 'openadapt-flow[console]'"
        )
    from openadapt_flow.console.server import LOOPBACK_HOST, serve

    allow_actions = args.allow_actions
    mode = "ACTIONS ENABLED (confirm-gated)" if allow_actions else "read-only"
    if args.attend:
        mode += "; attention-first"
    print(
        f"operator console on http://{LOOPBACK_HOST}:{args.port}  [{mode}]\n"
        f"  bundles: {Path(args.bundles).resolve()}\n"
        f"  runs:    {Path(args.runs).resolve()}"
    )
    with _attended_service_from_args(args) as attended_service:
        serve(
            args.bundles,
            args.runs,
            args.skills,
            allow_actions=allow_actions,
            attend=args.attend,
            attended_service=attended_service,
            decision_supervisor=_decision_supervisor_from_args(args, attended_service),
            port=args.port,
        )
    return 0


def _connector_flags(args: argparse.Namespace) -> dict[str, object]:
    """Collect the connector CLI flags into the settings-resolution dict."""
    keys = (
        "control_plane_url",
        "token",
        "org_id",
        "name",
        "profile",
        "policy",
        "storage_backend",
        "storage_root",
        "poll_wait",
    )
    return {
        k: getattr(args, k, None) for k in keys if getattr(args, k, None) is not None
    }


def _cmd_connector(args: argparse.Namespace) -> int:
    """The BYOC connector: enroll this machine, or run governed jobs locally.

    Both sub-verbs are thin wrappers over :mod:`openadapt_flow.connector`. The
    engine ships the connector so a dispatched run is the SAME fail-closed
    ``openadapt-flow run`` admission gate + governed Replayer the local CLI uses,
    executed inside the customer perimeter against the customer's own storage.
    """
    import os

    from openadapt_flow.connector import (
        ConnectorClient,
        ConnectorClientError,
        load_settings,
        run_loop,
        save_enrollment,
    )
    from openadapt_flow.connector.config import ConnectorConfigError
    from openadapt_flow.connector.executor import _subprocess_runner

    verb = getattr(args, "connector_cmd", None)
    try:
        settings = load_settings(_connector_flags(args))
    except ConnectorConfigError as exc:
        print(f"connector: {exc}")
        return 2

    if verb == "enroll":
        enrollment_secret = getattr(args, "enrollment_secret", None) or os.environ.get(
            "BYOC_ENROLLMENT_SECRET"
        )
        client = ConnectorClient(settings.control_plane_url)
        try:
            data = client.enroll(
                enrollment_secret=enrollment_secret,
                org_id=settings.org_id,
                name=settings.name,
            )
        except ConnectorClientError as exc:
            print(f"connector enroll failed: {exc}")
            return 1
        finally:
            client.close()
        settings.token = data.get("token") or settings.token
        settings.org_id = data.get("org_id") or settings.org_id
        path = save_enrollment(settings)
        print(
            f"enrolled connector {data.get('connector_id', '?')} for org "
            f"{settings.org_id}; token persisted to {path} (0600)."
        )
        return 0

    # verb == "run"
    if not settings.token:
        print(
            "connector run: no token. Create an organization connector in "
            "OpenAdapt Cloud → Settings, then pass --token or set "
            "BYOC_CONNECTOR_TOKEN."
        )
        return 2
    client = ConnectorClient(settings.control_plane_url, token=settings.token)
    try:
        return run_loop(
            client,
            settings,
            runner=_subprocess_runner,
            once=bool(getattr(args, "once", False)),
        )
    finally:
        client.close()


def _cmd_business_decisions_serve(args: argparse.Namespace) -> int:
    """Run typed-decision transport only; no workflow can continue here."""

    from openadapt_flow.interop.decision_relay_transport import RelayRefused
    from openadapt_flow.runner.business_decision_service import (
        BusinessDecisionServiceLoop,
        build_business_decision_supervisor,
        resolve_business_decision_origin,
    )
    from openadapt_flow.runner.config import RunnerConfigError, load_runner_config

    try:
        config = load_runner_config(
            Path(args.runner_config) if args.runner_config else None
        )
        origin = resolve_business_decision_origin(args.cloud_origin)
        supervisor = build_business_decision_supervisor(
            runs_root=Path(args.runs),
            runner_config=config,
            profile=args.profile,
            origin=origin,
        )
        loop = BusinessDecisionServiceLoop(supervisor, wait_s=args.poll_wait_seconds)
    except (RelayRefused, RunnerConfigError, OSError, ValueError) as exc:
        print(f"business decisions: {exc}", file=sys.stderr)
        return 2
    if args.once:
        health = loop.serve_once()
        print(health.as_json())
        return 0 if health.state == "ready" else 1
    loop.run(lambda health: print(health.as_json(), flush=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    from openadapt_flow._browser_setup import BrowserSupportMissing

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrowserSupportMissing as exc:
        # Missing optional browser support is a setup decision, not a crash.
        print(f"Browser setup required:\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
