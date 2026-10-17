"""Repair input and identity checks using encrypted bundles and real vision.

These are boundary tests with constructed local pixels. They do not stand in
for retained runtime repair evidence or a release qualification campaign.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow import vision
from openadapt_flow.ir import ActionKind, Anchor, Resolution, Step, Workflow
from openadapt_flow.repair.campaign import (
    FaultKind,
    _patch_for_anchor,
    fault_battery,
    run_fault_campaign,
    run_replay_campaign,
)
from openadapt_flow.repair.cli import _campaign_inputs
from openadapt_flow.repair.registration import build_candidate
from openadapt_flow.runtime import resolver
from openadapt_flow.runtime.healing.perturbation import (
    DriftCase,
    DriftKind,
    anchor_band_verdict,
    band_sampler,
    identity_row_region,
    perturb,
    perturbation_set,
    replay_patch,
)
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.replayer import Replayer

VIEWPORT = (1000, 260)
REGION = (720, 90, 220, 70)
POINT = (830, 125)
IDENTITY = "Marta Rivera Neurology"


def pixels(identity: str = IDENTITY) -> bytes:
    image = Image.new("RGB", VIEWPORT, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(28)
    draw.text((35, 108), identity, font=font, fill="black")
    draw.rounded_rectangle((720, 90, 940, 160), radius=12, fill=(224, 235, 249))
    draw.text((782, 108), "Save", font=font, fill=(17, 17, 17))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def png_crop(frame: bytes) -> bytes:
    image = Image.open(io.BytesIO(frame))
    stream = io.BytesIO()
    x, y, width, height = REGION
    image.crop((x, y, x + width, y + height)).save(stream, format="PNG")
    return stream.getvalue()


def hashed_anchor() -> Anchor:
    return Anchor(
        template="templates/save.png",
        region=REGION,
        click_point=POINT,
        ocr_text="Save",
        context_text=None,
        identity_template=build_identity_template(IDENTITY),
        identifier_region=(30, 100, 460, 60),
    )


def encrypted_candidate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-test-key")
    frame = pixels()
    source_anchor = hashed_anchor()
    for name, label in (("prior", "Save note"), ("proposed", "Save")):
        bundle = tmp_path / name
        (bundle / "templates").mkdir(parents=True)
        (bundle / "templates/save.png").write_bytes(png_crop(frame))
        anchor = source_anchor.model_copy(update={"ocr_text": label})
        workflow = Workflow(
            name="encrypted-repair-input",
            viewport=VIEWPORT,
            steps=[
                Step(
                    id="save",
                    intent="Save the note",
                    action=ActionKind.CLICK,
                    anchor=anchor,
                )
            ],
        )
        workflow.save(bundle, encrypt=True)
    run = tmp_path / "run"
    (run / "heals/save").mkdir(parents=True)
    (run / "heals/save/screen.png").write_bytes(frame)
    return build_candidate(
        tmp_path / "prior",
        tmp_path / "proposed",
        source="heal",
        evidence_run_dir=run,
    )


def test_campaign_inputs_use_decrypted_crop_without_disk_derivative(
    tmp_path, monkeypatch
):
    candidate = encrypted_candidate(tmp_path, monkeypatch)
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    inputs = _campaign_inputs(candidate)
    assert len(inputs) == 1
    _, anchor, frame, template = inputs[0]
    assert template == png_crop(frame)
    assert anchor.context_text is None
    assert anchor.identity_template is not None
    # The encrypted crop remains encrypted; reading campaign input adds no files.
    assert not (tmp_path / "proposed/templates/save.png").exists()
    assert (tmp_path / "proposed/templates/save.png.enc").exists()
    assert before == {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "observed,expected",
    [(IDENTITY, "verified"), ("David Chen Oncology", "mismatch"), ("", "unreadable")],
)
def test_hashed_band_uses_canonical_identity_verdict(observed, expected):
    anchor = hashed_anchor()
    assert anchor_band_verdict(anchor, observed) == expected


def test_plaintext_cannot_replace_hashed_identity():
    anchor = hashed_anchor().model_copy(update={"context_text": "David Chen Oncology"})
    assert anchor_band_verdict(anchor, "David Chen Oncology") != "verified"


def test_structured_only_identity_is_unavailable_to_pixel_campaign():
    anchor = hashed_anchor().model_copy(
        update={
            "identity_template": build_identity_template(
                None, structured_identity="record=200"
            ),
            "identifier_region": None,
        }
    )
    assert anchor_band_verdict(anchor, "record=200") == "unreadable"


def real_resolver(anchor: Anchor, template: bytes):
    def locate(frame: bytes):
        result = resolver.resolve(anchor, frame, vision, template_png=template)
        return result[0].point if result else None

    return locate


def test_real_ocr_checks_hashed_identity_in_translated_and_scaled_identifier_region():
    anchor = hashed_anchor()
    frame = pixels()
    sample = band_sampler(VIEWPORT, vision, anchor=anchor)
    for kind in (DriftKind.SHIFT, DriftKind.SCALE, DriftKind.RETHEME):
        case = perturb(frame, POINT, kind)
        observed = sample(case.frame_png, case.expected_point) or ""
        assert anchor_band_verdict(anchor, observed) == "verified", (kind, observed)
    assert (
        anchor_band_verdict(anchor, sample(pixels("David Chen Oncology"), POINT) or "")
        != "verified"
    )


def test_real_ocr_excludes_target_label_from_unscoped_identity_band():
    anchor = hashed_anchor().model_copy(update={"identifier_region": None})
    observed = band_sampler(VIEWPORT, vision, anchor=anchor)(pixels(), POINT)
    assert observed is not None and "Save" not in observed
    assert anchor_band_verdict(anchor, observed) == "verified"


def test_replay_requires_hash_material_and_rejects_wrong_live_identity():
    anchor = hashed_anchor()
    frame = pixels()
    patch = _patch_for_anchor("save", anchor)
    sample = band_sampler(VIEWPORT, vision, anchor=anchor)
    locate = real_resolver(anchor, png_crop(frame))
    baseline = DriftCase("baseline", DriftKind.SHIFT, frame, POINT)
    missing = replay_patch(patch, [baseline], resolve=locate, sample_band=sample)
    assert not missing.promotable
    assert not missing.results[0].identity_ok
    correct = replay_patch(
        patch, [baseline], resolve=locate, sample_band=sample, identity_anchor=anchor
    )
    assert correct.promotable
    wrong = DriftCase("wrong", DriftKind.SHIFT, pixels("David Chen Oncology"), POINT)
    report = replay_patch(
        patch, [wrong], resolve=locate, sample_band=sample, identity_anchor=anchor
    )
    assert not report.promotable
    assert report.results[0].located and not report.results[0].identity_ok


def test_real_fault_battery_preserves_all_cases_and_checks_hashed_band():
    anchor = hashed_anchor()
    frame = pixels()
    result = run_fault_campaign(
        "save",
        anchor,
        frame,
        resolve=real_resolver(anchor, png_crop(frame)),
        sample_band=band_sampler(VIEWPORT, vision, anchor=anchor),
    )
    cases = {case.kind: case for case in result.cases}
    assert set(cases) == {
        "ambiguity",
        "wrong_entity",
        "stale_target",
        "unexpected_dialog",
        "verifier_failure",
    }
    assert cases["wrong_entity"].passed
    assert cases["verifier_failure"].passed
    assert all("unarmed" not in case.detail for case in result.cases)


def test_compact_scaled_identifier_does_not_pass_when_runtime_refuses():
    image = Image.new("RGB", VIEWPORT, "white")
    ImageDraw.Draw(image).text(
        (35, 108), "Triage note entry", font=ImageFont.load_default(18), fill="black"
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    anchor = hashed_anchor().model_copy(
        update={
            "identity_template": build_identity_template("Triage note entry"),
            "identifier_region": (30, 100, 190, 45),
        }
    )
    step = Step(
        id="entry", intent="Focus the note", action=ActionKind.CLICK, anchor=anchor
    )
    workflow = Workflow(name="compact-identity", viewport=VIEWPORT, steps=[step])
    case = perturb(output.getvalue(), POINT, DriftKind.SCALE)
    live_size = Image.open(io.BytesIO(case.frame_png)).size
    replayer = Replayer(SimpleNamespace(viewport=live_size), vision=vision)
    runtime = replayer._verify_identity_ocr(
        step,
        Resolution(
            rung="template", point=case.expected_point, confidence=1.0, elapsed_ms=0.0
        ),
        case.frame_png,
        {},
        workflow,
    )
    observed = (
        band_sampler(VIEWPORT, vision, anchor=anchor)(
            case.frame_png, case.expected_point
        )
        or ""
    )
    campaign = anchor_band_verdict(anchor, observed)
    assert runtime.status != "verified"
    assert campaign != "verified"


def test_reflow_moves_complete_target_and_identity_row_without_splitting_pixels():
    anchor = hashed_anchor()
    frame = pixels()
    case = next(
        c for c in perturbation_set(frame, anchor) if c.kind == DriftKind.REFLOW
    )
    assert case.construction_error is None
    x, y, width, height = identity_row_region(anchor, VIEWPORT)
    before = Image.open(io.BytesIO(frame))
    after = Image.open(io.BytesIO(case.frame_png))
    assert (
        before.crop((x, y, x + width, y + height)).tobytes()
        == after.crop((x, y + 24, x + width, y + height + 24)).tobytes()
    )
    assert case.expected_point == (POINT[0], POINT[1] + 24)
    observed = (
        band_sampler(VIEWPORT, vision, anchor=anchor)(
            case.frame_png, case.expected_point
        )
        or ""
    )
    assert anchor_band_verdict(anchor, observed) == "verified"
    located = real_resolver(anchor, png_crop(frame))(case.frame_png)
    assert located == case.expected_point


def test_ambiguity_has_two_valid_targets_in_search_scope_without_original():
    anchor = hashed_anchor()
    frame = pixels()
    case = next(
        c for c in fault_battery(frame, anchor) if c.kind == FaultKind.AMBIGUITY
    )
    assert case.construction_error is None
    assert len(case.candidate_points) == 2
    sx, sy, sw, sh = resolver.pad_region(anchor.region, anchor.search_pad, VIEWPORT)
    sample = band_sampler(VIEWPORT, vision, anchor=anchor)
    for point in case.candidate_points:
        dx, dy = point[0] - POINT[0], point[1] - POINT[1]
        x, y, width, height = anchor.region
        region = (x + dx, y + dy, width, height)
        assert sx <= region[0] and region[0] + width <= sx + sw
        assert sy <= region[1] and region[1] + height <= sy + sh
        match = vision.find_template(
            case.frame_png,
            png_crop(frame),
            search_region=region,
            scales=(1.0,),
            threshold=0.99,
        )
        assert match is not None and match.confidence >= 0.99
        assert (
            anchor_band_verdict(anchor, sample(case.frame_png, point) or "")
            == "verified"
        )
    assert (
        vision.find_template(
            case.frame_png,
            png_crop(frame),
            search_region=anchor.region,
            scales=(1.0,),
            threshold=0.99,
        )
        is None
    )
    with pytest.raises(resolver.AmbiguousOcrMatchError):
        real_resolver(anchor, png_crop(frame))(case.frame_png)
    result = run_fault_campaign(
        "save",
        anchor,
        frame,
        resolve=real_resolver(anchor, png_crop(frame)),
        sample_band=sample,
    )
    assert result.passed
    assert len(result.cases) == 5


def test_invalid_ambiguity_geometry_is_retained_as_failure_not_a_safe_refusal():
    anchor = hashed_anchor().model_copy(update={"search_pad": 0})
    frame = pixels()
    result = run_fault_campaign(
        "save",
        anchor,
        frame,
        resolve=real_resolver(anchor, png_crop(frame)),
        sample_band=band_sampler(VIEWPORT, vision, anchor=anchor),
    )
    assert len(result.cases) == 5
    ambiguity = next(c for c in result.cases if c.kind == "ambiguity")
    assert not ambiguity.passed
    assert "invalid fault fixture" in ambiguity.detail
    assert not result.passed


def test_reflow_that_would_clip_identity_is_retained_as_failed_condition():
    anchor = hashed_anchor().model_copy(
        update={"identifier_region": (30, 220, 190, 40)}
    )
    frame = pixels()
    result = run_replay_campaign(
        "save",
        anchor,
        frame,
        resolve=real_resolver(anchor, png_crop(frame)),
        sample_band=band_sampler(VIEWPORT, vision, anchor=anchor),
    )
    assert len(result.cases) == 5
    reflow = next(c for c in result.cases if c.kind == "reflow")
    assert not reflow.passed
    assert "invalid drift fixture" in reflow.detail
    assert not result.passed
