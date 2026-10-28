"""Provenance: each written value names its source, and the record matches it.

See ``docs/VERIFICATION_COVERAGE.md`` section 3. The referral is made up.
"""

from __future__ import annotations

from openadapt_flow.intake.document import IntakeDocument
from openadapt_flow.intake.extract import extract_fields
from openadapt_flow.ir import ParamKind, ParamSpec
from openadapt_flow.provenance import (
    CallableExtractor,
    LabelExtractor,
    ParamProvenance,
    ProvenanceFile,
    SourceRef,
    cross_check,
    from_intake,
    method_of,
    round_trip,
    sha256_bytes,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

REFERRAL = IntakeDocument(
    "referral.txt",
    "text",
    [
        "Referral to: Example Family Clinic",
        "Last name: Okafor",
        "Date of birth: 03/27/1991",
        "Postal code: 02139",
        "Home phone: 555-0142",
    ],
)
DOC = sha256_bytes(REFERRAL.text.encode("utf-8"))
SPECS = {
    "lname": ParamSpec(name="lname", type=ParamKind.STRING),
    "DOB": ParamSpec(name="DOB", type=ParamKind.DATE),
    "postal_code": ParamSpec(name="postal_code", type=ParamKind.STRING),
    "phone_home": ParamSpec(name="phone_home", type=ParamKind.STRING),
}
ALIASES = {
    "lname": ["last name"],
    "DOB": ["date of birth"],
    "phone_home": ["home phone"],
}
LABELS = LabelExtractor(SPECS, ALIASES)


def _prov(**values: str) -> ProvenanceFile:
    lines = {"lname": 2, "DOB": 3, "postal_code": 4, "phone_home": 5}
    return ProvenanceFile(
        params={
            name: ParamProvenance(
                value=value,
                source=SourceRef(kind="document", sha256=DOC, line=lines[name]),
                extractor="model:first-reader",
            )
            for name, value in values.items()
        }
    )


GOOD = {
    "lname": "Okafor",
    "DOB": "1991-03-27",
    "postal_code": "02139",
    "phone_home": "555-0142",
}


def test_intake_keeps_the_line_it_read() -> None:
    result = extract_fields(REFERRAL, SPECS, aliases=ALIASES)
    prov = from_intake(result, DOC)
    assert prov.params["postal_code"].source.line == 4
    assert prov.params["postal_code"].extractor == "intake:local"
    assert prov.params["DOB"].value == "1991-03-27"


def test_two_methods_that_agree_let_the_save_go() -> None:
    check = cross_check(_prov(**GOOD), {DOC: REFERRAL}, LABELS)
    assert check.status == "agree"
    assert check.disagreements == []


def test_a_misread_value_holds_the_save() -> None:
    misread = {**GOOD, "postal_code": "02193"}
    check = cross_check(_prov(**misread), {DOC: REFERRAL}, LABELS)
    assert check.status == "hold"
    assert check.disagreements == ["postal_code"]
    # The summary carries names and counts, never the values.
    assert "02193" not in str(check.summary())


def test_the_same_method_twice_is_not_a_second_extraction() -> None:
    result = extract_fields(REFERRAL, SPECS, aliases=ALIASES)
    check = cross_check(from_intake(result, DOC), {DOC: REFERRAL}, LABELS)
    assert check.status == "hold"
    assert {f.result for f in check.fields} == {"not_independent"}
    assert method_of("intake:local") != method_of("model:second-reader")


def test_a_missing_document_holds() -> None:
    assert cross_check(_prov(**GOOD), {}, LABELS).status == "hold"


def test_instruction_inputs_need_no_second_extraction() -> None:
    prov = ProvenanceFile(
        params={
            "lname": ParamProvenance(
                value="Okafor", source=SourceRef(kind="instruction"), extractor="person"
            )
        }
    )
    assert cross_check(prov, {}, LABELS).status == "instruction"


def _effects() -> list[Effect]:
    match = {"lname": {"param": "lname"}}
    return [
        Effect(kind=EffectKind.RECORD_WRITTEN, match=match),
        *(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=match,
                field=name,
                value={"param": name},
            )
            for name in ("DOB", "postal_code", "phone_home")
        ),
    ]


def test_round_trip_holds_for_a_faithful_record() -> None:
    from openadapt_flow.provenance import bound_fields

    record = {
        "id": 7,
        "lname": "Okafor",
        "DOB": "1991-03-27",
        "postal_code": "02139",
        "phone_home": "555-0142",
    }
    result = round_trip(
        _prov(**GOOD), record, bound_fields(_effects()), {DOC: REFERRAL}, LABELS
    )
    assert result.status == "held", result.reason


