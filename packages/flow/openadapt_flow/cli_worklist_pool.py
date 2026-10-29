"""CLI glue for parallel worklist runs (``--workers N``).

``replay`` and ``run`` with ``--workers N`` (or a read-back window above 0)
run a worklist program once per item across N workers. See
``runtime/worklist_pool.py`` for the scheduling rules and docs/THROUGHPUT.md
for how to set it up.

Each item is the same governed run a serial ``--worklist`` loop would make
for that row: the program with a one-row worklist, its own run directory
(``<run_dir>/items/NNNNN``), its own authorization bound to that row, and
every check. Workers differ only in their session.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from openadapt_flow.runtime.worklist_pool import (
    ItemRun,
    PoolPolicy,
    PoolRefusal,
    PoolReport,
    cookie_fingerprint,
    item_detail_from_report,
    item_state_from_report,
    run_worklist_pool,
)

#: Called by a worker to replay one item on its own backend:
#: ``(backend_source, idx, row, item_dir, pipeline) -> RunReport``.
RunItem = Callable[[Any, int, dict[str, Any], Path, Optional[ReadbackPipeline]], Any]


def requested_pool(args: Any, cfg: Any) -> Optional[PoolPolicy]:
    """The pool policy this run asked for, or None for a plain serial run."""

    workers = getattr(args, "workers", None)
    window = getattr(args, "readback_window", None)
    section = getattr(cfg, "worklist", None)
    if window is None:
        window = getattr(section, "readback_window", 0) if section is not None else 0
    if workers is None and window == 0:
        return None
    if section is None:
        from openadapt_flow.deployment import WorklistSection

        section = WorklistSection()
    return PoolPolicy(
        workers=int(workers or 1),
        readback_window=int(window),
        on_halt=section.on_halt,
        items_independent=section.items_independent,
        max_workers=section.max_workers,
    )


def pool_profile_refusal(*, managed: bool, qualification_case: bool) -> Optional[str]:
    """Why ``--workers`` / ``--readback-window`` can't run here, or None.

    Workers and a read-back window are for local runs. A managed dispatch and
    a qualification case each bind one exact run.
    """

    if managed or qualification_case:
        return (
            "--workers and --readback-window apply to local runs, not a managed "
            "dispatch or a qualification case"
        )
    return None


def loop_relation(workflow: Any) -> str:
    """The relation of a single-LOOP program (the ``for-each`` shape).

    Parallel workers split the loop's rows, so the program must be exactly
    one LOOP state followed by terminals: anything before or after the loop
    would run once per item instead of once per run.
    """

    from openadapt_flow.ir import StateKind

    program = getattr(workflow, "program", None)
    if program is None:
        raise PoolRefusal(
            "--workers needs a worklist program (a for-each bundle); this "
            "bundle is a single linear run"
        )
    entry = program.states.get(program.entry)
    others = [
        s
        for sid, s in program.states.items()
        if sid != program.entry and s.kind is not StateKind.TERMINAL
    ]
    if entry is None or entry.kind is not StateKind.LOOP or others:
        raise PoolRefusal(
            "--workers needs a program that is one loop over a worklist (the "
            "shape `for-each` writes); this program has other states"
        )
    for tr in entry.transitions:
        target = program.states.get(tr.target)
        if target is None or target.kind is not StateKind.TERMINAL:
            raise PoolRefusal("--workers needs the loop to end the program")
    return entry.loop.relation


def pool_rows(
    workflow: Any, worklists: Mapping[str, list[dict[str, str]]], relation: str
) -> list[dict[str, str]]:
    if relation in worklists:
        return list(worklists[relation])
    source = (getattr(workflow, "data_sources", None) or {}).get(relation)
    if source is None:
        raise PoolRefusal(f"no rows for the loop's worklist {relation!r}")
    return list(source.rows)


def check_accounts(policy: PoolPolicy, section: Any, backend_kind: str) -> list[Any]:
    """The accounts the workers will use; refuse unsafe declarations."""

    accounts = list(section.accounts)
    if policy.workers == 1 and not accounts:
        return [None]
    if len(accounts) < policy.workers:
        raise PoolRefusal(
            f"--workers {policy.workers} needs {policy.workers} accounts in "
            f"worklist.accounts (found {len(accounts)}); workers never share "
            "a signed-in session"
        )
    accounts = accounts[: policy.workers]
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise PoolRefusal("worklist.accounts names must be distinct")
    seen: dict[tuple, str] = {}
    for account in accounts:
        missing = [
            env
            for env in [
                *account.params_env.values(),
                *account.secrets_env.values(),
                *([account.rdp_password_env] if account.rdp_password_env else []),
            ]
            if not os.environ.get(env)
        ]
        if missing:
            raise PoolRefusal(
                f"account {account.name}: environment variable(s) "
                f"{', '.join(sorted(missing))} are not set"
            )
        who = (
            tuple(sorted((k, os.environ[v]) for k, v in account.params_env.items())),
            account.rdp_username,
            account.browser_cdp_endpoint,
        )
        if who != ((), None, None) and who in seen:
            raise PoolRefusal(
                f"accounts {seen[who]} and {account.name} sign in as the same "
                "user; many apps lock a record per user, so each worker needs "
                "its own"
            )
        seen[who] = account.name
        if (
            backend_kind == "web"
            and not account.browser_cdp_endpoint
            and not account.params_env
        ):
            raise PoolRefusal(
                f"account {account.name} must name its user in params_env (for "
                "example username: EMR_USER_1), so two accounts for the same "
                "user can be told apart"
            )
        if backend_kind == "web" and not (
            account.browser_cdp_endpoint or section.sign_in_bundle
        ):
            raise PoolRefusal(
                f"account {account.name} has no way to sign in: set "
                "worklist.sign_in_bundle or the account's browser_cdp_endpoint"
            )
        if backend_kind == "rdp" and not (
            account.rdp_username and account.rdp_password_env
        ):
            raise PoolRefusal(
                f"account {account.name} needs rdp_username and rdp_password_env"
            )
    return accounts


@dataclass
class WebPoolWorker:
    """One browser context, one account, one thread.

    ``sign_in(page)`` signs the fresh context in (replaying the sign-in
    bundle with this worker's account, or a caller's own function). With
    ``cdp_endpoint`` the worker instead opens a tab in that account's own
    running browser and never closes it.
    """

    name: str
    entry_url: str
    run_item_on_page: RunItem
    sign_in: Optional[Callable[[Any], None]] = None
    #: How to reach the item's start page; default ``page.goto(entry_url)``.
    open_entry: Optional[Callable[[Any], None]] = None
    #: After an item that halted before any write, when the session takes
    #: another item: clean the screen, check the start screen and read the
    #: records back (``halt_cleanup.run_cleanup``; returns a CleanupResult).
    clean: Optional[Callable[[Any, Path], Any]] = None
    #: On the fresh session, before any item: the start check alone. Returns
    #: None when the start screen shows, else why not.
    start_check: Optional[Callable[[Any, Path], Optional[str]]] = None
    cdp_endpoint: Optional[str] = None
    headed: bool = False
    readback_window: int = 0
    viewport: Mapping[str, int] = field(
        default_factory=lambda: {"width": 1280, "height": 800}
    )
    _pw: Any = None
    _browser: Any = None
    _context: Any = None
    _page: Any = None
    _pipeline: Optional[ReadbackPipeline] = None

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        if self.cdp_endpoint:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
            contexts = self._browser.contexts
            if not contexts:
                raise RuntimeError("the attached browser has no signed-in context")
            self._context = contexts[0]
            self._page = self._context.new_page()
            self._page.set_viewport_size(dict(self.viewport))
            self._page.goto(self.entry_url)
        else:
            from openadapt_flow._browser_setup import launch_chromium

            self._browser = launch_chromium(self._pw.chromium, headless=not self.headed)
            self._context = self._browser.new_context(
                viewport=dict(self.viewport), device_scale_factor=1
            )
            self._page = self._context.new_page()
            if self.sign_in is not None:
                self.sign_in(self._page)
        if self.readback_window > 0:
            self._pipeline = ReadbackPipeline()

    def session_fingerprint(self) -> Optional[str]:
        return cookie_fingerprint(self._context.cookies())

    def run_item(self, idx: int, row: dict[str, Any], item_dir: Path) -> ItemRun:
        if self.open_entry is not None:
            self.open_entry(self._page)
        else:
            self._page.goto(self.entry_url)
        report = self.run_item_on_page(self._page, idx, row, item_dir, self._pipeline)
        return _item_run(report, self._pipeline)

    def clean_after_halt(self, cleanup_dir: Path) -> Any:
        if self.clean is None:
            return None
        return self.clean(self._page, cleanup_dir)

    def check_start(self, check_dir: Path) -> Optional[str]:
        if self.start_check is None:
            return None
        return self.start_check(self._page, check_dir)

    def close(self) -> None:
        try:
            if self._pipeline is not None:
                self._pipeline.close()
            if self.cdp_endpoint:
                if self._page is not None:
                    self._page.close()
            else:
                if self._context is not None:
                    self._context.close()
                if self._browser is not None:
                    self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


@dataclass
class BackendPoolWorker:
    """A worker over an already-connected non-web session (RDP pool slot)."""

    name: str
    backend: Any
    run_item_on_backend: RunItem
    fingerprint: Optional[str] = None
    readback_window: int = 0
    #: After a halted item: clean the screen, check the start screen and
    #: read the records back (returns a CleanupResult).
    clean: Optional[Callable[[Any, Path], Any]] = None
    #: On the fresh session, before any item: the start check alone.
    start_check: Optional[Callable[[Any, Path], Optional[str]]] = None
    _pipeline: Optional[ReadbackPipeline] = None

    def open(self) -> None:
        if self.readback_window > 0:
            self._pipeline = ReadbackPipeline()

    def clean_after_halt(self, cleanup_dir: Path) -> Any:
        if self.clean is None:
            return None
        return self.clean(self.backend, cleanup_dir)

    def check_start(self, check_dir: Path) -> Optional[str]:
        if self.start_check is None:
            return None
        return self.start_check(self.backend, check_dir)

    def session_fingerprint(self) -> Optional[str]:
        return self.fingerprint

    def run_item(self, idx: int, row: dict[str, Any], item_dir: Path) -> ItemRun:
        report = self.run_item_on_backend(
            self.backend, idx, row, item_dir, self._pipeline
        )
        return _item_run(report, self._pipeline)

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.close()


def _item_run(report: Any, pipeline: Optional[ReadbackPipeline]) -> ItemRun:
    state = item_state_from_report(report)
    detail = item_detail_from_report(report)
    handoff = pipeline.take() if pipeline is not None else None
    if handoff is None:
        if pipeline is not None and pipeline.inline_reason:
            detail["readback"] = f"inline ({pipeline.inline_reason})"
        return ItemRun(state=state, detail=detail)
    result: Future = Future()

    def _settled(fut: Future) -> None:
        try:
            settled = fut.result()
            result.set_result(
                (item_state_from_report(settled), item_detail_from_report(settled))
            )
        except Exception as exc:  # noqa: BLE001
            result.set_exception(exc)

    handoff.settled.add_done_callback(_settled)
    return ItemRun(state=state, detail=detail, pending_readback=result)


def sign_in_with_bundle(
    page: Any,
    *,
    bundle: Path,
    account: Any,
    backend_factory: Callable[[Any], Any],
    url: str,
    run_dir: Path,
) -> None:
    """Replay the recorded sign-in bundle with this account's credentials."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    page.goto(url)
    workflow = Workflow.load(bundle)
    params = {k: os.environ[v] for k, v in account.params_env.items()}
    report = Replayer(
        backend_factory(page),
        secret_env_overrides=dict(account.secrets_env),
    ).run(workflow, params=params, bundle_dir=bundle, run_dir=run_dir)
    if not report.success:
        raise RuntimeError(f"sign-in bundle did not finish for {account.name}")


