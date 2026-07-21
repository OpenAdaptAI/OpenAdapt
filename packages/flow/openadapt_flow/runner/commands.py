"""Map verified governed dispatches onto the EXISTING flow entry points.

The runner never grows a private execution path: a dispatched run is the same
fail-closed ``openadapt-flow run`` admission gate + shared replayer the local
CLI uses, in a child process (crash isolation; the design doc's "the agent
shells them"). This module only BUILDS argv — executing it belongs to the
future daemon, which is deliberately not in this library (see
``docs/design/RUNNER_CLIENT_LIBRARY.md``).

Verb coverage, honestly stated:

* ``run``    → :func:`build_run_argv` (fully mapped).
* ``resume`` → :func:`build_resume_argv` (durable checkpoint resume; the CLI
  verb exists and is governed — never re-runs a confirmed write).
* ``pause`` / ``approve`` / ``rollback-to-version`` → UNMAPPED
  (:func:`map_control_verb` raises :class:`UnmappedVerbError`). ``pause`` has
  no CLI verb today; ``approve`` requires an authenticated human approval
  that must NOT be minted from a cloud POST body (review S2); and the merged
  queue cannot deliver mid-run control at all — control verbs must ride a
  separate non-leased channel in the contract revision (review E3). Refusing
  is correct until then.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from openadapt_flow.runner.verify import VerifiedDispatch


class UnmappedVerbError(ValueError):
    """A control verb the client cannot honestly execute yet."""


#: Verbs the merged contract could conceivably carry, with their mapping
#: status. Anything absent from this table is refused as unknown.
VERB_STATUS: dict[str, str] = {
    "run": "mapped",
    "resume": "mapped",
    "pause": "unmapped: no governed pause CLI verb exists",
    "approve": "unmapped: approvals must come from a local authenticated human",
    "rollback-to-version": "unmapped: no governed rollback CLI verb exists",
}


def build_run_argv(
    verified: VerifiedDispatch, run_dir: Path, params_file: Optional[Path]
) -> list[str]:
    """The exact governed CLI invocation for a verified ``run`` dispatch.

    Everything security-relevant is pinned from LOCAL material: the bundle
    path and policy come from the operator trust manifest, the deployment
    profile from the local profile map, and ``--pin-digest`` re-binds the
    child's admission gate to the digest the authorization was minted for
    (defense in depth — the child re-runs the whole fail-closed gate
    regardless of what this library already verified).

    ``params_file`` (not raw argv ``--param``) keeps values out of the
    process table; the caller writes it mode-0600 inside the run directory.
    """
    argv = [
        sys.executable,
        "-m",
        "openadapt_flow",
        "run",
        str(verified.bundle.path),
        "--config",
        str(verified.profile_path),
        "--run-dir",
        str(run_dir),
        "--pin-digest",
        verified.payload.authorization.bundle_content_digest,
    ]
    if params_file is not None:
        argv += ["--params-file", str(params_file)]
    if verified.bundle.policy:
        argv += ["--policy", verified.bundle.policy]
    if (
        verified.bundle.allow_unverified_writes
        and verified.payload.authorization.unverified_write_approvals
    ):
        argv.append("--approve-unverified-writes")
    if verified.bundle.allow_unencrypted:
        argv.append("--allow-unencrypted")
    return argv


def build_resume_argv(
    run_dir: Path,
    *,
    params_file: Optional[Path] = None,
    require_approval: bool = True,
) -> list[str]:
    """Governed durable resume of a paused run directory.

    ``require_approval`` defaults ON: a cloud-initiated resume must find a
    locally recorded human approval (``openadapt-flow approve``) — the cloud
    cannot supply one.
    """
    argv = [sys.executable, "-m", "openadapt_flow", "resume", str(run_dir)]
    if require_approval:
        argv.append("--require-approval")
    if params_file is not None:
        argv += ["--params-file", str(params_file)]
    return argv


def map_control_verb(verb: str, run_dir: Path) -> list[str]:
    """argv for a control verb targeting an existing run directory.

    Raises:
        UnmappedVerbError: for every verb without an honest governed mapping
            (including unknown verbs) — the caller reports the refusal.
    """
    status = VERB_STATUS.get(verb)
    if status is None:
        raise UnmappedVerbError(f"unknown control verb {verb!r}")
    if verb == "resume":
        return build_resume_argv(run_dir)
    if verb == "run":
        raise UnmappedVerbError(
            "run is a leased dispatch, not a control verb; use build_run_argv "
            "with a VerifiedDispatch"
        )
    raise UnmappedVerbError(f"control verb {verb!r} is {status}")
