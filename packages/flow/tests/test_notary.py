"""Notarized Seals: salted commitments, RFC 3161 timestamps, verify output, dossier.

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
pytest.importorskip("openadapt_types.seal")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from openadapt_types.seal import parse_key_registry, verify_sealed_receipt  # noqa: E402

from openadapt_flow.notary import commitments as C  # noqa: E402
from openadapt_flow.notary.dossier import (  # noqa: E402
    build_dossier,
    extract_dossier_data,
    write_dossier,
)
from openadapt_flow.notary.notarize import (  # noqa: E402
    EXTENSION_KEY,
    CloudNotaryIssuer,
    LocalNotaryIssuer,
    NotaryError,
    notarize_document,
    receipt_sha256,
    timestamp_baseline,
    verify_notarized,
)
from openadapt_flow.notary.render import render_report  # noqa: E402
from openadapt_flow.notary.tsa import (  # noqa: E402
    HttpTsa,
    LocalTsa,
    TimestampError,
    build_timestamp_request,
    verify_timestamp_token,
)

FIXTURES = Path(__file__).parent / "fixtures" / "notary"
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


@pytest.fixture
def issuer() -> LocalNotaryIssuer:
    return LocalNotaryIssuer(
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
    signer: LocalNotaryIssuer, *, kind="agent", outcome="verified", verdict="CONFIRMED"
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


def _execute_doc(signer: LocalNotaryIssuer) -> dict:
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


# -- notarize and verify -----------------------------------------------------


def _notarized(issuer, tsa, *, doc=None, fields=None, baseline=None, countersign=True):
    doc = doc or _effect_doc(issuer)
    block = kept = None
    if fields:
        block, kept = C.commit_fields(fields)
    out = notarize_document(
        doc,
        tsa=tsa,
        tsa_pem=tsa.certificate_pem(),
        commitments=block,
        baseline_timestamp=baseline,
        issuer=issuer if countersign else None,
    )
    return out, block, kept


def test_notarizing_leaves_the_receipt_and_its_seal_untouched(issuer, tsa):
    doc = _effect_doc(issuer)
    out, _, _ = _notarized(issuer, tsa, doc=doc, fields={"mrn": "004217"})
    assert out["receipt"] == doc["receipt"] and out["evidence"] == doc["evidence"]
    keys = parse_key_registry([issuer.registry_entry()])
    assert verify_sealed_receipt(
        out["receipt"], keys
    ).ok  # the existing verifier, unchanged
    assert "004217" not in json.dumps(out) and "mrn" not in json.dumps(out)
    with pytest.raises(NotaryError):
        notarize_document(out, tsa=tsa)


def test_agent_receipt_never_reads_like_a_flow_replay(issuer, tsa):
    out, _, _ = _notarized(issuer, tsa)
    reg = [issuer.registry_entry()]
    report = verify_notarized(out, registry=reg, tsa_certificates=[tsa.certificate])
    text = render_report(report)
    assert report.exit_code == 1
    assert text.splitlines()[0].startswith(
        "SIGNATURE GENUINE, NOT A FLOW REPLAY (AGENT)"
    )
    assert "AGENT: a computer-use agent acted. This is not a Flow replay" in text
    assert "independent_readback" in text
    assert "Receipt timestamp   VERIFIED 2026-10-17T05:01:00Z" in text
    accepted = verify_notarized(
        out, registry=reg, tsa_certificates=[tsa.certificate], accept_non_flow=True
    )
    assert accepted.exit_code == 0
    assert render_report(accepted).startswith(
        "SEAL VERIFIED FOR A AGENT RUN (not a Flow replay)"
    )


def test_declared_flow_replay_in_an_effect_receipt_is_not_trusted(issuer, tsa):
    out, _, _ = _notarized(issuer, tsa, doc=_effect_doc(issuer, kind="flow_replay"))
    report = verify_notarized(
        out, registry=[issuer.registry_entry()], tsa_certificates=[tsa.certificate]
    )
    assert report.exit_code == 1
    assert "DECLARED by the run record" in render_report(report)


def test_execute_receipt_reads_as_flow_replay(issuer, tsa):
    bare = _execute_doc(issuer)
    out, _, _ = _notarized(issuer, tsa, doc=bare)
    # A bare receipt is wrapped, never edited: legacy verifiers read "receipt".
    assert out["receipt"] == bare and "extensions" not in out["receipt"]
    keys = parse_key_registry([issuer.registry_entry()])
    assert verify_sealed_receipt(out["receipt"], keys).ok
    report = verify_notarized(
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
    out, _, _ = _notarized(
        issuer, tsa, doc=_effect_doc(issuer, outcome=outcome, verdict=verdict)
    )
    report = verify_notarized(
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
    out, _, _ = _notarized(issuer, tsa)
    report = verify_notarized(
        out, registry=[issuer.registry_entry()], accept_non_flow=True
    )
    assert report.receipt_timestamp["status"] == "not_checked"
    assert report.exit_code == 1
    # The certificate inside the document is a convenience, never a trust anchor.
    assert (
        "tsa_certificates_pem" in out["extensions"][EXTENSION_KEY]["receipt_timestamp"]
    )


def test_tampering_after_notarization_is_caught(issuer, tsa):
    out, _, _ = _notarized(issuer, tsa)
    reg = [issuer.registry_entry()]
    edited = json.loads(json.dumps(out))
    edited["receipt"]["executor_kind"] = "flow_replay"
    report = verify_notarized(edited, registry=reg, tsa_certificates=[tsa.certificate])
    assert report.seal["status"] == "receipt_mismatch"
    assert "statement_mismatch" in report.problems

    # A fresh timestamp over a forged statement does not survive the statement check.
    swapped = json.loads(json.dumps(out))
    swapped["extensions"][EXTENSION_KEY]["statement"]["nonce"] = "nonce-forged000000"
    report = verify_notarized(
        swapped, registry=reg, tsa_certificates=[tsa.certificate], accept_non_flow=True
    )
    assert "statement_mismatch" in report.problems and report.exit_code == 1


def test_countersignature_reuses_the_seal_scheme(issuer, tsa):
    out, _, _ = _notarized(issuer, tsa)
    block = out["extensions"][EXTENSION_KEY]
    att = block["countersignature"]["attestation"]
    assert att["schema_version"] == "openadapt.notary-attestation/v1"
    assert (
        block["countersignature"]["seal_signature"]["schema"]
        == "openadapt.seal-signature/v1"
    )
    report = verify_notarized(
        out,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert report.countersignature["status"] == "verified"
    other = LocalNotaryIssuer(Ed25519PrivateKey.generate())
    forged = json.loads(json.dumps(out))
    forged["extensions"][EXTENSION_KEY]["countersignature"]["seal_signature"] = (
        other.countersign(att)
    )
    report = verify_notarized(
        forged,
        registry=[issuer.registry_entry()],
        tsa_certificates=[tsa.certificate],
        accept_non_flow=True,
    )
    assert report.countersignature["status"] == "unknown_key" and report.exit_code == 1
    with pytest.raises(NotaryError):
        CloudNotaryIssuer().countersign(att)


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
    out = notarize_document(doc, tsa=late, baseline_timestamp=good, issuer=issuer)
    report = verify_notarized(
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
    out = notarize_document(doc, tsa=late, baseline_timestamp=other_run)
    assert (
        verify_notarized(
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
    out = notarize_document(doc, tsa=late, baseline_timestamp=after)
    assert (
        verify_notarized(
            out, registry=reg, tsa_certificates=[early.certificate]
        ).baseline_timestamp["status"]
        == "after_receipt"
    )


def test_disclosure_counts_only_against_bound_commitments(issuer, tsa):
    out, block, kept = _notarized(
        issuer, tsa, fields={"mrn": "004217", "dob": "1961-03-04"}, countersign=False
    )
    rsha = receipt_sha256(out["receipt"])
    label = next(s.label for s in kept if s.name == "mrn")
    bundle = C.build_disclosure(block, kept, [label], receipt_sha256=rsha)
    reg = [issuer.registry_entry()]
    report = verify_notarized(
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
    report = verify_notarized(
        out, registry=reg, disclosures=[bundle], accept_non_flow=True
    )
    assert "commitments_unbound" in report.problems
    wrong = json.loads(json.dumps(bundle))
    wrong["fields"][0]["value"] = "004218"
    report = verify_notarized(
        out,
        registry=reg,
        tsa_certificates=[tsa.certificate],
        disclosures=[wrong],
        accept_non_flow=True,
    )
    assert "disclosure_mismatch" in report.problems and report.exit_code == 1


# -- (c) dossier -------------------------------------------------------------


def test_dossier_embeds_no_phi_by_default_and_reverifies(issuer, tsa, tmp_path):
    out, block, kept = _notarized(
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
    assert "SEAL VERIFIED FOR A AGENT RUN" in page
    again = extract_dossier_data(page)
    re_report = verify_notarized(
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
    out, block, kept = _notarized(
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


def test_cli_notarize_disclose_verify_dossier(issuer, tmp_path, capsys, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    from openadapt_flow.__main__ import main

    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_PATH", raising=False)
    doc = tmp_path / "receipt.json"
    doc.write_text(json.dumps(_effect_doc(issuer)))
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"mrn": "004217"}))
    key = tmp_path / "notary.pem"
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
    notarized = tmp_path / "notarized.json"
    tsa_dir = tmp_path / "tsa"
    assert (
        main(
            [
                "notary",
                "notarize",
                str(doc),
                "-o",
                str(notarized),
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
    assert (notarized.stat().st_mode & 0o777) == 0o600
    rsha = receipt_sha256(json.loads(doc.read_text())["receipt"])
    secrets_file = vault / rsha / "field-secrets.json"
    assert (
        secrets_file.stat().st_mode & 0o777
    ) == 0o600 and "004217" in secrets_file.read_text()
    assert "004217" not in notarized.read_text()
    label = next(
        iter(
            json.loads(notarized.read_text())["extensions"][EXTENSION_KEY][
                "commitments"
            ]["fields"]
        )
    )
    disclosure = tmp_path / "disclosure.json"
    assert (
        main(
            [
                "notary",
                "disclose",
                str(notarized),
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
        "notary",
        "verify",
        str(notarized),
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
                "notary",
                "dossier",
                str(notarized),
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
                "notary",
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
