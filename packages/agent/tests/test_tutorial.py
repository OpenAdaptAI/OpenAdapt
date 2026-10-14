"""Public synthetic tutorial is generated at serve time, never vendored."""

from __future__ import annotations

from pathlib import Path

from openadapt_agent.tutorial import prepare_tutorial_session, _deployment_yaml
from openadapt_flow.ir import ActionKind, Step, Workflow


def test_deployment_yaml_is_synthetic_and_fail_closed() -> None:
    text = _deployment_yaml("http://127.0.0.1:9/", "http://127.0.0.1:9/?fault=ok")
    assert "kind: rest" in text
    assert "profile: standard" in text
    assert "policy: clinical-write" in text
    assert "allow_model_grounding: false" in text
    assert "approve-unverified" not in text
    assert "patient" not in text.lower()
    assert "phi" not in text.lower()


def test_prepare_tutorial_session_records_then_keeps_mockmed(
    monkeypatch, tmp_path: Path
) -> None:
    stopped = {"n": 0}

    def fake_serve(port: int = 0, *, host: str = "127.0.0.1"):
        return "http://127.0.0.1:9/", object(), lambda: stopped.__setitem__("n", 1)

    def fake_record(base_url, recording_dir, *, headed=False, presentation_delay_s=0.0):
        Path(recording_dir).mkdir(parents=True, exist_ok=True)
        return Path(recording_dir)

    def fake_compile(recording_dir, bundle_dir, **kwargs):
        workflow = Workflow(
            name="local-quickstart",
            params={"note": "Synthetic follow-up in two weeks"},
            steps=[
                Step(id="s1", intent="Save the synthetic note", action=ActionKind.CLICK),
            ],
        )
        workflow.save(Path(bundle_dir))
        return workflow

    monkeypatch.setattr("openadapt_flow.mockmed.fault_server.serve", fake_serve)
    monkeypatch.setattr("openadapt_flow.tutorial.record_tutorial", fake_record)
    monkeypatch.setattr("openadapt_flow.compiler.compile_recording", fake_compile)
    monkeypatch.setattr("openadapt_flow.tutorial.certify_tutorial", lambda workflow: object())

    session = prepare_tutorial_session(tmp_path / "tutorial", reuse_bundle=False)
    assert session.bundle_dir.joinpath("workflow.json").is_file()
    assert "fault=ok" in session.url
    yaml_text = session.deployment_config.read_text(encoding="utf-8")
    assert "synthetic-tutorial" in yaml_text
    assert "phi" not in yaml_text.lower()
    session.close()
    assert stopped["n"] == 1


def test_bind_param_effects_reads_the_callers_value(tmp_path: Path) -> None:
    import pytest

    effect_module = pytest.importorskip("openadapt_flow.runtime.effects.effect")
    from openadapt_agent.tutorial import bind_param_effects

    try:
        effect = effect_module.Effect(
            kind=effect_module.EffectKind.FIELD_EQUALS,
            match={"type": effect_module.ValueExpr(literal="Triage")},
            field="note",
            value=effect_module.ValueExpr(literal="Synthetic follow-up in two weeks"),
            risk="irreversible",
        )
    except Exception as exc:  # older Flow effect model
        pytest.skip(f"effect model differs on this Flow: {type(exc).__name__}")
    bundle = tmp_path / "bundle"
    Workflow(
        name="local-quickstart",
        params={"note": "Synthetic follow-up in two weeks"},
        steps=[
            Step(
                id="save",
                intent="Save the synthetic note",
                action=ActionKind.CLICK,
                effects=[effect],
            )
        ],
    ).save(bundle)
    workflow = bind_param_effects(bundle)
    bound = workflow.steps[0].effects[0].value
    assert bound.param == "note"
    assert bound.literal is None
    # The re-sealed bundle still loads, and binding twice changes nothing.
    assert bind_param_effects(bundle).steps[0].effects[0].value.param == "note"
