"""Persistent, thread-affine service for governed attended actions.

This is the public embedding seam for the localhost console and trusted local
bridges. Callers provide a reviewed :class:`DeploymentConfig`, keep one service
open for the lifetime of the visible authenticated application session, and
submit exact :class:`AttendedActionRequest` values. They never receive a raw
backend, Playwright page, or replayer.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast

from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.runtime.durable.approval import ApprovalRecord
from openadapt_flow.runtime.durable.attended import (
    AttendedActionExecutor,
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedDecision,
    AttendedExecutionResult,
    AttendedPauseCapability,
    BoundAttendedExecutor,
    execute_attended_action,
)

_VIEWPORT = {"width": 1280, "height": 800}


class AttendedExecutorTimeout(RuntimeError):
    """The owner thread did not return a terminal execution receipt in time."""


@dataclass(frozen=True)
class _AttendedExecutorCommand:
    action: Literal["continue", "skip"]
    run_dir: Path
    capability: AttendedPauseCapability
    approval: ApprovalRecord
    future: Future[AttendedExecutionResult]


_OWNER_STOP = object()


class _ThreadOwnedAttendedExecutor:
    """Keep a thread-affine live backend on one synchronous owner thread."""

    def __init__(
        self,
        executor_context_factory: Callable[
            [], AbstractContextManager[AttendedActionExecutor]
        ],
        *,
        startup_timeout_s: float,
        action_timeout_s: float,
        shutdown_timeout_s: float,
    ) -> None:
        if min(startup_timeout_s, action_timeout_s, shutdown_timeout_s) <= 0:
            raise ValueError("attended executor timeouts must be positive")
        self._executor_context_factory = executor_context_factory
        self._startup_timeout_s = startup_timeout_s
        self._action_timeout_s = action_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._commands: queue.Queue[object] = queue.Queue()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._state_lock = threading.Lock()
        self._started = False
        self._closing = False
        self._failure: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run,
            name="openadapt-attended-executor",
            daemon=True,
        )

    @property
    def owner_thread_id(self) -> Optional[int]:
        """Diagnostic thread identity used by deterministic lifecycle tests."""
        return self._thread.ident

    def __enter__(self) -> "_ThreadOwnedAttendedExecutor":
        with self._state_lock:
            if self._started:
                raise RuntimeError("attended executor owner cannot be restarted")
            self._started = True
            self._thread.start()
        if not self._ready.wait(self._startup_timeout_s):
            self._request_stop()
            raise AttendedExecutorTimeout(
                "the deployment-bound attended executor did not become ready"
            )
        if self._failure is not None:
            self._thread.join(timeout=self._shutdown_timeout_s)
            raise self._failure
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def _request_stop(self) -> None:
        with self._state_lock:
            if not self._closing:
                self._closing = True
                self._commands.put(_OWNER_STOP)

    def close(self) -> None:
        if not self._started:
            return
        self._request_stop()
        if not self._stopped.wait(self._shutdown_timeout_s):
            raise AttendedExecutorTimeout(
                "the deployment-bound attended executor did not close in time"
            )
        self._thread.join(timeout=0)
        if self._failure is not None:
            raise self._failure

    def _fail_queued(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return
            if item is _OWNER_STOP:
                continue
            command = cast(_AttendedExecutorCommand, item)
            if not command.future.done():
                command.future.set_exception(exc)

    def _run(self) -> None:
        current: Optional[_AttendedExecutorCommand] = None
        try:
            with self._executor_context_factory() as executor:
                self._ready.set()
                while True:
                    item = self._commands.get()
                    if item is _OWNER_STOP:
                        break
                    command = cast(_AttendedExecutorCommand, item)
                    current = command
                    if not command.future.set_running_or_notify_cancel():
                        current = None
                        continue
                    try:
                        method = (
                            executor.continue_run
                            if command.action == "continue"
                            else executor.skip_run
                        )
                        command.future.set_result(
                            method(
                                command.run_dir,
                                command.capability,
                                command.approval,
                            )
                        )
                    except Exception as exc:
                        command.future.set_exception(exc)
                    finally:
                        current = None
        except BaseException as exc:
            self._failure = exc
            if current is not None and not current.future.done():
                current.future.set_exception(exc)
            self._fail_queued(exc)
        finally:
            self._ready.set()
            self._stopped.set()

    def _submit(
        self,
        action: Literal["continue", "skip"],
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        future: Future[AttendedExecutionResult] = Future()
        command = _AttendedExecutorCommand(
            action=action,
            run_dir=run_dir,
            capability=capability,
            approval=approval,
            future=future,
        )
        with self._state_lock:
            if not self._started or self._closing:
                raise RuntimeError("the attended executor owner is not accepting work")
            if self._failure is not None:
                raise self._failure
            self._commands.put(command)
        try:
            return future.result(timeout=self._action_timeout_s)
        except FutureTimeoutError as exc:
            if future.done():
                return future.result()
            if future.cancel():
                return AttendedExecutionResult(
                    status="refused",
                    message=(
                        "the qualified live application session remained busy; "
                        "this request was cancelled before execution began"
                    ),
                    report_success=False,
                    resumed_from=capability.step_id,
                    next_transition=capability.expected_next_transition,
                )
            raise AttendedExecutorTimeout(
                "the deployment-bound action did not return a terminal receipt "
                "before the attended executor deadline"
            ) from exc

    def continue_run(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        return self._submit("continue", run_dir, capability, approval)

    def skip_run(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        return self._submit("skip", run_dir, capability, approval)


@contextmanager
def _deployment_executor(
    deployment: DeploymentConfig,
    *,
    key: Optional[str],
):
    """Construct and close the qualified backend on the current owner thread."""
    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.deployment import (
        build_api_actuator,
        build_effect_verifier,
        build_replayer,
    )

    def bound_executor(backend: Any) -> BoundAttendedExecutor:
        def replayer_for_manifest(manifest: Any) -> Any:
            try:
                effect_verifier = build_effect_verifier(
                    deployment.effects, params=dict(manifest.params)
                )
                api_actuator = build_api_actuator(deployment.actuation)
            except ValueError as exc:
                raise AttendedActionRefused(str(exc)) from exc
            return build_replayer(
                backend,
                allow_egress=deployment.runtime.allow_model_grounding,
                effect_verifier=effect_verifier,
                api_actuator=api_actuator,
                durable=True,
                use_structural=True,
                pixel_verify_enabled=deployment.runtime.pixel_verify_enabled,
                governed_authorization=manifest.governed_authorization,
                runtime_config=deployment.runtime,
            )

        return BoundAttendedExecutor(replayer_for_manifest, key=key)

    backend_cfg = deployment.backend
    if _normalize_kind(backend_cfg.kind) == "web":
        if not backend_cfg.url:
            raise ValueError("attended web actions require deployment.backend.url")
        if not backend_cfg.headed:
            raise ValueError(
                "attended web actions require deployment.backend.headed: true"
            )

        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                page = browser.new_page(viewport=cast(Any, _VIEWPORT))
                page.goto(backend_cfg.url)
                yield bound_executor(build_backend(backend_cfg, page=page))
            finally:
                browser.close()
        return

    backend = build_backend(backend_cfg)
    try:
        yield bound_executor(backend)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


class AttendedActionService:
    """Persistent exact-capability attended-action service.

    Construction takes only the public deployment schema. Entering the context
    starts one dedicated owner thread and attaches one visible authenticated
    application session. :meth:`execute` retains the engine's signed-capability,
    caller-identity, idempotency, revalidation, receipt, and no-re-actuation
    semantics. A timeout raises so the durable decision log records delivery as
    uncertain and blocks automatic retry.
    """

    def __init__(
        self,
        deployment: DeploymentConfig,
        *,
        key: Optional[str] = None,
        startup_timeout_s: float = 30.0,
        action_timeout_s: float = 300.0,
        shutdown_timeout_s: float = 30.0,
    ) -> None:
        self._deployment = deployment.model_copy(deep=True)
        self._key = key
        self._owner = _ThreadOwnedAttendedExecutor(
            lambda: _deployment_executor(self._deployment, key=self._key),
            startup_timeout_s=startup_timeout_s,
            action_timeout_s=action_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
        )
        self._entered = False

    def __enter__(self) -> "AttendedActionService":
        self._owner.__enter__()
        self._entered = True
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def execute(
        self,
        run_dir: Path | str,
        request: AttendedActionRequest,
        *,
        operator: str,
    ) -> AttendedDecision:
        """Admit and execute one exact attended decision on the live session."""
        if not self._entered:
            raise RuntimeError("attended action service is not open")
        return execute_attended_action(
            run_dir,
            request,
            operator=operator,
            executor=self._owner,
            key=self._key,
        )

    def close(self) -> None:
        """Close the deployment backend on its owner thread."""
        self._owner.close()
        self._entered = False
