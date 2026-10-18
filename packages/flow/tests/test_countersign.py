"""Countersigned Seals: salted commitments, RFC 3161 timestamps, verify output, dossier.

No test touches the network: HTTP TSAs run over an in-process transport, and
the DigiCert token is a fixture captured once with ``openssl ts -query``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("asn1crypto")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from openadapt_flow.contracts.seal import parse_key_registry, verify_sealed_receipt  # noqa: E402

from openadapt_flow.countersign import commitments as C  # noqa: E402
from openadapt_flow.countersign.document import (  # noqa: E402
    EXTENSION_KEY,
    CloudCountersignIssuer,
    CountersignError,
    LocalCountersignIssuer,
    countersign_document,
    receipt_sha256,
    timestamp_baseline,
    verify_countersigned,
)
from openadapt_flow.countersign.dossier import (  # noqa: E402
    build_dossier,
    extract_dossier_data,
    write_dossier,
)
from openadapt_flow.countersign.render import render_report  # noqa: E402
from openadapt_flow.countersign.tsa import (  # noqa: E402
    HttpTsa,
    LocalTsa,
    TimestampError,
    build_timestamp_request,
    verify_timestamp_token,
)

FIXTURES = Path(__file__).parent / "fixtures" / "countersign"
#: The fixed TEST seed openadapt-cloud's sealSignature tests use. Not a real key.
TEST_SEED = bytes([7]) * 32
T0 = datetime(2026, 10, 17, 5, 0, 0, tzinfo=timezone.utc)


def _canon(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _clock(at: datetime):
    return lambda: at


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import httpx

    def refuse(*a, **k):  # pragma: no cover - a failure path
        raise AssertionError("tests must not open a network connection")

    monkeypatch.setattr(httpx, "Client", refuse)


@pytest.fixture(autouse=True)
def _private_default_vault(monkeypatch, tmp_path):
    # A command without --vault must never touch the real ~/.openadapt vault.
    monkeypatch.setenv("OPENADAPT_COUNTERSIGN_VAULT", str(tmp_path / "default-vault"))
    monkeypatch.delenv("OPENADAPT_EXCEPTION_RETENTION_DAYS", raising=False)


@pytest.fixture
def issuer() -> LocalCountersignIssuer:
    return LocalCountersignIssuer(
        Ed25519PrivateKey.from_private_bytes(TEST_SEED), clock=_clock(T0)
    )


@pytest.fixture
def tsa() -> LocalTsa:
    return LocalTsa.generate(
        clock=_clock(T0 + timedelta(minutes=1)), valid_from=T0 - timedelta(days=1)
    )


def _evidence(
    kind: str = "agent", verdict: str = "CONFIRMED", tier: str = "independent_readback"
) -> dict:
    return {
        "schema_version": "openadapt.effect-evidence/v1",
        "executor": {
            "kind": kind,
            "ref_sha256": "sha256:" + "1" * 64,
            "run_record_sha256": "sha256:" + "2" * 64,
            "claims_counted_as_evidence": False,
        },
        "authorization": {
            "reference_sha256": "sha256:" + "3" * 64,
            "evaluated_by_verifier": False,
            "approver": None,
        },
        "contract": {
            "contract_sha256": "sha256:" + "4" * 64,
            "minimum_effect_strength": "independent_system_of_record",
        },
        "readback": {
            "verifier_identity": "sha256:" + "5" * 64,
            "verification_tier": tier,
            "baseline_sha256": "sha256:" + "6" * 64,
            "baseline_read_at": "2026-10-17T04:58:00Z",
            "read_at": "2026-10-17T04:59:30Z",
            "effects": [],
        },
        "verifier": {"name": "openadapt-flow verify-run", "model_calls": 0},
        "effect_verdict": verdict,
    }


def _effect_doc(
    signer: LocalCountersignIssuer,
    *,
    kind="agent",
    outcome="verified",
    verdict="CONFIRMED",
) -> dict:
    evidence = _evidence(kind, verdict)
    receipt = {
        "schema_version": "openadapt.effect-receipt/v1",
        "receipt_id": "rcpt-0000000001",
        "execution_id": "exec-0000000001",
        "workflow_digest": "sha256:" + "4" * 64,
        "workflow_version": "contract-4444444444444444",
        "qualification_id": "unqualified-verify-run",
        "environment_id": "unnamed-environment",
        "runner_id": "executor-1111111111111111",
        "nonce": "nonce-abcdef012345",
        "oracle_tier": 2,
        "outcome": outcome,
        "contracts": {
            "authorization_passed": outcome == "verified",
            "identity_passed": outcome == "verified",
            "postcondition_passed": outcome == "verified",
            "effect_passed": outcome == "verified",
            "minimum_effect_strength": "independent_system_of_record",
            "observed_effect_strength": "independent_system_of_record",
            "model_used": True,
            "external_network_used": True,
        },
        "delivery_uncertain": outcome != "verified"
        and outcome != "halted_before_effect",
        "compensation_effect_verified": False,
        "evidence_digest": "sha256:" + hashlib.sha256(_canon(evidence)).hexdigest(),
        "issued_at": "2026-10-17T05:00:00Z",
        "executor_kind": kind,
        "executor_ref_sha256": "sha256:" + "1" * 64,
        "effect_verdict": verdict,
        "verification_tier": "independent_readback",
    }
    receipt["seal_signature"] = signer.countersign(receipt)
    return {
        "schema_version": "openadapt.effect-receipt/v1",
        "receipt": receipt,
        "evidence": evidence,
    }


def _execute_doc(signer: LocalCountersignIssuer) -> dict:
    receipt = {
        "schema_version": "openadapt.execute-evidence-receipt/v1",
        "receipt_id": "rcpt-0000000002",
        "execution_id": "exec-0000000002",
        "workflow_digest": "sha256:" + "a" * 64,
        "workflow_version": "v-0000000001",
        "qualification_id": "qual-00000001",
        "environment_id": "env-00000001",
        "runner_id": "runner-00000001",
        "nonce": "nonce-00000002",
        "oracle_tier": 2,
        "outcome": "verified",
        "evidence_digest": "sha256:" + "b" * 64,
        "issued_at": "2026-10-17T05:00:00Z",
    }
    receipt["seal_signature"] = signer.countersign(receipt)
    return receipt


# -- (a) salted field commitments -------------------------------------------


def test_unrevealed_field_cannot_be_brute_forced_from_a_small_domain():
    mrn = "004217"
    block, kept = C.commit_fields({"mrn": mrn, "dob": "1961-03-04"})
    commitments = set(block["fields"].values())
    domain = range(20_000)  # a small MRN space; real ones are a few million
    # The gap this closes: an unsalted hash of the MRN falls to enumeration.
    unsalted = hashlib.sha256(mrn.encode()).hexdigest()
    assert any(
        hashlib.sha256(f"{i:06d}".encode()).hexdigest() == unsalted for i in domain
    )
    # Salted: the attacker knows the label, the field name, and the whole value
    # domain, but not the salt. Every guess fails, with an empty salt or a
    # guessed one.
    label = next(s.label for s in kept if s.name == "mrn")
    for guess_salt in (bytes(32), b"\xff" * 32, hashlib.sha256(b"mrn").digest()):
        hits = [
            i
            for i in domain
            if C.field_commitment(label, "mrn", f"{i:06d}", guess_salt) in commitments
        ]
        assert hits == []
    # Each field has an independent 256-bit salt, so equal values never collide.
    again, _ = C.commit_fields({"mrn": mrn})
    assert set(again["fields"].values()).isdisjoint(commitments)
    assert all(len(base64.b64decode(s.salt)) == 32 for s in kept)


def test_commitment_block_carries_no_names_or_values():
    block, _ = C.commit_fields({"mrn": "004217", "patient_name": "Ada Lovelace"})
    text = json.dumps(block)
    for leak in ("mrn", "004217", "patient_name", "Ada"):
        assert leak not in text


def test_disclosed_value_checks_and_wrong_value_fails():
    block, kept = C.commit_fields({"mrn": "004217", "dob": "1961-03-04"})
    mrn_label = next(s.label for s in kept if s.name == "mrn")
    bundle = C.build_disclosure(block, kept, [mrn_label], receipt_sha256="r" * 64)
    assert [f["name"] for f in bundle["fields"]] == ["mrn"]  # dob stays hidden
    assert C.verify_disclosure(bundle, block, receipt_sha256="r" * 64).ok

    wrong = json.loads(json.dumps(bundle))
    wrong["fields"][0]["value"] = "004218"
    check = C.verify_disclosure(wrong, block)
    assert not check.ok and check.fields[0].reason == "value_mismatch"

    other_salt = json.loads(json.dumps(bundle))
    other_salt["fields"][0]["salt"] = base64.b64encode(bytes(32)).decode()
    assert not C.verify_disclosure(other_salt, block).ok

    renamed = json.loads(json.dumps(bundle))
    renamed["fields"][0]["name"] = "dob"
    assert not C.verify_disclosure(renamed, block).ok

    assert (
        C.verify_disclosure(bundle, block, receipt_sha256="s" * 64).reason
        == "receipt_mismatch"
    )
    swapped, _ = C.commit_fields({"mrn": "004217"})
    assert C.verify_disclosure(bundle, swapped).reason == "commitments_mismatch"


# -- (b) RFC 3161 timestamps -------------------------------------------------


def test_local_tsa_token_verifies_offline_and_fails_closed(tsa):
    digest = hashlib.sha256(b"statement").digest()
    token = tsa.timestamp(digest)
    ok = verify_timestamp_token(token, digest, [tsa.certificate])
    assert ok.ok and ok.gen_time == "2026-10-17T05:01:00Z"
    assert (
        verify_timestamp_token(
            token, hashlib.sha256(b"other").digest(), [tsa.certificate]
        ).reason
        == "imprint_mismatch"
    )
    assert verify_timestamp_token(token, digest, []).reason == "no_pinned_tsa"
    stranger = LocalTsa.generate()
    assert (
        verify_timestamp_token(token, digest, [stranger.certificate]).reason
        == "untrusted_tsa"
    )
    # Flip one byte inside the TSTInfo (the stamped serial number).
    tampered = bytearray(token)
    at = bytes(token).find(digest) + len(digest) + 4
    tampered[at] ^= 0x01
    assert not verify_timestamp_token(bytes(tampered), digest, [tsa.certificate]).ok
    assert (
        verify_timestamp_token(b"\x30\x03junk", digest, [tsa.certificate]).reason
        == "malformed"
    )


def test_tsa_certificate_must_be_valid_at_gen_time():
    late = LocalTsa.generate(clock=_clock(T0), valid_from=T0 + timedelta(days=2))
    digest = hashlib.sha256(b"x").digest()
    result = verify_timestamp_token(late.timestamp(digest), digest, [late.certificate])
    assert result.reason == "certificate_not_valid_at_gen_time"


def test_real_digicert_token_verifies_against_pinned_root():
    token = (FIXTURES / "digicert-token.der").read_bytes()
    digest = bytes.fromhex((FIXTURES / "digicert-digest.hex").read_text().strip())
    root = x509.load_pem_x509_certificates(
        (FIXTURES / "digicert-trusted-root-g4.pem").read_bytes()
    )
    result = verify_timestamp_token(token, digest, root)
    assert result.ok, result
    assert result.gen_time == "2026-10-17T05:20:40Z"
    assert "DigiCert" in (result.tsa_subject or "")
    assert (
        verify_timestamp_token(token, digest, [LocalTsa.generate().certificate]).reason
        == "untrusted_tsa"
    )


def test_token_naming_its_signer_only_by_sha1_ess_v1_is_refused():
    """ESS v1 (RFC 2634) names the signer certificate by SHA-1 only; refuse it."""
    from asn1crypto import cms

    token = (FIXTURES / "digicert-token.der").read_bytes()
    digest = bytes.fromhex((FIXTURES / "digicert-digest.hex").read_text().strip())
    root = x509.load_pem_x509_certificates(
        (FIXTURES / "digicert-trusted-root-g4.pem").read_bytes()
    )
    info = cms.ContentInfo.load(token)
    signer = info["content"]["signer_infos"][0]
    kept = [
        attr
        for attr in signer["signed_attrs"]
        if attr["type"].native != "signing_certificate_v2"
    ]
    assert any(attr["type"].native == "signing_certificate" for attr in kept)
    signer["signed_attrs"] = cms.CMSAttributes(kept)
    result = verify_timestamp_token(info.dump(force=True), digest, root)
    assert not result.ok
    assert result.reason == "malformed"


def test_http_tsa_sends_only_the_digest_and_checks_the_nonce(tsa):
    from asn1crypto import tsp

    seen: list[bytes] = []

    def server(url: str, body: bytes):
        seen.append(body)
        req = tsp.TimeStampReq.load(body)
        token = tsa.timestamp(
            req["message_imprint"]["hashed_message"].native, nonce=req["nonce"].native
        )
        resp = tsp.TimeStampResp(
            {
                "status": {"status": "granted"},
                "time_stamp_token": __import__("asn1crypto").cms.ContentInfo.load(
                    token
                ),
            }
        )
        return 200, resp.dump()

    digest = hashlib.sha256(b"receipt").digest()
    token = HttpTsa("https://tsa.example.test/", transport=server).timestamp(digest)
    assert verify_timestamp_token(token, digest, [tsa.certificate]).ok
    assert len(seen) == 1 and len(seen[0]) < 100  # a digest and a nonce, nothing else

    def replay(url: str, body: bytes):
        token = tsa.timestamp(digest, nonce=12345)
        return 200, tsp.TimeStampResp(
            {
                "status": {"status": "granted"},
                "time_stamp_token": __import__("asn1crypto").cms.ContentInfo.load(
                    token
                ),
            }
        ).dump()

    with pytest.raises(TimestampError) as err:
        HttpTsa("https://tsa.example.test/", transport=replay).timestamp(digest)
    assert err.value.code == "nonce_mismatch"

    def refuse(url: str, body: bytes):
        # TimeStampResp { status { rejection(2) } }, no token. asn1crypto
        # cannot encode a response without a token, so spell out the DER.
        return 200, bytes.fromhex("30053003020102")

    with pytest.raises(TimestampError) as err:
        HttpTsa("https://tsa.example.test/", transport=refuse).timestamp(digest)
    assert err.value.code == "tsa_refused"
    assert len(build_timestamp_request(digest, 1)) < 80


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_openssl_accepts_local_tsa_tokens(tsa, tmp_path):
    digest = hashlib.sha256(b"interop").digest()
    (tmp_path / "t.tsr").write_bytes(tsa.timestamp(digest))
    (tmp_path / "ca.pem").write_bytes(tsa.certificate_pem())
    proc = subprocess.run(
        [
            "openssl",
            "ts",
            "-verify",
            "-digest",
            digest.hex(),
            "-in",
            str(tmp_path / "t.tsr"),
            "-token_in",
            "-CAfile",
            str(tmp_path / "ca.pem"),
            "-partial_chain",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if "unknown option" in proc.stderr or "partial_chain" in proc.stderr:
        pytest.skip("this openssl has no ts -partial_chain")
    assert "Verification: OK" in proc.stdout, proc.stderr


# -- countersign and verify --------------------------------------------------


def _countersigned(
    issuer, tsa, *, doc=None, fields=None, baseline=None, countersign=True
):
    doc = doc or _effect_doc(issuer)
    block = kept = None
    if fields:
        block, kept = C.commit_fields(fields)
    out = countersign_document(
        doc,
        tsa=tsa,
        tsa_pem=tsa.certificate_pem(),
        commitments=block,
        baseline_timestamp=baseline,
        issuer=issuer if countersign else None,
    )
    return out, block, kept


def test_countersigning_leaves_the_receipt_and_its_seal_untouched(issuer, tsa):
    doc = _effect_doc(issuer)
    out, _, _ = _countersigned(issuer, tsa, doc=doc, fields={"mrn": "004217"})
    assert out["receipt"] == doc["receipt"] and out["evidence"] == doc["evidence"]
    keys = parse_key_registry([issuer.registry_entry()])
    assert verify_sealed_receipt(
        out["receipt"], keys
    ).ok  # the existing verifier, unchanged
    assert "004217" not in json.dumps(out) and "mrn" not in json.dumps(out)
    with pytest.raises(CountersignError):
        countersign_document(out, tsa=tsa)


def test_agent_receipt_never_reads_like_a_flow_replay(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    reg = [issuer.registry_entry()]
    report = verify_countersigned(out, registry=reg, tsa_certificates=[tsa.certificate])
    text = render_report(report)
    assert report.exit_code == 1
    assert text.splitlines()[0].startswith(
        "SIGNATURE GENUINE, NOT A FLOW REPLAY (AGENT)"
    )
    assert "AGENT: a computer-use agent acted. This is not a Flow replay" in text
    assert "independent_readback" in text
    assert "Receipt timestamp   VERIFIED 2026-10-17T05:01:00Z" in text
    accepted = verify_countersigned(
        out, registry=reg, tsa_certificates=[tsa.certificate], accept_non_flow=True
    )
    assert accepted.exit_code == 0
    assert render_report(accepted).startswith(
        "SEAL VERIFIED: AGENT RUN (not a Flow replay)"
    )


def test_declared_flow_replay_in_an_effect_receipt_is_not_trusted(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa, doc=_effect_doc(issuer, kind="flow_replay"))
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], tsa_certificates=[tsa.certificate]
    )
    assert report.exit_code == 1
    assert "DECLARED by the run record" in render_report(report)


def test_execute_receipt_reads_as_flow_replay(issuer, tsa):
    bare = _execute_doc(issuer)
    out, _, _ = _countersigned(issuer, tsa, doc=bare)
    # A bare receipt is wrapped, never edited: legacy verifiers read "receipt".
    assert out["receipt"] == bare and "extensions" not in out["receipt"]
    keys = parse_key_registry([issuer.registry_entry()])
    assert verify_sealed_receipt(out["receipt"], keys).ok
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], tsa_certificates=[tsa.certificate]
    )
    assert report.exit_code == 0, report.problems
    text = render_report(report)
    assert text.startswith("SEAL VERIFIED (Flow replay)")
    assert "implied by openadapt.execute-evidence-receipt/v1" in text


@pytest.mark.parametrize(
    "verdict,outcome",
    [("REFUTED", "halted_before_effect"), ("INDETERMINATE", "reconciliation_required")],
)
def test_signed_refuted_and_indeterminate_receipts_exit_nonzero(
    issuer, tsa, verdict, outcome
):
    out, _, _ = _countersigned(
        issuer, tsa, doc=_effect_doc(issuer, outcome=outcome, verdict=verdict)
    )
    report = verify_countersigned(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert report.signature_genuine and report.exit_code == 1
    assert render_report(report).startswith(
        f"SIGNATURE GENUINE, OUTCOME NOT VERIFIED ({outcome})"
    )


def test_unpinned_tsa_is_not_checked_and_not_a_pass(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], accept_non_flow=True
    )
    assert report.receipt_timestamp["status"] == "not_checked"
    assert report.exit_code == 1
    # The certificate inside the document is a convenience, never a trust anchor.
    assert (
        "tsa_certificates_pem" in out["extensions"][EXTENSION_KEY]["receipt_timestamp"]
    )


def test_tampering_after_countersigning_is_caught(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    reg = [issuer.registry_entry()]
    edited = json.loads(json.dumps(out))
    edited["receipt"]["executor_kind"] = "flow_replay"
    report = verify_countersigned(
        edited, registry=reg, tsa_certificates=[tsa.certificate]
    )
    assert report.seal["status"] == "receipt_mismatch"
    assert "statement_mismatch" in report.problems

    # A fresh timestamp over a forged statement does not survive the statement check.
    swapped = json.loads(json.dumps(out))
    swapped["extensions"][EXTENSION_KEY]["statement"]["nonce"] = "nonce-forged000000"
    report = verify_countersigned(
        swapped, registry=reg, tsa_certificates=[tsa.certificate], accept_non_flow=True
    )
    assert "statement_mismatch" in report.problems and report.exit_code == 1


def test_countersignature_reuses_the_seal_scheme(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    block = out["extensions"][EXTENSION_KEY]
    att = block["countersignature"]["attestation"]
    assert att["schema_version"] == "openadapt.countersign-attestation/v1"
    assert (
        block["countersignature"]["seal_signature"]["schema"]
        == "openadapt.seal-signature/v1"
    )
    report = verify_countersigned(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert report.countersignature["status"] == "verified"
    other = LocalCountersignIssuer(Ed25519PrivateKey.generate())
    forged = json.loads(json.dumps(out))
    forged["extensions"][EXTENSION_KEY]["countersignature"]["seal_signature"] = (
        other.countersign(att)
    )
    report = verify_countersigned(
        forged,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert report.countersignature["status"] == "unknown_key" and report.exit_code == 1
    with pytest.raises(CountersignError):
        CloudCountersignIssuer().countersign(att)


def test_baseline_timestamp_binds_read_time_and_run_nonce(issuer):
    early = LocalTsa.generate(
        clock=_clock(T0 - timedelta(minutes=2)), valid_from=T0 - timedelta(days=1)
    )
    late = LocalTsa(
        early._key, early.certificate, clock=_clock(T0 + timedelta(minutes=1))
    )
    doc = _effect_doc(issuer)
    rb = doc["evidence"]["readback"]
    reg = [issuer.registry_entry()]
    good = timestamp_baseline(
        early,
        baseline_sha256=rb["baseline_sha256"],
        baseline_read_at=rb["baseline_read_at"],
        nonce=doc["receipt"]["nonce"],
    )
    out = countersign_document(doc, tsa=late, baseline_timestamp=good, issuer=issuer)
    report = verify_countersigned(
        out, registry=reg, tsa_certificates=[early.certificate], accept_non_flow=True
    )
    assert report.baseline_timestamp["status"] == "verified" and report.exit_code == 0
    assert report.baseline_timestamp["gen_time"] < report.receipt_timestamp["gen_time"]

    other_run = timestamp_baseline(
        early,
        baseline_sha256=rb["baseline_sha256"],
        baseline_read_at=rb["baseline_read_at"],
        nonce="nonce-another-run0",
    )
    out = countersign_document(doc, tsa=late, baseline_timestamp=other_run)
    assert (
        verify_countersigned(
            out, registry=reg, tsa_certificates=[early.certificate]
        ).baseline_timestamp["status"]
        == "binding_mismatch"
    )

    after = timestamp_baseline(
        LocalTsa(early._key, early.certificate, clock=_clock(T0 + timedelta(hours=1))),
        baseline_sha256=rb["baseline_sha256"],
        baseline_read_at=rb["baseline_read_at"],
        nonce=doc["receipt"]["nonce"],
    )
    out = countersign_document(doc, tsa=late, baseline_timestamp=after)
    assert (
        verify_countersigned(
            out, registry=reg, tsa_certificates=[early.certificate]
        ).baseline_timestamp["status"]
        == "after_receipt"
    )


def test_disclosure_counts_only_against_bound_commitments(issuer, tsa):
    out, block, kept = _countersigned(
        issuer, tsa, fields={"mrn": "004217", "dob": "1961-03-04"}, countersign=False
    )
    rsha = receipt_sha256(out["receipt"])
    label = next(s.label for s in kept if s.name == "mrn")
    bundle = C.build_disclosure(block, kept, [label], receipt_sha256=rsha)
    reg = [issuer.registry_entry()]
    report = verify_countersigned(
        out,
        registry=reg,
        tsa_certificates=[tsa.certificate],
        disclosures=[bundle],
        accept_non_flow=True,
    )
    assert report.commitments["bound_by"] == ["timestamp"] and report.exit_code == 0
    assert "mrn) OK" in render_report(report)
    # Without a checked timestamp or countersignature the commitments could
    # have been written after the fact; a matching value proves nothing.
    report = verify_countersigned(
        out, registry=reg, disclosures=[bundle], accept_non_flow=True
    )
    assert "commitments_unbound" in report.problems
    wrong = json.loads(json.dumps(bundle))
    wrong["fields"][0]["value"] = "004218"
    report = verify_countersigned(
        out,
        registry=reg,
        tsa_certificates=[tsa.certificate],
        disclosures=[wrong],
        accept_non_flow=True,
    )
    assert "disclosure_mismatch" in report.problems and report.exit_code == 1


# -- (c) dossier -------------------------------------------------------------


def test_dossier_embeds_no_phi_by_default_and_reverifies(issuer, tsa, tmp_path):
    out, block, kept = _countersigned(
        issuer, tsa, fields={"mrn": "004217", "patient_name": "Ada Lovelace"}
    )
    raw_png = b"\x89PNG\r\n\x1a\nRAW-PHI-SCREENSHOT"
    shot = tmp_path / "frame.png"
    shot.write_bytes(raw_png)
    data, report = build_dossier(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates_pem=tsa.certificate_pem(),
        screenshots=[shot],
        accept_non_flow=True,
    )
    path = write_dossier(data, report, tmp_path / "d.html")
    page = path.read_text()
    for leak in ("004217", "Ada Lovelace", "patient_name", "RAW-PHI-SCREENSHOT"):
        assert leak not in page
    assert "left out: no redaction provider" in page
    assert "SEAL VERIFIED: AGENT RUN" in page
    assert "is a notarial act, a government certification" in page
    again = extract_dossier_data(page)
    re_report = verify_countersigned(
        again["document"],
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert re_report.exit_code == 0


def test_dossier_includes_disclosures_and_redacted_screenshots_only_when_given(
    issuer, tsa, tmp_path
):
    from PIL import Image

    from openadapt_flow import privacy

    class FakeScrubber:
        def scrub_text(self, text):
            return text

        def scrub_image(self, image):
            return Image.new("RGB", image.size, (0, 0, 0))

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
    shot = tmp_path / "frame.png"
    shot.write_bytes(buf.getvalue())
    out, block, kept = _countersigned(
        issuer, tsa, fields={"mrn": "004217", "patient_name": "Ada Lovelace"}
    )
    label = next(s.label for s in kept if s.name == "mrn")
    bundle = C.build_disclosure(
        block, kept, [label], receipt_sha256=receipt_sha256(out["receipt"])
    )
    privacy.set_text_scrubber(FakeScrubber())
    try:
        data, report = build_dossier(
            out,
            registry=[issuer.registry_entry()],
            tsa_certificates_pem=tsa.certificate_pem(),
            disclosures=[bundle],
            screenshots=[shot],
            accept_non_flow=True,
        )
    finally:
        privacy.reset_scrubbers()
    page = write_dossier(data, report, tmp_path / "d.html").read_text()
    assert "004217" in page and "Ada Lovelace" not in page
    assert base64.b64encode(buf.getvalue()).decode() not in page
    assert data["screenshots"] and "automatically redacted" in page
    zpath = write_dossier(data, report, tmp_path / "d.zip", fmt="zip")
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        assert {
            "dossier.html",
            "document.json",
            "receipt-timestamp.tsr",
            "tsa-ca.pem",
            "VERIFY.md",
        } <= names
        token = zf.read("receipt-timestamp.tsr")
    statement = out["extensions"][EXTENSION_KEY]["statement"]
    assert verify_timestamp_token(
        token, hashlib.sha256(_canon(statement)).digest(), [tsa.certificate]
    ).ok


# -- CLI end to end ------------------------------------------------------------


def test_cli_add_disclose_verify_dossier(issuer, tmp_path, capsys, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    from openadapt_flow.__main__ import main

    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_PATH", raising=False)
    doc = tmp_path / "receipt.json"
    doc.write_text(json.dumps(_effect_doc(issuer)))
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"mrn": "004217"}))
    key = tmp_path / "issuer.pem"
    key.write_bytes(
        Ed25519PrivateKey.from_private_bytes(TEST_SEED).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keys = tmp_path / "seal-keys.json"
    keys.write_text(json.dumps({"keys": [issuer.registry_entry()]}))
    vault = tmp_path / "vault"
    countersigned = tmp_path / "countersigned.json"
    tsa_dir = tmp_path / "tsa"
    assert (
        main(
            [
                "countersign",
                "add",
                str(doc),
                "-o",
                str(countersigned),
                "--fields-file",
                str(fields),
                "--tsa-local",
                str(tsa_dir),
                "--countersign-key",
                str(key),
                "--vault",
                str(vault),
            ]
        )
        == 0
    )
    assert (countersigned.stat().st_mode & 0o777) == 0o600
    rsha = receipt_sha256(json.loads(doc.read_text())["receipt"])
    secrets_file = vault / rsha / "field-secrets.json"
    assert (
        secrets_file.stat().st_mode & 0o777
    ) == 0o600 and "004217" in secrets_file.read_text()
    assert "004217" not in countersigned.read_text()
    label = next(
        iter(
            json.loads(countersigned.read_text())["extensions"][EXTENSION_KEY][
                "commitments"
            ]["fields"]
        )
    )
    disclosure = tmp_path / "disclosure.json"
    assert (
        main(
            [
                "countersign",
                "disclose",
                str(countersigned),
                "--field",
                label,
                "-o",
                str(disclosure),
                "--vault",
                str(vault),
            ]
        )
        == 0
    )
    capsys.readouterr()
    base = [
        "countersign",
        "verify",
        str(countersigned),
        "--keys",
        str(keys),
        "--tsa-cert",
        str(tsa_dir / "tsa-cert.pem"),
        "--disclosure",
        str(disclosure),
    ]
    assert main(base) == 1  # an agent receipt without --accept-non-flow
    assert "NOT A FLOW REPLAY (AGENT)" in capsys.readouterr().out
    assert main(base + ["--accept-non-flow", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert (
        verdict["executor_kind"] == "agent"
        and verdict["verification_tier"] == "independent_readback"
    )
    assert verdict["receipt_timestamp"]["status"] == "verified"
    assert verdict["disclosures"][0]["fields"][0]["status"] == "matches"
    html_out = tmp_path / "dossier.html"
    assert (
        main(
            [
                "countersign",
                "dossier",
                str(countersigned),
                "-o",
                str(html_out),
                "--keys",
                str(keys),
                "--tsa-cert",
                str(tsa_dir / "tsa-cert.pem"),
                "--accept-non-flow",
            ]
        )
        == 0
    )
    assert "004217" not in html_out.read_text()
    assert (
        main(
            [
                "countersign",
                "verify",
                str(html_out),
                "--keys",
                str(keys),
                "--tsa-cert",
                str(tsa_dir / "tsa-cert.pem"),
                "--accept-non-flow",
            ]
        )
        == 0
    )


# -- Sigstore default TSA ----------------------------------------------------


def test_sigstore_token_verifies_offline_against_bundled_root():
    """A real token from timestamp.sigstore.dev (captured 2026-10-17)."""

    import hashlib
    from pathlib import Path

    from openadapt_flow.countersign.tsa import (
        load_pem_certificates,
        sigstore_tsa_root_pem,
        verify_timestamp_token,
    )

    token = (
        Path(__file__).parent / "fixtures" / "countersign" / "sigstore-token.der"
    ).read_bytes()
    digest = hashlib.sha256(b"openadapt sigstore probe").digest()
    roots = load_pem_certificates(sigstore_tsa_root_pem())
    result = verify_timestamp_token(token, digest, roots)
    assert result.ok and result.reason == "verified"
    assert result.tsa_subject == "CN=sigstore-tsa,O=sigstore.dev"
    assert result.accuracy_seconds == 1.0
    wrong = verify_timestamp_token(token, hashlib.sha256(b"x").digest(), roots)
    assert not wrong.ok and wrong.reason == "imprint_mismatch"


def test_default_authority_is_sigstore_and_no_timestamp_opts_out():
    import argparse

    from openadapt_flow.countersign.cli import _authority, _tsa_pem
    from openadapt_flow.countersign.tsa import (
        SIGSTORE_TSA_URL,
        HttpTsa,
        sigstore_tsa_root_pem,
    )

    tsa, pem = _authority(argparse.Namespace(tsa_url=None, tsa_local=None))
    assert isinstance(tsa, HttpTsa) and tsa.url == SIGSTORE_TSA_URL and pem is None
    assert _authority(argparse.Namespace(no_timestamp=True)) == (None, None)
    assert _tsa_pem([]) == sigstore_tsa_root_pem()


# -- review hardening ----------------------------------------------------------


def _cert(
    subject: str,
    key,
    *,
    issuer_name: str | None = None,
    issuer_key=None,
    ca: bool = False,
    path_length: int | None = None,
    cert_sign: bool | None = None,
    eku: list | None = None,
    eku_critical: bool = True,
):
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, issuer_name or subject)]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(T0 - timedelta(days=1))
        .not_valid_after(T0 + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), True)
    )
    if cert_sign is not None:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=cert_sign,
                crl_sign=cert_sign,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), eku_critical)
    return builder.sign(issuer_key or key, hashes.SHA256())


def _ec():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def test_tsa_certificate_needs_a_critical_eku_of_timestamping_alone():
    from cryptography.x509.oid import ExtendedKeyUsageOID as EKU

    digest = hashlib.sha256(b"eku").digest()
    for eku, critical in (
        ([EKU.TIME_STAMPING, EKU.SERVER_AUTH], True),  # an extra purpose
        ([EKU.TIME_STAMPING], False),  # not critical
        ([EKU.SERVER_AUTH], True),  # not a TSA at all
    ):
        key = _ec()
        cert = _cert("tsa", key, eku=eku, eku_critical=critical)
        tsa = LocalTsa(key, cert, clock=_clock(T0))
        result = verify_timestamp_token(tsa.timestamp(digest), digest, [cert])
        assert result.reason == "not_a_tsa_certificate", (eku, critical)
    key = _ec()
    cert = _cert("tsa", key, eku=[EKU.TIME_STAMPING])
    good = LocalTsa(key, cert, clock=_clock(T0)).timestamp(digest)
    assert verify_timestamp_token(good, digest, [cert]).ok


def test_chain_to_pinned_root_checks_each_issuer():
    from cryptography.x509.oid import ExtendedKeyUsageOID as EKU

    digest = hashlib.sha256(b"chain").digest()

    def token_via(**intermediate_kwargs):
        root_key, mid_key, leaf_key = _ec(), _ec(), _ec()
        root = _cert("root", root_key, ca=True, cert_sign=True)
        mid = _cert(
            "mid",
            mid_key,
            issuer_name="root",
            issuer_key=root_key,
            ca=True,
            **intermediate_kwargs,
        )
        leaf = _cert(
            "leaf",
            leaf_key,
            issuer_name="mid",
            issuer_key=mid_key,
            eku=[EKU.TIME_STAMPING],
        )
        tsa = LocalTsa(leaf_key, leaf, clock=_clock(T0), extra_certificates=[mid])
        return tsa.timestamp(digest), root

    token, root = token_via(cert_sign=True, path_length=0, eku=[EKU.TIME_STAMPING])
    result = verify_timestamp_token(token, digest, [root])
    assert result.ok and result.trust_anchor_subject == "CN=root"
    # Each of these intermediates must not be able to vouch for a TSA.
    for bad in (
        {"cert_sign": False},  # key usage forbids certificate signing
        {"cert_sign": True, "eku": [EKU.SERVER_AUTH], "eku_critical": False},
    ):
        token, root = token_via(**bad)
        assert verify_timestamp_token(token, digest, [root]).reason == "untrusted_tsa"
    # Without the pinned root, the shipped intermediate is never an anchor.
    token, root = token_via(cert_sign=True)
    stranger = _cert("root", _ec(), ca=True, cert_sign=True)
    assert verify_timestamp_token(token, digest, [stranger]).reason == "untrusted_tsa"


def test_path_length_constraint_is_enforced():
    from cryptography.x509.oid import ExtendedKeyUsageOID as EKU

    digest = hashlib.sha256(b"pathlen").digest()
    root_key, a_key, b_key, leaf_key = _ec(), _ec(), _ec(), _ec()
    root = _cert("root", root_key, ca=True, cert_sign=True, path_length=0)
    a = _cert("a", a_key, issuer_name="root", issuer_key=root_key, ca=True)
    b = _cert("b", b_key, issuer_name="a", issuer_key=a_key, ca=True)
    leaf = _cert(
        "leaf", leaf_key, issuer_name="b", issuer_key=b_key, eku=[EKU.TIME_STAMPING]
    )
    tsa = LocalTsa(leaf_key, leaf, clock=_clock(T0), extra_certificates=[b, a])
    # root allows no CA below it, but "a" is one: two CAs sit under root.
    assert (
        verify_timestamp_token(tsa.timestamp(digest), digest, [root]).reason
        == "untrusted_tsa"
    )
    assert verify_timestamp_token(tsa.timestamp(digest), digest, [a]).ok


def test_a_timestamp_that_cannot_be_checked_is_never_kept(issuer, tsa):
    doc = _effect_doc(issuer)
    with pytest.raises(CountersignError) as err:
        countersign_document(
            doc, tsa=tsa, tsa_certificates=[LocalTsa.generate().certificate]
        )
    assert err.value.code == "timestamp_unverified"
    out = countersign_document(doc, tsa=tsa, tsa_certificates=[tsa.certificate])
    assert EXTENSION_KEY in out["extensions"]
    # A baseline token for another run is refused before it is countersigned.
    rb = doc["evidence"]["readback"]
    other_run = timestamp_baseline(
        tsa,
        baseline_sha256=rb["baseline_sha256"],
        baseline_read_at=rb["baseline_read_at"],
        nonce="nonce-another-run0",
    )
    with pytest.raises(CountersignError):
        countersign_document(
            doc,
            tsa=tsa,
            baseline_timestamp=other_run,
            issuer=issuer,
            tsa_certificates=[tsa.certificate],
        )


def test_cli_tsa_failure_writes_nothing_and_a_retry_works(issuer, tmp_path, capsys):
    from openadapt_flow.__main__ import main

    doc = tmp_path / "receipt.json"
    doc.write_text(json.dumps(_effect_doc(issuer)))
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"mrn": "004217"}))
    out = tmp_path / "out.json"
    vault = tmp_path / "vault"
    base = ["countersign", "add", str(doc), "-o", str(out)]
    base += ["--fields-file", str(fields), "--vault", str(vault)]
    # The default Sigstore TSA is unreachable here (the network is refused).
    assert main(base) == 2
    assert "tsa_unreachable" in capsys.readouterr().err
    assert not out.exists()
    assert not list(vault.rglob("field-secrets.json"))
    assert main(base + ["--tsa-local", str(tmp_path / "tsa")]) == 0
    assert len(list(vault.rglob("field-secrets.json"))) == 1


def test_countersigning_issuer_sees_only_digests(issuer, tsa):
    seen: list[dict] = []

    class Recording:
        def countersign(self, attestation):
            seen.append(json.loads(json.dumps(attestation)))
            return issuer.countersign(attestation)

    doc = _effect_doc(issuer)
    block, kept = C.commit_fields({"mrn": "004217", "patient_name": "Ada Lovelace"})
    countersign_document(doc, tsa=tsa, commitments=block, issuer=Recording())
    (att,) = seen
    assert set(att) == {
        "schema_version",
        "execution_id",
        "statement_sha256",
        "receipt_timestamp_token_sha256",
        "baseline_timestamp_token_sha256",
    }
    text = json.dumps(att)
    for secret in kept:
        for leak in (secret.value, secret.salt, secret.name, secret.label):
            assert leak not in text


def test_dossier_refuses_unverified_disclosures_and_strips_extra_keys(
    issuer, tsa, tmp_path
):
    # No countersignature, so only a pinned timestamp binds the commitments.
    out, block, kept = _countersigned(
        issuer, tsa, fields={"mrn": "004217", "dob": "1961-03-04"}, countersign=False
    )
    rsha = receipt_sha256(out["receipt"])
    reg = [issuer.registry_entry()]
    pem = tsa.certificate_pem()
    # Someone passes the vault's secrets file as if it were a disclosure.
    vault_file = {
        "schema": "openadapt.countersign-vault-secrets/v1",
        "fields": [s.as_dict() for s in kept],
    }
    mrn = next(s.label for s in kept if s.name == "mrn")
    good = C.build_disclosure(block, kept, [mrn], receipt_sha256=rsha)
    padded = json.loads(json.dumps(good))
    padded["fields"][0]["dob"] = "1961-03-04"
    padded["vault"] = vault_file
    for bad, kwargs in (
        (vault_file, {"tsa_certificates_pem": pem}),
        (good, {}),  # unbound: no TSA pinned and no countersignature
    ):
        with pytest.raises(CountersignError) as err:
            build_dossier(
                out, registry=reg, disclosures=[bad], accept_non_flow=True, **kwargs
            )
        assert err.value.code == "disclosure_rejected"
    data, report = build_dossier(
        out,
        registry=reg,
        tsa_certificates_pem=pem,
        disclosures=[padded],
        accept_non_flow=True,
    )
    page = write_dossier(data, report, tmp_path / "d.html").read_text()
    assert "004217" in page and "1961-03-04" not in page


def test_verify_output_states_tsa_accuracy(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    report = verify_countersigned(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert "(accuracy 1 s)" in render_report(report)
    token = (FIXTURES / "digicert-token.der").read_bytes()
    digest = bytes.fromhex((FIXTURES / "digicert-digest.hex").read_text().strip())
    root = x509.load_pem_x509_certificates(
        (FIXTURES / "digicert-trusted-root-g4.pem").read_bytes()
    )
    assert verify_timestamp_token(token, digest, root).accuracy_seconds is None


# -- exception queue ----------------------------------------------------------

APPROVER_SEED = bytes([11]) * 32
LATER = "2026-10-17T06:00:00Z"


def _receipt_doc(
    signer,
    *,
    outcome="verified",
    verdict="CONFIRMED",
    kind="agent",
    n=1,
    issued_at="2026-10-17T05:00:00Z",
    workflow="4",
    **extra,
):
    doc = _effect_doc(signer, kind=kind, outcome=outcome, verdict=verdict)
    receipt = doc["receipt"]
    receipt.pop("seal_signature")
    receipt["receipt_id"] = f"rcpt-{n:010d}"
    receipt["execution_id"] = f"exec-{n:010d}"
    receipt["issued_at"] = issued_at
    receipt["workflow_digest"] = "sha256:" + workflow * 64
    receipt.update(extra)
    receipt["seal_signature"] = signer.countersign(receipt)
    return doc


def _failed(signer, n=1, **kw):
    return _receipt_doc(signer, outcome="halted", verdict="REFUTED", n=n, **kw)


def _fix(signer, n=2, **kw):
    kw.setdefault("issued_at", LATER)
    kw.setdefault("remediates_receipt_id", "rcpt-0000000001")
    return _receipt_doc(signer, n=n, **kw)


def _vaulted(issuer, tsa, vault, doc, *, params=None, enqueue=True):
    """What 'countersign add' does: countersign, vault the secrets, queue."""
    from openadapt_flow.countersign.exceptions import ExceptionQueue
    from openadapt_flow.countersign.tsa import load_pem_certificates

    out, _, kept = _countersigned(
        issuer, tsa, doc=doc, fields=params or {"mrn": "004217", "dose": "5mg"}
    )
    certs = load_pem_certificates(tsa.certificate_pem())
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], tsa_certificates=certs
    )
    vault.put_secrets(report.receipt_sha256, kept)
    vault.put_json(report.receipt_sha256, "document.json", out, overwrite=True)
    entry = (
        ExceptionQueue(vault).enqueue(out, report, [issuer.registry_entry()])
        if enqueue
        else None
    )
    return out, entry, certs


@pytest.fixture
def approver() -> LocalCountersignIssuer:
    return LocalCountersignIssuer(
        Ed25519PrivateKey.from_private_bytes(APPROVER_SEED), clock=_clock(T0)
    )


@pytest.fixture
def queue(tmp_path):
    from openadapt_flow.countersign.exceptions import ExceptionQueue
    from openadapt_flow.countersign.vault import CountersignVault

    return ExceptionQueue(
        CountersignVault(tmp_path / "vault"), clock=lambda: "2026-10-17T07:00:00Z"
    )


def test_exception_queue_takes_only_genuine_seals_on_unverified_outcomes(
    issuer, tsa, queue
):
    _, refuted, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    assert refuted["status"] == "open" and refuted["category"] == "effect_refuted"
    assert refuted["resolution_receipt_id"] is None
    assert refuted["retention_days"] == 2190
    assert "004217" not in json.dumps(refuted)  # values stay in the vault
    _, unclear, _ = _vaulted(
        issuer,
        tsa,
        queue.vault,
        _receipt_doc(issuer, outcome="halted", verdict="INDETERMINATE", n=2),
    )
    assert unclear["verdict"] == "INDETERMINATE"
    assert _vaulted(issuer, tsa, queue.vault, _receipt_doc(issuer, n=3))[1] is None
    stranger = LocalCountersignIssuer(
        Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32), clock=_clock(T0)
    )
    forged = _failed(stranger, n=4)
    report = verify_countersigned(forged, registry=[issuer.registry_entry()])
    assert queue.enqueue(forged, report, [issuer.registry_entry()]) is None
    assert len(queue.entries("open")) == 2


def test_resolved_by_seal_needs_a_later_linked_verified_seal_with_same_params(
    issuer, tsa, queue
):
    _, entry, certs = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    registry = [issuer.registry_entry()]
    rsha = entry["receipt_sha256"]

    def refused(code, resolution, reg=registry):
        with pytest.raises(CountersignError) as exc:
            queue.resolve_by_seal(rsha, resolution, reg, tsa_certificates=certs)
        assert exc.value.code == code
        assert queue.get(rsha)["status"] == "open"

    def fix(n, params=None, **kw):
        return _vaulted(
            issuer, tsa, queue.vault, _fix(issuer, n=n, **kw), params=params
        )[0]

    refused("not_linked", fix(2, remediates_receipt_id=None))
    refused("not_linked", fix(3, remediates_receipt_id="rcpt-0000000099"))
    refused("different_params", fix(4, params={"mrn": "999999", "dose": "5mg"}))
    refused("different_params", fix(5, params={"mrn": "004217"}))
    refused("not_later", fix(6, issued_at="2026-10-17T04:00:00Z"))
    refused("different_contract", fix(7, workflow="5"))
    still_failing = _vaulted(
        issuer,
        tsa,
        queue.vault,
        _failed(issuer, n=8, issued_at=LATER, remediates_receipt_id="rcpt-0000000001"),
    )[0]
    refused("resolution_not_verified", still_failing)
    good = fix(9)
    tampered = json.loads(json.dumps(good))
    tampered["receipt"]["issued_at"] = "2026-10-17T06:30:00Z"
    refused("resolution_not_verified", tampered)
    refused("no_pinned_keys", good, reg=[])
    extra = LocalCountersignIssuer(
        Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
    ).registry_entry()
    refused("different_pinned_keys", good, reg=registry + [extra])
    unvaulted, _, _ = _countersigned(
        issuer, tsa, doc=_fix(issuer, n=10), fields={"mrn": "004217", "dose": "5mg"}
    )
    refused("params_unproven", unvaulted)

    done = queue.resolve_by_seal(rsha, good, registry, tsa_certificates=certs)
    assert done["status"] == "resolved_by_seal"
    assert done["resolution_receipt_id"] == "rcpt-0000000009"
    assert done["terminal_at"] == "2026-10-17T07:00:00Z"
    assert done["retain_until"].startswith("2032-10-15")
    with pytest.raises(CountersignError) as exc:
        queue.resolve_by_seal(rsha, good, registry, tsa_certificates=certs)
    assert exc.value.code == "not_open"


def test_a_persons_verify_run_and_a_correlation_id_resolve(issuer, tsa, queue):
    cid = "corr-2026-10-17-0001"
    _, entry, certs = _vaulted(
        issuer, tsa, queue.vault, _failed(issuer, correlation_id=cid)
    )
    human = _vaulted(
        issuer,
        tsa,
        queue.vault,
        _fix(issuer, kind="human", remediates_receipt_id=None, correlation_id=cid),
    )[0]
    done = queue.resolve_by_seal(
        entry["receipt_sha256"],
        human,
        [issuer.registry_entry()],
        tsa_certificates=certs,
    )
    assert done["status"] == "resolved_by_seal"


def test_closed_by_attestation_needs_a_signed_disposition_from_another_person(
    issuer, approver, tsa, queue
):
    from openadapt_flow.countersign.exceptions import DISPOSITION_SCHEMA

    _, entry, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    rsha = entry["receipt_sha256"]
    pinned = [approver.registry_entry()]
    args = dict(
        approver_id="appr-7f3a91",
        role="nurse-manager",
        disposition_code="remediated_manually_in_sor",
        verification_method="ehr_audit_log_review",
        external_reference="INC-004217",
    )
    record = queue.disposition_record(
        rsha, signer=approver, notes="called MRN 004217", **args
    )
    assert record["schema"] == DISPOSITION_SCHEMA
    assert record["external_reference"].startswith("hmac-sha256:")
    assert "INC-004217" not in json.dumps(record)
    assert "004217" not in json.dumps(record)
    notes = queue.dir / f"{rsha}.notes.txt"
    assert notes.read_text() == "called MRN 004217"
    assert notes.stat().st_mode & 0o777 == 0o600

    def refused(code, rec, reg=pinned):
        with pytest.raises(CountersignError) as exc:
            queue.close_by_attestation(rsha, rec, reg)
        assert exc.value.code == code
        assert queue.get(rsha)["status"] == "open"

    refused("no_pinned_approver_keys", record, reg=[])
    refused("bad_disposition_signature", record, reg=[issuer.registry_entry()])
    edited = dict(record, disposition_code="cancelled_by_requester")
    refused("bad_disposition_signature", edited)
    for bad in (
        dict(record, disposition_code="looked_fine"),
        dict(record, verification_method="gut_feel"),
        dict(record, external_reference="INC-004217"),
        dict(record, approver={**record["approver"], "kind": "agent"}),
        dict(record, dispositioned_at="2026-10-17 07:00"),
        dict(record, notes="free text"),
    ):
        refused("bad_disposition", bad)
    # The key that sealed the failed run cannot close it.
    self_closed = queue.disposition_record(rsha, signer=issuer, **args)
    refused("reviewer_is_executor", self_closed, reg=pinned + [issuer.registry_entry()])
    # Nor can the agent executor under its own reference.
    as_executor = queue.disposition_record(
        rsha, signer=approver, **{**args, "approver_id": "sha256:" + "1" * 64}
    )
    refused("reviewer_is_executor", as_executor)

    done = queue.close_by_attestation(rsha, record, pinned)
    assert done["status"] == "closed_by_attestation"
    assert done["resolution_receipt_id"] is None
    assert done["disposition"]["disposition_code"] == "remediated_manually_in_sor"
    # The two terminal states never stand in for each other.
    good = _vaulted(issuer, tsa, queue.vault, _fix(issuer))[0]
    with pytest.raises(CountersignError) as exc:
        queue.resolve_by_seal(rsha, good, [issuer.registry_entry()])
    assert exc.value.code == "not_open"


def test_retention_defaults_to_six_years_with_export_before_purge(
    issuer, approver, tsa, queue, tmp_path
):
    from openadapt_flow.countersign.exceptions import retention_days

    assert retention_days(env={}) == 2190
    assert retention_days(env={"OPENADAPT_EXCEPTION_RETENTION_DAYS": "400"}) == 400
    with pytest.raises(CountersignError):
        retention_days(182, env={})
    _, closed, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    _, still_open, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer, n=3))
    record = queue.disposition_record(
        closed["receipt_sha256"],
        signer=approver,
        approver_id="appr-7f3a91",
        role="nurse-manager",
        disposition_code="cancelled_by_requester",
        verification_method="task_cancelled_no_check_needed",
        external_reference="INC-1",
        notes="n",
    )
    queue.close_by_attestation(
        closed["receipt_sha256"], record, [approver.registry_entry()]
    )
    export = tmp_path / "export.json"
    assert queue.purge(export, now="2032-10-14T00:00:00Z") == []
    assert not export.exists()
    export.write_text("{}")  # an export already there: refuse, delete nothing
    with pytest.raises(Exception):
        queue.purge(export, now="2032-10-16T00:00:00Z")
    assert queue.get(closed["receipt_sha256"])
    export.unlink()
    assert queue.purge(export, now="2032-10-16T00:00:00Z") == [closed["receipt_sha256"]]
    body = json.loads(export.read_text())
    assert (
        body["entries"][0]["disposition"]["disposition_code"]
        == "cancelled_by_requester"
    )
    assert export.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CountersignError):
        queue.get(closed["receipt_sha256"])
    assert not (queue.dir / f"{closed['receipt_sha256']}.notes.txt").exists()
    assert queue.get(still_open["receipt_sha256"])["status"] == "open"


def test_ledger_counts_add_up_across_all_four_buckets(issuer, approver, tsa, queue):
    registry = [issuer.registry_entry()]
    _vaulted(issuer, tsa, queue.vault, _receipt_doc(issuer, n=20))  # verified
    _, resolved, certs = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    fix = _vaulted(issuer, tsa, queue.vault, _fix(issuer))[0]  # verified too
    queue.resolve_by_seal(
        resolved["receipt_sha256"], fix, registry, tsa_certificates=certs
    )
    _, closed, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer, n=3))
    rec = queue.disposition_record(
        closed["receipt_sha256"],
        signer=approver,
        approver_id="appr-7f3a91",
        role="nurse-manager",
        disposition_code="superseded_by_external_workflow",
        verification_method="counterparty_confirmation",
        external_reference="INC-2",
    )
    queue.close_by_attestation(
        closed["receipt_sha256"], rec, [approver.registry_entry()]
    )
    _vaulted(issuer, tsa, queue.vault, _failed(issuer, n=4))  # open
    _vaulted(
        issuer, tsa, queue.vault, _failed(issuer, n=5), enqueue=False
    )  # never queued
    ledger = queue.ledger(registry, certs)
    assert ledger["counts"] == {
        "verified": 2,
        "resolved_by_seal": 1,
        "closed_by_attestation": 1,
        "open": 2,
    }
    assert ledger["total_seals"] == 6
    assert len(ledger["exceptions"]) == 4
    assert "004217" not in json.dumps(ledger)


def test_cli_exceptions_resolve_close_and_purge(issuer, approver, tmp_path, capsys):
    from cryptography.hazmat.primitives import serialization

    from openadapt_flow.__main__ import main

    keys = tmp_path / "seal-keys.json"
    keys.write_text(json.dumps({"keys": [issuer.registry_entry()]}))
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"mrn": "004217"}))
    vault = tmp_path / "vault"
    tsa_dir = tmp_path / "tsa"
    common = ["--fields-file", str(fields), "--vault", str(vault), "--keys", str(keys)]
    common += ["--tsa-local", str(tsa_dir)]

    def add(doc, name):
        src = tmp_path / f"{name}.json"
        src.write_text(json.dumps(doc))
        out = tmp_path / f"{name}-cs.json"
        assert main(["countersign", "add", str(src), "-o", str(out)] + common) == 0
        printed = capsys.readouterr().out
        return (
            out,
            verify_countersigned(
                json.loads(out.read_text()), registry=[issuer.registry_entry()]
            ).receipt_sha256,
            printed,
        )

    _, rsha, printed = add(_failed(issuer), "failed")
    assert "Exception open:" in printed
    _, other, _ = add(_failed(issuer, n=3), "failed2")
    assert main(["countersign", "exceptions", "--vault", str(vault)]) == 0
    listed = capsys.readouterr().out
    assert rsha in listed and other in listed and "REFUTED" in listed

    with pytest.raises(SystemExit):  # --with is required: no resolve by assertion
        main(["countersign", "resolve", rsha, "--vault", str(vault)])
    capsys.readouterr()

    fix, _, printed = add(_fix(issuer), "fix")
    assert "Exception" not in printed
    tsa_cert = tmp_path / "tsa.pem"
    tsa_cert.write_bytes(LocalTsa.load_or_create(tsa_dir).certificate_pem())
    resolve = [
        "countersign",
        "resolve",
        rsha,
        "--with",
        str(fix),
        "--vault",
        str(vault),
    ]
    assert main(resolve + ["--keys", str(keys), "--tsa-cert", str(tsa_cert)]) == 0
    assert "resolved_by_seal: receipt rcpt-0000000002" in capsys.readouterr().out

    approver_pem = tmp_path / "approver.pem"
    approver_pem.write_bytes(
        Ed25519PrivateKey.from_private_bytes(APPROVER_SEED).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    approver_keys = tmp_path / "approver-keys.json"
    approver_keys.write_text(json.dumps({"keys": [approver.registry_entry()]}))
    notes = tmp_path / "notes.txt"
    notes.write_text("phoned the pharmacy")
    close = ["countersign", "close", other, "--vault", str(vault)]
    close += ["--approver-id", "appr-7f3a91", "--role", "pharmacist"]
    close += [
        "--code",
        "remediated_manually_in_sor",
        "--method",
        "sor_direct_inspection",
    ]
    close += ["--external-reference", "INC-77", "--notes-file", str(notes)]
    close += ["--approver-key", str(approver_pem)]
    assert main(close + ["--approver-keys", str(keys)]) == 2  # approver not pinned
    assert "bad_disposition_signature" in capsys.readouterr().err
    assert main(close + ["--approver-keys", str(approver_keys)]) == 0
    assert "closed_by_attestation" in capsys.readouterr().out

    assert (
        main(
            [
                "countersign",
                "exceptions",
                "--vault",
                str(vault),
                "--json",
                "--status",
                "all",
            ]
        )
        == 0
    )
    entries = {e["receipt_sha256"]: e for e in json.loads(capsys.readouterr().out)}
    assert entries[rsha]["status"] == "resolved_by_seal"
    assert entries[other]["status"] == "closed_by_attestation"
    assert "INC-77" not in json.dumps(entries) and "pharmacy" not in json.dumps(entries)

    export = tmp_path / "export.json"
    assert (
        main(["countersign", "purge", "--export", str(export), "--vault", str(vault)])
        == 0
    )
    assert "Nothing is past" in capsys.readouterr().out


# -- timing and verify output ---------------------------------------------------


def test_check_timing_flags_skew_beyond_the_limit():
    from openadapt_flow.countersign.document import check_timing

    at = "2026-10-17T05:00:00Z"
    assert check_timing(at, "2026-10-17T05:05:00Z")["status"] == "ok"
    assert check_timing(at, "2026-10-17T04:55:00Z")["status"] == "ok"
    late = check_timing(at, "2026-10-17T05:05:01Z")
    assert late["status"] == "backdated_receipt" and late["delta_seconds"] == 301
    assert check_timing(at, "2026-10-17T06:00:01Z")["status"] == "late_timestamp"
    assert check_timing(at, "2026-10-17T04:54:59Z")["status"] == "clock_skew"
    assert (
        check_timing(None, "2026-10-17T05:00:00Z")["status"] == "issued_at_unreadable"
    )
    assert check_timing(at, "2026-10-17T05:00:30Z", max_skew_seconds=10)["status"] == (
        "backdated_receipt"
    )


@pytest.mark.parametrize(
    "offset,status",
    [
        (timedelta(minutes=1), "ok"),
        (timedelta(minutes=10), "backdated_receipt"),
        (timedelta(hours=3), "late_timestamp"),
        (-timedelta(minutes=10), "clock_skew"),
    ],
)
def test_verify_reports_timestamp_skew_visibly(issuer, offset, status):
    stamp = LocalTsa.generate(
        clock=_clock(T0 + offset), valid_from=T0 - timedelta(days=1)
    )
    out, _, _ = _countersigned(issuer, stamp)
    report = verify_countersigned(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates=[stamp.certificate],
        accept_non_flow=True,
    )
    assert report.timing["status"] == status
    assert report.timing["delta_seconds"] == int(offset.total_seconds())
    text = render_report(report)
    if status == "ok":
        assert report.warnings == [] and "WARNING" not in text
    else:
        assert report.warnings == [status]
        assert text.splitlines()[1].startswith(f"WARNING: WARNING {status}")
        assert f"{int(offset.total_seconds()):+d} s" in text


def test_verify_prints_tsa_facts_and_never_the_word_qualified(issuer, tsa):
    out, _, _ = _countersigned(issuer, tsa)
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], tsa_certificates=[tsa.certificate]
    )
    text = render_report(report)
    ts = report.receipt_timestamp
    for fact in (ts["gen_time"], ts["tsa_subject"], ts["policy"], ts["serial_number"]):
        assert fact and str(fact) in text
    assert "qualified" not in text.lower()


def test_dossier_and_docs_carry_the_disclaimer_and_metering_rule(issuer, tsa):
    from openadapt_flow.countersign.dossier import (
        DISCLAIMER,
        METERING_NOTE,
        render_html,
    )

    out, _, _ = _countersigned(issuer, tsa)
    report = verify_countersigned(
        out, registry=[issuer.registry_entry()], tsa_certificates=[tsa.certificate]
    )
    page = render_html({"document": out}, report)
    import html as _html

    assert _html.escape(DISCLAIMER) in page and _html.escape(METERING_NOTE) in page
    docs = " ".join(
        (Path(__file__).parents[1] / "docs" / "COUNTERSIGNED_SEALS.md")
        .read_text()
        .split()
    )
    assert DISCLAIMER in docs and METERING_NOTE in docs


def test_dossier_lists_every_exception_with_the_four_bucket_header(
    issuer, approver, tsa, queue, tmp_path, capsys
):
    from openadapt_flow.__main__ import main
    from openadapt_flow.countersign.dossier import extract_dossier_data

    registry = [issuer.registry_entry()]
    verified, _, certs = _vaulted(issuer, tsa, queue.vault, _receipt_doc(issuer, n=20))
    _, still_open, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer))
    _, closed, _ = _vaulted(issuer, tsa, queue.vault, _failed(issuer, n=3))
    rec = queue.disposition_record(
        closed["receipt_sha256"],
        signer=approver,
        approver_id="appr-7f3a91",
        role="nurse-manager",
        disposition_code="verified_manually_oracle_unavailable",
        verification_method="sor_direct_inspection",
        external_reference="INC-555",
        notes="secret note",
    )
    queue.close_by_attestation(
        closed["receipt_sha256"], rec, [approver.registry_entry()]
    )
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": registry}))
    pem = tmp_path / "tsa.pem"
    pem.write_bytes(tsa.certificate_pem())
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps(verified))
    out = tmp_path / "dossier.html"
    argv = ["countersign", "dossier", str(doc), "-o", str(out), "--keys", str(keys)]
    argv += [
        "--tsa-cert",
        str(pem),
        "--vault",
        str(queue.vault.root),
        "--accept-non-flow",
    ]
    assert main(argv) == 0
    header = (
        "Total Seals 3 = Verified 1 + Resolved_by_seal 0 + "
        "Closed_by_attestation 1 + Open 1"
    )
    assert header in capsys.readouterr().out
    page = out.read_text()
    assert header in page
    for e in (still_open, closed):
        assert e["receipt_sha256"] in page
    assert "verified_manually_oracle_unavailable" in page
    assert "INC-555" not in page and "secret note" not in page and "004217" not in page
    ledger = extract_dossier_data(page)["exception_ledger"]
    assert ledger["counts"]["open"] == 1 and len(ledger["exceptions"]) == 2
