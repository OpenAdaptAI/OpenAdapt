"""Variant router on MockMed: theme and input variants, each separately admitted.

The theme cases read real MockMed frames (light, and the ``?drift=theme`` dark
palette) captured with headless Chromium. Every routing case, the no-match
halt, and the admission refusals run without a model call.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Iterator

import pytest
from PIL import Image

from openadapt_flow.admitted_composition import live_bundle_content_digest
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.mockmed.server import serve
from openadapt_flow.receipt import ReceiptError, build_receipt
from openadapt_flow.runtime.composition import ChildRunResult
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.variant_router import (
    VARIANT_REPORT_FILENAME,
    VariantRunReport,
    classify_theme,
    execute_variant_set,
    route_variant,
)
from openadapt_flow.runtime.variant_stamp import current_variant_route
from openadapt_flow.variants import (
    VariantInput,
    VariantSet,
    VariantSetError,
    VariantWhen,
    add_variant,
    author_variant_set,
    is_variant_set_artifact,
)
from tests.test_admitted_composition_authoring import _envelope_for
from tests.test_loop_authoring import (
    _encounter_body,
    _RowWritingBackend,
    _vision_confirms_saved,
)
from tests.test_qualification_admission import NOW, _trust
from tests.test_run_receipt import _report

ADMISSIONS = {
    "light": "a1111111-1111-4111-8111-111111111111",
    "dark": "a2222222-2222-4222-8222-222222222222",
    "medicare": "a3333333-3333-4333-8333-333333333333",
    "medicare_v2": "a4444444-4444-4444-8444-444444444444",
}
VERSIONS = {
    "light": "b1111111-1111-4111-8111-111111111111",
    "dark": "b2222222-2222-4222-8222-222222222222",
    "medicare": "b3333333-3333-4333-8333-333333333333",
    "medicare_v2": "b4444444-4444-4444-8444-444444444444",
}

#: Light-theme MockMed, commercial claims; dark-theme VDI, any payer;
#: light-theme MockMed, Medicare claims (a different path through the app).
CONDITIONS = {
    "light": VariantWhen(theme="light", inputs={"payer": ["commercial"]}),
    "dark": VariantWhen(theme="dark"),
    "medicare": VariantWhen(theme="light", inputs={"payer": ["medicare"]}),
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _shot(url: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800}, device_scale_factor=1
            )
            page.goto(url)
            page.wait_for_selector("#signin")
            return page.screenshot()
        finally:
            browser.close()


@pytest.fixture(scope="module")
def mockmed_frames() -> Iterator[dict[str, bytes]]:
    url, stop = serve(port=0)
    try:
        yield {"light": _shot(url), "dark": _shot(f"{url}?drift=theme")}
    finally:
        stop()


def _bundle(tmp_path: Path, name: str) -> tuple[Path, Path]:
    workflow = _encounter_body()
    workflow.name = f"save-encounter-{name}"
    folder = tmp_path / "bundles" / name
    folder.mkdir(parents=True)
    workflow.save(folder)
    envelope = _envelope_for(
        folder, admission_id=ADMISSIONS[name], workflow_version_id=VERSIONS[name]
    )
    return folder, envelope


def _author(
    tmp_path: Path, names=("light", "dark", "medicare")
) -> tuple[VariantSet, Path]:
    items = []
    for name in names:
        bundle, envelope = _bundle(tmp_path, name)
        items.append(
            VariantInput(
                name=name, bundle=bundle, admission=envelope, when=CONDITIONS[name]
            )
        )
    out = tmp_path / "intake-variants"
    return author_variant_set(items, name="encounter-intake", out=out), out


def _recording_child_run(calls: list[dict]):
    """A governed-run stand-in that writes report.json the way the replayer does."""

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        calls.append({"child": child, "inputs": dict(inputs), "bundle": bundle_dir})
        report = RunReport(
            workflow_name=workflow.name,
            started_at="2026-08-18T18:00:00+00:00",
            execution_outcome="VERIFIED",
            success=True,
            variant_route=current_variant_route(),
        )
        path = report.save(run_dir)
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
            report_path=str(path),
        )

    return child_run


def _run(variant_set, parent, tmp_path, *, inputs, frame, calls, **kwargs):
    return execute_variant_set(
        variant_set,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        inputs=inputs,
        first_screen=frame,
        child_run=_recording_child_run(calls),
        trusted_signers=_trust(),
        now=NOW,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# routing cases
# ---------------------------------------------------------------------------


def test_real_mockmed_frames_fingerprint_by_theme(mockmed_frames) -> None:
    from openadapt_flow.runtime.variant_router import screen_fingerprint

    light = screen_fingerprint(mockmed_frames["light"], markers=[])
    dark = screen_fingerprint(mockmed_frames["dark"], markers=[])
    assert light.theme == "light"
    assert dark.theme == "dark"
    assert light.frame_sha256 != dark.frame_sha256


@pytest.mark.parametrize(
    ("theme", "payer", "expected"),
    [
        ("light", "commercial", "light"),
        ("dark", "commercial", "dark"),
        ("dark", "medicare", "dark"),
        ("light", "medicare", "medicare"),
    ],
)
def test_each_routing_case_runs_exactly_the_matching_variant(
    tmp_path: Path, mockmed_frames, theme, payer, expected
) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": payer, "patient_id": "p-100", "note": "triage"},
        frame=mockmed_frames[theme],
        calls=calls,
    )
    assert report.outcome == "VERIFIED"
    assert report.reason == "routed"
    assert report.decision.variant == expected
    assert report.decision.model_calls == 0
    assert [call["child"] for call in calls] == [expected]
    # Routing-only inputs stay with the router; the bundle gets its own params.
    assert calls[0]["inputs"] == {"patient_id": "p-100", "note": "triage"}
    assert report.stamp is not None
    assert report.stamp.variant == expected
    assert report.stamp.admission_id == ADMISSIONS[expected]
    assert report.child is not None and report.child.report_names_variant
    written = VariantRunReport.model_validate_json(
        (tmp_path / "run" / VARIANT_REPORT_FILENAME).read_text()
    )
    assert written.decision.variant == expected
    child_report = RunReport.model_validate_json(
        (tmp_path / "run" / expected / "report.json").read_text()
    )
    assert child_report.variant_route == report.stamp


def test_input_only_set_never_reads_the_screen(tmp_path: Path) -> None:
    items = []
    for name, payer in (("light", "commercial"), ("medicare", "medicare")):
        bundle, envelope = _bundle(tmp_path, name)
        items.append(
            VariantInput(
                name=name,
                bundle=bundle,
                admission=envelope,
                when=VariantWhen(inputs={"payer": [payer]}),
            )
        )
    variant_set = author_variant_set(items, name="by-payer", out=tmp_path / "set")

    def no_screen() -> bytes:
        raise AssertionError("an input-only set must not capture the screen")

    decision = route_variant(
        variant_set, inputs={"payer": "medicare"}, first_screen=no_screen
    )
    assert decision.variant == "medicare"
    assert decision.fingerprint is None


class _OcrVision:
    """OCR stand-in: the lines the local OCR read returns for the frame."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)

    def ocr(self, frame: bytes, *, region=None):
        return [type("Line", (), {"text": line})() for line in self.lines]

    def text_present(self, frame: bytes, text: str) -> bool:  # pragma: no cover
        raise AssertionError("routing markers must not use fuzzy text_present")


