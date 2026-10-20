"""The commit guard: hold the agent's save until the right record is on screen.

:class:`CommitGuard` decides what happens to each request the agent's tab
sends. It does no browser I/O itself: a :class:`GuardPage` (the CDP adapter in
:mod:`openadapt_flow.agent_guard.cdp`, or a fake in tests) continues or blocks
the paused request, reads the screen and shows the agent a notice.

For a save (:func:`~openadapt_flow.agent_guard.requests.classify`) it runs the
guarded-save contract of :mod:`openadapt_flow.guard`, which the agent never
calls itself:

1. :meth:`GuardedSave.begin <openadapt_flow.guard.GuardedSave.begin>` checks
   that every value of the task's ``expected_record`` is in the text of the
   open record (:func:`~openadapt_flow.guard.check_identity`), and reads the
   system of record for the baseline. A refusal blocks the request.
2. :meth:`Guard.commit <openadapt_flow.guard.Guard.commit>` re-checks the
   screen, waits for a person when the approval mode is ``each_item``, and
   records that this task's save may happen once. A second save of the same
   task is blocked.
3. The request goes on. When it finishes (or ``response_wait_s`` passes),
   :meth:`Guard.confirm <openadapt_flow.guard.Guard.confirm>` reads the
   record back through ``verify-run``. A verdict other than ``VERIFIED`` is
   shown on the agent's page; a verified save shows nothing, so the run is
   recorded as the app drew it.

``observe_only`` never blocks. It runs the same checks, logs what the guard
would have blocked, and still reads the save back. Without a baseline (the
store couldn't be read before the write), observe-only lets the save through
and reports that it couldn't be checked.

The event log holds no record values: methods, resource types, origins, a
digest of each path, decisions and codes.
"""

from __future__ import annotations

import hashlib
import html
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from openadapt_flow.agent_guard.requests import (
    HeldRequest,
    NotASave,
    classify,
    origin_of,
)
from openadapt_flow.guard import (
    Guard,
    GuardedSave,
    GuardRefused,
    GuardResult,
    GuardTask,
    GuardVerdict,
    check_identity,
)

__all__ = [
    "BLOCK_STATUS",
    "CommitGuard",
    "GuardPage",
    "GuardSummary",
]

#: The status a blocked save gets. 409 reads as "not now" to most apps; the
#: body and the ``X-OpenAdapt-Guard`` header say why.
BLOCK_STATUS = 409
GUARD_HEADER = "X-OpenAdapt-Guard"
SUMMARY_SCHEMA = "openadapt.agent-guard-run/v1"


class GuardPage(Protocol):
    """What the guard needs from the agent's tab."""

    def continue_request(self, request_id: str) -> None: ...

    def block_request(
        self,
        request_id: str,
        *,
        status: int,
        body: str,
        content_type: str,
        headers: dict[str, str],
    ) -> None: ...

    def screen_text(self) -> Optional[str]: ...

    def screenshot(self) -> Optional[bytes]: ...

    def notify(self, message: str, *, level: str) -> None: ...

    def clear_notice(self) -> None: ...


@dataclass
class GuardSummary:
    """One guard session, without record values."""

    task_id: str
    mode: str
    approval: str
    verdict: Optional[str] = None
    receipt_path: Optional[str] = None
    blocked: list[dict[str, Any]] = field(default_factory=list)
    would_block: list[dict[str, Any]] = field(default_factory=list)
    saves_held: int = 0
    hold_ms: list[float] = field(default_factory=list)
    readback_ms: Optional[float] = None
    requests: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    #: Requests the task's teach scope stopped: code, method, origin and
    #: resource type. No paths, no values.
    scope_blocked: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SUMMARY_SCHEMA, **self.__dict__}


