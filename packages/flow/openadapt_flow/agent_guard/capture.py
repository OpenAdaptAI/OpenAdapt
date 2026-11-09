"""Run an external agent through the authorized JIT recording boundary.

``GuardedCapture`` connects the existing held-save guard to an active JIT
``Recording``. The agent receives only the recording-owned dispatcher; every
action therefore crosses ``CaptureComputer`` and its ``TeachGuard``. The
guarded-save check is drained after actions and must finish with an independent
read-back before the recording can write a draft.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from openadapt_flow.agent_guard.cdp import CdpGuardPage
from openadapt_flow.agent_guard.commit_guard import CommitGuard
from openadapt_flow.agent_guard.config import GuardConfig
from openadapt_flow.guard import GuardedSave, GuardTask
from openadapt_flow.verify_run import load_verify_config

__all__ = ["GuardedCapture", "GuardedCaptureError"]


class GuardedCaptureError(RuntimeError):
    """A guarded capture did not finish with a verified save."""


class GuardedCapture:
    """Attach the agent_guard held-save protocol to an active JIT recording.

    Construct this inside ``with compiled.recording(...) as recording`` and
    before giving ``recording.dispatcher`` to the agent. The recording must
    already have bound a signed teach authorization for the same task.
    """

    def __init__(
        self,
        recording: Any,
        *,
        cfg: GuardConfig,
        task: GuardTask,
        out_dir: Path | str,
        agent_id: str = "authorized-agent",
        timeout_s: float = 60.0,
    ) -> None:
        recording = getattr(recording, "recording", recording)
        dispatcher = getattr(recording, "dispatcher", None)
        computer = getattr(recording, "computer", None)
        teach_guard = getattr(recording, "guard", None)
        if dispatcher is None or computer is None or teach_guard is None:
            raise GuardedCaptureError(
                "a guarded capture needs an active JIT recording with a signed teach scope"
            )
        if not teach_guard.scope.authorized:
            raise GuardedCaptureError("a signed teach authorization is required")
        if cfg.observe_only:
            raise GuardedCaptureError(
                "an observe-only guard cannot authorize a teach capture"
            )
        if teach_guard.scope.task_id != task.task_id:
            raise GuardedCaptureError(
                "the authorization task and guarded task must match exactly"
            )
        page = computer.page
        if page is None:
            raise GuardedCaptureError("the authorized recording has no browser page")

        # The assigned task may narrow the signed scope, but cannot widen it.
        from openadapt_flow import teach_authorization as ta

        task_scope = _task_scope(cfg, task, teach_guard.scope)
        scope = ta.narrowed(task_scope, teach_guard.scope)
        teach_guard.scope = scope

        self.out_dir = Path(out_dir).expanduser().resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.page = CdpGuardPage(
            page,
            screen_selector=cfg.screen_selector,
            all_resources=True,
        )
        self.guarded = GuardedSave(
            load_verify_config(cfg.verify),
            self.out_dir / "guard",
            approval=cfg.approval,
        )
        self.commit_guard = CommitGuard(
            self.guarded,
            task,
            self.page,
            app_origin=cfg.app_origin,
            not_saves=cfg.not_saves,
            observe_only=cfg.observe_only,
            agent_id=agent_id,
            response_wait_s=cfg.response_wait_s,
            events_path=self.out_dir / "guard_events.jsonl",
            scope=scope,
        )
        self._closed = False
        # Drain CDP while Recorder waits for a step to settle. This keeps a
        # held save in flight until its write has been observed and binds the
        # SOR after-state to the action that performed it.
        dispatcher.add_settle_callback(self._pump)
        dispatcher.add_after_action(self._after_action)
        dispatcher.before_finish(self._finish)
        dispatcher.on_close(self.close)

    @property
    def summary(self) -> dict[str, Any]:
        return self.commit_guard.summary.to_dict()

    def _after_action(self, _native: Any) -> None:
        self._pump()

    def _pump(self, deadline: float | None = None) -> None:
        # Playwright dispatches CDP events while the page waits. Drain after
        # each action so the next action cannot overtake a held save.
        if self._closed:
            raise GuardedCaptureError("the guarded capture has already closed")
        wait_ms = 25
        if deadline is not None:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise GuardedCaptureError(
                    "the held-save guard exceeded the recorder's settle deadline"
                )
            wait_ms = min(wait_ms, remaining_ms)
        self.page.page.wait_for_timeout(wait_ms)
        self.page.drain(self.commit_guard)
        if deadline is not None and time.monotonic() > deadline:
            raise GuardedCaptureError(
                "the held-save guard exceeded the recorder's settle deadline"
            )
        if self.commit_guard.summary.scope_blocked:
            first = self.commit_guard.summary.scope_blocked[0]
            raise GuardedCaptureError(
                f"the guarded task left its signed scope ({first.get('code', 'scope_refused')})"
            )

    def _finish(self) -> None:
        deadline = time.monotonic() + self.timeout_s
        while not self.commit_guard.finished and time.monotonic() < deadline:
            self._pump()
        if not self.commit_guard.finished:
            raise GuardedCaptureError(
                "the held-save guard did not finish its independent read-back"
            )
        if self.commit_guard.summary.verdict != "VERIFIED":
            raise GuardedCaptureError(
                "the held-save guard read-back was "
                + str(self.commit_guard.summary.verdict)
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.page.release()


def _task_scope(cfg: GuardConfig, task: GuardTask, authorized_scope: Any) -> Any:
    from openadapt_flow.teach_scope import TeachScope, load_write_adapter

    # GuardTask.expected_record also carries UI identity used by CommitGuard
    # (for example, a patient's display name). The signed teach scope names
    # the record identity used by the system-of-record adapter. Keep those
    # UI-only checks in CommitGuard without pretending the SOR can verify
    # display fields it does not persist.
    record = {}
    for name in authorized_scope.record:
        value = task.expected_record.get(name)
        if value is None:
            raise GuardedCaptureError(
                f"the guarded task omits authorized record identity {name!r}"
            )
        record[name] = value
    origins = [
        cfg.app_origin,
        *cfg.allow_origins,
        *(rule.origin for rule in cfg.not_saves if rule.origin),
    ]
    return TeachScope.for_task(
        task.task_id,
        origins=origins,
        record=record,
        inputs={},
        fields=task.intended_fields,
        constants=cfg.allow_text,
        write_adapter=(
            load_write_adapter(cfg.write_adapter) if cfg.write_adapter else None
        ),
    )
