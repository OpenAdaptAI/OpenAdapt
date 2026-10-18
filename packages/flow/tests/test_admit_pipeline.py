"""``openadapt-flow admit``: one command, one stop, a record of who decided.

Made-up values only. The fixture is the tiny qualified-write workflow from
test_qualification_proposal: one irreversible save with a system-of-record
effect, so the check proposal is a draft rather than a HALT.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import admit_pipeline as ap
from openadapt_flow.__main__ import main
from openadapt_flow.cli_admit import EXIT_STOPPED, _ask_once, admit_default_form
from openadapt_flow.compiler.motif_induction import induce_candidate, write_candidate
from openadapt_flow.ir import Workflow
from tests.test_qualification_proposal import _png, _workflow

SHADOW = Path(__file__).parent / "fixtures" / "shadow"
PERSON = ap.Decider(kind="person", id="pat", route="cli")
AGENT_A = ap.Decider(kind="agent", id="agent:intake-bot", route="mcp")
AGENT_B = ap.Decider(kind="agent", id="agent:reviewer", route="mcp")


def _recording(root: Path, name: str = "rec1", *, effects: bool = True) -> Path:
    """A compiled bundle with its recording's meta.json beside it."""

    folder = root / name
    (folder / "templates").mkdir(parents=True)
    (folder / "templates" / "save.png").write_bytes(_png())
    workflow = _workflow(effects=effects)
    workflow.surface = "web"
    workflow.save(folder)
    (folder / "meta.json").write_text(
        json.dumps(
            {
                "app_url": "http://127.0.0.1:9/",
                "application": "MockMed",
                "application_version": "tutorial",
                "viewport": [1280, 800],
                "surface": "web",
                "params": dict(workflow.params),
            }
        ),
        encoding="utf-8",
    )
    return folder


def _prepare(tmp_path: Path, by: ap.Decider = PERSON, **kwargs) -> ap.PendingAdmission:
    return ap.prepare_admission(
        [_recording(tmp_path)],
        out=tmp_path / "admission",
        name="qualified-write",
        prepared_by=by,
        **kwargs,
    )


def test_one_recording_stops_once_with_the_proposed_checks(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    assert pending.status == "needs_decision"
    assert pending.recordings == 1 and pending.induced is False
    assert pending.n_writes == 1
    assert {"step_id": "save", "kind": "field_equals", "tier": 1} in pending.checks
    assert pending.failure_cases >= 1
    assert [pin["status"] for pin in pending.pins] == ["proposed"] * 4
    summary = ap.render_summary(pending)
    assert "Proposed checks" in summary and "save: field_equals" in summary
    assert "Prepared by person:pat via cli" in summary
    # Nothing is applied before the decision.
    assert Workflow.load(tmp_path / "admission" / "bundle").qualification is None
    assert not (tmp_path / "admission" / ap.DECISION_FILE).exists()


def test_person_accept_applies_checks_and_records_who_decided(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    result = ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=PERSON,
    )
    assert result.decided_by == PERSON and result.prepared_by == PERSON
    assert result.oracle_gate_passed is True
    assert result.accepted_bundle_content_digest != pending.bundle_content_digest
    bundle = Workflow.load(tmp_path / "admission" / "bundle")
    assert bundle.qualification is not None
    assert "fault-break-it" in {case.id for case in bundle.qualification.cases}
    record = json.loads(
        (tmp_path / "admission" / "bundle" / ap.DECISION_FILE).read_text()
    )
    assert record["decided_by"] == {"kind": "person", "id": "pat", "route": "cli"}
    assert "qualify campaign" in record["next"][0]
    # No admission is signed here.
    assert not (
        tmp_path / "admission" / "bundle" / "qualification-admission.local-dev.json"
    ).exists()


def test_an_agent_cannot_decide_what_it_prepared(tmp_path: Path) -> None:
    pending = _prepare(tmp_path, by=AGENT_A)
    with pytest.raises(ap.AdmissionError, match="self-approval"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=AGENT_A,
        )
    assert not (tmp_path / "admission" / ap.DECISION_FILE).exists()
    result = ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=AGENT_B,
    )
    assert result.decided_by == AGENT_B and result.prepared_by == AGENT_A


def test_regulated_pack_needs_a_person() -> None:
    pending = ap.PendingAdmission(
        admission_id="0" * 32,
        status="needs_decision",
        workflow_name="w",
        policy_pack="regulated",
        prepared_by=PERSON,
        created_at="2026-10-17T00:00:00+00:00",
    )
    with pytest.raises(ap.AdmissionError, match="needs a person"):
        ap.check_decider(pending, AGENT_B)
    ap.check_decider(pending, PERSON)


