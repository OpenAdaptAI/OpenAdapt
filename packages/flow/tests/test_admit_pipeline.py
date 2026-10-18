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
from openadapt_flow.approval_mode.approver import ApproverPolicyV1, StepClass
from openadapt_flow.cli_admit import EXIT_STOPPED, _ask_once, admit_default_form
from openadapt_flow.compiler.motif_induction import induce_candidate, write_candidate
from openadapt_flow.ir import Workflow
from tests._approver_grant_support import key_dir, signed_grant
from tests.test_qualification_proposal import _png, _workflow

SHADOW = Path(__file__).parent / "fixtures" / "shadow"
PERSON = ap.Decider(kind="person", id="pat", route="cli")
AGENT_A = ap.Decider(kind="agent", id="agent:intake-bot", route="mcp")
AGENT_B = ap.Decider(kind="agent", id="agent:reviewer", route="mcp")


@pytest.fixture(autouse=True)
def _local_admission_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local admission key lives in the test's key directory, not ~."""

    monkeypatch.setenv("OPENADAPT_JIT_KEY_DIR", str(key_dir(tmp_path)))


def _agent_policy() -> ApproverPolicyV1:
    allowed = {c.value: ["person"] for c in StepClass}
    allowed[StepClass.CONSEQUENTIAL.value] = ["person", "agent"]
    return ApproverPolicyV1.model_validate({"allowed": allowed})


def _grant(tmp_path: Path, **kwargs):
    kwargs.setdefault("delegates", ("agent:reviewer",))
    return signed_grant(tmp_path, _agent_policy(), **kwargs)


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
    assert record["approval_mode"] == "problems_only"
    assert record["approver_grant"] is None
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
    grant = _grant(tmp_path)
    result = ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=AGENT_B,
        approver_grant=grant,
        key_dir=key_dir(tmp_path),
    )
    assert result.decided_by.id == AGENT_B.id and result.prepared_by == AGENT_A
    # The record names the grant and the person who signed it.
    assert result.decided_by.policy_ref == grant.grant.digest()
    assert result.decided_by.on_behalf_of == grant.grant.signer
    assert result.approval_mode == "problems_only"
    assert "under grant" in result.decided_by.label()


@pytest.mark.parametrize(
    "grant_kwargs, refusal",
    [
        (None, "approver_grant_missing"),
        ({"delegates": ("agent:someone-else",)}, "delegate_not_in_grant"),
        ({"workflows": ("another-workflow",)}, "workflow_not_in_grant"),
        ({"days": 1, "signed_at": "expired"}, "approver_grant_expired"),
    ],
)
def test_an_agent_admits_only_under_a_person_signed_grant(
    tmp_path: Path, grant_kwargs, refusal
) -> None:
    from datetime import datetime, timedelta, timezone

    pending = _prepare(tmp_path)
    grant = None
    if grant_kwargs is not None:
        kwargs = dict(grant_kwargs)
        if kwargs.get("signed_at") == "expired":
            kwargs["signed_at"] = datetime.now(timezone.utc) - timedelta(days=3)
        grant = _grant(tmp_path, **kwargs)
    with pytest.raises(ap.AdmissionError, match=refusal):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=AGENT_B,
            approver_grant=grant,
            key_dir=key_dir(tmp_path),
        )
    assert not (tmp_path / "admission" / ap.DECISION_FILE).exists()


def test_a_tampered_or_person_only_grant_admits_nothing(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    grant = _grant(tmp_path)
    tampered = grant.model_dump(mode="json", by_alias=True)
    tampered["grant"]["delegates"] = ["agent:reviewer", "agent:intruder"]
    with pytest.raises(ap.AdmissionError, match="approver_grant_signature_invalid"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=AGENT_B,
            approver_grant=tampered,
            key_dir=key_dir(tmp_path),
        )
    # A grant that lets an agent approve only read-only steps can't admit a
    # workflow whose saves would then go ahead without a person.
    allowed = {c.value: ["person"] for c in StepClass}
    allowed[StepClass.READ_ONLY.value] = ["person", "agent"]
    narrow = signed_grant(
        tmp_path,
        ApproverPolicyV1.model_validate({"allowed": allowed}),
        delegates=("agent:reviewer",),
    )
    with pytest.raises(ap.AdmissionError, match="grant_does_not_allow_agent_saves"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=AGENT_B,
            approver_grant=narrow,
            key_dir=key_dir(tmp_path),
        )


def test_a_person_cannot_claim_a_grant() -> None:
    with pytest.raises(ValueError):
        ap.Decider(
            kind="person",
            id="pat",
            route="cli",
            policy_ref="sha256:" + "a" * 64,
            on_behalf_of="person-0001",
        )


def test_an_admitted_bundle_runs_only_problems(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    bundle = tmp_path / "admission" / "bundle"
    assert ap.admitted_approval_mode(bundle) == ap.AdmittedMode()
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=PERSON,
    )
    admitted = ap.admitted_approval_mode(bundle)
    assert admitted.approval_mode == "problems_only"
    assert admitted.admitted_by == PERSON
    # Changing the bundle after admission drops back to the default.
    workflow = Workflow.load(bundle)
    workflow.name = "edited-after-admission"
    workflow.save(bundle)
    changed = ap.admitted_approval_mode(bundle)
    assert changed.approval_mode is None
    assert changed.refusal == "admission_bundle_changed"


def test_a_rejected_admission_sets_no_mode(tmp_path: Path) -> None:
    pending = _prepare(tmp_path)
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="reject",
        decided_by=PERSON,
        reason="wrong program",
    )
    assert ap.admitted_approval_mode(tmp_path / "admission" / "bundle") == (
        ap.AdmittedMode()
    )


