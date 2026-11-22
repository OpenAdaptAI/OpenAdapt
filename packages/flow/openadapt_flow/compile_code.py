"""Compile demonstrated ``openadapt-flow`` workflows into readable Python code.

Generates a deterministic, git-diffable, AST-validated Python module exposing
``run_workflow(params, *, verify=True, receipt_dir=None) -> CommitResult`` with
parameter validation, deterministic ``openadapt_flow`` replay + read-back
verification, signed ``record/run/v1`` receipt handling, and a non-sensitive
``memory_log.md`` append hook.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class CommitResult:
    """Result of executing a compiled workflow module."""

    workflow_name: str
    outcome: str
    verified: bool
    steps_executed: int
    bundle_sha256: str
    receipt_path: Optional[str] = None
    receipt: Optional[dict[str, Any]] = None
    memory_log_path: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "outcome": self.outcome,
            "verified": self.verified,
            "steps_executed": self.steps_executed,
            "bundle_sha256": self.bundle_sha256,
            "receipt_path": self.receipt_path,
            "memory_log_path": self.memory_log_path,
            "reason": self.reason,
        }


def _safe_module_name(name: str) -> str:
    cleaned = _IDENT_RE.sub("_", name.strip().lower()).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"workflow_{cleaned}" if cleaned else "workflow_module"
    return cleaned


def _canonical_digest(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_spec(
    bundle_or_spec: Any,
    *,
    bundle_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Normalize a bundle path, ``Workflow`` IR, or mapping into a pure spec dict."""
    resolved_bundle_path = bundle_path
    if isinstance(bundle_or_spec, (str, Path)):
        path = Path(bundle_or_spec)
        if path.is_dir():
            resolved_bundle_path = path
            from openadapt_flow.ir import Workflow

            bundle_or_spec = Workflow.load(path)
        elif path.is_file():
            resolved_bundle_path = path.parent
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"workflow spec file must be a JSON object: {path}")
            bundle_or_spec = raw
        else:
            raise FileNotFoundError(f"workflow bundle or spec not found: {path}")

    if isinstance(bundle_or_spec, Mapping):
        data = dict(bundle_or_spec)
    elif hasattr(bundle_or_spec, "model_dump"):
        data = bundle_or_spec.model_dump(mode="json")
    else:
        raise TypeError(
            f"unsupported workflow input type: {type(bundle_or_spec).__name__}"
        )

    name = str(data.get("name") or "compiled_workflow").strip()
    if not name:
        name = "compiled_workflow"
    schema_version = int(data.get("schema_version") or 1)

    raw_params = data.get("params") or ()
    params_list: list[dict[str, Any]] = []
    if isinstance(raw_params, Mapping):
        for pname, pval in sorted(raw_params.items()):
            pinfo = dict(pval) if isinstance(pval, Mapping) else {}
            params_list.append(
                {
                    "name": str(pname),
                    "kind": str(pinfo.get("kind") or "string"),
                    "required": bool(pinfo.get("required", True)),
                    "secret": bool(pinfo.get("secret", False)),
                }
            )
    elif isinstance(raw_params, Sequence) and not isinstance(raw_params, (str, bytes)):
        seen_names: set[str] = set()
        for item in raw_params:
            if isinstance(item, Mapping):
                pname = str(item.get("name") or "").strip()
                if not pname or pname in seen_names:
                    continue
                seen_names.add(pname)
                params_list.append(
                    {
                        "name": pname,
                        "kind": str(item.get("kind") or "string"),
                        "required": bool(item.get("required", True)),
                        "secret": bool(item.get("secret", False)),
                    }
                )
            elif (
                isinstance(item, str)
                and item.strip()
                and item.strip() not in seen_names
            ):
                seen_names.add(item.strip())
                params_list.append(
                    {
                        "name": item.strip(),
                        "kind": "string",
                        "required": True,
                        "secret": False,
                    }
                )
        params_list.sort(key=lambda entry: entry["name"])

    raw_steps = data.get("steps") or ()
    steps_list: list[dict[str, Any]] = []
    for idx, step in enumerate(raw_steps):
        if not isinstance(step, Mapping):
            continue
        action = step.get("action")
        action_kind = (
            str(action.get("type") or action.get("kind") or "action")
            if isinstance(action, Mapping)
            else str(step.get("action_kind") or "action")
        )
        postconditions = step.get("postconditions") or ()
        steps_list.append(
            {
                "index": idx,
                "step_id": str(
                    step.get("id") or step.get("step_id") or f"step_{idx + 1}"
                ),
                "action_kind": action_kind,
                "risk": str(step.get("risk") or "reversible"),
                "postcondition_count": len(postconditions)
                if isinstance(postconditions, Sequence)
                else 0,
            }
        )

    raw_effects = data.get("verify_effects") or data.get("effects") or ()
    effects_list: list[dict[str, Any]] = []
    for eff in raw_effects:
        if not isinstance(eff, Mapping):
            continue
        match = eff.get("match")
        match_keys = (
            sorted(str(k) for k in match.keys()) if isinstance(match, Mapping) else []
        )
        effects_list.append(
            {
                "kind": str(eff.get("kind") or "record_written"),
                "match_keys": match_keys,
            }
        )

    digest_basis = {
        "name": name,
        "schema_version": schema_version,
        "params": params_list,
        "steps": steps_list,
        "effects": effects_list,
    }
    return {
        "name": name,
        "schema_version": schema_version,
        "bundle_sha256": _canonical_digest(digest_basis),
        "bundle_path": str(resolved_bundle_path) if resolved_bundle_path else None,
        "params": params_list,
        "steps": steps_list,
        "effects": effects_list,
    }


