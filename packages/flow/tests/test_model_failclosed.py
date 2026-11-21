"""Remote-model offload review (Nov 21): fail-open and egress reproductions.

Each test pins the fail-closed behaviour the review asks for. On origin/main
576f87b41 they fail; the fixes on ``claude/model-failclosed`` make them pass.

1. A model "yes" must never clear a failed postcondition, on any step.
2. A model component with no ``MAY_EGRESS`` marker counts as egress.
3. Identity-VLM calls count as model calls, and identity crops sent off the
   box never produce ``external_network_calls == "none"``.
4. The VLM service passes ``VLM_VLLM_URL`` to the vLLM backend.
5. The regulated profile refuses a non-loopback cleartext (``http://``)
   appliance.
6. A pinned model that is unavailable never falls back to another placement
   or model.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.deployment import GroundingModelConfig, RuntimeSection
from openadapt_flow.ir import (
    Anchor,
    Postcondition,
    PostconditionKind,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.grounder import component_may_egress
from openadapt_flow.runtime.remote_vlm import (
    RemoteGrounder,
    RemoteIdentityVLM,
    RemoteStateVerifier,
)
from openadapt_flow.runtime.replayer import EgressNotPermitted, Replayer
from tests.test_drift_oracle import _check, _step, _Verifier, _Vision
from tests.test_replayer import FakeBackend

# --------------------------------------------------------------------------
# 1. Drift rescue: a model "yes" is a hint, never a pass.
# --------------------------------------------------------------------------


def test_drift_yes_does_not_clear_failed_text_postcondition_on_navigation(
    tmp_path,
):
    pc = Postcondition(
        kind=PostconditionKind.TEXT_PRESENT, text="Patient list", timeout_s=0.0
    )
    ok, failed, _result = _check(_Vision(present=set()), _Verifier("yes"), pc, tmp_path)
    assert ok is False
    assert failed


def test_drift_yes_does_not_clear_failed_region_postcondition_on_read_only(
    tmp_path,
):
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=(0, 0, 10, 10),
        phash="ff",
        timeout_s=0.0,
    )
    step = _step(pc, intent="view the chart", label="Chart")
    ok, failed, _result = _check(
        _Vision(present=set()), _Verifier("yes"), pc, tmp_path, step=step
    )
    assert ok is False
    assert failed


# --------------------------------------------------------------------------
# 2. Unmarked components count as egress.
# --------------------------------------------------------------------------


class _UnmarkedGrounder:
    """A third-party grounder that never declares ``MAY_EGRESS``."""

    def locate(self, screen_png, intent, ocr_text=None):
        return None


def test_unmarked_component_counts_as_egress():
    assert component_may_egress(_UnmarkedGrounder()) is True


def test_unmarked_grounder_requires_explicit_egress_optin():
    with pytest.raises(EgressNotPermitted):
        Replayer(FakeBackend(), grounder=_UnmarkedGrounder())


# --------------------------------------------------------------------------
# 3. Identity-VLM calls are model calls; off-box crops are never "none".
# --------------------------------------------------------------------------


def _crop_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 12), (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


class _OffBoxIdentityVLM:
    """An appliance identity VLM: crops leave the box on every call."""

    MAY_EGRESS = True

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls = 0

    def same_or_different(self, recorded_png, live_png):
        self.calls += 1
        return self.verdict


def _identity_step() -> tuple[Step, Workflow, Resolution]:
    anchor = Anchor(
        template="t.png",
        region=(0, 0, 40, 12),
        click_point=(20, 6),
        # A glyph-confusable identifier, so the VLM tier is consulted.
        context_text="Smith AC50061",
    )
    step = Step(
        id="open_record",
        intent="open record",
        action="click",
        anchor=anchor,
        risk="irreversible",
    )
    workflow = Workflow(name="identity_vlm_egress", params={}, steps=[step])
    res = Resolution(rung="ocr", point=(20, 6), confidence=0.9, elapsed_ms=1.0)
    return step, workflow, res


@pytest.mark.parametrize("verdict", ["different", "same"])
def test_identity_vlm_call_counts_as_model_call(monkeypatch, tmp_path, verdict):
    vlm = _OffBoxIdentityVLM(verdict)
    rp = Replayer(FakeBackend(), identity_vlm=vlm, allow_model_grounding=True)
    crop = _crop_png()
    monkeypatch.setattr(rp, "_identifier_crops", lambda *a, **k: (crop, crop))
    step, workflow, res = _identity_step()
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    rp._identity_gate_error(step, res, crop, {}, workflow, tmp_path, result)
    assert vlm.calls == 1, "precondition: the identity VLM was consulted"
    assert Replayer._result_model_calls(result) >= 1


def test_off_box_identity_crops_never_report_no_external_calls(monkeypatch, tmp_path):
    from openadapt_flow.execution_profiles import _external_network_call_state

    vlm = _OffBoxIdentityVLM("different")
    rp = Replayer(FakeBackend(), identity_vlm=vlm, allow_model_grounding=True)
    crop = _crop_png()
    monkeypatch.setattr(rp, "_identifier_crops", lambda *a, **k: (crop, crop))
    step, workflow, res = _identity_step()
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    rp._identity_gate_error(step, res, crop, {}, workflow, tmp_path, result)
    assert vlm.calls == 1, "precondition: identity crops left the box"

    # A loopback web run (the bundled tutorial's shape) whose identity crops
    # went to an off-box appliance.
    report = RunReport(
        workflow_name=workflow.name,
        started_at=datetime.now(timezone.utc).isoformat(),
        execution_origin="http://127.0.0.1:5001",
        execution_entry_url="http://127.0.0.1:5001/",
        execution_target_kind="web",
        screenshots_may_leave_box=True,
        steps=[result],
    )
    report.model_calls = Replayer._result_model_calls(result)
    assert _external_network_call_state(report) != "none"


# --------------------------------------------------------------------------
# 4. The VLM service honours VLM_VLLM_URL.
# --------------------------------------------------------------------------


def test_vlm_service_passes_vllm_url_to_backend(monkeypatch):
    pytest.importorskip("fastapi")
    import openadapt_flow.services.vlm_service.app as app_mod
    from openadapt_flow.services.vlm_service.config import ServiceConfig

    seen: dict = {}

    def _capture(backend, model=None, **kwargs):
        seen.update(backend=backend, model=model, **kwargs)
        from openadapt_flow.services.vlm_service.backends import StubBackend

        return StubBackend()

    monkeypatch.setattr(app_mod, "build_backend", _capture)
    monkeypatch.setenv("VLM_BACKEND", "vllm")
    monkeypatch.setenv("VLM_VLLM_URL", "http://gpu-box.internal:9000/v1")
    app_mod.create_app(ServiceConfig.from_env())
    assert seen.get("base_url") == "http://gpu-box.internal:9000/v1"


# --------------------------------------------------------------------------
# 5 and 6. build_replayer wiring (no real Replayer, no real OCR rung).
# --------------------------------------------------------------------------


@pytest.fixture()
def wiring(monkeypatch):
    captured: dict = {}

    import openadapt_flow.runtime as rt
    import openadapt_flow.runtime.grounder as gmod

    class _FakeReplayer:
        def __init__(self, backend, **kwargs):
            captured["replayer_kwargs"] = kwargs

    def _fake_build_grounder(fallback=None, **_kwargs):
        captured["fallback"] = fallback
        return fallback

    monkeypatch.setattr(rt, "Replayer", _FakeReplayer)
    monkeypatch.setattr(gmod, "build_grounder", _fake_build_grounder)
    monkeypatch.delenv("OPENADAPT_FLOW_VLM_URL", raising=False)
    monkeypatch.delenv("OPENADAPT_FLOW_VLM_TOKEN", raising=False)
    return captured


def _build(*, runtime_config, phi_mode, governed_authorization=None):
    from openadapt_flow.deployment import build_replayer

    build_replayer(
        object(),
        allow_egress=True,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=True,
        runtime_config=runtime_config,
        phi_mode=phi_mode,
        governed_authorization=governed_authorization,
    )


def _wired_appliance_handles(captured: dict) -> list[str]:
    kwargs = captured.get("replayer_kwargs") or {}
    wired = [
        name
        for name in ("identity_vlm", "state_verifier")
        if isinstance(
            kwargs.get(name), (RemoteIdentityVLM, RemoteStateVerifier)
        )
    ]
    if isinstance(captured.get("fallback"), RemoteGrounder):
        wired.append("grounder")
    return wired


def test_regulated_profile_refuses_cleartext_non_loopback_appliance(
    wiring, monkeypatch
):
    monkeypatch.setenv("OPENADAPT_FLOW_VLM_URL", "http://10.20.30.40:8000")
    monkeypatch.setenv("OPENADAPT_FLOW_VLM_TOKEN", "t")
    runtime = RuntimeSection(phi_grounding_allowlist=["10.20.30.40"])
    authorization = SimpleNamespace(
        execution_profile="regulated", qualification_case_id=None
    )
    _build(
        runtime_config=runtime,
        phi_mode=True,
        governed_authorization=authorization,
    )
    assert _wired_appliance_handles(wiring) == []


def test_unavailable_pinned_model_never_falls_back_to_appliance(wiring, monkeypatch):
    # The operator pinned an openai-compatible model, but it cannot be built
    # (no base_url). The on-prem appliance is a different placement and
    # model; it must not silently stand in for the pinned one.
    monkeypatch.setenv("OPENADAPT_FLOW_VLM_URL", "https://vlm.internal:8443")
    runtime = RuntimeSection(
        grounding_model=GroundingModelConfig(
            enabled=True, provider="openai_compatible", base_url="", model="pinned-m"
        )
    )
    _build(runtime_config=runtime, phi_mode=False)
    assert not isinstance(wiring.get("fallback"), RemoteGrounder)
