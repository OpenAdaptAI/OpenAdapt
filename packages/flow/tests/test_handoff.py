"""Agent-to-agent handoffs: a payload with its run's receipt, verified on arrival.

Receipts come from Flow's real writer (``write_receipt_record``) over a run
report, or from the shared ``record/run/v1`` vectors for confirmation lines.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from openadapt_types import receipt as r  # noqa: E402

from openadapt_flow import handoff as h  # noqa: E402
from openadapt_flow.__main__ import main as flow_main  # noqa: E402
from openadapt_flow.receipt_record import PRIVATE_FILE, write_receipt_record  # noqa: E402
from openadapt_flow.seal_keys import SoftwareSigner  # noqa: E402

VECTORS = (
    Path(__file__).resolve().parents[2] / "types" / "tests" / "vectors" / "record-run-v1"
)

REPORT = {
    "workflow_name": "Demo Triage",
    "execution_outcome": "VERIFIED",
    "run_id": "run-handoff-1",
    "started_at": "2026-11-21T01:02:03Z",
    "results": [
        {"step_id": "s1", "ok": True},
        {"step_id": "s2", "ok": True, "effect_verified": True, "postconditions_ok": True},
    ],
}


def _signer() -> SoftwareSigner:
    return SoftwareSigner(Ed25519PrivateKey.generate(), "generated")


def _registry(*signers: SoftwareSigner) -> list[r.RoleKey]:
    return r.parse_registry(
        {
            "schema": r.REGISTRY_SCHEMA,
            "keys": [{**s.registry_entry(), "role": "this_computer"} for s in signers],
        }
    )


@pytest.fixture()
def signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SoftwareSigner:
    """This computer's receipt key, in a throwaway seal home."""
    from openadapt_flow.offline_seal import signer_for

    home = tmp_path / "seal-home"
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(home))
    monkeypatch.delenv("OPENADAPT_SEAL_KEY", raising=False)
    return signer_for(home)


@pytest.fixture()
def run_dir(tmp_path: Path, signer: SoftwareSigner) -> Path:
    run = tmp_path / "run-handoff-1"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(REPORT, indent=2))
    write_receipt_record(run, signer=signer, flow_version="3.4.0")
    return run


def _bound(payload, run_dir, signer, **kwargs):
    return h.make_handoff(payload, h.receipt_for_run(run_dir), signer=signer, **kwargs)


def test_a_good_handoff_says_who_signed_and_what_that_does_not_prove(run_dir, signer):
    doc = _bound({"outcome": "done", "count": 3}, run_dir, signer)
    verdict = h.verify_handoff(doc, _registry(signer))
    assert verdict.ok and verdict.integrity == "intact"
    assert verdict.signer_roles == ["this_computer"]
    assert verdict.signer_lines == ["Signed by this computer"]
    assert verdict.outcome == "VERIFIED"
    assert verdict.checks["writes"] == {"confirmed": 1, "refuted": 0, "not_checked": 0}
    assert "field_values" in verdict.checks["not_checked"]
    # The honesty rule: a signature proves who signed, not that it is true.
    assert verdict.what_this_proves[0] == h.WHAT_A_SIGNATURE_PROVES
    assert "does not prove" in verdict.what_this_proves[0]
    # The sender's key bound these exact bytes as the payload subject.
    assert verdict.payload["bound"] is True
    assert verdict.payload["media_type"] == "application/json"
    assert verdict.confirmations == [] and verdict.independent_confirmations == 0


def test_binding_keeps_the_signer_and_names_the_payload(run_dir, signer):
    original = h.receipt_for_run(run_dir)
    doc = _bound(b"%PDF-1.7 claim", run_dir, signer, media_type="application/pdf")
    statement = json.loads(base64.b64decode(doc["receipt"]["payload"]))
    (payload,) = [s for s in statement["subject"] if s["name"] == "payload"]
    assert payload["media_type"] == "application/pdf"
    assert [s["keyid"] for s in doc["receipt"]["signatures"]] == [
        s["keyid"] for s in original["signatures"]
    ]
    # A key that didn't sign the run can't bind a payload to it.
    with pytest.raises(h.HandoffError, match="didn't sign that receipt"):
        h.make_handoff({"x": 1}, original, signer=_signer())


def test_an_unbound_receipt_is_refused_once_the_payload_is_checked(run_dir, signer):
    """Missing: a receipt that names no payload doesn't vouch for this one."""
    doc = h.make_handoff({"outcome": "done"}, h.receipt_for_run(run_dir))
    verdict = h.verify_handoff(doc, _registry(signer))
    assert not verdict.ok and verdict.reason == "payload_not_bound"