def test_an_agent_admission_stops_applying_when_its_grant_does(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    pending = _prepare(tmp_path, by=AGENT_A)
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=AGENT_B,
        approver_grant=_grant(tmp_path, days=2),
        key_dir=key_dir(tmp_path),
    )
    bundle = tmp_path / "admission" / "bundle"
    keys = key_dir(tmp_path)
    assert ap.admitted_approval_mode(bundle, key_dir=keys).approval_mode == (
        "problems_only"
    )
    later = datetime.now(timezone.utc) + timedelta(days=3)
    expired = ap.admitted_approval_mode(bundle, key_dir=keys, now=later)
    assert expired.approval_mode is None
    assert expired.refusal == "approver_grant_expired"
    # A grant swapped into the record no longer matches the recorded digest.
    record_path = bundle / ap.DECISION_FILE
    record = json.loads(record_path.read_text())
    record["approver_grant"] = _grant(tmp_path, days=5).model_dump(
        mode="json", by_alias=True
    )
    record_path.write_text(json.dumps(record))
    assert ap.admitted_approval_mode(bundle, key_dir=keys).refusal == (
        "admission_signature_invalid"
    )
    # Even re-signed by someone holding the local key, the swapped grant
    # doesn't match the digest the decision recorded.
    record.pop("signature")
    ap._write_signed(record_path, record, keys)
    assert ap.admitted_approval_mode(bundle, key_dir=keys).refusal == (
        "policy_ref_mismatch"
    )


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


def test_run_uses_the_admitted_mode_unless_told_otherwise(tmp_path, capsys) -> None:
    from openadapt_flow.__main__ import _resolve_run_approval_mode

    pending = _prepare(tmp_path)
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=PERSON,
    )
    bundle = str(tmp_path / "admission" / "bundle")
    no_cfg = SimpleNamespace(runtime=SimpleNamespace(approval_mode=None))
    args = SimpleNamespace(approval_mode=None, bundle=bundle)
    assert _resolve_run_approval_mode(args, no_cfg, None).value == "problems_only"
    assert "Admitted by person:pat via cli" in capsys.readouterr().out
    # A flag or the deployment's runtime.approval_mode still wins.
    flagged = SimpleNamespace(approval_mode="each_item", bundle=bundle)
    assert _resolve_run_approval_mode(flagged, no_cfg, None).value == "each_item"
    deployed = SimpleNamespace(runtime=SimpleNamespace(approval_mode="each_item"))
    assert _resolve_run_approval_mode(args, deployed, None).value == "each_item"
    # Without an admission the default is a person approving each save.
    bare = SimpleNamespace(approval_mode=None, bundle=str(tmp_path))
    assert _resolve_run_approval_mode(bare, no_cfg, None).value == "each_item"


# -- fail-closed review of #1212 ---------------------------------------------


def _forged_decision(bundle: Path, pending: ap.PendingAdmission) -> dict:
    return {
        "schema_version": ap.DECISION_SCHEMA,
        "admission_id": pending.admission_id,
        "decision": "accept",
        "decided_by": {"kind": "person", "id": "pat", "route": "cli"},
        "prepared_by": {"kind": "person", "id": "pat", "route": "cli"},
        "decided_at": "2026-01-01T00:00:00+00:00",
        "pending_sha256": pending.sha256(),
        "bundle_content_digest": pending.bundle_content_digest,
        "proposal_sha256": pending.proposal_sha256,
        "accepted_bundle_content_digest": ap._bundle_digest(bundle),
        "approval_mode": "problems_only",
        "workflow_name": pending.workflow_name,
    }


def test_a_decision_file_written_without_the_admission_key_admits_nothing(
    tmp_path: Path,
) -> None:
    pending = _prepare(tmp_path)
    bundle = tmp_path / "admission" / "bundle"
    path = bundle / ap.DECISION_FILE
    path.write_text(json.dumps(_forged_decision(bundle, pending)))
    forged = ap.admitted_approval_mode(bundle)
    assert forged.approval_mode is None
    assert forged.refusal == "admission_signature_invalid"
    # A signature by another key is no better.
    record = _forged_decision(bundle, pending)
    ap._write_signed(path, record, tmp_path / "other-keys")
    assert ap.admitted_approval_mode(bundle).refusal == "admission_signature_invalid"


