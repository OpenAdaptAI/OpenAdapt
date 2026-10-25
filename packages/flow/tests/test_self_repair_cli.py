"""``openadapt-flow run --self-repair`` end to end on MockMed.

The same scenario as ``test_self_repair`` through the CLI: the redesigned
save control halts the run, a scripted agent loaded with ``--repair-agent``
proposes the new control, and a person approves on the phone portal while
the CLI waits. The record check reads MockMed's store; the run makes no
model calls and the agent calls no model.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from tests._approver_keys import approvers_yaml

from openadapt_flow.__main__ import main
from openadapt_flow.ir import RunReport
from openadapt_flow.repair.self_repair import RepairProposal, RepairRequest
from tests.test_self_repair import (  # noqa: F401 - the shared MockMed fixture
    QUERY,
    _browser,
    _center_of,
    _records,
    _reset,
    mockmed,
)

pytest.importorskip("playwright.sync_api")

# The phone portal records the person's tap signed with its registered
# approver key (OPENADAPT_PORTAL_APPROVER_KEY); without one it records nothing.
pytestmark = pytest.mark.usefixtures("approver")

#: Set by the test before the CLI loads the agent.
TARGET: Optional[tuple[int, int]] = None


class CliAgent:
    dialect = "generic"
    agent_id = "agent:cli-scripted"
    requires_model_key = False

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        if TARGET is None or request.attempt > 1:
            return None
        return RepairProposal(action={"kind": "click", "x": TARGET[0], "y": TARGET[1]})


def _config(tmp_path: Path, base_url: str) -> Path:
    """A deployment config whose record check reads MockMed's store."""

    config = tmp_path / "deploy.yaml"
    config.write_text(
        f"effects:\n  kind: rest\n  base_url: {base_url}\n"
        "policy:\n  policy: clinical-write\n" + approvers_yaml()
    )
    return config


def _answer_on_phone(root: Path, stop: threading.Event, answered: list[str]) -> None:
    from openadapt_flow.console.save_approvals import (
        SaveApprovalActionRequest,
        decide_save_approval,
        list_save_approvals,
        save_approval_detail,
    )

    while not stop.is_set():
        for row in list_save_approvals(root):
            detail = save_approval_detail(root, row.request_id)
            decide_save_approval(
                root,
                row.request_id,
                SaveApprovalActionRequest(
                    action="verify_and_resume",
                    task_digest=detail["task_digest"],
                    task_signature=detail["task"]["signature"],
                ),
            )
            answered.append(row.request_id)
            return
        time.sleep(0.2)


def test_run_self_repair_cli_resumes_after_one_phone_tap(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    global TARGET
    base_url, bundle = mockmed
    _reset(base_url)
    with _browser():
        TARGET = _center_of(base_url, "#submit-encounter")
    config = _config(tmp_path, base_url)
    run_dir = tmp_path / "run"
    stop = threading.Event()
    answered: list[str] = []
    phone = threading.Thread(
        target=_answer_on_phone, args=(run_dir, stop, answered), daemon=True
    )
    phone.start()
    try:
        code = main(
            [
                "run",
                str(bundle),
                "--config",
                str(config),
                "--backend",
                "web",
                "--url",
                f"{base_url}/{QUERY}&drift=redesign#tasks",
                "--run-dir",
                str(run_dir),
                "--param",
                "note=Synthetic follow-up in two weeks",
                "--approval-mode",
                "not_gated",
                "--allow-unencrypted",
                "--self-repair",
                "--repair-agent",
                "tests.test_self_repair_cli:CliAgent",
                "--repair-screen-access-by",
                "ops-lead",
                "--repair-screen-share-mode",
                "allow_unscrubbed",
                "--repair-store",
                str(tmp_path / "repair-store"),
                "--repair-fault-url",
                f"{base_url}/{QUERY}&drift=redesign,missing#tasks",
                "--repair-approval-wait",
                "120",
            ]
        )
    finally:
        stop.set()
        phone.join(timeout=5)
        TARGET = None
    out = capsys.readouterr().out
    assert answered, out
    assert "item-approval approve" in out
    # The printed Run it command signs with the person's approver key.
    assert "--approver-key" in out
    assert "Self-repair: resumed" in out
    resumed = RunReport.model_validate_json(
        (run_dir / "self-repair" / "resume" / "report.json").read_text()
    )
    assert resumed.self_repair is not None
    assert resumed.self_repair.status == "resumed"
    assert resumed.self_repair.approved_by == "decision_portal:person"
    assert resumed.model_calls == 0
    # The record check confirmed the save in MockMed's own store.
    assert any(
        evidence.final_verdict == "confirmed"
        for result in resumed.results
        for evidence in result.effect_evidence
    )
    assert len(_records(base_url)) == 1
    # Without a named profile the halted run has no outcome word; its halt and
    # its self-repair block say what happened.
    halted = RunReport.model_validate_json((run_dir / "report.json").read_text())
    assert halted.halt is not None and halted.halt.state_id == "step_005"
    assert halted.self_repair == resumed.self_repair
    assert code in (0, 1)


def test_run_self_repair_refuses_a_pinned_bundle(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    from openadapt_flow.ir import Workflow

    base_url, bundle = mockmed
    digest = Workflow.load(bundle).manifest.content_digest
    code = main(
        [
            "run",
            str(bundle),
            "--config",
            str(_config(tmp_path, base_url)),
            "--backend",
            "web",
            "--url",
            f"{base_url}/{QUERY}#tasks",
            "--run-dir",
            str(tmp_path / "run"),
            "--approval-mode",
            "not_gated",
            "--allow-unencrypted",
            "--pin-digest",
            digest,
            "--self-repair",
            "--repair-agent",
            "tests.test_self_repair_cli:CliAgent",
            "--repair-screen-access-by",
            "ops-lead",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert not (tmp_path / "run" / "report.json").exists()
    assert "--self-repair: --pin-digest pins the original bundle" in out
