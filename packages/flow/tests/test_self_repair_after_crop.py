"""Synthetic coverage for the first crop staged by a one-click after bridge."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw

from openadapt_flow.compiler import compile as compiler
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.repair.candidate import ScreenScrubEvidence
from openadapt_flow.repair.self_repair import (
    AppSession,
    RepairProposal,
    ReplayTarget,
    ScreenAccess,
    SelfRepair,
)
from openadapt_flow.runtime.identity_template import build_identity_template


def _frame(rect=None):
    image = Image.new("RGB", (160, 120), "white")
    if rect is not None:
        ImageDraw.Draw(image).rectangle(rect, fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_single_after_bridge_stages_the_halted_step_region(tmp_path, monkeypatch):
    before = _frame()
    notice = _frame((40, 36, 100, 78))
    dismissed = _frame((65, 35, 115, 65))
    monkeypatch.setattr(compiler, "ocr", lambda _png: [])

    identity = "Synthetic record R-17"
    step = Step(
        id="open_details",
        intent="Open details",
        action=ActionKind.CLICK,
        risk="reversible",
        anchor=Anchor(
            template="templates/open_details.png",
            region=(8, 8, 24, 24),
            click_point=(20, 20),
            identity_template=build_identity_template(
                None, structured_identity=identity
            ),
        ),
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Details ready")
        ],
    )
    source = tmp_path / "source"
    (source / "templates").mkdir(parents=True)
    original_crop = source / "templates/open_details.png"
    original_crop.write_bytes(before)
    Workflow(name="synthetic-after-bridge", steps=[step]).save(source)
    original_manifest = (source / "manifest.json").read_bytes()
    loaded = Workflow.load(source)

    click = Mock()
    backend = SimpleNamespace(
        click=click,
        structured_text_at=lambda _x, _y: identity,
        structural_locator_at=lambda _x, _y: StructuralLocator(
            role="button", name="Dismiss notice"
        ),
    )
    session = AppSession(backend=backend, close=lambda: None)
    repair = SelfRepair(
        SimpleNamespace(
            dialect="generic", agent_id="test:synthetic", requires_model_key=False
        ),
        ReplayTarget(lambda _fault: session, effect_verifier=None),
        store=tmp_path / "store",
        screen_access=ScreenAccess(enabled_by="test"),
        vision=SimpleNamespace(
            ocr=lambda *_args, **_kwargs: [], wait_settled=lambda _backend: dismissed
        ),
    )
    attempt = tmp_path / "run" / "self-repair" / "attempt-1"
    staged = attempt / "evidence" / "expect"
    assert not staged.exists()
    patch = repair._patch(
        loaded,
        source,
        loaded.steps[0],
        "after",
        RepairProposal(actions=[{"kind": "click", "x": 60, "y": 55}]),
        session,
        notice,
        1,
        attempt,
        ScreenScrubEvidence(provider="synthetic-test", package_version="0.0.0"),
        [False],
        before,
    )

    click.assert_called_once_with(60, 55, double=False)
    repaired = Workflow.load(patch.bundle_dir)
    halted, bridge = repaired.steps
    assert halted.id == step.id and bridge.id == f"{step.id}_bridge_1"
    assert halted.anchor == loaded.steps[0].anchor
    assert bridge.expect == loaded.steps[0].expect
    [region] = [
        pc for pc in halted.expect if pc.kind is PostconditionKind.REGION_STABLE
    ]
    crop = "templates/open_details_now_expect.png"
    assert region.template == crop
    assert repaired.manifest is not None and crop in repaired.manifest.file_hashes
    assert (patch.bundle_dir / crop).read_bytes() == (staged / crop).read_bytes()
    assert not (source / crop).exists()
    assert original_crop.read_bytes() == before
    assert (source / "manifest.json").read_bytes() == original_manifest