def _version_set(tmp_path: Path) -> VariantSet:
    items = []
    for name, when in (
        ("light", VariantWhen(text_present=["Version 11"])),
        ("medicare", VariantWhen(text_present=["Version 12"])),
    ):
        bundle, envelope = _bundle(tmp_path, name)
        items.append(
            VariantInput(name=name, bundle=bundle, admission=envelope, when=when)
        )
    return author_variant_set(items, name="by-version", out=tmp_path / "set")


def test_app_version_marker_routes_on_exact_words(tmp_path: Path) -> None:
    variant_set = _version_set(tmp_path)
    frame = _png(240)

    def route(*lines: str):
        return route_variant(
            variant_set, inputs={}, first_screen=frame, vision=_OcrVision(*lines)
        )

    # A fuzzy match would find "Version 11" in this line; whole words do not.
    assert route("MockMed EMR Version 12").variant == "medicare"
    assert route("MockMed EMR  version 11").variant == "light"
    # An OCR misread satisfies neither presence condition: halt, not a default.
    assert route("MockMed EMR Version l2").reason == "no_variant_matched"
    assert route("Version 12.1").reason == "no_variant_matched"
    # Both markers on one screen: each variant matches, so the router halts.
    assert route("Version 11", "Version 12").reason == "ambiguous_variant_match"