def test_a_tampered_receipt_is_refused(run_dir, signer):
    doc = _bound({"outcome": "done"}, run_dir, signer)
    statement = json.loads(base64.b64decode(doc["receipt"]["payload"]))
    statement["predicate"]["model_calls"] = 7
    doc["receipt"]["payload"] = base64.b64encode(
        r.canonical_json(statement).encode()
    ).decode()
    verdict = h.verify_handoff(doc, _registry(signer))
    assert not verdict.ok
    assert verdict.reason == "bad_signature"
    assert verdict.signer_lines == [
        "Not verified: the receipt was changed after it was signed"
    ]


def test_an_unknown_signer_is_refused_even_when_the_signature_is_valid(run_dir, signer):
    doc = _bound({"outcome": "done"}, run_dir, signer)
    verdict = h.verify_handoff(doc, _registry(_signer()))
    assert not verdict.ok and verdict.reason == "unknown_signer"
    assert verdict.signer_lines == [r.UNKNOWN_SIGNER_LINE]
    assert h.verify_handoff(doc, []).reason == "unknown_signer"


def test_a_swapped_payload_is_refused(run_dir, signer):
    """Mismatched: the receipt signed other bytes than the ones received."""
    doc = _bound({"outcome": "done"}, run_dir, signer)
    # Swap the content but keep the old digest: the wrapper no longer matches.
    swapped = json.loads(json.dumps(doc))
    swapped["payload"]["content"] = {"outcome": "done", "amount": 1_000_000}
    verdict = h.verify_handoff(swapped, _registry(signer))
    assert not verdict.ok and verdict.reason == "payload_mismatch"
    # Re-staple the bound receipt to a new payload with its own digest: the
    # receipt names other bytes, so it is refused too.
    restapled = h.make_handoff({"outcome": "done", "amount": 1_000_000}, doc["receipt"])
    verdict = h.verify_handoff(restapled, _registry(signer))
    assert not verdict.ok and verdict.reason == "payload_mismatch"


def test_private_openings_never_travel(run_dir):
    private = json.loads((run_dir / PRIVATE_FILE).read_text())
    with pytest.raises(h.HandoffError, match="never leaves this computer"):
        h.make_handoff({"x": 1}, private)
    doc = h.make_handoff({"x": 1}, h.receipt_for_run(run_dir))
    doc["receipt"] = private
    assert h.verify_handoff(doc, []).reason == "malformed"


def test_a_handoff_with_extra_fields_or_another_schema_is_refused(run_dir, signer):
    doc = _bound({"x": 1}, run_dir, signer)
    assert h.verify_handoff({**doc, "signer_role": "openadapt_cloud"}, []).reason == (
        "malformed"
    )
    assert h.verify_handoff({**doc, "schema": "other/v1"}, []).reason == "unknown_schema"
    assert h.verify_handoff(b"not json", []).reason == "malformed"


def test_a_detached_handoff_checks_the_file_beside_it(run_dir, signer, tmp_path):
    attachment = tmp_path / "claim.pdf"
    attachment.write_bytes(b"%PDF-1.7 synthetic")
    doc = _bound(
        attachment.read_bytes(),
        run_dir,
        signer,
        name=str(attachment),
        detached=True,
        media_type="application/pdf",
    )
    assert "content" not in doc["payload"] and "content_base64" not in doc["payload"]
    assert doc["payload"]["name"] == "claim.pdf"
    good = h.verify_handoff(doc, _registry(signer), payload=attachment.read_bytes())
    assert good.ok and good.payload["bound"] is True
    bad = h.verify_handoff(doc, _registry(signer), payload=b"another file")
    assert not bad.ok and bad.reason == "payload_mismatch"
    missing = h.verify_handoff(doc, _registry(signer))
    assert missing.ok and missing.payload["present"] is False
    assert missing.payload["bound"] is False


def _vector(name: str) -> dict:
    return json.loads((VECTORS / name).read_text())


def _vector_registry(case: dict) -> list[r.RoleKey]:
    return r.parse_registry(json.loads((VECTORS / case["registry"]).read_text()))


