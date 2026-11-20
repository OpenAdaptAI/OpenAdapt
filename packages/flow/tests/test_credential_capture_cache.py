"""Protected cache entry points cannot skip the authenticated capture mode."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openadapt_flow.compile_agent.pipeline import FirstRunCompiler
from openadapt_flow.jit import TaskSignature, WebApp, compile_agent
from openadapt_flow.jit.lifecycle import AdmissionError
from tests.test_skill_cache_runtime import make_cache
from tests.test_skill_cache_runtime import model as model

MESSAGE = "CREDENTIAL_CAPTURE_PRIVACY_MISMATCH"


def _refuse():
    raise AdmissionError(MESSAGE)


def test_existing_emitted_server_does_not_bypass_mode_proof(model, monkeypatch):
    model.compiler.require_credential_withholding = _refuse
    monkeypatch.setattr(
        model.compiler,
        "emit",
        lambda: pytest.fail("emitted before privacy proof"),
        raising=False,
    )
    key = model.cache.key("Create the synthetic record with {value}", {"value": "one"})
    with pytest.raises(AdmissionError, match=MESSAGE):
        model.cache._server(model.compiler, key)
    assert model.rows == [] and model.sent == []


def test_direct_skill_dispatch_refuses_before_stop_baseline_or_tool(model, monkeypatch):
    from openadapt_flow.integrations.agents import skills

    class NoBaseline:
        @property
        def system_of_record(self):
            pytest.fail("read baseline before privacy proof")

    model.compiler.observer = NoBaseline()
    model.compiler.require_credential_withholding = _refuse
    monkeypatch.setattr(
        skills,
        "_call_skill",
        lambda *_: pytest.fail("called cached tool"),
    )
    with pytest.raises(AdmissionError, match=MESSAGE):
        model.cache._skill_run(
            model.compiler, model.compiler.out_dir / "server.py", {}, 0
        )
    assert not (model.compiler.dir / skills._STOP_FILE).exists()
    assert model.sent == []


@pytest.mark.parametrize("entry", ["emit", "run"])
def test_direct_compiler_entry_refuses_before_emission_or_authority(tmp_path, entry):
    compiler = SimpleNamespace(
        _jit=lambda: SimpleNamespace(admitted=True),
        require_credential_withholding=_refuse,
        task=SimpleNamespace(validate=lambda values: dict(values)),
        cli_line=lambda: pytest.fail("emitted before privacy proof"),
        _governed=lambda *_: pytest.fail("reserved authority before privacy proof"),
    )
    with pytest.raises(AdmissionError, match=MESSAGE):
        if entry == "emit":
            FirstRunCompiler.emit(compiler)
        else:
            FirstRunCompiler.run(compiler, value="one")
    assert list(tmp_path.iterdir()) == []


def test_loaded_task_json_cannot_opt_out_of_wrap_protection(tmp_path):
    cache = make_cache(tmp_path)
    compiler, _key = cache._compiler("Create {value}", {"value": "one"})
    path = compiler.save_spec()
    document = json.loads(path.read_text())
    document["withhold_credentials"] = False
    document["credential_withholding"] = False
    document["task"]["withhold_credentials"] = False
    path.write_text(json.dumps(document))
    loaded = FirstRunCompiler.load(compiler.dir)
    assert loaded._jit().withhold_credentials is True


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true"])
def test_only_a_real_python_bool_can_select_the_capture_mode(tmp_path, value):
    with pytest.raises(TypeError, match="withhold_credentials must be a bool"):
        compile_agent(
            task=TaskSignature("strict.mode", "do nothing", {}),
            app=WebApp("http://synthetic.invalid/"),
            verifier=None,
            policy="clinical-write",
            cache_dir=tmp_path,
            withhold_credentials=value,
        )
