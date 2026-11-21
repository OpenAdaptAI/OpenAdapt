"""The shared record/run/v1 vectors, checked against the Python verifier."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from openadapt_types import receipt as r  # noqa: E402

VECTORS = Path(__file__).parent / "vectors" / "record-run-v1"
NAMES = sorted(
    p.stem
    for p in VECTORS.glob("*.json")
    if not p.name.startswith("registry")
    and p.stem != "jcs"
    and not p.stem.startswith("chain-")
)


def _registry():
    return r.parse_registry(json.loads((VECTORS / "registry.json").read_text()))


def test_the_vector_set_is_complete():
    for required in (
        "valid",
        "tampered",
        "unknown-signer",
        "demo-key",
        "public-redaction",
        "cloud-countersign",
        "embedded-image-in-card",
        "embedded-image-in-record",
        "confirmation-recorded-as-intended",
        "confirmation-recorded-differently",
        "confirmation-acknowledged",
        "confirmation-sent",
        "confirmation-tampered",
        "confirmation-public-redaction",
        "confirmation-reason-no-answer",
        "payload-bound",
        "payload-mismatch",
        "payload-missing",
        "approval-public-card",
        "approval-none",
    ):
        assert required in NAMES


@pytest.mark.parametrize("name", NAMES)
def test_vector(name):
    vector = json.loads((VECTORS / f"{name}.json").read_text())
    received = vector.get("received")
    result = r.verify_receipt(
        vector["document"],
        _registry(),
        openings=vector.get("openings"),
        received=base64.b64decode(received) if received is not None else None,
    )
    expected = vector["expected"]
    assert result.ok is expected["ok"], result.detail
    assert result.reason == expected["reason"]
    assert list(result.signer_lines) == expected["signer_lines"]
    if expected["ok"]:
        assert result.outcome == expected["outcome"]
        assert result.demo is expected["demo"]
        assert dict(result.disclosed) == expected["disclosed"]
        assert list(result.confirmation_lines) == expected.get("confirmation_lines", [])
        assert list(result.independent) == expected.get("independent", [])
        # A confirmation line never joins or upgrades the signer lines.
        assert not set(result.confirmation_lines) & set(result.signer_lines)
        if "approval_line" in expected:
            assert result.approval_line == expected["approval_line"]
        assert result.payload_bound == expected.get("payload_bound")
    else:
        assert result.outcome is None


def test_vectors_are_byte_stable(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", VECTORS / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for name, vector in gen.build().items():
        on_disk = (VECTORS / f"{name}.json").read_text()
        assert json.dumps(vector, indent=2, sort_keys=True) + "\n" == on_disk, name


def test_unknown_role_refuses_the_registry():
    data = json.loads((VECTORS / "registry-unknown-role.json").read_text())
    with pytest.raises(r.RegistryError) as info:
        r.parse_registry(data)
    assert info.value.reason == "unknown_role"


def test_an_empty_registry_trusts_nothing():
    vector = json.loads((VECTORS / "valid.json").read_text())
    result = r.verify_receipt(vector["document"], [])
    assert (result.ok, result.reason) == (False, "unknown_signer")
    assert result.signer_lines == ("Not verified: unknown signer",)


def test_the_builder_refuses_embedded_content():
    with pytest.raises(ValueError):
        r.make_openings({"program_name": "data:image/png;base64,iVBORw0KGgo"})
    with pytest.raises(ValueError):
        r.make_openings({"program_name": "https://example.test/patient/7"})
    with pytest.raises(ValueError):
        r.make_openings({"page_text": "Jane Doe"})


def test_outcomes_are_never_upgraded():
    assert r.receipt_outcome("VERIFIED") == ("VERIFIED", "verified")
    for run in ("HALTED", "ROLLED_BACK", "COMPLETED_UNVERIFIED", "FAILED", "weird", None, ""):
        assert r.receipt_outcome(run)[0] != "VERIFIED"


def test_public_card_refuses_a_field_off_the_allow_list():
    with pytest.raises(ValueError):
        r.public_card({}, {}, reveal=["run_id"])


def test_allow_list_fields_are_disclosure_fields():
    for version, allowed in r.PUBLIC_DISCLOSURE.items():
        assert set(allowed) <= set(r.DISCLOSURE_FIELDS)
        for source in allowed.values():
            assert source.split(":")[0] in ("public-card/v1", "public-evidence-receipt/v1")


def test_a_statement_without_confirmations_is_byte_identical():
    vector = json.loads((VECTORS / "valid.json").read_text())
    import base64

    payload = base64.b64decode(vector["document"]["payload"])
    assert b"confirmations" not in payload
    assert r.statement_payload(json.loads(payload)) == payload


@pytest.mark.parametrize(
    "change",
    [
        {"state": "recorded_as_intended", "read_method": "acknowledgment_message"},
        {"state": "sent", "strength": "read_back"},
        {"state": "recorded_differently", "differing_count": 0},
        {"for_step": 3},
    ],
)
def test_inconsistent_confirmations_are_refused(change):
    vector = json.loads((VECTORS / "confirmation-recorded-as-intended.json").read_text())
    import base64

    statement = json.loads(base64.b64decode(vector["document"]["payload"]))
    statement["predicate"]["confirmations"][0].update(change)
    with pytest.raises(ValueError):
        r.statement_payload(statement)


def test_jcs_cases():
    cases = json.loads((VECTORS / "jcs.json").read_text())["cases"]
    assert len(cases) >= 10
    for case in cases:
        assert r.jcs(case["input"]) == case["canonical"]
    # UTF-16 key order, not code point order.
    assert r.jcs({"\uffff": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"\uffff":1}'


def test_report_digests_never_stand_in_for_each_other():
    import hashlib

    vector = json.loads((VECTORS / "report-canonical.json").read_text())
    statement = json.loads(base64.b64decode(vector["document"]["payload"]))
    file_bytes = base64.b64decode(vector["report_file"])
    canonical = r.report_canonical_sha256(vector["report"])
    assert canonical == vector["expected"]["report_canonical_sha256"]
    assert r.jcs(vector["report"]) == vector["expected"]["report_canonical"]
    assert r.report_subject_match(statement, file_bytes=file_bytes) == "report.json"
    assert r.report_subject_match(statement, canonical_sha256=canonical) == "report.canonical"
    # Neither digest is accepted in the other's place.
    file_digest = hashlib.sha256(file_bytes).hexdigest()
    assert r.report_subject_match(statement, canonical_sha256=file_digest) is None
    assert (
        r.report_subject_match(statement, file_bytes=r.jcs(vector["report"]).encode())
        is None
    )


CHAINS = sorted(p.stem for p in VECTORS.glob("chain-*.json"))


def test_the_chain_vectors_are_there():
    assert {"chain-valid", "chain-wrong-original", "chain-out-of-sequence",
            "chain-tampered", "chain-empty", "chain-different-signer"} <= set(CHAINS)


@pytest.mark.parametrize("name", CHAINS)
def test_chain_vector(name):
    vector = json.loads((VECTORS / f"{name}.json").read_text())
    chain = vector["chain"]
    result = r.verify_chain(chain["original"], chain["updates"], _registry())
    expected = vector["expected"]
    assert result.ok is expected["ok"], result.detail
    assert result.reason == expected["reason"]
    assert list(result.lines) == expected["lines"]
