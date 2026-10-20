"""Named sections of a compiled program (openadapt_flow.sections).

Covers the real OpenEMR referral bundle (with and without the recorded form
structure), a form with no landmarks (fallback titles), the save section's
isolation, review edits, the bundle file and its place outside both digests,
the program-graph spec, and the compiler writing sections.json.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow import sections as S
from openadapt_flow.bundle_validation import compute_content_digest
from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.loop_authoring import author_data_driven_loop
from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator, Workflow
from openadapt_flow.qualification import workflow_contract_sha256
from openadapt_flow.visualize import (
    PresentationProfile,
    build_program_graph,
    project_program_graph,
)

_FLOW = Path(__file__).resolve().parent.parent
REFERRAL = _FLOW / "registry" / "bundles" / "openemr" / "new-referral"

#: What OpenEMR's new-patient form shows around each field, as the recorder's
#: section capture read it from the live local test copy (accordion headers).
_OPENEMR_FORM = {
    "#form_title": "Who",
    "#form_fname": "Who",
    "#form_lname": "Who",
    "#form_DOB": "Who",
    "#form_sex": "Who",
    "#form_street": "Contact",
    "#form_city": "Contact",
    "#form_state": "Contact",
    "#form_postal_code": "Contact",
    "#form_phone_home": "Contact",
    "#form_email": "Contact",
    "#form_country_code": "Contact",
}


def _span(section: S.Section) -> tuple[str, str]:
    return section.steps[0], section.steps[-1]


# -- helpers to build programs -------------------------------------------------


def _anchor(selector=None, role=None, name=None, ocr=None) -> Anchor:
    return Anchor(
        template="templates/x.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text=ocr,
        structural=StructuralLocator(selector=selector, role=role, name=name)
        if (selector or role or name)
        else None,
    )


def _click(i, selector=None, role="textbox", name=None, ocr=None, **kw) -> Step:
    return Step(
        id=f"step_{i:03d}",
        intent="click",
        action=ActionKind.CLICK,
        anchor=_anchor(selector, role, name, ocr),
        **kw,
    )


def _type(i, param=None, text=None, field_label=None) -> Step:
    return Step(
        id=f"step_{i:03d}",
        intent="type",
        action=ActionKind.TYPE,
        param=param,
        text=None if param else (text or "x"),
        field_label=field_label,
    )


def _key(i, key="Enter") -> Step:
    return Step(id=f"step_{i:03d}", intent="key", action=ActionKind.KEY, key=key)


def _scroll(i) -> Step:
    return Step(
        id=f"step_{i:03d}", intent="scroll", action=ActionKind.SCROLL, scroll_dy=400
    )


def _workflow(steps: list[Step], params: dict[str, str] | None = None) -> Workflow:
    return Workflow(name="t", steps=steps, params=params or {})


def _form(fields: list[tuple[str, str]], *, save_name="Save", start=0) -> list[Step]:
    """click + type per (selector, param), then an irreversible save click."""
    steps: list[Step] = []
    i = start
    for selector, param in fields:
        steps.append(_click(i, selector))
        steps.append(_type(i + 1, param=param))
        i += 2
    steps.append(
        _click(i, "#save", role="button", name=save_name, risk="irreversible")
    )
    return steps


# -- OpenEMR referral -----------------------------------------------------------


def test_openemr_referral_matches_the_five_form_sections() -> None:
    """The registry referral bundle: the same five groups the site's fixture
    showed from a hand-written table, now compiled from the bundle alone."""
    workflow = Workflow.load(REFERRAL)
    sections = S.compute_sections(workflow)
    assert [(_span(s), s.kind, s.title) for s in sections] == [
        (("step_000", "step_008"), "fields", "3 fields: title, first name, last name"),
        (("step_009", "step_014"), "fields", "2 fields: date of birth, sex"),
        (
            ("step_015", "step_028"),
            "fields",
            "Contact: street, city, state, postal code",
        ),
        (("step_029", "step_037"), "fields", "Contact: home phone, email, country code"),
        (("step_038", "step_040"), "save", "Create New Patient"),
    ]
    assert [s.params for s in sections[:4]] == [
        ["title", "fname", "lname"],
        ["DOB", "sex"],
        ["street", "city", "state_label", "postal_code"],
        ["phone_home", "email", "country_code"],
    ]
    assert S.validate_sections(workflow, sections) == []


def test_openemr_referral_titles_from_the_recorded_form_structure() -> None:
    workflow = Workflow.load(REFERRAL)
    events = []
    for step in workflow.steps:
        selector = step.anchor.structural.selector if step.anchor and step.anchor.structural else None
        index = int(step.id.split("_")[1])
        event = {"i": index, "kind": step.action.value}
        if step.anchor and step.anchor.structural:
            event["structural"] = step.anchor.structural.model_dump(exclude_none=True)
        if selector in _OPENEMR_FORM:
            event["section"] = {"kind": "accordion", "text": _OPENEMR_FORM[selector]}
        if step.anchor and step.anchor.structural and step.anchor.structural.name == "Contact":
            event["section"] = {"kind": "accordion", "text": "Contact", "header": True}
        events.append(event)
    context = S.context_for_workflow(events, workflow)
    titles = [s.title for s in S.compute_sections(workflow, context=context)]
    assert titles == [
        "Who: title, first name, last name",
        "Who: date of birth, sex",
        "Contact: street, city, state, postal code",
        "Contact: home phone, email, country code",
        "Create New Patient",
    ]


def test_context_only_transfers_to_the_same_target() -> None:
    workflow = Workflow.load(REFERRAL)
    events = [
        {
            "i": 3,
            "structural": {"selector": "#form_fname"},
            "section": {"kind": "legend", "text": "Who"},
        },
        {
            "i": 6,
            "structural": {"selector": "#something_else"},
            "section": {"kind": "legend", "text": "Elsewhere"},
        },
    ]
    assert set(S.context_for_workflow(events, workflow)) == {"step_003"}


# -- openIMIS -------------------------------------------------------------------


def _openimis_claim() -> list[Step]:
    """The openIMIS health-facility claim demonstration
    (scripts/openimis_claims_demo.py): the insuree number typed into the
    focused field, the claim number, a diagnosis picked from a search list,
    the explanation, a scroll to the Services grid, a service picked the same
    way, and the icon-only Save button."""
    return [
        _type(0, param="insurance_no", field_label="Insurance No."),
        _click(1, "#claim-no"),
        _type(2, param="claim_no", field_label="Claim No."),
        _click(3, None, role="combobox", name="Search Diagnosis"),
        _type(4, text="A00", field_label="Main Diagnosis"),
        _click(5, None, role="option", name="A00 Cholera"),
        _click(6, "#explanation"),
        _type(7, param="explanation", field_label="Explanation"),
        _scroll(8),
        _click(9, None, role="combobox", name="Search Service"),
        _type(10, text="A1", field_label="Service"),
        _click(11, None, role="option", name="A1 General consultation"),
        _click(12, None, role="button", risk="irreversible"),
    ]


def test_openimis_claim_sections() -> None:
    workflow = _workflow(_openimis_claim())
    context = {
        "step_009": S.StepContext(container="Services", kind="heading"),
        "step_001": S.StepContext(container="Claim", kind="heading"),
        "step_003": S.StepContext(container="Claim", kind="heading"),
        "step_006": S.StepContext(container="Claim", kind="heading"),
    }
    sections = S.compute_sections(workflow, context=context)
    assert [(_span(s), s.kind, s.title) for s in sections] == [
        (("step_000", "step_000"), "fields", "1 field: insurance number"),
        (("step_001", "step_007"), "fields", "Claim"),
        (("step_008", "step_011"), "fields", "Services"),
        (("step_012", "step_012"), "save", "Save"),
    ]
    # The picked option stays with the search it answers.
    assert "step_005" in sections[1].steps
    assert S.validate_sections(workflow, sections) == []


def test_openimis_claim_without_recorded_structure() -> None:
    workflow = _workflow(_openimis_claim())
    sections = S.compute_sections(workflow)
    assert [(_span(s), s.title) for s in sections] == [
        (
            ("step_000", "step_011"),
            "5 fields: insurance number, claim number, main diagnosis, "
            "explanation, service",
        ),
        (("step_012", "step_012"), "Save"),
    ]


# -- a form with no landmarks ------------------------------------------------------


def test_form_without_landmarks_gets_field_titles() -> None:
    steps = _form([("#a", "title"), ("#b", "fname"), ("#c", "lname")])
    sections = S.compute_sections(_workflow(steps))
    assert [s.title for s in sections] == [
        "3 fields: title, first name, last name",
        "Save",
    ]
    assert [s.title_source for s in sections] == ["fields", "ui"]


def test_long_form_without_landmarks_splits_by_kind_of_field() -> None:
    steps = _form(
        [
            ("#f", "first_name"),
            ("#l", "last_name"),
            ("#s", "street"),
            ("#c", "city"),
            ("#z", "zip"),
            ("#p", "phone_mobile"),
        ]
    )
    titles = [s.title for s in S.compute_sections(_workflow(steps))]
    assert titles == [
        "2 fields: first name, last name",
        "4 fields: street, city, zip code, mobile phone",
        "Save",
    ]


def test_unknown_fields_stay_together() -> None:
    steps = _form([(f"#f{i}", f"amount_{i}") for i in range(6)])
    sections = S.compute_sections(_workflow(steps))
    assert len(sections) == 2 and sections[0].title.startswith("6 fields: amount 0")


def test_a_title_never_carries_a_recorded_value() -> None:
    steps = _form([("#a", "fname"), ("#b", "lname")])
    context = {"step_000": S.StepContext(container="Patient: Rosa Delgado", kind="heading")}
    workflow = _workflow(steps, params={"fname": "Rosa", "lname": "Delgado"})
    titles = [s.title for s in S.compute_sections(workflow, context=context)]
    assert titles[0] == "2 fields: first name, last name"
    context = {"step_000": S.StepContext(container="Visit 2026-10-19", kind="heading")}
    assert S.compute_sections(workflow, context=context)[0].title_source == "fields"


def test_narration_titles_an_untitled_section() -> None:
    steps = _form([("#a", "fname"), ("#b", "lname")])
    sections = S.compute_sections(
        _workflow(steps), narration={"step_000": ["Enter the patient's name"]}
    )
    assert (sections[0].title, sections[0].title_source) == (
        "Enter the patient's name",
        "narration",
    )


# -- the save section -----------------------------------------------------------------


@pytest.mark.parametrize(
    "steps",
    [
        # click Save, then a confirm dialog that carries the effects
        [
            *_form([("#a", "a"), ("#b", "b")])[:-1],
            _scroll(4),
            _click(5, "#create", role="button", name="Create", risk="irreversible"),
            _click(6, "#confirm", role="button", name="Confirm"),
        ],
        # Enter submits the form right after the last value
        [_click(0, "#a"), _type(1, param="a"), _key(2, "Enter")],
        # two saves, with fields between and steps after
        [
            *_form([("#a", "a")]),
            _click(3, "#b"),
            _type(4, param="b"),
            _click(5, "#save2", role="button", name="Save again", risk="irreversible"),
            _click(6, "#home", role="link", name="Home"),
        ],
        # a write with no field before it
        [_click(0, "#approve", role="button", name="Approve", risk="irreversible")],
    ],
)
def test_the_save_section_is_always_separate(steps) -> None:
    if steps[-1].id == "step_002" and steps[-1].action is ActionKind.KEY:
        steps[-1].risk = "irreversible"
    workflow = _workflow(steps)
    sections = S.compute_sections(workflow)
    assert S.validate_sections(workflow, sections) == []
    by_id = {s.id: s for s in workflow.steps}
    writes = {s.id for s in workflow.steps if S.is_write(s)}
    for section in sections:
        members = [by_id[i] for i in section.steps]
        if section.kind == "save":
            assert not any(S.is_input(m) for m in members)
        else:
            assert not writes.intersection(section.steps)
    assert writes <= {i for s in sections if s.kind == "save" for i in s.steps}


def test_confirm_dialog_steps_join_the_save() -> None:
    workflow = Workflow.load(REFERRAL)
    save = S.compute_sections(workflow)[-1]
    assert save.steps == ["step_038", "step_039", "step_040"]


def test_review_cannot_break_the_save_section() -> None:
    workflow = Workflow.load(REFERRAL)
    sections = S.compute_sections(workflow)
    with pytest.raises(S.SectionsError, match="save section stays separate"):
        S.apply_edit(
            workflow,
            sections,
            S.SectionEdit(op="merge", section="save-step_038", other="fields-step_029"),
        )
    with pytest.raises(S.SectionsError, match="not split"):
        S.apply_edit(
            workflow,
            sections,
            S.SectionEdit(op="split", section="save-step_038", at="step_039"),
        )
    moved = [s.model_copy() for s in sections]
    moved[-2] = moved[-2].model_copy(update={"steps": [*moved[-2].steps, "step_038", "step_039"]})
    moved[-1] = moved[-1].model_copy(update={"steps": ["step_040"]})
    assert any("write outside a save section" in p for p in S.validate_sections(workflow, moved))


# -- review edits ------------------------------------------------------------------------


def test_rename_merge_and_split() -> None:
    workflow = Workflow.load(REFERRAL)
    sections = S.compute_sections(workflow)
    sections, _ = S.apply_edits(
        workflow,
        sections,
        [
            S.SectionEdit(op="rename", section="fields-step_000", title="  Name "),
            S.SectionEdit(op="merge", section="fields-step_000", other="fields-step_009"),
            S.SectionEdit(op="split", section="fields-step_015", at="step_020"),
        ],
    )
    assert [(s.id, s.title, s.title_source) for s in sections] == [
        ("fields-step_000", "Name", "person"),
        ("fields-step_015", "Contact: street", "ui"),
        ("fields-step_020", "Contact: city, state, postal code", "ui"),
        ("fields-step_029", "Contact: home phone, email, country code", "ui"),
        ("save-step_038", "Create New Patient", "ui"),
    ]
    assert S.validate_sections(workflow, sections) == []


def test_only_adjacent_sections_merge() -> None:
    workflow = Workflow.load(REFERRAL)
    sections = S.compute_sections(workflow)
    with pytest.raises(S.SectionsError, match="adjacent"):
        S.apply_edit(
            workflow,
            sections,
            S.SectionEdit(op="merge", section="fields-step_000", other="fields-step_015"),
        )


# -- the bundle file ----------------------------------------------------------------------


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    out = tmp_path / "bundle"
    shutil.copytree(REFERRAL, out)
    return out


def test_sections_are_outside_both_digests(bundle: Path) -> None:
    workflow = Workflow.load(bundle)
    digest = workflow.manifest.content_digest
    contract = workflow_contract_sha256(workflow)
    S.write(bundle, S.build(workflow))
    S.edit(bundle, workflow, [S.SectionEdit(op="rename", section="save-step_038", title="Create the chart")])
    again = Workflow.load(bundle)  # verifies integrity
    assert again.manifest.content_digest == digest
    assert compute_content_digest(again, again.manifest.file_hashes) == digest
    assert workflow_contract_sha256(again) == contract
    stored = json.loads((bundle / S.SECTIONS_FILE).read_text())
    assert stored["schema"] == S.SECTIONS_SCHEMA
    assert stored["workflow_contract_sha256"] == contract
    assert stored["sections"][-1]["title"] == "Create the chart"
    assert stored["compiled"][-1]["title"] == "Create New Patient"
    assert stored["edits"] == [
        {"op": "rename", "section": "save-step_038", "title": "Create the chart", "other": None, "at": None}
    ]


def test_execution_ignores_sections() -> None:
    """Only display code reads sections: nothing that runs a program does."""
    import openadapt_flow

    root = Path(openadapt_flow.__file__).parent
    readers = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.name != "sections.py"
        and re.search(
            r"^\s*(from openadapt_flow import sections\b|from openadapt_flow\.sections import"
            r"|import openadapt_flow\.sections)",
            p.read_text(encoding="utf-8"),
            re.M,
        )
    )
    assert readers == [
        "__main__.py",
        "cli_sections.py",
        "compiler/compile.py",
        "visualize/builder.py",
    ]


def test_load_recomputes_a_stale_file_and_keeps_edits(bundle: Path) -> None:
    workflow = Workflow.load(bundle)
    S.write(bundle, S.build(workflow))
    S.edit(bundle, workflow, [S.SectionEdit(op="rename", section="fields-step_000", title="Name")])
    stored = json.loads((bundle / S.SECTIONS_FILE).read_text())
    stored["workflow_contract_sha256"] = "0" * 64
    (bundle / S.SECTIONS_FILE).write_text(json.dumps(stored))
    doc = S.load(bundle, workflow)
    assert doc.workflow_contract_sha256 == workflow_contract_sha256(workflow)
    assert doc.sections[0].title == "Name"


def test_a_tampered_file_that_breaks_a_rule_is_not_shown(bundle: Path) -> None:
    workflow = Workflow.load(bundle)
    doc = S.build(workflow)
    broken = doc.model_copy(update={"sections": doc.sections[:-1]})
    S.write(bundle, broken)
    assert S.load(bundle, workflow).sections == doc.sections


def test_missing_file_is_computed(tmp_path: Path) -> None:
    workflow = Workflow.load(REFERRAL)
    assert [s.title for s in S.load(tmp_path, workflow).sections][-1] == "Create New Patient"


# -- programs and the graph spec --------------------------------------------------------------


def test_loop_body_sections_and_graph_spec() -> None:
    body = Workflow.load(REFERRAL)
    looped = author_data_driven_loop(
        body,
        [{p: "x" for p in body.params}],
        relation="referrals",
        loop_var="referral",
        name="loop",
    )
    sections = S.compute_sections(looped)
    assert [s.steps for s in sections] == [s.steps for s in S.compute_sections(body)]
    spec = build_program_graph(looped, sections)
    nodes = {n.id for n in spec.nodes}
    assert len(spec.sections) == 5
    for section in spec.sections:
        assert section.id not in nodes
        assert set(section.nodes) <= nodes
        assert all(node.endswith(step) for node, step in zip(section.nodes, section.steps))
    save = spec.sections[-1]
    assert save.kind.value == "save"
    assert save.subtitle == "3 steps · reads the record back"
    public = project_program_graph(spec, PresentationProfile.PUBLIC_SYNTHETIC)
    assert [s.title for s in public.sections] == [
        "Fill declared inputs",
        "Fill declared inputs",
        "Fill declared inputs",
        "Fill declared inputs",
        "Commit the write",
    ]
    assert all(s.fields == [] for s in public.sections)
    assert "Contact" not in public.model_dump_json()


def test_for_each_carries_the_body_sections(bundle: Path, tmp_path: Path) -> None:
    import subprocess
    import sys

    workflow = Workflow.load(bundle)
    S.edit(bundle, workflow, [S.SectionEdit(op="rename", section="fields-step_000", title="Name")])
    records = tmp_path / "records.csv"
    records.write_text(
        ",".join(workflow.params) + "\n" + ",".join("x" for _ in workflow.params) + "\n"
    )
    out = tmp_path / "loop"
    subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "for-each", str(bundle), "--records", str(records), "--out", str(out)],
        check=True,
        cwd=_FLOW,
        capture_output=True,
    )
    looped = Workflow.load(out)
    doc = S.load(out, looped)
    assert doc.workflow_contract_sha256 == workflow_contract_sha256(looped)
    assert [s.title for s in doc.sections][:2] == ["Name", "2 fields: date of birth, sex"]


def test_visualize_json_carries_the_bundle_sections(bundle: Path, tmp_path: Path) -> None:
    import subprocess
    import sys

    workflow = Workflow.load(bundle)
    S.edit(bundle, workflow, [S.SectionEdit(op="rename", section="save-step_038", title="Create the chart")])
    out = tmp_path / "graph.json"
    subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "visualize", str(bundle), "--format", "json", "-o", str(out)],
        check=True,
        cwd=_FLOW,
        capture_output=True,
    )
    spec = json.loads(out.read_text())
    assert [s["title"] for s in spec["sections"]][-1] == "Create the chart"


def test_sections_cli(bundle: Path) -> None:
    import subprocess
    import sys

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "openadapt_flow", "sections", str(bundle), *args],
            cwd=_FLOW,
            capture_output=True,
            text=True,
        )

    shown = run()
    assert shown.returncode == 0 and "Create New Patient" in shown.stdout
    assert not (bundle / S.SECTIONS_FILE).exists()  # showing writes nothing
    assert run("--rename", "fields-step_000", "Name").returncode == 0
    refused = run("--merge", "save-step_038", "fields-step_029")
    assert refused.returncode != 0 and "REFUSED" in refused.stderr
    assert json.loads(run("--json").stdout)["sections"][0]["title"] == "Name"
    assert run("--reset").returncode == 0
    assert json.loads((bundle / S.SECTIONS_FILE).read_text())["edits"] == []


# -- the compiler -------------------------------------------------------------------------------


def _frame() -> np.ndarray:
    img = np.full((800, 1280, 3), 245, dtype=np.uint8)
    for y, label in ((200, "First name"), (300, "Last name"), (500, "Save")):
        cv2.putText(img, label, (100, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        cv2.rectangle(img, (300, y - 30), (700, y + 10), (90, 90, 90), 2)
    return img


def test_compile_writes_sections_from_the_recorded_form(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    (recording / "frames").mkdir(parents=True)
    events = [
        {"i": 0, "kind": "click", "x": 500, "y": 190, "t": 1.0,
         "structural": {"selector": "#first", "role": "textbox"},
         "section": {"kind": "legend", "text": "Patient"}},
        {"i": 1, "kind": "type", "text": "Ada", "param": "first", "field_label": "First name", "t": 2.0},
        {"i": 2, "kind": "click", "x": 500, "y": 290, "t": 3.0,
         "structural": {"selector": "#last", "role": "textbox"},
         "section": {"kind": "legend", "text": "Patient"}},
        {"i": 3, "kind": "type", "text": "Lovelace", "param": "last", "field_label": "Last name", "t": 4.0},
        {"i": 4, "kind": "click", "x": 500, "y": 490, "t": 5.0,
         "structural": {"selector": "#save", "role": "button", "name": "Save patient"}},
    ]
    ok, buf = cv2.imencode(".png", _frame())
    assert ok
    for event in events:
        for suffix in ("before", "after"):
            (recording / "frames" / f"{event['i']:04d}_{suffix}.png").write_bytes(buf.tobytes())
    (recording / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-sections-001",
                "created_at": "2026-10-19T00:00:00+00:00",
                "viewport": [1280, 800],
                "app_url": "http://localhost:0/",
                "params": {"first": "Ada", "last": "Lovelace"},
            }
        )
    )
    out = tmp_path / "bundle"
    workflow = compile_recording(
        recording, out, name="sections", risk_overrides={"step_004": "irreversible"}
    )
    stored = json.loads((out / S.SECTIONS_FILE).read_text())
    assert [(s["title"], s["kind"]) for s in stored["sections"]] == [
        ("Patient", "fields"),
        ("Save patient", "save"),
    ]
    assert stored["context"]["step_000"] == {"container": "Patient", "kind": "legend", "header": False}
    assert stored["workflow_contract_sha256"] == workflow_contract_sha256(workflow)
    # The context is display metadata only: it never reaches the program.
    assert "Patient" not in (out / "workflow.json").read_text().replace("Save patient", "")