@dataclass
class _Save:
    """The save that went through, waiting for its read-back."""

    guard: Optional[Guard]
    network_id: Optional[str]
    sent_at: float
    done_at: Optional[float] = None
    #: observe-only: where its baseline is, or None when it has none.
    baseline: Optional[Path] = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class CommitGuard:
    """Decides each paused request of one task's run (see the module docstring)."""

    def __init__(
        self,
        guarded: GuardedSave,
        task: GuardTask,
        page: GuardPage,
        *,
        app_origin: str,
        not_saves: Sequence[NotASave] = (),
        observe_only: bool = False,
        agent_id: str = "attached-agent",
        response_wait_s: float = 30.0,
        events_path: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
        scope: Optional[Any] = None,
    ) -> None:
        self.guarded = guarded
        #: The task's :class:`~openadapt_flow.teach_scope.TeachScope`: reads
        #: to an undeclared origin are blocked, and a save is read against
        #: the declared record and values when it has a write adapter.
        self.scope = scope
        self.task = task
        self.page = page
        self.app_origin = origin_of(app_origin) or app_origin
        self.not_saves = tuple(not_saves)
        self.observe_only = observe_only
        self.agent_id = agent_id
        self.response_wait_s = response_wait_s
        self.events_path = events_path
        self._clock = clock
        self._started = clock()
        #: A save waiting for a person: (guard, request, paused at).
        self._waiting: Optional[tuple[Guard, HeldRequest, float]] = None
        self._save: Optional[_Save] = None
        self.result: Optional[GuardResult] = None
        #: observe-only: the verify-run receipt, when the save was read back.
        self.observed: Optional[dict[str, Any]] = None
        self.summary = GuardSummary(
            task_id=task.task_id,
            mode="observe_only" if observe_only else "fail_closed",
            approval=guarded.approval,
        )

    # -- state -----------------------------------------------------------------

    @property
    def finished(self) -> bool:
        """The task's save was sent and read back."""

        return self.summary.verdict is not None

    @property
    def busy(self) -> bool:
        """A save is waiting for a person or for its read-back."""

        return self._waiting is not None or (
            self._save is not None and not self.finished
        )

    # -- requests ----------------------------------------------------------------

    def on_request(self, request: HeldRequest) -> None:
        """Decide one paused request. Every request is continued or blocked,
        except a save that waits for a person (``each_item``)."""

        kind = classify(request, app_origin=self.app_origin, not_saves=self.not_saves)
        self.summary.requests[kind] = self.summary.requests.get(kind, 0) + 1
        if self.scope is not None and kind in ("read", "save"):
            code = self.scope.request_refusal(
                request.method,
                request.url,
                request.post_data.encode("utf-8") if request.post_data else None,
            )
            if code is not None:
                self._scope_refuse(request, kind, code)
                return
        if kind in ("read", "not_a_save"):
            self.page.continue_request(request.request_id)
            if kind == "not_a_save":
                self._log(request, kind, "continued")
            return
        held_at = self._clock()
        if kind == "other_origin":
            self._refuse(
                request,
                kind,
                held_at,
                "other_origin",
                "the request goes to another site than the app, and the operator "
                "didn't list it as not a save",
            )
            return
        self._on_save(request, held_at)

    def _on_save(self, request: HeldRequest, held_at: float) -> None:
        self.summary.saves_held += 1
        if self.observe_only:
            self._observe_save(request, held_at)
            return
        if self._waiting is not None:
            self._refuse(
                request,
                "save",
                held_at,
                "save_waiting_for_person",
                "another save of this task is waiting for a person to approve it",
            )
            return
        screen = self.page.screen_text()
        try:
            guard = self.guarded.begin(
                self.task, screen_text=screen, agent_id=self.agent_id
            )
            self._commit(guard, request, held_at, screen)
        except GuardRefused as exc:
            self._refuse(request, "save", held_at, exc.code, exc.message)

    def _commit(
        self,
        guard: Guard,
        request: HeldRequest,
        held_at: float,
        screen: Optional[str],
    ) -> None:
        shot = self.page.screenshot() if self.guarded.approval == "each_item" else None
        commit = guard.commit(screenshot_png=shot, screen_text=screen, wait_s=0.0)
        if commit.status == "waiting_for_approval":
            self._waiting = (guard, request, held_at)
            self._log(request, "save", "held_for_person", guard_id=guard.guard_id)
            self.page.notify(
                "OpenAdapt is holding this save until a person approves it. "
                "Don't save again.",
                level="info",
            )
            return
        self._send(guard, request, held_at)

    def _send(
        self, guard: Optional[Guard], request: HeldRequest, held_at: float
    ) -> None:
        sent = self._clock()
        # Known before it goes on, so its completion is never missed.
        self._save = _Save(guard=guard, network_id=request.network_id, sent_at=sent)
        hold_ms = round((sent - held_at) * 1000.0, 1)
        self.summary.hold_ms.append(hold_ms)
        if guard is not None and guard.approval is not None:
            self.page.clear_notice()  # the "waiting for a person" notice
        self.page.continue_request(request.request_id)
        self._log(request, "save", "continued", hold_ms=hold_ms)

    def _observe_save(self, request: HeldRequest, held_at: float) -> None:
        """observe-only: log what the guard would block, never block."""

        from openadapt_flow.verify_run import capture_baseline

        screen = self.page.screen_text()
        status, missing = check_identity(self.task.expected_record, screen)
        code = None
        if self._save is not None:
            code = "already_committed"
        elif status != "verified":
            code = f"identity_{status}"
        if code is not None:
            self.summary.would_block.append(
                {"code": code, "missing": missing if status != "verified" else []}
            )
        if self._save is not None:
            # Only the first save is read back; a second one is logged.
            self.page.continue_request(request.request_id)
            self._log(request, "save", "continued", code=code)
            return
        baseline: Optional[Path] = None
        work = self.guarded.state_dir / f"observe-{self.task.task_id}"
        try:
            path = work / "baseline.json"
            snap = capture_baseline(self.guarded.verify, self.task.params, path)
            if snap["state"].get("reachable"):
                baseline = path
        except Exception:  # noqa: BLE001 - observe-only never blocks
            baseline = None
        self._send(None, request, held_at)
        assert self._save is not None
        self._save.baseline = baseline
        if code is not None:
            self._log(request, "save", "would_block", code=code)

    def on_request_done(self, network_id: Optional[str]) -> None:
        """The save request finished or failed (the read-back decides which)."""

        if (
            self._save is not None
            and self._save.done_at is None
            and network_id is not None
            and network_id == self._save.network_id
        ):
            self._save.done_at = self._clock()

    # -- time ---------------------------------------------------------------------

    def poll(self) -> None:
        """Ask again for a waiting approval; read a finished save back."""

        if self._waiting is not None:
            guard, request, held_at = self._waiting
            try:
                commit = guard.commit(wait_s=0.0)
            except GuardRefused as exc:
                self._waiting = None
                self._refuse(request, "save", held_at, exc.code, exc.message)
                return
            if commit.status == "go":
                self._waiting = None
                self._send(guard, request, held_at)
            return
        save = self._save
        if save is None or self.finished:
            return
        now = self._clock()
        if save.done_at is None and now - save.sent_at < self.response_wait_s:
            return
        self._read_back(save)

    def _read_back(self, save: _Save) -> None:
        started = self._clock()
        if save.guard is not None:
            self.result = save.guard.confirm()
            verdict = self.result.verdict
            self.summary.receipt_path = self.result.receipt_path
            message = self.result.what_to_do
        else:
            verdict, message = self._observe_read_back(save)
        self.summary.readback_ms = round((self._clock() - started) * 1000.0, 1)
        self.summary.verdict = verdict.value
        if verdict is GuardVerdict.VERIFIED:
            # Nothing on the page: a verified run 1 is recorded as the app
            # drew it, so run 2 never expects the guard's notice.
            self.page.clear_notice()
        else:
            self.page.notify(
                "OpenAdapt read the record back: " + message, level="error"
            )
        self._event({"event": "read_back", "verdict": verdict.value})

    def _observe_read_back(self, save: _Save) -> tuple[GuardVerdict, str]:
        from openadapt_flow.guard import _verdict
        from openadapt_flow.verify_run import verify_run, write_result

        if save.baseline is None:
            return (
                GuardVerdict.RECONCILIATION_REQUIRED,
                "the system of record couldn't be read before the save, so the "
                "save couldn't be checked. A person must check the record.",
            )
        try:
            doc = verify_run(
                self.guarded.verify,
                params=self.task.params,
                run_record={"executor_kind": "agent", "executor_id": self.agent_id},
                authorization_ref=self.task.authorization,
                baseline_path=save.baseline,
                private_dir=save.baseline.parent,
                write_attempted=True,
            )
            self.summary.receipt_path = str(write_result(doc, save.baseline.parent))
        except Exception:  # noqa: BLE001 - an unreadable store is not verified
            doc = None
        self.observed = doc
        verdict = _verdict(doc)
        text = {
            GuardVerdict.VERIFIED: "saved, and the system of record shows it.",
            GuardVerdict.REFUTED: (
                "the system of record shows a different write than intended. "
                "A person must correct the record."
            ),
            GuardVerdict.RECONCILIATION_REQUIRED: (
                "the system of record doesn't show this save, or couldn't be "
                "read. A person must check the record."
            ),
        }[verdict]
        return verdict, text

    # -- refusing -----------------------------------------------------------------

    def _scope_refuse(self, request: HeldRequest, kind: str, code: str) -> None:
        """A request outside the task's teach scope: page content (an image
        beacon, a script's fetch) or the agent steered toward another origin,
        record or value."""

        self.summary.scope_blocked.append(
            {
                "code": code,
                "method": request.method,
                "origin": origin_of(request.url),
                "resource_type": request.resource_type,
            }
        )
        if kind == "save":
            self._refuse(
                request,
                kind,
                self._clock(),
                code,
                "the save is not the one the task declared (another record, "
                "field or value)",
            )
            return
        if self.observe_only:
            self.summary.would_block.append({"code": code})
            self.page.continue_request(request.request_id)
            self._log(request, kind, "would_block", code=code)
            return
        self.page.block_request(
            request.request_id,
            status=BLOCK_STATUS,
            body="",
            content_type="text/plain",
            headers={GUARD_HEADER: f"blocked; code={code}"},
        )
        self._log(request, kind, "blocked", code=code)

    def _refuse(
        self,
        request: HeldRequest,
        kind: str,
        held_at: float,
        code: str,
        message: str,
    ) -> None:
        if self.observe_only:
            self.summary.would_block.append({"code": code})
            self.page.continue_request(request.request_id)
            self._log(request, kind, "would_block", code=code)
            return
        what = (
            "Nothing was sent to the app. Open the record the task names and "
            "save again, or stop and tell a person."
            if code.startswith("identity_")
            else "Nothing was sent to the app. Don't retry this save."
        )
        payload = {
            "openadapt_guard": {
                "blocked": True,
                "code": code,
                "message": message,
                "what_to_do": what,
            }
        }
        if request.resource_type == "Document":
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>OpenAdapt blocked this save</title></head><body>"
                '<h1 id="openadapt-guard-blocked">OpenAdapt blocked this save</h1>'
                f"<p>{html.escape(message)}.</p><p>{html.escape(what)}</p>"
                "</body></html>"
            )
            content_type = "text/html; charset=utf-8"
        else:
            body = json.dumps(payload)
            content_type = "application/json"
        self.page.block_request(
            request.request_id,
            status=BLOCK_STATUS,
            body=body,
            content_type=content_type,
            headers={GUARD_HEADER: f"blocked; code={code}"},
        )
        self.summary.blocked.append(
            {"code": code, "hold_ms": round((self._clock() - held_at) * 1000.0, 1)}
        )
        self._log(request, kind, "blocked", code=code)
        self.page.notify(
            f"OpenAdapt blocked this save: {message}. {what}", level="error"
        )

    # -- log -------------------------------------------------------------------------

    def _log(
        self, request: HeldRequest, kind: str, decision: str, **extra: Any
    ) -> None:
        self._event(
            {
                "event": "request",
                "method": request.method,
                "resource_type": request.resource_type,
                "origin": origin_of(request.url),
                "path_sha256": _digest(request.path),
                "kind": kind,
                "decision": decision,
                **{k: v for k, v in extra.items() if v is not None},
            }
        )

    def _event(self, record: dict[str, Any]) -> None:
        if self.events_path is None:
            return
        record = {"t": round(self._clock() - self._started, 3), **record}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