def _format_literal(value: Any) -> str:
    return repr(value)


def emit_workflow_code(
    bundle_or_spec: Any,
    *,
    module_name: Optional[str] = None,
    bundle_path: Optional[Path] = None,
) -> str:
    """Emit a deterministic, AST-validated Python module for a compiled workflow."""
    spec = _normalize_spec(bundle_or_spec, bundle_path=bundle_path)
    resolved_module = _safe_module_name(module_name or spec["name"])
    params_tuple = tuple(spec["params"])
    steps_tuple = tuple(spec["steps"])
    effects_tuple = tuple(spec["effects"])

    lines: list[str] = [
        f'"""Compiled OpenAdapt workflow module: {spec["name"]}.',
        "",
        f"Module: {resolved_module}",
        f"Bundle SHA-256: {spec['bundle_sha256']}",
        f"Steps: {len(steps_tuple)} | Declared Effects: {len(effects_tuple)}",
        "",
        "Generated by ``openadapt compile --emit-code``. Deterministic replay with",
        "read-back verification, fail-closed signed receipt output, and audit logging.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "import json",
        "from collections.abc import Callable, Mapping",
        "from datetime import datetime, timezone",
        "from pathlib import Path",
        "from typing import Any, Optional",
        "",
        "from openadapt_flow.compile_code import CommitResult",
        "",
        f"WORKFLOW_NAME: str = {_format_literal(spec['name'])}",
        f"MODULE_NAME: str = {_format_literal(resolved_module)}",
        f"SCHEMA_VERSION: int = {spec['schema_version']}",
        f"BUNDLE_SHA256: str = {_format_literal(spec['bundle_sha256'])}",
        f"DEFAULT_BUNDLE_PATH: Optional[str] = {_format_literal(spec['bundle_path'])}",
        "",
        f"PARAMS: tuple[dict[str, Any], ...] = {_format_literal(params_tuple)}",
        f"STEPS: tuple[dict[str, Any], ...] = {_format_literal(steps_tuple)}",
        f"VERIFY_EFFECTS: tuple[dict[str, Any], ...] = {_format_literal(effects_tuple)}",
        "",
        "",
        "def validate_params(params: Mapping[str, Any]) -> dict[str, Any]:",
        '    """Validate runtime parameters fail-closed against declared PARAMS."""',
        "    if not isinstance(params, Mapping):",
        '        raise ValueError("params must be a mapping")',
        '    declared = {entry["name"]: entry for entry in PARAMS}',
        "    unknown = sorted(k for k in params if k not in declared)",
        "    if unknown:",
        '        raise ValueError(f"Unknown workflow parameter(s): {unknown}")',
        "    resolved: dict[str, Any] = {}",
        "    for name, spec in declared.items():",
        "        if name not in params or params[name] is None:",
        '            if spec.get("required", True):',
        '                raise ValueError(f"Missing required workflow parameter: {name!r}")',
        "            continue",
        "        value = params[name]",
        '        if spec.get("secret") and isinstance(value, str) and not value.strip():',
        '            raise ValueError(f"Secret workflow parameter cannot be empty: {name!r}")',
        "        resolved[name] = value",
        "    return resolved",
        "",
        "",
        "def _append_memory_log(memory_log: Path, result: CommitResult) -> None:",
        '    """Append one non-sensitive execution line to memory_log.md."""',
        "    memory_log.parent.mkdir(parents=True, exist_ok=True)",
        '    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")',
        "    line = (",
        '        f"- {ts} | workflow={result.workflow_name} | outcome={result.outcome} "',
        '        f"| verified={result.verified} | steps={result.steps_executed} "',
        "        f\"| bundle={result.bundle_sha256[:12]} | receipt={result.receipt_path or 'none'}\\n\"",
        "    )",
        '    with memory_log.open("a", encoding="utf-8") as fh:',
        "        fh.write(line)",
        "",
        "",
        "def _default_replay(",
        "    bundle_path: Optional[str],",
        "    params: Mapping[str, Any],",
        "    *,",
        "    verify: bool,",
        "    receipt_dir: Optional[Path],",
        ") -> dict[str, Any]:",
        '    """Run deterministic openadapt_flow replay and optional receipt verification."""',
        "    if not bundle_path:",
        "        raise ValueError(",
        '            "No bundle_path configured; pass bundle_path or a custom executor."',
        "        )",
        "    from openadapt_flow.ir import Workflow",
        "    from openadapt_flow.runtime.replayer import Replayer",
        "",
        "    workflow = Workflow.load(Path(bundle_path))",
        "    replayer = Replayer()",
        "    report = replayer.replay(workflow, params=dict(params))",
        '    raw_outcome = str(getattr(report, "outcome", "NOT_CHECKED"))',
        '    steps_run = int(getattr(report, "steps_executed", len(STEPS)))',
        "    receipt_path: Optional[str] = None",
        "    receipt_doc: Optional[dict[str, Any]] = None",
        "    if receipt_dir is not None:",
        "        from openadapt_flow.receipt_record import receipt_after_run",
        "",
        "        receipt_dir.mkdir(parents=True, exist_ok=True)",
        "        written = receipt_after_run(receipt_dir, report=report)",
        "        if written is not None:",
        "            receipt_path = str(written)",
        '            receipt_doc = json.loads(Path(written).read_text(encoding="utf-8"))',
        "    return {",
        '        "outcome": raw_outcome,',
        '        "steps_executed": steps_run,',
        '        "receipt_path": receipt_path,',
        '        "receipt": receipt_doc,',
        '        "reason": str(getattr(report, "reason", "") or ""),',
        "    }",
        "",
        "",
        "def run_workflow(",
        "    params: Mapping[str, Any],",
        "    *,",
        "    verify: bool = True,",
        "    receipt_dir: Optional[str | Path] = None,",
        "    memory_log: Optional[str | Path] = None,",
        "    bundle_path: Optional[str | Path] = None,",
        "    executor: Optional[Callable[..., Mapping[str, Any]]] = None,",
        ") -> CommitResult:",
        '    """Execute the compiled workflow with read-back verification and receipts."""',
        "    validated = validate_params(params)",
        "    target_receipt_dir = Path(receipt_dir) if receipt_dir is not None else None",
        "    resolved_bundle = (",
        "        str(bundle_path) if bundle_path is not None else DEFAULT_BUNDLE_PATH",
        "    )",
        "    runner = executor if executor is not None else _default_replay",
        "    raw = runner(",
        "        resolved_bundle,",
        "        validated,",
        "        verify=verify,",
        "        receipt_dir=target_receipt_dir,",
        "    )",
        '    outcome = str(raw.get("outcome") or "NOT_CHECKED")',
        '    steps_executed = int(raw.get("steps_executed", len(STEPS)))',
        '    receipt_path = raw.get("receipt_path")',
        '    receipt_doc = raw.get("receipt")',
        '    reason = str(raw.get("reason") or "")',
        "",
        "    # Fail closed: verified=True requires verify=True, VERIFIED outcome,",
        "    # and a signed receipt whenever receipt_dir was requested.",
        '    verified = bool(verify and outcome == "VERIFIED")',
        "    if verify and target_receipt_dir is not None and not receipt_doc:",
        "        verified = False",
        '        outcome = "NOT_CHECKED"',
        '        reason = reason or "missing signed receipt in receipt_dir"',
        "",
        "    mem_path_str: Optional[str] = None",
        "    result = CommitResult(",
        "        workflow_name=WORKFLOW_NAME,",
        "        outcome=outcome,",
        "        verified=verified,",
        "        steps_executed=steps_executed,",
        "        bundle_sha256=BUNDLE_SHA256,",
        "        receipt_path=str(receipt_path) if receipt_path else None,",
        "        receipt=dict(receipt_doc) if isinstance(receipt_doc, Mapping) else None,",
        "        memory_log_path=str(memory_log) if memory_log is not None else None,",
        "        reason=reason,",
        "    )",
        "    if memory_log is not None:",
        "        mem_path = Path(memory_log)",
        "        _append_memory_log(mem_path, result)",
        "        mem_path_str = str(mem_path)",
        "        result = CommitResult(",
        "            workflow_name=result.workflow_name,",
        "            outcome=result.outcome,",
        "            verified=result.verified,",
        "            steps_executed=result.steps_executed,",
        "            bundle_sha256=result.bundle_sha256,",
        "            receipt_path=result.receipt_path,",
        "            receipt=result.receipt,",
        "            memory_log_path=mem_path_str,",
        "            reason=result.reason,",
        "        )",
        "    return result",
        "",
        "",
        "def main(argv: Optional[list[str]] = None) -> int:",
        "    parser = argparse.ArgumentParser(",
        "        prog=MODULE_NAME,",
        '        description=f"Run compiled workflow {WORKFLOW_NAME}.",',
        "        allow_abbrev=False,",
        "    )",
        '    parser.add_argument("--params-json", default="{}", help="JSON object of workflow params.")',
        '    parser.add_argument("--no-verify", action="store_true", help="Disable verification gate.")',
        '    parser.add_argument("--receipt-dir", default=None, help="Directory for signed receipts.")',
        '    parser.add_argument("--memory-log", default=None, help="Path to memory_log.md.")',
        "    args = parser.parse_args(argv)",
        "    params = json.loads(args.params_json)",
        "    res = run_workflow(",
        "        params,",
        "        verify=not args.no_verify,",
        "        receipt_dir=args.receipt_dir,",
        "        memory_log=args.memory_log,",
        "    )",
        "    print(json.dumps(res.to_dict(), indent=2, sort_keys=True))",
        "    return 0 if res.verified else 1",
        "",
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
    ]
    source = "\n".join(lines)
    # Fail closed if the emitted source is not valid Python AST.
    ast.parse(source)
    return source


