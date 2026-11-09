"""A governed run never acts on an armed step whose identity it could not verify.

Regression for the OpenEMR referral qualification (2026-10-19): the Contact
tab (step_015) was armed by a static-control identifier crop that sat on the
viewport's bottom edge. At replay the live crop fell off the frame, the pixel
tier abstained, and the identity came back ``unreadable``. The step was not on
the authorization's identity list, so the replayer clicked it anyway, and only
the durable checkpoint refused it afterwards, by raising ``StateDiverged``.
The replayer now halts before the click.
"""

from __future__ import annotations

from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.replayer import Replayer
from tests.test_governed_authorization import _authorization
from tests.test_replayer import (
    VIEWPORT,
    FakeBackend,
    click_step,
    context_click_step,
    make_png,
    resolving_vision,
)


def _seal(tmp_path, workflow: Workflow, *, crop: bool = False):
    bundle = tmp_path / workflow.name
    (bundle / "templates" / "identifiers").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    if crop:
        (bundle / "templates" / "identifiers" / "s1.png").write_bytes(
            make_png((50, 20), color=(20, 120, 220))
        )
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


def _edge_crop_step():
    """An armed button whose only identity is a crop at the viewport edge."""

    step = click_step(ocr_text=None)
    step.identity_armed = True
    step.anchor.identifier_crop = "templates/identifiers/s1.png"
    # The recorded crop ends on the frame's bottom edge; at replay the
    # resolved point sits a few pixels lower, so the live crop is cut off.
    step.anchor.identifier_region = (100, VIEWPORT[1], 50, 20)
    return step


def test_armed_step_with_unreadable_identity_halts_before_clicking(tmp_path):
    # The authorization lists no identity step (as for step_015).
    step = context_click_step("Contact Choices Demographics")
    step.identity_armed = True
    workflow, bundle = _seal(tmp_path, Workflow(name="armed-unreadable", steps=[step]))
    vision = resolving_vision()
    vision.ocr_lines = []  # the context band reads nothing
    backend = FakeBackend()

    report = Replayer(
        backend, vision=vision, governed_authorization=_authorization(workflow)
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is False
    assert backend.actions == []  # never clicked
    result = report.results[0]
    assert result.identity is not None and result.identity.status == "unreadable"
    assert result.delivery_attempted is not True
    assert "refusing to act" in (result.error or "")


def test_edge_crop_identity_halts_before_clicking(tmp_path):
    step = _edge_crop_step()
    workflow, bundle = _seal(
        tmp_path, Workflow(name="edge-crop", steps=[step]), crop=True
    )
    backend = FakeBackend()

    report = Replayer(
        backend,
        vision=resolving_vision(),
        governed_authorization=_authorization(workflow),
        pixel_verify_enabled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].identity is not None
    assert report.results[0].identity.status != "verified"
    assert report.execution_outcome != "VERIFIED"


def test_durable_checkpoint_is_not_the_only_guard(tmp_path):
    """With durable runs on, the halt comes from the identity check, before
    any input, as a normal HALTED report: no StateDiverged escapes."""

    step = _edge_crop_step()
    workflow, bundle = _seal(
        tmp_path, Workflow(name="edge-crop-durable", steps=[step]), crop=True
    )
    backend = FakeBackend()

    report = Replayer(
        backend,
        vision=resolving_vision(),
        governed_authorization=_authorization(workflow),
        pixel_verify_enabled=True,
        durable=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].delivery_attempted is not True


def test_ungoverned_reversible_step_still_proceeds_flagged(tmp_path):
    # Unchanged outside governed runs: a reversible step with an unreadable
    # identity proceeds and carries the flag.
    step = context_click_step("Contact Choices Demographics")
    step.identity_armed = True
    workflow, bundle = _seal(tmp_path, Workflow(name="ungoverned", steps=[step]))
    vision = resolving_vision()
    vision.ocr_lines = []
    backend = FakeBackend()

    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is True
    assert report.results[0].identity.status == "unreadable"