def test_round_trip_refutes_a_correctly_saved_wrong_entry() -> None:
    """The params said 02193, the store holds 02193, the referral says 02139."""

    from openadapt_flow.provenance import bound_fields

    misread = {**GOOD, "postal_code": "02193"}
    record = {**GOOD, "id": 7, "postal_code": "02193"}
    result = round_trip(
        _prov(**misread), record, bound_fields(_effects()), {DOC: REFERRAL}, LABELS
    )
    assert result.status == "refuted"
    assert result.summary()["mismatched_fields"] == ["postal_code"]


def test_round_trip_without_a_record_is_not_checked() -> None:
    result = round_trip(
        _prov(**GOOD), None, {"lname": "lname"}, {DOC: REFERRAL}, LABELS
    )
    assert result.status == "not_checked"


def test_a_caller_supplies_another_method() -> None:
    def ocr_like(document, name, ref):
        return document.lines[ref.line - 1].split(":", 1)[1].strip()

    check = cross_check(
        _prov(postal_code="02139"), {DOC: REFERRAL}, CallableExtractor("ocr", ocr_like)
    )
    assert check.status == "agree"


# -- verify-run and the guarded save ---------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from openadapt_flow.mockmed.fault_server import serve as fault_serve  # noqa: E402

SOURCE_TEXT = "Referral\nPatient: p1\nNote: Referral to orthopedics for knee pain\n"
TRUE_NOTE = "Referral to orthopedics for knee pain"
MISREAD_NOTE = "Referral to orthopaedics for knee pain"


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _source(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "referral.txt"
    path.write_text(SOURCE_TEXT)
    from openadapt_flow.provenance import document_sha256

    return path, document_sha256(path)


def _note_provenance(sha: str, value: str) -> ProvenanceFile:
    return ProvenanceFile(
        params={
            "note": ParamProvenance(
                value=value,
                source=SourceRef(kind="document", sha256=sha, line=3, label="Note"),
                extractor="model:first-reader",
            )
        }
    )


def test_verify_run_refutes_a_faithfully_saved_misread(sor, tmp_path: Path) -> None:
    import requests

    from openadapt_flow.guard_demo import verify_yaml
    from openadapt_flow.provenance import read_sources
    from openadapt_flow.verify_run import (
        capture_baseline,
        parse_verify_config,
        verify_run,
    )

    base, _db = sor
    path, sha = _source(tmp_path)
    cfg = parse_verify_config(verify_yaml(base))

    def run(note: str, name: str) -> dict:
        params = {"patient_id": "p1", "note": note}
        baseline = tmp_path / f"{name}.json"
        capture_baseline(cfg, params, baseline)
        requests.post(
            f"{base}/api/encounter",
            json={"patient_id": "p1", "type": "Referral", "note": note},
            timeout=5,
        )
        return verify_run(
            cfg,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": "test-agent"},
            authorization_ref="ticket-0000-source",
            baseline_path=baseline,
            private_dir=tmp_path / name,
            provenance=_note_provenance(sha, note),
            documents=read_sources([path]),
        )

    good = run(TRUE_NOTE, "good")
    assert good["receipt"]["checked"] == {
        "delta": "held",
        "frame": "held",
        "source": "held",
    }
    assert good["receipt"]["outcome"] == "verified"
    bad = run(MISREAD_NOTE, "bad")
    assert bad["receipt"]["checked"]["delta"] == "held"
    assert bad["receipt"]["checked"]["source"] == "refuted"
    assert bad["receipt"]["outcome"] == "reconciliation_required"
    assert bad["evidence"]["source"]["mismatched_fields"] == ["note"]
    assert MISREAD_NOTE not in json.dumps(bad)


def _task_file(tmp_path: Path, note: str) -> Path:
    path, sha = _source(tmp_path)
    task = {
        "schema_version": "openadapt.guard-task/v1",
        "task_id": f"source-{abs(hash(note)) % 10**8:08d}",
        "expected_record": {"patient_id": "p1"},
        "intended_fields": {"note": note},
        "provenance": json.loads(_note_provenance(sha, note).model_dump_json()),
        "sources": [path.name],
    }
    out = tmp_path / "task.json"
    out.write_text(json.dumps(task))
    return out


def test_the_guard_sends_a_disagreement_to_a_person(sor, tmp_path: Path) -> None:
    from openadapt_flow.guard import GuardedSave, GuardRefused, load_task
    from openadapt_flow.guard_demo import FakeAgent, verify_yaml
    from openadapt_flow.verify_run import parse_verify_config

    base, _db = sor
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(base)), tmp_path / "state", approval="not_gated"
    )
    agent = FakeAgent(base)
    screen = agent.open_chart("p1")

    agreed = guarded.begin(
        load_task(_task_file(tmp_path, TRUE_NOTE)), screen_text=screen
    )
    assert agreed.source_check == "agree"
    assert agreed.commit().status == "go"

    misread = guarded.begin(
        load_task(_task_file(tmp_path, MISREAD_NOTE)), screen_text=screen
    )
    assert misread.source_check == "hold"
    assert misread.source_held_fields == ["note"]
    # Not gated, but this save goes to a person: commit needs what they see.
    with pytest.raises(GuardRefused) as refused:
        misread.commit()
    assert refused.value.code == "screenshot_required"