def test_marker_word_boundaries() -> None:
    from openadapt_flow.runtime.variant_router import marker_on_lines

    assert marker_on_lines("Version 12", ["EMR Version 12"])
    assert not marker_on_lines("Version 1", ["EMR Version 11"])
    assert not marker_on_lines("Version 11", ["EMR Version 12"])
    assert marker_on_lines("Acme Clinic", ["Tenant: (ACME   clinic)"])


# ---------------------------------------------------------------------------
# halts: never guess
# ---------------------------------------------------------------------------


def test_no_variant_matches_halts_with_a_named_reason(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "SECRET-SELF-PAY", "patient_id": "SECRET-MRN"},
        frame=mockmed_frames["light"],
        calls=calls,
    )
    assert report.outcome == "HALTED"
    assert report.reason == "no_variant_matched"
    assert report.child is None and report.stamp is None
    assert calls == []
    unmet = {item.variant: item.unmet for item in report.decision.evaluations}
    assert unmet["dark"] == ["theme is light, variant needs dark"]
    assert unmet["light"] == ["input 'payer' is not one of ['commercial']"]
    text = (tmp_path / "run" / VARIANT_REPORT_FILENAME).read_text()
    assert "SECRET-SELF-PAY" not in text
    assert "SECRET-MRN" not in text
    assert report.decision.inputs_supplied == ["patient_id", "payer"]


def test_missing_routing_input_is_a_no_match(tmp_path: Path, mockmed_frames) -> None:
    variant_set, _parent = _author(tmp_path)
    decision = route_variant(
        variant_set, inputs={}, first_screen=mockmed_frames["light"]
    )
    assert decision.reason == "no_variant_matched"
    unmet = {item.variant: item.unmet for item in decision.evaluations}
    assert unmet["medicare"] == ["input 'payer' was not supplied"]


def test_indeterminate_theme_is_not_guessed(tmp_path: Path) -> None:
    variant_set, _parent = _author(tmp_path)
    assert classify_theme(128.0) == "indeterminate"
    decision = route_variant(
        variant_set, inputs={"payer": "commercial"}, first_screen=_png(128)
    )
    assert decision.reason == "no_variant_matched"
    assert decision.fingerprint is not None
    assert decision.fingerprint.theme == "indeterminate"


def test_screen_condition_without_a_frame_halts(tmp_path: Path) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "commercial"},
        frame=None,
        calls=calls,
    )
    assert report.reason == "first_screen_unavailable"
    assert calls == []

    def broken() -> bytes:
        raise RuntimeError("window is gone")

    decision = route_variant(variant_set, inputs={}, first_screen=broken)
    assert decision.reason == "first_screen_unavailable"


def test_two_matches_halt_as_ambiguous(tmp_path: Path, mockmed_frames) -> None:
    variant_set, _parent = _author(tmp_path)
    # A hand-edited set that skipped validation: both variants accept light.
    overlapping = VariantSet.model_construct(
        schema_version=variant_set.schema_version,
        name=variant_set.name,
        variants=[
            variant_set.variant("light").model_copy(
                update={"when": VariantWhen(theme="light")}
            ),
            variant_set.variant("medicare").model_copy(
                update={"when": VariantWhen(theme="light")}
            ),
        ],
    )
    decision = route_variant(
        overlapping, inputs={}, first_screen=mockmed_frames["light"]
    )
    assert decision.outcome == "HALTED"
    assert decision.reason == "ambiguous_variant_match"
    assert decision.variant is None


def test_authoring_refuses_conditions_that_can_overlap(tmp_path: Path) -> None:
    items = []
    for name, when in (
        ("dark", VariantWhen(theme="dark")),
        ("medicare", VariantWhen(inputs={"payer": ["medicare"]})),
    ):
        bundle, envelope = _bundle(tmp_path, name)
        items.append(
            VariantInput(name=name, bundle=bundle, admission=envelope, when=when)
        )
    with pytest.raises(VariantSetError, match="can both match"):
        author_variant_set(items, name="overlap", out=tmp_path / "set")


def test_unconditional_variant_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one condition"):
        VariantWhen()


# ---------------------------------------------------------------------------
# admission per variant
# ---------------------------------------------------------------------------


