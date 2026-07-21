"""Operator console: read-only projections, governed actions, loopback boot.

Behavior under test (import-light -- no browser, no OCR):

1. The GET surface is a faithful projection of REAL engine artifacts: bundles
   written by ``Workflow.save``, run dirs written by ``RunReport.save`` +
   ``CheckpointStore``, and skill libraries written by ``SkillLibrary``.
2. Coverage numbers come from the same policy helpers the CLI uses
   (identity armed / effect contracts on irreversible steps) and degrade to
   None -- never a fabricated 100% -- when a bundle has no consequential step.
3. Mutating endpoints are FAIL-CLOSED: read-only servers refuse with 403 and
   the exact CLI command; non-executable verbs (teach) are never executed;
   artifact serving refuses path traversal.
4. With actions enabled, ``approve`` shells out to the real CLI verb and
   ``promote``/``rollback`` go through the same ``SkillLibrary`` entry points
   the teach pipeline uses.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
import shutil
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from openadapt_flow.console.app import create_app  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    HaltObservation,
    IdentityCheck,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.learning.library import SkillLibrary  # noqa: E402
from openadapt_flow.runtime.durable.checkpoint import (  # noqa: E402
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.effects import Effect, EffectKind  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures: a real bundle, a halted run, a paused run, a skill library
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\nfake-crop-bytes"


def _armed_click(step_id: str, *, risk: str = "reversible", effects=()) -> Step:
    return Step(
        id=step_id,
        intent=f"click {step_id}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save",
            context_text="Jane Roe 1980-01-01 MRN 12345",
        ),
        risk=risk,
        effects=list(effects),
    )


def _unarmed_click(step_id: str) -> Step:
    return Step(
        id=step_id,
        intent=f"click {step_id}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(10, 10, 40, 18),
            click_point=(20, 15),
        ),
        identity_armed=False,
        identity_unarmed_reason="no identity context recorded at compile time",
    )


def _make_bundle(root: Path, dirname: str, *, extra_step: bool = False) -> Path:
    b = root / dirname
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(_PNG)
    steps = [
        _armed_click(
            "s1",
            risk="irreversible",
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"patient_id": "p1"},
                )
            ],
        ),
        _unarmed_click("s2"),
        Step(id="s3", intent="press enter", action=ActionKind.KEY, key="Enter"),
    ]
    if extra_step:
        steps.append(
            Step(id="s4", intent="press tab", action=ActionKind.KEY, key="Tab")
        )
    wf = Workflow(name="triage-note", steps=steps, params={"note": "hello"})
    wf.save(b)
    return b


def _make_halted_run(runs_root: Path, dirname: str) -> Path:
    run = runs_root / dirname
    run.mkdir(parents=True)
    (run / "s1_before.png").write_bytes(_PNG)
    (run / "s1_after.png").write_bytes(_PNG)
    report = RunReport(
        workflow_name="triage-note",
        started_at="2026-07-17T10:00:00+00:00",
        success=False,
        results=[
            StepResult(
                step_id="s1",
                intent="click s1",
                ok=True,
                identity=IdentityCheck(
                    status="verified",
                    mode="context",
                    expected="Jane Roe MRN-SECRET",
                    observed="Jane Roe MRN-SECRET",
                ),
                effect_verified=True,
                effect_results=["CONFIRMED record_written patient_id=p1"],
                before_png="s1_before.png",
                after_png="s1_after.png",
                elapsed_ms=812.0,
            ),
            StepResult(
                step_id="s2",
                intent="click s2",
                ok=False,
                error="resolution failed: template not found",
                elapsed_ms=15000.0,
            ),
        ],
        params={"patient_id": "MRN-SECRET"},
        halt=HaltObservation(
            state_id="s2",
            intent="click s2",
            reason="resolution failed: template not found",
            observed_texts=["Session expired", "Log in again"],
            completed_intents=["click s1"],
        ),
        identity_applicable_steps=2,
        identity_armed_steps=1,
        total_ms=15900.0,
    )
    report.save(run)
    return run


def _make_paused_run(runs_root: Path, dirname: str, bundle: Path) -> Path:
    run = runs_root / dirname
    run.mkdir(parents=True)
    report = RunReport(
        workflow_name="triage-note",
        started_at="2026-07-17T11:00:00+00:00",
        success=False,
        results=[],
        total_ms=1000.0,
    )
    report.save(run)
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(workflow_name="triage-note", bundle_dir=str(bundle))
    )
    store.write_checkpoint(
        RunCheckpoint(
            workflow_name="triage-note",
            step_index=0,
            step_id="s1",
            intent="click s1",
            next_step_index=1,
        )
    )
    store.write_pending(
        PendingEscalation(
            workflow_name="triage-note",
            step_index=1,
            step_id="s2",
            intent="click s2",
            category="effect_indeterminate",
            reason="verifier could not confirm the write",
            proposed_options=["approve and resume", "teach a fix"],
            resume_from_index=1,
        )
    )
    return run


def _make_library(root: Path) -> Path:
    # A minimal real ProgramGraph via the linear lift of a tiny workflow.
    from openadapt_flow.ir import lift_to_program

    wf = Workflow(name="lib-wf", steps=[_armed_click("t1")])
    graph = lift_to_program(wf)
    lib = SkillLibrary(root)
    lib.create_skill("triage-note", graph)
    lib.add_candidate("triage-note", graph, validation_score=0.9)
    return lib.path


@pytest.fixture()
def console_env(tmp_path: Path):
    bundles = tmp_path / "bundles"
    runs = tmp_path / "runs"
    bundles.mkdir()
    runs.mkdir()
    bundle = _make_bundle(bundles, "triage-note")
    bundle_v2 = _make_bundle(bundles, "triage-note-v2", extra_step=True)
    halted = _make_halted_run(runs, "replay-halted")
    paused = _make_paused_run(runs, "replay-paused", bundle)
    library = _make_library(bundles / "triage-note.skills")
    return {
        "bundles": bundles,
        "runs": runs,
        "bundle": bundle,
        "bundle_v2": bundle_v2,
        "halted": halted,
        "paused": paused,
        "library": library,
    }


@pytest.fixture(autouse=True)
def _server_derived_test_operator(monkeypatch):
    """Keep attribution deterministic while production derives the OS account."""
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity",
        lambda: "local-operator",
    )


def _client(env, *, allow_actions: bool = False) -> TestClient:
    app = create_app(
        env["bundles"],
        env["runs"],
        allow_actions=allow_actions,
    )
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )


def _workflow_id(client: TestClient, *, n_steps: int) -> str:
    return next(
        workflow["id"]
        for workflow in client.get("/api/workflows").json()
        if workflow["n_steps"] == n_steps
    )


def _run_id(client: TestClient, *, halted: bool = False, paused: bool = False) -> str:
    return next(
        run["id"]
        for run in client.get("/api/runs").json()
        if run["halted"] is halted and run["paused"] is paused
    )


# ---------------------------------------------------------------------------
# 1. read surface
# ---------------------------------------------------------------------------


def test_health_reports_read_only(console_env):
    body = _client(console_env).get("/api/health").json()
    assert body["status"] == "ok"
    assert body["read_only"] is True
    assert body["attended_decisions_ready"] is False
    assert body["attended_actions_ready"] is False
    assert "bundles_root" not in body
    assert "runs_root" not in body


def test_index_serves_ui(console_env):
    r = _client(console_env).get("/")
    assert r.status_code == 200
    assert "Operator Console" in r.text


def test_workflow_list_with_last_run(console_env):
    body = _client(console_env).get("/api/workflows").json()
    assert len(body) == 2
    assert all(len(w["id"]) == 24 for w in body)
    assert all("name" not in w for w in body)
    w = next(x for x in body if x["n_steps"] == 3)
    assert w["n_steps"] == 3
    assert w["compiler_version"]  # sealed by Workflow.save
    assert w["certified"] is False
    # An unsealed legacy report has no digest join; never join by raw workflow
    # name because that label can contain recorded identifiers.
    assert w["last_run"] is None


def test_workflow_detail_coverage_from_policy_helpers(console_env):
    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    body = client.get(f"/api/workflows/{bundle_id}").json()
    idc = body["identity_coverage"]
    assert idc["applicable"] == 2
    assert idc["armed"] == 1
    assert idc["unarmed"][0]["step_id"] == "step-002"
    efc = body["effect_coverage"]
    assert efc["consequential"] == 1
    assert efc["consequential_with_contract"] == 1
    assert efc["coverage_pct"] == 100.0
    # lint flags the unarmed click (same helper the CLI lint verb uses)
    codes = {f["code"] for f in body["lint"]["findings"]}
    assert "unarmed_click" in codes
    steps = {s["id"]: s for s in body["steps"]}
    assert steps["step-001"]["identity_armed"] is True
    assert steps["step-001"]["effects"][0]["kind"] == "record_written"
    assert steps["step-002"]["identity_armed"] is False
    assert steps["step-003"]["identity_applicable"] is False


def test_effect_coverage_degrades_to_none_without_consequential_steps(tmp_path):
    bundles = tmp_path / "b"
    bundles.mkdir()
    b = bundles / "benign"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(_PNG)
    Workflow(name="benign", steps=[_armed_click("s1")]).save(b)
    runs = tmp_path / "r"
    runs.mkdir()
    app = create_app(bundles, runs)
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
        },
    )
    bundle_id = _workflow_id(client, n_steps=1)
    efc = client.get(f"/api/workflows/{bundle_id}").json()["effect_coverage"]
    assert efc["consequential"] == 0
    assert efc["coverage_pct"] is None  # honest n/a, not a fake 100%


def test_workflow_detail_live_certification_via_policy_param(console_env):
    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    body = client.get(
        f"/api/workflows/{bundle_id}", params={"policy": "clinical-write"}
    ).json()
    cert = body["certification"]
    assert cert["sealed"]["certified"] is False
    live = cert["live"]
    assert live is not None and live["policy_name"] == "clinical-write"
    # the unarmed click must surface as a violation, not be hidden
    assert live["passed"] is False
    assert any(v["rule"] == "prohibit_unarmed_clicks" for v in live["violations"])
    assert all(set(v) == {"rule"} for v in live["violations"])


def test_workflow_diff(console_env):
    client = _client(console_env)
    original = _workflow_id(client, n_steps=3)
    updated = _workflow_id(client, n_steps=4)
    body = client.get(f"/api/workflows/{original}/diff/{updated}").json()
    assert body["steps_added_count"] == 1
    assert body["steps_removed_count"] == 0
    assert body["steps_changed_count"] == 0
    assert body["identical"] is False


def test_unreadable_bundle_degrades(console_env):
    bad = console_env["bundles"] / "corrupt"
    bad.mkdir()
    (bad / "workflow.json").write_text("{ not json")
    listed = _client(console_env).get("/api/workflows").json()
    entry = next(w for w in listed if w["load_error"])
    assert entry["load_error"]
    body = _client(console_env).get(f"/api/workflows/{entry['id']}").json()
    assert body["load_error"]


def test_unknown_ids_404(console_env):
    client = _client(console_env)
    assert client.get("/api/workflows/nope").status_code == 404
    assert client.get("/api/runs/nope").status_code == 404


# ---------------------------------------------------------------------------
# 2. runs: history, timeline, halt evidence, artifacts
# ---------------------------------------------------------------------------


def test_run_list_flags(console_env):
    runs = _client(console_env).get("/api/runs").json()
    assert all(len(run["id"]) == 24 for run in runs)
    halted = next(run for run in runs if run["halted"])
    assert halted["halted"] is True and halted["paused"] is False
    assert halted["n_failed"] == 1
    assert halted["identity_armed_steps"] == 1
    paused = next(run for run in runs if run["paused"])
    assert paused["paused"] is True and paused["approved"] is False
    # newest first
    assert runs[0]["id"] == paused["id"]


def test_run_detail_timeline_and_halt(console_env):
    client = _client(console_env)
    run_id = _run_id(client, halted=True)
    body = client.get(f"/api/runs/{run_id}").json()
    t = body["timeline"]
    assert [x["step_id"] for x in t] == ["step-001", "step-002"]
    assert t[0]["identity"]["status"] == "verified"
    assert t[0]["effect_verified"] is True
    assert t[0]["before_artifact_id"] == "step-001-before"
    halt = body["halt"]
    assert halt["observed_text_count"] == 2
    assert halt["completed_intent_count"] == 1


def test_run_detail_pause_checkpoints_manifest(console_env):
    client = _client(console_env)
    run_id = _run_id(client, paused=True)
    body = client.get(f"/api/runs/{run_id}").json()
    pending = body["pending_escalation"]
    assert pending["category"] == "operator_review"
    assert pending["resume_from_index"] == 1
    assert body["manifest"] == {"present": True}
    assert body["checkpoints"][0]["step_index"] == 0


def test_artifact_served_and_traversal_refused(console_env):
    client = _client(console_env)
    run_id = _run_id(client, halted=True)
    ok = client.get(f"/api/runs/{run_id}/artifact", params={"id": "step-001-before"})
    assert ok.status_code == 200
    assert ok.content == _PNG
    for evil in ("../replay-paused/report.json", "/etc/passwd", "..%2f..", ".."):
        r = client.get(f"/api/runs/{run_id}/artifact", params={"id": evil})
        assert r.status_code == 404, evil


# ---------------------------------------------------------------------------
# 3. governance actions: catalog + fail-closed read-only gate
# ---------------------------------------------------------------------------


def test_halted_run_offers_teach_as_copy_only(console_env):
    client = _client(console_env)
    run_id = _run_id(client, halted=True)
    actions = client.get(f"/api/runs/{run_id}/actions").json()
    by_id = {a["id"]: a for a in actions}
    teach = by_id["teach"]
    assert teach["executable"] is False
    assert "openadapt-flow teach" in teach["command"]
    assert "--fix" in teach["command"]
    # not paused => approve/resume do not apply
    assert "approve" not in by_id and "resume" not in by_id


def test_paused_run_offers_approve_resume_with_exact_commands(console_env):
    client = _client(console_env)
    run_id = _run_id(client, paused=True)
    actions = client.get(f"/api/runs/{run_id}/actions").json()
    by_id = {a["id"]: a for a in actions}
    assert by_id["approve"]["executable"] is True
    assert by_id["approve"]["command"].startswith("openadapt-flow approve ")
    assert "--approver '<local-os-account>'" in by_id["approve"]["command"]
    assert str(console_env["paused"]) not in str(actions)
    assert by_id["resume"]["executable"] is False
    assert "--require-approval" in by_id["resume"]["command"]
    assert "<deployment.yaml>" in by_id["resume"]["command"]


def test_read_only_refuses_mutation_with_command(console_env):
    client = _client(console_env)
    run_id = _run_id(client, paused=True)
    r = client.post(f"/api/runs/{run_id}/actions/approve", json={})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "read-only" in detail["error"]
    assert detail["command"].startswith("openadapt-flow approve ")


def test_teach_never_executes_even_with_actions_enabled(console_env):
    client = _client(console_env, allow_actions=True)
    run_id = _run_id(client, halted=True)
    r = client.post(f"/api/runs/{run_id}/actions/teach", json={})
    assert r.status_code == 409
    assert "openadapt-flow teach" in r.json()["detail"]["command"]


def test_unknown_action_404(console_env):
    client = _client(console_env, allow_actions=True)
    run_id = _run_id(client, paused=True)
    r = client.post(f"/api/runs/{run_id}/actions/delete-everything", json={})
    assert r.status_code == 404


def test_approve_executes_cli_verb(console_env, monkeypatch):
    from openadapt_flow.console import actions as actions_mod

    calls = {}

    def fake_run(argv, capture_output, text, timeout):
        calls["argv"] = argv

        class P:
            returncode = 0
            stdout = "approval recorded"
            stderr = ""

        return P()

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    client = _client(console_env, allow_actions=True)
    run_id = _run_id(client, paused=True)
    r = client.post(
        f"/api/runs/{run_id}/actions/approve",
        json={"approver": "forged-user", "resolution": "retry after login"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["returncode"] == 0
    # the console ran the REAL CLI verb, module-invoked, with the run dir
    assert calls["argv"][1:3] == ["-m", "openadapt_flow"]
    assert calls["argv"][3] == "approve"
    assert str(console_env["paused"]) in calls["argv"]
    assert "local-operator" in calls["argv"]
    assert "forged-user" not in calls["argv"]


def test_certify_action_runs_without_allow_actions(console_env, monkeypatch):
    """certify is non-mutating: executable even on a read-only server."""
    from openadapt_flow.console import actions as actions_mod

    def fake_run(argv, capture_output, text, timeout):
        class P:
            returncode = 2
            stdout = "FAIL: workflow 'triage-note'"
            stderr = ""

        return P()

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    r = client.post(
        f"/api/workflows/{bundle_id}/actions/certify",
        json={"policy": "clinical-write"},
    )
    assert r.status_code == 200
    assert r.json()["returncode"] == 2


# ---------------------------------------------------------------------------
# 4. skill library: lineage view + promote/rollback entry points
# ---------------------------------------------------------------------------


def test_skills_lineage_listed(console_env):
    libs = _client(console_env).get("/api/skills").json()
    assert len(libs) == 1
    assert "path" not in libs[0]
    assert len(libs[0]["id"]) == 24
    skill = libs[0]["skills"][0]
    assert len(skill["id"]) == 24
    assert "skill_id" not in skill
    statuses = {v["version"]: v["status"] for v in skill["versions"]}
    assert statuses == {1: "active", 2: "candidate"}


def test_promote_via_library_entry_point(console_env):
    client = _client(console_env, allow_actions=True)
    lib_path = str(console_env["library"])
    library_id = client.get("/api/skills").json()[0]["id"]
    skill_id = client.get("/api/skills").json()[0]["skills"][0]["id"]
    r = client.post(
        f"/api/skills/{skill_id}/actions/promote",
        json={"library": library_id, "version": 2},
    )
    assert r.status_code == 200 and r.json()["returncode"] == 0
    lib = SkillLibrary(Path(lib_path).parent)
    assert lib.active_version("triage-note").version == 2
    assert lib.get("triage-note").by_version(1).status == "superseded"


def test_rollback_via_library_entry_point(console_env):
    client = _client(console_env, allow_actions=True)
    lib_path = str(console_env["library"])
    library_id = client.get("/api/skills").json()[0]["id"]
    skill_id = client.get("/api/skills").json()[0]["skills"][0]["id"]
    r = client.post(
        f"/api/skills/{skill_id}/actions/rollback",
        json={
            "library": library_id,
            "version": 2,
            "reason": "regressed on validation",
        },
    )
    assert r.status_code == 200 and r.json()["returncode"] == 0
    lib = SkillLibrary(Path(lib_path).parent)
    v2 = lib.get("triage-note").by_version(2)
    assert v2.status == "rolled_back"
    assert v2.reason == "regressed on validation"


def test_skill_actions_read_only_refused_with_command(console_env):
    client = _client(console_env)
    library_id = client.get("/api/skills").json()[0]["id"]
    skill_id = client.get("/api/skills").json()[0]["skills"][0]["id"]
    r = client.post(
        f"/api/skills/{skill_id}/actions/promote",
        json={"library": library_id, "version": 2},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["command"] == (
        "server-bound governed skill action; no local path exported"
    )


def test_skill_library_outside_roots_404(console_env, tmp_path):
    foreign = tmp_path / "elsewhere"
    _make_library(foreign)
    r = _client(console_env, allow_actions=True).post(
        "/api/skills/triage-note/actions/promote",
        json={"library": "0" * 24, "version": 2},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 5. smoke: the server boots on loopback against the fixture dirs
# ---------------------------------------------------------------------------


def test_server_boots_on_loopback(console_env):
    uvicorn = pytest.importorskip("uvicorn")
    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = create_app(console_env["bundles"], console_env["runs"])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        health = None
        while time.time() < deadline:
            try:
                health = httpx.get(
                    f"http://127.0.0.1:{port}/api/health",
                    headers={
                        "Authorization": (f"Bearer {app.state.console_access_token}")
                    },
                    timeout=1.0,
                )
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert health is not None, "server did not come up"
        assert health.json()["status"] == "ok"
        page = httpx.get(f"http://127.0.0.1:{port}/", timeout=5.0)
        assert "Operator Console" in page.text
        wf = httpx.get(
            f"http://127.0.0.1:{port}/api/workflows",
            headers={"Authorization": f"Bearer {app.state.console_access_token}"},
            timeout=5.0,
        )
        assert len(wf.json()) == 2
        assert all(len(w["id"]) == 24 for w in wf.json())
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# 6. adversarial security boundary
# ---------------------------------------------------------------------------


def test_api_requires_bearer_but_fixed_shell_is_public(console_env):
    app = create_app(
        console_env["bundles"],
        console_env["runs"],
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    shell = client.get("/")
    assert shell.status_code == 200
    assert app.state.console_access_token not in shell.text
    assert client.get("/static/console.js").status_code == 200
    denied = client.get("/api/health")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    allowed = client.get(
        "/api/health",
        headers={"Authorization": f"Bearer {app.state.console_access_token}"},
    )
    assert allowed.status_code == 200


def test_host_and_origin_are_strict(console_env):
    app = create_app(
        console_env["bundles"],
        console_env["runs"],
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    auth = {"Authorization": f"Bearer {app.state.console_access_token}"}
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert (
        client.get(
            "/api/health",
            headers={
                **auth,
                "Host": "attacker.example",
                "Origin": "https://attacker.example",
            },
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/health",
            headers={**auth, "Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/health",
            headers={**auth, "Origin": "http://localhost"},
        ).status_code
        == 403
    )


def test_mutations_require_origin_json_and_csrf(console_env):
    app = create_app(
        console_env["bundles"],
        console_env["runs"],
        allow_actions=True,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    auth = {"Authorization": f"Bearer {app.state.console_access_token}"}
    list_client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers=auth,
    )
    run_id = _run_id(list_client, paused=True)
    path = f"/api/runs/{run_id}/actions/approve"
    assert client.post(path, headers=auth, json={}).status_code == 403
    same_origin = {**auth, "Origin": "http://127.0.0.1"}
    assert client.post(path, headers=same_origin, json={}).status_code == 403
    assert (
        client.post(
            path,
            headers={
                **same_origin,
                "X-OpenAdapt-CSRF": app.state.console_csrf_token,
                "Content-Type": "text/plain",
            },
            content="{}",
        ).status_code
        == 415
    )


def test_security_headers_and_strict_csp(console_env):
    client = _client(console_env)
    for path in ("/", "/static/console.js", "/api/health"):
        response = client.get(path)
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp
        assert "'unsafe-eval'" not in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "data:" not in csp


def test_static_ui_has_no_inline_handlers_or_artifact_paths(console_env):
    client = _client(console_env)
    html = client.get("/").text.lower()
    script = client.get("/static/console.js").text
    assert "<style" not in html
    assert "<script src=" in html
    assert re.search(r"\son[a-z]+\s*=", html) is None
    assert "style=" not in html
    assert "library.path" not in script
    assert "payload.approver" not in script


def test_sensitive_bundle_and_run_values_never_cross_api(console_env):
    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    workflow = client.get(f"/api/workflows/{bundle_id}").json()
    assert "params" not in workflow
    assert "param_specs" not in workflow
    assert workflow["parameter_count"] == 1
    assert "hello" not in str(workflow)
    run_id = _run_id(client, halted=True)
    run = client.get(f"/api/runs/{run_id}").json()
    serialized = str(run)
    assert "report" not in run
    assert "MRN-SECRET" not in serialized
    assert "Jane Roe" not in serialized
    assert "Session expired" not in serialized
    assert run["timeline"][0]["identity"] == {
        "status": "verified",
        "mode": "context",
        "coverage": 0.0,
    }


def test_adversarial_labels_urls_mrns_and_paths_are_redacted(console_env):
    """Every browser projection stays safe even when all source labels are PHI."""
    secret = "Jane Roe MRN-7788 https://ehr.example/patient/7788"
    path_label = "Jane-Roe-MRN-7788-ehr.example"

    workflow = Workflow.load(console_env["bundle"])
    workflow.name = secret
    workflow.params = {secret: secret}
    workflow.steps[0].id = secret
    workflow.steps[0].intent = secret
    workflow.steps[0].anchor.ocr_text = secret
    workflow.steps[0].anchor.context_text = secret
    assert workflow.manifest is not None
    assert workflow.manifest.provenance is not None
    workflow.manifest.provenance.certification_status = secret
    workflow.save(console_env["bundle"])

    report = RunReport.model_validate_json(
        (console_env["halted"] / "report.json").read_text()
    )
    report.workflow_name = secret
    report.params = {secret: secret}
    report.terminal_outcome = secret
    report.results[0].step_id = secret
    report.results[0].intent = secret
    report.results[0].actuation = secret
    report.results[0].effect_results = [secret]
    report.results[0].error = secret
    protected_image = console_env["halted"] / f"{path_label}.png"
    (console_env["halted"] / "s1_before.png").replace(protected_image)
    report.results[0].before_png = protected_image.name
    assert report.halt is not None
    report.halt.state_id = secret
    report.halt.intent = secret
    report.halt.reason = secret
    report.halt.outcome = secret
    report.halt.observed_texts = [secret]
    report.halt.completed_intents = [secret]
    report.save(console_env["halted"])

    pending_path = console_env["paused"] / "pending_escalation.json"
    pending = json.loads(pending_path.read_text())
    for field in (
        "workflow_name",
        "step_id",
        "intent",
        "category",
        "reason",
    ):
        pending[field] = secret
    pending["proposed_options"] = [secret]
    pending_path.write_text(json.dumps(pending))
    checkpoint_path = next((console_env["paused"] / "checkpoints").glob("step_*.json"))
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["step_index"] = secret
    checkpoint["created_at"] = secret
    checkpoint_path.write_text(json.dumps(checkpoint))

    library = SkillLibrary(console_env["library"].parent)
    graph = library.active_version("triage-note").graph
    library.create_skill(secret, graph)
    protected_skill = library.get(secret).versions[0]
    protected_skill.provenance.note = secret
    protected_skill.reason = secret
    protected_skill.provenance.trace_ids = [secret]
    library.save()

    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    halted_id = _run_id(client, halted=True)
    paused_id = _run_id(client, paused=True)
    responses = [
        client.get("/api/workflows"),
        client.get(f"/api/workflows/{bundle_id}"),
        client.get(f"/api/workflows/{bundle_id}/actions"),
        client.get("/api/runs"),
        client.get(f"/api/runs/{halted_id}"),
        client.get(f"/api/runs/{halted_id}/actions"),
        client.get(f"/api/runs/{paused_id}"),
        client.get(f"/api/runs/{paused_id}/actions"),
        client.get("/api/skills"),
        client.post(f"/api/runs/{paused_id}/actions/approve", json={}),
    ]
    assert all(response.status_code in {200, 403} for response in responses)
    serialized = "\n".join(response.text for response in responses)
    for forbidden in (
        "Jane Roe",
        "MRN-7788",
        "ehr.example",
        path_label,
        str(console_env["bundle"]),
        str(console_env["halted"]),
        str(console_env["paused"]),
        str(console_env["library"]),
    ):
        assert forbidden not in serialized

    artifact = client.get(
        f"/api/runs/{halted_id}/artifact",
        params={"id": "step-001-before"},
    )
    assert artifact.status_code == 200
    assert artifact.content == _PNG
    assert path_label not in str(artifact.headers)


def test_validation_errors_never_echo_rejected_payload(console_env):
    secret = "Jane Roe MRN-9988 https://ehr.example/patient/9988 /private/phi"
    client = _client(console_env, allow_actions=True)
    run_id = _run_id(client, paused=True)
    path = f"/api/runs/{run_id}/actions/approve"

    wrong_shape = client.post(path, json=[secret])
    assert wrong_shape.status_code == 422
    assert wrong_shape.json() == {
        "detail": "request did not match the console action schema"
    }
    assert secret not in wrong_shape.text

    malformed = client.post(
        path,
        content=f'{{"resolution": "{secret}"',
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert secret not in malformed.text


def test_symlinked_run_metadata_is_never_read(console_env, tmp_path):
    secret = "Jane Roe MRN-5544 https://ehr.example/private"
    paused = console_env["paused"]

    pending = paused / "pending_escalation.json"
    pending.unlink()
    outside_pending = tmp_path / "outside-pending.json"
    outside_pending.write_text(json.dumps({"status": secret, "reason": secret}))
    pending.symlink_to(outside_pending)

    outside_approval = tmp_path / "outside-approval.json"
    outside_approval.write_text(json.dumps({"approved_at": secret}))
    (paused / "approval.json").symlink_to(outside_approval)

    checkpoints = paused / "checkpoints"
    shutil.rmtree(checkpoints)
    outside_checkpoints = tmp_path / "outside-checkpoints"
    outside_checkpoints.mkdir()
    (outside_checkpoints / "step_000001.json").write_text(
        json.dumps({"step_index": secret, "created_at": secret})
    )
    (outside_checkpoints / "_manifest.json").write_text(
        json.dumps({"bundle_dir": secret})
    )
    checkpoints.symlink_to(outside_checkpoints, target_is_directory=True)

    client = _client(console_env)
    run_summary = next(
        item
        for item in client.get("/api/runs").json()
        if item["started_at"] == "2026-07-17T11:00:00+00:00"
    )
    assert run_summary["paused"] is False
    assert run_summary["approved"] is False
    run_id = run_summary["id"]
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["pending_escalation"] is None
    assert response.json()["approval"] is None
    assert response.json()["manifest"] is None
    assert response.json()["checkpoints"] == []
    assert secret not in response.text


def test_artifact_requires_bearer(console_env):
    authorized = _client(console_env)
    run_id = _run_id(authorized, halted=True)
    app = create_app(console_env["bundles"], console_env["runs"])
    unauthenticated = TestClient(app, base_url="http://127.0.0.1")
    response = unauthenticated.get(
        f"/api/runs/{run_id}/artifact",
        params={"id": "step-001-before"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "console bearer token required"}


def test_artifact_endpoint_allows_only_report_referenced_real_png(console_env):
    client = _client(console_env)
    run_id = _run_id(client, halted=True)
    run = console_env["halted"]
    (run / "unreferenced.png").write_bytes(_PNG)
    (run / "notes.txt").write_text("protected value")
    for artifact_id in ("unreferenced.png", "notes.txt", "report.json"):
        assert (
            client.get(
                f"/api/runs/{run_id}/artifact", params={"id": artifact_id}
            ).status_code
            == 404
        )
    assert (
        client.get(
            f"/api/runs/{run_id}/artifact",
            params={"id": "step-001-before"},
        ).status_code
        == 200
    )


def test_symlinked_run_and_artifact_are_never_followed(console_env, tmp_path):
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir()
    (outside_run / "secret.png").write_bytes(_PNG)
    RunReport(
        workflow_name="outside",
        started_at="2026-07-18T00:00:00+00:00",
        results=[
            StepResult(
                step_id="s1",
                intent="outside",
                ok=True,
                before_png="secret.png",
            )
        ],
    ).save(outside_run)
    (console_env["runs"] / "linked").symlink_to(outside_run, target_is_directory=True)
    client = _client(console_env)
    assert len(client.get("/api/runs").json()) == 2
    assert client.get("/api/runs/linked").status_code == 404

    run_id = _run_id(client, halted=True)
    outside_file = tmp_path / "outside.png"
    outside_file.write_bytes(_PNG)
    linked_file = console_env["halted"] / "linked.png"
    linked_file.symlink_to(outside_file)
    report = RunReport.model_validate_json(
        (console_env["halted"] / "report.json").read_text()
    )
    report.results[0].before_png = "linked.png"
    report.save(console_env["halted"])
    assert (
        client.get(
            f"/api/runs/{run_id}/artifact",
            params={"id": "step-001-before"},
        ).status_code
        == 404
    )


def test_configured_symlink_root_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="must not traverse a symlink"):
        create_app(linked, real)


def test_skill_copy_command_quotes_hostile_library_path(tmp_path):
    from openadapt_flow.console.actions import actions_for_skill

    hostile = tmp_path / "lib-$(touch PWNED)-`id`" / "skills.json"
    command = actions_for_skill(hostile, "skill';print(1)", 2)[0].command
    argv = shlex.split(command)
    assert argv[3] == str(hostile.parent)
    assert argv[4] == "skill';print(1)"
    assert "$(" not in argv[2]
    assert "`" not in argv[2]


def test_public_command_placeholders_are_literal_shell_arguments(console_env):
    client = _client(console_env)
    run_id = _run_id(client, paused=True)
    commands = [
        action["command"] for action in client.get(f"/api/runs/{run_id}/actions").json()
    ]
    bundle_id = _workflow_id(client, n_steps=3)
    commands.extend(
        action["command"]
        for action in client.get(f"/api/workflows/{bundle_id}/actions").json()
    )
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "openadapt-flow"
        assert all("\n" not in arg and "\r" not in arg for arg in argv)
        for placeholder in (arg for arg in argv if arg.startswith("<")):
            assert placeholder.endswith(">")
            assert shlex.quote(placeholder) in command


def test_custom_policy_paths_are_not_read_by_console(console_env, tmp_path):
    policy = tmp_path / "secret.yaml"
    policy.write_text("name: private\nsecret: MRN-SECRET\n")
    client = _client(console_env)
    bundle_id = _workflow_id(client, n_steps=3)
    response = client.get(f"/api/workflows/{bundle_id}", params={"policy": str(policy)})
    assert response.status_code == 400
    assert "MRN-SECRET" not in response.text


# ---------------------------------------------------------------------------
# 7. CLI wiring
# ---------------------------------------------------------------------------


def test_cli_console_parser_wired():
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(
        ["console", "--bundles", "/b", "--runs", "/r", "--port", "9999"]
    )
    assert args.bundles == "/b"
    assert args.runs == "/r"
    assert args.port == 9999
    assert args.allow_actions is False
    assert args.func.__name__ == "_cmd_console"


def test_encrypted_pending_escalation_flagged(console_env):
    run = console_env["runs"] / "replay-enc"
    run.mkdir()
    RunReport(workflow_name="triage-note", started_at="2026-07-17T12:00:00+00:00").save(
        run
    )
    (run / "pending_escalation.json.enc").write_bytes(b"sealed")
    client = _client(console_env)
    run_id = next(
        item["id"]
        for item in client.get("/api/runs").json()
        if item["paused"] and item["started_at"] == "2026-07-17T12:00:00+00:00"
    )
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["summary"]["paused"] is True
    assert body["pending_escalation"] is None
    assert body["pending_escalation_encrypted"] is True


# ---------------------------------------------------------------------------
# 8. staff-attended exception queue
# ---------------------------------------------------------------------------


def _make_human_required_pause(console_env):
    run = console_env["runs"] / "replay-human"
    run.mkdir()
    (run / "challenge.png").write_bytes(_PNG)
    result = StepResult(
        step_id="s2",
        intent="protected intent",
        ok=False,
        error="MFA verification code required for Jane Roe MRN-7788",
        before_png="challenge.png",
    )
    RunReport(
        workflow_name="Jane Roe eligibility",
        started_at="2026-07-18T12:00:00+00:00",
        success=False,
        results=[result],
        params={"patient": "Jane Roe MRN-7788"},
        halt=HaltObservation(
            state_id="s2",
            intent="protected intent",
            reason=result.error or "",
            observed_texts=[
                "Enter code sent to jane@example.com",
                "MRN-7788",
            ],
        ),
    ).save(run)
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(
            workflow_name="Jane Roe eligibility",
            bundle_dir=str(console_env["bundle"]),
        )
    )
    store.write_checkpoint(
        RunCheckpoint(
            workflow_name="Jane Roe eligibility",
            step_index=0,
            step_id="s1",
            next_step_index=1,
        )
    )
    store.write_pending(
        PendingEscalation(
            workflow_name="Jane Roe eligibility",
            step_index=1,
            step_id="s2",
            intent="protected intent",
            category="human_required",
            reason=result.error or "",
            proposed_options=["enter Jane Roe's private verification code"],
            params={"patient": "Jane Roe MRN-7788"},
            resume_from_index=1,
        )
    )
    return run


@pytest.mark.parametrize(
    "message",
    [
        "Please verify you are human",
        "Complete the CAPTCHA",
        "Enter your one-time passcode",
        "Session expired; sign in again",
    ],
)
def test_human_presence_interruptions_only_classify_as_halts(message):
    from openadapt_flow.runtime.durable.controller import classify_halt

    category, options = classify_halt(
        None,
        StepResult(step_id="s", intent="i", ok=False, error=message),
    )
    assert category == "human_required"
    joined = " ".join(options)
    assert "LIVE application" in joined
    assert "never answers, solves, or retries" in joined


def test_human_presence_token_does_not_match_inside_opaque_identifier():
    from openadapt_flow.runtime.durable.controller import looks_like_human_required

    assert looks_like_human_required("MFA required") is True
    assert looks_like_human_required("opaque request id e2fa9d") is False


def test_attention_dto_is_opaque_and_redacted(console_env):
    _make_human_required_pause(console_env)
    response = _client(console_env).get("/api/attention")
    assert response.status_code == 200
    items = response.json()
    human = next(item for item in items if item["human_required"])
    assert re.fullmatch(r"[a-f0-9]{24}", human["id"])
    assert human["category"] == "human_required"
    assert human["before_artifact_id"] == "step-001-before"
    assert "can_approve_human_step" not in human
    assert "resume_requires_fresh_validation" not in human
    assert "prior_verified_steps_will_not_repeat" not in human
    serialized = response.text
    for protected in (
        "Jane Roe",
        "MRN-7788",
        "jane@example.com",
        "verification code",
        str(console_env["runs"]),
        str(console_env["bundle"]),
    ):
        assert protected not in serialized


def test_public_attention_resolver_is_current_bounded_and_symlink_safe(
    console_env,
    tmp_path,
):
    from openadapt_flow.console.attention import (
        list_attention,
        resolve_attention,
    )

    run = _make_human_required_pause(console_env)
    human = next(
        item for item in list_attention(console_env["runs"]) if item.human_required
    )

    resolved = resolve_attention(console_env["runs"], human.id)
    assert resolved is not None
    resolved_path, current = resolved
    assert resolved_path == run.resolve()
    assert current == human

    for invalid in ("", "../replay-human", "0" * 23, "g" * 24):
        assert resolve_attention(console_env["runs"], invalid) is None

    alias = tmp_path / "runs-alias"
    alias.symlink_to(console_env["runs"], target_is_directory=True)
    assert resolve_attention(alias, human.id) is None

    (run / "pending_escalation.json").unlink()
    RunReport(
        workflow_name="Jane Roe eligibility",
        started_at="2026-07-18T12:00:00+00:00",
        success=True,
    ).save(run)
    assert resolve_attention(console_env["runs"], human.id) is None


def test_attention_uses_program_action_evidence_not_synthetic_terminal(console_env):
    run = console_env["runs"] / "replay-program-human"
    run.mkdir()
    (run / "challenge.png").write_bytes(_PNG)
    failure = StepResult(
        step_id="enter-code",
        intent="protected intent",
        ok=False,
        error="one-time passcode required",
        before_png="challenge.png",
    )
    RunReport(
        workflow_name="protected workflow",
        started_at="2026-07-18T12:05:00+00:00",
        results=[
            failure,
            StepResult(
                step_id="<terminal>",
                intent="program halt",
                ok=False,
                error=failure.error,
            ),
        ],
        halt=HaltObservation(state_id="code-state", reason=failure.error or ""),
    ).save(run)
    CheckpointStore(run).write_pending(
        PendingEscalation(
            workflow_name="protected workflow",
            step_index=0,
            step_id="code-state",
            category="human_required",
            reason=failure.error or "",
            program=True,
        )
    )

    item = next(
        item
        for item in _client(console_env).get("/api/attention").json()
        if item["created_at"] == "2026-07-18T12:05:00+00:00"
    )
    assert item["before_artifact_id"] == "step-001-before"
    assert "can_approve_human_step" not in item


def test_attention_notification_payload_is_phi_free(console_env):
    _make_human_required_pause(console_env)
    response = _client(console_env).get("/api/attention/notification")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"title", "body", "open_count", "route"}
    assert body["title"] == "OpenAdapt needs attention"
    assert body["route"] == "#/attention"
    assert body["open_count"] >= 1
    assert "Jane" not in response.text
    assert "MRN" not in response.text
    assert "workflow" not in response.text.lower()


def test_attention_requires_bearer_and_preserves_host_origin_boundary(console_env):
    app = create_app(console_env["bundles"], console_env["runs"], attend=True)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/attention").status_code == 401
    auth = {"Authorization": f"Bearer {app.state.console_access_token}"}
    assert (
        client.get(
            "/api/attention", headers={**auth, "Host": "attacker.test"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/attention",
            headers={**auth, "Origin": "https://attacker.test"},
        ).status_code
        == 403
    )


def test_attention_preserves_typed_effect_category_over_incidental_marker(console_env):
    pending_path = console_env["paused"] / "pending_escalation.json"
    pending = json.loads(pending_path.read_text())
    pending["category"] = "effect_indeterminate"
    pending["reason"] = "verifier session expired while checking the write"
    pending_path.write_text(json.dumps(pending))
    client = _client(console_env)
    run = next(
        item
        for item in client.get("/api/attention").json()
        if item["created_at"] == "2026-07-17T11:00:00+00:00"
    )
    assert run["category"] == "effect_indeterminate"
    assert run["human_required"] is False


def test_attend_mode_preserves_normal_console_and_requires_pause_capability(
    console_env,
):
    _make_human_required_pause(console_env)
    app = create_app(
        console_env["bundles"],
        console_env["runs"],
        allow_actions=True,
        attend=True,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    assert client.get("/api/health").json()["read_only"] is False

    human = next(
        item for item in client.get("/api/attention").json() if item["human_required"]
    )
    paused_id = next(
        item["id"] for item in client.get("/api/runs").json() if item["paused"]
    )
    workflow_id = _workflow_id(client, n_steps=3)

    # Attended mode is additive: the normal console catalogs remain available.
    assert client.get(f"/api/runs/{paused_id}/actions").json()
    assert client.get(f"/api/workflows/{workflow_id}/actions").json()

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/attention/{run_id:path}/actions/{action_id}" in route_paths
    # This manually assembled legacy pause has no engine-issued capability;
    # enabling actions cannot manufacture authority for it.
    refused = client.post(
        f"/api/attention/{human['id']}/actions/continue",
        json={
            "capability_digest": "sha256:" + ("0" * 64),
            "idempotency_key": "request-key-legacy-pause",
            "action": "continue",
            "disposition": "completed_by_operator",
        },
    )
    assert refused.status_code == 409
    assert "capability" in refused.text.lower()

    script = client.get("/static/console.js").text
    assert "I fixed it — Continue" in script
    assert "Teach the fix" in script
    assert "!HEALTH.attend" not in script
    assert "captcha_answer" not in script
    assert "verification_code" not in script


def test_symlinked_pause_never_enters_attention_queue_or_leaks(console_env, tmp_path):
    secret = "Jane Roe MRN-9911 https://ehr.example/private"
    pending = console_env["paused"] / "pending_escalation.json"
    pending.unlink()
    outside = tmp_path / "outside-pause.json"
    outside.write_text(
        json.dumps(
            {
                "category": "human_required",
                "reason": secret,
                "status": "pending",
            }
        )
    )
    pending.symlink_to(outside)
    client = _client(console_env)
    listing = client.get("/api/attention")
    assert listing.status_code == 200
    assert secret not in listing.text
    run = next(
        item
        for item in client.get("/api/runs").json()
        if item["started_at"] == "2026-07-17T11:00:00+00:00"
    )
    assert run["paused"] is False


def test_attend_is_attention_first_and_actions_are_explicit(console_env, monkeypatch):
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(["console", "--attend", "--allow-actions"])
    assert args.attend is True
    assert args.allow_actions is True

    served = {}

    def fake_serve(_bundles, _runs, _skills, **kwargs):
        served.update(kwargs)

    service = object()
    monkeypatch.setattr(
        "openadapt_flow.__main__._attended_service_from_args",
        lambda _args: nullcontext(service),
    )
    monkeypatch.setattr("openadapt_flow.console.server.serve", fake_serve)
    assert args.func(args) == 0
    assert served["attend"] is True
    assert served["allow_actions"] is True
    assert served["attended_service"] is service

    app = create_app(
        console_env["bundles"],
        console_env["runs"],
        allow_actions=True,
        attend=True,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {app.state.console_access_token}"},
    )
    health = client.get("/api/health").json()
    assert health["attend"] is True
    assert health["read_only"] is False
    assert health["attended_decisions_ready"] is True
    assert health["attended_actions_ready"] is False
    script = client.get("/static/console.js").text
    html = client.get("/").text
    assert 'HEALTH.attend ? "#/attention"' in script
    assert "Needs Attention" in html
    assert "<script src=" in html
    assert re.search(r"\son[a-z]+\s*=", html.lower()) is None


def test_attended_action_console_requires_explicit_live_target():
    from openadapt_flow.__main__ import _attended_service_from_args, build_parser

    args = build_parser().parse_args(["console", "--attend", "--allow-actions"])
    with pytest.raises(SystemExit, match="explicit --config or --backend"):
        with _attended_service_from_args(args):
            pass

    web = build_parser().parse_args(
        ["console", "--attend", "--allow-actions", "--backend", "web"]
    )
    with pytest.raises(SystemExit, match="require --url"):
        with _attended_service_from_args(web):
            pass

    hidden = build_parser().parse_args(
        [
            "console",
            "--attend",
            "--allow-actions",
            "--backend",
            "web",
            "--url",
            "https://example.test",
        ]
    )
    with pytest.raises(SystemExit, match="visible live session"):
        with _attended_service_from_args(hidden):
            pass


def test_attended_console_owns_one_backend_and_closes_it(monkeypatch):
    import openadapt_flow.__main__ as cli
    from openadapt_flow.runtime.durable.attended import AttendedExecutionResult

    args = cli.build_parser().parse_args(
        [
            "console",
            "--attend",
            "--allow-actions",
            "--backend",
            "windows",
            "--agent-url",
            "http://127.0.0.1:5001",
        ]
    )

    main_thread = threading.get_ident()
    event_threads: dict[str, list[int]] = {
        "build": [],
        "executor": [],
        "configured": [],
        "close": [],
    }

    class Backend:
        closed = 0

        def close(self):
            self.closed += 1
            event_threads["close"].append(threading.get_ident())

    backend = Backend()
    built: list[object] = []

    def fake_build(_cfg, **_kwargs):
        event_threads["build"].append(threading.get_ident())
        built.append(backend)
        return backend

    configured: list[dict] = []

    def fake_configured(live_backend, **kwargs):
        event_threads["configured"].append(threading.get_ident())
        configured.append({"backend": live_backend, **kwargs})
        return object()

    class FakeBoundAttendedExecutor:
        def __init__(self, replayer_factory, *, key=None):
            assert key is None
            self.replayer_factory = replayer_factory
            event_threads["executor"].append(threading.get_ident())

        def continue_run(self, _run_dir, capability, _approval):
            event_threads["executor"].append(threading.get_ident())
            manifest = SimpleNamespace(params={}, governed_authorization=None)
            self.replayer_factory(manifest)
            return AttendedExecutionResult(
                status="completed",
                message="continued",
                resumed_from=capability.step_id,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("skip was not requested")

    monkeypatch.setattr("openadapt_flow.backends.factory.build_backend", fake_build)
    monkeypatch.setattr("openadapt_flow.deployment.build_replayer", fake_configured)
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service.BoundAttendedExecutor",
        FakeBoundAttendedExecutor,
    )
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service.execute_attended_action",
        lambda _run_dir, _request, *, operator, executor, key: executor.continue_run(
            Path("run"),
            SimpleNamespace(step_id=f"step-for-{operator}"),
            SimpleNamespace(),
        ),
    )

    with cli._attended_service_from_args(args) as service:
        assert service is not None
        owner_thread = service._owner.owner_thread_id
        result = service.execute(
            Path("run"),
            SimpleNamespace(),
            operator="staff",
        )
        assert result.status == "completed"
        assert built == [backend]
        assert [item["backend"] for item in configured] == [backend]
        assert all(item["durable"] is True for item in configured)
        assert all(item["use_structural"] is True for item in configured)
        assert backend.closed == 0
    assert backend.closed == 1
    assert owner_thread is not None
    assert owner_thread != main_thread
    assert {
        *event_threads["build"],
        *event_threads["executor"],
        *event_threads["configured"],
        *event_threads["close"],
    } == {owner_thread}


def test_attended_console_closes_backend_when_server_path_raises(monkeypatch):
    import openadapt_flow.__main__ as cli

    args = cli.build_parser().parse_args(
        [
            "console",
            "--attend",
            "--allow-actions",
            "--backend",
            "windows",
            "--agent-url",
            "http://127.0.0.1:5001",
        ]
    )

    class Backend:
        closed = False

        def close(self):
            self.closed = True

    backend = Backend()
    monkeypatch.setattr(
        "openadapt_flow.backends.factory.build_backend",
        lambda _cfg, **_kwargs: backend,
    )
    with pytest.raises(RuntimeError, match="server stopped"):
        with cli._attended_service_from_args(args):
            raise RuntimeError("server stopped")
    assert backend.closed is True


def test_attended_web_playwright_lifecycle_stays_on_owner_thread(monkeypatch):
    import openadapt_flow.__main__ as cli
    from openadapt_flow.runtime.durable.attended import AttendedExecutionResult

    args = cli.build_parser().parse_args(
        [
            "console",
            "--attend",
            "--allow-actions",
            "--backend",
            "web",
            "--url",
            "https://example.test",
            "--headed",
        ]
    )
    main_thread = threading.get_ident()
    lifecycle: list[tuple[str, int]] = []

    def mark(name: str) -> None:
        lifecycle.append((name, threading.get_ident()))

    class Page:
        def goto(self, url):
            assert url == "https://example.test"
            mark("goto")

    class Browser:
        def new_page(self, *, viewport):
            assert viewport == {"width": 1280, "height": 800}
            mark("new-page")
            return Page()

        def close(self):
            mark("browser-close")

    class Chromium:
        def launch(self, *, headless):
            assert headless is False
            mark("browser-launch")
            return Browser()

    class PlaywrightContext:
        def __enter__(self):
            mark("playwright-enter")
            return SimpleNamespace(chromium=Chromium())

        def __exit__(self, *_args):
            mark("playwright-exit")

    class FakeBoundAttendedExecutor:
        def __init__(self, replayer_factory, *, key=None):
            assert key is None
            self.replayer_factory = replayer_factory
            mark("executor-construct")

        def continue_run(self, _run_dir, capability, _approval):
            mark("execute")
            self.replayer_factory(
                SimpleNamespace(params={}, governed_authorization=None)
            )
            return AttendedExecutionResult(
                status="completed",
                message="continued",
                resumed_from=capability.step_id,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("skip was not requested")

    def fake_build(_cfg, *, page):
        assert isinstance(page, Page)
        mark("backend-build")
        return object()

    def fake_configured(_backend, **_kwargs):
        mark("replayer-build")
        return object()

    monkeypatch.setattr(
        "openadapt_flow._browser_setup.ensure_chromium_installed",
        lambda: mark("ensure-browser"),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: PlaywrightContext(),
    )
    monkeypatch.setattr("openadapt_flow.backends.factory.build_backend", fake_build)
    monkeypatch.setattr("openadapt_flow.deployment.build_replayer", fake_configured)
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service.BoundAttendedExecutor",
        FakeBoundAttendedExecutor,
    )
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service.execute_attended_action",
        lambda _run_dir, _request, *, operator, executor, key: executor.continue_run(
            Path("run"),
            SimpleNamespace(step_id=f"step-for-{operator}"),
            SimpleNamespace(),
        ),
    )

    with cli._attended_service_from_args(args) as service:
        result = service.execute(
            Path("run"),
            SimpleNamespace(),
            operator="staff",
        )
        owner_thread = service._owner.owner_thread_id
    assert result.status == "completed"
    assert owner_thread is not None
    assert owner_thread != main_thread
    assert {thread_id for _, thread_id in lifecycle} == {owner_thread}
    assert [name for name, _ in lifecycle] == [
        "ensure-browser",
        "playwright-enter",
        "browser-launch",
        "new-page",
        "goto",
        "backend-build",
        "executor-construct",
        "execute",
        "replayer-build",
        "browser-close",
        "playwright-exit",
    ]


def test_ordinary_console_does_not_construct_attended_backend(monkeypatch):
    import openadapt_flow.__main__ as cli

    args = cli.build_parser().parse_args(["console", "--allow-actions"])
    monkeypatch.setattr(
        "openadapt_flow.backends.factory.build_backend",
        lambda *_args, **_kwargs: pytest.fail("ordinary console built a backend"),
    )
    with cli._attended_service_from_args(args) as service:
        assert service is None


def test_thread_owned_attended_executor_serializes_concurrent_calls():
    from openadapt_flow.runtime.durable.attended import (
        AttendedExecutionResult,
    )
    from openadapt_flow.runtime.durable.attended_service import (
        _ThreadOwnedAttendedExecutor,
    )

    first_started = threading.Event()
    release_first = threading.Event()
    lifecycle: list[tuple[str, int]] = []
    call_lock = threading.Lock()
    active = 0
    maximum_active = 0
    call_number = 0

    class Executor:
        def continue_run(self, _run_dir, capability, _approval):
            nonlocal active, call_number, maximum_active
            with call_lock:
                call_number += 1
                current = call_number
                active += 1
                maximum_active = max(maximum_active, active)
            lifecycle.append((f"call-{current}", threading.get_ident()))
            if current == 1:
                first_started.set()
                assert release_first.wait(timeout=2)
            with call_lock:
                active -= 1
            return AttendedExecutionResult(
                status="completed",
                message=f"continued {current}",
                resumed_from=capability.step_id,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("skip was not requested")

    @contextmanager
    def factory():
        lifecycle.append(("construct", threading.get_ident()))
        try:
            yield Executor()
        finally:
            lifecycle.append(("close", threading.get_ident()))

    results: dict[str, AttendedExecutionResult] = {}

    def submit(name: str) -> None:
        results[name] = owner.continue_run(
            Path(name),
            SimpleNamespace(step_id=name),
            SimpleNamespace(),
        )

    with _ThreadOwnedAttendedExecutor(
        factory,
        startup_timeout_s=2,
        action_timeout_s=2,
        shutdown_timeout_s=2,
    ) as owner:
        first = threading.Thread(target=submit, args=("first",))
        second = threading.Thread(target=submit, args=("second",))
        first.start()
        assert first_started.wait(timeout=2)
        second.start()
        time.sleep(0.05)
        assert second.is_alive()
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive()
        assert not second.is_alive()
        owner_thread = owner.owner_thread_id

    assert set(results) == {"first", "second"}
    assert maximum_active == 1
    assert owner_thread is not None
    assert {thread_id for _, thread_id in lifecycle} == {owner_thread}
    assert [name for name, _ in lifecycle] == [
        "construct",
        "call-1",
        "call-2",
        "close",
    ]


def test_thread_owned_attended_executor_refuses_timed_out_queued_call():
    from openadapt_flow.runtime.durable.attended import AttendedExecutionResult
    from openadapt_flow.runtime.durable.attended_service import (
        _ThreadOwnedAttendedExecutor,
    )

    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []
    first_outcomes: list[object] = []

    class Executor:
        def continue_run(self, _run_dir, capability, _approval):
            calls.append(capability.step_id)
            if capability.step_id == "first":
                first_started.set()
                assert release_first.wait(timeout=2)
            return AttendedExecutionResult(
                status="completed",
                message="continued",
                resumed_from=capability.step_id,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("skip was not requested")

    @contextmanager
    def factory():
        yield Executor()

    def submit_first() -> None:
        try:
            first_outcomes.append(
                owner.continue_run(
                    Path("first"),
                    SimpleNamespace(
                        step_id="first",
                        expected_next_transition="after-first",
                    ),
                    SimpleNamespace(),
                )
            )
        except BaseException as exc:
            first_outcomes.append(exc)

    with _ThreadOwnedAttendedExecutor(
        factory,
        startup_timeout_s=2,
        action_timeout_s=0.2,
        shutdown_timeout_s=2,
    ) as owner:
        first = threading.Thread(target=submit_first)
        first.start()
        assert first_started.wait(timeout=2)
        queued = owner.continue_run(
            Path("second"),
            SimpleNamespace(
                step_id="second",
                expected_next_transition="after-second",
            ),
            SimpleNamespace(),
        )
        assert queued.status == "refused"
        assert queued.report_success is False
        assert queued.resumed_from == "second"
        assert queued.next_transition == "after-second"
        assert "cancelled before execution began" in queued.message
        assert calls == ["first"]
        release_first.set()
        first.join(timeout=2)
        assert not first.is_alive()

    assert len(first_outcomes) == 1
    assert calls == ["first"]


def test_thread_owned_attended_executor_propagates_action_failure_and_recovers():
    from openadapt_flow.runtime.durable.attended import (
        AttendedExecutionResult,
    )
    from openadapt_flow.runtime.durable.attended_service import (
        _ThreadOwnedAttendedExecutor,
    )

    class Executor:
        def continue_run(self, _run_dir, _capability, _approval):
            raise ValueError("live verification failed")

        def skip_run(self, _run_dir, capability, _approval):
            return AttendedExecutionResult(
                status="completed",
                message="skipped",
                resumed_from=capability.step_id,
            )

    @contextmanager
    def factory():
        yield Executor()

    with _ThreadOwnedAttendedExecutor(
        factory,
        startup_timeout_s=30,
        action_timeout_s=300,
        shutdown_timeout_s=30,
    ) as owner:
        with pytest.raises(ValueError, match="live verification failed"):
            owner.continue_run(
                Path("run"),
                SimpleNamespace(step_id="step-1"),
                SimpleNamespace(),
            )
        result = owner.skip_run(
            Path("run"),
            SimpleNamespace(step_id="step-1"),
            SimpleNamespace(),
        )
    assert result.status == "completed"


def test_thread_owned_attended_executor_timeout_is_not_a_refusal_receipt():
    from openadapt_flow.runtime.durable.attended import (
        AttendedExecutionResult,
    )
    from openadapt_flow.runtime.durable.attended_service import (
        AttendedExecutorTimeout,
        _ThreadOwnedAttendedExecutor,
    )

    release = threading.Event()

    class Executor:
        def continue_run(self, _run_dir, capability, _approval):
            assert release.wait(timeout=2)
            return AttendedExecutionResult(
                status="completed",
                message="continued after caller deadline",
                resumed_from=capability.step_id,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("skip was not requested")

    @contextmanager
    def factory():
        yield Executor()

    with _ThreadOwnedAttendedExecutor(
        factory,
        startup_timeout_s=2,
        action_timeout_s=0.02,
        shutdown_timeout_s=2,
    ) as owner:
        with pytest.raises(
            AttendedExecutorTimeout, match="did not return a terminal receipt"
        ):
            owner.continue_run(
                Path("run"),
                SimpleNamespace(step_id="step-1"),
                SimpleNamespace(),
            )
        release.set()


def test_thread_owned_attended_executor_propagates_startup_failure():
    from openadapt_flow.runtime.durable.attended_service import (
        _ThreadOwnedAttendedExecutor,
    )

    @contextmanager
    def factory():
        raise RuntimeError("backend construction failed")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="backend construction failed"):
        with _ThreadOwnedAttendedExecutor(
            factory,
            startup_timeout_s=2,
            action_timeout_s=2,
            shutdown_timeout_s=2,
        ):
            pass


def test_public_attended_action_service_contract_is_stable():
    from openadapt_flow.deployment import DeploymentConfig
    from openadapt_flow.runtime.durable import AttendedActionService

    assert list(inspect.signature(AttendedActionService).parameters) == [
        "deployment",
        "key",
        "startup_timeout_s",
        "action_timeout_s",
        "shutdown_timeout_s",
    ]
    execute = inspect.signature(AttendedActionService.execute)
    assert list(execute.parameters) == [
        "self",
        "run_dir",
        "request",
        "operator",
    ]
    assert execute.parameters["operator"].kind is inspect.Parameter.KEYWORD_ONLY

    service = AttendedActionService(DeploymentConfig())
    assert not hasattr(service, "continue_run")
    assert not hasattr(service, "skip_run")
    with pytest.raises(RuntimeError, match="not open"):
        service.execute(
            Path("run"),
            SimpleNamespace(),
            operator="staff",
        )
    service.close()
