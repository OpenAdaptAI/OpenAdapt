"""``openadapt-flow wrap-dispatch``: run a wrapped task's admitted program.

    openadapt-flow wrap-dispatch --task TASK --request-id ID --param NAME=VALUE
    openadapt-flow wrap-dispatch --task TASK --request-id ID --param NAME=VALUE \\
        --fallback agent --fallback-max-cost-usd 0.50
    openadapt-flow wrap-dispatch status TASK
    openadapt-flow wrap-dispatch release TASK --person NAME
    openadapt-flow wrap-dispatch evict TASK

A run uses only the task's admitted program. With no admitted program it
reports ``NO_ADMITTED_PROGRAM`` and runs nothing (``openadapt wrap`` makes the
first run). When the program stops, the task is held, as ``openadapt wrap``
holds it, unless ``--fallback agent`` is given: then
:class:`~openadapt_flow.wrap.fallback.FallbackPolicy` may hand a proved
pre-effect drift to the agent behind a pre-commit gate at this terminal.

Each run prints one result JSON with ``openadapt wrap``'s per-run fields
(``path``, ``status``, ``verified``, ``model_calls``, ``cost_usd``,
``seconds`` ...), where ``path`` is ``program``, ``agent_fallback`` or
``held``, plus ``outcome``: ``VERIFIED``, ``ESCALATED``, ``CAUGHT_ERROR``
or ``FAILED``. See docs/WRAP_FALLBACK.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

#: The program, or the agent, needs a person to look before the task goes on.
_ESCALATED = frozenset(
    {
        "CACHE_HALTED",
        "HALTED",
        "NO_ADMITTED_PROGRAM",
        "RECONCILIATION_REQUIRED",
        "REQUEST_ID_CONFLICT",
        "GATE_REFUSED",
        "FALLBACK_GATE_UNATTENDED",
        "FALLBACK_NO_AGENT",
        "FALLBACK_COST_UNBOUNDED",
        "FALLBACK_REQUEST_REUSED",
        "refused_by_hold",
    }
)
#: A check stopped something wrong from passing as done.
_CAUGHT = frozenset(
    {
        "RECORD_CHECK_FAILED",
        "RECEIPT_NOT_VERIFIED",
        "MODEL_CALLS_NOT_ZERO",
        "COST_CAP_EXCEEDED",
        "GATE_NOT_PROVED",
        "BASELINE_UNAVAILABLE",
        "refused_wrong_record",
        "readback_wrong_record",
        "readback_refused",
        "record_check_failed",
    }
)
_HOLD_VERBS = ("status", "release", "evict")


def outcome(result: Mapping[str, Any]) -> str:
    """The run's accounting outcome, from its status alone."""
    if result.get("verified") is True:
        return "VERIFIED"
    status = str(result.get("status"))
    if status in _ESCALATED:
        return "ESCALATED"
    if status in _CAUGHT:
        return "CAUGHT_ERROR"
    return "FAILED"


def dispatch_path(result: Mapping[str, Any]) -> str:
    """``program``, ``agent_fallback`` or ``held`` for one run's result."""
    from openadapt_flow.wrap.fallback import FALLBACK_PATH

    if result.get("path") == FALLBACK_PATH:
        return FALLBACK_PATH
    if result.get("status") in ("CACHE_HALTED", "NO_ADMITTED_PROGRAM"):
        return "held"
    return "program"


