"""Receipts carry handoff lines, and follow-up receipts record later changes.

A real run through the replayer saves a claim with a deferred check; the
after-run hook writes the receipt with the claim's ``sent`` line. A referral
handoff of the same run is read back later, and ``sign_updates`` signs a
follow-up for each change. ``verify_chain`` checks the chain. Synthetic data.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

r = pytest.importorskip("openadapt_types.receipt")
if not hasattr(r, "verify_chain"):
    pytest.skip("openadapt-types without follow-up receipts", allow_module_level=True)
pytest.importorskip("cryptography")

from openadapt_flow.handoffs import followup, intake, registry  # noqa: E402
from openadapt_flow.handoffs.ledger import HandoffLedger  # noqa: E402
from openadapt_flow.handoffs.payer import SettleRecorder, handoff_ledger  # noqa: E402
from openadapt_flow.handoffs.readers import ReadResult  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects.deferred_report import (  # noqa: E402
    DeferredReportVerifier,
    PendingLedger,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_deferred_report import (  # noqa: E402
    FMT_277,
    _claim_effect,
    _spec,
    ack_277ca,
)
from tests.test_replayer import FakeBackend, FakeVision, Match  # noqa: E402

FIELDS = {
    "family_name": ("Quill", "text"),
    "birth_date": ("1984-03-09", "date"),
}


class Fake:
    read_method = "api_readback"

    def __init__(self, result: ReadResult) -> None:
        self.result = result

    def read(self, key: str) -> ReadResult:
        return self.result


def _found(**changes: str) -> ReadResult:
    fields = {"family_name": "Quill", "birth_date": "1984-03-09", **changes}
    return ReadResult("found", fields=fields)


@pytest.fixture
def env(tmp_path, monkeypatch):
    registry.forget()
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.delenv("OPENADAPT_RECEIPT", raising=False)
    monkeypatch.delenv(registry.ENV, raising=False)
    yield {"OPENADAPT_SEAL_HOME": str(tmp_path / "seal")}
    registry.forget()


def _run(tmp_path: Path) -> Path:
    workflow = Workflow(
        name="claim",
        steps=[
            Step(
                id="submit",
                intent="submit claim",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Submitted",
                        timeout_s=0.2,
                    )
                ],
                effects=[_claim_effect()],
            )
        ],
    )
    vision = FakeVision()
    vision.text_results = {
        "Submitted": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    run_dir = tmp_path / "run"
    verifier = DeferredReportVerifier(_spec(tmp_path), "CLM-1001")
    report = Replayer(
        FakeBackend(), vision=vision, effect_verifier=verifier, poll_interval_s=0.01
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.transaction_outcome == "SAVED_AWAITING_CHECK"
    return run_dir


def _registry(tmp_path: Path):
    from openadapt_flow.receipt_record import local_registry

    return local_registry(tmp_path / "seal")


def _payload(path: Path) -> dict:
    envelope = json.loads(path.read_text())
    return json.loads(base64.b64decode(envelope["payload"]))


def test_a_real_run_writes_a_receipt_that_carries_the_claims_line(tmp_path, env):
    from openadapt_flow.receipt_record import receipt_after_run, verify_run_receipt

    run_dir = _run(tmp_path)
    note = receipt_after_run(run_dir, env)
    assert note and note.startswith("receipt:"), note
    confirmations = _payload(run_dir / "receipt.json")["predicate"]["confirmations"]
    assert [
        (c["for_step"], c["system_kind"], c["read_method"], c["state"])
        for c in confirmations
    ] == [(0, "payer_portal", "acknowledgment_message", "sent")]
    result = verify_run_receipt(run_dir, _registry(tmp_path))
    assert result.ok, (result.reason, result.detail)
    assert result.confirmation_lines[0].startswith("Sent to the other party")
    # Awaiting the other party: signed, and never VERIFIED.
    predicate = _payload(run_dir / "receipt.json")["predicate"]
    assert (predicate["outcome"], predicate["reason_code"]) == (
        "UNKNOWN",
        "awaiting_counterparty",
    )
    assert result.outcome != "VERIFIED"
    sidecar = run_dir / followup.SIDECAR
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert "CLM-1001" not in sidecar.read_text()


def test_a_pending_receipt_is_only_for_sent_handoff_lines():
    """permissive-default guarded: any other unsettled write refuses."""
    from types import SimpleNamespace

    from openadapt_flow.receipt_record import ReceiptRecordError, _awaiting_counterparty

    def statement(*checks):
        return {
            "predicate": {
                "outcome": "UNKNOWN",
                "reason_code": "x",
                "lines": [{"step": i, "check": c} for i, c in enumerate(checks)],
            }
        }

    sent = SimpleNamespace(step=0, state="sent")
    ok = _awaiting_counterparty(statement("not_checked", "confirmed"), [sent])
    assert ok["predicate"]["outcome"] == "UNKNOWN"
    assert ok["predicate"]["reason_code"] == "awaiting_counterparty"
    # A handoff line plus another unverified write refuses.
    with pytest.raises(ReceiptRecordError, match="readback_pending"):
        _awaiting_counterparty(statement("not_checked", "not_checked"), [sent])
    # A pending write with no handoff line refuses.
    with pytest.raises(ReceiptRecordError, match="readback_pending"):
        _awaiting_counterparty(statement("not_checked"), [])
    # A handoff already answered is not "sent": refuses.
    answered = SimpleNamespace(step=0, state="recorded_differently")
    with pytest.raises(ReceiptRecordError, match="readback_pending"):
        _awaiting_counterparty(statement("not_checked"), [answered])
    # A refuted write refuses.
    with pytest.raises(ReceiptRecordError, match="readback_pending"):
        _awaiting_counterparty(statement("refuted"), [sent])


def test_a_run_without_handoffs_has_a_receipt_without_confirmations(tmp_path, env):
    from openadapt_flow.receipt_record import receipt_after_run

    run_dir = _run(tmp_path)
    registry.forget()
    # The claim's ledger isn't noted or named: the pending write has no
    # handoff line, so the run awaits its check with no receipt yet.
    note = receipt_after_run(run_dir, env)
    assert note == "receipt not written: readback_pending"
    assert not (run_dir / "receipt.json").exists()


def test_follow_ups_record_each_change_and_verify_as_a_chain(tmp_path, env):
    from openadapt_flow.offline_seal import signer_for
    from openadapt_flow.receipt_record import receipt_after_run

    run_dir = _run(tmp_path)
    referrals = HandoffLedger(tmp_path / "referrals")
    referral = referrals.send(
        "REF-1",
        FIELDS,
        counterparty="receiving clinic",
        system_kind="ehr",
        follow_up_hours=24,
        run_id=str(run_dir),
        step=0,
    )
    receipt_after_run(run_dir, env)
    states = [
        c["state"]
        for c in _payload(run_dir / "receipt.json")["predicate"]["confirmations"]
    ]
    assert sorted(states) == ["sent", "sent"]
    signer = signer_for(tmp_path / "seal")
    claims = handoff_ledger(PendingLedger(tmp_path / "ledger"))
    ledgers = [claims, referrals]

    # Nothing changed yet: no update.
    assert followup.sign_updates(run_dir, ledgers, signer=signer) is None

    # The referral is recorded as intended: update 1 carries only its line.
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))
    first = followup.sign_updates(run_dir, ledgers, signer=signer)
    assert first is not None and first.name == "receipt.update-1.json"
    p1 = _payload(first)["predicate"]
    assert [c["state"] for c in p1["confirmations"]] == ["recorded_as_intended"]
    original = _payload(run_dir / "receipt.json")
    assert p1["follows"]["sequence"] == 1
    assert p1["lines"] == original["predicate"]["lines"]

    # The payer acknowledges the claim, and the referral now reads
    # differently: update 2 carries both changes.
    pending = PendingLedger(tmp_path / "ledger")
    pending.settle(
        ack_277ca(datetime.now(timezone.utc).replace(tzinfo=None)),
        FMT_277,
        observer=SettleRecorder(pending, "x12-277ca", None),
    )
    intake.check(referrals, referral.id, "REF-1", Fake(_found(birth_date="1984-03-10")))
    second = followup.sign_updates(run_dir, ledgers, signer=signer)
    assert second is not None and second.name == "receipt.update-2.json"
    assert sorted(
        c["state"] for c in _payload(second)["predicate"]["confirmations"]
    ) == [
        "acknowledged",
        "recorded_differently",
    ]
    assert followup.sign_updates(run_dir, ledgers, signer=signer) is None

    chain = followup.verify_history(run_dir, _registry(tmp_path))
    assert chain.ok, (chain.reason, chain.detail)
    assert len(chain.lines) == 5
    assert all(line.startswith("Sent to the other party") for line in chain.lines[:2])
    assert [line.split(":")[0] for line in chain.lines[2:]] == [
        "Update 1",
        "Update 2",
        "Update 2",
    ]
    assert chain.lines[2].startswith("Update 1: Confirmed in the other party's system")
    assert any("1 field differs" in line for line in chain.lines[3:])
    assert any("Acknowledged by the other party" in line for line in chain.lines[3:])


def _chain_setup(tmp_path, env):
    from openadapt_flow.offline_seal import signer_for
    from openadapt_flow.receipt_record import receipt_after_run

    run_dir = _run(tmp_path)
    referrals = HandoffLedger(tmp_path / "referrals")
    referral = referrals.send(
        "REF-1",
        FIELDS,
        counterparty="receiving clinic",
        system_kind="ehr",
        follow_up_hours=24,
        run_id=str(run_dir),
        step=0,
    )
    receipt_after_run(run_dir, env)
    signer = signer_for(tmp_path / "seal")
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))
    followup.sign_updates(run_dir, [referrals], signer=signer)
    intake.check(referrals, referral.id, "REF-1", Fake(_found(birth_date="1984-03-10")))
    followup.sign_updates(run_dir, [referrals], signer=signer)
    return run_dir, signer


def test_a_broken_chain_is_refused(tmp_path, env):
    run_dir, signer = _chain_setup(tmp_path, env)
    registry_ = _registry(tmp_path)
    original = json.loads((run_dir / "receipt.json").read_text())
    one = json.loads((run_dir / "receipt.update-1.json").read_text())
    two = json.loads((run_dir / "receipt.update-2.json").read_text())
    assert r.verify_chain(original, [one, two], registry_).ok
    assert r.verify_chain(original, [two, one], registry_).reason == "out_of_sequence"
    assert r.verify_chain(original, [two], registry_).reason == "out_of_sequence"
    assert r.verify_chain(one, [], registry_).reason == "original_is_follow_up"
    kid = r.key_id(signer.public_raw())

    def resign(statement):
        return r.sign_statement(statement, [(kid, signer.sign)])

    wrong = _payload(run_dir / "receipt.update-1.json")
    wrong["predicate"]["follows"]["receipt"]["sha256"] = "0" * 64
    assert r.verify_chain(original, [resign(wrong)], registry_).reason == (
        "wrong_original"
    )
    changed = _payload(run_dir / "receipt.update-1.json")
    changed["predicate"]["model_calls"] = 7
    assert r.verify_chain(original, [resign(changed)], registry_).reason == (
        "changed_original"
    )
    tampered = dict(one, payload=two["payload"])
    assert r.verify_chain(original, [tampered], registry_).reason == "update_refused"
    empty = _payload(run_dir / "receipt.update-1.json")
    empty["predicate"]["confirmations"] = []
    with pytest.raises(ValueError, match="at least one confirmation"):
        resign(empty)


def test_sign_updates_refuses_a_receipt_it_cannot_verify_or_did_not_sign(tmp_path, env):
    from openadapt_flow.offline_seal import signer_for

    run_dir, signer = _chain_setup(tmp_path, env)
    referrals = HandoffLedger(tmp_path / "referrals")
    (referral,) = referrals.entries()
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))
    receipt = run_dir / "receipt.json"
    good = receipt.read_text()
    envelope = json.loads(good)
    other = _payload(receipt)
    other["predicate"]["model_calls"] = 9
    receipt.write_text(
        json.dumps(
            dict(
                envelope,
                payload=r.sign_statement(
                    other, [(r.key_id(signer.public_raw()), signer.sign)]
                )["payload"],
            )
        )
    )
    with pytest.raises(ValueError, match="does not verify"):
        followup.sign_updates(run_dir, [referrals], signer=signer)
    receipt.write_text(good)
    stranger = signer_for(tmp_path / "stranger-seal")
    with pytest.raises(ValueError, match="does not verify"):
        followup.sign_updates(run_dir, [referrals], signer=stranger)
    assert not (run_dir / "receipt.update-3.json").exists()
    # An update past a gap is never ignored: the history fails closed.
    one_path = run_dir / "receipt.update-1.json"
    saved = one_path.read_text()
    one_path.rename(run_dir / "receipt.update-9.json")
    with pytest.raises(ValueError, match="skip a number"):
        followup.verify_history(run_dir, _registry(tmp_path))
    with pytest.raises(ValueError, match="skip a number"):
        followup.sign_updates(run_dir, [referrals], signer=signer)
    (run_dir / "receipt.update-9.json").unlink()
    one_path.write_text(saved)
    (run_dir / "receipt.update-x.json").write_text("{}")
    with pytest.raises(ValueError, match="unexpected update file"):
        followup.updates(run_dir)
    (run_dir / "receipt.update-x.json").unlink()
    # A report that isn't the one the receipt signed never picks handoffs.
    report = run_dir / "report.json"
    signed_report = report.read_bytes()
    report.write_bytes(signed_report + b" ")
    with pytest.raises(ValueError, match="not the report the receipt signed"):
        followup.sign_updates(run_dir, [referrals], signer=signer)
    report.write_bytes(signed_report)
    # A signed update that went missing (with its openings) breaks the history.
    two = run_dir / "receipt.update-2.json"
    two_private = run_dir / "receipt.update-2.private.json"
    kept = two.read_bytes(), two_private.read_bytes()
    two.unlink()
    two_private.unlink()
    with pytest.raises(ValueError, match="don't match the record"):
        followup.verify_history(run_dir, _registry(tmp_path))
    two.write_bytes(kept[0])
    two_private.write_bytes(kept[1])
    # One signer at a time: a held lock refuses, and signs nothing.
    lock = run_dir / ".receipt-update.lock"
    lock.write_text("")
    with pytest.raises(ValueError, match="another signer holds"):
        followup.sign_updates(run_dir, [referrals], signer=signer)
    lock.unlink()
    # A publication interrupted after its files but before the sidecar is
    # completed by the next call, never left as a broken history.
    import json as _json

    sidecar = run_dir / followup.SIDECAR
    data = _json.loads(sidecar.read_text())
    last = data["records"].pop()
    sidecar.write_text(_json.dumps(data))
    (run_dir / ".receipt-update.pending.json").write_text(
        _json.dumps(
            {
                "file": last["file"],
                "private": last["file"].replace(".json", ".private.json"),
                "lines": last["lines"],
            }
        )
    )
    with pytest.raises(ValueError):  # the history doesn't verify meanwhile
        followup.verify_history(run_dir, _registry(tmp_path))
    followup._finish_pending(run_dir, _registry(tmp_path))  # what the next call runs
    assert not (run_dir / ".receipt-update.pending.json").exists()
    assert followup.verify_history(run_dir, _registry(tmp_path)).ok
    # A tampered update already in the chain is never extended.
    update = run_dir / "receipt.update-2.json"
    good_update = update.read_text()
    one = json.loads((run_dir / "receipt.update-1.json").read_text())
    update.write_text(json.dumps(dict(json.loads(good_update), payload=one["payload"])))
    with pytest.raises(ValueError, match="does not verify"):
        followup.sign_updates(run_dir, [referrals], signer=signer)
    assert not (run_dir / "receipt.update-3.json").exists()


def test_handoff_list_signs_updates_and_shows_the_history(tmp_path, env, capsys):
    from openadapt_flow.__main__ import main
    from openadapt_flow.receipt_record import receipt_after_run

    run_dir = _run(tmp_path)
    referrals = HandoffLedger(tmp_path / "referrals")
    referral = referrals.send(
        "REF-1",
        FIELDS,
        counterparty="receiving clinic",
        system_kind="ehr",
        follow_up_hours=24,
        run_id=str(run_dir),
        step=0,
    )
    receipt_after_run(run_dir, env)
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))
    argv = [
        "handoff",
        "list",
        "--ledger",
        str(referrals.directory),
        "--run",
        str(run_dir),
    ]
    assert main([*argv, "--sign-updates"]) == 0
    out = capsys.readouterr().out
    assert "Signed receipt.update-1.json" in out
    assert "Signed history (receipt and 1 update(s), verified)" in out
    assert "Update 1: Confirmed in the other party's system" in out
    assert main(argv) == 0
    assert "Signed receipt.update" not in capsys.readouterr().out
    # A history that no longer verifies fails the command, and nothing new
    # is signed on top of it.
    update = run_dir / "receipt.update-1.json"
    envelope = json.loads(update.read_text())
    update.write_text(json.dumps(dict(envelope, signatures=[])))
    assert main(argv) == 1
    assert "Signed history: not verified" in capsys.readouterr().out
    intake.check(referrals, referral.id, "REF-1", Fake(_found(birth_date="1984-03-10")))
    assert main([*argv, "--sign-updates"]) == 2
    assert not (run_dir / "receipt.update-2.json").exists()


def test_sign_updates_signs_intermediate_transitions_in_order(tmp_path, env):
    """Issue #2475: a handoff that differs and then matches again signs both transitions."""
    from openadapt_flow.offline_seal import seal_home, signer_for
    from openadapt_flow.receipt_record import receipt_after_run

    run_dir = _run(tmp_path)
    referrals = HandoffLedger(tmp_path / "referrals")
    referral = referrals.send(
        "REF-1",
        FIELDS,
        counterparty="receiving clinic",
        system_kind="ehr",
        follow_up_hours=24,
        run_id=str(run_dir),
        step=0,
    )
    receipt_after_run(run_dir, env)
    signer = signer_for(seal_home(env))

    # 1. Confirm as intended and sign update-1.
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))
    first = followup.sign_updates(run_dir, [referrals], signer=signer)
    assert first is not None and first.name == "receipt.update-1.json"

    # 2. Record a differing read, then a matching read, before signing again.
    intake.check(referrals, referral.id, "REF-1", Fake(_found(family_name="Other")))
    intake.check(referrals, referral.id, "REF-1", Fake(_found()))

    last = followup.sign_updates(run_dir, [referrals], signer=signer)
    assert last is not None and last.name == "receipt.update-3.json"
    assert [p.name for p in followup.updates(run_dir)] == [
        "receipt.update-1.json",
        "receipt.update-2.json",
        "receipt.update-3.json",
    ]
    verified = followup.verify_history(run_dir, _registry(tmp_path))
    assert verified is not None and verified.ok

    # Calling sign_updates again with no new observations signs nothing.
    assert followup.sign_updates(run_dir, [referrals], signer=signer) is None
