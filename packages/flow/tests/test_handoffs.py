"""Handoffs and counterparty confirmations: unit tests.

Two producers over one mechanism: the deferred claim check seen as payer
handoffs (synthetic X12 999 and 277CA), and a field read-back from the
receiving system (a fake reader here; the real OpenEMR receiver is in
``test_handoffs_live_openemr.py``). Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.handoffs import intake, line, payer
from openadapt_flow.handoffs.ledger import HandoffLedger, LedgerError
from openadapt_flow.handoffs.readers import (
    FhirSearchProfile,
    FhirSearchReader,
    ReadResult,
)
from openadapt_flow.reports.parse import ReportFormat
from openadapt_flow.runtime.effects.deferred_report import PendingLedger
from tests.test_deferred_report import (
    ACK_277CA,
    ACK_999,
    FMT_277,
    HASH,
    SETTLE_AT,
    T0,
)

FMT_999 = ReportFormat(format="x12-999")
LATER = SETTLE_AT + timedelta(days=5)


# -- payer acknowledgments --------------------------------------------------------


def _claims(tmp_path: Path, fmt: ReportFormat, keys: list[str], hours: float = 48):
    pending = PendingLedger(tmp_path / "deferred")
    entries = {
        key: pending.add(
            key=key,
            effect_contract_hash=HASH,
            fmt=fmt,
            describe=f"payer {fmt.format}",
            deadline_hours=hours,
            now=T0,
        )
        for key in keys
    }
    return pending, entries


def _settle(pending: PendingLedger, text, fmt, now=SETTLE_AT):
    sha = None if text is None else "0" * 64
    recorder = payer.SettleRecorder(pending, fmt.format if fmt else "", sha)
    settled = pending.settle(text, fmt, now=now, observer=recorder)
    return settled, payer.handoff_ledger(pending)


def _h(handoffs: HandoffLedger, entry):
    return handoffs.get(payer.handoff_id(entry))


def test_a_submitted_claim_is_sent_with_follow_up_due_at_the_deadline(tmp_path):
    pending, entries = _claims(tmp_path, FMT_277, ["CLM-1001"])
    handoffs = payer.sync(pending)
    h = _h(handoffs, entries["CLM-1001"])
    assert h.state == "sent"
    assert h.follow_up_due == "2026-10-20T06:00:00Z"
    assert h.status(SETTLE_AT) == "waiting"
    assert h.status(LATER) == "needs_follow_up"
    assert h.system_kind == "payer_portal"
    assert "CLM-1001" not in (handoffs.directory / f"{h.id[8:]}.json").read_text()


def test_277ca_received_accepted_and_rejected(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002", "CLM-1003"])
    settled, handoffs = _settle(pending, ACK_277CA, FMT_277)
    # The deferred check settles exactly as before.
    assert {s.id: s.state for s in settled} == {
        e["CLM-1001"].id: "verified",
        e["CLM-1002"].id: "reconciliation_required",
    }
    accepted = _h(handoffs, e["CLM-1001"])
    rejected = _h(handoffs, e["CLM-1002"])
    received = _h(handoffs, e["CLM-1003"])
    assert (accepted.state, accepted.last.counterparty_accepted) == (
        "acknowledged",
        True,
    )
    assert accepted.status(LATER) == "done"
    assert (rejected.state, rejected.last.reason) == (
        "sent",
        "rejected_by_counterparty",
    )
    assert rejected.status(SETTLE_AT) == "needs_follow_up"
    assert (received.state, received.last.reason) == ("acknowledged", None)
    assert received.status(SETTLE_AT) == "waiting"
    assert received.status(LATER) == "needs_follow_up"
    for h in (accepted, rejected, received):
        assert h.last.read_method == "x12_277ca"
        assert h.state != "recorded_as_intended"  # status only, never fields


def test_999_accepted_file_is_acknowledged_and_rejected_file_is_not(tmp_path):
    pending, e = _claims(tmp_path, FMT_999, ["17:0001", "17:0002"])
    _, handoffs = _settle(pending, ACK_999, FMT_999)
    ok = _h(handoffs, e["17:0001"])
    bad = _h(handoffs, e["17:0002"])
    assert (ok.state, ok.last.counterparty_accepted) == ("acknowledged", False)
    assert (bad.state, bad.last.reason) == ("sent", "rejected_by_counterparty")


def test_the_same_report_twice_adds_nothing(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1003"])
    _settle(pending, ACK_277CA, FMT_277)
    _, handoffs = _settle(pending, ACK_277CA, FMT_277)
    assert len(_h(handoffs, e["CLM-1003"]).observations) == 1


def test_no_answer_by_the_deadline_keeps_the_state_and_needs_follow_up(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1003", "CLM-9999"], hours=8)
    _settle(pending, ACK_277CA, FMT_277)  # CLM-1003 received (A1)
    _, handoffs = _settle(pending, None, None, now=LATER)
    received = _h(handoffs, e["CLM-1003"])
    silent = _h(handoffs, e["CLM-9999"])
    assert (received.state, received.last.reason) == (
        "acknowledged",
        "no_answer_by_deadline",
    )
    assert (silent.state, silent.last.reason) == ("sent", "no_answer_by_deadline")
    assert received.status(LATER) == silent.status(LATER) == "needs_follow_up"


def test_two_claims_on_one_key_are_ambiguous(tmp_path):
    pending, _ = _claims(tmp_path, FMT_277, ["CLM-1001"])
    twin = pending.add(
        key="CLM-1001",
        effect_contract_hash=HASH,
        fmt=FMT_277,
        describe="payer x12-277ca",
        deadline_hours=48,
        now=T0,
    )
    _, handoffs = _settle(pending, ACK_277CA, FMT_277)
    h = _h(handoffs, twin)
    assert (h.state, h.last.reason) == ("sent", "ambiguous_key")
    assert h.status(SETTLE_AT) == "needs_follow_up"


def test_sync_maps_claims_settled_before_handoffs_existed(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002"])
    pending.settle(ACK_277CA, FMT_277, now=SETTLE_AT)  # no observer
    handoffs = payer.sync(pending)
    assert _h(handoffs, e["CLM-1001"]).last.counterparty_accepted
    assert _h(handoffs, e["CLM-1002"]).last.reason == "rejected_by_counterparty"


def test_export_reports_are_not_counterparty_handoffs_by_default(tmp_path):
    fmt = ReportFormat(format="csv", key_column="k", status_column="s", accept=["ok"])
    pending, _ = _claims(tmp_path, fmt, ["ORD-1"])
    assert payer.sync(pending).entries() == []
    assert len(payer.sync(pending, exports=True).entries()) == 1


# -- read-back of what the receiver recorded -----------------------------------


FIELDS = {
    "given_name": ("Avery", "text"),
    "family_name": ("Quill", "text"),
    "birth_date": ("1984-03-09", "date"),
    "sex": ("female", "code"),
}
RECORD = {
    "given_name": "Avery",
    "family_name": "Quill",
    "birth_date": "1984-03-09",
    "sex": "female",
}


class Fake:
    read_method = "api_readback"

    def __init__(self, result: ReadResult) -> None:
        self.result = result

    def read(self, key: str) -> ReadResult:
        return self.result


def _referral(tmp_path: Path, key: str = "REF-0001", hours: float = 24):
    ledger = HandoffLedger(tmp_path / "handoffs")
    h = ledger.send(
        key,
        FIELDS,
        counterparty="receiving clinic",
        system_kind="ehr",
        follow_up="referrals desk",
        follow_up_hours=hours,
        run_id="run-1",
        step=2,
    )
    return ledger, h


def _found(**changes):
    return ReadResult("found", fields={**RECORD, **changes}, evidence_sha256="1" * 64)


def test_recorded_as_intended(tmp_path):
    ledger, h = _referral(tmp_path)
    after = intake.check(ledger, h.id, "REF-0001", Fake(_found()))
    assert after.state == "recorded_as_intended"
    assert after.status() == "done"
    assert after.last.strength == "field_readback"


@pytest.mark.parametrize(
    "changes,differs",
    [
        ({"birth_date": "1984-03-10"}, ["birth_date"]),
        ({"birth_date": "1984-09-03"}, ["birth_date"]),  # day and month swapped
        ({"family_name": "Quil"}, ["family_name"]),
        ({"sex": "male"}, ["sex"]),
        ({"given_name": None}, ["given_name"]),  # dropped field
        ({"birth_date": "unknown"}, ["birth_date"]),  # unparseable
        ({"birth_date": "1984-03-10", "sex": "male"}, ["birth_date", "sex"]),
    ],
)
def test_recorded_differently_names_only_the_field_ids(tmp_path, changes, differs):
    ledger, h = _referral(tmp_path)
    after = intake.check(ledger, h.id, "REF-0001", Fake(_found(**changes)))
    assert after.state == "recorded_differently"
    assert after.last.differing_fields == differs
    assert after.status() == "needs_follow_up"
    text = (ledger.directory / f"{h.id[8:]}.json").read_text()
    for value in [*RECORD.values(), *changes.values(), "REF-0001"]:
        if value and len(value) > 3:
            assert value not in text


@pytest.mark.parametrize(
    "changes",
    [
        {"given_name": "AVERY", "family_name": "  quill "},
        {"birth_date": "1984-03-09T00:00:00"},
        {"birth_date": "March 9, 1984"},
        {"sex": "FEMALE"},
    ],
)
def test_benign_formatting_is_no_false_alarm(tmp_path, changes):
    ledger, h = _referral(tmp_path)
    after = intake.check(ledger, h.id, "REF-0001", Fake(_found(**changes)))
    assert after.state == "recorded_as_intended", after.last.differing_fields


def test_never_arrived_stays_sent_and_is_due(tmp_path):
    ledger, h = _referral(tmp_path, hours=1)
    after = intake.check(ledger, h.id, "REF-0001", Fake(ReadResult("not_found")))
    assert (after.state, after.last.reason) == ("sent", "not_found")
    assert after.status() == "waiting"
    assert after.status(datetime.now(timezone.utc) + timedelta(hours=2)) == (
        "needs_follow_up"
    )


def test_ambiguous_and_unreachable_confirm_nothing(tmp_path):
    ledger, h = _referral(tmp_path)
    after = intake.check(ledger, h.id, "REF-0001", Fake(ReadResult("ambiguous")))
    assert (after.state, after.last.reason) == ("sent", "ambiguous_key")
    assert after.status() == "needs_follow_up"
    after = intake.check(ledger, h.id, "REF-0001", Fake(ReadResult("unreachable")))
    assert after.last.reason == "receiver_unreachable"
    assert after.state == "sent"


def test_unreachable_never_undoes_a_confirmation_but_not_found_does(tmp_path):
    ledger, h = _referral(tmp_path)
    intake.check(ledger, h.id, "REF-0001", Fake(_found()))
    after = intake.check(ledger, h.id, "REF-0001", Fake(ReadResult("unreachable")))
    assert after.state == "recorded_as_intended" and after.status() == "done"
    after = intake.check(ledger, h.id, "REF-0001", Fake(ReadResult("not_found")))
    assert after.state == "sent"


def test_the_ledger_refuses_what_it_could_never_compare(tmp_path):
    ledger = HandoffLedger(tmp_path / "handoffs")
    with pytest.raises(LedgerError):
        ledger.send(
            "K", {"d": ("03/04/2020", "date")}, counterparty="x", follow_up_hours=1
        )
    ledger.send("K", {"d": ("2020-03-04", "date")}, counterparty="x", follow_up_hours=1)
    with pytest.raises(LedgerError):  # a second open handoff on the same key
        ledger.send(
            "K", {"d": ("2020-03-04", "date")}, counterparty="x", follow_up_hours=1
        )
    ledger2, h = _referral(tmp_path / "b")
    with pytest.raises(LedgerError):
        intake.check(ledger2, h.id, "REF-OTHER", Fake(_found()))
    assert ledger2.directory.stat().st_mode & 0o777 == 0o700


# -- the FHIR reader ---------------------------------------------------------------

PROFILE = FhirSearchProfile(
    resource="Patient",
    key_param="identifier",
    key_path="identifier.value",
    fields={
        "family_name": {"path": "name.family"},
        "birth_date": {"path": "birthDate", "kind": "date"},
    },
)


def _patient(key: str, family: str = "Quill") -> dict:
    return {
        "resourceType": "Patient",
        "identifier": [{"value": key}],
        "name": [{"family": family}],
        "birthDate": "1984-03-09",
    }


class Client:
    def __init__(self, *pages, status: int = 200) -> None:
        self.pages = list(pages)
        self.status = status
        self.calls: list = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        body = self.pages.pop(0) if self.pages else {}
        return SimpleNamespace(
            status_code=self.status, content=json.dumps(body).encode()
        )


def _bundle(*resources, next_url=None):
    out = {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}
    if next_url:
        out["link"] = [{"relation": "next", "url": next_url}]
    return out


def _reader(client):
    return FhirSearchReader(
        PROFILE, "https://receiver.test/fhir", token="t", client=client
    )


def test_fhir_reader_found_not_found_and_key_check():
    found = _reader(Client(_bundle(_patient("REF-1")))).read("REF-1")
    assert found.status == "found"
    assert found.fields == {"family_name": "Quill", "birth_date": "1984-03-09"}
    # A server that ignores the search parameter returns everyone: only the
    # record that carries the key counts.
    noisy = _reader(Client(_bundle(_patient("REF-2"), _patient("REF-1")))).read("REF-1")
    assert noisy.status == "found"
    assert _reader(Client(_bundle(_patient("REF-2")))).read("REF-1").status == (
        "not_found"
    )


def test_fhir_reader_ambiguous_across_pages_and_fails_closed():
    client = Client(
        _bundle(_patient("REF-1"), next_url="https://receiver.test/fhir/Patient?p=2"),
        _bundle(_patient("REF-1", "Other")),
    )
    assert _reader(client).read("REF-1").status == "ambiguous"
    assert client.calls[0][2]["Authorization"] == "Bearer t"
    assert _reader(Client(_bundle(), status=401)).read("REF-1").status == "unreachable"
    assert _reader(Client({"resourceType": "Patient"})).read("REF-1").status == (
        "unreachable"
    )
    off_origin = Client(_bundle(next_url="https://elsewhere.test/fhir/Patient?p=2"))
    assert _reader(off_origin).read("REF-1").status == "unreachable"


# -- the receipt line ----------------------------------------------------------------


def test_lines_follow_the_schema_rules(tmp_path):
    ledger, h = _referral(tmp_path)
    assert line.to_line(h).line["state"] == "sent"
    assert line.to_line(h).line["strength"] == "none"
    h = intake.check(ledger, h.id, "REF-0001", Fake(_found(birth_date="1984-03-10")))
    item = line.to_line(h)
    assert item.line == {
        "kind": "counterparty_confirmation",
        "for_step": 2,
        "system_kind": "ehr",
        "read_method": "api",
        "state": "recorded_differently",
        "strength": "read_back",
        "differing_count": 1,
    }
    assert item.values["counterparty"].startswith("ehr:cp-")
    assert [
        v.endswith(":differs")
        for k, v in sorted(item.values.items())
        if k.startswith("field.")
    ] == [
        True,
        False,
        False,
        False,
    ]
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002"])
    _, handoffs = _settle(pending, ACK_277CA, FMT_277)
    accepted = line.to_line(_h(handoffs, e["CLM-1001"])).line
    rejected = line.to_line(_h(handoffs, e["CLM-1002"]))
    assert (accepted["state"], accepted["read_method"], accepted["strength"]) == (
        "acknowledged",
        "acknowledgment_message",
        "acknowledgment",
    )
    assert rejected.line["state"] == "sent"
    assert rejected.values["reason"] == "rejected_by_counterparty"


def test_lines_validate_against_the_receipt_schema_and_sign(tmp_path):
    r = pytest.importorskip("openadapt_types.receipt")
    if not hasattr(r, "Confirmation"):
        pytest.skip("openadapt-types without counterparty confirmations")
    pytest.importorskip("cryptography")
    assert {k: set(v) for k, v in line.REASONS_FOR_STATE.items()} == {
        k: set(v) for k, v in r.REASONS_FOR_STATE.items()
    }
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from openadapt_flow.receipt_record import build_statement

    ledger, h = _referral(tmp_path)
    states = [h]
    states.append(intake.check(ledger, h.id, "REF-0001", Fake(_found())))
    ledger2, h2 = _referral(tmp_path / "2")
    states.append(intake.check(ledger2, h2.id, "REF-0001", Fake(_found(sex="male"))))
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002", "CLM-1003"])
    _, handoffs = _settle(pending, ACK_277CA, FMT_277)
    states += [_h(handoffs, x) for x in e.values()]
    report = {
        "run_id": "run-1",
        "execution_outcome": "VERIFIED",
        "results": [
            {"ok": True},
            {"ok": True},
            {"ok": True, "effect_verified": True, "effect_field_checks": [1]},
        ],
    }
    statement, openings = build_statement(report, "a" * 64, flow_version="3.4.0")
    statement, openings = line.attach_confirmations(statement, openings, states)
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = r.key_id(raw)
    envelope = r.sign_statement(statement, [(kid, key.sign)])
    registry = r.parse_registry(
        {
            "schema": r.REGISTRY_SCHEMA,
            "keys": [
                {
                    "key_id": kid,
                    "public_key": base64.b64encode(raw).decode(),
                    "role": "this_computer",
                }
            ],
        }
    )
    result = r.verify_receipt(
        envelope, registry, openings=r.private_openings(envelope, openings)
    )
    assert result.ok, (result.reason, result.detail)
    assert len(result.confirmation_lines) == 6
    assert result.confirmation_lines[1].startswith(
        "Confirmed in the other party's system"
    )
    assert "1 field differs" in result.confirmation_lines[2]
    assert result.confirmation_lines[3].startswith("Acknowledged by the other party")
    assert result.confirmation_lines[4].startswith("Sent to the other party")


# -- the CLI -----------------------------------------------------------------------


def test_handoff_list_groups_by_who_must_follow_up(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    ledger, h = _referral(tmp_path)
    intake.check(ledger, h.id, "REF-0001", Fake(_found(birth_date="1984-03-10")))
    pending, _ = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002", "CLM-1003"])
    _settle(pending, ACK_277CA, FMT_277)
    code = main(
        [
            "handoff",
            "list",
            "--ledger",
            str(ledger.directory),
            "--ledger",
            str(pending.directory),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    needs = out.split("Needs follow-up")[1].split("Waiting")[0]
    assert "recorded differently: birth_date" in needs
    assert "who: referrals desk" in needs
    assert "they refused it" in needs and "who: billing" in needs
    assert "CLM-" not in out and "REF-0001" not in out and "1984" not in out
    assert "Done: 1 (--all lists them)" in out
    code = main(["handoff", "list", "--ledger", str(ledger.directory), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["receipt_line"]["state"] == "recorded_differently"
    assert data[0]["differing_fields"] == ["birth_date"]


def test_handoff_list_for_one_run_sets_the_claims_step(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001"])
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "run_id": "r",
        "results": [
            {"ok": True},
            {"ok": True, "effect_evidence": [{"deferred_check": e["CLM-1001"].id}]},
        ],
    }
    (run / "report.json").write_text(json.dumps(report))
    assert (
        main(
            [
                "handoff",
                "list",
                "--ledger",
                str(pending.directory),
                "--run",
                str(run),
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert [d["receipt_line"]["for_step"] for d in data] == [1]
    assert data[0]["state"] == "sent"


def test_a_failing_observer_never_changes_what_settles(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002", "CLM-1003"])

    def broken(entry, row, count):
        raise OSError("disk full")

    settled = pending.settle(ACK_277CA, FMT_277, now=SETTLE_AT, observer=broken)
    assert {s.id: s.state for s in settled} == {
        e["CLM-1001"].id: "verified",
        e["CLM-1002"].id: "reconciliation_required",
    }
    # The handoffs catch up from the settled entries.
    handoffs = payer.sync(pending)
    assert _h(handoffs, e["CLM-1001"]).last.counterparty_accepted
    assert _h(handoffs, e["CLM-1002"]).last.reason == "rejected_by_counterparty"


def test_sync_catches_up_a_settlement_the_observer_missed(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1003"], hours=8)
    _settle(pending, ACK_277CA, FMT_277)  # A1: acknowledged
    pending.settle(None, None, now=LATER)  # expired by a caller with no observer
    handoffs = payer.sync(pending)
    h = _h(handoffs, e["CLM-1003"])
    assert (h.state, h.last.reason) == ("acknowledged", "no_answer_by_deadline")
    before = len(h.observations)
    assert len(_h(payer.sync(pending), e["CLM-1003"]).observations) == before


def test_sync_adds_nothing_after_the_observer_recorded_it(tmp_path):
    pending, e = _claims(tmp_path, FMT_277, ["CLM-1001", "CLM-1002"])
    _, handoffs = _settle(pending, ACK_277CA, FMT_277)
    counts = {k: len(_h(handoffs, v).observations) for k, v in e.items()}
    handoffs = payer.sync(pending)
    assert {k: len(_h(handoffs, v).observations) for k, v in e.items()} == counts


def test_a_refused_claim_reads_as_refused_not_unanswered(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    pending, _ = _claims(tmp_path, FMT_277, ["CLM-1002"])
    _settle(pending, ACK_277CA, FMT_277)
    assert main(["handoff", "list", "--ledger", str(pending.directory)]) == 0
    out = capsys.readouterr().out
    assert "sent; they refused it" in out
    assert "no answer yet" not in out