def test_a_signed_decision_edited_afterwards_admits_nothing(tmp_path: Path) -> None:
    pending = _prepare(tmp_path, by=AGENT_A)
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=AGENT_B,
        approver_grant=_grant(tmp_path),
        key_dir=key_dir(tmp_path),
    )
    bundle = tmp_path / "admission" / "bundle"
    path = bundle / ap.DECISION_FILE
    record = json.loads(path.read_text())
    # An agent's admission relabelled as a person's, to skip the grant check.
    record["decided_by"] = {"kind": "person", "id": "pat", "route": "cli"}
    record["approver_grant"] = None
    path.write_text(json.dumps(record))
    edited = ap.admitted_approval_mode(bundle)
    assert edited.approval_mode is None
    assert edited.refusal == "admission_signature_invalid"


def test_an_edited_pending_file_is_refused(tmp_path: Path) -> None:
    _prepare(tmp_path, by=AGENT_B)
    path = tmp_path / "admission" / ap.PENDING_FILE
    data = json.loads(path.read_text())
    # Relabel the preparer so the agent that prepared it could decide it.
    data["prepared_by"] = {"kind": "person", "id": "pat", "route": "cli"}
    path.write_text(json.dumps(data))
    with pytest.raises(ap.AdmissionError, match="not signed"):
        ap.load_pending(tmp_path / "admission")
    data.pop("signature")
    edited = ap.PendingAdmission.model_validate(data)
    with pytest.raises(ap.AdmissionError, match="not signed"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=edited.sha256(),
            decision="accept",
            decided_by=AGENT_B,
            approver_grant=_grant(tmp_path),
            key_dir=key_dir(tmp_path),
        )
    assert not (tmp_path / "admission" / ap.DECISION_FILE).exists()


def test_an_agent_cannot_decide_what_an_unidentified_client_prepared(
    tmp_path: Path,
) -> None:
    unidentified = ap.Decider(kind="agent", id=ap.UNIDENTIFIED_AGENT, route="mcp")
    pending = _prepare(tmp_path, by=unidentified)
    with pytest.raises(ap.AdmissionError, match="unidentified"):
        ap.decide_admission(
            tmp_path / "admission",
            pending_sha256=pending.sha256(),
            decision="accept",
            decided_by=AGENT_B,
            approver_grant=_grant(tmp_path),
            key_dir=key_dir(tmp_path),
        )
    # A person still can.
    result = ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=PERSON,
    )
    assert result.decided_by == PERSON


def test_the_value_free_summary_quotes_no_recorded_text() -> None:
    recorded = "Sample Label Alpha"
    halted = ap.PendingAdmission(
        admission_id="0" * 32,
        status="halted",
        halt_reason=f"induction refused near {recorded}",
        workflow_name="w",
        policy_pack="community",
        prepared_by=PERSON,
        created_at="2026-01-01T00:00:00+00:00",
        record_next=[
            {
                "code": "ambiguous_branch",
                "instruction": f"Record each of {recorded} vs Other under a "
                "detectable condition.",
            }
        ],
    )
    assert recorded in ap.render_summary(halted)
    agent_view = ap.render_summary(halted, value_free=True)
    assert recorded not in agent_view
    assert "ambiguous_branch" in agent_view
    ready = halted.model_copy(
        update={
            "status": "needs_decision",
            "halt_reason": None,
            "record_next": [],
            "shadow_suggestions": [
                {"kind": "file_saved", "summary": f"saved {recorded}.xlsx"}
            ],
        }
    )
    assert recorded not in ap.render_summary(ready, value_free=True)


def test_an_admitted_bundle_still_asks_a_person_until_the_switch_is_on(
    tmp_path: Path, capsys
) -> None:
    """Admission picks problems_only; the switch record still gates each save."""

    from openadapt_flow.__main__ import _resolve_run_approval_mode
    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.approval_mode.model import ItemDecisionKind
    from tests._approval_mode_support import BUNDLE_DIGEST, good_item
    from tests.test_approval_mode_gate import Harness

    pending = _prepare(tmp_path)
    ap.decide_admission(
        tmp_path / "admission",
        pending_sha256=pending.sha256(),
        decision="accept",
        decided_by=PERSON,
    )
    args = SimpleNamespace(
        approval_mode=None, bundle=str(tmp_path / "admission" / "bundle")
    )
    no_cfg = SimpleNamespace(runtime=SimpleNamespace(approval_mode=None))
    mode = _resolve_run_approval_mode(args, no_cfg, None)
    assert mode.value == "problems_only"
    # No signed switch record: the gate sends the save to a person.
    gate = ApprovalGate(mode, item=good_item())
    gate.begin_item(
        run_id="r", bundle_content_digest=BUNDLE_DIGEST, profile="regulated"
    )
    verdict = gate.before_save(Harness(tmp_path / "gate").save_context())
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
