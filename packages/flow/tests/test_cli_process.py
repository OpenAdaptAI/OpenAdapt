"""CLI wiring for process contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.admitted_composition import (
    ProcessContract,
    is_process_contract_artifact,
)
from openadapt_flow.cli_process import bind_process_child_run
from tests.test_admitted_composition_authoring import _two_admitted


def test_parser_dispatches_process() -> None:
    args = build_parser().parse_args(
        [
            "process",
            "--child",
            "intake=./a",
            "--admission",
            "intake=./a.json",
            "--child",
            "posting=./b",
            "--admission",
            "posting=./b.json",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            "process",
        ]
    )
    assert args.func.__name__ == "_cmd_process"
    assert args.child == ["intake=./a", "posting=./b"]
    assert args.admission == ["intake=./a.json", "posting=./b.json"]


def test_cli_process_writes_artifact(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    rc = main(
        [
            "process",
            "--child",
            f"intake={intake}",
            "--admission",
            f"intake={intake_env}",
            "--child",
            f"posting={posting}",
            "--admission",
            f"posting={posting_env}",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            str(out),
            "--name",
            "two-step",
        ]
    )
    assert rc == 0
    assert is_process_contract_artifact(out)


def test_cli_process_accepts_a_complete_v1_spec(tmp_path: Path) -> None:
    spec = tmp_path / "process-v1.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.process-contract/v1",
                "name": "review-process",
                "human_children": [
                    {
                        "name": "review",
                        "kind": "human",
                        "task_kind": "review",
                        "substrate": "browser",
                        "risk_class": "consequential",
                        "required_authn": "aal2",
                    }
                ],
            }
        )
    )
    out = tmp_path / "process"

    assert main(["process", "--spec", str(spec), "--out", str(out)]) == 0
    assert ProcessContract.load(out).schema_version == "openadapt.process-contract/v1"


def test_cli_v1_certify_refuses_a_false_parent_shortcut(tmp_path: Path) -> None:
    ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="review-process",
        human_children=[
            {
                "name": "review",
                "kind": "human",
                "task_kind": "review",
                "substrate": "browser",
                "risk_class": "consequential",
                "required_authn": "aal2",
            }
        ],
    ).save(tmp_path)

    with pytest.raises(SystemExit, match="no parent certification shortcut"):
        main(["certify", str(tmp_path)])


def test_cli_process_without_admission_refuses_compose_style_children(
    tmp_path: Path,
) -> None:
    intake, _, posting, _ = _two_admitted(tmp_path)
    with pytest.raises(SystemExit, match="admission"):
        main(
            [
                "process",
                "--child",
                f"intake={intake}",
                "--child",
                f"posting={posting}",
                "--out",
                str(tmp_path / "process"),
            ]
        )


def test_replay_refuses_process(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    rc = main(
        [
            "process",
            "--child",
            f"intake={intake}",
            "--admission",
            f"intake={intake_env}",
            "--child",
            f"posting={posting}",
            "--admission",
            f"posting={posting_env}",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    with pytest.raises(SystemExit, match="replay refuses a process contract"):
        main(["replay", str(out), "--url", "http://example.invalid"])


def test_process_run_defaults_to_governed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENADAPT_EXECUTE_URL", raising=False)
    monkeypatch.delenv("OPENADAPT_EXECUTE_TOKEN", raising=False)
    run_child = MagicMock(return_value=0)
    bound = bind_process_child_run(argparse.Namespace(), run_child)
    assert getattr(bound, "execute_via") == "governed_run"


def test_process_run_binds_execute_client_when_cloud_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openadapt_types")
    monkeypatch.setenv("OPENADAPT_EXECUTE_URL", "https://app.openadapt.ai/api")
    monkeypatch.setenv("OPENADAPT_EXECUTE_TOKEN", "partner-token")
    monkeypatch.setenv("OPENADAPT_EXECUTE_ENVIRONMENT_ID", "environment_12345678")
    run_child = MagicMock(return_value=0)
    bound = bind_process_child_run(argparse.Namespace(), run_child)
    assert getattr(bound, "execute_via") == "execute_client"
