"""``openadapt-flow run`` with repair on: the CLI over :mod:`openadapt_flow.repair.self_repair`.

Repair is the default. When a repair agent is configured (``--repair-agent``
or ``repair.agent`` in the deployment file), every run hands a stop that wrote
nothing to it (:mod:`openadapt_flow.repair.auto_repair`), and each stop goes in
the repair store's ``auto-repair/ledger.jsonl``. ``--no-auto-repair`` (or
``repair.auto: false``) turns it off. ``--self-repair`` asks for it explicitly:
then a run that can't be repaired is refused; in the automatic mode it runs
plainly and says why repair is off for it.

``_cmd_run`` admits the bundle as usual and then hands the run here instead of
to the plain replay. The first run is the ordinary run: the same
authorization, the same approval gate, the same browser setup. Only when it
halts before any effect on a changed screen does the repair agent see
anything. Probes and the resumed run admit the patched bundle through the
same run gate, with a fresh authorization each.

Scope: a browser run that Flow launches against ``--url``. A run bound to a
signed qualification admission, a managed dispatch, a qualification case or a
pinned digest is refused, because each binds the exact original bundle; the
approved bundle needs its own admission first.
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from openadapt_flow.repair.self_repair import AppSession

__all__ = [
    "add_self_repair_flags",
    "cmd_run_self_repair",
    "load_agent",
    "resolve_auto_repair",
]


def add_self_repair_flags(p: argparse.ArgumentParser) -> None:
    """The ``run`` flags that turn on and configure self-repair."""

    group = p.add_argument_group(
        "self-repair",
        "When the run halts before any effect because a screen changed, a "
        "repair agent proposes a fix, Flow probes it up to the save, and one "
        "person approves it before the run resumes. On by default whenever a "
        "repair agent is configured (here or in the deployment file's repair "
        "section). See docs/SELF_REPAIR.md.",
    )
    group.add_argument(
        "--self-repair",
        action="store_true",
        help=(
            "Require self-repair for this run (web runs against --url): refuse "
            "the run when it can't be repaired."
        ),
    )
    group.add_argument(
        "--no-auto-repair",
        action="store_true",
        help="Do not hand stops to the repair agent; a stop waits for a person.",
    )
    group.add_argument(
        "--repair-agent",
        metavar="MODULE:NAME",
        help=(
            "The repair agent: an object, class or factory with dialect, "
            "agent_id and propose(request)."
        ),
    )
    group.add_argument(
        "--repair-screen-access-by",
        metavar="NAME",
        help="The person who allowed the agent to see screens (recorded).",
    )
    group.add_argument(
        "--repair-screen-share-mode",
        choices=["scrubbed_only", "allow_unscrubbed"],
        default=None,
        help="Scrub screens before the agent sees them (default) or not.",
    )
    group.add_argument(
        "--repair-model-provider",
        choices=["anthropic", "openai"],
        help="Load the customer's own key for an agent that calls a model.",
    )
    group.add_argument(
        "--repair-model-key-file",
        metavar="PATH",
        help='The customer\'s key file ({"anthropic": {"api_key": ...}}).',
    )
    group.add_argument(
        "--repair-store",
        default=None,
        help="The repair store the candidate enters (default repair-store/).",
    )
    group.add_argument(
        "--repair-attempts",
        type=int,
        default=None,
        help="Proposals tried for one halt (default 3).",
    )
    group.add_argument(
        "--repair-fault-url",
        action="append",
        default=[],
        metavar="URL",
        help=(
            "A URL that presents a fault the patched run must refuse before the "
            "save (repeatable). Needed when the step's identity is read from "
            "the DOM."
        ),
    )
    group.add_argument(
        "--repair-approval-wait",
        type=float,
        default=None,
        metavar="SECONDS",
        help="How long to wait for a person's answer (default 900).",
    )


def resolve_auto_repair(args: argparse.Namespace, cfg: Any) -> str:
    """Fill the repair flags from the deployment and say how repair runs.

    Returns ``explicit`` (``--self-repair``), ``auto`` (an agent is
    configured and nothing turned it off) or ``off``. A flag on the command
    line wins over the deployment's ``repair`` section.
    """

    section = getattr(cfg, "repair", None)
    fill = {
        "repair_agent": "agent",
        "repair_screen_access_by": "screen_access_by",
        "repair_screen_share_mode": "screen_share_mode",
        "repair_store": "store",
        "repair_attempts": "attempts",
        "repair_approval_wait": "approval_wait_s",
        "repair_model_provider": "model_provider",
        "repair_model_key_file": "model_key_file",
        "repair_requalify": "requalify",
        "repair_requalify_trials": "requalify_trials",
    }
    defaults = {
        "repair_screen_share_mode": "scrubbed_only",
        "repair_store": "repair-store",
        "repair_attempts": 3,
        "repair_approval_wait": 900.0,
        "repair_requalify_trials": 1,
    }
    for flag, name in fill.items():
        if getattr(args, flag, None) is None:
            value = getattr(section, name, None) if section is not None else None
            setattr(args, flag, value if value is not None else defaults.get(flag))
    if not getattr(args, "repair_fault_url", None) and section is not None:
        args.repair_fault_url = list(section.fault_urls)
    if getattr(args, "self_repair", False):
        return "explicit"
    if getattr(args, "no_auto_repair", False):
        return "off"
    if section is not None and not section.auto:
        return "off"
    return "auto" if args.repair_agent else "off"


def load_agent(spec: str) -> Any:
    """Import ``module:name``; instantiate it when it is a class or factory."""

    from openadapt_flow.repair.self_repair import SelfRepairRefused

    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SelfRepairRefused("--repair-agent must look like module:name")
    obj = getattr(importlib.import_module(module_name), attr)
    if hasattr(obj, "propose") and not isinstance(obj, type):
        return obj
    agent = obj()
    if not hasattr(agent, "propose"):
        raise SelfRepairRefused(f"{spec} did not produce an agent with propose()")
    return agent


def _refusal(
    args: argparse.Namespace, backend_cfg: Any, url: Optional[str]
) -> Optional[str]:
    from openadapt_flow.backends.factory import _normalize_kind
    from openadapt_flow.compatibility import REPAIR_MANAGED, REPAIR_QUALIFIED

    if getattr(args, "managed_dispatch_file", None):
        return REPAIR_MANAGED
    if getattr(args, "qualification_authority_file", None):
        return REPAIR_QUALIFIED
    if getattr(args, "_qualification_case_execution", None) is not None:
        return "a qualification case can't be repaired"
    if getattr(args, "pin_digest", None) and not getattr(
        args, "repair_requalify", None
    ):
        return (
            "--pin-digest pins the original bundle; set repair.requalify to the "
            "bundle's verify.yaml so a fix is re-qualified and re-admitted"
        )
    if _normalize_kind(backend_cfg.kind) != "web":
        return "only browser runs can be repaired for now"
    if backend_cfg.browser_cdp_endpoint or backend_cfg.browser_page_url:
        return "an attached browser can't be reopened for a probe"
    if not url:
        return "--url is required"
    for flag in ("hold_on_halt", "save_healed_to", "record_video"):
        if getattr(args, flag, None):
            return f"--{flag.replace('_', '-')} can't be combined with --self-repair"
    if not args.repair_agent:
        return "--repair-agent is required"
    if not args.repair_screen_access_by:
        return "--repair-screen-access-by is required"
    return None


def _requalification(args: argparse.Namespace) -> Any:
    """The parent's campaign on its test copy, when repair.requalify is set."""

    path = getattr(args, "repair_requalify", None)
    if not path:
        return None
    from openadapt_flow.qualify.config import load_verify_config
    from openadapt_flow.repair.readmission import Requalification

    return Requalification(
        config=load_verify_config(Path(path)),
        trials=int(getattr(args, "repair_requalify_trials", None) or 1),
    )