def entry_with_bundle(
    page: Any,
    *,
    url: str,
    workflow: Any,
    bundle: Path,
    backend_factory: Callable[[Any], Any],
    run_dir: Path,
) -> None:
    """Go to the entry URL, then replay the recorded entry navigation."""

    from uuid import uuid4

    from openadapt_flow.runtime import Replayer

    page.goto(url)
    report = Replayer(backend_factory(page)).run(
        workflow, params={}, bundle_dir=bundle, run_dir=run_dir / uuid4().hex[:12]
    )
    if not report.success:
        raise RuntimeError("the entry bundle did not reach the start page")


def load_entry_bundle(path: Path) -> Any:
    """Load the entry bundle; refuse one that declares any effect."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.traversal import iter_workflow_steps

    workflow = Workflow.load(path)
    if any(step.effects for step in iter_workflow_steps(workflow)):
        raise PoolRefusal("worklist.entry_bundle declares a save; it may only navigate")
    return workflow


def print_pool_report(report: PoolReport, run_dir: Path) -> int:
    counts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts().items()))
    print(
        f"Worklist: {len(report.items)} items on {report.workers} workers in "
        f"{report.wall_s:.1f} s ({counts})."
    )
    rate = report.entries_per_hour()
    if rate is not None:
        print(f"Finished items per hour, this run: {rate}")
    for w in report.per_worker:
        states = ", ".join(f"{k} {v}" for k, v in sorted(w.states.items())) or "nothing"
        print(f"  {w.name}: {len(w.items)} items ({states})")
        if w.dirty:
            print(f"    dirty: {w.dirty}; this session took no more items")
    if report.stop:
        print(
            f"STOPPED by {report.stop.get('worker')} at item {report.stop.get('idx')}: "
            f"{report.stop.get('reason')}"
        )
    for idx, after in sorted(report.written_after_failed_readback.items()):
        print(
            f"Read-back of item {idx} failed; written after it by the same "
            f"worker: {', '.join(map(str, after)) or 'none'}"
        )
    print(f"Per-item reports: {run_dir / 'items'}; summary: {run_dir / 'WORKLIST.md'}")
    return 0 if report.success else 1


def run_pool(
    rows: Sequence[Mapping[str, Any]],
    workers: Sequence[Any],
    *,
    run_dir: Path,
    policy: PoolPolicy,
    salt: str,
) -> int:
    lock = threading.Lock()

    def say(msg: str) -> None:
        with lock:
            print(msg, flush=True)

    try:
        report = run_worklist_pool(
            rows,
            workers,
            run_dir=run_dir,
            policy=policy,
            fingerprint_salt=salt,
            on_event=say,
        )
    except PoolRefusal as exc:
        print(f"REFUSED: {exc}. No item was run.")
        return 2
    return print_pool_report(report, run_dir)
