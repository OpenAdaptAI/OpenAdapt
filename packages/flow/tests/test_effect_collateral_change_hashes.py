"""``Effect.forbid_collateral_change`` (#1373) leaves earlier hashes unchanged.

A certification and a governed authorization bind the workflow contract hash.
Before this fix, that hash dumped the new field's False default, so a bundle
certified before #1373 no longer reproduced its certification and
``Workflow.load`` refused it ("governed authorization template cannot be
reproduced"). The golden values below were computed by the code at 7a7a3c548,
which predates the field.
"""

from __future__ import annotations

import json

from openadapt_flow.bundle_validation import compute_content_digest
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.qualification import workflow_contract_sha256
from openadapt_flow.runtime.effects.effect import Effect, EffectKind

#: workflow_contract_sha256 and the content digest of :func:`_workflow`, as
#: computed at 7a7a3c548 (before Effect.forbid_collateral_change existed).
PRE_1373_CONTRACT = "e0a19619160d52c0c45450bbe5dc56c52d5f144949aa1d5fefa78c2fa84765e7"
PRE_1373_DIGEST = "74c1e7fca00431dc9914d33cf9b6b027288c572a033275b65d98829f3029c3d2"


def _workflow(*, forbid_collateral_change: bool = False) -> Workflow:
    return Workflow(
        name="pre-1373",
        created_at="2026-10-19T00:00:00+00:00",
        params={"lname": "Example"},
        steps=[
            Step(id="step_000", intent="type", action=ActionKind.TYPE, param="lname"),
            Step(
                id="step_001",
                intent="save",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"lname": "{lname}"},
                        risk="irreversible",
                        forbid_collateral_change=forbid_collateral_change,
                    )
                ],
            ),
        ],
    )


def test_contract_hash_and_digest_match_a_bundle_made_before_the_field() -> None:
    workflow = _workflow()
    assert workflow_contract_sha256(workflow) == PRE_1373_CONTRACT
    assert compute_content_digest(workflow, {}) == PRE_1373_DIGEST


def test_turning_the_check_on_changes_both_hashes() -> None:
    workflow = _workflow(forbid_collateral_change=True)
    assert workflow_contract_sha256(workflow) != PRE_1373_CONTRACT
    assert compute_content_digest(workflow, {}) != PRE_1373_DIGEST


def test_the_false_default_is_not_serialized_and_true_round_trips() -> None:
    off = _workflow().steps[1].effects[0]
    on = _workflow(forbid_collateral_change=True).steps[1].effects[0]
    assert "forbid_collateral_change" not in off.model_dump(mode="json")
    assert on.model_dump(mode="json")["forbid_collateral_change"] is True
    assert Effect.model_validate(off.model_dump(mode="json")) == off
    assert Effect.model_validate(on.model_dump(mode="json")) == on


def test_a_certified_bundle_saved_before_the_field_loads(tmp_path) -> None:
    """The saved bytes carry no field; loading and re-saving keep the digest."""
    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    raw = json.loads((bundle / "workflow.json").read_text())
    assert "forbid_collateral_change" not in raw["steps"][1]["effects"][0]
    loaded = Workflow.load(bundle)
    assert workflow_contract_sha256(loaded) == PRE_1373_CONTRACT
    assert loaded.manifest.content_digest == PRE_1373_DIGEST