def test_decision_binds_the_summary_and_the_bundle(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    out = tmp_path / "admission"
    with pytest.raises(ap.AdmissionError, match="changed since it was shown"):
        ap.decide_admission(
            out, pending_sha256="0" * 64, decision="accept", decided_by=PERSON
        )
    (out / "bundle" / "templates" / "save.png").write_bytes(b"not the same crop")
    with pytest.raises(ap.AdmissionError, match="bundle changed"):
        ap.decide_admission(
            out, pending_sha256=pending.sha256(), decision="accept", decided_by=PERSON
        )


def test_one_decision_only_and_reject_needs_a_reason(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    out = tmp_path / "admission"
    with pytest.raises(ap.AdmissionError, match="reason"):
        ap.decide_admission(
            out, pending_sha256=pending.sha256(), decision="reject", decided_by=PERSON
        )
    result = ap.decide_admission(
        out,
        pending_sha256=pending.sha256(),
        decision="reject",
        decided_by=PERSON,
        reason="wrong checks",
    )
    assert result.decision == "reject"
    assert Workflow.load(out / "bundle").qualification is None
    with pytest.raises(ap.AdmissionError, match="already decided"):
        ap.decide_admission(
            out, pending_sha256=pending.sha256(), decision="accept", decided_by=PERSON
        )


def test_a_halted_proposal_has_nothing_to_accept(tmp_path: Path) -> None:
    pending = ap.prepare_admission(
        [_recording(tmp_path, effects=False)],
        out=tmp_path / "admission",
        name="qualified-write",
        prepared_by=PERSON,
    )
    assert pending.status == "halted"
    assert "effect pin is missing" in (pending.halt_reason or "")
    with pytest.raises(ap.AdmissionError, match="nothing to decide"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=PERSON,
        )


def test_refused_induction_stops_without_a_bundle(tmp_path, monkeypatch) -> None:
    from openadapt_flow.compiler import induction

    monkeypatch.setattr(
        induction,
        "induce_program",
        lambda traces: SimpleNamespace(certified=False, workflow=None, record_next=[]),
    )
    pending = ap.prepare_admission(
        [_recording(tmp_path, "rec1"), _recording(tmp_path, "rec2")],
        out=tmp_path / "admission",
        name="qualified-write",
        prepared_by=PERSON,
    )
    assert pending.status == "halted"
    assert "induction refused" in (pending.halt_reason or "")
    assert not (tmp_path / "admission" / "bundle").exists()


def test_two_recordings_are_induced_into_one_program(tmp_path: Path) -> None:
    pending = ap.prepare_admission(
        [_recording(tmp_path, "rec1"), _recording(tmp_path, "rec2")],
        out=tmp_path / "admission",
        name="qualified-write",
        prepared_by=PERSON,
    )
    assert pending.recordings == 2
    assert pending.status == "needs_decision"
    assert pending.induced is True


def test_raw_recordings_are_compiled(tmp_path, monkeypatch) -> None:
    from openadapt_flow import compiler

    source = _recording(tmp_path, "compiled-source")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "meta.json").write_text((source / "meta.json").read_text())
    calls = []

    def fake_compile(recording, out, *, name):
        calls.append((Path(recording), name))
        import shutil

        shutil.copytree(source, out)
        return Workflow.load(out)

    monkeypatch.setattr(compiler, "compile_recording", fake_compile)
    pending = ap.prepare_admission(
        [raw], out=tmp_path / "admission", name="qualified-write", prepared_by=PERSON
    )
    assert calls == [(raw, "qualified-write")]
    assert pending.status == "needs_decision"
    assert (tmp_path / "admission" / "compiled" / "00" / "workflow.json").is_file()


def test_candidate_without_a_recording_asks_for_one(tmp_path: Path) -> None:
    export = json.loads((SHADOW / "report_mail.episodes.json").read_text())
    candidate = induce_candidate(export)
    folder = write_candidate(candidate, tmp_path / "shadow-candidates" / "c1")
    pending = ap.prepare_admission(
        [],
        out=tmp_path / "admission",
        name="report-mail",
        prepared_by=PERSON,
        candidate=ap.resolve_candidate("c1", tmp_path / "shadow-candidates"),
    )
    assert folder.name == "c1"
    assert pending.status == "needs_recording"
    assert pending.record_next[0]["code"] == "shadow_full_demonstration"
    assert pending.shadow_suggestions
    assert all("not evidence" in item["note"] for item in pending.shadow_suggestions)


def test_candidate_suggestions_are_shown_but_never_applied(tmp_path: Path) -> None:
    export = json.loads((SHADOW / "report_mail.episodes.json").read_text())
    folder = write_candidate(induce_candidate(export), tmp_path / "cand")
    _recording(folder / "recordings", "rec1")
    pending = ap.prepare_admission(
        [],
        out=tmp_path / "admission",
        name="qualified-write",
        prepared_by=PERSON,
        candidate=folder,
    )
    assert pending.status == "needs_decision"
    assert "not evidence, not applied" in ap.render_summary(pending)
    # Only checks mined from the recording are proposed.
    kinds = {check["kind"] for check in pending.checks}
    assert not kinds & {"file_written", "row_added", "page_reached"}


def test_cli_default_form_stops_without_a_terminal(tmp_path, capsys) -> None:
    assert admit_default_form(["admit", "a", "b"]) == ["admit", "run", "a", "b"]
    assert admit_default_form(["admit", "status"]) == ["admit", "status"]
    assert admit_default_form(["admit", "--candidate", "x"])[1] == "run"
    rec = _recording(tmp_path)
    out = tmp_path / "admission"
    code = main(["admit", str(rec), "--out", str(out), "--name", "w", "--by", "pat"])
    assert code == EXIT_STOPPED
    text = capsys.readouterr().out
    assert "Proposed checks" in text and "admit decide" in text
    assert not (out / ap.DECISION_FILE).exists()


def test_cli_single_stop_accepts_once_at_a_terminal(tmp_path, capsys) -> None:
    _prepare(tmp_path)
    answers = iter(["accept"])
    code = _ask_once(
        tmp_path / "admission", "pat", read=lambda _: next(answers), interactive=True
    )
    assert code == 0
    assert "Accepted by person:pat via cli" in capsys.readouterr().out
    record = json.loads((tmp_path / "admission" / ap.DECISION_FILE).read_text())
    assert record["decided_by"]["kind"] == "person"


def test_cli_decide_refuses_without_a_terminal(tmp_path) -> None:
    _prepare(tmp_path)
    with pytest.raises(SystemExit, match="interactive terminal"):
        main(["admit", "decide", str(tmp_path / "admission")])
