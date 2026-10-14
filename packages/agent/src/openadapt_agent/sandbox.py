"""The zero-flag sandbox: one synthetic workflow that shows every outcome.

``openadapt-agent serve`` with no flags serves one workflow,
``add_triage_note``, on MockMed, a synthetic clinic app that ships with
openadapt-flow. The caller picks a ``sandbox_case`` to see each outcome the
partner contract can return, the way a payments sandbox uses test card
numbers:

=====================  ===================  =========================================
sandbox_case           outcome              what the synthetic app does
=====================  ===================  =========================================
normal                 done                 saves the note; the record check finds it
duplicate_record       needs_review         lists the same referral twice; it stops
                                            before saving
false_saved_banner     not_sure_if_saved    shows "saved" but its record store
                                            rejected the note
timeout_after_save     not_sure_if_saved    saves the note, then reports an error
app_offline            did_not_run          isn't reachable, so nothing starts
=====================  ===================  =========================================

Two engines produce these results:

- ``flow``: openadapt-flow records the workflow once, then every run drives
  MockMed in a hidden browser under the standard profile, with the same run
  gate, right-record checks, and independent record-store read a production
  run uses. Needs the ``tutorial`` extra (Playwright and Chromium).
- ``simulated``: the same contract results without opening any app, for a
  plain install. Results say ``proof: "simulated"``.

Results always say ``mode: "sandbox"`` and name the engine. Nothing here
touches a real record.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.request import Request, urlopen

from openadapt_agent.cards import WorkflowCard
from openadapt_agent.contract import parse_failed_checks, reason_for_run
from openadapt_agent.service import (
    CatalogEntry,
    EngineResult,
    attention_for,
    reason_from_run_dir,
)

__all__ = [
    "CASES",
    "SANDBOX_WORKFLOW",
    "SandboxEngine",
    "flow_engine_available",
]

_LOG = logging.getLogger(__name__)

SANDBOX_WORKFLOW = "add_triage_note"
#: How long a first run waits for the synthetic app to be recorded and compiled.
PREPARE_TIMEOUT_S = 900.0
_APPROVAL_SOURCE = "openadapt-agent-sandbox"
_POLICY = "clinical-write"


@dataclass(frozen=True)
class SandboxCase:
    name: str
    outcome: str
    shows: str
    #: MockMed entry query: fault mode and optional drift. None = app offline.
    query: Optional[str]
    #: Outcome fields a real openadapt-flow 1.35.1 run produced for this case.
    execution: Optional[str] = None
    transaction: Optional[str] = None
    attention_category: Optional[str] = None


CASES: dict[str, SandboxCase] = {
    case.name: case
    for case in (
        SandboxCase(
            "normal",
            "done",
            "The app saves the note and the record check finds it in the app's record store.",
            "?fault=ok&idempotency=demo#tasks",
            "VERIFIED",
            "VERIFIED",
        ),
        SandboxCase(
            "duplicate_record",
            "needs_review",
            "The queue lists the same referral twice, so OpenAdapt can't be sure "
            "which record to open. It stops before saving.",
            "?fault=ok&idempotency=demo&drift=ambiguous#tasks",
            "HALTED",
            "HALTED_BEFORE_EFFECT",
            "disambiguation",
        ),
        SandboxCase(
            "false_saved_banner",
            "not_sure_if_saved",
            "The app shows a saved message, but its record store rejected the note. "
            "The record check doesn't find it.",
            "?fault=optimistic&idempotency=demo#tasks",
            "HALTED",
            "RECONCILIATION_REQUIRED",
            "effect_escalated",
        ),
        SandboxCase(
            "timeout_after_save",
            "not_sure_if_saved",
            "The record store saves the note, then the app times out and shows an "
            "error. Only a person checking the record can tell it saved.",
            "?fault=timeout&idempotency=demo#tasks",
            "HALTED",
            "RECONCILIATION_REQUIRED",
            "postcondition",
        ),
        SandboxCase(
            "app_offline",
            "did_not_run",
            "The app isn't reachable, so OpenAdapt doesn't start.",
            None,
        ),
    )
}


def _card() -> WorkflowCard:
    case_list = "; ".join(f"{case.name}: {case.outcome}" for case in CASES.values())
    return WorkflowCard(
        name=SANDBOX_WORKFLOW,
        purpose=(
            "Sandbox: add a triage note to the first referral in a synthetic clinic "
            "app. Nothing real changes."
        ),
        done_means=(
            "The note is in the synthetic app's record store, confirmed by reading "
            "the store directly instead of trusting the screen."
        ),
        inputs={
            "note": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "The triage note to add. Use made-up text.",
            },
            "sandbox_case": {
                "type": "string",
                "enum": list(CASES),
                "default": "normal",
                "description": f"Which situation the synthetic app shows ({case_list}).",
            },
        },
        required=["note"],
        changes_records=True,
        authored=True,
    )


def flow_engine_available() -> tuple[bool, str]:
    """Whether this install can drive MockMed with real openadapt-flow."""
    try:
        import importlib.util

        if importlib.util.find_spec("playwright.sync_api") is None:
            return False, "browser_extra_missing"
        from openadapt_flow import tutorial as flow_tutorial

        if "entry_query" not in inspect.signature(flow_tutorial.run_tutorial_workflow).parameters:
            return False, "flow_too_old"
        from openadapt_flow.run_gate import (  # noqa: F401
            build_runtime_authorization,
            evaluate_run_gate,
        )
        from openadapt_flow.runtime.effects import RestRecordVerifier  # noqa: F401
    except Exception:
        return False, "flow_unavailable"
    return True, "ok"


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reachable(base_url: str) -> bool:
    try:
        with urlopen(f"{base_url.rstrip('/')}/api/db", timeout=2.0) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _reset(base_url: str) -> None:
    request = Request(
        f"{base_url.rstrip('/')}/api/reset",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5.0):
        pass


class SandboxEngine:
    """Run the sandbox workflow and report through the partner contract."""

    def __init__(
        self,
        runs_dir: Path | str,
        *,
        engine: str = "auto",
        headed: bool = False,
        attended: Any = None,
    ) -> None:
        if engine not in {"auto", "flow", "simulated"}:
            raise ValueError("sandbox engine must be auto, flow, or simulated")
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.headed = headed
        self.unavailable_reason: Optional[str] = None
        if engine == "simulated":
            self.engine = "simulated"
        else:
            available, why = flow_engine_available()
            if available:
                self.engine = "flow"
            elif engine == "flow":
                raise RuntimeError(
                    "the flow sandbox engine needs the tutorial extra: "
                    "pip install 'openadapt-agent[tutorial]'"
                )
            else:
                self.engine = "simulated"
                self.unavailable_reason = why
        if attended is None:
            from openadapt_agent.attended import AttendedBridge

            attended = AttendedBridge(self.runs_dir)
        self.attended = attended
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._session: Any = None
        self._workflow: Any = None
        self._closed = False
        self._entry = CatalogEntry(card=_card(), engine=self)
        if self.engine == "flow":
            threading.Thread(
                target=self._prepare, name="openadapt-sandbox-prepare", daemon=True
            ).start()
        else:
            self._ready.set()

    # -- catalog -------------------------------------------------------------

    def entries(self) -> list[CatalogEntry]:
        return [self._entry]

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "engine": self.engine,
            "app": "MockMed, a synthetic clinic app with made-up patients",
            "ready": self._ready.is_set(),
            "cases": [
                {"case": case.name, "outcome": case.outcome, "what_it_shows": case.shows}
                for case in CASES.values()
            ],
        }
        if self.engine == "flow":
            info["engine_detail"] = (
                "Each run drives the synthetic app in a hidden browser with the real "
                "OpenAdapt engine. The first run waits while OpenAdapt records the "
                "workflow once."
            )
        else:
            info["engine_detail"] = (
                "Results are simulated without opening the app. To drive the "
                "synthetic app in a hidden browser, install the tutorial extra: "
                "uvx --python 3.12 --from 'openadapt-agent[tutorial]' openadapt-agent serve"
            )
        return info

    # -- preparation ---------------------------------------------------------

    def _prepare(self) -> None:
        try:
            from openadapt_flow.ir import Workflow

            from openadapt_agent.tutorial import prepare_tutorial_session

            session = prepare_tutorial_session(
                self.runs_dir / "sandbox-app", headed=self.headed, reuse_bundle=True
            )
            if self._closed:
                session.close()
                return
            self._session = session
            self._workflow = Workflow.load(session.bundle_dir)
        except Exception as exc:
            _LOG.warning(
                "the synthetic app could not start (%s); sandbox results are now simulated",
                type(exc).__name__,
            )
            self.engine = "simulated"
            self.unavailable_reason = "app_failed_to_start"
        finally:
            self._ready.set()

    def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                _LOG.warning("the synthetic app did not stop cleanly")

    # -- runs ------------------------------------------------------------------

    def _sandbox(self, case: SandboxCase, engine: Optional[str] = None) -> dict[str, str]:
        return {"case": case.name, "engine": engine or self.engine}

    def execute(
        self,
        entry: CatalogEntry,
        run_id: str,
        inputs: dict[str, str],
        raw: Mapping[str, Any],
    ) -> EngineResult:
        case = CASES[str(raw.get("sandbox_case") or "normal")]
        if self.engine == "flow" and not self._ready.wait(PREPARE_TIMEOUT_S):
            return EngineResult("platform_error", sandbox=self._sandbox(case))
        if self.engine != "flow":
            return self._simulate(case)
        return self._run_flow(case, run_id, {"note": inputs["note"]})

    def _simulate(self, case: SandboxCase) -> EngineResult:
        sandbox = self._sandbox(case, "simulated")
        if case.query is None:
            return EngineResult("app_unreachable", sandbox=sandbox)
        verified = case.execution == "VERIFIED"
        report = {
            "success": verified,
            "execution_outcome": case.execution,
            "transaction_outcome": case.transaction,
            "execution_profile": "standard",
            "production_eligible": verified,
            "model_calls": 0,
        }
        attention = (
            {"status": "pending", "durably_paused": True, "category": case.attention_category}
            if case.attention_category
            else None
        )
        return EngineResult(
            reason_for_run(exit_code=None, report=report, attention=attention),
            execution_outcome=case.execution,
            transaction_outcome=case.transaction,
            model_calls=0,
            sandbox=sandbox,
        )

    def _run_flow(self, case: SandboxCase, run_id: str, params: dict[str, str]) -> EngineResult:
        sandbox = self._sandbox(case, "flow")
        session = self._session
        if session is None or self._workflow is None:
            return EngineResult("platform_error", sandbox=sandbox)
        base_url = session.base_url or session.url.split("?", 1)[0]
        if case.query is None:
            base_url = f"http://127.0.0.1:{_closed_local_port()}/"
        started = time.monotonic()
        with self._lock:
            # Pre-flight: nothing has been sent to the app yet.
            if not _reachable(base_url):
                return EngineResult("app_unreachable", sandbox=sandbox)
            return self._governed_run(case, run_id, params, base_url, started, sandbox)

    def _governed_run(
        self,
        case: SandboxCase,
        run_id: str,
        params: dict[str, str],
        base_url: str,
        started: float,
        sandbox: dict[str, str],
    ) -> EngineResult:
        """Admit and run once, the way Flow's tutorial does, with the caller's note."""
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            ExecutionProfile,
            execution_profile_contract,
        )
        from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
        from openadapt_flow.runtime import Replayer
        from openadapt_flow.runtime.effects import RestRecordVerifier

        workflow = self._workflow
        bundle_dir = self._session.bundle_dir
        run_dir = self.runs_dir / run_id
        entry_url = f"{base_url.rstrip('/')}/{case.query}"
        try:
            _reset(base_url)
            verifier = RestRecordVerifier(
                base_url,
                records_path="/api/db",
                records_key="records",
                timeout_s=2.0,
                poll_interval_s=0.05,
            )
            gate = evaluate_run_gate(
                workflow,
                bundle_dir=bundle_dir,
                deployment=DeploymentConfig(policy=PolicySection(policy=_POLICY)),
                effect_verifier=verifier,
                profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
                effective_durable=True,
                effective_require_settled=True,
            )
            if not gate.passed:
                return EngineResult(
                    "not_ready_to_run",
                    failed_checks=parse_failed_checks(gate.render()),
                    sandbox=sandbox,
                )
            authorization = build_runtime_authorization(
                workflow, gate, approval_source=_APPROVAL_SOURCE, params=params
            )
            backend, close = PlaywrightBackend.launch(entry_url, headless=not self.headed)
        except Exception:
            # Nothing reached the app: the gate, the authorization, or the
            # browser launch failed before any action.
            _LOG.exception("sandbox run could not start")
            return EngineResult("platform_error", sandbox=sandbox)
        try:
            replayer = Replayer(
                backend,
                effect_verifier=verifier,
                governed_authorization=authorization,
                durable=True,
                require_settled=True,
            )
            report_model = replayer.run(
                workflow.model_copy(deep=True),
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                execution_target_kind="web",
                execution_origin=base_url.rstrip("/"),
                execution_entry_url=entry_url,
            )
        except Exception:
            _LOG.exception("sandbox run failed after it started")
            return EngineResult("result_unreadable", sandbox=sandbox)
        finally:
            try:
                close()
            except Exception:
                pass
        report = self._report(run_dir, report_model)
        verified = isinstance(report, dict) and report.get("transaction_outcome") == "VERIFIED"
        attention = None if verified else attention_for(self.attended, run_dir)
        model_calls = report.get("model_calls") if isinstance(report, dict) else None
        return EngineResult(
            reason_for_run(exit_code=None, report=report, attention=attention),
            execution_outcome=report.get("execution_outcome") if report else None,
            transaction_outcome=report.get("transaction_outcome") if report else None,
            needs_attention_id=(attention or {}).get("id") if attention else None,
            model_calls=model_calls if isinstance(model_calls, int) else None,
            seconds=time.monotonic() - started,
            sandbox=sandbox,
        )

    @staticmethod
    def _report(run_dir: Path, report_model: Any) -> Optional[dict[str, Any]]:
        path = run_dir / "report.json"
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text())
                if isinstance(value, dict):
                    return value
            except (OSError, ValueError):
                pass
        dump = getattr(report_model, "model_dump", None)
        if callable(dump):
            value = dump(mode="json")
            return value if isinstance(value, dict) else None
        return None

    def refresh(self, entry: CatalogEntry, run_id: str) -> Optional[EngineResult]:
        """Re-read a reviewed flow-engine run; simulated runs never change."""
        if self.engine != "flow":
            return None
        return reason_from_run_dir(self.attended, self.runs_dir / run_id)


def default_sandbox_dir() -> Path:
    """A stable per-user directory, so the sandbox never writes into a project."""
    base = os.environ.get("OPENADAPT_AGENT_SANDBOX_DIR")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".openadapt" / "agent-sandbox"
