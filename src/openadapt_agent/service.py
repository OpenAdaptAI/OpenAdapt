"""``run_workflow`` and ``get_run``: one request, one durable run, one result.

The service validates a request, asks :class:`~openadapt_agent.runs.RunStore`
whether it starts a new attempt or replays an earlier one, runs new attempts on
a worker thread, and waits up to ``wait_seconds`` for the result. A run that is
still going returns ``outcome: "running"`` with its ``run_id``; ``get_run``
picks it up later, from this process or after a restart.

Engines do the work. :class:`FlowCliEngine` shells out to the governed
``openadapt-flow run`` CLI for an operator's bundles. The sandbox engine in
:mod:`openadapt_agent.sandbox` drives Flow against a synthetic app.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from openadapt_agent.cards import WorkflowCard, coerce_inputs, input_problems
from openadapt_agent.contract import REASONS, build_result, reason_for_run
from openadapt_agent.runner import FlowRunner, is_safe_run_id
from openadapt_agent.runs import RunStore, pid_alive

__all__ = [
    "REQUEST_ID_PATTERN",
    "attention_for",
    "reason_from_run_dir",
    "CatalogEntry",
    "EngineResult",
    "FlowCliEngine",
    "RunService",
    "ServiceError",
    "MAX_WAIT_SECONDS",
    "DEFAULT_WAIT_SECONDS",
]

_LOG = logging.getLogger(__name__)
#: The caller's id for one piece of work: no spaces, so free text can't ride in it.
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$"
REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
MAX_WAIT_SECONDS = 600
DEFAULT_WAIT_SECONDS = 60
#: Outcomes a person can still change after the run reports.
_REFRESHABLE = {"needs_review"}


class ServiceError(Exception):
    """A request the service can't turn into a run record (bad run_id)."""


@dataclass
class EngineResult:
    """What an engine learned from one attempt. Closed values only."""

    reason: str
    execution_outcome: Optional[str] = None
    transaction_outcome: Optional[str] = None
    failed_checks: list[str] = field(default_factory=list)
    needs_attention_id: Optional[str] = None
    model_calls: Optional[int] = None
    seconds: Optional[float] = None
    sandbox: Optional[dict[str, str]] = None


class Engine(Protocol):
    def execute(
        self, entry: "CatalogEntry", run_id: str, inputs: dict[str, str], raw: Mapping[str, Any]
    ) -> EngineResult: ...

    def refresh(self, entry: "CatalogEntry", run_id: str) -> Optional[EngineResult]: ...


@dataclass
class CatalogEntry:
    """One workflow the server can list and, when allowed, run."""

    card: WorkflowCard
    engine: Any
    available: bool = True
    info: Any = None  # bundles.WorkflowInfo for an operator bundle
    legacy_id: Optional[str] = None

    @property
    def name(self) -> str:
        return self.card.name


#: What a failed attention lookup stands for: a pause may be open, so the
#: run waits for a person instead of inviting a retry.
_UNKNOWN_PAUSE = {"status": "pending", "durably_paused": True, "category": None}


def attention_for(attended: Any, run_dir: Any) -> Optional[dict[str, Any]]:
    """Flow's Needs Attention projection for a run, failing toward caution."""
    try:
        return attended.for_run_dir(run_dir)
    except Exception:
        _LOG.exception("could not read the Needs Attention state for a run")
        return dict(_UNKNOWN_PAUSE)


class FlowCliEngine:
    """Run an operator bundle through the governed ``openadapt-flow run`` CLI."""

    def __init__(self, runner: FlowRunner, attended: Any) -> None:
        self.runner = runner
        self.attended = attended

    def execute(
        self, entry: CatalogEntry, run_id: str, inputs: dict[str, str], raw: Mapping[str, Any]
    ) -> EngineResult:
        outcome = self.runner.run(
            workflow=entry.legacy_id or entry.name,
            bundle_dir=entry.info.bundle_dir,
            params=inputs,
            run_id=run_id,
        )
        attention = None
        if outcome.status == "halt":
            attention = attention_for(self.attended, outcome.run_dir)
            outcome.apply_attention(attention)
        summary = outcome.summary if isinstance(outcome.summary, dict) else {}
        total_ms = summary.get("total_ms")
        return EngineResult(
            reason=outcome.contract_reason(),
            execution_outcome=outcome.execution_outcome,
            transaction_outcome=outcome.transaction_outcome,
            failed_checks=list(outcome.failed_checks),
            needs_attention_id=(attention or {}).get("id"),
            model_calls=summary.get("model_calls"),
            seconds=total_ms / 1000.0 if isinstance(total_ms, (int, float)) else None,
        )

    def refresh(self, entry: CatalogEntry, run_id: str) -> Optional[EngineResult]:
        """Re-read a reviewed run: a person may have continued or ended it."""
        return reason_from_run_dir(self.attended, Path(self.runner.config.runs_dir) / run_id)


