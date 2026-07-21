"""Egress guard (PHI audit REM-3): the load-bearing "stays local" claim.

Three guarantees:

1. A DEFAULT ``Replayer(backend).run(...)`` performs ZERO outbound HTTP — the
   transport is stubbed to raise on any use and a full replay still completes.
2. Wiring an egress-capable model component (a grounder / identity-VLM /
   state-verifier that could send a screenshot off the box) WITHOUT the
   operator's explicit ``allow_model_grounding`` opt-in FAILS CLOSED.
3. When such a component IS opted in, the run report flags that screenshots
   could have left the box (surfaced in REPORT.md).
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.report import render_run_report
from openadapt_flow.runtime.grounder import (
    FallbackGrounder,
    NullGrounder,
    component_may_egress,
)
from openadapt_flow.runtime.replayer import EgressNotPermitted, Replayer

from tests.test_replayer import (  # noqa: F401
    FakeBackend,
    FakeVision,
    Match,
    bundle,
    click_step,
    run_dir,
)


class _EgressStub:
    """A stand-in for an off-box grounder (marked like AnthropicGrounder)."""

    MAY_EGRESS = True

    def locate(self, screen_png, intent, ocr_text=None):
        return None  # never actually reaches out; the marker is what matters


def _one_click_workflow() -> tuple[Workflow, FakeVision]:
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    return Workflow(name="wf", steps=[click_step()]), vision


def test_default_replay_makes_zero_outbound_http(bundle, run_dir, monkeypatch):
    # Stub every HTTP transport entry point to raise; a default local replay
    # must never touch them.
    import httpx

    def _boom(*a, **k):  # pragma: no cover - only hit on a regression
        raise AssertionError("default replay attempted an outbound HTTP call")

    monkeypatch.setattr(httpx.Client, "send", _boom, raising=True)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom, raising=True)
    try:
        import requests

        monkeypatch.setattr(requests.sessions.Session, "request", _boom, raising=True)
    except Exception:
        pass

    workflow, vision = _one_click_workflow()
    replayer = Replayer(FakeBackend(), vision=vision)  # no grounder => local
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success
    assert report.screenshots_may_leave_box is False


def test_egress_component_requires_explicit_optin():
    backend = FakeBackend()
    with pytest.raises(EgressNotPermitted):
        Replayer(backend, grounder=_EgressStub())
    with pytest.raises(EgressNotPermitted):
        Replayer(backend, identity_vlm=_EgressStub())
    with pytest.raises(EgressNotPermitted):
        Replayer(backend, state_verifier=_EgressStub())

    # Opting in is allowed and records that screenshots may leave the box.
    rp = Replayer(backend, grounder=_EgressStub(), allow_model_grounding=True)
    assert rp._screenshots_may_leave_box is True


def test_local_grounder_never_trips_the_guard():
    backend = FakeBackend()
    rp = Replayer(backend, grounder=NullGrounder())
    assert rp._screenshots_may_leave_box is False


def test_component_may_egress_classification():
    assert component_may_egress(None) is False
    assert component_may_egress(NullGrounder()) is False
    assert component_may_egress(_EgressStub()) is True
    # A fallback chain is egress iff ANY member is.
    assert component_may_egress(FallbackGrounder([NullGrounder()])) is False
    assert (
        component_may_egress(FallbackGrounder([NullGrounder(), _EgressStub()])) is True
    )


def test_egress_optin_flagged_in_report(bundle, run_dir):
    workflow, vision = _one_click_workflow()
    replayer = Replayer(
        FakeBackend(),
        vision=vision,
        grounder=_EgressStub(),
        allow_model_grounding=True,
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.screenshots_may_leave_box is True
    md = render_run_report(run_dir).read_text()
    assert "Data egress" in md and "left the box" in md
