"""A relabeled control: proof that only its label changed, then re-admission.

openadapt_flow/repair/relabel.py. When a vendor renames a save button, the
run stops on the structured identity check. The relabel repair proposes a fix
only when the recorded identity hash matches the observed identity with the
label swapped back, so the control id, kind and form are proven unchanged.
Every other identity stop, and any stop after input reached a save, is
refused. The candidate changes the label and nothing else, and is admitted
only after re-qualification and a person's approval, through signed lineage.

The workflow and reports are made-up; the observed identity has the shape the
OpenEMR referral's save button showed in proof-15.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    RunReport,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.repair import relabel
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_structured_template,
)

OLD = "Confirm Create New Patient"
NEW = "Save new patient"


def _identity(label: str, **changes: str) -> str:
    value = {
        "form_path": "/interface/new/new_comprehensive.php",
        "label": label,
        "target_id": "confirm_create",
        "target_kind": "action",
        **changes,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _workflow(*, open_saves: bool = False) -> Workflow:
    template = build_identity_template(None, structured_identity=_identity(OLD))
    return Workflow(
        name="referral",
        params={"email": "a@example.invalid", "lname": "Example"},
        steps=[
            # A step before the save: typing a value, or (open_saves) a click
            # the fail-closed classifier counts as a save.
            Step(
                id="open",
                intent="click 'Next'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/open.png",
                    region=(0, 0, 40, 20),
                    click_point=(20, 10),
                    ocr_text="Next",
                ),
            )
            if open_saves
            else Step(
                id="open", intent="type <lname>", action=ActionKind.TYPE, text="x"
            ),
            Step(
                id="save",
                intent=f"click '{OLD}'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(520, 248, 160, 64),
                    click_point=(600, 280),
                    ocr_text=OLD,
                    structural=StructuralLocator(selector="#confirmCreate"),
                    identity_template=template,
                ),
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"email": ValueExpr(param="email")},
                        field="lname",
                        value=ValueExpr(param="lname"),
                        risk="irreversible",
                    )
                ],
                risk="irreversible",
                identity_armed=True,
            ),
        ],
    )


def _result(step_id: str, **fields: Any) -> dict[str, Any]:
    return {"step_id": step_id, "intent": step_id, **fields}


def _halted(
    observed: str,
    *,
    rung: str = "structural",
    mode: str = "structured",
    acted: bool = False,
) -> RunReport:
    open_result = _result("open", ok=True, delivery_attempted=True)
    return RunReport.model_validate(
        {
            "workflow_name": "referral",
            "started_at": "2026-10-24T00:00:00+00:00",
            "success": False,
            "transaction_outcome": "REJECTED_POLICY",
            "results": [
                open_result,
                _result(
                    "save",
                    ok=False,
                    delivery_attempted=acted,
                    resolution={
                        "rung": rung,
                        "point": [600, 280],
                        "confidence": 1.0,
                        "elapsed_ms": 50.0,
                        "structural_handle": {
                            "point": [600, 280],
                            "confidence": 1.0,
                            "region": [526, 261, 149, 38],
                            "candidate_count": 1,
                        },
                    },
                    identity={
                        "status": "mismatch",
                        "mode": mode,
                        "expected": "<structured identity template>",
                        "observed": observed,
                    },
                    error="Identity check failed for step 'save'",
                ),
            ],
            "halt": {
                "state_id": "save",
                "intent": f"click '{OLD}'",
                "reason": "Identity check failed",
            },
        }
    )


def test_a_relabeled_save_is_proposed_with_proof() -> None:
    proposal = relabel.propose(_halted(_identity(NEW)), _workflow())
    assert isinstance(proposal, relabel.RelabelProposal)
    assert (proposal.old_label, proposal.new_label) == (OLD, NEW)
    assert proposal.unchanged["target_id"] == "confirm_create"


@pytest.mark.parametrize(
    "observed",
    [
        # another control on the same form, with the new label
        _identity(NEW, target_id="cancel"),
        # the same id on another form
        _identity(NEW, form_path="/interface/patient_file/delete.php"),
        # a record field, not a control
        _identity(NEW, target_kind="field"),
    ],
)
def test_a_control_that_changed_more_than_its_label_is_refused(observed: str) -> None:
    assert isinstance(relabel.propose(_halted(observed), _workflow()), str)


def test_unstructured_or_unproven_stops_are_refused() -> None:
    wf = _workflow()
    assert isinstance(relabel.propose(_halted("Save new patient"), wf), str)
    assert isinstance(relabel.propose(_halted(_identity(NEW), mode="context"), wf), str)
    assert isinstance(
        relabel.propose(_halted(_identity(NEW), rung="geometry"), wf), str
    )


def test_a_step_that_may_have_written_is_never_relabeled() -> None:
    wf = _workflow()
    # The save itself received input.
    assert "save" in str(relabel.propose(_halted(_identity(NEW), acted=True), wf))
    # An earlier step that saves received input.
    refused = relabel.propose(_halted(_identity(NEW)), _workflow(open_saves=True))
    assert isinstance(refused, str) and "open" in refused


def test_the_candidate_changes_only_the_label(tmp_path: Path) -> None:
    wf = _workflow()
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    proposal = relabel.propose(_halted(_identity(NEW)), Workflow.load(bundle))
    assert isinstance(proposal, relabel.RelabelProposal)
    out = relabel.build(bundle, proposal, tmp_path / "candidate")
    before, after = Workflow.load(bundle), Workflow.load(out)
    old, new = before.steps[1], after.steps[1]
    assert new.intent == f"click '{NEW}'" and new.anchor.ocr_text == NEW
    template = new.anchor.identity_template
    assert verify_structured_template(template, _identity(NEW)).status == "verified"
    assert verify_structured_template(template, _identity(OLD)).status == "mismatch"
    # The salt and every other part of the step are unchanged.
    assert template.salt == old.anchor.identity_template.salt
    assert new.effects == old.effects and new.risk == old.risk
    assert new.anchor.structural == old.anchor.structural
    assert after.steps[0] == before.steps[0]
    assert after.params == before.params


class _Store:
    """Records the store calls readmit makes, in order."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    def add_candidate(self, candidate: Any) -> Any:
        self.calls.append("add")
        return candidate

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return SimpleNamespace(state="ok")

        return call

    def bundle_path(self, digest: str) -> Path:
        return self.root / "bundles" / digest


