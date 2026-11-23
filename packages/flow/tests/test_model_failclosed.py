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
7. Every frame sent off the box is on a local append-only ledger, written
   before the call, halted runs included; no ledger line, no call.
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
from tests.test_drift_oracle import _Backend, _png, _step, _Vision
from tests.test_replayer import FakeBackend

# --------------------------------------------------------------------------
# 1. Drift rescue: a model "yes" is a hint, never a pass.
# --------------------------------------------------------------------------


class _Verifier:
    """An appliance state-verifier (marked egress) with a scripted answer."""

    MAY_EGRESS = True

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def holds(self, screenshot, expected_state) -> bool:
        self.calls += 1
        return self.answer == "yes"


def _check(vision, verifier, pc, tmp_path, step=None):
    rp = Replayer(
        _Backend(),
        vision=vision,
        state_verifier=verifier,
        allow_model_grounding=True,
        poll_interval_s=0.0,
    )
    step = step if step is not None else _step(pc)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    ok, _frame, failed = rp._check_postconditions(step, _png(), tmp_path, {}, result)
    return ok, failed, result


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
    rp._egress.bind_run(tmp_path / "run")
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
    rp._egress.bind_run(tmp_path / "run")
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
        if isinstance(kwargs.get(name), (RemoteIdentityVLM, RemoteStateVerifier))
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


def test_empty_pinned_model_is_refused_not_defaulted(wiring, monkeypatch, capsys):
    from openadapt_flow.deployment import build_model_grounder

    cfg = GroundingModelConfig(enabled=True, provider="anthropic", model="  ")
    assert build_model_grounder(cfg) is None
    assert "requires a model" in capsys.readouterr().out

    # Wired through build_replayer: no default model, and no appliance either.
    monkeypatch.setenv("OPENADAPT_FLOW_VLM_URL", "https://vlm.internal:8443")
    _build(runtime_config=RuntimeSection(grounding_model=cfg), phi_mode=False)
    assert wiring.get("fallback") is None


def test_regulated_profile_keeps_tls_and_loopback_appliances(wiring, monkeypatch):
    for url in ("https://10.20.30.40:8443", "http://127.0.0.1:8000"):
        wiring.clear()
        monkeypatch.setenv("OPENADAPT_FLOW_VLM_URL", url)
        runtime = RuntimeSection(phi_grounding_allowlist=["10.20.30.40", "127.0.0.1"])
        authorization = SimpleNamespace(
            execution_profile="regulated", qualification_case_id=None
        )
        _build(
            runtime_config=runtime,
            phi_mode=True,
            governed_authorization=authorization,
        )
        assert _wired_appliance_handles(wiring) == [
            "identity_vlm",
            "state_verifier",
            "grounder",
        ], url


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("https://vlm.internal:8443", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("http://localhost:8000", True),
        ("http://10.20.30.40:8000", False),
        ("http://localhost.evil.example:8000", False),
        ("ftp://vlm.internal", False),
        ("vlm.internal:8000", False),
        ("", False),
    ],
)
def test_regulated_transport_rule(url, allowed):
    from openadapt_flow.model_transport import regulated_transport_refusal

    assert (regulated_transport_refusal(url) is None) is allowed


