"""Real admission/store/receipt joins with the browser action modeled.

The prior recording, campaign results and governed browser action are explicit
component fixtures. Signatures, teach authorization, admission checks, normal
store transitions, SQLite readback and verify-run receipts are genuine. These
controls do not claim a browser, provider, real campaign or app qualification.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace

import pytest
import yaml
from PIL import Image

from openadapt_flow.compile_agent import repair_bridge as bridge
from openadapt_flow.compile_agent.pipeline import FirstRunCompiler, _write_run1_values
from openadapt_flow.integrations.agents.skills import SkillCache
from openadapt_flow.ir import Anchor, HaltObservation, RunReport, StepResult, Workflow
from openadapt_flow.jit import TaskSignature, lifecycle
from openadapt_flow.repair.candidate import Actor, CampaignResult, RepairProvenance, TaughtStep
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.registration import build_candidate
from openadapt_flow.verify_run import _build_verifier, capture_baseline, load_verify_config
from tests._teach_authorization_support import repair_authorization
from tests.test_jit_lifecycle import REVIEWER, _ci, _jit, _policy_passes, _report, _review, _stub_runs  # noqa: F401 -- genuine shared autouse policy fixture

PROMPT = "Create a synthetic note with {value}"


@pytest.fixture
def prepared(tmp_path, monkeypatch, _policy_passes):
    database = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("create table records (id integer primary key, type text, value text)")
    config_data = {
        "verifier": {"kind": "sql", "sqlite_database": str(database), "sql_query": "select * from records"},
        "effects": [{"kind": "record_written", "match": {"type": "note", "value": {"param": "value"}}, "expected_count": 1, "count_new_only": True, "timeout_s": 0.1}],
    }
    config_path = tmp_path / "verify.yaml"
    config_path.write_text(yaml.safe_dump(config_data))
    cfg = load_verify_config(config_path)
    verifier = _build_verifier(cfg, {"value": "seed"})
    cache = SkillCache(url="http://127.0.0.1:9/", verify=config_path, expect={"type": "note"}, on_screen=["Synthetic note"], offer=None, canary_params=[], cache_dir=tmp_path / "cache")
    task = TaskSignature(cache.task_id(PROMPT, {"value": "held"}), PROMPT, {"value": str})
    jit = _jit(tmp_path, verifier=verifier, task=task)
    prior = jit.bundle_dir
    Image.new("RGB", (20, 20), "white").save(prior / "target.png")
    workflow = Workflow.load(prior)
    workflow.params = {"value": ""}
    workflow.steps[-1].anchor = Anchor(template="target.png", region=(0, 0, 20, 20), click_point=(10, 10))
    workflow.save(prior)
    # A source fixture changed the draft before admission, so its genuine
    # recorder signature is recomputed; no admitted bundle is re-signed here.
    candidate_path = jit.program_dir / "candidate.json"
    candidate_path.write_text(json.dumps(lifecycle.sign_recorder(
        jit.program_dir, json.loads(candidate_path.read_text()), key_dir=jit.key_dir
    )))
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER, value="seed")
    assert jit.admit_with_key(_ci(jit, monkeypatch))["outcome"] == "admitted"
    normal_prior = lifecycle.verify_admission(prior, key_dir=jit.key_dir)

    compiler = SimpleNamespace(dir=jit.task_dir, out_dir=jit.task_dir / "compiled", task=task, _jit=lambda: jit)
    compiler.out_dir.mkdir()
    shutil.copyfile(config_path, compiler.out_dir / "verify.yaml")
    _write_run1_values(compiler.out_dir / "run1_values.json", {"value": "seed"})
    compiler._confirmed_config = lambda: cfg
    compiler._governed = MethodType(FirstRunCompiler._governed, compiler)
    from openadapt_flow.record_sor import observer_from_config

    compiler.observer, _ = observer_from_config(config_data["verifier"])
    key = cache.key(PROMPT, {"value": "held"})
    (compiler.dir / "skill-key.json").write_text(json.dumps(key))
    monkeypatch.setattr(cache, "_compiler", lambda *_: (compiler, key))
    halted = jit.runs_dir / "halted-original"
    halted.mkdir(parents=True)
    report = RunReport(
        workflow_name=workflow.name, started_at="2026-01-01T00:00:00Z", params={"value": "held"},
        execution_outcome="HALTED", transaction_outcome="HALTED_BEFORE_EFFECT",
        bundle_content_digest=normal_prior["digest"],
        halt=HaltObservation(state_id="step_001", reason="target_not_found"),
        results=[StepResult(step_id="step_001", intent="Save synthetic value", risk="irreversible", ok=False, delivery_attempted=False, error="Could not resolve target for step step_001")],
    )
    (halted / "report.json").write_text(report.model_dump_json())
    (compiler.dir / "skill-stop.json").write_text(json.dumps({"phase": "halted", "bundle_digest": normal_prior["digest"], "replay": {"run_dir": str(halted)}}))
    proposed = tmp_path / "proposed"
    shutil.copytree(prior, proposed)
    changed = Workflow.load(proposed)
    changed.steps[-1].anchor.click_point = (11, 10)
    changed.save(proposed)
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk

    desk = RepairDesk(compiler.dir / "repairs", RepairAccessPolicy(None))
    store = RepairStore(desk.store_for(workflow.name))
    candidate = build_candidate(prior, proposed, source="teach")
    candidate.provenance = RepairProvenance(
        agent_id="agent:component", halted_run_id=halted.name, halted_step_id="step_001",
        repair_token_sha256="a" * 64, screen_access_enabled_by="lab-demo",
        teach_authorization=repair_authorization(store.root, jit.key_dir, task.task_id, monkeypatch),
    )
    candidate.taught_steps = [TaughtStep(step_id="step_001", frame_sha256="b" * 64)]
    candidate = store.add_candidate(candidate)
    reviewer = Actor(kind="human", id="Synthetic Repair Reviewer")
    candidate = store.review(candidate.candidate_id, reviewer)
    for name in ("replay", "fault"):
        candidate = store.record_campaign(candidate.candidate_id, CampaignResult(campaign=name, passed=True, total=1, failed=0), Actor(kind="automation", id="component-campaign-boundary"))
    candidate = store.approve(candidate.candidate_id, reviewer, non_interactive=True)
    calls = []
    failure = {"mode": None, "at": 1}

    def browser_action(_workflow, digest, values, run_dir, **_):
        # Only this browser boundary is modeled. The bridge still enters
        # actual _run_admitted and the normal independent receipt path.
        calls.append(dict(values))
        mode = failure["mode"] if len(calls) == failure["at"] else None
        if mode == "cancel":
            raise KeyboardInterrupt("private component cancellation")
        _cfg, _values, baseline = compiler._pending
        capture_baseline(_cfg, _values, baseline)
        compiler._receipt_baseline = baseline
        started = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(database) as connection:
            connection.execute("insert into records(type,value) values (?,?)", ("note", values["value"]))
        run_dir.mkdir(parents=True)
        outcome = "HALTED" if mode == "halt" else "VERIFIED"
        proof = RunReport(workflow_name=_workflow.name, started_at=started, params={"value": "foreign"} if mode == "wrong_params" else values, execution_outcome=outcome, transaction_outcome="RECONCILIATION_REQUIRED" if outcome == "HALTED" else "VERIFIED", bundle_content_digest="f" * 64 if mode == "wrong_digest" else digest, model_calls=1 if mode == "model_call" else 0)
        (run_dir / "report.json").write_text(proof.model_dump_json())
        return None, proof

    monkeypatch.setattr(jit, "_run_governed", browser_action)
    return SimpleNamespace(cache=cache, compiler=compiler, jit=jit, desk=desk, store=store, candidate=candidate, halted=halted, calls=calls, failure=failure, rows=[{"value": f"canary-{i}"} for i in range(candidate.canary.max_runs)])


def complete(p, **changes):
    return p.cache.complete_repair(PROMPT, {"value": "held"}, repair_desk=p.desk, candidate_id=p.candidate.candidate_id, canary_params=changes.get("rows", p.rows))


def test_normal_signed_repair_canaries_promote_and_select_without_agent(prepared):
    p = prepared
    result = complete(p)
    assert result.canary_runs == p.candidate.canary.max_runs == len(p.calls)
    assert p.calls == p.rows and {"value": "held"} not in p.calls
    assert p.store.load_candidate(p.candidate.candidate_id).state == "active"
    admission = lifecycle.verify_admission(result.bundle_dir, key_dir=p.jit.key_dir)
    assert admission["admission_mode"] == "repair"
    assert admission["repair_of"]["candidate_id"] == p.candidate.candidate_id
    assert (p.compiler.dir / "skill-repair.json").is_file()
    assert not (p.compiler.dir / "skill-stop.json").exists()
    assert (p.compiler.dir / f"skill-stop-repaired-{p.candidate.candidate_id}.json").is_file()


def test_programmatic_teach_authority_root_is_used_without_ambient_pin(prepared, monkeypatch):
    from openadapt_flow import teach_authorization as ta
    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from tests._teach_authorization_support import authorizer

    p = prepared
    p.jit.teach_authorization = ta.TeachAuthority(
        p.jit.bundle_dir / lifecycle.TEACH_AUTHORIZATION_DIR / ta.STATEMENT_FILE,
        key_dir=p.jit.key_dir, approver_root=authorizer(p.jit.key_dir).root,
    )
    monkeypatch.delenv(ROOT_ENV)
    result = complete(p)
    assert result.canary_runs == p.candidate.canary.max_runs
    assert lifecycle.verify_admission(result.bundle_dir, key_dir=p.jit.key_dir)["admission_mode"] == "repair"


@pytest.mark.parametrize("change", ["unapproved", "prior", "two_steps", "canary_used", "regressed", "uncertain", "delivered", "identity"])
def test_invalid_or_consumed_repair_never_runs_canary(prepared, change):
    p = prepared
    candidate = p.store.load_candidate(p.candidate.candidate_id)
    if change == "unapproved": candidate.approval = None
    elif change == "prior": candidate.approval.prior_content_digest = "f" * 64
    elif change == "two_steps": candidate.taught_steps.append(TaughtStep(step_id="other", frame_sha256="c" * 64))
    elif change == "canary_used": candidate.canary_metrics.runs_completed = 1
    elif change == "regressed": candidate.canary_metrics.halted = True
    else:
        path = p.halted / "report.json"
        report = RunReport.model_validate_json(path.read_text())
        if change == "uncertain": report.transaction_outcome = "RECONCILIATION_REQUIRED"
        elif change == "delivered": report.results[0].delivery_attempted = True
        else:
            from openadapt_flow.ir import IdentityCheck
            report.results[0].identity = IdentityCheck(status="mismatch", mode="context")
        path.write_text(report.model_dump_json())
    p.store.save_candidate(candidate)
    with pytest.raises(bridge.RepairBridgeError): complete(p)
    assert p.calls == [] and (p.compiler.dir / "skill-stop.json").is_file()


@pytest.mark.parametrize("change", ["short", "duplicate", "run1", "held"])
def test_canary_inputs_are_fresh_and_match_existing_budget(prepared, change):
    p = prepared
    rows = list(p.rows)
    if change == "short": rows.pop()
    else: rows[0] = rows[1] if change == "duplicate" else {"value": "seed" if change == "run1" else "held"}
    with pytest.raises(bridge.RepairBridgeError): complete(p, rows=rows)
    assert p.calls == [] and (p.compiler.dir / "skill-stop.json").is_file()


@pytest.mark.parametrize("mode", ["halt", "cancel"])
@pytest.mark.parametrize("at", [1, 3, 5])
def test_first_failed_or_cancelled_canary_stays_held_and_is_not_retried(prepared, mode, at):
    p = prepared; p.failure.update(mode=mode, at=at)
    expected = KeyboardInterrupt if mode == "cancel" else bridge.RepairBridgeError
    with pytest.raises(expected): complete(p)
    assert len(p.calls) == at and (p.compiler.dir / "skill-stop.json").is_file()
    with pytest.raises(bridge.RepairBridgeError): complete(p)
    assert len(p.calls) == at


@pytest.mark.parametrize("mode", ["wrong_params", "wrong_digest", "model_call"])
def test_every_canary_requires_its_own_current_report_join(prepared, mode):
    p = prepared; p.failure["mode"] = mode
    with pytest.raises(bridge.RepairBridgeError): complete(p)
    assert len(p.calls) == 1 and (p.compiler.dir / "skill-stop.json").is_file()
    assert not (p.compiler.dir / "skill-repair.json").exists()


@pytest.mark.parametrize("change", ["traversal", "aliased_candidate", "wrong_record_id"])
def test_candidate_reference_refuses_before_any_canary(prepared, change):
    p = prepared
    candidate_id = p.candidate.candidate_id
    path = p.store.candidate_path(candidate_id)
    if change == "traversal": candidate_id = "../candidates/" + candidate_id
    elif change == "aliased_candidate":
        outside = p.compiler.dir / "outside-candidate.json"
        path.rename(outside); path.symlink_to(outside)
    else:
        document = json.loads(path.read_text()); document["candidate_id"] = "another-candidate"
        path.write_text(json.dumps(document))
    with pytest.raises(bridge.RepairBridgeError):
        p.cache.complete_repair(PROMPT, {"value": "held"}, repair_desk=p.desk, candidate_id=candidate_id, canary_params=p.rows)
    assert p.calls == [] and (p.compiler.dir / "skill-stop.json").is_file()


@pytest.mark.parametrize("rows", [[], [{"type": "note", "value": "wrong"}], [{"type": "note", "value": "canary-4"}, {"type": "note", "value": "canary-4"}]])
def test_adoption_still_refuses_missing_wrong_or_duplicate_current_record(prepared, monkeypatch, rows):
    from openadapt_flow.agent.repair import RepairError

    p = prepared
    # Run real canaries but replace only the final fresh observer read.
    class Observer:
        @property
        def system_of_record(self): return rows
    p.compiler.observer = Observer()
    with pytest.raises(RepairError, match="matching verified cache handoff"): complete(p)
    assert len(p.calls) == p.candidate.canary.max_runs
    assert (p.compiler.dir / "skill-stop.json").is_file()
