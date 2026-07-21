"""Governance actions for the console: EXISTING verbs only, shown-then-run.

Every action here is one of the engine's existing entry points -- the
``openadapt-flow`` CLI verbs (``approve`` / ``resume`` / ``certify`` /
``teach``) or the skill library's governed ``promote`` / ``quarantine``
(:class:`openadapt_flow.learning.library.SkillLibrary`). The console adds NO
new engine semantics: it renders the exact command an action will run, and --
only when the server was started with ``--allow-actions`` -- executes that
same command (a CLI subprocess, or the same library call the CLI path uses).

``teach`` is deliberately RENDER-ONLY: it needs a fix demonstration the
operator must record, so the console shows the exact command to copy instead
of faking an execution path that cannot exist.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

#: Seconds an executed verb may run before the console gives up on it.
#: ``resume`` relaunches a real backend, so this is generous.
EXECUTE_TIMEOUT_S = 600


class ActionSpec(BaseModel):
    """One operator action: what it is, exactly what it runs, and whether the
    console itself can execute it (vs. copy-to-terminal only)."""

    id: str
    verb: str
    title: str
    description: str
    command: str = Field(description="Exact shell command this action runs")
    mutating: bool = True
    executable: bool = Field(
        description=(
            "True when the console can run this itself (an existing CLI verb "
            "or library call with no interactive input); False => the operator "
            "copies the command"
        )
    )
    placeholders: list[str] = Field(
        default_factory=list,
        description="<angle-bracket> arguments the operator must fill in",
    )


def _cli(*args: str) -> str:
    return shlex.join(["openadapt-flow", *args])


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------


def actions_for_run(
    run_dir: Path,
    *,
    halted: bool,
    paused: bool,
    bundle_dir: Optional[str] = None,
    operator_identity: Optional[str] = None,
) -> list[ActionSpec]:
    run = str(run_dir)
    out: list[ActionSpec] = []
    if halted:
        bundle = bundle_dir or "<bundle-dir>"
        out.append(
            ActionSpec(
                id="teach",
                verb="teach",
                title="Teach a fix (halt -> learn)",
                description=(
                    "Resolve this halted run from a fix demonstration: record "
                    "ONLY the corrective actions (or write a .json correction "
                    "spec), then run the command. The correction is induced as "
                    "a guarded exception branch, gated and validated; an "
                    "updated bundle is written ONLY if it passes."
                ),
                command=_cli(
                    "teach",
                    run,
                    "--fix",
                    "<fix-recording-or-spec.json>",
                    "--bundle",
                    bundle,
                    "--out",
                    "<updated-bundle-dir>",
                ),
                mutating=True,
                executable=False,  # needs a fix demonstration the console
                # cannot record for the operator
                placeholders=["<fix-recording-or-spec.json>", "<updated-bundle-dir>"]
                + ([] if bundle_dir else ["<bundle-dir>"]),
            )
        )
    if paused:
        approver = (operator_identity or "").strip()
        out.append(
            ActionSpec(
                id="approve",
                verb="approve",
                title="Approve resume",
                description=(
                    "Record approval attributed to the local OS account running "
                    "this console, plus the chosen resolution, for this "
                    "durably-paused run. Written to approval.json in the run dir."
                ),
                command=_cli(
                    "approve",
                    run,
                    *(["--approver", approver] if approver else []),
                ),
                mutating=True,
                executable=bool(approver),
            )
        )
        out.append(
            ActionSpec(
                id="resume",
                verb="resume",
                title="Resume from last verified checkpoint",
                description=(
                    "Resume the paused run from its last verified checkpoint "
                    "-- never re-running an already-confirmed write. Resume "
                    "needs the deployment's backend, effect verifier, and "
                    "run-bound parameters, so the console never executes an "
                    "unbound resume."
                ),
                command=_cli(
                    "resume",
                    run,
                    "--require-approval",
                    "--config",
                    "<deployment.yaml>",
                    "--params-file",
                    "<params.json>",
                ),
                mutating=True,
                executable=False,
                placeholders=["<deployment.yaml>", "<params.json>"],
            )
        )
    return out


def actions_for_bundle(bundle_dir: Path, policy: Optional[str]) -> list[ActionSpec]:
    bundle = str(bundle_dir)
    pol = policy or "clinical-write"
    return [
        ActionSpec(
            id="certify",
            verb="certify",
            title="Certify against policy",
            description=(
                "Enforce a policy on this bundle (exit nonzero + report on "
                "failure) -- makes 'runnable' distinct from 'certified safe'. "
                "Read-only with respect to the bundle."
            ),
            command=_cli("certify", bundle, "--policy", pol),
            mutating=False,
            executable=True,
        ),
        ActionSpec(
            id="run",
            verb="run",
            title="Execute under a deployment config",
            description=(
                "Run the bundle through the fail-closed admission gate under "
                "a deployment config. Needs the deployment environment "
                "(backend, effects verifier, params), so copy it to a "
                "terminal on the runner."
            ),
            command=_cli("run", bundle, "--config", "<deployment.yaml>"),
            mutating=True,
            executable=False,
            placeholders=["<deployment.yaml>"],
        ),
    ]


def actions_for_skill(
    library_file: Path, skill_id: str, version: int
) -> list[ActionSpec]:
    """Promote / roll back one version in a skill library.

    There is no CLI verb for these today; the entry points are the SAME
    library calls ``teach`` uses (``SkillLibrary.promote`` /
    ``SkillLibrary.quarantine``), invoked in-process. The rendered command is
    the equivalent invocation for the audit trail."""
    lib_root = str(library_file.parent)

    def _library_command(method: str, *method_args: str) -> str:
        # Pass every filesystem- and artifact-derived value as a positional
        # argv entry.  Building Python source with an interpolated path inside a
        # shell double-quoted string would make ``$()`` / backticks in a valid
        # directory name execute when the displayed command is pasted.
        source = (
            "import sys; "
            "from openadapt_flow.learning.library import SkillLibrary; "
            f"getattr(SkillLibrary(sys.argv[1]), {method!r})"
            "(*[int(v) if i == 1 else v for i, v in enumerate(sys.argv[2:])])"
        )
        return shlex.join([sys.executable, "-c", source, lib_root, *method_args])

    return [
        ActionSpec(
            id="promote",
            verb="promote",
            title=f"Promote v{version} to active",
            description=(
                "Make this candidate version the ACTIVE one; the prior active "
                "is retired to 'superseded' (never deleted -- full lineage "
                "stays auditable). SkillLibrary.promote, the same entry point "
                "the teach pipeline uses."
            ),
            command=_library_command("promote", skill_id, str(version)),
            mutating=True,
            executable=True,
        ),
        ActionSpec(
            id="rollback",
            verb="rollback",
            title=f"Roll back / quarantine v{version}",
            description=(
                "Quarantine this version (status 'rolled_back') with a "
                "recorded reason -- the library's governed rejection. To "
                "restore an earlier revision afterwards, promote it "
                "explicitly. SkillLibrary.quarantine, the same entry point "
                "the teach pipeline's gate uses."
            ),
            command=_library_command("quarantine", skill_id, str(version), "<reason>"),
            mutating=True,
            executable=True,
            placeholders=["<reason>"],
        ),
    ]


# ---------------------------------------------------------------------------
# execution (only reachable when the server allows actions)
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    action_id: str
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Run an ``openadapt-flow`` verb as the CURRENT interpreter's module CLI
    (same code path as the installed script), never a shell."""
    proc = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", *args],
        capture_output=True,
        text=True,
        timeout=EXECUTE_TIMEOUT_S,
    )
    return proc.returncode, proc.stdout, proc.stderr