def test_a_failed_requalification_asks_nobody(tmp_path: Path, monkeypatch) -> None:
    from openadapt_flow.repair import readmission

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise readmission.ReadmissionRefused("the representative case halted")

    monkeypatch.setattr(readmission, "requalify", failing)
    wf = _workflow()
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    proposal = relabel.propose(_halted(_identity(NEW)), wf)
    assert isinstance(proposal, relabel.RelabelProposal)
    candidate = relabel.build(bundle, proposal, tmp_path / "candidate")
    asked: list[Any] = []
    store = _Store(tmp_path / "store")
    result = relabel.readmit(
        parent_bundle=bundle,
        candidate_bundle=candidate,
        proposal=proposal,
        store=store,
        requalification=readmission.Requalification(config=None),
        ask_person=lambda review: asked.append(review) or ("approve", "p"),
        work_dir=tmp_path / "work",
    )
    assert result.status == "refused" and "halted" in result.reason
    assert asked == [] and store.calls == []


def test_a_declined_relabel_admits_nothing(tmp_path: Path, monkeypatch) -> None:
    from openadapt_flow.repair import readmission, registration

    wf = _workflow()
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    proposal = relabel.propose(_halted(_identity(NEW)), wf)
    assert isinstance(proposal, relabel.RelabelProposal)
    candidate = relabel.build(bundle, proposal, tmp_path / "candidate")
    certified = tmp_path / "certified"
    shutil.copytree(candidate, certified)
    monkeypatch.setattr(
        readmission,
        "requalify",
        lambda *a, **k: readmission.Requalified(
            bundle_dir=certified,
            digest="c" * 64,
            policy_name="clinical-write",
            policy_contract_sha256="d" * 64,
            case_ids=("representative",),
            parent_report_sha256="e" * 64,
            report_sha256="f" * 64,
            result_sha256=None,
        ),
    )
    monkeypatch.setattr(
        registration,
        "build_candidate",
        lambda *a, **k: SimpleNamespace(state="candidate", candidate_id="cand-1"),
    )
    admitted: list[Any] = []
    monkeypatch.setattr(readmission, "admit", lambda *a, **k: admitted.append(k))
    store = _Store(tmp_path / "store")
    result = relabel.readmit(
        parent_bundle=bundle,
        candidate_bundle=candidate,
        proposal=proposal,
        store=store,
        requalification=readmission.Requalification(config=None),
        ask_person=lambda review: ("decline", "operator-1"),
        work_dir=tmp_path / "work",
    )
    assert result.status == "declined"
    assert admitted == [] and "approve" not in store.calls
    assert store.calls == ["add", "reject"]
