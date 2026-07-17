"""Transport-agnostic bridge: tool specs + dispatch over discovered bundles.

Kept free of ``mcp`` imports so the tool surface (schema generation,
gating, outcome mapping) is unit-testable without an MCP transport. The
thin MCP wiring lives in :mod:`openadapt_agent.mcp`.

Safety model (documented in ``docs/DESIGN.md``):

- Read-only tools (``list_workflows`` / ``get_workflow`` /
  ``get_run_report``) are always registered.
- ``run_<slug>`` tools are registered ONLY when the operator started the
  server with ``--allow-run``; even then, every call shells out to the
  governed ``openadapt-flow run`` CLI, so flow's fail-closed admission
  gates cannot be bypassed from here.
- The MCP client's user is the operator of record for anything a run
  writes; the server itself performs no authentication.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openadapt_agent.bundles import (
    WorkflowInfo,
    discover_bundles,
    tool_input_schema,
)
from openadapt_agent.runner import FlowRunner, RunnerConfig, is_safe_run_id

__all__ = ["AgentBridge", "BridgeError", "ToolSpec"]

_EXPERIMENTAL_NOTE = (
    "openadapt-agent is EXPERIMENTAL (v2.0.0.dev0): unproven in production."
)


class BridgeError(Exception):
    """A tool-level error the transport should surface to the caller."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


class AgentBridge:
    """Expose a directory of compiled workflow bundles as agent tools."""

    def __init__(
        self,
        bundles_dir: Path,
        runner_config: RunnerConfig,
        *,
        allow_run: bool = False,
        runner: Optional[FlowRunner] = None,
    ):
        self.bundles_dir = Path(bundles_dir)
        self.allow_run = allow_run
        self.runner_config = runner_config
        self.runner = runner or FlowRunner(runner_config)
        self.workflows: dict[str, WorkflowInfo] = {
            info.slug: info for info in discover_bundles(self.bundles_dir)
        }

    # -- tool surface ------------------------------------------------------

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="list_workflows",
                description=(
                    "List the compiled openadapt-flow workflows this server "
                    "exposes (name, parameters, step count, whether run tools "
                    f"are enabled). {_EXPERIMENTAL_NOTE}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="get_workflow",
                description=(
                    "Inspect one workflow: step intents, declared parameters "
                    "with recorded example values, and certification status "
                    "(evaluated via `openadapt-flow certify` when the server "
                    "was configured with a policy)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": "Workflow slug from list_workflows.",
                        }
                    },
                    "required": ["workflow"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="get_run_report",
                description=(
                    "Fetch the persisted report.json for a previous run made "
                    "through this server (by run_id from a run tool result). "
                    "This is the halt/success evidence trail."
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
            ),
        ]
        if self.allow_run:
            for slug, info in sorted(self.workflows.items()):
                if not info.ok:
                    continue
                n_steps = len(info.step_intents)
                specs.append(
                    ToolSpec(
                        name=f"run_{slug}",
                        description=(
                            f"Execute the compiled workflow {info.name!r} "
                            f"({n_steps} steps) via the governed "
                            "`openadapt-flow run` CLI (fail-closed admission "
                            "gates). Returns a structured outcome: status is "
                            "'success', 'halt' (run stopped with evidence in "
                            "report.json — NOT a success), 'refused' "
                            "(admission gate refused; nothing executed), "
                            "'timeout', or 'error'. Never treat a non-success "
                            "status as success."
                        ),
                        input_schema=tool_input_schema(
                            info,
                            allow_url_override=self.runner_config.allow_url_override,
                        ),
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
        if name.startswith("run_"):
            return self._run(name[len("run_") :], arguments)
        raise BridgeError(f"unknown tool: {name}")

    def _list_workflows(self) -> dict:
        return {
            "experimental": True,
            "run_tools_enabled": self.allow_run,
            "bundles_dir": str(self.bundles_dir),
            "workflows": [
                {
                    "slug": info.slug,
                    "name": info.name or None,
                    "steps": len(info.step_intents),
                    "params": info.params,
                    "encrypted": info.encrypted,
                    "load_error": info.load_error,
                }
                for info in self.workflows.values()
            ],
            "note": (
                "Run tools are disabled unless the operator started the "
                "server with --allow-run."
                if not self.allow_run
                else None
            ),
        }

    def _require_workflow(self, slug: str) -> WorkflowInfo:
        info = self.workflows.get(slug)
        if info is None:
            raise BridgeError(
                f"unknown workflow {slug!r}; call list_workflows for the "
                "available slugs"
            )
        return info

    def _get_workflow(self, slug: str) -> dict:
        info = self._require_workflow(slug)
        if not info.ok:
            return {
                "slug": info.slug,
                "bundle_dir": str(info.bundle_dir),
                "load_error": info.load_error,
            }
        return {
            "slug": info.slug,
            "name": info.name,
            "bundle_dir": str(info.bundle_dir),
            "schema_version": info.schema_version,
            "encrypted": info.encrypted,
            "params": info.params,
            "steps": info.step_intents,
            "certification": self.runner.certify(info.bundle_dir),
        }

    def _get_run_report(self, run_id: str) -> dict:
        if not run_id or not is_safe_run_id(run_id):
            raise BridgeError("run_id must be a single path component")
        run_dir = (Path(self.runner_config.runs_dir) / run_id).resolve()
        runs_root = Path(self.runner_config.runs_dir).resolve()
        if runs_root not in run_dir.parents:
            raise BridgeError("run_id resolves outside the server's runs directory")
        report_path = run_dir / "report.json"
        if not report_path.is_file():
            raise BridgeError(f"no report.json for run_id {run_id!r}")
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"report.json unreadable: {exc}") from exc
        return {"run_id": run_id, "run_dir": str(run_dir), "report": report}

    def _run(self, slug: str, arguments: dict) -> dict:
        info = self._require_workflow(slug)
        if not self.allow_run:
            raise BridgeError(
                "run tools are disabled: the operator did not start the "
                "server with --allow-run"
            )
        if not info.ok:
            raise BridgeError(
                f"workflow {slug!r} could not be loaded: {info.load_error}"
            )
        url_override = arguments.pop("url", None)
        unknown = set(arguments) - set(info.params)
        if unknown:
            raise BridgeError(
                f"unknown parameter(s) {sorted(unknown)!r}; declared "
                f"parameters: {sorted(info.params)!r}"
            )
        params = {k: str(v) for k, v in arguments.items()}
        outcome = self.runner.run(
            workflow=info.slug,
            bundle_dir=info.bundle_dir,
            params=params,
            url_override=url_override,
        )
        return outcome.to_dict()
