"""Transport-agnostic bridge: tool specs + dispatch over discovered workflows.

Kept free of ``mcp`` imports so the tool surface (schema generation,
gating, outcome mapping) is unit-testable without an MCP transport. The
thin MCP wiring lives in :mod:`openadapt_agent.mcp`.

The partner contract is three tools:

- ``list_workflows``: names, purposes, what done means, typed inputs.
- ``run_workflow``: one request with the caller's ``request_id``; returns
  ``done``, ``needs_review``, ``not_sure_if_saved``, ``did_not_run``, or
  ``running`` with a ``run_id``.
- ``get_run``: the result of a run by ``run_id``.

Safety model (documented in ``docs/DESIGN.md``):

- Results come from :mod:`openadapt_agent.contract`, which reads Flow's
  ``transaction_outcome``. Only a verified write is ``done``; uncertain
  delivery is ``not_sure_if_saved`` and is never safe to retry.
- ``run_workflow`` is registered only in sandbox mode or when the operator
  enabled runs. Operator bundles always run through the governed
  ``openadapt-flow run`` CLI, so Flow's fail-closed admission gates apply.
- The same ``request_id`` never starts a second write (Flow's idempotency
  ledger, see :mod:`openadapt_agent.runs`).
- The older tools (``get_workflow``, ``get_run_report``, Needs Attention,
  attended actions, and per-workflow ``run_<id>``) stay registered for
  operator bundles so existing clients keep working.
- Remote authentication is outside this local stdio process. The local OS
  user is the operator of record for attended decisions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from openadapt_agent.attended import (
    ATTENDED_TOOLS,
    AttendedBridge,
    AttendedBridgeError,
    action_input_schema,
)
from openadapt_agent.bundles import (
    WorkflowInfo,
    discover_bundles,
    tool_input_schema,
)
from openadapt_agent.cards import card_for_bundle, inputs_schema
from openadapt_agent.contract import (
    RUN_RESULT_SCHEMA,
    WORKFLOW_LIST_SCHEMA,
    reason_for_run,
)
from openadapt_agent.runner import (
    FlowRunner,
    RunnerConfig,
    RunOutcome,
    classify_report_status,
    is_safe_run_id,
    public_report_summary,
    status_for_reason,
)
from openadapt_agent.runs import RunStore, read_record
from openadapt_agent.service import (
    DEFAULT_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    REQUEST_ID_PATTERN,
    CatalogEntry,
    FlowCliEngine,
    RunService,
    ServiceError,
    attention_for,
)

__all__ = ["AgentBridge", "BridgeError", "ToolSpec", "REQUEST_ID_PATTERN"]

_LOG = logging.getLogger(__name__)

_RUN_ID_PATTERN = r"^run-[A-Za-z0-9._-]{1,124}$"


class BridgeError(Exception):
    """A tool-level error the transport should surface to the caller."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    annotations: Optional[dict[str, Any]] = None
    meta: Optional[dict[str, Any]] = None
    output_schema: Optional[dict[str, Any]] = None
    #: Fixed guidance returned when the arguments don't match the schema.
    usage: Optional[str] = None


_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_RUN_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
#: run_workflow writes, but the same request_id never writes twice.
_RUN_WORKFLOW_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}
_DEPRECATED_META = {"deprecated": True, "use_instead": "run_workflow"}

_HOW_TO_RUN = (
    "Call run_workflow with a workflow name from this list, its inputs, and "
    "your own request_id for this piece of work. Only outcome done means the "
    "change was saved and checked. Never retry not_sure_if_saved. Reuse the "
    "same request_id when you retry anything."
)
_RUN_WORKFLOW_DESCRIPTION = (
    "Do one workflow in an app on this computer, for example enter a referral "
    "in a clinic's EMR, and check that it saved. Send the workflow name from "
    "list_workflows, its inputs, and your own request_id for this piece of "
    "work. The outcome is done (saved and checked), needs_review (stopped "
    "before saving; a person decides), not_sure_if_saved (a person must check "
    "the record; never retry), or did_not_run (nothing was written; fix it "
    "and retry when safe_to_retry is true). Reuse the same request_id when "
    "you retry: the same id never writes twice. If the outcome is running, "
    "call get_run with the run_id."
)
_RUN_WORKFLOW_USAGE = (
    "run_workflow needs workflow (a name from list_workflows), inputs (an "
    "object), and request_id (4 to 128 letters, digits, '.', '_', ':' or '-'). "
    "wait_seconds is optional, 0 to 600. Nothing ran."
)
_GET_RUN_USAGE = (
    "get_run needs run_id (the run_id from run_workflow). wait_seconds is "
    "optional, 0 to 600."
)


