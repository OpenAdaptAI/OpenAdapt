"""Surface-selection neutrality for the CLI (roadmap Section 5).

The browser is one execution surface among six, not a privileged default.
Production execution profiles (Standard / Regulated) require the operator to
select the surface explicitly. The Demo / permissive posture may omit
``--backend``: ``record`` then captures on this OS (macos / windows / linux)
unless ``--url`` selected the browser, and it must say so visibly.

This module owns three small, deliberately CLI-local concerns:

- the closed surface vocabulary and the refusal / notice texts;
- the surface binding check: a workflow recorded or qualified on one surface
  refuses to run on another without an explicit, report-recorded override;
- the operator's last-used target, persisted as a CLI convenience default
  ONLY for the Demo profile, in a per-user state file. It is never written
  into a workflow bundle: a workflow's surface is a qualification property,
  not a preference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, cast

from openadapt_flow.ir import ExecutionMode, ExecutionTargetKind

#: The closed set of execution surfaces the CLI can drive, in doc order.
SURFACES: tuple[ExecutionTargetKind, ...] = (
    "web",
    "windows",
    "macos",
    "linux",
    "rdp",
    "citrix",
)

SURFACE_LIST_TEXT = "web (browser), windows, macos, linux, rdp, citrix"

#: Native desktop surfaces ``record`` can default to on this OS.
NATIVE_SURFACES: tuple[ExecutionTargetKind, ...] = ("windows", "macos", "linux")

#: Environment variable overriding the CLI state file path (tests, CI).
CLI_STATE_ENV = "OPENADAPT_FLOW_CLI_STATE"


def native_surface_for_this_os(
    platform: Optional[str] = None,
) -> ExecutionTargetKind:
    """Return the native capture surface for this OS.

    ``record`` with no ``--backend`` and no ``--url`` uses this surface.
    Unknown platforms have no implicit default; pass ``--backend`` explicitly.
    """
    plat = sys.platform if platform is None else platform
    if plat == "darwin":
        return "macos"
    if plat == "win32":
        return "windows"
    if plat == "linux" or plat.startswith("linux"):
        return "linux"
    raise ValueError(
        f"no native capture surface for platform {plat!r}; pass --backend "
        f"with one of: {SURFACE_LIST_TEXT}"
    )


def implicit_record_surface(
    *,
    url: Optional[str],
    last_used: Optional[ExecutionTargetKind] = None,
    platform: Optional[str] = None,
) -> tuple[ExecutionTargetKind, bool]:
    """Return ``(surface, from_last_used)`` when ``record`` omitted ``--backend``.

    ``--url`` selects the browser. Otherwise capture on this OS. A Demo
    last-used target applies only when it is this OS; a last-used browser
    (or another desktop) does not override the this-OS default.
    """
    if url:
        return "web", False
    this_os = native_surface_for_this_os(platform)
    if last_used in NATIVE_SURFACES and last_used == this_os:
        return last_used, True
    return this_os, False


def execution_mode_for_surface(surface: ExecutionTargetKind) -> ExecutionMode:
    """Return the execution mode a surface implies.

    ``external`` drives a LOCAL client window of a remote session via
    pixels/keyboard/mouse (zero install inside the remote session);
    ``in_session`` runs inside the session it automates, with the
    accessibility/structured layer available where the platform provides one.
    The Windows surface is ``in_session`` even for a remote guest: the WAA
    agent runs inside that session. The mode is fixed by explicit capability
    negotiation at qualification, never silently switched at run time.
    """
    return "external" if surface in ("rdp", "citrix") else "in_session"


def explicit_surface_refusal(operation: str, profile: str) -> str:
    """The refusal printed when a production profile has no explicit surface."""
    consequence = "recorded" if operation == "record" else "executed"
    return (
        f"{operation} REFUSED: the {profile} profile requires an explicit "
        f"execution surface; there is no implicit browser default in "
        f"production. Pass --backend with one of: {SURFACE_LIST_TEXT} "
        f"(or set backend.kind in --config). Each surface has an equivalent "
        f"first-workflow path; see docs/SURFACES.md. Nothing was "
        f"{consequence}."
    )


def demo_default_notice(surface: ExecutionTargetKind, *, from_last_used: bool) -> str:
    """The visible notice printed when a permissive posture defaults a surface."""
    if from_last_used:
        return (
            f"NOTE: defaulting to backend '{surface}' (demo convenience: your "
            f"last-used target). Pass --backend to choose explicitly; "
            f"surfaces: {SURFACE_LIST_TEXT}."
        )
    if surface == "web":
        return (
            "NOTE: defaulting to browser (demo convenience). Pass --backend to "
            f"choose the surface explicitly; surfaces: {SURFACE_LIST_TEXT}."
        )
    return (
        f"NOTE: defaulting to capture on this OS ({surface}) "
        f"(demo convenience). Pass --backend to choose the surface "
        f"explicitly; surfaces: {SURFACE_LIST_TEXT}."
    )


def surface_mismatch_refusal(
    operation: str,
    *,
    recorded: ExecutionTargetKind,
    requested: ExecutionTargetKind,
    execution_mode: Optional[str],
) -> str:
    """The refusal printed when a run targets a different surface than bound."""
    mode = f", execution mode '{execution_mode}'" if execution_mode else ""
    return (
        f"{operation} REFUSED: this workflow is bound to surface "
        f"'{recorded}'{mode}, but the resolved backend targets "
        f"'{requested}'. A workflow qualified on one surface must not "
        f"silently run on another. Re-record or re-qualify it on "
        f"'{requested}', or pass --allow-surface-override to proceed; the "
        f"override is recorded in the run report (surface_override) as "
        f"compatibility evidence. Nothing was executed."
    )


def surface_override_notice(
    recorded: ExecutionTargetKind, requested: ExecutionTargetKind
) -> str:
    """The visible notice printed when an operator overrides the bound surface."""
    return (
        f"NOTE: surface override in effect: workflow bound to '{recorded}' is "
        f"executing on '{requested}'. This run's report records "
        f"surface_override=true as compatibility evidence."
    )


def _cli_state_path() -> Path:
    """The per-user CLI state file (never part of any workflow bundle)."""
    override = os.environ.get(CLI_STATE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".openadapt" / "flow_cli.json"


def load_last_surface() -> Optional[ExecutionTargetKind]:
    """Return the persisted last-used surface, or None when absent/invalid."""
    path = _cli_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("last_backend") if isinstance(data, dict) else None
    if value in SURFACES:
        return cast(ExecutionTargetKind, value)
    return None


def store_last_surface(surface: ExecutionTargetKind) -> None:
    """Persist the last-used surface as a Demo-profile CLI convenience.

    Best-effort: a read-only home directory never breaks the actual run.
    """
    path = _cli_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, object] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            pass
        existing["last_backend"] = surface
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def surface_target_origin(backend: object) -> Optional[str]:
    """The app a non-web backend section names, as ``<kind>://<target>``.

    ``backend`` is a :class:`~openadapt_flow.deployment.BackendConfig`. The
    target is the Windows agent's host and port, the macOS or Linux app name,
    the RDP host (and a non-default port) or the local client window's owner.
    Window titles and credentials are left out: a title can carry a patient
    or document name, and a URL's userinfo is a secret. ``None`` for ``web``,
    which is named by its browser origin instead.
    """
    from urllib.parse import quote, urlsplit

    kind = str(getattr(backend, "kind", None) or "web").strip().lower()
    kind = kind.replace("_", "-")
    kind = "rdp" if kind == "remote-display" else kind
    if kind == "web":
        return None
    if kind == "windows":
        parts = urlsplit(str(getattr(backend, "agent_url", "") or ""))
        if parts.username or parts.password:
            raise ValueError(
                "backend.agent_url must not carry credentials; use agent_token"
            )
        target = parts.hostname or ""
        if parts.port:
            target += f":{parts.port}"
    elif kind == "macos":
        target = str(getattr(backend, "macos_app", "") or "")
    elif kind == "linux":
        target = str(getattr(backend, "linux_app", "") or "")
    elif kind == "rdp" and getattr(backend, "rdp_host", None):
        port = int(getattr(backend, "rdp_port", None) or 3389)
        target = str(backend.rdp_host) + ("" if port == 3389 else f":{port}")  # type: ignore[attr-defined]
    else:
        target = str(getattr(backend, "rdp_window", None) or "workspace")
    return f"{kind}://{quote(target.lower(), safe='.:-_ ')}"


def report_backend_kind(kind: object) -> "ExecutionTargetKind":
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


def resolve_backend_config(args: argparse.Namespace, cfg, workflow=None):
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
        selected_kind is None or report_backend_kind(selected_kind) == hints.backend
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
    if getattr(args, "browser_cdp_endpoint", None):
        backend = backend.model_copy(
            update={"browser_cdp_endpoint": args.browser_cdp_endpoint}
        )
    if getattr(args, "browser_page_url", None):
        backend = backend.model_copy(update={"browser_page_url": args.browser_page_url})
    return backend
