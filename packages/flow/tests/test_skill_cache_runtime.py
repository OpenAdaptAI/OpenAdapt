"""Cache dispatch models: real cache/record checks; MCP is the action boundary.

No browser, model, admission qualification or application is exercised here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.integrations.agents import skills
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    HaltObservation,
    IdentityCheck,
    RunReport,
    Step,
    StepResult,
    Workflow,
)

PROMPT = "Create the synthetic record with {value}"


def make_cache(tmp_path: Path, **options: Any) -> skills.SkillCache:
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n  kind: rest\n  base_url: http://127.0.0.1:1\n  records_path: /records\n  records_key: records\n"
    )
    return skills.SkillCache(
        url="http://127.0.0.1:1",
        verify=verify,
        expect={"kind": "synthetic"},
        on_screen=["Synthetic record"],
        offer=lambda _: None,
        canary_params=[],
        cache_dir=tmp_path / "cache",
        **options,
    )


@pytest.fixture
def model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cache = make_cache(tmp_path)
    directory = tmp_path / "cache" / "task"
    bundle = directory / "program" / "bundle"
    bundle.mkdir(parents=True)
    Workflow(name="synthetic-task").save(bundle)
    jit = SimpleNamespace(admitted=True, bundle_dir=bundle, runs_dir=directory / "runs")
    jit.runs_dir.mkdir()
    rows: list[dict[str, str]] = []

    class Observer:
        unavailable = False

        @property
        def system_of_record(self):
            return None if self.unavailable else copy.deepcopy(rows)

    compiler = SimpleNamespace(
        dir=directory,
        out_dir=directory / "compiled",
        observer=Observer(),
        key_dir=None,
        _jit=lambda: jit,
    )
    compiler.out_dir.mkdir()
    server = compiler.out_dir / "server.py"
    server.write_text("# Model boundary only; never executed.\n")
    (compiler.out_dir / "emitted.json").write_text(
        json.dumps({"mcp_server": str(server)})
    )
    key = cache.key(PROMPT, {"value": "one"})
    (directory / skills._KEY_FILE).write_text(json.dumps(key))
    monkeypatch.setattr(cache, "_compiler", lambda *_: (compiler, key))
    monkeypatch.setattr(
        cache, "_first_run", lambda *_: pytest.fail("full agent fallback")
    )
    return SimpleNamespace(cache=cache, compiler=compiler, jit=jit, rows=rows, sent=[])


def install_hit(
    model: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, **reply: Any
) -> None:
    async def hit(_python, _server, params):
        model.sent.append(dict(params))
        model.rows.append(
            {
                "id": str(len(model.rows) + 1),
                "kind": "synthetic",
                "value": params["value"],
            }
        )
        return {
            "status": "VERIFIED",
            "verified": True,
            "model_calls": 0,
            "receipt_outcome": "VERIFIED",
            "receipt": "/synthetic/receipt.json",
            **reply,
        }

    monkeypatch.setattr(skills, "_call_skill", hit)


def test_new_parameter_values_reuse_the_key_and_checked_hit(model, monkeypatch) -> None:
    install_hit(model, monkeypatch)
    assert model.cache.key(PROMPT, {"value": "one"}) == model.cache.key(
        PROMPT, {"value": "two"}
    )
    for value in ("one", "two"):
        result = model.cache.run(PROMPT, {"value": value}, object())
        assert result.path == "skill" and result.verified and result.model_calls == 0
        assert result.replay["receipt_outcome"] == "VERIFIED"
        assert result.record_check["passed"]
    assert model.sent == [{"value": "one"}, {"value": "two"}]
    assert not (model.compiler.dir / skills._STOP_FILE).exists()


def test_failed_hit_keeps_evidence_and_never_falls_back(model, monkeypatch) -> None:
    install_hit(
        model, monkeypatch, verified=False, status="HALTED", run_dir="/synthetic/halt"
    )
    first = model.cache.run(PROMPT, {"value": "one"}, object())
    second = model.cache.run(PROMPT, {"value": "two"}, object())
    assert first.status == "HALTED" and not first.verified
    assert second.status == "CACHE_HALTED" and not second.verified
    assert second.replay == first.replay and second.model_calls == 0
    assert model.sent == [{"value": "one"}]
    # A caller can tell the hold apart from a cache miss without a model.
    assert model.cache.held(PROMPT, {"value": ""})
    assert (
        model.jit.bundle_dir.is_dir()
        and model.cache.server(PROMPT, {"value": "two"}) is None
    )


@pytest.mark.parametrize("count", [None, "0", False, 1])
def test_zero_model_claim_requires_actual_integer_zero(
    model, monkeypatch, count
) -> None:
    install_hit(model, monkeypatch, model_calls=count)
    result = model.cache.run(PROMPT, {"value": "one"}, object())
    assert not result.verified and result.status == "MODEL_CALLS_NOT_ZERO"
    assert result.model_calls == (1 if count == 1 and type(count) is int else None)
    assert (model.compiler.dir / skills._STOP_FILE).is_file()


@pytest.mark.parametrize("receipt", [None, "RECONCILIATION_REQUIRED"])
def test_hit_cannot_promote_an_unverified_receipt(model, monkeypatch, receipt) -> None:
    install_hit(model, monkeypatch, receipt_outcome=receipt)
    result = model.cache.run(PROMPT, {"value": "one"}, object())
    assert not result.verified and result.status == "RECEIPT_NOT_VERIFIED"
    assert (model.compiler.dir / skills._STOP_FILE).is_file()


@pytest.mark.parametrize("error", [RuntimeError("private"), KeyboardInterrupt()])
def test_lost_reply_stays_held_on_restart(model, monkeypatch, error) -> None:
    async def hit(*_):
        model.sent.append("sent")
        raise error

    monkeypatch.setattr(skills, "_call_skill", hit)
    with pytest.raises(type(error)):
        model.cache.run(PROMPT, {"value": "one"}, object())
    assert (
        json.loads((model.compiler.dir / skills._STOP_FILE).read_text())["phase"]
        == "running"
    )
    again = model.cache.run(PROMPT, {"value": "two"}, object())
    assert again.status == "CACHE_HALTED" and model.sent == ["sent"]


def test_independent_wrong_write_is_not_silent(model, monkeypatch) -> None:
    async def wrong(*_):
        model.sent.append("sent")
        model.rows.append({"id": "1", "kind": "other", "value": "wrong"})
        return {
            "status": "VERIFIED",
            "verified": True,
            "model_calls": 0,
            "receipt_outcome": "VERIFIED",
        }

    monkeypatch.setattr(skills, "_call_skill", wrong)
    result = model.cache.run(PROMPT, {"value": "one"}, object())
    assert not result.verified and result.status == "RECORD_CHECK_FAILED"
    assert result.record_check["outside_run"]["created"] == 1
    assert model.cache.run(PROMPT, {"value": "two"}, object()).status == "CACHE_HALTED"
    assert model.sent == ["sent"]


def test_unreadable_baseline_stops_before_mcp(model, monkeypatch) -> None:
    install_hit(model, monkeypatch)
    model.compiler.observer.unavailable = True
    result = model.cache.run(PROMPT, {"value": "one"}, object())
    assert result.status == "BASELINE_UNAVAILABLE" and not result.verified
    assert model.sent == []


@pytest.mark.parametrize("raw", ["{", "[]", "null"])
def test_unreadable_hold_never_turns_into_an_agent_miss(model, raw) -> None:
    (model.compiler.dir / skills._STOP_FILE).write_text(raw)
    assert model.cache.run(PROMPT, {"value": "one"}, object()).status == "CACHE_HALTED"
    assert model.sent == []


def test_in_flight_hit_refuses_a_second_dispatch(model, monkeypatch) -> None:
    async def hit(*_):
        nested = model.cache.run(PROMPT, {"value": "two"}, object())
        assert nested.status == "CACHE_HALTED"
        model.rows.append({"id": "1", "kind": "synthetic", "value": "one"})
        return {
            "status": "VERIFIED",
            "verified": True,
            "model_calls": 0,
            "receipt_outcome": "VERIFIED",
        }

    monkeypatch.setattr(skills, "_call_skill", hit)
    assert model.cache.run(PROMPT, {"value": "one"}, object()).verified
    assert len(model.rows) == 1


def prepare_repair(model, monkeypatch) -> tuple[Any, Path]:
    from PIL import Image

    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk
    from openadapt_flow.jit import lifecycle

    bundle = model.jit.bundle_dir
    Image.new("RGB", (2, 2), "white").save(bundle / "target.png")
    Workflow(
        name="synthetic-task",
        steps=[
            Step(
                id="save",
                intent="Save synthetic value",
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=Anchor(
                    template="target.png", region=(0, 0, 2, 2), click_point=(1, 1)
                ),
            )
        ],
    ).save(bundle)
    workflow = Workflow.load(bundle)
    digest = workflow.manifest.content_digest
    monkeypatch.setattr(
        lifecycle, "verify_admission", lambda *_a, **_k: {"digest": digest}
    )
    run = model.jit.runs_dir / "synthetic-halt"
    run.mkdir()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-01-01T00:00:00Z",
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        bundle_content_digest=digest,
        halt=HaltObservation(state_id="save", reason="target_not_found"),
        results=[
            StepResult(
                step_id="save",
                intent="Save synthetic value",
                risk="irreversible",
                ok=False,
                delivery_attempted=False,
                error="Could not resolve target for step save",
            )
        ],
    )
    (run / "report.json").write_text(report.model_dump_json())
    (model.compiler.dir / skills._STOP_FILE).write_text(
        json.dumps(
            {
                "phase": "halted",
                "bundle_digest": digest,
                "replay": {"run_dir": str(run)},
            }
        )
    )
    desk = RepairDesk(model.compiler.dir / "repairs", RepairAccessPolicy(None))
    return desk, run


def test_explicit_repair_uses_real_desk_without_clearing_hold(
    model, monkeypatch
) -> None:
    from openadapt_flow.agent.repair import RepairError

    desk, _run = prepare_repair(model, monkeypatch)
    reply = model.cache.open_repair(PROMPT, {"value": "one"}, repair_desk=desk)
    assert reply["step_id"] == "save" and reply["screen_access"] == "off"
    assert not reply["teach_repair_available"]
    with pytest.raises(RepairError):
        desk.teach(reply["repair_token"], {"action": "click", "x": 0, "y": 0})
    assert (model.compiler.dir / skills._STOP_FILE).is_file()
    assert model.rows == [] and model.sent == []


@pytest.mark.parametrize(
    "change",
    [
        "uncertain",
        "delivery",
        "unknown_delivery",
        "wrong_digest",
        "wrong_identity",
        "foreign_path",
        "no_digest",
    ],
)
def test_repair_never_promotes_unknown_or_possibly_sent_writes(
    model, monkeypatch, change
) -> None:
    from openadapt_flow.agent.repair import RepairError

    desk, run = prepare_repair(model, monkeypatch)
    report = RunReport.model_validate_json((run / "report.json").read_text())
    if change == "uncertain":
        report.transaction_outcome = "RECONCILIATION_REQUIRED"
    elif change == "delivery":
        report.results[0].delivery_attempted = True
    elif change == "unknown_delivery":
        report.results[0].delivery_attempted = None
    elif change == "wrong_digest":
        report.bundle_content_digest = "f" * 64
    elif change == "no_digest":
        report.bundle_content_digest = None
    elif change == "wrong_identity":
        report.results[0].identity = IdentityCheck(status="mismatch", mode="context")
    else:
        marker = model.compiler.dir / skills._STOP_FILE
        value = json.loads(marker.read_text())
        value["replay"]["run_dir"] = str(run.parent.parent)
        marker.write_text(json.dumps(value))
    (run / "report.json").write_text(report.model_dump_json())
    with pytest.raises(RepairError):
        model.cache.open_repair(PROMPT, {"value": "one"}, repair_desk=desk)
    assert not (desk.root / "tokens").exists()
    assert model.rows == [] and (model.compiler.dir / skills._STOP_FILE).is_file()


def test_trusted_review_and_hold_reach_only_the_correct_constructor(
    tmp_path, monkeypatch
) -> None:
    from openadapt_flow import compile_agent

    review, hold = lambda *_: None, lambda *_: None
    cache = make_cache(tmp_path, review=review, hold=hold)
    compiler, key = cache._compiler(PROMPT, {"value": "one"})
    seen = {}

    def first(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            admitted=False,
            run1={},
            record_check={},
            status="draft",
            detail=None,
            to_dict=lambda: {},
        )

    monkeypatch.setattr(compile_agent, "compile_first_run", first)
    cache._first_run(compiler, key, PROMPT, {"value": "one"}, object(), 0)
    assert seen["review"] is review and seen["hold"] is hold
