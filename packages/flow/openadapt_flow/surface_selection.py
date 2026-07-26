"""Surface-selection neutrality for the CLI (roadmap Section 5).

The browser is one execution surface among six, not a privileged default.
Production execution profiles (Standard / Regulated) require the operator to
select the surface explicitly; only the Demo / permissive posture may fall
back to the browser, and then it must say so visibly.

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

import json
import os
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

#: Environment variable overriding the CLI state file path (tests, CI).
CLI_STATE_ENV = "OPENADAPT_FLOW_CLI_STATE"


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
    return (
        "NOTE: defaulting to browser (demo convenience). Pass --backend to "
        f"choose the surface explicitly; surfaces: {SURFACE_LIST_TEXT}."
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
        data = json.loads(path.read_text())
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
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            pass
        existing["last_backend"] = surface
        path.write_text(json.dumps(existing, indent=2) + "\n")
    except OSError:
        return