def _owned_elsewhere(record: Mapping[str, Any]) -> bool:
    """Whether another live process may still be running this record.

    A record from another computer (a shared runs directory) can't be
    checked from here, so it counts as running rather than being overwritten.
    """
    host = record.get("host")
    if isinstance(host, str) and host and host != socket.gethostname():
        return True
    pid = record.get("pid")
    return pid != os.getpid() and pid_alive(pid)


def reason_from_run_dir(attended: Any, run_dir: Path) -> Optional[EngineResult]:
    """Re-derive a run's result from Flow's report and review state on disk."""
    report_path = run_dir / "report.json"
    if run_dir.is_symlink() or report_path.is_symlink() or not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    attention = attention_for(attended, run_dir)
    transaction = report.get("transaction_outcome")
    execution = report.get("execution_outcome")
    return EngineResult(
        reason=reason_for_run(exit_code=None, report=report, attention=attention),
        execution_outcome=execution if isinstance(execution, str) else None,
        transaction_outcome=transaction if isinstance(transaction, str) else None,
        needs_attention_id=(attention or {}).get("id"),
    )


class RunService:
    """Validate, deduplicate, run, and report workflow requests."""

    def __init__(
        self,
        store: RunStore,
        catalog: Mapping[str, CatalogEntry],
        *,
        mode: str,
        allow_run: bool,
        require_all_inputs: bool = True,
    ) -> None:
        self.store = store
        self.catalog = dict(catalog)
        self.mode = mode
        self.allow_run = allow_run
        self.require_all_inputs = require_all_inputs
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # Reserving a request and marking its run active happen together, so a
        # concurrent call with the same request_id never finds the new run
        # unowned in the moment before its worker starts.
        self._begin_lock = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _result(self, reason: str, record: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        return build_result(
            reason,
            workflow=record.get("workflow"),
            run_id=record.get("run_id"),
            request_id=record.get("request_id"),
            mode=self.mode,
            **extra,
        )

    def _refuse(
        self,
        reason: str,
        *,
        request_id: Optional[str],
        workflow: Optional[str],
        **extra: Any,
    ) -> dict[str, Any]:
        record = self.store.new_record(request_id=request_id, workflow=workflow, mode=self.mode)
        result = self._result(reason, record, **extra)
        self.store.save_refusal(record, result)
        return result

    @staticmethod
    def _wait_seconds(value: Any, default: int) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float(default)
        return float(min(max(value, 0), MAX_WAIT_SECONDS))

    # -- run_workflow ----------------------------------------------------------

    def run(
        self,
        workflow: Any,
        inputs: Any,
        request_id: Any,
        wait_seconds: Any = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise ServiceError(
                "request_id must be 4 to 128 letters, digits, '.', '_', ':' or '-'. "
                "Nothing ran."
            )
        wait = self._wait_seconds(wait_seconds, DEFAULT_WAIT_SECONDS)
        entry = self.catalog.get(workflow) if isinstance(workflow, str) else None
        if entry is None:
            # Never echo an unknown workflow name back: it is caller text.
            return self._refuse("unknown_workflow", request_id=request_id, workflow=None)
        if not entry.available:
            return self._refuse("workflow_unavailable", request_id=request_id, workflow=entry.name)
        if not self.allow_run:
            return self._refuse("runs_disabled", request_id=request_id, workflow=entry.name)
        inputs = {} if inputs is None else inputs
        if isinstance(inputs, Mapping):
            # Fill declared defaults so "omitted" and "sent the default" are
            # the same request for request_id purposes.
            defaults = {
                name: schema["default"]
                for name, schema in entry.card.inputs.items()
                if "default" in schema and name not in inputs
            }
            inputs = {**defaults, **inputs}
        problems = input_problems(entry.card, inputs, require_all=self.require_all_inputs)
        if problems:
            return self._refuse(
                "invalid_input",
                request_id=request_id,
                workflow=entry.name,
                invalid_inputs=problems,
            )
        event = threading.Event()
        with self._begin_lock:
            begin = self.store.begin(
                request_id=request_id, workflow=entry.name, inputs=inputs, mode=self.mode
            )
            if begin.kind == "new":
                with self._lock:
                    self._active[begin.run_id] = event
        if begin.kind == "conflict":
            return self._refuse(
                "request_id_conflict",
                request_id=request_id,
                workflow=entry.name,
                first_run_id=begin.first_run_id,
            )
        if begin.kind == "retry_limit":
            return self._refuse("retry_limit", request_id=request_id, workflow=entry.name)
        if begin.kind == "replay":
            if begin.record is None and self.store.read(begin.run_id) is None:
                # Another process won the reservation and hasn't written its
                # record yet. It is running; never start a second attempt.
                placeholder = {
                    "run_id": begin.run_id,
                    "request_id": request_id,
                    "workflow": entry.name,
                }
                return {**self._result("running", placeholder), "replayed": True}
            return self.get(begin.run_id, wait_seconds=wait, replayed=True)
        try:
            self._start(entry, begin.record, coerce_inputs(entry.card, inputs), inputs, event)
        except BaseException:
            # No worker owns the run, so get_run reports it as interrupted
            # (check the record), never as running forever.
            with self._lock:
                self._active.pop(begin.run_id, None)
            event.set()
            raise
        return self.get(begin.run_id, wait_seconds=wait)

    def _start(
        self,
        entry: CatalogEntry,
        record: dict[str, Any],
        rendered: dict[str, str],
        raw: Mapping[str, Any],
        event: threading.Event,
    ) -> None:
        """Run one reserved attempt on a worker; ``run`` already marked it active."""
        run_id = record["run_id"]

        def work() -> None:
            try:
                try:
                    outcome = entry.engine.execute(entry, run_id, rendered, raw)
                except Exception:
                    # The engine may have acted before it failed: uncertain.
                    _LOG.exception("workflow engine failed inside the protected boundary")
                    outcome = EngineResult(reason="result_unreadable")
                result = self._result(
                    outcome.reason,
                    record,
                    execution_outcome=outcome.execution_outcome,
                    transaction_outcome=outcome.transaction_outcome,
                    failed_checks=outcome.failed_checks,
                    needs_attention_id=outcome.needs_attention_id,
                    model_calls=outcome.model_calls,
                    seconds=outcome.seconds,
                    sandbox=outcome.sandbox,
                )
                self.store.finish(
                    record, result, transaction_outcome=outcome.transaction_outcome
                )
            except Exception:
                _LOG.exception("could not persist a run result")
            finally:
                with self._lock:
                    self._active.pop(run_id, None)
                event.set()

        thread = threading.Thread(target=work, name=f"openadapt-run-{run_id}", daemon=True)
        thread.start()

    # -- get_run -----------------------------------------------------------

    def get(
        self, run_id: Any, wait_seconds: Any = DEFAULT_WAIT_SECONDS, *, replayed: bool = False
    ) -> dict[str, Any]:
        if not isinstance(run_id, str) or not is_safe_run_id(run_id):
            raise ServiceError("run_id must be a run id returned by run_workflow")
        with self._lock:
            event = self._active.get(run_id)
        if event is not None:
            event.wait(self._wait_seconds(wait_seconds, DEFAULT_WAIT_SECONDS))
        # Check activity BEFORE reading the record. A worker writes its result
        # and only then leaves the active set, so "not active" followed by an
        # unfinished record really means nobody in this process is running it.
        with self._lock:
            active = run_id in self._active
        record = self.store.read(run_id)
        if record is None:
            raise ServiceError("no run with that run_id on this server")
        if record.get("state") != "finished":
            other_process = _owned_elsewhere(record)
            if active or other_process:
                result = self._result("running", record)
                if replayed:
                    result["replayed"] = True
                return result
            # The process that ran it is gone without a result. A write may
            # have landed, so this is uncertain and must never be retried.
            interrupted = self._result("interrupted", record)
            record = self.store.finish(record, interrupted)
        result = dict(record.get("result") or {})
        if result.get("outcome") in _REFRESHABLE:
            result = self._refresh(record, result)
        if replayed:
            result["replayed"] = True
        return result

    def _refresh(self, record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        entry = self.catalog.get(record.get("workflow") or "")
        refresh = getattr(getattr(entry, "engine", None), "refresh", None)
        if entry is None or refresh is None:
            return result
        try:
            update = refresh(entry, record["run_id"])
        except Exception:
            _LOG.exception("could not refresh a reviewed run")
            return result
        if update is None or update.reason == result.get("reason") or update.reason not in REASONS:
            return result
        if REASONS[update.reason].safe_to_retry:
            # A reviewed run only leaves review through a person's decision.
            # Flow clears a continued pause just before it replaces the
            # pre-resume report, so a no-effect report with no pause can be
            # stale. Keep waiting rather than invite a second write.
            return result
        fresh = self._result(
            update.reason,
            record,
            execution_outcome=update.execution_outcome,
            transaction_outcome=update.transaction_outcome,
            needs_attention_id=update.needs_attention_id,
            sandbox=result.get("sandbox"),
        )
        self.store.update_result(record, fresh)
        return fresh