def test_each_variant_keeps_its_own_admission(tmp_path: Path) -> None:
    variant_set, parent = _author(tmp_path)
    assert is_variant_set_artifact(parent)
    assert {spec.name: spec.admission_id for spec in variant_set.variants} == {
        name: ADMISSIONS[name] for name in ("light", "dark", "medicare")
    }
    stored = json.loads((parent / "variant-set.json").read_text())
    assert stored["schema_version"] == "openadapt.variant-set/v0"
    assert len({spec.bundle_content_digest for spec in variant_set.variants}) == 3


def test_revoked_chosen_admission_halts_without_fallback(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "commercial"},
        frame=mockmed_frames["dark"],
        calls=calls,
        revoked_admission_ids={ADMISSIONS["dark"]},
    )
    assert report.outcome == "HALTED"
    assert report.reason == "variant_admission_refused"
    assert report.decision.variant == "dark"
    assert "revoked" in report.detail
    assert calls == []


def test_tampered_chosen_bundle_halts(tmp_path: Path, mockmed_frames) -> None:
    variant_set, parent = _author(tmp_path)
    workflow = Workflow.load(tmp_path / "bundles" / "dark")
    workflow.steps[1].intent = "type something else"
    workflow.save(tmp_path / "bundles" / "dark")
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={},
        frame=mockmed_frames["dark"],
        calls=calls,
    )
    assert report.reason == "variant_admission_refused"
    assert "live bundle digest" in report.detail
    assert calls == []


def test_unadmitted_bundle_cannot_become_a_variant(tmp_path: Path) -> None:
    bundle, envelope = _bundle(tmp_path, "light")
    other, _ = _bundle(tmp_path, "dark")
    with pytest.raises(VariantSetError, match="does not match its admission"):
        author_variant_set(
            [
                VariantInput(
                    name="light",
                    bundle=other,
                    admission=envelope,
                    when=CONDITIONS["light"],
                )
            ],
            name="mismatch",
            out=tmp_path / "set",
        )


# ---------------------------------------------------------------------------
# real governed replay against the MockMed system of record
# ---------------------------------------------------------------------------


def test_routed_variant_runs_through_the_replayer_and_its_report_names_it(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path)
    url, db, stop = fault_serve()
    try:

        def child_run(
            capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
        ):
            report = Replayer(
                _RowWritingBackend(url),
                vision=_vision_confirms_saved(),
                effect_verifier=RestRecordVerifier(url),
                poll_interval_s=0.01,
            ).run(
                workflow,
                params=dict(inputs),
                bundle_dir=bundle_dir,
                run_dir=run_dir,
            )
            return ChildRunResult(
                child=child,
                outcome="VERIFIED" if report.success else "HALTED",
                bound_params=dict(inputs),
                effect_facts=dict(inputs),
                model_calls=report.model_calls,
                success=report.success,
                report_path=str(Path(run_dir) / "report.json"),
            )

        report = execute_variant_set(
            variant_set,
            parent_dir=parent,
            run_dir=tmp_path / "run",
            inputs={"payer": "medicare", "patient_id": "alice", "note": "alice triage"},
            first_screen=mockmed_frames["dark"],
            child_run=child_run,
            trusted_signers=_trust(),
            now=NOW,
        )
        assert report.outcome == "VERIFIED", report.detail
        assert report.decision.variant == "dark"
        assert report.child is not None
        assert report.child.model_calls == 0
        assert report.child.report_names_variant
        child_report = RunReport.model_validate_json(
            (tmp_path / "run" / "dark" / "report.json").read_text()
        )
        assert child_report.variant_route is not None
        assert child_report.variant_route.variant == "dark"
        assert child_report.variant_route.admission_id == ADMISSIONS["dark"]
        assert {rec["patient_id"] for rec in db.snapshot()["records"]} == {"alice"}
    finally:
        stop()
    # Outside the routed run, the replayer stamps nothing.
    assert current_variant_route() is None


def test_unrouted_report_serializes_without_a_variant_key(tmp_path: Path) -> None:
    report = RunReport(workflow_name="w", started_at="2026-08-18T18:00:00+00:00")
    assert "variant_route" not in json.loads(report.model_dump_json())


# ---------------------------------------------------------------------------
# receipt
# ---------------------------------------------------------------------------


def _stamp(bundle_digest: str):
    from openadapt_flow.ir import VariantRouteStamp

    return VariantRouteStamp(
        variant_set="encounter-intake",
        variant_set_digest="1" * 64,
        variant="dark",
        variant_digest="2" * 64,
        admission_id=ADMISSIONS["dark"],
        bundle_content_digest=bundle_digest,
        fingerprint_digest="3" * 64,
    )


