"""The soak's scripted person signs its approvals with a fixture-only key.

Since a person's approval needs a registered approver key's signature, the
harness's old unsigned answer is refused. The fixture approver gives each run
its own key, roster and pinned deployment config, so the scripted person's
answers count only the way a real approver's do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.lifecycle_proof.approver import FixtureApprover
from benchmark.lifecycle_proof.run import ScriptedPerson
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalRefused,
    answer_refusal,
    make_answer,
)
from openadapt_flow.approval_mode.model import ApprovalMode

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, kind: str, **fields) -> None:
        self.rows.append({"kind": kind, **fields})


def _request(store):
    return store.open_request(
        run_id="run-0001",
        workflow_name="referral",
        step_id="step_040",
        step_index=28,
        step_intent="click 'Save new patient'",
        mode=ApprovalMode.PROBLEMS_ONLY,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=("dom", "sha256:" + "1" * 64),
    )


@pytest.fixture
def run(tmp_path, monkeypatch):
    # The environment pin is never used: a stray one must not help or hurt.
    monkeypatch.delenv(ROOT_ENV, raising=False)
    approver = FixtureApprover.for_run(tmp_path / "arm", policy="clinical-write")
    store = approver.store(tmp_path / "arm" / "items" / "ref-00026")
    store.write_approver_root()
    return approver, store


def test_the_old_unsigned_scripted_answer_is_refused(run):
    _approver, store = run
    request = _request(store)

    with pytest.raises(ItemApprovalRefused) as refused:
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id="lifecycle-soak-operator",
            auth_method="cloud_aal2",
            policy=store.context(),
        )
    assert refused.value.code == "auth_method_unverified"

    with pytest.raises(ItemApprovalRefused):
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route="desktop_run_it",
            approver_id="soak-front-desk",
            auth_method="none",
            policy=store.context(),
        )


def test_the_fixture_approver_signs_an_answer_the_run_accepts(run):
    approver, store = run
    request = _request(store)

    answer = approver.answer(store, request, approve=True)
    assert answer.attestation is not None
    assert answer_refusal(request, answer, policy=store.context()) is None
    store.write_answer(answer)
    assert store.status(request) == "approved"


def test_the_scripted_person_signs_and_a_missing_approver_is_logged(run):
    approver, store = run
    request = _request(store)

    ledger = _Ledger()
    person = ScriptedPerson(ledger, approver)
    person.watch(store)
    person.sleep(0)
    assert store.status(request) == "approved"
    assert [r["kind"] for r in ledger.rows] == ["person"]

    other = approver.store(store.run_dir.parent / "ref-00027")
    other.write_approver_root()
    unsigned = _request(other)
    ledger = _Ledger()
    person = ScriptedPerson(ledger, None)
    person.watch(other)
    with pytest.raises(ItemApprovalRefused):
        person.sleep(0)
    assert ledger.rows == [
        {
            "kind": "person_answer_refused",
            "request_id": unsigned.request_id,
            "code": "human_answer_unsigned",
        }
    ]
    assert other.status(unsigned) == "waiting"


def test_a_changed_deployment_config_is_refused(run):
    approver, _store = run
    approver.config_path.write_text(
        approver.config_path.read_text().replace("lifecycle-soak-fixture", "edited")
    )
    with pytest.raises(ValueError):
        approver.deployment()


def test_the_run_reopens_its_own_approver_and_the_ledger_has_no_secret(run, tmp_path):
    approver, _store = run
    again = FixtureApprover.for_run(tmp_path / "arm", policy="clinical-write")
    assert again.root_public_key == approver.root_public_key
    assert again.config_sha256 == approver.config_sha256

    fields = approver.ledger_fields()
    assert set(fields) == {"principal", "root_public_key", "config_sha256"}
    key_text = next(Path(approver.key_dir).glob("*.pem")).read_text()
    passphrase = (Path(approver.key_dir) / ".passphrase").read_text()
    blob = json.dumps(fields)
    assert passphrase not in blob and key_text not in blob
    # The key and its passphrase live in the run directory, owner-only.
    assert (Path(approver.key_dir) / ".passphrase").stat().st_mode & 0o077 == 0
