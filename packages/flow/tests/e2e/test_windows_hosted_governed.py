"""The governed run path on Windows: Standard profile, an approval, a hold and a resume.

Builds on ``test_windows_hosted_replay.py`` (same app, database and agent on
this machine). The recording is compiled into a bundle with a qualification
project for one practice case, then run through the real CLI as Desktop runs
it:

1. ``qualify run-case`` under Standard, ``each_item`` approval. The run waits
   before the save; a person's answer, signed with a registered approver key,
   is given through ``item-approval approve`` in a separate process. The save
   lands once and the SQLite record check confirms it (``VERIFIED``).
2. The same case with a short ``--approval-wait`` and nobody answering. The
   run stops before the save and is held; nothing is written. The person then
   approves, and ``resume`` finishes the item from its durable checkpoint
   (``VERIFIED``).

The database decides every outcome; it is read with ``pn_db.py all``, never
through the screen.

Opt-in: skipped unless ``OAFLOW_WINDOWS_HOSTED_E2E=1`` on Windows.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_windows_hosted_replay import (
    DEMO_NOTE,
    DEMO_SEARCH,
    RUN_E2E,
    TARGET_ID,
    _Host,
    _points,
)

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in hosted Windows e2e; set OAFLOW_WINDOWS_HOSTED_E2E=1 on Windows",
    ),
    pytest.mark.timeout(1500),
]

FLOW = Path(__file__).resolve().parents[2]
BUNDLE_KEY = "windows-governed-test-key"
CAMPAIGN_ID = "windows-governed-local"
RUNNER_KEY_ID = "windows-governed-runner"
EVIDENCE_ENV = "OAFLOW_WINDOWS_GOVERNED_EVIDENCE"


def _flow(*argv: str, env: dict[str, str], timeout: float = 600):
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=FLOW,
        timeout=timeout,
        check=False,
    )


class _Person:
    """Answers waiting approval requests from a separate process, as Desktop does."""

    def __init__(self, run_dir: Path, env: dict[str, str], key: Path) -> None:
        self.run_dir, self.env, self.key = run_dir, env, key
        self.shown: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "_Person":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=60)

    def answer_waiting(self) -> int:
        shown = _flow("item-approval", "show", str(self.run_dir), env=self.env)
        if shown.returncode != 0:
            self.errors.append(shown.stdout + shown.stderr)
            return 0
        rows = json.loads(shown.stdout)["requests"]
        waiting = [r for r in rows if r["status"] in ("waiting", "timed_out")]
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
                "--approver-key",
                str(self.key),
                env=self.env,
            )
            if answered.returncode != 0:
                self.errors.append(answered.stdout + answered.stderr)
                continue
            self.answers.append(json.loads(answered.stdout))
        return len(waiting)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if (self.run_dir / "item-approval").is_dir():
                shown = _flow("item-approval", "show", str(self.run_dir), env=self.env)
                if shown.returncode == 0 and any(
                    r["status"] == "waiting"
                    for r in json.loads(shown.stdout)["requests"]
                ):
                    self.answer_waiting()
                    return
            time.sleep(0.5)


def _build_bundle(backend, recording: Path, work: Path, agent_url: str) -> Path:
    """Compile, declare the save's record check, and bind one practice case."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openadapt_flow import __version__
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        IdentityEnforcement,
        IdentityPolicy,
        QualificationActionTarget,
        QualificationCase,
        QualificationCaseKind,
        QualificationOutcome,
        add_case,
        init_project,
        set_action_classification,
        set_case_scope,
        set_effect_policy,
        set_identity_policy,
        set_trusted_runner_key,
    )
    from openadapt_flow.qualification_environment import (
        BackendQualificationEnvironmentObserver,
    )
    from openadapt_flow.runtime.authorization import (
        runtime_inputs_bytes,
        runtime_inputs_digest,
    )
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr
    from openadapt_flow.verification import VerificationTier

    workflow = compile_recording(recording, work / "compiled", name="pn-governed")
    clicks = [s for s in workflow.steps if s.action is ActionKind.CLICK]
    save = clicks[-1]
    save.risk = "irreversible"
    # The operator's record check: the note on the selected patient.
    save.effects = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={
                "first": ValueExpr(literal="Neil"),
                "last": ValueExpr(literal="Sorenson"),
            },
            field="note",
            value=ValueExpr(param="note"),
            # Saving the same note again is the same write, not a second one.
            idempotency_key=ValueExpr(param="note"),
            key_field="note",
            risk="irreversible",
            probe="read-only patients row lookup",
        )
    ]
    observer = BackendQualificationEnvironmentObserver(backend)
    observed = observer.observe(backend, "windows")
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="windows",
            application="Patient Notes (in-tree WinForms fixture)",
            application_identity=observed.application_identity,
            application_version=observed.application_version,
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=observer.contract_sha256,
            environment_digest=observed.environment_digest,
            runtime_version=__version__,
            required_capabilities=["independent_system_of_record", "windows_uia"],
        ),
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    for step in workflow.steps:
        irreversible = step.id == save.id
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
                    "the save writes the patient's note"
                    if irreversible
                    else "search and form entry before the save"
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
    set_effect_policy(
        workflow,
        step_id=save.id,
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    params = {"note": DEMO_NOTE}
    add_case(
        workflow,
        QualificationCase(
            id="practice",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
            description="One practice note on the demonstrated patient",
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
    runner = Ed25519PrivateKey.generate()
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
    return bundle


def _config(work: Path, host: _Host, agent_url: str, token: str, pin: str, root: str):
    path = work / "deployment.yaml"
    path.write_text(
        "\n".join(
            [
                "backend:",
                "  kind: windows",
                f"  agent_url: {agent_url}",
                f"  agent_token: {token}",
                f"  agent_tls_pin: {pin}",
                "effects:",
                "  kind: sql",
                f"  sqlite_database: {json.dumps(str(host.root / 'patients.db'))}",
                "  sql_query: SELECT id, first, last, dob, note FROM patients",
                "approvers:",
                f"  root_public_key: {root}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _focus_app(backend, points) -> None:
    """Give the app the foreground, as the person who opened it would."""

    backend.click(*points["searchBox"])
    time.sleep(0.5)


def _report(run_dir: Path):
    from openadapt_flow.ir import RunReport

    return RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )


def test_standard_run_with_approval_hold_and_resume(host, tmp_path) -> None:
    evidence: dict[str, Any] = {}
    try:
        _governed(host, tmp_path, evidence)
    finally:
        shutil.rmtree(_state_root(tmp_path), ignore_errors=True)
        if os.environ.get(EVIDENCE_ENV):
            Path(os.environ[EVIDENCE_ENV]).write_text(
                json.dumps(evidence, indent=2, default=str)
            )


def _state_root(tmp_path: Path) -> Path:
    return Path.home() / f".openadapt-ci-{tmp_path.name}"


def _governed(host, tmp_path, evidence: dict[str, Any]) -> None:
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from openadapt_flow.ir import StructuralLocator
    from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
    from tests import _approver_keys as approver_keys

    backend = host.start_agent()
    host.launch_app()
    points = _points(backend)

    def driver(rec) -> None:
        rec.click(*points["searchBox"])
        rec.type_text(DEMO_SEARCH)
        rec.press("Enter")
        time.sleep(0.4)
        rec.click(*points["searchButton"])
        time.sleep(0.4)
        rec.click(*points["noteBox"])
        rec.type_text(DEMO_NOTE, param="note")
        rec.click(*points["saveButton"])
        time.sleep(0.4)

    recording = record_desktop_demo(backend, tmp_path / "recording", driver)
    # The operator marks the record-identifying region once, as
    # `record --identifier X,Y,W,H` does: the patient header above the note.
    header = backend.locate_structural(StructuralLocator(automation_id="patientHeader"))
    assert header is not None, "UIA never found the patient header"
    hx, hy = int(header.point[0]), int(header.point[1])
    meta_path = recording / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["identifier_region"] = [hx - 190, hy - 11, 380, 22]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    host.launch_app()
    _focus_app(backend, _points(backend))
    evidence["environment_at_qualification"] = list(
        backend.qualification_environment_identity() or ()
    )
    bundle = _build_bundle(backend, recording, tmp_path, backend.server_url)
    lint = _flow(
        "certify",
        str(bundle),
        "--policy",
        "clinical-write",
        env={**host.env, "OPENADAPT_BUNDLE_KEY": BUNDLE_KEY},
    )
    evidence["certify"] = (lint.stdout + lint.stderr)[-6000:]

    key_dir = tmp_path / "approver-keys"
    state_root = _state_root(tmp_path)
    env = {
        **host.env,
        # The attempt ledger must sit in this user's profile, as the default
        # ~/.openadapt does; the runner's temp folder is on another drive.
        "OPENADAPT_HOME": str(state_root),
        "OPENADAPT_BUNDLE_KEY": BUNDLE_KEY,
        KEY_DIR_ENV: str(key_dir),
        KEY_PASSPHRASE_ENV: approver_keys.PASSPHRASE,
    }
    os.environ.update({KEY_DIR_ENV: str(key_dir)})
    approver_key, _signer = approver_keys.provision(key_dir)
    root = approver_keys.root_of(key_dir)
    env[ROOT_ENV] = root
    config = _config(
        tmp_path,
        host,
        backend.server_url,
        backend._auth_token,
        backend._pin_fingerprint,
        root,
    )

    def run_case(run_id: str, wait_s: int):
        run_dir = tmp_path / "runs" / run_id
        argv = [
            "qualify",
            "run-case",
            str(bundle),
            "--case-id",
            "practice",
            "--inputs",
            str(tmp_path / "practice-inputs.json"),
            "--campaign-id",
            CAMPAIGN_ID,
            "--run-id",
            run_id,
            "--run-dir",
            str(run_dir),
            "--config",
            str(config),
            "--approval-wait",
            str(wait_s),
        ]
        return run_dir, argv

    # 1. A person answers while the run waits; the save lands and verifies.
    host.launch_app()
    _focus_app(backend, _points(backend))
    evidence["environment_before_run"] = list(
        backend.qualification_environment_identity() or ()
    )
    run_dir, argv = run_case("practice-1", 300)
    with _Person(run_dir, env, approver_key) as person:
        done = _flow(*argv, env=env)
    rows = {r["id"]: r for r in host.rows()}
    evidence["environment_after_run"] = list(
        backend.qualification_environment_identity() or ()
    )
    evidence["approved"] = {
        "exit": done.returncode,
        "tail": (done.stdout + done.stderr)[-3000:],
        "probe": [
            line for line in (done.stdout + done.stderr).splitlines() if "PROBE" in line
        ][:40],
        "answers": person.answers,
        "errors": person.errors,
    }
    assert person.errors == [], evidence["approved"]
    assert done.returncode == 0, evidence["approved"]
    report = _report(run_dir)
    evidence["approved"]["outcome"] = report.transaction_outcome
    assert report.transaction_outcome == "VERIFIED", evidence["approved"]
    assert report.approval is not None and report.approval.decision.value == "approved"
    assert rows[TARGET_ID]["note"] == DEMO_NOTE
    assert all(r["note"] == "" for i, r in rows.items() if i != TARGET_ID)

    # 2. Nobody answers in time: the run is held before the save and writes
    # nothing. The person approves later and `resume` finishes it.
    host.launch_app()
    _focus_app(backend, _points(backend))
    run_dir, argv = run_case("practice-2", 5)
    held = _flow(*argv, env=env)
    rows = {r["id"]: r for r in host.rows()}
    evidence["held"] = {
        "exit": held.returncode,
        "tail": (held.stdout + held.stderr)[-3000:],
        "probe": [
            line for line in (held.stdout + held.stderr).splitlines() if "PROBE" in line
        ][:40],
    }
    assert held.returncode != 0, evidence["held"]
    assert all(r["note"] == "" for r in rows.values()), "a held run wrote"
    evidence["held"]["outcome"] = _report(run_dir).transaction_outcome
    person = _Person(run_dir, env, approver_key)
    assert person.answer_waiting() == 1, person.errors
    assert person.errors == [], person.errors
    evidence["held"]["answer"] = person.answers
    resumed = _flow("resume", str(run_dir), "--config", str(config), env=env)
    rows = {r["id"]: r for r in host.rows()}
    evidence["resumed"] = {
        "exit": resumed.returncode,
        "tail": (resumed.stdout + resumed.stderr)[-3000:],
        "probe": [
            line for line in (resumed.stdout + resumed.stderr).splitlines() if "PROBE" in line
        ][:40],
    }
    assert resumed.returncode == 0, evidence["resumed"]
    assert rows[TARGET_ID]["note"] == DEMO_NOTE
    assert all(r["note"] == "" for i, r in rows.items() if i != TARGET_ID)
    assert _report(run_dir).transaction_outcome == "VERIFIED"


@pytest.fixture
def host(tmp_path):
    h = _Host(tmp_path)
    try:
        yield h
    finally:
        h.close()
