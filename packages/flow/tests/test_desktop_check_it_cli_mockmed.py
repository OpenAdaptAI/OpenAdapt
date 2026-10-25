"""Desktop's Check it and Run it, through the CLI commands Desktop calls (B276).

This is the contract Desktop uses, end to end in a real browser against the
made-up MockMed task and its independent record store:

1. ``openadapt-flow qualify drills BUNDLE --json`` sets up the local drill
   campaign from the task's practice case.
2. ``openadapt-flow qualify run-case ... --case-id practice`` runs the practice
   item. Under the default ``each_item`` mode it waits before the save.
   Desktop reads the waiting request with ``openadapt-flow item-approval show
   RUN_DIR`` and answers it with ``openadapt-flow item-approval approve``,
   both as separate processes. The item then saves once and ends
   ``VERIFIED``.
3. ``openadapt-flow qualify run-case ... --case-id <drill>`` runs each drill.
   It ends on the engine's own detector before any input is delivered, asks
   nobody, writes nothing, and leaves its signed receipt and mutation record
   in ``RUN_DIR/qualification-fault/``.

``qualify run-case`` runs in this process so the test can supply MockMed's
environment observer (MockMed has no environment meta tags for the generic
browser observer). Everything else is the real CLI. Every read-back here is
MockMed's test setup (``/api/db``). All people and values are made up.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

import openadapt_flow.__main__ as main  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import RunReport, Workflow  # noqa: E402
from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV  # noqa: E402
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV  # noqa: E402
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.qualify.environment import (  # noqa: E402
    WEB_OBSERVER_CONTRACT_SHA256,
    WEB_OBSERVER_ID,
    WebEnvironmentObserver,
    web_environment_digest,
    web_environment_payload,
)
from openadapt_flow.qualification import (  # noqa: E402
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    add_case,
    init_project,
    qualification_campaign_id_sha256,
    qualification_run_id_sha256,
    record_case_results,
    set_action_classification,
    set_case_scope,
    set_effect_policy,
    set_identity_policy,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_faults import (  # noqa: E402
    FaultMutationReceipt,
    expected_fault_detector,
    fault_detector_contract_error,
)
from openadapt_flow.runtime.authorization import (  # noqa: E402
    runtime_inputs_bytes,
    runtime_inputs_digest,
)
from openadapt_flow.tutorial import (  # noqa: E402
    TUTORIAL_ENTRY_QUERY,
    TUTORIAL_NOTE,
    consequential_step,
    record_tutorial,
)
from openadapt_flow.verification import VerificationTier  # noqa: E402
from scripts import export_public_demo_evidence as demo  # noqa: E402
from tests import _approver_keys as approver_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_KEY = "desktop-check-it-test-key"
CAMPAIGN_ID = "desktop-check-it-local"
RUNNER_KEY_ID = "desktop-check-it-runner"


def _flow(*argv: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one Flow command as its own process, the way Desktop does."""

    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )


class DesktopRunIt:
    """Answers the run's waiting approval request, as Desktop's Run it would."""

    def __init__(self, run_dir: Path, env: dict[str, str], approver_key: Path) -> None:
        self.run_dir = run_dir
        self.env = env
        self.approver_key = approver_key
        self.shown: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "DesktopRunIt":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not (self.run_dir / "item-approval").is_dir():
                time.sleep(0.2)
                continue
            shown = _flow("item-approval", "show", str(self.run_dir), env=self.env)
            if shown.returncode != 0:
                self.errors.append(shown.stdout + shown.stderr)
                return
            waiting = [
                row
                for row in json.loads(shown.stdout)["requests"]
                if row["status"] == "waiting"
            ]
            for row in waiting:
                self.shown.append(row)
                answered = _flow(
                    "item-approval",
                    "approve",
                    str(self.run_dir),
                    "--request-id",
                    row["request_id"],
                    "--request-sha256",
                    row["request_sha256"],
                    "--decided-by",
                    "human",
                    "--route",
                    "desktop_run_it",
                    "--unauthenticated",
                    # Desktop signs the person's answer with its registered
                    # approver key; a claimed Cloud sign-in proves nothing here.
                    "--approver-key",
                    str(self.approver_key),
                    env=self.env,
                )
                if answered.returncode != 0:
                    self.errors.append(answered.stdout + answered.stderr)
                    return
                self.answers.append(json.loads(answered.stdout))
            time.sleep(0.2)


