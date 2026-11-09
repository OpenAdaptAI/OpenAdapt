"""An identity halt caused by the app's own window over the band (#1724).

Rule tests for ``self_repair._covered_band_halt`` over a small linear
workflow; the backend is a stand-in whose ``owned_window_over`` says whether
the app's own window covers a region.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.repair.self_repair import _covered_band_halt
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.identity_template import build_identity_template

BAND = (200, 300, 220, 30)
RECORDED = "Patient: Neil Sorenson (1984-03-12)"
#: What the identity check read with the session notice over the band's right.
READ_BESIDE_DIALOG = "Patient: Neil Sorenson (1984-"
TEMPLATE = build_identity_template(RECORDED)


def _halt(report, workflow, backend, recorded=RECORDED):
    return _covered_band_halt(
        report, workflow, backend, recorded_band=lambda step: recorded
    )


def _anchor(click=(100, 100), band=BAND) -> Anchor:
    return Anchor(
        template="templates/t.png",
        region=(click[0] - 20, click[1] - 10, 40, 20),
        click_point=click,
        identity_template=TEMPLATE,
        identifier_region=band,
    )


def _workflow(*, search_saves: bool = False) -> Workflow:
    return Workflow(
        name="notes",
        steps=[
            Step(
                id="search",
                intent="click 'Search'",
                action=ActionKind.CLICK,
                anchor=_anchor((500, 50)),
                risk="irreversible" if search_saves else "reversible",
            ),
            Step(
                id="note",
                intent="click the note field",
                action=ActionKind.CLICK,
                anchor=_anchor((300, 350)),
                risk="reversible",
            ),
            Step(
                id="save",
                intent="click 'Save Note'",
                action=ActionKind.CLICK,
                anchor=_anchor((100, 450)),
                risk="irreversible",
                effects=[Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "1"})],
            ),
        ],
    )


def _structural(point=(300, 350)) -> Resolution:
    return Resolution(rung="structural", point=point, confidence=1.0, elapsed_ms=1)


def _report(
    *,
    identity: str = "mismatch",
    halted_delivered: bool = False,
    search_rung: str = "structural",
    search_ok: bool = True,
    point=(300, 350),
    observed: str = READ_BESIDE_DIALOG,
    search_verified: bool = True,
    outcome: str = "REJECTED_POLICY",
) -> RunReport:
    from openadapt_flow.ir import HaltObservation

    return RunReport(
        workflow_name="notes",
        started_at="2026-11-03T00:00:00Z",
        halt=HaltObservation(state_id="note", reason="Identity check failed"),
        transaction_outcome=outcome,
        results=[
            StepResult(
                step_id="search",
                intent="click 'Search'",
                ok=search_ok,
                delivery_attempted=True,
                identity=IdentityCheck(status="verified", mode="context")
                if search_verified
                else None,
                resolution=Resolution(
                    rung=search_rung, point=(500, 50), confidence=1.0, elapsed_ms=1
                ),
            ),
            StepResult(
                step_id="note",
                intent="click the note field",
                ok=False,
                delivery_attempted=halted_delivered,
                resolution=_structural(point),
                identity=IdentityCheck(
                    status=identity, mode="context", observed=observed, coverage=0.62
                ),
                error="Identity check failed",
            ),
        ],
    )


def _cover_for(observed: str = READ_BESIDE_DIALOG, band=BAND) -> list[int]:
    """A dialog whose left edge is where ``observed`` stops, over the band's
    right part at full height (the shape the session notice has)."""
    from openadapt_flow.runtime.identity import ocr_canonical, squash

    share = len(ocr_canonical(squash(observed))) / len(ocr_canonical(squash(RECORDED)))
    left = band[0] + round(band[2] * share)
    return [left, band[1] - 40, band[0] + band[2] - left + 60, band[3] + 120]


class Backend:
    def __init__(self, covered: Any = True, region: Any = "default") -> None:
        self.covered = covered
        self.region = _cover_for() if region == "default" else region
        self.regions: list[tuple] = []
        self.pinned: list[str] = []

    def owned_window_over(self, region, *, pinned_step_id) -> Optional[dict]:
        self.regions.append(tuple(region))
        self.pinned.append(pinned_step_id)
        if isinstance(self.covered, Exception):
            raise self.covered
        if not self.covered:
            return None
        cover = {"title": "Session notice", "hwnd": 7, "owner_hwnd": 3, "pid": 42}
        if self.region is not None:
            cover["region"] = list(self.region)
        return cover


def test_a_covered_band_is_repaired_after_the_previous_click() -> None:
    backend = Backend()
    found = _halt(_report(), _workflow(), backend)
    assert found is not None
    step, mode, result = found
    assert (step.id, mode, result.step_id) == ("search", "after", "search")
    assert backend.regions == [BAND]
    assert backend.pinned == ["search"]  # the last identity-verified step


def test_no_identity_verified_step_means_no_pinned_window() -> None:
    backend = Backend()
    assert _halt(_report(search_verified=False), _workflow(), backend) is None
    assert backend.regions == []


def test_the_band_moves_with_the_resolved_point() -> None:
    backend = Backend()
    _halt(_report(point=(310, 345)), _workflow(), backend)
    assert backend.regions == [(BAND[0] + 10, BAND[1] - 5, BAND[2], BAND[3])]


@pytest.mark.parametrize(
    "backend, report, workflow",
    [
        (Backend(False), _report(), _workflow()),  # nothing covers the band
        (Backend(RuntimeError("AX")), _report(), _workflow()),  # no proof
        (object(), _report(), _workflow()),  # a backend without the hook
        (Backend(), _report(identity="verified"), _workflow()),
        (Backend(), _report(halted_delivered=True), _workflow()),  # input sent
        (Backend(), _report(search_rung="geometry"), _workflow()),
        (Backend(), _report(search_ok=False), _workflow()),
        (Backend(), _report(), _workflow(search_saves=True)),  # a save took input
    ],
)
def test_anything_else_keeps_its_usual_routing(backend, report, workflow) -> None:
    assert _halt(report, workflow, backend) is None


def test_an_observed_effect_is_never_reclassified() -> None:
    from openadapt_flow.ir import EffectVerificationEvidence

    report = _report()
    report.results[0].effect_evidence = [
        EffectVerificationEvidence.model_construct(observed_effect="present")
    ]
    assert _halt(report, _workflow(), Backend()) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Nell Sorensen (1986-",  # the look-alike sibling behind it
        "Patient: Maria Alvarez (1979-",  # another patient behind it
        "Patient: Neil Sorensen (1983-",  # a one-letter surname and DOB change
        "Patient: Neil Sorenson Maria",  # a name the record can't explain
        "Patient: Neil",  # too little of the band to tell
        "",  # nothing read
    ],
)
def test_another_patient_behind_the_dialog_stays_an_identity_halt(observed) -> None:
    report = _report(observed=observed)
    assert _halt(report, _workflow(), Backend()) is None


def test_no_template_is_doubt() -> None:
    workflow = _workflow()
    workflow.steps[1].anchor.identity_template = None
    assert _halt(_report(), workflow, Backend()) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Neil Sorenson (1991-",  # a different birth year shows
        "Patient: Sorenson",  # the first name is not the record's
        "Patient: Neil Sorenson (1984-03-13)",  # a full, different date
        "Neil Sorenson (1984-",  # does not start where the band starts
    ],
)
def test_every_word_read_must_be_the_records(observed) -> None:
    assert _halt(_report(observed=observed), _workflow(), Backend()) is None


def test_a_same_name_patient_with_a_visible_other_dob_is_refused() -> None:
    from openadapt_flow.repair.self_repair import _uncovered_band_matches

    recorded = "Patient: Christopherson, Maximilian DOB 1/2/80 MRN 4471"
    step = _workflow().steps[1]
    step.anchor.identity_template = build_identity_template(recorded)
    assert not _uncovered_band_matches(
        step, "Patient: Christopherson, Maximilian DOB 3/9/91", recorded
    )
    assert _uncovered_band_matches(
        step, "Patient: Christopherson, Maximilian DOB 1/2/8", recorded
    )


def test_no_recorded_band_is_doubt() -> None:
    assert _halt(_report(), _workflow(), Backend(), recorded=None) is None


def test_a_parameterised_band_is_never_reclassified() -> None:
    workflow = _workflow()
    template = workflow.steps[1].anchor.identity_template
    workflow.steps[1].anchor.identity_template = template.model_copy(
        update={"param_token_indices": {"patient": [2]}}
    )
    assert _halt(_report(), workflow, Backend()) is None


@pytest.mark.parametrize(
    "outcome", ["CANCELED", "RECONCILIATION_REQUIRED", "FAILED_PLATFORM", "ROLLED_BACK"]
)
def test_only_an_identity_refusal_before_effect_qualifies(outcome) -> None:
    assert _halt(_report(outcome=outcome), _workflow(), Backend()) is None


def test_a_structured_identity_mismatch_is_a_real_mismatch() -> None:
    report = _report()
    report.results[1].identity = report.results[1].identity.model_copy(
        update={"mode": "structured"}
    )
    assert _halt(report, _workflow(), Backend()) is None


def test_uncertain_delivery_is_never_reclassified() -> None:
    from openadapt_flow.ir import ActionDeliveryUncertainty

    report = _report()
    report.results[0].delivery_uncertainty = ActionDeliveryUncertainty.model_construct(
        operation="click"
    )
    assert _halt(report, _workflow(), Backend()) is None


def test_the_recorded_band_comes_from_the_crop_and_must_meet_the_template(tmp_path):
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import _recorded_band_text

    workflow = _workflow()
    step = workflow.steps[1]
    step.anchor.identifier_crop = "templates/identifiers/note.png"
    crop = tmp_path / step.anchor.identifier_crop
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"png")

    def vision(text):
        line = SimpleNamespace(text=text, region=(0, 0, 10, 10))
        return SimpleNamespace(ocr=lambda png: [line])

    assert _recorded_band_text(workflow, tmp_path, step, vision(RECORDED)) == RECORDED
    assert (
        _recorded_band_text(workflow, tmp_path, step, vision("Patient: Neil")) is None
    )
    other = vision("Patient: Nell Sorensen (1986-07-09)")
    assert _recorded_band_text(workflow, tmp_path, step, other) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Neil Sorens",  # the dialog hides where Sorenson/Sorensen differ
        "Patient: Neil Soren",
        "Patient: Neil So",
    ],
)
def test_a_dialog_hiding_the_difference_below_the_floor_stays_a_halt(
    observed,
) -> None:
    assert _halt(_report(observed=observed), _workflow(), Backend()) is None


def test_a_hidden_birth_date_is_reclassified_only_for_the_probe_to_recheck() -> None:
    # The whole name shows and the dialog hides the birth date, so a
    # same-name patient could be behind it. Reclassifying only routes the
    # stop to an ``after`` repair: the probe replays this step with its own
    # identity check on the uncovered band, which refuses another birth date.
    observed = "Patient: Neil Sorenson (19"
    found = _halt(
        _report(observed=observed), _workflow(), Backend(region=_cover_for(observed))
    )
    assert found is not None and found[1] == "after"
    assert found[0].id == "search"  # the note step itself is replayed unchanged


# -- the cover must be what cut the band (review of #1735) ---------------------


def test_the_found_halt_carries_the_proved_cover() -> None:
    found = _halt(_report(), _workflow(), Backend())
    assert found.pinned == "search"
    assert found.cover["hwnd"] == 7 and found.cover["region"] == _cover_for()


@pytest.mark.parametrize(
    "region",
    [
        None,  # no rect reported: doubt
        [0, 0, 0, 0],
        "garbage",
        # a tooltip over the band's right end, far right of where the read stops
        [BAND[0] + BAND[2] - 8, BAND[1] - 2, 60, BAND[3] + 4],
        # starts where the read stops but is shorter than the band
        [_cover_for()[0], BAND[1] + 5, 200, BAND[3] - 10],
        # starts where the read stops but ends before the band's right edge
        [_cover_for()[0], BAND[1] - 10, 20, BAND[3] + 20],
        # covers the band from its left edge: nothing could be read beside it
        [BAND[0] - 5, BAND[1] - 10, 400, BAND[3] + 20],
        # its left edge well left of where the read stops
        [BAND[0] + 40, BAND[1] - 10, 400, BAND[3] + 20],
    ],
)
def test_a_window_that_did_not_cut_the_band_is_not_the_cause(region) -> None:
    assert _halt(_report(), _workflow(), Backend(region=region)) is None


def test_a_recorded_band_that_raises_is_doubt() -> None:
    def broken(step):
        raise OSError("crop unreadable")

    assert (
        _covered_band_halt(_report(), _workflow(), Backend(), recorded_band=broken)
        is None
    )


# -- the dismiss click: only in the proved dialog, only on a notice -------------


class _Line:
    def __init__(self, text: str, region: tuple[int, int, int, int]) -> None:
        self.text, self.region, self.confidence = text, region, 1.0


def _guard(
    *, at: Any, message: str, cover: Optional[dict] = None, lines_in: bool = True
):
    """Run ``SelfRepair._dismisses_the_cover`` on a stand-in self."""
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import SelfRepair, _CoveredHalt

    cover = cover or {"hwnd": 7, "pid": 42, "region": [380, 300, 270, 160]}
    found = _CoveredHalt(("s", "after", None), cover=cover, pinned="search")
    lines = [
        _Line("Session notice", (395, 318, 80, 14)),
        _Line(message, (450, 374, 170, 14)),
        _Line("OK", (590, 433, 20, 14)),
        _Line("Patient: Neil Sorenson (1984-", (230, 320, 153, 14)),  # 3px in
    ]
    if not lines_in:
        lines = lines[-1:]
    vision = SimpleNamespace(ocr=lambda png: lines)
    queried: list[tuple] = []

    class Here:
        def owned_window_over(self, region, *, pinned_step_id):
            queried.append((tuple(region), pinned_step_id))
            if isinstance(at, Exception):
                raise at
            return at

    me = SimpleNamespace(_covered=found, vision=vision)
    session = SimpleNamespace(backend=Here())
    SelfRepair._dismisses_the_cover(me, session, (598, 440), b"png")
    return queried


def test_a_notice_in_the_proved_dialog_may_be_dismissed() -> None:
    queried = _guard(
        at={"hwnd": 7, "pid": 42}, message="Your session expires in 5 minutes."
    )
    assert queried == [((598, 440, 1, 1), "search")]


@pytest.mark.parametrize(
    "at",
    [
        None,  # nothing owned at the point
        {"hwnd": 8, "pid": 42},  # another of the app's windows
        {"hwnd": 7, "pid": 43},
        {"title": "x"},  # no id
        RuntimeError("agent"),
    ],
)
def test_a_click_outside_the_proved_dialog_is_refused(at) -> None:
    from openadapt_flow.repair.self_repair import SelfRepairRefused

    with pytest.raises(SelfRepairRefused, match="not in the dialog"):
        _guard(at=at, message="Your session expires in 5 minutes.")


def test_a_cover_without_an_id_never_matches_a_click_without_one() -> None:
    from openadapt_flow.repair.self_repair import SelfRepairRefused

    with pytest.raises(SelfRepairRefused, match="not in the dialog"):
        _guard(
            at={"title": "x"},
            message="Your session expires in 5 minutes.",
            cover={"region": [380, 300, 270, 160]},
        )


@pytest.mark.parametrize(
    "message",
    [
        "Discard unsaved changes?",
        "End the other session and continue?",
        "Overwrite the existing note?",
        "Save changes before closing?",
        "Delete this draft?",
        "Submit the order now?",
        "Your changes will be lost.",
        # what a list of forbidden words would miss (review of this PR)
        "Are you sure you want to leave? Yes No",
        "Log out of the other session? OK",
        "Your changes will be deleted. OK",
        "Quit the application",
        "Stop the running import",
        "Disconnect other user",
        "Proceed anyway",
        "Änderungen verwerfen OK",
        # notice-like words that still act (re-review of this PR)
        "Update the application now. OK",
        "A new version of the application is available. Update now",
        "This session will expire now. OK",
        "Your session has expired. OK",
        "Session notice Your session expires in 5 minutes？ OK",  # fullwidth
        "¿Session notice OK",
        "Session notice; OK",
        # nothing proves a notice: the message line was not read (third review)
        "OK",
        "This is the information. OK",
        "Session notice OK",
    ],
)
def test_a_dialog_whose_message_can_lose_or_commit_data_is_refused(message) -> None:
    from openadapt_flow.repair.self_repair import SelfRepairRefused

    with pytest.raises(SelfRepairRefused, match="covering dialog|session expires"):
        _guard(at={"hwnd": 7, "pid": 42}, message=message)


# -- what protects a hidden difference above the floor: the full recheck -------


@pytest.mark.parametrize(
    "after_dismiss",
    [
        "Patient: Neil Sorenson (1991-07-02)",  # same name, another birth date
        "Patient: Neil Sorensen (1984-03-12)",  # the surname differs past the cut
        "Patient: Neil Sorenson (1984-",  # the dialog is still there
    ],
)
def test_the_probes_full_identity_check_refuses_what_the_dialog_hid(
    after_dismiss,
) -> None:
    # The note step's identity is checked again, unchanged, when the probe
    # replays the repaired bundle. Whatever the dialog hid must match then.
    from openadapt_flow.runtime.identity_template import verify_template_identity

    hidden = "Patient: Neil Sorenson (19"
    assert _halt(
        _report(observed=hidden), _workflow(), Backend(region=_cover_for(hidden))
    )
    step = _workflow().steps[1]
    assert step.anchor.identity_template == TEMPLATE  # not relaxed by the repair
    check = verify_template_identity(TEMPLATE, after_dismiss)
    assert check.status != "verified"
    assert verify_template_identity(TEMPLATE, RECORDED).status == "verified"


def test_a_dialog_with_nothing_readable_in_it_is_refused() -> None:
    from openadapt_flow.repair.self_repair import SelfRepairRefused

    with pytest.raises(SelfRepairRefused, match="can't be read"):
        _guard(at={"hwnd": 7, "pid": 42}, message="", lines_in=False)


@pytest.mark.parametrize(
    "kinds", [["type"], ["click", "click"], ["double_click"], ["click", "type"]]
)
def test_a_covered_band_bridge_is_exactly_one_click(kinds) -> None:
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import (
        SelfRepair,
        SelfRepairRefused,
        _CoveredHalt,
    )

    ops = iter(SimpleNamespace(kind=kind) for kind in kinds)
    me = SimpleNamespace(
        _covered=_CoveredHalt(("s", "after", None), cover={}, pinned="search"),
        max_bridge=4,
        _one_op=lambda native: next(ops),
    )
    proposal = SimpleNamespace(bridge=lambda: [{}] * len(kinds))
    workflow = _workflow()
    with pytest.raises(SelfRepairRefused, match="one click that dismisses"):
        SelfRepair._patch(
            me,
            workflow,
            None,
            workflow.steps[0],
            "after",
            proposal,
            None,
            b"",
            1,
            None,
            None,
            [False],
            None,
        )


@pytest.mark.parametrize(
    "text",
    [
        "Session notice Your session expires in 5 minutes. OK",
        "Session notice × Your session expires in 5 minutes. OK",
        "Session notice x Your session expires in 5 minutes. OK",
        "Session notice i Your session expires in 5 minutes. OK",
    ],
)
def test_only_notice_words_pass_the_message_gate(text) -> None:
    from openadapt_flow.repair.self_repair import _dialog_message_refusal

    assert _dialog_message_refusal(text) is None


def test_new_click_runs_the_cover_guard_before_admitting() -> None:
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import (
        SelfRepair,
        SelfRepairRefused,
        _CoveredHalt,
    )

    calls: list[tuple] = []

    def guard(session, point, screen):
        calls.append(tuple(point))
        raise SelfRepairRefused("guarded")

    me = SimpleNamespace(
        _covered=_CoveredHalt(("s", "after", None), cover={}, pinned="search"),
        vision=SimpleNamespace(ocr=lambda png: [], wait_settled=lambda b: None),
        _dismisses_the_cover=guard,
    )
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    structural = SimpleNamespace(name="OK")
    with pytest.raises(SelfRepairRefused, match="guarded"):
        SelfRepair._new_click(
            me,
            _workflow(),
            "search_bridge_1",
            SimpleNamespace(kind="click"),
            (598, 440),
            structural,
            buf.getvalue(),
            (0, 0, 40, 20),
            SimpleNamespace(backend=object()),
        )
    assert calls == [(598, 440)]


@pytest.mark.parametrize("name", ["Yes", "Continue", "Log out", "Close"])
def test_the_dismiss_click_must_be_ok(name) -> None:
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import (
        SelfRepair,
        SelfRepairRefused,
        _CoveredHalt,
    )

    me = SimpleNamespace(
        _covered=_CoveredHalt(("s", "after", None), cover={}, pinned="search"),
        vision=SimpleNamespace(ocr=lambda png: []),
        _dismisses_the_cover=lambda *a: None,
    )
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    with pytest.raises(SelfRepairRefused, match="only with 'OK'|reads as a save"):
        SelfRepair._new_click(
            me,
            _workflow(),
            "search_bridge_1",
            SimpleNamespace(kind="click"),
            (598, 440),
            SimpleNamespace(name=name),
            buf.getvalue(),
            (0, 0, 40, 20),
            SimpleNamespace(backend=object()),
        )


def test_text_running_across_the_dialog_edge_is_refused() -> None:
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import (
        SelfRepair,
        SelfRepairRefused,
        _CoveredHalt,
    )

    cover = {"hwnd": 7, "pid": 42, "region": [380, 300, 270, 160]}
    lines = [
        _Line("Session notice", (395, 318, 80, 14)),
        # merged with text outside: its center is left of the dialog
        _Line("Patient: Neil Sorenson Delete this record", (200, 374, 300, 14)),
        _Line("Your session expires in 5 minutes.", (450, 400, 170, 14)),
        _Line("OK", (590, 433, 20, 14)),
    ]
    me = SimpleNamespace(
        _covered=_CoveredHalt(("s", "after", None), cover=cover, pinned="search"),
        vision=SimpleNamespace(ocr=lambda png: lines),
    )

    class Here:
        def owned_window_over(self, region, *, pinned_step_id):
            return {"hwnd": 7, "pid": 42}

    with pytest.raises(SelfRepairRefused, match="across the covering dialog"):
        SelfRepair._dismisses_the_cover(
            me, SimpleNamespace(backend=Here()), (598, 440), b"png"
        )


def test_band_text_that_ends_at_the_dialog_edge_is_not_its_message() -> None:
    # The cut band line ends where the dialog starts (within a few pixels).
    queried = _guard(
        at={"hwnd": 7, "pid": 42}, message="Your session expires in 5 minutes."
    )
    assert queried


@pytest.mark.parametrize(
    "text",
    [
        "Sessionnotice Yoursession expires in5minutes. OK",
        "Session notice Yoursessionexpiresin5minutes. OK ×",
    ],
)
def test_words_ocr_runs_together_still_pass(text) -> None:
    from openadapt_flow.repair.self_repair import _dialog_message_refusal

    assert _dialog_message_refusal(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Yoursession expires. Deleteall OK",
        "Session notice Your session expires axdelete OK",
        "Session notice Your session expiresnow OK",
    ],
)
def test_a_run_together_token_must_split_into_notice_words(text) -> None:
    from openadapt_flow.repair.self_repair import _dialog_message_refusal

    assert _dialog_message_refusal(text) is not None


def test_the_real_ocr_reads_a_session_notice_as_dismissable() -> None:
    # The run's own OCR on a drawn "Session notice" dialog (the lab app's
    # wording), so OCR quirks such as run-together words are covered.
    pytest.importorskip("rapidocr_onnxruntime")
    import io

    from PIL import Image, ImageDraw, ImageFont

    from openadapt_flow.repair.self_repair import _dialog_message_refusal
    from openadapt_flow.vision import ocr

    img = Image.new("RGB", (520, 300), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        try:
            font = ImageFont.truetype("Arial.ttf", 18)
        except OSError:
            pytest.skip("no TrueType font to draw the dialog")
    draw.text((20, 20), "Session notice", fill="black", font=font)
    draw.text((480, 20), "×", fill="black", font=font)
    draw.text((70, 110), "Your session expires in 5 minutes.", fill="black", font=font)
    draw.rectangle((380, 230, 490, 270), outline="black")
    draw.text((420, 238), "OK", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    text = " ".join(line.text for line in ocr(buf.getvalue()))
    assert _dialog_message_refusal(text) is None, text


def test_the_dialog_band_uses_the_proved_window_before_a_pixel_search() -> None:
    import io
    from types import SimpleNamespace

    from PIL import Image

    from openadapt_flow.repair.self_repair import SelfRepairRefused, _dialog_band

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")  # no drawn box
    lines = [_Line("Your session expires in 5 minutes.", (450, 374, 170, 14))]
    vision = SimpleNamespace(ocr=lambda png: lines)
    admission = SimpleNamespace(element=None)
    with pytest.raises(SelfRepairRefused, match="no dialog was found"):
        _dialog_band(_workflow(), admission, buf.getvalue(), (598, 440), vision)
    template, region, crop = _dialog_band(
        _workflow(),
        admission,
        buf.getvalue(),
        (598, 440),
        vision,
        proved_region=(380, 300, 270, 160),
    )
    assert region == (380, 300, 270, 160) and template is not None and crop
    with pytest.raises(SelfRepairRefused, match="outside the dialog"):
        _dialog_band(
            _workflow(),
            admission,
            buf.getvalue(),
            (100, 100),
            vision,
            proved_region=(380, 300, 270, 160),
        )


def test_a_dialog_that_moved_since_the_halt_is_refused() -> None:
    from openadapt_flow.repair.self_repair import SelfRepairRefused

    cover = {"hwnd": 7, "pid": 42, "region": [380, 300, 270, 160]}
    with pytest.raises(SelfRepairRefused, match="moved"):
        _guard(
            at={"hwnd": 7, "pid": 42, "region": [300, 300, 270, 160]},
            message="Your session expires in 5 minutes.",
            cover=cover,
        )
    assert _guard(
        at={"hwnd": 7, "pid": 42, "region": [380, 300, 270, 160]},
        message="Your session expires in 5 minutes.",
        cover=cover,
    )