def emit_workflow_code_file(
    bundle_or_spec: Any,
    out_path: Path,
    *,
    module_name: Optional[str] = None,
    bundle_path: Optional[Path] = None,
) -> Path:
    """Write the AST-validated compiled workflow module to ``out_path``."""
    source = emit_workflow_code(
        bundle_or_spec,
        module_name=module_name or out_path.stem,
        bundle_path=bundle_path,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(source, encoding="utf-8")
    return out_path


def register_compile_code_args(parser: argparse.ArgumentParser) -> None:
    """Attach ``--emit-code`` to the ``openadapt-flow compile`` subparser."""
    parser.add_argument(
        "--emit-code",
        default=None,
        metavar="PATH",
        help=(
            "Emit a clean, git-diffable Python module wrapping deterministic "
            "replay, read-back verification, and signed receipts."
        ),
    )


def maybe_emit_code_from_cli(
    args: argparse.Namespace,
    bundle_dir: Path,
    workflow: Any = None,
) -> Optional[Path]:
    """If ``--emit-code`` was passed on ``compile``, emit the Python module."""
    target = getattr(args, "emit_code", None)
    if not target:
        return None
    out_path = Path(str(target))
    source_obj = workflow if workflow is not None else bundle_dir
    written = emit_workflow_code_file(source_obj, out_path, bundle_path=bundle_dir)
    print(f"Emitted compiled workflow code to {written}")
    return written


__all__ = [
    "CommitResult",
    "emit_workflow_code",
    "emit_workflow_code_file",
    "maybe_emit_code_from_cli",
    "register_compile_code_args",
]