def _records(base_url: str) -> list[dict[str, Any]]:
    return list(demo._http_json(f"{base_url}api/db").get("records", []))


@pytest.fixture(scope="module")
def check_it(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("desktop-check-it")
    home = work / "openadapt-home"
    saved_env = {
        name: os.environ.get(name)
        for name in (
            "OPENADAPT_HOME",
            "OPENADAPT_BUNDLE_KEY",
            KEY_DIR_ENV,
            KEY_PASSPHRASE_ENV,
            ROOT_ENV,
        )
    }
    os.environ["OPENADAPT_HOME"] = str(home)
    os.environ["OPENADAPT_BUNDLE_KEY"] = BUNDLE_KEY
    # The person's answers are signed by Desktop's registered approver key,
    # and the run verifies them against this key directory.
    key_dir = work / "approver-keys"
    os.environ[KEY_DIR_ENV] = str(key_dir)
    os.environ[KEY_PASSPHRASE_ENV] = approver_keys.PASSPHRASE
    approver_key, _signer = approver_keys.provision(key_dir)
    # Desktop's runs pin the roster's root (approval_mode/approver_roster).
    os.environ[ROOT_ENV] = approver_keys.root_of(key_dir)
    env = {
        **os.environ,
        "OPENADAPT_HOME": str(home),
        "OPENADAPT_BUNDLE_KEY": BUNDLE_KEY,
    }
    base_url, _db, stop = serve()
    observer_patch = pytest.MonkeyPatch()
    try:
        recording = record_tutorial(base_url, work / "recording")
        plain = work / "compiled"
        workflow = compile_recording(
            recording, plain, name="desktop-check-it", mine_effects=True
        )
        save = consequential_step(workflow)
        app_digest = demo._tree_digest(REPO_ROOT / "openadapt_flow" / "mockmed")
        app_version = f"sha256:{app_digest}"
        entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"
        backend, close = PlaywrightBackend.launch(entry_url, headless=True)
        try:
            payload = web_environment_payload(
                backend, application_version=app_version, headless=True
            )
        finally:
            close()
        init_project(
            workflow,
            environment=EnvironmentBoundary(
                target_kind="web",
                application="OpenAdapt MockMed synthetic reference",
                application_identity=demo._origin(base_url),
                application_version=app_version,
                environment_observer_id=WEB_OBSERVER_ID,
                environment_observer_contract_sha256=WEB_OBSERVER_CONTRACT_SHA256,
                environment_digest=web_environment_digest(payload),
                runtime_version=demo.FLOW_VERSION,
                required_capabilities=[
                    "headless_chromium",
                    "independent_system_of_record",
                    "playwright_dom",
                ],
            ),
            minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
        )
        for step in workflow.steps:
            irreversible = step.risk == "irreversible"
            set_action_classification(
                workflow,
                ActionRiskClassification(
                    step_id=step.id,
                    classification=(
                        ActionRiskClass.IRREVERSIBLE
                        if irreversible
                        else ActionRiskClass.READ_ONLY
                    ),
                    explanation=(
                        "the save writes the encounter"
                        if irreversible
                        else "navigation and form entry before the save"
                    ),
                    operator_confirmed=True,
                ),
            )
        set_identity_policy(
            workflow,
            IdentityPolicy(
                step_id=save.id, enforcement=IdentityEnforcement.CANONICAL_LADDER
            ),
        )
        for index, _effect in enumerate(save.effects):
            set_effect_policy(
                workflow,
                step_id=save.id,
                effect_index=index,
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        params = {"note": TUTORIAL_NOTE}
        add_case(
            workflow,
            QualificationCase(
                id="practice",
                kind=QualificationCaseKind.REPRESENTATIVE,
                expected_outcome=QualificationOutcome.VERIFIED,
                description="The customer's practice item",
            ),
        )
        set_case_scope(
            workflow,
            case_id="practice",
            runtime_input_sha256=runtime_inputs_digest(workflow, params, None),
            action_targets=[
                QualificationActionTarget(step_id=save.id, actuation_path="gui")
            ],
        )
        # Desktop's runner key signs the case results it records.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        runner = Ed25519PrivateKey.generate()
        runner_private = runner.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        set_trusted_runner_key(
            workflow,
            key_id=RUNNER_KEY_ID,
            public_key_base64=base64.b64encode(
                runner.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ).decode("ascii"),
        )
        bundle = work / "bundle"
        workflow.save(bundle, encrypt=True, key=BUNDLE_KEY)
        inputs = work / "practice-inputs.json"
        inputs.write_bytes(runtime_inputs_bytes(workflow, params, None))
        inputs.chmod(0o600)

        # 1. Desktop sets up the local drill campaign from the task.
        drills = _flow("qualify", "drills", str(bundle), "--json", env=env)
        assert drills.returncode == 0, drills.stdout + drills.stderr
        campaign = json.loads(drills.stdout)

        # The declarative web observer stands in for the generic one.
        observer_patch.setattr(
            "openadapt_flow.qualification_environment."
            "BackendQualificationEnvironmentObserver",
            lambda _backend: WebEnvironmentObserver(application_version=app_version),
        )

        def run_case(case_id: str) -> SimpleNamespace:
            demo._http_json(f"{base_url}api/reset", method="POST", body={})
            run_dir = work / "runs" / case_id
            argv = [
                "qualify",
                "run-case",
                str(bundle),
                "--case-id",
                case_id,
                "--inputs",
                str(inputs),
                "--campaign-id",
                CAMPAIGN_ID,
                "--run-id",
                f"{case_id}-1",
                "--run-dir",
                str(run_dir),
                "--backend",
                "web",
                "--url",
                entry_url,
                "--effects-kind",
                "rest",
                "--effects-base-url",
                base_url,
                "--approval-wait",
                "120",
                # A governed run takes its approver pin only from its config.
                "--config",
                str(approver_keys.approver_config(work / "approvers.yaml")),
            ]
            with DesktopRunIt(run_dir, env, approver_key) as person:
                exit_code = main.main(argv)
            report = RunReport.model_validate_json(
                (run_dir / "report.json").read_text(encoding="utf-8")
            )
            return SimpleNamespace(
                exit_code=exit_code,
                report=report,
                run_dir=run_dir,
                person=person,
                records=_records(base_url),
            )

        runs = {"practice": run_case("practice")}
        for drill in campaign["drills"]:
            runs[drill["case_id"]] = run_case(drill["case_id"])
        yield SimpleNamespace(
            campaign=campaign,
            runs=runs,
            bundle=bundle,
            work=work,
            inputs=inputs,
            params=params,
            runner_private=runner_private,
            base_url=base_url,
            entry_url=entry_url,
            env=env,
            approver_key=approver_key,
        )
    finally:
        observer_patch.undo()
        stop()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_qualify_drills_sets_up_the_five_drills_from_the_practice_case(check_it):
    campaign = check_it.campaign
    assert campaign["schema"] == "openadapt.local-drill-campaign/v1"
    assert campaign["from_case_id"] == "practice"
    assert {drill["kind"] for drill in campaign["drills"]} == {
        "wrong_identity",
        "ambiguity",
        "stale_identity",
        "missing_effect",
        "weak_effect",
    }
    reloaded = Workflow.load(check_it.bundle, key=BUNDLE_KEY)
    assert reloaded.qualification.trusted_fault_driver_keys == {
        campaign["driver_key_id"]: reloaded.qualification.trusted_fault_driver_keys[
            campaign["driver_key_id"]
        ]
    }


def test_the_practice_item_waits_for_desktop_and_saves_once_after_approval(check_it):
    run = check_it.runs["practice"]
    assert run.person.errors == []
    assert run.exit_code == 0
    assert run.report.transaction_outcome == "VERIFIED"
    assert run.report.approval is not None
    assert run.report.approval.mode.value == "each_item"
    assert run.report.approval.decision.value == "approved"
    (approval,) = run.report.approval.approvals
    # A signed Run it answer names no Cloud user: its proof is the signature.
    assert approval.approver_id is None
    assert approval.auth_method == "none"
    # Desktop saw one request, with a screenshot of the filled form.
    (shown,) = run.person.shown
    assert Path(shown["screenshot"]).is_file()
    assert run.person.answers[0]["ok"] is True
    assert run.person.answers[0]["resume_required"] is False
    assert len(run.records) == 1
    assert run.records[0]["note"] == TUTORIAL_NOTE


@pytest.mark.parametrize(
    "kind",
    ["wrong_identity", "ambiguity", "stale_identity", "missing_effect", "weak_effect"],
)
def test_each_drill_runs_from_desktop_and_stops_on_the_engines_detector(check_it, kind):
    case_id = next(
        drill["case_id"]
        for drill in check_it.campaign["drills"]
        if drill["kind"] == kind
    )
    run = check_it.runs[case_id]
    report = run.report
    assert run.person.errors == []
    assert run.person.shown == [], "a drill never asks a person"
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome in {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY"}
    assert run.records == [], "a drill never writes"
    folder = run.run_dir / "qualification-fault"
    receipt = FaultMutationReceipt.model_validate_json(
        (folder / "receipt.json").read_bytes()
    )
    assert fault_detector_contract_error(report, receipt) is None
    refused = next(r for r in report.results if r.safety_refusal_evidence is not None)
    assert (
        refused.safety_refusal_evidence.stage,
        refused.safety_refusal_evidence.code,
    ) == expected_fault_detector(kind)
    assert refused.delivery_attempted is False
    status = json.loads((folder / "status.json").read_text())
    assert status["staged"] is True
    assert (folder / "mutation.json").is_file()
    assert TUTORIAL_NOTE not in (folder / "mutation.json").read_text()


def test_flow_accepts_every_case_result_desktop_records(check_it):
    """The practice result and all five drill results pass Flow's own check."""

    workflow = Workflow.load(check_it.bundle, key=BUNDLE_KEY)
    project = workflow.qualification
    root = check_it.work

    def ref(path: Path, kind: str) -> EvidenceRef:
        return EvidenceRef(
            kind=kind,
            sha256=demo._sha256(path),
            relative_path=path.relative_to(root).as_posix(),
        )

    results = []
    for case in project.cases:
        run = check_it.runs[case.id]
        drill = case.kind is not QualificationCaseKind.REPRESENTATIVE
        fault = run.run_dir / "qualification-fault"
        evidence = [
            ref(run.run_dir / "report.json", "run_report"),
            ref(check_it.inputs, "case_input"),
        ]
        if drill:
            evidence += [
                ref(fault / "receipt.json", "fault_receipt"),
                ref(fault / "mutation.json", "fault_mutation"),
            ]
        # The run's frames, approval files and durable evidence, hash-bound.
        evidence += [
            ref(path, "other")
            for path in sorted(run.run_dir.rglob("*"))
            if path.is_file()
            and path.name != "report.json"
            and "qualification-fault" not in path.parts
        ]
        result = QualificationCaseResult(
            case_id=case.id,
            project_id=project.project_id,
            project_revision=project.revision,
            project_contract_sha256=project.contract_sha256(),
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            environment_contract_sha256=project.environment.contract_sha256(),
            environment_digest=project.environment.environment_digest,
            runtime_version=project.environment.runtime_version,
            runner_id=RUNNER_KEY_ID,
            runner_capabilities=list(project.environment.required_capabilities),
            status="passed",
            observed_outcome=(
                QualificationOutcome.HALTED if drill else QualificationOutcome.VERIFIED
            ),
            campaign_id_sha256=qualification_campaign_id_sha256(CAMPAIGN_ID),
            case_input_sha256=runtime_inputs_digest(workflow, check_it.params, None),
            run_id_sha256=qualification_run_id_sha256(f"{case.id}-1"),
            evidence=evidence,
            attestation_key_id=RUNNER_KEY_ID,
        )
        results.append(sign_case_result(result, private_key=check_it.runner_private))
    record_case_results(workflow, results, evidence_root=root)
    assert all(len(case.results) == 1 for case in workflow.qualification.cases)


def test_run_it_saves_once_after_approval_and_never_saves_a_form_nobody_sees(
    check_it,
):
    """Run it: ``run --approval-mode each_item``, through the CLI.

    1. Nobody answers in time: the run stops before the save, with nothing
       written, and leaves a pause.
    2. Desktop approves late. ``item-approval approve`` says a resume is
       required. This run launched its own browser, which closed with it, so
       ``resume`` opens a new one where the filled form the person approved
       isn't on screen: it can't find the save and writes nothing. The
       approval is then spent, and a second resume can't use it.
    3. Desktop runs the item again and the person answers in time: it saves
       exactly once.

    The run has no profile (a Standard ``run`` needs a signed production
    admission this test doesn't have), so its report carries no transaction
    outcome: MockMed's record store is the check here.
    """

    base_url = check_it.base_url
    demo._http_json(f"{base_url}api/reset", method="POST", body={})
    params_file = check_it.work / "run-it-params.json"
    params_file.write_text(json.dumps(check_it.params), encoding="utf-8")
    params_file.chmod(0o600)
    target = [
        "--backend",
        "web",
        "--url",
        check_it.entry_url,
        "--effects-kind",
        "rest",
        "--effects-base-url",
        base_url,
        "--params-file",
        str(params_file),
        "--config",
        str(approver_keys.approver_config(check_it.bundle.parent / "approvers.yaml")),
    ]

    def run(run_dir: Path, wait_s: str) -> int:
        return main.main(
            [
                "run",
                str(check_it.bundle),
                "--run-dir",
                str(run_dir),
                "--durable",
                "--approval-mode",
                "each_item",
                "--approval-wait",
                wait_s,
                *target,
            ]
        )

    # 1. Nobody answers in time.
    late = check_it.work / "runs" / "run-it-late"
    assert run(late, "1") != 0
    stopped = RunReport.model_validate_json((late / "report.json").read_text())
    assert stopped.approval.decision.value == "approval_required"
    (save,) = [result for result in stopped.results if not result.ok]
    assert "waiting for a person to approve" in (save.error or "")
    assert save.delivery_attempted is not True
    assert _records(base_url) == []

    # 2. A late approval, then a resume in a new browser.
    shown = _flow("item-approval", "show", str(late), env=check_it.env)
    (request,) = json.loads(shown.stdout)["requests"]
    assert request["status"] == "waiting"
    approved = _flow(
        "item-approval",
        "approve",
        str(late),
        "--request-id",
        request["request_id"],
        "--request-sha256",
        request["request_sha256"],
        "--decided-by",
        "human",
        "--route",
        "desktop_run_it",
        "--unauthenticated",
        "--approver-key",
        str(check_it.approver_key),
        env=check_it.env,
    )
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert json.loads(approved.stdout)["resume_required"] is True
    assert main.main(["resume", str(late), *target]) != 0
    assert _records(base_url) == []
    assert main.main(["resume", str(late), *target]) != 0
    assert _records(base_url) == []

    # 3. The item again, answered in time.
    again = check_it.work / "runs" / "run-it-again"
    with DesktopRunIt(again, check_it.env, check_it.approver_key) as person:
        assert run(again, "120") == 0
    assert person.errors == []
    assert len(person.answers) == 1
    saved = RunReport.model_validate_json((again / "report.json").read_text())
    (approval,) = saved.approval.approvals
    assert approval.decision == "approved"
    assert saved.approval.saves_without_person == 0
    records = _records(base_url)
    assert len(records) == 1 and records[0]["note"] == TUTORIAL_NOTE
