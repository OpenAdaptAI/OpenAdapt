"""Transport-agnostic bridge: tool specs + dispatch over discovered bundles.

Kept free of ``mcp`` imports so the tool surface (schema generation,
gating, outcome mapping) is unit-testable without an MCP transport. The
thin MCP wiring lives in :mod:`openadapt_agent.mcp`.

Safety model (documented in ``docs/DESIGN.md``):

- PHI-safe read-only tools (workflow inspection, run summaries, and Needs
  Attention projections) are always registered.
- ``run_<opaque-id>`` tools are registered ONLY when the operator started the
  server with ``--allow-run``; even then, every call shells out to the
  governed ``openadapt-flow run`` CLI, so flow's fail-closed admission
  gates cannot be bypassed from here.
- Needs Attention projections are read-only and PHI-safe. Attended mutations
  are registered only under ``--allow-attended-actions`` and are submitted to
  Flow's signed capability/idempotency/audit contract.
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
from openadapt_agent.runner import (
    FlowRunner,
    RunnerConfig,
    is_safe_run_id,
    public_report_summary,
)

__all__ = ["AgentBridge", "BridgeError", "ToolSpec"]

_LOG = logging.getLogger(__name__)


class BridgeError(Exception):
    """A tool-level error the transport should surface to the caller."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    annotations: Optional[dict[str, Any]] = None


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


class AgentBridge:
    """Expose a directory of compiled workflow bundles as agent tools."""

    def __init__(
        self,
        bundles_dir: Path,
        runner_config: RunnerConfig,
        *,
        allow_run: bool = False,
        runner: Optional[FlowRunner] = None,
        allow_attended_actions: bool = False,
        attended_service: Optional[object] = None,
        attended: Optional[AttendedBridge] = None,
        allow_protected_export: bool = False,
        allow_recorded_defaults: bool = False,
    ):
        self.bundles_dir = Path(bundles_dir)
        self.allow_run = allow_run
        self.allow_protected_export = allow_protected_export
        self.allow_recorded_defaults = allow_recorded_defaults
        self.runner_config = runner_config
        self.runner = runner or FlowRunner(runner_config)
        self.attended = attended or AttendedBridge(
            runner_config.runs_dir,
            allow_actions=allow_attended_actions,
            service=attended_service,
        )
        self.workflows: dict[str, WorkflowInfo] = {}
        for info in discover_bundles(self.bundles_dir):
            base_id = info.public_id
            workflow_id = base_id
            suffix = 2
            while workflow_id in self.workflows:
                workflow_id = f"{base_id}_{suffix}"
                suffix += 1
            self.workflows[workflow_id] = info

    # -- tool surface ------------------------------------------------------

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="list_workflows",
                description=(
                    "List workflows by opaque id with parameter names/types, "
                    "step count, availability, and whether run tools are "
                    "enabled. Demonstration labels, values, intents, paths, "
                    "and load exceptions stay local by default."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY_ANNOTATIONS,
            ),
            ToolSpec(
                name="get_workflow",
                description=(
                    "Inspect one workflow's PHI-safe structural metadata and "
                    "certification result by opaque id. Recorded values, raw "
                    "intents, names, paths, and exception text stay local by "
                    "default."
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
                    "Fetch a PHI-safe status and count-only summary of a "
                    "persisted run by opaque run id. The raw report, observed "
                    "text, local paths, stdout, and stderr stay in the local "
                    "operator experience unless protected export was "
                    "explicitly enabled when the server started."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "run_id returned by a run_* tool.",
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
                        "destructiveHint": tool.action in {"continue", "skip"},
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
                            f"Execute {workflow_copy} ({n_steps} steps) via the governed "
                            "`openadapt-flow run` CLI (fail-closed admission "
                            "gates). Returns a PHI-safe structured outcome: "
                            "status is 'success', 'halt' (run stopped with "
                            "protected evidence retained locally — NOT a "
                            "success), 'refused' "
                            "(admission gate refused; nothing executed), "
                            "'timeout', or 'error'. Never treat a non-success "
                            "status as success."
                        ),
                        input_schema=tool_input_schema(
                            info,
                            allow_url_override=self.runner_config.allow_url_override,
                            allow_recorded_defaults=self.allow_recorded_defaults,
                        ),
                        annotations=_RUN_ANNOTATIONS,
                    )
                )
        return specs

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, name: str, arguments: Optional[dict]) -> dict:
        arguments = arguments or {}
        if name == "list_workflows":
            return self._list_workflows()
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
        result = {
            "schema_version": 1,
            "lifecycle": "beta",
            "run_tools_enabled": self.allow_run,
            "protected_export_enabled": self.allow_protected_export,
            "synthetic_recorded_defaults_enabled": self.allow_recorded_defaults,
            "workflows": [
                self._workflow_projection(workflow_id, info)
                for workflow_id, info in self.workflows.items()
            ],
            "note": (
                "Run tools are disabled unless the operator started the server with --allow-run."
                if not self.allow_run
                else None
            ),
        }
        if self.allow_protected_export:
            result["protected"] = {"bundles_dir": str(self.bundles_dir)}
        return result

    def _workflow_projection(
        self,
        workflow_id: str,
        info: WorkflowInfo,
    ) -> dict:
        result = {
            "id": workflow_id,
            "available": info.ok,
            "step_count": len(info.step_intents) if info.ok else None,
            "parameters": [
                {
                    "name": name,
                    "type": "string",
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
            raise BridgeError("no local report exists for that run id")
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.exception("protected local report could not be read")
            raise BridgeError("the local report could not be read safely") from exc
        success = report.get("success")
        status = "success" if success is True else ("halt" if success is False else "error")
        messages = {
            "success": "The persisted local report confirms success.",
            "halt": (
                "The persisted local report confirms the run did not complete. "
                "Review protected evidence in the local operator experience."
            ),
            "error": (
                "The persisted local report has no trustworthy terminal status. Review it locally."
            ),
        }
        result = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "success": status == "success",
            "message": messages[status],
            "summary": public_report_summary(report),
        }
        if self.allow_protected_export:
            result["protected"] = {
                "run_dir": str(run_dir),
                "report": report,
            }
        return result

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
        result = outcome.to_dict(
            include_protected=self.allow_protected_export,
        )
        if outcome.status == "halt":
            result["needs_attention"] = self.attended.for_run_dir(outcome.run_dir)
        return result