def pinned_lineage(args: argparse.Namespace, cfg: Any) -> Any:
    """The lineage check a ``--pin-digest`` run uses, or None.

    A repaired, re-qualified version of the pinned bundle passes the pin
    through the signed lineage in the repair store; nothing else does.
    """

    if not getattr(args, "pin_digest", None):
        return None
    section = getattr(cfg, "repair", None)
    store = getattr(args, "repair_store", None) or (
        section.store if section is not None else "repair-store"
    )
    from openadapt_flow.repair.readmission import READMISSIONS_DIR, lineage_check

    if not (Path(store) / READMISSIONS_DIR).is_dir():
        return None
    return lineage_check(Path(store))


class CliTarget:
    """Open sessions at ``--url`` and run the engine the way ``run`` does."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        bundle: Path,
        url: str,
        cfg: Any,
        backend_cfg: Any,
        gate_kwargs: dict[str, Any],
        params: dict[str, Any],
        worklists: dict[str, Any],
        effect_verifier: Any,
        api_actuator: Any,
        durable: bool,
        allow_egress: bool,
        run_replayer: Callable[..., Any],
        approval_gate_for_run: Callable[..., Any],
    ) -> None:
        self.args = args
        self.bundle = bundle.resolve()
        self.url = url
        self.cfg = cfg
        self.backend_cfg = backend_cfg
        self.gate_kwargs = gate_kwargs
        self.params = params
        self.worklists = worklists
        self.effect_verifier = effect_verifier
        self.api_actuator = api_actuator
        self.durable = durable
        self.allow_egress = allow_egress
        self._run_replayer = run_replayer
        self._approval_gate_for_run = approval_gate_for_run
        self.headed = bool(args.headed or cfg.backend.headed)

    def open(self, fault: Optional[str] = None) -> "AppSession":
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.repair.self_repair import AppSession

        backend, close = PlaywrightBackend.launch(
            fault or self.url, headless=not self.headed
        )
        return AppSession(backend=backend, close=close)

    def execute(
        self,
        workflow: Any,
        bundle_dir: Path,
        session: "AppSession",
        run_dir: Path,
        *,
        approval_gate: Any = None,
        gui_only: bool = False,
    ) -> Any:
        from urllib.parse import urlsplit

        from openadapt_flow.repair.self_repair import (
            REPAIR_GUI_ONLY,
            SelfRepairRefused,
        )
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )

        original = approval_gate is None and Path(bundle_dir).resolve() == self.bundle
        if original:
            # The ordinary run: the authorization and approval gate ``run``
            # already admitted and bound.
            authorization = self.args._governed_run_authorization
            gate = self._approval_gate_for_run(
                self.args,
                run_dir,
                policy_name=getattr(self.args, "policy", None)
                or self.cfg.policy.policy,
            )
        else:
            report = evaluate_run_gate(
                workflow, bundle_dir=bundle_dir, **self.gate_kwargs
            )
            if not report.passed:
                raise SelfRepairRefused(
                    "the run gate refused the patched bundle:\n" + report.render()
                )
            authorization = build_runtime_authorization(
                workflow, report, params=self.params, worklists=self.worklists
            )
            gate = approval_gate
        page_url = session.backend.page.url
        parts = urlsplit(page_url)
        return self._run_replayer(
            session.backend,
            workflow=workflow,
            params=self.params,
            worklists=self.worklists,
            bundle=bundle_dir,
            run_dir=run_dir,
            save_healed_to=None,
            allow_egress=self.allow_egress,
            effect_verifier=self.effect_verifier,
            api_actuator=self.api_actuator,
            durable=self.durable,
            use_structural=True,
            pixel_verify_enabled=self.cfg.runtime.pixel_verify_enabled,
            governed_authorization=authorization,
            runtime_config=self.cfg.runtime,
            execution_target_kind="web",
            execution_origin=f"{parts.scheme}://{parts.netloc}",
            execution_entry_url=page_url,
            approval_gate=gate,
            deployment_surface=self.backend_cfg.deployment_surface,
            # A probe or the resumed run of a repaired bundle runs the GUI
            # step the person approved (repair.self_repair.keep_gui_for_repair).
            api_steps_off_reason=(
                None if original and not gui_only else REPAIR_GUI_ONLY
            ),
        )


def cmd_run_self_repair(
    args: argparse.Namespace,
    *,
    workflow: Any,
    bundle: Path,
    cfg: Any,
    backend_cfg: Any,
    gate_kwargs: dict[str, Any],
    params: dict[str, Any],
    worklists: dict[str, Any],
    effect_verifier: Any,
    api_actuator: Any,
    durable: bool,
    allow_egress: bool,
    run_replayer: Callable[..., Any],
    approval_gate_for_run: Callable[..., Any],
    finish: Callable[..., int],
    default_run_dir: Callable[[], Path],
    attest_pre_state: Optional[Callable[..., None]] = None,
    mode: str = "explicit",
) -> Optional[int]:
    """Run the admitted bundle with self-repair on. Returns the exit code.

    In ``auto`` mode a run that can't be repaired is not refused: this
    prints why repair is off for it and returns ``None`` before anything
    ran, and the caller runs it plainly.
    """

    from openadapt_flow.cli_hints import command
    from openadapt_flow.repair.auto_repair import AutoRepair
    from openadapt_flow.repair.self_repair import (
        ScreenAccess,
        SelfRepair,
        SelfRepairRefused,
    )

    url = args.url or cfg.backend.url
    refusal = _refusal(args, backend_cfg, url)
    if refusal is None and (
        worklists
        or getattr(args, "workers", None)
        or getattr(args, "readback_window", None)
    ):
        refusal = (
            "a worklist run halts after earlier items saved; repair a worklist "
            "with WorklistQueue (docs/SELF_REPAIR.md)"
        )
    if refusal is not None:
        if mode == "auto":
            print(
                f"Auto-repair is off for this run: {refusal}. A stop waits for a "
                "person.",
                flush=True,
            )
            return None
        print(f"run REFUSED: --self-repair: {refusal}. Nothing was executed.")
        return 2
    model_key = None
    try:
        agent = load_agent(args.repair_agent)
        if args.repair_model_provider:
            from openadapt_flow.jit.keys import load_customer_model_key

            model_key = load_customer_model_key(
                args.repair_model_provider, config_path=args.repair_model_key_file
            )

        def announce(run_dir: Path, request_id: str, request_sha256: str) -> None:
            print(
                "\nSelf-repair: a fix passed its probe and is waiting for one "
                "person.\nApprove it on the phone (decision portal) or here:\n  "
                + command(
                    f"item-approval approve {run_dir} --request-id {request_id} "
                    f"--request-sha256 {request_sha256} --decided-by human "
                    "--route desktop_run_it --unauthenticated "
                    "--approver-key YOUR_APPROVER_KEY.pem"
                )
                + "\n(with OPENADAPT_JIT_KEY_PASSPHRASE set; the key is on the "
                "approver roster: jit approver add)",
                flush=True,
            )

        target = CliTarget(
            args,
            bundle=bundle,
            url=url,
            cfg=cfg,
            backend_cfg=backend_cfg,
            gate_kwargs=gate_kwargs,
            params=params,
            worklists=worklists,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=durable,
            allow_egress=allow_egress,
            run_replayer=run_replayer,
            approval_gate_for_run=approval_gate_for_run,
        )
        repair = SelfRepair(
            agent,
            target,
            store=Path(args.repair_store),
            screen_access=ScreenAccess(
                enabled_by=args.repair_screen_access_by,
                mode=args.repair_screen_share_mode,
            ),
            model_key=model_key,
            max_attempts=args.repair_attempts,
            fault_probes=tuple(args.repair_fault_url),
            approval_wait_s=args.repair_approval_wait,
            on_approval_request=announce,
            requalification=_requalification(args),
        )
    except (SelfRepairRefused, ImportError, AttributeError, ValueError) as exc:
        if mode == "auto":
            print(
                f"Auto-repair is off for this run: {exc}. A stop waits for a person.",
                flush=True,
            )
            return None
        print(f"run REFUSED: --self-repair: {exc}. Nothing was executed.")
        return 2
    run_dir = Path(args.run_dir) if args.run_dir else default_run_dir()
    if attest_pre_state is not None:
        # Same point as a plain run: admitted, nothing actuated yet.
        attest_pre_state(run_dir, args)
    from openadapt_flow.backends._browser_process import shared_browser

    auto = AutoRepair(repair)
    try:
        # The halted session stays open while each probe opens its own. Two
        # Playwright drivers can't run on one thread, so every session of
        # this run shares one browser process (each still gets a fresh
        # context: no cookies or storage carry over).
        with shared_browser(headless=not target.headed):
            result = auto.run(bundle, run_dir)
    except SelfRepairRefused as exc:
        print(f"Self-repair stopped: {exc}. Nothing was saved by self-repair.")
        return 1
    evidence = result.evidence
    print(
        f"\nSelf-repair: {evidence.status}"
        + (f" ({evidence.reason})" if evidence.reason else "")
    )
    for record in evidence.steps:
        mark = "ok " if record.passed else "NO "
        print(f"  [{mark}] {record.stage}: {record.detail[:160]}")
    print(
        f"  repair agent model calls: {evidence.model_calls}, "
        f"est. cost ${evidence.est_model_cost_usd:.4f}"
    )
    if result.candidate is not None:
        print(
            f"  candidate {result.candidate.candidate_id} [{result.candidate.state}] "
            f"in {args.repair_store}"
        )
    if evidence.status != "not_needed":
        print(f"  stop logged in {auto.ledger.path}")
    return finish(result.run_dir, result.report, args, backend_kind="web")
