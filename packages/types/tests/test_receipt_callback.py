"""The managed sender accepts only a verified, public runner envelope."""

import hashlib
import json
from pathlib import Path

import pytest
from openadapt_types import receipt as r
from openadapt_types.receipt_callback import verified_receipt_metrics

VECTORS = Path(__file__).parent / "vectors" / "record-run-v1"


def _load(name):
    return json.loads((VECTORS / f"{name}.json").read_bytes())


def _pin():
    key = next(
        item for item in _load("registry")["keys"] if item["role"] == "this_computer"
    )
    return {field: key[field] for field in ("key_id", "public_key")}


@pytest.mark.parametrize(
    "name", ["public-redaction", "tampered", "unknown-signer", "cloud-countersign"]
)
def test_private_managed_callback_refuses_an_unusable_public_document(name):
    wire = json.dumps(_load(name)["document"]).encode()
    with pytest.raises(ValueError):
        verified_receipt_metrics(
            wire, pinned_signer=_pin(), report_jcs_sha256="a" * 64, outcome="VERIFIED"
        )


def test_pin_is_limited_to_public_fields_and_private_openings_stay_local():
    vector = _load("valid")
    for data, pin in (
        (json.dumps(vector["openings"]).encode(), _pin()),
        (json.dumps(vector["document"]).encode(), {**_pin(), "status": "active"}),
        (b"x" * (2 * r.MAX_PAYLOAD_BYTES + 1), _pin()),
        (b'{"payload":0,"payload":1}', _pin()),
    ):
        with pytest.raises(ValueError):
            verified_receipt_metrics(
                data, pinned_signer=pin, report_jcs_sha256="a" * 64, outcome="VERIFIED"
            )


def test_callback_binds_only_the_jcs_digest_of_the_shared_report_vector():
    vector = _load("report-canonical")
    wire = json.dumps(vector["document"]).encode()
    digest = r.report_canonical_sha256(vector["report"])
    outcome = vector["expected"]["outcome"]
    assert verified_receipt_metrics(
        wire, pinned_signer=_pin(), report_jcs_sha256=digest, outcome=outcome
    )["receipt_record"] == vector["document"]
    # This digest vector signs STOPPED but its sample report says VERIFIED.
    report_outcome = r.receipt_outcome(vector["report"]["execution_outcome"])[0]
    assert report_outcome != outcome
    with pytest.raises(ValueError, match="outcome"):
        verified_receipt_metrics(
            wire, pinned_signer=_pin(), report_jcs_sha256=digest, outcome=report_outcome
        )
    compact = hashlib.sha256(r.canonical_json(vector["report"]).encode()).hexdigest()
    exact = hashlib.sha256(vector["report_file"].encode()).hexdigest()
    assert len({digest, compact, exact}) == 3
    for other in (compact, exact):
        with pytest.raises(ValueError, match="canonical report"):
            verified_receipt_metrics(
                wire, pinned_signer=_pin(), report_jcs_sha256=other, outcome=outcome
            )