@pytest.mark.parametrize(
    ("name", "independent"),
    [
        ("confirmation-recorded-as-intended.json", True),
        ("confirmation-recorded-differently.json", True),
        ("confirmation-sent.json", False),
        ("confirmation-acknowledged.json", False),
    ],
)
def test_confirmation_lines_come_from_the_shared_verifier(name, independent):
    case = _vector(name)
    # The vectors' keys aren't ours to bind with, so the payload stays detached.
    doc = h.make_handoff(b"claim", case["document"], detached=True)
    verdict = h.verify_handoff(doc, _vector_registry(case))
    assert verdict.ok
    assert [c["independent"] for c in verdict.confirmations] == [independent]
    # Without the private openings the line carries no time; otherwise it is
    # the shared verifier's own line.
    expected = r.verify_receipt(case["document"], _vector_registry(case))
    assert verdict.confirmation_lines == list(expected.confirmation_lines)
    assert verdict.signer_lines == ["Signed by this computer"]
    if independent:
        assert verdict.independent_confirmations == 1
        assert any("read back from the other party" in line for line in verdict.what_this_proves)
    else:
        assert any("rests on the signer's word" in line for line in verdict.what_this_proves)


def test_a_tampered_confirmation_is_refused():
    case = _vector("confirmation-tampered.json")
    doc = h.make_handoff(b"claim", case["document"], detached=True)
    assert h.verify_handoff(doc, _vector_registry(case)).reason == "bad_signature"


def test_cli_send_and_verify_inline_and_detached(run_dir, signer, tmp_path, capsys):
    keys = tmp_path / "keys.json"
    keys.write_text(
        json.dumps(
            {
                "schema": r.REGISTRY_SCHEMA,
                "keys": [{**signer.registry_entry(), "role": "this_computer"}],
            }
        )
    )
    payload = tmp_path / "result.json"
    payload.write_text('{"outcome": "done"}')
    out = tmp_path / "result.handoff.json"
    assert flow_main(["handoff", "send", str(payload), "--run", str(run_dir), "--out", str(out)]) == 0
    assert flow_main(["handoff", "verify", str(out), "--keys", str(keys), "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["integrity"] == "intact" and verdict["signer_roles"] == ["this_computer"]

    assert flow_main(["handoff", "send", str(payload), "--run", str(run_dir), "--detached"]) == 0
    detached = h.detached_path(payload)
    assert detached.is_file()
    assert flow_main(["handoff", "verify", str(detached), "--keys", str(keys)]) == 0
    text = capsys.readouterr().out
    assert "Signed by this computer" in text and h.WHAT_A_SIGNATURE_PROVES in text
    payload.write_text('{"outcome": "done", "changed": true}')
    assert flow_main(["handoff", "verify", str(detached), "--keys", str(keys)]) == 1

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": r.REGISTRY_SCHEMA, "keys": []}))
    assert flow_main(["handoff", "verify", str(out), "--keys", str(other)]) == 1


def test_cli_key_and_trust_pin_the_other_party_by_name(run_dir, signer, tmp_path, capsys):
    key = tmp_path / "sender-key.json"
    key.write_text(json.dumps({k: v for k, v in signer.registry_entry().items() if k != "status"}))
    keys = tmp_path / "trusted.json"
    args = ["handoff", "trust", str(key), "--org", "Example Clinic", "--keys", str(keys)]
    assert flow_main(args) == 0
    registry = r.parse_registry(json.loads(keys.read_text()))
    assert [(k.role, k.org) for k in registry] == [("customer_key", "Example Clinic")]
    doc = _bound({"outcome": "done"}, run_dir, signer)
    assert h.verify_handoff(doc, registry).signer_lines == ["Signed by Example Clinic's key"]
    bad = ["handoff", "trust", str(key), "--org", "x" * 80, "--keys", str(keys)]
    assert flow_main(bad) == 2
    key.write_text('{"key_id": "seal-ed25519-0000000000000000", "public_key": "AAAA"}')
    assert flow_main(args) == 2
    capsys.readouterr()


def test_cli_send_refuses_the_private_file_and_plain_http(run_dir, tmp_path, capsys):
    payload = tmp_path / "p.json"
    payload.write_text("{}")
    code = flow_main(
        ["handoff", "send", str(payload), "--receipt", str(run_dir / PRIVATE_FILE)]
    )
    assert code == 2
    assert "never leaves this computer" in capsys.readouterr().err
    from openadapt_flow.cli_handoff import _post_allowed

    assert _post_allowed("https://agent.example/inbox")
    assert _post_allowed("http://127.0.0.1:9000/inbox")
    assert not _post_allowed("http://agent.example/inbox")
    assert not _post_allowed("file:///etc/passwd")


def test_cli_send_posts_to_a_local_webhook(run_dir, signer, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[tuple[str, bytes]] = []

    class Inbox(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers["Content-Type"], body))
            self.send_response(202)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Inbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = tmp_path / "p.json"
        payload.write_text('{"outcome": "done"}')
        url = f"http://127.0.0.1:{server.server_address[1]}/inbox"
        assert flow_main(["handoff", "send", str(payload), "--run", str(run_dir), "--post", url]) == 0
    finally:
        server.shutdown()
    content_type, body = received[0]
    assert content_type == h.MEDIA_TYPE
    # The webhook receiver verifies exactly what arrived.
    assert h.verify_handoff(body, _registry(signer)).ok


def test_decide_accepts_only_what_the_receivers_own_record_shows(run_dir, signer):
    registry = _registry(signer)
    receipt = h.receipt_for_run(run_dir)
    records = {"ref-1": "note A", "ref-2": "note B"}

    def own_record(claim):
        value = records.get(claim["ref"])
        if value is None:
            return h.ReceiverRecord("not_found")
        if value != claim["note"]:
            return h.ReceiverRecord("recorded_differently", ("note",))
        return h.ReceiverRecord("recorded_as_intended")

    def decision(claim, check=own_record):
        return h.decide(h.make_handoff(claim, receipt, signer=signer), registry, check)

    true = decision({"ref": "ref-1", "note": "note A"})
    assert true.decision == "accept" and true.what_this_proves[-1] == h.ACCEPT_LINE
    assert true.receiver_check == {
        "state": "recorded_as_intended",
        "differing_fields": [],
        "read_method": "api",
    }
    # A valid signature from a trusted sender does not save a false claim.
    assert decision({"ref": "ref-9", "note": "x"}).decision == "reject"
    differing = decision({"ref": "ref-2", "note": "note C"})
    assert differing.decision == "reject"
    assert differing.receiver_check["differing_fields"] == ["note"]

    def broken(claim):
        raise OSError("database is locked")

    assert decision({"ref": "ref-1", "note": "note A"}, broken).decision == "reject"
    # No own read: never accepted on the receipt alone.
    unchecked = h.decide(h.make_handoff({"ref": "ref-1"}, receipt, signer=signer), registry)
    assert unchecked.decision == "unchecked" and unchecked.what_this_proves[-1] == h.UNCHECKED_LINE
    # A refused receipt is rejected before the receiver reads anything.
    calls = []
    refused = h.decide(h.make_handoff({"ref": "ref-1"}, receipt), [], calls.append)
    assert refused.decision == "reject" and calls == []


def test_the_handoff_group_is_shared_whichever_module_registers_first():
    import argparse

    from openadapt_flow.cli_handoff import handoff_group, register

    for list_first in (True, False):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        if list_first:
            handoff_group(sub).add_parser("list")
        register(sub)
        if not list_first:
            handoff_group(sub).add_parser("list")
        assert sorted(handoff_group(sub).choices) == ["key", "list", "send", "trust", "verify"]


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("payload-bound.json", None),
        ("payload-mismatch.json", "payload_mismatch"),
        ("payload-missing.json", "payload_not_bound"),
    ],
)
def test_the_shared_payload_vectors_give_the_same_answer_through_a_handoff(name, reason):
    case = _vector(name)
    received = base64.b64decode(case["received"])
    doc = h.make_handoff(received, case["document"], detached=True)
    verdict = h.verify_handoff(doc, _vector_registry(case), payload=received)
    assert verdict.reason == reason
    assert verdict.signer_lines == case["expected"]["signer_lines"]
    if reason is None:
        assert verdict.ok and verdict.payload["bound"] is True


