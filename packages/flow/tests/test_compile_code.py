"""Tests for ``openadapt_flow.compile_code`` (``openadapt compile --emit-code``)."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from openadapt_flow.__main__ import build_parser
from openadapt_flow.compile_code import (
    CommitResult,
    emit_workflow_code,
    emit_workflow_code_file,
    maybe_emit_code_from_cli,
)


def _sample_spec() -> dict[str, Any]:
    return {
        "name": "submit-work-item",
        "version": "1.0",
        "params": [
            {"name": "item_id", "required": True, "secret": False},
            {"name": "api_token", "required": True, "secret": True},
            {"name": "note", "required": False, "secret": False},
        ],
        "steps": [
            {
                "id": "step_1",
                "action": "focus_field",
                "target": {"role": "textbox", "name": "Item ID"},
                "param": "item_id",
            },
            {
                "id": "step_2",
                "action": "click",
                "target": {"role": "button", "name": "Submit"},
                "irreversible": True,
            },
        ],
    }


def _load_emitted_module(
    source: str, tmp_path: Path, name: str = "emitted_wf"
) -> ModuleType:
    mod_path = tmp_path / f"{name}.py"
    mod_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, mod_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_emit_workflow_code_is_valid_ast_and_deterministic(tmp_path: Path) -> None:
    spec = _sample_spec()
    code_a = emit_workflow_code(spec, module_name="submit_work_item")
    code_b = emit_workflow_code(spec, module_name="submit_work_item")

    assert code_a == code_b
    tree = ast.parse(code_a)
    func_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert {"validate_params", "run_workflow", "main"}.issubset(func_names)

    # Loading from a JSON spec file produces identical deterministic output
    spec_file = tmp_path / "workflow.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    code_from_file = emit_workflow_code(spec_file, module_name="submit_work_item")
    # Bundle path metadata differs because spec_file has a parent dir; digest & structure match.
    assert ast.parse(code_from_file) is not None
    mod_a = _load_emitted_module(code_a, tmp_path, "wf_det_a")
    mod_file = _load_emitted_module(code_from_file, tmp_path, "wf_det_file")
    assert mod_a.BUNDLE_SHA256 == mod_file.BUNDLE_SHA256
    assert mod_a.WORKFLOW_NAME == "submit-work-item"
    assert len(mod_a.STEPS) == 2


def test_emitted_validate_params_enforces_required_unknown_and_secret(
    tmp_path: Path,
) -> None:
    mod = _load_emitted_module(
        emit_workflow_code(_sample_spec()), tmp_path, "wf_validate"
    )

    validated = mod.validate_params(
        {"item_id": "ITEM-42", "api_token": "tok_secret_123"}
    )
    assert validated == {"item_id": "ITEM-42", "api_token": "tok_secret_123"}

    with pytest.raises(ValueError, match="Missing required workflow parameter"):
        mod.validate_params({"item_id": "ITEM-42"})

    with pytest.raises(ValueError, match="Unknown workflow parameter"):
        mod.validate_params(
            {"item_id": "ITEM-42", "api_token": "tok_secret_123", "extra": "nope"}
        )

    with pytest.raises(ValueError, match="Secret workflow parameter cannot be empty"):
        mod.validate_params({"item_id": "ITEM-42", "api_token": "   "})

    with pytest.raises(ValueError, match="params must be a mapping"):
        mod.validate_params(["not", "a", "mapping"])


def test_run_workflow_verified_with_signed_receipt_and_memory_log(
    tmp_path: Path,
) -> None:
    mod = _load_emitted_module(emit_workflow_code(_sample_spec()), tmp_path, "wf_ok")
    receipt_dir = tmp_path / "receipts"
    memory_log = tmp_path / "memory_log.md"
    secret_val = "SUPER_SECRET_TOKEN_XYZ"

    def fake_executor(
        bundle_path: str | None,
        params: dict[str, Any],
        *,
        verify: bool,
        receipt_dir: Path | None,
    ) -> dict[str, Any]:
        assert verify is True
        assert receipt_dir == tmp_path / "receipts"
        assert params["item_id"] == "ITEM-99"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / "run_receipt.json"
        receipt_payload = {
            "schema": "record/run/v1",
            "outcome": "VERIFIED",
            "signature": {"alg": "Ed25519", "sig": "base64sig=="},
        }
        receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")
        return {
            "outcome": "VERIFIED",
            "verified": True,
            "steps_executed": 2,
            "receipt_path": str(receipt_path),
            "receipt": receipt_payload,
            "reason": "read-back matched",
        }

    result = mod.run_workflow(
        {"item_id": "ITEM-99", "api_token": secret_val},
        verify=True,
        receipt_dir=receipt_dir,
        memory_log=memory_log,
        executor=fake_executor,
    )

    assert isinstance(result, CommitResult)
    assert result.verified is True
    assert result.outcome == "VERIFIED"
    assert result.steps_executed == 2
    assert result.receipt_path is not None
    assert result.memory_log_path == str(memory_log)

    log_text = memory_log.read_text(encoding="utf-8")
    assert "submit-work-item" in log_text
    assert "outcome=VERIFIED" in log_text
    assert "verified=True" in log_text
    assert secret_val not in log_text


def test_run_workflow_fails_closed_on_missing_receipt_or_refuted(
    tmp_path: Path,
) -> None:
    mod = _load_emitted_module(
        emit_workflow_code(_sample_spec()), tmp_path, "wf_failclosed"
    )
    receipt_dir = tmp_path / "receipts"

    # Case 1: Executor claims VERIFIED with verify=True and receipt_dir set, but provides no receipt.
    def no_receipt_executor(
        bundle_path: str | None,
        params: dict[str, Any],
        *,
        verify: bool,
        receipt_dir: Path | None,
    ) -> dict[str, Any]:
        return {"outcome": "VERIFIED", "verified": True, "steps_executed": 2}

    res_missing = mod.run_workflow(
        {"item_id": "ITEM-1", "api_token": "tok"},
        verify=True,
        receipt_dir=receipt_dir,
        executor=no_receipt_executor,
    )
    assert res_missing.verified is False
    assert res_missing.outcome == "NOT_CHECKED"
    assert "receipt" in res_missing.reason

    # Case 2: Executor returns REFUTED -> verified must be False even if flag says True.
    def refuted_executor(
        bundle_path: str | None,
        params: dict[str, Any],
        *,
        verify: bool,
        receipt_dir: Path | None,
    ) -> dict[str, Any]:
        return {
            "outcome": "REFUTED",
            "verified": True,
            "steps_executed": 1,
            "reason": "post-write read-back mismatch",
        }

    res_refuted = mod.run_workflow(
        {"item_id": "ITEM-1", "api_token": "tok"},
        verify=True,
        executor=refuted_executor,
    )
    assert res_refuted.verified is False
    assert res_refuted.outcome == "REFUTED"


def test_emit_workflow_code_file_and_cli_integration(tmp_path: Path) -> None:
    parser = build_parser()
    out_code = tmp_path / "generated" / "submit_work_item.py"
    ns = parser.parse_args(
        [
            "compile",
            str(tmp_path / "rec"),
            "--out",
            str(tmp_path / "bundle"),
            "--name",
            "submit-work-item",
            "--emit-code",
            str(out_code),
        ]
    )
    assert ns.emit_code == str(out_code)

    written = maybe_emit_code_from_cli(ns, tmp_path / "bundle", _sample_spec())
    assert written == out_code
    assert out_code.exists()
    source = out_code.read_text(encoding="utf-8")
    assert ast.parse(source) is not None
    assert "allow_abbrev=False" in source

    # Without --emit-code, helper is a no-op.
    ns_no_emit = parser.parse_args(
        [
            "compile",
            str(tmp_path / "rec"),
            "--out",
            str(tmp_path / "bundle"),
            "--name",
            "demo",
        ]
    )
    assert (
        maybe_emit_code_from_cli(ns_no_emit, tmp_path / "bundle", _sample_spec())
        is None
    )

    # Direct emit_workflow_code_file helper.
    direct_out = tmp_path / "direct" / "wf.py"
    assert emit_workflow_code_file(_sample_spec(), direct_out) == direct_out
    assert direct_out.exists()
