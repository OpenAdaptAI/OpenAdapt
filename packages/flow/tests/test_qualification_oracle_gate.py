"""Execute the qualify-proposal --break-it oracle gate.

A banner-only oracle is rejected. A same-page oracle is rejected for channel
overlap. A disjoint SoR read that catches the lying backend can be accepted
after the operator confirms. Missing a second channel HALTs. The proposer-off
path stays the happy MockMed mine.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.qualification_oracle_gate import (
    BROKEN_CASE_ACCEPTED,
    CHANNEL_ACTING_SCREEN,
    MISSING_SECOND_READ,
    evaluate_oracle_gate,
)
from openadapt_flow.qualification_proposal import (
    QualificationProposalError,
    accept_proposal,
    propose_qualification,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.effects.effect import ReadbackSpec


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _write_workflow(
    tmp_path: Path,
    workflow: Workflow,
    *,
    app_url: str = "http://127.0.0.1:9/",
) -> tuple[Workflow, Path, Path]:
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "app_url": app_url,
                "application": "MockMed",
                "application_version": "tutorial",
                "viewport": [1280, 800],
                "surface": "web",
                "params": dict(workflow.params),
            }
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_png())
    workflow.surface = "web"
    workflow.save(bundle)
    return Workflow.load(bundle), recording, bundle


def _step(effects: list[Effect]) -> Step:
    return Step(
        id="save",
        intent="Save the record",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/save.png",
            region=(10, 10, 40, 20),
            click_point=(30, 20),
            ocr_text="Save",
            structured_identity="record identity",
        ),
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="Saved",
            )
        ],
        effects=effects,
        risk="irreversible",
        identity_armed=True,
    )


def _workflow(*, effects: Optional[list[Effect]] = None) -> Workflow:
    if effects is None:
        effects = [
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"id": ValueExpr(param="record_id")},
                field="note",
                value=ValueExpr(param="note"),
                idempotency_key=ValueExpr(param="record_id"),
                risk="irreversible",
            )
        ]
    return Workflow(
        name="qualified-write",
        params={"record_id": "example", "note": "example"},
        steps=[_step(effects)],
    )


def test_banner_oracle_is_rejected_by_break_it(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(
        tmp_path,
        _workflow(
            effects=[
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    value="Encounter saved",
                    readback=ReadbackSpec(
                        region=(10, 10, 40, 20), different_path=False
                    ),
                    probe="same-session success banner",
                    risk="irreversible",
                )
            ]
        ),
    )
    proposal = propose_qualification(workflow, recording_dir=recording)
    assert proposal.status == "halted"
    assert proposal.halt_reason is not None
    assert BROKEN_CASE_ACCEPTED in proposal.halt_reason
    gate = proposal.oracle_gate or {}
    assert gate.get("break_it_executed") is True
    assert gate.get("screen_claimed_success") is True
    assert gate.get("store_unchanged") is True
    assert gate.get("system_of_record_records") == 0
    verdicts = gate.get("break_it_verdicts") or []
    assert verdicts
    assert verdicts[0]["verdict"] == "confirmed"
    with pytest.raises(QualificationProposalError, match="accepted the broken case"):
        accept_proposal(workflow, proposal)


def test_acting_page_oracle_is_rejected_for_channel_overlap(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(
        tmp_path,
        _workflow(
            effects=[
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    value=ValueExpr(param="note"),
                    readback=ReadbackSpec(
                        region=(10, 10, 40, 20), different_path=False
                    ),
                    risk="irreversible",
                )
            ]
        ),
    )
    proposal = propose_qualification(workflow, recording_dir=recording)
    assert proposal.status == "halted"
    assert proposal.halt_reason is not None
    assert "shares the acting channel" in proposal.halt_reason
    assert CHANNEL_ACTING_SCREEN in proposal.halt_reason
    shared = (proposal.oracle_gate or {}).get("shared_channels") or []
    assert CHANNEL_ACTING_SCREEN in shared


def test_disjoint_sor_read_is_accepted_after_operator_confirm(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow())
    proposal = propose_qualification(workflow, recording_dir=recording)
    assert proposal.status == "draft"
    gate = proposal.oracle_gate or {}
    assert gate.get("passed") is True
    assert gate.get("break_it_executed") is True
    assert gate.get("store_unchanged") is True
    verdicts = gate.get("break_it_verdicts") or []
    assert verdicts
    assert verdicts[0]["verdict"] == "refuted"
    accepted = accept_proposal(workflow, proposal)
    assert accepted.status == "accepted"
    assert workflow.qualification is not None


def test_missing_second_channel_halts(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow(effects=[]))
    proposal = propose_qualification(workflow, recording_dir=recording)
    assert proposal.status == "halted"
    assert proposal.halt_reason is not None
    assert MISSING_SECOND_READ in proposal.halt_reason
    gate = evaluate_oracle_gate(workflow)
    assert gate.passed is False
    assert gate.halt_reason is not None
    assert MISSING_SECOND_READ in gate.halt_reason
    with pytest.raises(QualificationProposalError, match=MISSING_SECOND_READ):
        accept_proposal(workflow, proposal)


def test_proposer_off_path_matches_happy_mockmed_mine(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow())
    proposal = propose_qualification(workflow, recording_dir=recording)
    assert proposal.status == "draft"
    assert proposal.suggestions == []
    assert [pin["kind"] for pin in proposal.pins] == [
        "application",
        "environment",
        "identity",
        "effect",
    ]
    assert all(pin["status"] == "proposed" for pin in proposal.pins)
    assert proposal.pin("effect")["payload"]["effects"][0]["kind"] == "field_equals"
    classes = [case["case_class"] for case in proposal.failure_matrix]
    assert "break_it" in classes


def test_proposer_sketches_are_flagged_and_still_gated(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow())
    seen: dict[str, Any] = {}

    class _FakeProposer:
        source = "test"

        def propose(self, target: str, kind: str, context: dict[str, Any]) -> str:
            seen[kind] = context
            if kind == "identity_field":
                return "record_id"
            return "field_equals on the persisted note; do not use the banner"

    proposal = propose_qualification(
        workflow, recording_dir=recording, proposer=_FakeProposer()
    )
    assert proposal.status == "draft"
    kinds = {item["kind"] for item in proposal.suggestions}
    assert kinds == {"identity_field", "effect_contract_sketch"}
    assert all(item["trusted"] is False for item in proposal.suggestions)
    assert all(
        item["schema_version"] == "openadapt.qualification-suggestion/v1"
        for item in proposal.suggestions
    )
    recording_ctx = seen["identity_field"]["recording"]
    assert "frame" not in recording_ctx
    assert "screenshot" not in recording_ctx
    assert "params" not in recording_ctx
    assert "param_names" in recording_ctx
    accepted = accept_proposal(workflow, proposal)
    assert accepted.status == "accepted"
    # The sketch is not the pin. The structural SoR effect still is.
    assert accepted.pin("effect")["payload"]["effects"][0]["kind"] == "field_equals"