def test_receipt_names_the_variant_by_digest() -> None:
    receipt = build_receipt(_report(variant_route=_stamp("a" * 64)))
    assert receipt.variant is not None
    assert receipt.variant.variant_digest == "2" * 64
    assert receipt.variant.variant_set_digest == "1" * 64
    assert receipt.variant.admission_id == ADMISSIONS["dark"]
    text = receipt.canonical_json().decode()
    # The variant and set names are operator text; only digests leave.
    assert "encounter-intake" not in text
    assert '"dark"' not in text


def test_unrouted_receipt_keeps_its_exact_bytes() -> None:
    receipt = build_receipt(_report())
    assert receipt.variant is None
    assert "variant" not in json.loads(receipt.canonical_json())
    assert "variant" not in receipt.model_dump(mode="json")


def test_receipt_refuses_a_stamp_for_another_bundle() -> None:
    with pytest.raises(ReceiptError, match="different bundle"):
        build_receipt(_report(variant_route=_stamp("f" * 64)))


# ---------------------------------------------------------------------------
# promotion of a repaired case
# ---------------------------------------------------------------------------


def _healed_bundle(
    tmp_path: Path, *, prior_digest: str, state: str = "approved"
) -> tuple[Path, Path]:
    bundle, envelope = _bundle(tmp_path, "medicare_v2")
    record = bundle / "repair" / "candidate.json"
    record.parent.mkdir()
    record.write_text(
        json.dumps(
            {
                "candidate_id": "cand-0001",
                "state": state,
                "prior_content_digest": prior_digest,
                "proposed_content_digest": live_bundle_content_digest(
                    Workflow.load(bundle), bundle
                ),
            }
        )
    )
    # The record lives beside the admitted bundle; admission covers the bundle.
    workflow = Workflow.load(bundle)
    assert (
        live_bundle_content_digest(workflow, bundle)
        == json.loads(envelope.read_text())["payload"]["bundle_content_digest"]
    )
    return bundle, envelope


def test_repaired_case_is_promoted_to_a_new_admitted_variant(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path, names=("light", "dark"))
    base = variant_set.variant("light")
    bundle, envelope = _healed_bundle(tmp_path, prior_digest=base.bundle_content_digest)
    updated = add_variant(
        parent,
        VariantInput(
            name="medicare_v2",
            bundle=bundle,
            admission=envelope,
            when=VariantWhen(theme="light", inputs={"payer": ["medicare"]}),
            derived_from="light",
        ),
    )
    added = updated.variant("medicare_v2")
    assert added.admission_id == ADMISSIONS["medicare_v2"]
    assert added.promotion is not None
    assert added.promotion.derived_from == "light"
    assert added.promotion.repair_candidate_id == "cand-0001"
    assert VariantSet.load(parent).digest() == updated.digest()
    decision = route_variant(
        updated, inputs={"payer": "medicare"}, first_screen=mockmed_frames["light"]
    )
    assert decision.variant == "medicare_v2"


def test_promotion_refuses_a_candidate_from_another_variant(tmp_path: Path) -> None:
    _variant_set, parent = _author(tmp_path, names=("light", "dark"))
    bundle, envelope = _healed_bundle(tmp_path, prior_digest="9" * 64)
    with pytest.raises(VariantSetError, match="was not derived from variant"):
        add_variant(
            parent,
            VariantInput(
                name="medicare_v2",
                bundle=bundle,
                admission=envelope,
                when=VariantWhen(theme="light", inputs={"payer": ["medicare"]}),
                derived_from="light",
            ),
        )