def execute_run_action(
    action_id: str,
    run_dir: Path,
    *,
    operator_identity: str,
    resolution: Optional[str] = None,
) -> ExecutionResult:
    """Execute an executable run-scoped verb (``approve`` / ``resume``) via
    the CLI. Anything else raises ``ValueError`` (the API returns 400)."""
    if action_id == "approve":
        if not operator_identity.strip():
            raise ValueError("local operator identity is unavailable")
        args = [
            "approve",
            str(run_dir),
            "--approver",
            operator_identity.strip(),
        ]
        if resolution:
            args += ["--resolution", resolution]
    elif action_id == "resume":
        args = ["resume", str(run_dir), "--require-approval"]
    else:
        raise ValueError(f"not an executable run action: {action_id!r}")
    code, out, err = _run_cli(args)
    return ExecutionResult(
        action_id=action_id,
        command=_cli(*args),
        returncode=code,
        stdout=out,
        stderr=err,
    )


def execute_bundle_action(
    action_id: str, bundle_dir: Path, *, policy: Optional[str] = None
) -> ExecutionResult:
    if action_id != "certify":
        raise ValueError(f"not an executable bundle action: {action_id!r}")
    args = ["certify", str(bundle_dir), "--policy", policy or "clinical-write"]
    code, out, err = _run_cli(args)
    return ExecutionResult(
        action_id=action_id,
        command=_cli(*args),
        returncode=code,
        stdout=out,
        stderr=err,
    )


def execute_skill_action(
    action_id: str,
    library_file: Path,
    skill_id: str,
    version: int,
    *,
    reason: Optional[str] = None,
) -> ExecutionResult:
    """Promote / quarantine via the SAME SkillLibrary entry points the teach
    pipeline uses (no CLI verb exists for these)."""
    from openadapt_flow.learning.library import SkillLibrary

    spec_by_id = {s.id: s for s in actions_for_skill(library_file, skill_id, version)}
    if action_id not in spec_by_id:
        raise ValueError(f"not an executable skill action: {action_id!r}")
    lib = SkillLibrary(library_file.parent)
    try:
        # promote/quarantine persist the library themselves (same as the
        # teach pipeline's calls) -- no extra save here.
        if action_id == "promote":
            lib.promote(skill_id, version)
        else:  # rollback
            lib.quarantine(
                skill_id, version, reason or "rolled back from operator console"
            )
    except (KeyError, ValueError) as e:
        return ExecutionResult(
            action_id=action_id,
            command=spec_by_id[action_id].command,
            returncode=1,
            stderr=str(e),
        )
    return ExecutionResult(
        action_id=action_id,
        command=spec_by_id[action_id].command,
        returncode=0,
        stdout=f"{action_id} ok: skill {skill_id!r} v{version}",
    )


def collect_execution_kwargs(payload: dict[str, Any]) -> dict[str, str]:
    """Whitelist the free-text arguments an execution may carry."""
    out: dict[str, str] = {}
    # Approver identity is deliberately absent: the server derives it from the
    # local OS account and a remote/browser caller cannot override attribution.
    for key in ("resolution", "reason", "policy"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out