def test_send_records_the_handoff_in_the_ledger_handoff_list_reads(run_dir, signer, tmp_path):
    ledger_mod = pytest.importorskip("openadapt_flow.handoffs.ledger")
    payload = tmp_path / "referral.json"
    payload.write_text(json.dumps({"ref": "ref-77", "note": "Follow-up in 2 weeks", "n": 3}))
    ledger = tmp_path / "ledger"
    args = ["handoff", "send", str(payload), "--run", str(run_dir), "--out", str(tmp_path / "o.json")]
    assert flow_main([*args, "--ledger", str(ledger), "--counterparty", "receiving clinic",
                      "--key-field", "ref"]) == 0
    (entry,) = ledger_mod.HandoffLedger(ledger).entries()
    assert entry.counterparty == "receiving clinic"
    assert entry.source == "agent_handoff" and entry.run_id == run_dir.name
    assert sorted(entry.fields) == ["note"]  # text fields only, as digests
    stored = "".join(p.read_text() for p in ledger.rglob("*.json"))
    assert "Follow-up" not in stored and "ref-77" not in stored
    assert entry.key_digest == ledger_mod.HandoffLedger(ledger).digest("ref-77")
    # A key that another open handoff uses is refused, and nothing is sent.
    assert flow_main([*args, "--ledger", str(ledger), "--counterparty", "x", "--key-field", "ref"]) == 2
    assert flow_main([*args, "--counterparty", "x"]) == 2