def test_appliance_still_backs_an_unpinned_run(wiring, monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_VLM_URL", "https://vlm.internal:8443")
    _build(runtime_config=RuntimeSection(), phi_mode=False)
    assert isinstance(wiring.get("fallback"), RemoteGrounder)


# --------------------------------------------------------------------------
# 7. The egress ledger, and grounder calls that abstained.
# --------------------------------------------------------------------------


class _OffBoxGrounder:
    MAY_EGRESS = True
    base_url = "https://vlm.internal:8443"

    def __init__(self) -> None:
        self.calls = 0

    def locate(self, screen_png, intent, ocr_text=None):
        self.calls += 1
        return None  # abstains after the frame was sent


def _ledger_lines(path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines()]


def test_off_box_frame_is_ledgered_before_the_call(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENADAPT_FLOW_EGRESS_LEDGER", raising=False)
    inner = _OffBoxGrounder()
    rp = Replayer(FakeBackend(), grounder=inner, allow_model_grounding=True)
    rp._egress.bind_run(tmp_path / "runs" / "r1")
    rp._egress.step_id = "s1"
    frame = _png()
    assert rp.grounder.locate(frame, "open the chart") is None
    assert inner.calls == 1
    (line,) = _ledger_lines(tmp_path / "runs" / "egress-ledger.jsonl")
    assert line["component"] == "grounder"
    assert line["destination"] == "vlm.internal"
    assert line["run"] == "r1" and line["step"] == "s1"
    import hashlib

    assert line["frames"] == [
        {"sha256": hashlib.sha256(frame).hexdigest(), "bytes": len(frame)}
    ]
    mode = (tmp_path / "runs" / "egress-ledger.jsonl").stat().st_mode & 0o777
    assert mode == 0o600

    # The abstaining call still counts as an off-box model call.
    result = StepResult(step_id="s1", intent="open the chart", ok=False)
    rp._egress.drain(result)
    assert result.grounder_calls == 1
    assert Replayer._result_model_calls(result) == 1


def test_no_ledger_line_means_no_call(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("OPENADAPT_FLOW_EGRESS_LEDGER", str(blocker / "ledger.jsonl"))
    inner = _OffBoxGrounder()
    vlm = _OffBoxIdentityVLM("same")
    verifier = _Verifier("yes")
    rp = Replayer(
        FakeBackend(),
        grounder=inner,
        identity_vlm=vlm,
        state_verifier=verifier,
        allow_model_grounding=True,
    )
    rp._egress.bind_run(tmp_path / "run")
    crop = _crop_png()
    assert rp.grounder.locate(crop, "x") is None
    assert rp.identity_vlm.same_or_different(crop, crop) == "different"
    assert rp.state_verifier.holds(crop, "x") is False
    assert (inner.calls, vlm.calls, verifier.calls) == (0, 0, 0)


def test_halted_run_frames_are_on_the_ledger(tmp_path, monkeypatch):
    from tests.test_drift_oracle import _run, _text_present_pc

    monkeypatch.delenv("OPENADAPT_FLOW_EGRESS_LEDGER", raising=False)
    verifier = _Verifier("yes")
    report, _backend = _run(
        tmp_path, [_step(_text_present_pc("Patient list"))], verifier
    )
    assert report.success is False
    (line,) = _ledger_lines(tmp_path / "egress-ledger.jsonl")
    assert line["component"] == "state_verifier"
    assert line["step"] == "s1"
    assert report.model_calls == 1


def test_in_process_identity_vlm_counts_as_local(monkeypatch, tmp_path):
    vlm = _OffBoxIdentityVLM("different")
    vlm.MAY_EGRESS = False
    rp = Replayer(FakeBackend(), identity_vlm=vlm)
    crop = _crop_png()
    monkeypatch.setattr(rp, "_identifier_crops", lambda *a, **k: (crop, crop))
    step, workflow, res = _identity_step()
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    rp._identity_gate_error(step, res, crop, {}, workflow, tmp_path, result)
    assert vlm.calls == 1
    assert Replayer._result_model_calls(result) == 0
    assert Replayer._result_local_model_calls(result) == 1
    assert rp._egress.entries == []


# --------------------------------------------------------------------------
# 3, receipt side. The model-call rule is split by placement.
# --------------------------------------------------------------------------


def _receipt_report(*, model_calls, local_model_calls, network):
    from tests.test_run_receipt import _report

    base = _report()
    classes = list(base.outcome_envelope.evidence_classes)
    if model_calls:
        classes = sorted({*classes, "model"})
    report = _report(
        model_calls=model_calls,
        outcome_envelope=base.outcome_envelope.model_copy(
            update={"evidence_classes": classes, "external_network_calls": network}
        ),
    )
    report.local_model_calls = local_model_calls
    return report


def test_receipt_refuses_off_box_calls_with_no_external_calls():
    from openadapt_flow.model_call_evidence import model_call_evidence_error
    from openadapt_flow.receipt import build_receipt

    assert model_call_evidence_error(1, ["model"], "none") is not None
    with pytest.raises(Exception, match="observed external network"):
        build_receipt(
            _receipt_report(model_calls=1, local_model_calls=0, network="none")
        )
    receipt = build_receipt(
        _receipt_report(model_calls=1, local_model_calls=0, network="observed")
    )
    assert receipt.model_calls == 1 and receipt.external_network_calls == "observed"


def test_receipt_lets_in_process_calls_say_no_external_calls():
    from openadapt_flow.model_call_evidence import model_call_evidence_error
    from openadapt_flow.receipt import build_receipt

    assert model_call_evidence_error(0, [], "none") is None
    receipt = build_receipt(
        _receipt_report(model_calls=0, local_model_calls=2, network="none")
    )
    assert receipt.model_calls == 0
    assert receipt.external_network_calls == "none"
    assert "model" not in receipt.evidence_classes


def test_counts_from_a_raised_step_never_land_on_the_next_run(tmp_path):
    from openadapt_flow.runtime.model_egress import EgressRecorder

    recorder = EgressRecorder()
    recorder.count("identity_vlm_calls")
    recorder.drain(None)  # the step raised: no result to put them on
    recorder.bind_run(tmp_path / "run-2")
    fresh = StepResult(step_id="s1", intent="x", ok=True)
    recorder.drain(fresh)
    assert fresh.identity_vlm_calls == 0


def test_no_bound_ledger_means_no_off_box_call(monkeypatch):
    from openadapt_flow.runtime.model_egress import LEDGER_ENV, EgressRecorder

    monkeypatch.delenv(LEDGER_ENV, raising=False)
    recorder = EgressRecorder()
    grounder = _OffBoxGrounder()
    wrapped = recorder.wrap("grounder", grounder)
    assert wrapped.locate(b"frame", "click Save") is None
    assert grounder.calls == 0 and recorder.entries == []


def test_model_grounder_checked_at_the_url_its_client_uses(wiring, monkeypatch):
    # The Anthropic SDK takes ANTHROPIC_BASE_URL from the environment; the
    # regulated check must see that URL, not the empty configured base_url.
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://10.20.30.40:8080")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setenv("OPENADAPT_FLOW_SEND_UNSCRUBBED", "wiring unit test")
    runtime = RuntimeSection(
        profile="regulated",
        grounding_model=GroundingModelConfig(
            enabled=True, provider="anthropic", model="pinned-m"
        ),
    )
    _build(runtime_config=runtime, phi_mode=False)
    assert wiring.get("fallback") is None


def test_each_model_send_is_one_ledgered_counted_call(tmp_path, monkeypatch):
    # The Anthropic SDK retries twice by default, so one ledgered, counted call
    # could send the same frame three times. The grounder turns retries off:
    # three sends must be three ledger lines and three counted calls.
    anthropic = pytest.importorskip("anthropic")
    import httpx

    from openadapt_flow.runtime.grounder import AnthropicGrounder
    from openadapt_flow.runtime.model_egress import LEDGER_ENV, EgressRecorder

    sends = []

    def overloaded(request):
        sends.append(request)
        return httpx.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error"}}
        )

    client = anthropic.Anthropic(
        api_key="test-not-a-key",
        base_url="https://model.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(overloaded)),
    )
    assert client.max_retries > 0  # the SDK default this guards against
    monkeypatch.setenv("OPENADAPT_FLOW_SEND_UNSCRUBBED", "retry count unit test")
    ledger = tmp_path / "egress-ledger.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(ledger))
    recorder = EgressRecorder()
    grounder = recorder.wrap(
        "grounder", AnthropicGrounder(model="pinned-m", client=client)
    )
    for _ in range(3):
        with pytest.raises(anthropic.APIStatusError):
            grounder.locate(_png(), "click Save")
    assert len(sends) == 3
    result = StepResult(step_id="s1", intent="click Save", ok=False)
    recorder.drain(result)
    assert result.grounder_calls == 3
    lines = _ledger_lines(ledger)
    assert len(lines) == 3
    assert {line["destination"] for line in lines} == {"model.example.test"}


def test_ledger_names_the_openai_compatible_host(tmp_path, monkeypatch):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder
    from openadapt_flow.runtime.model_egress import LEDGER_ENV, EgressRecorder

    class _Refusing:
        def post(self, url, **kwargs):
            return SimpleNamespace(status_code=503)

    monkeypatch.setenv("OPENADAPT_FLOW_SEND_UNSCRUBBED", "host unit test")
    ledger = tmp_path / "egress-ledger.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(ledger))
    grounder = EgressRecorder().wrap(
        "grounder",
        OpenAICompatibleGrounder(
            base_url="https://vlm.example.test/v1", model="m", client=_Refusing()
        ),
    )
    assert grounder.locate(_png(), "click Save") is None
    (line,) = _ledger_lines(ledger)
    assert line["destination"] == "vlm.example.test"


def test_replayer_built_without_init_still_ledgers_off_box_calls(monkeypatch):
    # A Replayer made with __new__ gets its recorder on first use, and the
    # components already set are wrapped, so nothing is sent unrecorded.
    from openadapt_flow.runtime.model_egress import LEDGER_ENV

    monkeypatch.delenv(LEDGER_ENV, raising=False)
    rp = Replayer.__new__(Replayer)
    inner = _OffBoxGrounder()
    rp.grounder = inner
    recorder = rp._egress
    assert rp._egress is recorder
    assert rp.grounder is not inner
    assert rp.grounder.locate(b"frame", "click Save") is None  # no ledger bound
    assert inner.calls == 0