def test_a_task_whose_values_differ_from_its_provenance_is_refused(
    tmp_path: Path,
) -> None:
    from openadapt_flow.guard import GuardRefused, load_task

    path = _task_file(tmp_path, TRUE_NOTE)
    task = json.loads(path.read_text())
    task["intended_fields"]["note"] = MISREAD_NOTE
    path.write_text(json.dumps(task))
    with pytest.raises(GuardRefused) as refused:
        load_task(path)
    assert refused.value.code == "task_invalid"


def test_round_trip_maps_a_label_to_the_stored_code() -> None:
    """The referral prints the state's name; the record stores its code."""

    from openadapt_flow.provenance import bound_fields, bound_maps

    document = IntakeDocument(
        "r.txt", "text", ["Last name: Okafor", "State: Massachusetts"]
    )
    sha = sha256_bytes(document.text.encode("utf-8"))
    prov = ProvenanceFile(
        params={
            name: ParamProvenance(
                value=value,
                source=SourceRef(kind="document", sha256=sha, line=line, label=label),
                extractor="model:first-reader",
            )
            for name, value, line, label in (
                ("lname", "Okafor", 1, "Last name"),
                ("state_label", "Massachusetts", 2, "State"),
            )
        }
    )
    match = {"lname": {"param": "lname"}}
    effects = [
        Effect(kind=EffectKind.RECORD_WRITTEN, match=match),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="state",
            value={"param": "state_label", "map": {"Massachusetts": "MA"}},
        ),
    ]
    from openadapt_flow.provenance import default_extractor

    def judge(stored: str) -> str:
        return round_trip(
            prov,
            {"id": 1, "lname": "Okafor", "state": stored},
            bound_fields(effects),
            {sha: document},
            default_extractor(prov),
            maps=bound_maps(effects),
        ).status

    assert judge("MA") == "held"
    assert judge("RI") == "refuted"


def _mapped_round_trip(label: str, stored: dict, effects: list) -> str:
    from openadapt_flow.provenance import bound_fields, bound_maps, default_extractor

    document = IntakeDocument("r.txt", "text", ["Last name: Okafor", label])
    sha = sha256_bytes(document.text.encode("utf-8"))
    source_label, value = (part.strip() for part in label.split(":", 1))
    prov = ProvenanceFile(
        params={
            name: ParamProvenance(
                value=v,
                source=SourceRef(kind="document", sha256=sha, line=line, label=lab),
                extractor="model:first-reader",
            )
            for name, v, line, lab in (
                ("lname", "Okafor", 1, "Last name"),
                ("p", value, 2, source_label),
            )
        }
    )
    return round_trip(
        prov,
        {"id": 1, "lname": "Okafor", **stored},
        bound_fields(effects),
        {sha: document},
        default_extractor(prov),
        maps=bound_maps(effects),
    ).status


_MATCH = {"lname": {"param": "lname"}}


def _equals(field: str, mapping: dict | None = None) -> Effect:
    value: dict = {"param": "p"}
    if mapping:
        value["map"] = mapping
    return Effect(kind=EffectKind.FIELD_EQUALS, match=_MATCH, field=field, value=value)


def test_a_mapped_code_is_compared_exactly() -> None:
    """Adversarial review of #1572: codes that differ in case or separators."""

    written = Effect(kind=EffectKind.RECORD_WRITTEN, match=_MATCH)
    by_case = _equals("prov", {"Dr. Smith": "pr1A", "Dr. Jones": "PR1a"})
    assert (
        _mapped_round_trip("Provider: Dr. Smith", {"prov": "PR1a"}, [written, by_case])
        == "refuted"
    )
    by_dash = _equals("prov", {"Dr. Smith": "A1-23", "Dr. Jones": "A12-3"})
    assert (
        _mapped_round_trip("Provider: Dr. Smith", {"prov": "A12-3"}, [written, by_dash])
        == "refuted"
    )
    empty = _equals("sex", {"Unknown": ""})
    assert _mapped_round_trip("Sex: Unknown", {"sex": None}, [written, empty]) == (
        "not_checked"
    )


def test_a_map_follows_the_binding_its_field_comes_from() -> None:
    state = {"Massachusetts": "MA"}
    on_match = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={**_MATCH, "state": {"param": "p", "map": state}},
    )
    assert (
        _mapped_round_trip("State: Massachusetts", {"state": "MA"}, [on_match])
        == "held"
    )
    written = Effect(kind=EffectKind.RECORD_WRITTEN, match=_MATCH)
    two = [written, _equals("state_name"), _equals("state", state)]
    stored = {"state_name": "Massachusetts", "state": "MA"}
    assert _mapped_round_trip("State: Massachusetts", stored, two) == "held"