def test_promotion_refuses_overlapping_conditions(tmp_path: Path) -> None:
    variant_set, parent = _author(tmp_path, names=("light", "dark"))
    base = variant_set.variant("light")
    bundle, envelope = _healed_bundle(tmp_path, prior_digest=base.bundle_content_digest)
    with pytest.raises(VariantSetError, match="can both match"):
        add_variant(
            parent,
            VariantInput(
                name="medicare_v2",
                bundle=bundle,
                admission=envelope,
                when=VariantWhen(theme="light"),
                derived_from="light",
            ),
        )
    assert [spec.name for spec in VariantSet.load(parent).variants] == ["light", "dark"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _png(level: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (level, level, level)).save(buffer, format="PNG")
    return buffer.getvalue()


def _spec_file(tmp_path: Path) -> Path:
    entries = []
    for name in ("light", "dark", "medicare"):
        bundle, envelope = _bundle(tmp_path, name)
        entries.append(
            {
                "name": name,
                "bundle": str(bundle.relative_to(tmp_path)),
                "admission": str(envelope.relative_to(tmp_path)),
                "when": CONDITIONS[name].model_dump(exclude_defaults=True),
            }
        )
    spec = tmp_path / "variants.json"
    spec.write_text(json.dumps({"name": "encounter-intake", "variants": entries}))
    return spec


def test_cli_author_route_and_run(
    tmp_path: Path, mockmed_frames, monkeypatch, capsys
) -> None:
    from openadapt_flow import cli_variants
    from openadapt_flow.__main__ import build_parser, main

    out = tmp_path / "set"
    assert (
        main(
            [
                "variants",
                "author",
                "--spec",
                str(_spec_file(tmp_path)),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    dark = tmp_path / "dark.png"
    dark.write_bytes(mockmed_frames["dark"])
    light = tmp_path / "light.png"
    light.write_bytes(mockmed_frames["light"])

    capsys.readouterr()
    assert (
        main(
            [
                "route",
                str(out),
                "--param",
                "payer=medicare",
                "--first-screen",
                str(light),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["variant"] == "medicare"
    assert (
        main(
            [
                "route",
                str(out),
                "--param",
                "payer=self-pay",
                "--first-screen",
                str(light),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["reason"] == "no_variant_matched"

    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            {key: value.model_dump(mode="json") for key, value in _trust().items()}
        )
    )

    class FrozenClock:
        @staticmethod
        def now(tz=None):
            return NOW

    monkeypatch.setattr(cli_variants, "datetime", FrozenClock)
    seen: list[argparse.Namespace] = []

    def run_child(child_args: argparse.Namespace) -> int:
        seen.append(child_args)
        RunReport(
            workflow_name="w",
            started_at="2026-08-18T18:00:00+00:00",
            execution_outcome="VERIFIED",
            success=True,
            variant_route=current_variant_route(),
        ).save(child_args.run_dir)
        return 0

    args = build_parser().parse_args(
        [
            "run",
            str(out),
            "--run-dir",
            str(tmp_path / "run"),
            "--param",
            "payer=commercial",
            "--param",
            "patient_id=p-1",
            "--first-screen",
            str(dark),
            "--qualification-trust",
            str(trust),
        ]
    )
    assert cli_variants.cmd_run_variants(args, run_child=run_child) == 0
    assert len(seen) == 1
    assert Path(seen[0].bundle).name == "dark"
    assert seen[0].param == ["patient_id=p-1"]
    report = json.loads((tmp_path / "run" / VARIANT_REPORT_FILENAME).read_text())
    assert report["decision"]["variant"] == "dark"
    assert report["child"]["report_names_variant"] is True

    halted = build_parser().parse_args(
        [
            "run",
            str(out),
            "--run-dir",
            str(tmp_path / "halted"),
            "--param",
            "payer=self-pay",
            "--first-screen",
            str(light),
            "--qualification-trust",
            str(trust),
        ]
    )
    assert cli_variants.cmd_run_variants(halted, run_child=run_child) == 2
    assert len(seen) == 1
    assert "no_variant_matched" in capsys.readouterr().out

    dry = build_parser().parse_args(
        [
            "run",
            str(out),
            "--dry-run",
            "--run-dir",
            str(tmp_path / "dry"),
            "--param",
            "payer=medicare",
            "--first-screen",
            str(light),
            "--qualification-trust",
            str(trust),
        ]
    )
    assert cli_variants.cmd_run_variants(dry, run_child=run_child) == 0
    assert len(seen) == 1
    assert "Nothing was executed" in capsys.readouterr().out


def test_replay_refuses_a_variant_set(tmp_path: Path) -> None:
    from openadapt_flow.__main__ import main

    out = tmp_path / "set"
    assert (
        main(
            [
                "variants",
                "author",
                "--spec",
                str(_spec_file(tmp_path)),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    with pytest.raises(SystemExit, match="replay refuses a variant set"):
        main(["replay", str(out)])


def test_cli_add_promotes_a_repaired_bundle(tmp_path: Path) -> None:
    from openadapt_flow.__main__ import main

    variant_set, parent = _author(tmp_path, names=("light", "dark"))
    bundle, envelope = _healed_bundle(
        tmp_path, prior_digest=variant_set.variant("light").bundle_content_digest
    )
    rc = main(
        [
            "variants",
            "add",
            str(parent),
            "--variant",
            f"medicare_v2={bundle}",
            "--admission",
            str(envelope),
            "--when-theme",
            "light",
            "--when-input",
            "payer=medicare",
            "--when-input",
            "payer=medicare-advantage",
            "--derived-from",
            "light",
        ]
    )
    assert rc == 0
    added = VariantSet.load(parent).variant("medicare_v2")
    assert added.when.inputs == {"payer": ["medicare", "medicare-advantage"]}
    assert added.promotion is not None and added.promotion.derived_from == "light"


def test_promotion_requires_repair_evidence(tmp_path: Path) -> None:
    _variant_set, parent = _author(tmp_path, names=("light", "dark"))
    bundle, envelope = _bundle(tmp_path, "medicare_v2")
    item = VariantInput(
        name="medicare_v2",
        bundle=bundle,
        admission=envelope,
        when=VariantWhen(theme="light", inputs={"payer": ["medicare"]}),
        derived_from="light",
    )
    with pytest.raises(VariantSetError, match="lineage is unproven"):
        add_variant(parent, item)


def test_promotion_refuses_a_rejected_candidate(tmp_path: Path) -> None:
    variant_set, parent = _author(tmp_path, names=("light", "dark"))
    bundle, envelope = _healed_bundle(
        tmp_path,
        prior_digest=variant_set.variant("light").bundle_content_digest,
        state="rejected",
    )
    with pytest.raises(VariantSetError, match="rejected"):
        add_variant(
            parent,
            VariantInput(
                name="medicare_v2",
                bundle=bundle,
                admission=envelope,
                when=VariantWhen(theme="light", inputs={"payer": ["medicare"]}),
                derived_from="light",
            ),
        )


def test_variants_on_different_surfaces_are_refused(tmp_path: Path) -> None:
    items = []
    for name, when in (("light", CONDITIONS["light"]), ("dark", CONDITIONS["dark"])):
        bundle, envelope = _bundle(tmp_path, name)
        if name == "dark":
            workflow = Workflow.load(bundle)
            workflow.surface = "citrix"
            workflow.save(bundle)
            envelope = _envelope_for(
                bundle,
                admission_id=ADMISSIONS[name],
                workflow_version_id=VERSIONS[name],
            )
        items.append(
            VariantInput(name=name, bundle=bundle, admission=envelope, when=when)
        )
    with pytest.raises(VariantSetError, match="different surfaces"):
        author_variant_set(items, name="mixed", out=tmp_path / "set")


def test_dry_run_verifies_admission_and_executes_nothing(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    report = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "medicare"},
        frame=mockmed_frames["light"],
        calls=calls,
        dry_run=True,
    )
    assert report.outcome == "DRY_RUN"
    assert report.decision.variant == "medicare"
    assert calls == []
    revoked = _run(
        variant_set,
        parent,
        tmp_path / "again",
        inputs={"payer": "medicare"},
        frame=mockmed_frames["light"],
        calls=calls,
        dry_run=True,
        revoked_admission_ids={ADMISSIONS["medicare"]},
    )
    assert revoked.reason == "variant_admission_refused"


def test_reused_run_directory_halts_instead_of_reading_a_stale_report(
    tmp_path: Path, mockmed_frames
) -> None:
    variant_set, parent = _author(tmp_path)
    calls: list[dict] = []
    first = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "medicare"},
        frame=mockmed_frames["light"],
        calls=calls,
    )
    assert first.outcome == "VERIFIED"
    second = _run(
        variant_set,
        parent,
        tmp_path,
        inputs={"payer": "medicare"},
        frame=mockmed_frames["light"],
        calls=calls,
    )
    assert second.outcome == "HALTED"
    assert second.reason == "run_dir_not_empty"
    assert len(calls) == 1


def test_probe_refusal_becomes_a_named_halt(tmp_path: Path) -> None:
    from openadapt_flow.cli_variants import first_screen_probe

    variant_set, parent = _author(tmp_path)
    args = argparse.Namespace(
        first_screen=None,
        url="http://127.0.0.1:9",
        browser_cdp_endpoint="not-a-loopback-endpoint",
        backend="web",
    )
    decision = route_variant(
        variant_set,
        inputs={"payer": "medicare"},
        first_screen=first_screen_probe(args, variant_set, parent),
    )
    assert decision.reason == "first_screen_unavailable"