class AgentBridge:
    """Expose workflows as agent tools: operator bundles or the sandbox."""

    def __init__(
        self,
        bundles_dir: Optional[Path],
        runner_config: RunnerConfig,
        *,
        allow_run: bool = False,
        runner: Optional[FlowRunner] = None,
        allow_attended_actions: bool = False,
        attended_service: Optional[object] = None,
        attended: Optional[AttendedBridge] = None,
        allow_protected_export: bool = False,
        allow_recorded_defaults: bool = False,
        public_synthetic: bool = False,
        sandbox: Optional[Any] = None,
        mode: Optional[str] = None,
        store: Optional[RunStore] = None,
    ):
        self.bundles_dir = Path(bundles_dir) if bundles_dir is not None else None
        self.sandbox = sandbox
        self.allow_run = allow_run or sandbox is not None
        self.allow_protected_export = allow_protected_export
        self.allow_recorded_defaults = allow_recorded_defaults
        self.public_synthetic = public_synthetic
        self.runner_config = runner_config
        self.runner = runner or FlowRunner(runner_config)
        self.attended = attended or AttendedBridge(
            runner_config.runs_dir,
            allow_actions=allow_attended_actions,
            service=attended_service,
        )
        if mode is None:
            if sandbox is not None:
                mode = "sandbox"
            elif allow_attended_actions:
                mode = "attended"
            else:
                mode = "production"
        self.mode = mode
        #: Older per-bundle tools stay for operator bundles only.
        self.legacy_tools = sandbox is None
        self._store = store
        self._service: Optional[RunService] = None
        self.workflows: dict[str, WorkflowInfo] = {}
        if self.bundles_dir is not None:
            for info in discover_bundles(self.bundles_dir):
                base_id = info.slug if public_synthetic else info.public_id
                workflow_id = base_id
                suffix = 2
                while workflow_id in self.workflows:
                    workflow_id = f"{base_id}_{suffix}"
                    suffix += 1
                self.workflows[workflow_id] = info
        self.catalog: dict[str, CatalogEntry] = {}
        if sandbox is not None:
            for entry in sandbox.entries():
                self.catalog[entry.name] = entry
        else:
            engine = FlowCliEngine(self.runner, self.attended)
            for workflow_id, info in self.workflows.items():
                card = card_for_bundle(info, default_name=workflow_id)
                name = card.name
                suffix = 2
                while name in self.catalog:
                    name = f"{card.name}_{suffix}"
                    suffix += 1
                card.name = name
                self.catalog[name] = CatalogEntry(
                    card=card,
                    engine=engine,
                    available=info.ok,
                    info=info,
                    legacy_id=workflow_id,
                )
        self._entries_by_id = {
            entry.legacy_id: entry for entry in self.catalog.values() if entry.legacy_id
        }

    @property
    def service(self) -> RunService:
        """The run service, created on first use so read-only servers stay inert."""
        if self._service is None:
            store = self._store or RunStore(self.runner_config.runs_dir)
            self._service = RunService(
                store,
                self.catalog,
                mode=self.mode,
                allow_run=self.allow_run,
                require_all_inputs=not self.allow_recorded_defaults,
            )
        return self._service

    # -- tool surface ------------------------------------------------------

    def _contract_specs(self) -> list[ToolSpec]:
        names = sorted(self.catalog)
        specs = [
            ToolSpec(
                name="list_workflows",
                description=(
                    "List the workflows this computer can do: each one's name, "
                    "purpose, what done means, and its inputs. Start here, then "
                    "call run_workflow."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
                output_schema=WORKFLOW_LIST_SCHEMA,
            )
        ]
        if self.allow_run:
            description = _RUN_WORKFLOW_DESCRIPTION
            if self.sandbox is not None:
                description = (
                    "Sandbox: runs against a synthetic app, so no real record "
                    "changes. " + description
                )
            workflow_schema: dict[str, Any] = {
                "type": "string",
                "description": "A workflow name from list_workflows.",
            }
            if names:
                workflow_schema["enum"] = names
            specs.append(
                ToolSpec(
                    name="run_workflow",
                    description=description,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "workflow": workflow_schema,
                            "inputs": {
                                "type": "object",
                                "description": (
                                    "The inputs list_workflows shows for this workflow."
                                ),
                            },
                            "request_id": {
                                "type": "string",
                                "pattern": REQUEST_ID_PATTERN,
                                "description": (
                                    "Your own id for this piece of work, for "
                                    "example the referral id. Send the same id "
                                    "when you retry. Don't put patient details in it."
                                ),
                            },
                            "wait_seconds": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": MAX_WAIT_SECONDS,
                                "default": DEFAULT_WAIT_SECONDS,
                                "description": (
                                    "How long to wait for the result before "
                                    "returning outcome running."
                                ),
                            },
                        },
                        "required": ["workflow", "inputs", "request_id"],
                        "additionalProperties": False,
                    },
                    annotations=_RUN_WORKFLOW_ANNOTATIONS,
                    output_schema=RUN_RESULT_SCHEMA,
                    usage=_RUN_WORKFLOW_USAGE,
                )
            )
        specs.append(
            ToolSpec(
                name="get_run",
                description=(
                    "Get the result of a run started with run_workflow. Waits up "
                    "to wait_seconds while it is still running. Returns the same "
                    "result shape as run_workflow."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "pattern": _RUN_ID_PATTERN},
                        "wait_seconds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": MAX_WAIT_SECONDS,
                            "default": DEFAULT_WAIT_SECONDS,
                        },
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
                output_schema=RUN_RESULT_SCHEMA,
                usage=_GET_RUN_USAGE,
            )
        )
        return specs

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = self._contract_specs()
        if not self.legacy_tools:
            return specs
        specs += [
            ToolSpec(
                name="get_workflow",
                description=(
                    "Older tool. Inspect one workflow's PHI-safe structural "
                    "metadata and certification result by opaque id. Recorded "
                    "values, raw intents, names, paths, and exception text stay "
                    "local by default."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": "Opaque id from list_workflows.",
                        }
                    },
                    "required": ["workflow"],
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
            ),
            ToolSpec(
                name="get_run_report",
                description=(
                    "Older tool; get_run is preferred. Fetch a PHI-safe status "
                    "and count-only summary of a persisted run by opaque run id. "
                    "The raw report, observed text, local paths, stdout, and "
                    "stderr stay in the local operator experience unless "
                    "protected export was explicitly enabled when the server "
                    "started."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "run_id returned by a run tool.",
                        }
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
            ),
            ToolSpec(
                name="list_needs_attention",
                description=(
                    "List PHI-safe local halt cards and their exact currently "
                    "allowed attended actions. Raw workflow names, observed "
                    "text, parameters, reports, and filesystem paths are not "
                    "returned."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
            ),
            ToolSpec(
                name="get_attention_item",
                description=(
                    "Reload one PHI-safe halt card by its opaque id before an "
                    "operator decision. Use the newly returned capability "
                    "digest; stale capabilities are refused."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "attention_id": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{24}$",
                            "description": ("Opaque id returned by list_needs_attention."),
                        }
                    },
                    "required": ["attention_id"],
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
            ),
        ]
        for tool_name in self.attended.enabled_action_tools():
            tool = ATTENDED_TOOLS[tool_name]
            specs.append(
                ToolSpec(
                    name=tool_name,
                    description=tool.description,
                    input_schema=action_input_schema(tool),
                    annotations={
                        "readOnlyHint": False,
                        "destructiveHint": tool.action in {"continue", "skip", "reject"},
                        "idempotentHint": True,
                        "openWorldHint": tool.action in {"continue", "skip"},
                    },
                )
            )
        if self.allow_run:
            for workflow_id, info in sorted(self.workflows.items()):
                if not info.ok:
                    continue
                n_steps = len(info.step_intents)
                workflow_copy = (
                    f"the compiled workflow {info.name!r}"
                    if self.allow_protected_export
                    else "the selected compiled workflow"
                )
                specs.append(
                    ToolSpec(
                        name=f"run_{workflow_id}",
                        description=(
                            "Deprecated: use run_workflow with a request_id. "
                            f"Runs {workflow_copy} ({n_steps} steps) through the "
                            "governed `openadapt-flow run` CLI. Returns status "
                            "(success, halt, refused, timeout, or error) plus the "
                            "same outcome, safe_to_retry, and what_happened fields "
                            "as run_workflow. Only outcome done means the change was "
                            "saved and checked. Never retry not_sure_if_saved. This "
                            "tool takes no request_id, so a retry can write twice."
                        ),
                        input_schema=tool_input_schema(
                            info,
                            allow_url_override=self.runner_config.allow_url_override,
                            allow_recorded_defaults=self.allow_recorded_defaults,
                        ),
                        annotations=_RUN_ANNOTATIONS,
                        meta=_DEPRECATED_META,
                    )
                )
        return specs

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, name: str, arguments: Optional[dict]) -> dict:
        arguments = arguments or {}
        if name == "list_workflows":
            return self._list_workflows()
        if name == "run_workflow":
            if not self.allow_run:
                raise BridgeError(
                    "runs are disabled: the operator started this server without "
                    "--mode production or --mode attended"
                )
            try:
                return self.service.run(
                    arguments.get("workflow"),
                    arguments.get("inputs"),
                    arguments.get("request_id"),
                    arguments.get("wait_seconds", DEFAULT_WAIT_SECONDS),
                )
            except ServiceError as exc:
                raise BridgeError(str(exc)) from exc
        if name == "get_run":
            try:
                return self.service.get(
                    arguments.get("run_id"),
                    arguments.get("wait_seconds", DEFAULT_WAIT_SECONDS),
                )
            except ServiceError as exc:
                raise BridgeError(str(exc)) from exc
        if not self.legacy_tools:
            raise BridgeError("unknown tool name")
        if name == "get_workflow":
            return self._get_workflow(arguments.get("workflow", ""))
        if name == "get_run_report":
            return self._get_run_report(arguments.get("run_id", ""))
        if name == "list_needs_attention":
            return self.attended.list()
        if name == "get_attention_item":
            try:
                return self.attended.get(arguments.get("attention_id", ""))
            except AttendedBridgeError as exc:
                raise BridgeError(str(exc)) from exc
        if name in ATTENDED_TOOLS:
            try:
                return self.attended.act(name, arguments)
            except AttendedBridgeError as exc:
                raise BridgeError(str(exc)) from exc
        if name.startswith("run_"):
            return self._run(name[len("run_") :], arguments)
        raise BridgeError("unknown tool name")

    def _list_workflows(self) -> dict:
        workflows = []
        for name, entry in self.catalog.items():
            item = {
                **entry.card.projection(),
                "available": entry.available,
            }
            item["inputs"] = inputs_schema(entry.card, require_all=not self.allow_recorded_defaults)
            if entry.legacy_id is not None and entry.info is not None:
                item.update(self._workflow_projection(entry.legacy_id, entry.info, entry))
            workflows.append(item)
        result: dict[str, Any] = {
            "mode": self.mode,
            "run_tools_enabled": self.allow_run,
            "how_to_run": _HOW_TO_RUN,
            "workflows": workflows,
        }
        if self.sandbox is not None:
            result["sandbox"] = self.sandbox.describe()
            return result
        result.update(
            {
                "schema_version": 1,
                "lifecycle": "admission-derived",
                "protected_export_enabled": self.allow_protected_export,
                "synthetic_recorded_defaults_enabled": self.allow_recorded_defaults,
                "note": (
                    "Runs are disabled. The operator starts the server with "
                    "--mode production (or the older --allow-run) to enable them."
                    if not self.allow_run
                    else None
                ),
            }
        )
        if self.allow_protected_export:
            result["protected"] = {"bundles_dir": str(self.bundles_dir)}
        return result

    def _workflow_projection(
        self,
        workflow_id: str,
        info: WorkflowInfo,
        entry: Optional[CatalogEntry] = None,
    ) -> dict:
        if entry is None:
            entry = self._entries_by_id.get(workflow_id)
        properties = entry.card.inputs if entry is not None else {}
        result = {
            "id": workflow_id,
            "available": info.ok,
            "step_count": len(info.step_intents) if info.ok else None,
            "parameters": [
                {
                    "name": name,
                    "type": (properties.get(name) or {}).get("type", "string"),
                    "required": not self.allow_recorded_defaults,
                }
                for name in sorted(info.params)
            ],
            "encrypted": info.encrypted,
        }
        if self.allow_protected_export:
            result["protected"] = {
                "slug": info.slug,
                "name": info.name or None,
                "bundle_dir": str(info.bundle_dir),
                "recorded_params": info.params,
                "step_intents": info.step_intents,
                "load_error": info.load_error,
            }
        return result

    def _require_workflow(self, workflow_id: str) -> WorkflowInfo:
        info = self.workflows.get(workflow_id)
        if info is None:
            raise BridgeError("unknown workflow id; reload list_workflows")
        return info

    def _get_workflow(self, workflow_id: str) -> dict:
        info = self._require_workflow(workflow_id)
        result = self._workflow_projection(workflow_id, info)
        result.update(
            {
                "schema_version": info.schema_version,
                "certification": self._certification_projection(info),
            }
        )
        return result

    def _certification_projection(self, info: WorkflowInfo) -> dict:
        if not info.ok:
            if info.load_error:
                _LOG.warning(
                    "workflow %s could not be loaded locally: %s",
                    info.public_id,
                    info.load_error,
                )
            return {
                "certified": None,
                "message": "The local bundle could not be loaded safely.",
            }
        certification = self.runner.certify(info.bundle_dir)
        certified = certification.get("certified")
        if certified is True:
            message = "The configured policy certification passed."
        elif certified is False:
            message = "The configured policy certification did not pass."
        else:
            message = "Certification was not evaluated by this server."
        result = {"certified": certified, "message": message}
        if self.allow_protected_export:
            result["protected"] = certification
        return result

    def _get_run_report(self, run_id: str) -> dict:
        if not run_id or not is_safe_run_id(run_id):
            raise BridgeError("run_id must be a single path component")
        runs_candidate = Path(self.runner_config.runs_dir)
        run_candidate = runs_candidate / run_id
        if runs_candidate.is_symlink() or run_candidate.is_symlink():
            raise BridgeError("the configured run evidence boundary is unavailable")
        run_dir = run_candidate.resolve()
        runs_root = runs_candidate.resolve()
        if runs_root not in run_dir.parents:
            raise BridgeError("run_id resolves outside the server's runs directory")
        report_path = run_dir / "report.json"
        if report_path.is_symlink() or not report_path.is_file():
            record = read_record(self.runner_config.runs_dir, run_id)
            if record is not None and record.get("state") == "finished":
                return self._legacy_from_record(run_id, record)
            raise BridgeError("no local report exists for that run id")
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.exception("protected local report could not be read")
            raise BridgeError("the local report could not be read safely") from exc
        if not isinstance(report, dict):
            raise BridgeError("the local report has no trustworthy terminal structure")
        _status, execution_outcome = classify_report_status(report)
        transaction = report.get("transaction_outcome")
        reason = reason_for_run(
            exit_code=None,
            report=report,
            attention=attention_for(self.attended, run_dir),
        )
        outcome = RunOutcome(
            status=status_for_reason(reason),
            workflow="",
            run_id=run_id,
            execution_outcome=execution_outcome,
            transaction_outcome=transaction if isinstance(transaction, str) else None,
            reason=reason,
            summary=public_report_summary(report),
        )
        result = outcome.to_dict()
        result.pop("workflow_id", None)
        if self.allow_protected_export:
            result["protected"] = {
                "run_dir": str(run_dir),
                "report": report,
            }
        return result

    @staticmethod
    def _legacy_from_record(run_id: str, record: dict) -> dict:
        """Older get_run_report shape for a run that left no Flow report."""
        result = dict(record.get("result") or {})
        reason = str(result.get("reason") or "result_unreadable")
        technical = result.get("technical") or {}
        outcome = RunOutcome(
            status=status_for_reason(
                reason,
                refused=result.get("outcome") == "did_not_run"
                and not technical.get("transaction_outcome"),
            ),
            workflow="",
            run_id=run_id,
            reason=reason,
            failed_checks=list(result.get("failed_checks") or []),
            execution_outcome=technical.get("execution_outcome"),
            transaction_outcome=technical.get("transaction_outcome"),
        )
        payload = outcome.to_dict()
        payload.pop("workflow_id", None)
        return payload

    def _run(self, workflow_id: str, arguments: dict) -> dict:
        info = self._require_workflow(workflow_id)
        if not self.allow_run:
            raise BridgeError(
                "run tools are disabled: the operator did not start the server with --allow-run"
            )
        if not info.ok:
            if info.load_error:
                _LOG.warning(
                    "workflow %s could not be loaded locally: %s",
                    info.public_id,
                    info.load_error,
                )
            raise BridgeError("the selected workflow could not be loaded safely")
        arguments = dict(arguments)
        url_override = arguments.pop("url", None)
        unknown = set(arguments) - set(info.params)
        missing = set(info.params) - set(arguments)
        invalid_types = any(not isinstance(value, str) for value in arguments.values()) or (
            url_override is not None and not isinstance(url_override, str)
        )
        if unknown or (missing and not self.allow_recorded_defaults) or invalid_types:
            raise BridgeError("arguments do not match the declared workflow parameter schema")
        params = dict(arguments)
        outcome = self.runner.run(
            workflow=workflow_id,
            bundle_dir=info.bundle_dir,
            params=params,
            url_override=url_override,
        )
        attention = (
            attention_for(self.attended, outcome.run_dir) if outcome.status == "halt" else None
        )
        outcome.apply_attention(attention)
        result = outcome.to_dict(
            include_protected=self.allow_protected_export,
        )
        if outcome.status == "halt":
            result["needs_attention"] = attention
        return result