def finish(
    result: dict[str, Any], fallback: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Add the dispatch fields to a WrappedTask per-run result."""
    if "outcome" in result and "fallback" in result:
        # Already a dispatch envelope (stored, then replayed on a retry).
        return dict(result)
    out = dict(result)
    out["path"] = dispatch_path(result)
    out["outcome"] = outcome(result)
    out["fallback"] = dict(fallback) if fallback is not None else None
    return out


# -- parser ------------------------------------------------------------------------


def _add_task(p: argparse.ArgumentParser) -> None:
    p.add_argument("task", help="Generated task and read-back configuration")
    p.add_argument("--key-dir", help="Existing admission key directory")


def register(sub: argparse._SubParsersAction) -> None:
    """Add ``wrap-dispatch`` to ``openadapt-flow``."""
    p = sub.add_parser(
        "wrap-dispatch",
        help=(
            "Run a wrapped task's admitted program; on a proved pre-effect "
            "drift, optionally hand it to the agent behind a pre-commit gate"
        ),
        allow_abbrev=False,
    )
    add_run_arguments(p)
    verbs = p.add_subparsers(dest="dispatch_verb")
    q = verbs.add_parser(
        "status", help="Show the task's hold and fallback runs", allow_abbrev=False
    )
    _add_task(q)
    q = verbs.add_parser(
        "release",
        help="A person releases the task's hold (the evidence is kept)",
        allow_abbrev=False,
    )
    _add_task(q)
    q.add_argument("--person", required=True, help="Who checked and releases it")
    q.add_argument(
        "--after-reconciling",
        action="store_true",
        help="Also release a hold or fallback that may have written, after "
        "the person checked the record",
    )
    q = verbs.add_parser(
        "evict",
        help="Set the task's program aside (kept as evidence)",
        allow_abbrev=False,
    )
    _add_task(q)
    p.set_defaults(func=cmd_wrap_dispatch)


def add_run_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--task", help="Generated task and read-back configuration")
    p.add_argument("--request-id", help="This request's ID (reused on a retry)")
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A task input (a sign-in input may come from "
        "OPENADAPT_FLOW_SECRET_<NAME>)",
    )
    p.add_argument("--runs-dir", default="~/.openadapt/wrap/runs")
    p.add_argument("--key-dir", help="Existing admission key directory")
    p.add_argument("--teach-authorization", help="Signed teaching authorization")
    p.add_argument("--approver-root", help="Pinned approver root for it")
    p.add_argument(
        "--fallback",
        choices=("none", "agent"),
        default="none",
        help="On a proved pre-effect drift: hold (none, the default) or hand "
        "the task to the agent behind a pre-commit gate (agent)",
    )
    p.add_argument(
        "--fallback-max-cost-usd",
        type=float,
        help="Model spend cap for the fallback run (required with --fallback agent)",
    )
    p.add_argument(
        "--fallback-request-id",
        help="The fallback run's own ID (default: derived from --request-id)",
    )
    p.add_argument(
        "--fallback-gate",
        choices=("terminal", "item-approval"),
        default="terminal",
        help="Who approves each fallback save: a person at this terminal "
        "(terminal, the default), or a person on a paired phone or with "
        "openadapt-flow item-approval approve, through a signed item "
        "approval (item-approval; no terminal needed)",
    )
    p.add_argument(
        "--fallback-approvals-dir",
        help="With --fallback-gate item-approval: where each fallback's "
        "approval folder goes, such as the runs root the decision portal "
        "scans (default: inside the fallback run's directory)",
    )
    p.add_argument(
        "--fallback-approval-wait-s",
        type=float,
        default=60.0,
        help="With --fallback-gate item-approval: seconds to wait for each "
        "answer (less than --max-run-seconds)",
    )
    p.add_argument(
        "--fallback-approver-root",
        help="With --fallback-gate item-approval: the pinned approver root "
        "(else OPENADAPT_APPROVER_ROOT or --fallback-approvers-config)",
    )
    p.add_argument(
        "--fallback-approvers-config",
        help="With --fallback-gate item-approval: a deployment config whose "
        "approvers section pins the root; it is then the only pin counted",
    )
    p.add_argument("--provider", choices=("claude", "openai"), default="claude")
    p.add_argument("--model")
    p.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="KIND=USD_PER_MTOK",
        help="Explicit input, output, cache_write and cache_read model prices",
    )
    p.add_argument("--price-model", help="Exact model covered by supplied prices")
    p.add_argument("--pricing-source-url", help="Primary source for model prices")
    p.add_argument("--pricing-verified-at", help="Date prices were verified")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--max-run-seconds", type=float, default=110)
    p.add_argument("--request-timeout-seconds", type=float, default=25)
    p.add_argument("--max-input-bytes", type=int)
    p.add_argument("--result", help="Also write the result JSON to this file")


# -- commands ----------------------------------------------------------------------


class _NoAgent:
    """Stands in when no fallback is configured: it never drives anything."""

    requires_model_key = False
    dialect = "generic"
    principal_id = None

    def drive(self, request: Any, harness: Any) -> Any:
        raise RuntimeError("wrap-dispatch has no agent for this run")


def _setup(task: str, key_dir: Optional[str], teach: Any = None) -> Any:
    from openadapt_flow.integrations.agents.recording import load_task

    options: dict[str, Any] = {}
    if key_dir:
        options["key_dir"] = key_dir
    if teach is not None:
        options["teach_authorization"] = teach
    return load_task(task, **options)


def _names(setup: Any) -> dict[str, str]:
    # The cache key depends on parameter names, not their values.
    return dict.fromkeys(setup.parameters, "")


def _emit(result: Mapping[str, Any], path: Optional[str]) -> None:
    text = json.dumps(result, sort_keys=True, default=str)
    if path:
        Path(path).expanduser().write_text(text + "\n", encoding="utf-8")
    print(text)


def _fallback_agent(args: argparse.Namespace, setup: Any) -> Any:
    from openadapt_flow.integrations.agents.providers import build_agent
    from openadapt_flow.jit.keys import load_customer_model_key
    from openadapt_flow.wrap.cli import _prices

    agent = build_agent(
        args.provider,
        model=args.model,
        max_turns=args.max_turns,
        max_cost_usd=args.fallback_max_cost_usd,
        prices=_prices(args.price),
        max_run_seconds=args.max_run_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        max_input_bytes=args.max_input_bytes,
        price_model=args.price_model,
        pricing_source_url=args.pricing_source_url,
        pricing_verified_at=args.pricing_verified_at,
    )
    if getattr(agent, "requires_model_key", True):
        provider = getattr(agent, "model_key_provider", None)
        expected = "anthropic" if args.provider == "claude" else "openai"
        if provider != expected:
            raise SystemExit("provider adapter has no matching model-key provenance")
        setup.cache.model_key = load_customer_model_key(provider)
    return agent


def child_request_id(parent: str) -> str:
    """The fallback request id for a dispatch request id.

    A parent id is any 1 to 128 characters; a fallback id is a plain file
    name. A digest of the parent fits both, and stays the same on a retry.
    """

    digest = hashlib.sha256(f"wrap-dispatch:{parent}".encode("utf-8")).hexdigest()
    return f"fallback-{digest[:40]}"


def item_approval_gate(args: argparse.Namespace) -> Any:
    """The non-terminal gate from the ``--fallback-*`` flags."""

    from openadapt_flow.wrap.approval_gate import ItemApprovalGate, pin_from_config

    try:
        pin = pin_from_config(args.fallback_approvers_config)
    except ValueError as exc:
        raise SystemExit(f"REFUSED (deployment_config_unreadable): {exc}") from None
    key_dir = pin.pop("key_dir", None) or (
        Path(args.key_dir).expanduser() if args.key_dir else None
    )
    return ItemApprovalGate(
        approvals_dir=args.fallback_approvals_dir,
        wait_s=args.fallback_approval_wait_s,
        key_dir=key_dir,
        approver_root=args.fallback_approver_root,
        config_only=bool(args.fallback_approvers_config),
        **pin,
    )


def _task(policy: Any, **options: Any) -> Any:
    """A WrappedTask whose stored result is the whole dispatch envelope."""
    from openadapt_flow.wrap import WrappedTask

    class DispatchTask(WrappedTask):
        def finalize(self, result: dict[str, Any]) -> dict[str, Any]:
            last = getattr(policy, "last", None) if policy is not None else None
            return finish(result, last)

    return DispatchTask(**options)


def run(
    args: argparse.Namespace,
    *,
    agent: Any = None,
    gate: Any = None,
    repair_desk: Any = None,
) -> dict[str, Any]:
    """One dispatch. ``agent``, ``gate`` and ``repair_desk`` are for tests and
    embedding; the CLI builds the provider agent and the gate.

    Only the admitted program runs (:mod:`.compiled_only`): with none, the
    result is ``NO_ADMITTED_PROGRAM`` and nothing runs. The result,
    including ``fallback``, is stored with the request, so a retry of the
    same request id returns it without running anything.
    """
    from openadapt_flow.wrap.cli import _params, _with_secret_env
    from openadapt_flow.wrap.compiled_only import CompiledOnly

    if not args.task or not args.request_id:
        raise SystemExit("wrap-dispatch needs --task and --request-id")
    fallback_on = args.fallback == "agent"
    cap = args.fallback_max_cost_usd
    if fallback_on and (cap is None or not math.isfinite(cap) or cap <= 0):
        raise SystemExit("--fallback agent requires a positive --fallback-max-cost-usd")
    if not fallback_on and (
        cap is not None
        or args.fallback_request_id
        or args.fallback_gate != "terminal"
        or args.fallback_approvals_dir
    ):
        raise SystemExit("the --fallback-* options need --fallback agent")
    if args.fallback_gate == "item-approval" and not (
        0 < args.fallback_approval_wait_s < args.max_run_seconds
    ):
        raise SystemExit(
            "--fallback-approval-wait-s must be more than 0 and less than "
            "--max-run-seconds"
        )
    from openadapt_flow.compile_agent.cli import _teach_authority

    setup = _setup(args.task, args.key_dir, _teach_authority(args))
    names = _names(setup)
    cache: Any = CompiledOnly(setup.cache)
    policy = None
    if fallback_on:
        from openadapt_flow.wrap.fallback import (
            FallbackPolicy,
            TerminalGate,
            fallback_root,
        )

        if repair_desk is None:
            from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk

            root = fallback_root(setup.cache, setup.instruction, names)
            repair_desk = RepairDesk(root / "repairs", RepairAccessPolicy(None))
        if gate is None:
            gate = (
                item_approval_gate(args)
                if args.fallback_gate == "item-approval"
                else TerminalGate()
            )
        given = agent
        policy = FallbackPolicy(
            setup.cache,
            max_cost_usd=cap,
            gate=gate,
            request_id=args.fallback_request_id or child_request_id(args.request_id),
            repair_desk=repair_desk,
            # Built only once a fallback may start: a miss or a hold that
            # may not fall back never makes the agent or loads a model key.
            agent_factory=(lambda: given)
            if given is not None
            else (lambda: _fallback_agent(args, setup)),
            parent_request_id=args.request_id,
        )
        cache = policy
    task = _task(
        policy,
        cache=cache,
        agent=_NoAgent(),
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=args.runs_dir,
    )
    values = _with_secret_env(_params(args.param), task.parameters)
    return finish(task.call(args.request_id, values), None)


def _hold_file(setup: Any) -> Path:
    from openadapt_flow.integrations.agents.skills import _STOP_FILE

    return Path(setup.cache.receipt_root(setup.instruction, _names(setup))) / _STOP_FILE


def status(args: argparse.Namespace) -> dict[str, Any]:
    from openadapt_flow.privacy import scrub_text
    from openadapt_flow.wrap.fallback import fallback_runs, halt_evidence

    setup = _setup(args.task, args.key_dir)
    names = _names(setup)
    cache = setup.cache
    hold: Optional[dict[str, Any]] = None
    path = _hold_file(setup)
    if path.exists() or path.is_symlink():
        try:
            stopped = json.loads(path.read_text(encoding="utf-8"))
            replay = stopped.get("replay") or {}
            reason = replay.get("halt_reason") or stopped.get("reason")
            hold = {
                "phase": stopped.get("phase"),
                "status": replay.get("status"),
                "halt_reason": scrub_text(str(reason)) if reason else None,
                "bundle_digest": stopped.get("bundle_digest"),
            }
        except (OSError, ValueError, AttributeError):
            hold = {"phase": "unreadable"}
    found = halt_evidence(cache, setup.instruction, names)
    return {
        "task_id": cache.task_id(setup.instruction, names),
        "held": cache.held(setup.instruction, names),
        "hold": hold,
        "admitted_program": cache.admitted_program(setup.instruction, names)
        is not None,
        "fallback_eligible": found.eligible,
        "fallback_reason": found.reason,
        "fallback": fallback_runs(cache, setup.instruction, names),
    }


def _hold_is_proved_clean(setup: Any, names: Mapping[str, str]) -> bool:
    """The hold's own evidence shows nothing was written: the same rule the
    fallback uses (:func:`~openadapt_flow.wrap.fallback.halt_evidence`)."""
    from openadapt_flow.wrap.fallback import halt_evidence

    return halt_evidence(setup.cache, setup.instruction, names).eligible


def release(args: argparse.Namespace, ask: Any = input) -> dict[str, Any]:
    """A person releases a hold at a terminal; the hold file is archived.

    Without ``--after-reconciling`` only a hold whose evidence proves nothing
    was written is released (see :func:`_hold_is_proved_clean`); a
    ``RECONCILIATION_REQUIRED`` reply, a write, a ``running`` hold or an
    unfinished fallback needs the person to check the record first. A
    fallback whose owner is still running (or can't be checked from here)
    is never released.
    """
    from openadapt_flow.compile_agent.cli import _interactive
    from openadapt_flow.wrap.fallback import fallback_runs, release_in_flight

    person = (args.person or "").strip()
    if not person:
        raise SystemExit("release needs --person")
    if not _interactive():
        raise SystemExit("release needs a person at an interactive terminal")
    setup = _setup(args.task, args.key_dir)
    names = _names(setup)
    path = _hold_file(setup)
    runs = fallback_runs(setup.cache, setup.instruction, names)
    stuck = runs["in_flight"]
    if stuck and runs["in_flight_owner"] != "ended":
        return {
            "released": False,
            "reason": "FALLBACK_RUNNING",
            "detail": "A fallback of this task is still running, or its owner "
            "can't be checked from here; it may still save.",
            "fallback_request_id": runs["in_flight_request_id"],
        }
    phase = None
    if path.exists() or path.is_symlink():
        try:
            phase = json.loads(path.read_text(encoding="utf-8")).get("phase")
        except (OSError, ValueError, AttributeError):
            phase = "unreadable"
    elif not stuck:
        return {"released": False, "reason": "NOT_HELD"}
    clean = phase is None or _hold_is_proved_clean(setup, names)
    if (stuck or not clean) and not args.after_reconciling:
        return {
            "released": False,
            "reason": "RECONCILE_FIRST",
            "detail": "A write may exist. Check the record, then pass "
            "--after-reconciling.",
        }
    if ask("Type release to release this hold: ").strip().lower() != "release":
        return {"released": False, "reason": "NOT_CONFIRMED"}
    cleared = None
    if stuck:
        cleared = release_in_flight(setup.cache, setup.instruction, names)
        if not cleared["cleared"]:
            # Its owner came back to life, or it changed: keep everything.
            return {
                "released": False,
                "reason": "FALLBACK_RUNNING",
                "fallback_request_id": cleared.get("request_id"),
            }
    archived = None
    if phase is not None:
        archived = path.with_name(f"skill-stop-released-{time.time_ns()}.json")
        path.rename(archived)
        from openadapt_flow.wrap.fallback import _write

        _write(
            archived.with_suffix(".by.json"),
            {
                "person": person,
                "at": time.time(),
                "phase": phase,
                "after_reconciling": bool(args.after_reconciling),
            },
        )
    return {
        "released": True,
        "person": person,
        "archived": str(archived) if archived else None,
        "fallback_cleared": bool(cleared and cleared["cleared"]),
        "fallback_request_id": cleared.get("request_id") if cleared else None,
    }


def evict(args: argparse.Namespace) -> dict[str, Any]:
    setup = _setup(args.task, args.key_dir)
    kept = setup.cache.evict(setup.instruction, _names(setup))
    return {"evicted": kept is not None, "kept_at": str(kept) if kept else None}


def cmd_wrap_dispatch(args: argparse.Namespace) -> int:
    from openadapt_flow.integrations.agents.recording import TaskSetupError
    from openadapt_flow.wrap import TaskArgumentsError

    verb = getattr(args, "dispatch_verb", None)
    try:
        if verb == "status":
            _emit(status(args), None)
            return 0
        if verb == "release":
            result = release(args)
            _emit(result, None)
            return 0 if result.get("released") else 1
        if verb == "evict":
            _emit(evict(args), None)
            return 0
        result = run(args)
    except (TaskArgumentsError, TaskSetupError) as exc:
        print(f"openadapt-flow wrap-dispatch: {exc}", file=sys.stderr)
        return 2
    _emit(result, args.result)
    return 0 if result.get("verified") is True else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run as ``python -m openadapt_flow.wrap.dispatch_cli``."""
    parser = argparse.ArgumentParser(prog="openadapt-flow")
    register(parser.add_subparsers(dest="command", required=True))
    rows = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(["wrap-dispatch", *rows])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
