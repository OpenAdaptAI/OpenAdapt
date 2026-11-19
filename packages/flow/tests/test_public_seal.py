"""Public Seals: an allow-listed card, signed, small, the same on every rebuild."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.seal import canonical_json

from openadapt_flow.ir import IdentityCheck, Resolution, RunReport, StepResult
from openadapt_flow.offline_seal import seal_run, sign_subject
from openadapt_flow.public_seal import (
    MAX_BYTES,
    PublicSealError,
    build_card,
    build_public_seal,
    card_sha256,
    public_run_id,
    public_seal_bytes,
    verify_public_seal,
    write_public_seal,
)
from openadapt_flow.seal_keys import SoftwareSigner

NOW = datetime(2026, 11, 18, 12, 34, 56, tzinfo=timezone.utc)
BUNDLE = "b" * 64
VECTORS = Path(__file__).parent / "fixtures" / "public_seal" / "golden_vectors.json"

#: Values that must never leave the computer. Each is planted in a different
#: part of the report (or the folder name) and searched for in the output.
PLANTED = {
    "param": "Jane Q Planted",
    "intent": "Type MRN 4417-PLANTED into chart",
    "halt": "Patient Planted-Halt DOB 1961-02-03 did not match",
    "observed": "PLANTEDOBSERVED Smith 1980-01-01",
    "folder": "patient-planted-folder-9931",
    "run_id": "run-planted-id-5521",
    "url": "https://emr.example.test/patient/PLANTEDURL",
}


def _signer(seed: int = 7) -> SoftwareSigner:
    return SoftwareSigner(
        Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32), "file"
    )


def _results(count: int, *, stop_at: int | None = None) -> list[StepResult]:
    rungs = ["structural", "template", "ocr", "api"]
    out = []
    for index in range(count):
        ok = stop_at is None or index < stop_at
        out.append(
            StepResult(
                step_id=f"s{index}",
                intent=f"{PLANTED['intent']} {index}",
                ok=ok,
                resolution=Resolution(
                    rung=rungs[index % 4]
                    if rungs[index % 4] != "api"
                    else "structural",
                    point=(10, 20),
                    confidence=1.0,
                    elapsed_ms=1.0,
                )
                if ok
                else None,
                identity=IdentityCheck(status="verified", observed=PLANTED["observed"])
                if index % 3 == 0
                else None,
                postconditions_ok=True if index % 3 == 1 else None,
                effect_verified=True if index % 3 == 2 and ok else None,
            )
        )
    return out


def _report(count: int = 4, *, stop_at: int | None = None, **extra) -> dict:
    report = RunReport(
        workflow_name="add-visit-note",
        started_at="2026-11-18T09:41:07+00:00",
        execution_outcome="HALTED" if stop_at is not None else "VERIFIED",
        results=_results(count, stop_at=stop_at),
        params={"patient_name": PLANTED["param"], "entry_url": PLANTED["url"]},
        halt={"reason": PLANTED["halt"], "observed_texts": [PLANTED["observed"]]}
        if stop_at is not None
        else None,
        bundle_content_digest=BUNDLE,
        run_id=PLANTED["run_id"],
        heal_count=1,
        model_calls=0,
        total_ms=1234.6,
    )
    data = json.loads(report.model_dump_json())
    data.update(extra)
    return data


def _sealed_run(tmp_path: Path, report: dict, signer=None, name=None) -> Path:
    run = tmp_path / (name or PLANTED["folder"])
    run.mkdir()
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    seal_run(
        run, home=tmp_path / "home", now=NOW, enqueue=False, signer=signer or _signer()
    )
    return run


def test_planted_values_never_reach_the_public_seal(tmp_path):
    run = _sealed_run(tmp_path, _report(6, stop_at=4))
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    path = write_public_seal(run, doc)
    raw = path.read_bytes()
    for where, value in PLANTED.items():
        assert value.encode() not in raw, where
    for word in (b"Planted", b"PLANTED", b"planted", b"4417", b"1961", b"emr.example"):
        assert word not in raw
    card = doc["card"]
    # A real run: no name unless asked for.
    assert card["program_name"] is None
    assert set(card) == {
        "schema",
        "outcome",
        "report_sha256",
        "bundle_digest",
        "steps",
        "steps_total",
        "steps_ok",
        "heals",
        "model_calls",
        "duration_ms",
        "external_network_calls",
        "checks",
        "provenance",
        "flow_version",
        "program_name",
        "generated_at",
    }


def test_round_trip_and_the_card_is_what_is_signed(tmp_path):
    run = _sealed_run(tmp_path, _report())
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    assert verify_public_seal(public_seal_bytes(doc)).ok
    assert doc["seal"]["subject_kind"] == "public-card"
    assert doc["seal"]["subject_sha256"] == card_sha256(doc["card"])
    private = json.loads((run / "seal.json").read_text())
    assert doc["card"]["report_sha256"] == private["subject_sha256"]
    assert doc["card"]["generated_at"] == "2026-11-18T09:00:00Z"
    tampered = json.loads(public_seal_bytes(doc))
    tampered["card"]["heals"] = 0
    assert verify_public_seal(canonical_json(tampered)).reason == "card_mismatch"
    # The same document with other spacing is not the bytes that were signed.
    assert verify_public_seal(json.dumps(doc)).reason == "malformed"


def test_authorization_and_scope_item_are_carried_and_signed(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(_report()), encoding="utf-8")
    raw = (run / "report.json").read_bytes()
    seal = sign_subject(
        "run-report",
        hashlib.sha256(raw).hexdigest(),
        "run-1",
        outcome="VERIFIED",
        now=NOW,
        signer=_signer(),
        authorization_sha256="a" * 64,
        scope_item="r3",
    )
    (run / "seal.json").write_text(json.dumps(seal.wire()), encoding="utf-8")
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    assert doc["seal"]["authorization_sha256"] == "a" * 64
    assert doc["seal"]["scope_item"] == "r3"
    assert verify_public_seal(public_seal_bytes(doc)).ok
    changed = json.loads(public_seal_bytes(doc))
    changed["seal"]["scope_item"] = "r4"
    assert verify_public_seal(canonical_json(changed)).reason == "bad_signature"


def test_a_64_step_card_fits_in_4096_bytes(tmp_path):
    run = _sealed_run(tmp_path, _report(64))
    doc = build_public_seal(run, signer=_signer(), now=NOW, github="a" * 39, names=True)
    assert len(public_seal_bytes(doc)) <= MAX_BYTES
    assert doc["card"]["steps_total"] == 64
    # The strip is whole or empty, never cut short.
    assert len(doc["card"]["steps"]) in (0, 64)
    assert verify_public_seal(public_seal_bytes(doc)).ok


def test_a_short_run_keeps_its_whole_step_strip(tmp_path):
    run = _sealed_run(tmp_path, _report(12))
    card = build_public_seal(run, signer=_signer(), now=NOW)["card"]
    assert len(card["steps"]) == 12
    assert card["steps"][0] == {
        "ok": True,
        "rung": "structural",
        "evidence_class": "identity",
    }
    assert card["steps"][1]["evidence_class"] == "postcondition"
    assert card["steps"][2]["evidence_class"] == "effect"


def test_a_stopped_run_gets_a_card(tmp_path):
    run = _sealed_run(tmp_path, _report(5, stop_at=2))
    assert not (run / "receipt.json").exists()
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    card = doc["card"]
    assert card["outcome"] == "HALTED" == doc["seal"]["outcome"]
    assert card["steps_ok"] == 2 and card["steps_total"] == 5
    assert card["steps"][3]["ok"] is False and card["steps"][3]["rung"] == "none"
    assert card["provenance"] == "production"


def test_the_run_id_is_hashed_and_stable(tmp_path):
    run = _sealed_run(tmp_path, _report())
    signer = _signer()
    first = build_public_seal(run, signer=signer, now=NOW)
    private = json.loads((run / "seal.json").read_text())
    assert first["seal"]["run_id"] == public_run_id(signer.key_id, private["run_id"])
    assert private["run_id"] not in first["seal"]["run_id"]
    write_public_seal(run, first)
    later = datetime(2026, 11, 19, 1, 0, 0, tzinfo=timezone.utc)
    again = build_public_seal(run, signer=signer, now=later)
    # Same card and key: the Seal on disk is reused, so its digest is the same.
    assert again == first
    other_card = build_public_seal(run, signer=signer, now=later, names=True)
    assert other_card["seal"]["run_id"] == first["seal"]["run_id"]
    assert other_card["seal"]["issued_at"] != first["seal"]["issued_at"]


def test_names_that_fail_the_pattern_are_refused(tmp_path):
    run = _sealed_run(tmp_path, _report(workflow_name="Jane's chart / notes"))
    with pytest.raises(PublicSealError) as exc:
        build_public_seal(run, signer=_signer(), now=NOW, names=True)
    assert exc.value.code == "name_not_allowed"
    assert "--names" in str(exc.value)
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    assert doc["card"]["program_name"] is None
    assert "app_name" not in doc["card"]


def test_names_default_off_for_real_runs_and_on_for_the_tutorial(tmp_path):
    run = _sealed_run(tmp_path, _report())
    assert build_card(run)[0]["program_name"] is None
    assert build_card(run, names=True)[0]["program_name"] == "add-visit-note"
    assert build_card(run, provenance="synthetic-tutorial")[0]["program_name"] == (
        "add-visit-note"
    )
    assert (
        build_card(run, provenance="synthetic-tutorial", names=False)[0]["program_name"]
        is None
    )
    # A real run's report carrying an app field still never shows it.
    app_run = _sealed_run(tmp_path, _report(app="OpenEMR"), name="app")
    assert "app_name" not in build_card(app_run, names=True)[0]


def test_unsealed_and_other_key_runs_are_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(_report()), encoding="utf-8")
    with pytest.raises(PublicSealError) as exc:
        build_card(run)
    assert exc.value.code == "unsealed"
    seal_run(run, home=tmp_path / "home", now=NOW, enqueue=False, signer=_signer(1))
    with pytest.raises(PublicSealError) as exc:
        build_public_seal(run, signer=_signer(2), now=NOW)
    assert exc.value.code == "key_mismatch"
    (run / "report.json").write_text(json.dumps(_report(3)), encoding="utf-8")
    with pytest.raises(PublicSealError) as exc:
        build_card(run)
    assert exc.value.code == "subject_mismatch"


def test_receipt_provenance_marks_the_tutorial(tmp_path):
    run = _sealed_run(tmp_path, _report())
    (run / "receipt.json").write_text(json.dumps({"provenance": "synthetic-tutorial"}))
    assert build_card(run)[0]["provenance"] == "synthetic-tutorial"
    assert build_card(run, provenance="production")[0]["provenance"] == "production"


def test_contract_counts_come_from_the_outcome_envelope(tmp_path):
    envelope = {
        "required_contracts": {
            "authorization": 1,
            "identity": 5,
            "postcondition": 9,
            "effect": 2,
        },
        "passed_contracts": {
            "authorization": 1,
            "identity": 5,
            "postcondition": 9,
            "effect": 0,
        },
    }
    run = _sealed_run(tmp_path, _report(outcome_envelope=envelope))
    checks = build_card(run)[0]["checks"]
    assert checks == {
        "identity": {"required": 5, "confirmed": 5},
        "postconditions": {"required": 9, "confirmed": 9},
        "effects": {"required": 2, "confirmed": 0},
        "authorization": {"required": 1, "confirmed": 1},
    }


def test_the_written_file_is_compact_canonical_bytes(tmp_path):
    run = _sealed_run(tmp_path, _report())
    doc = build_public_seal(run, signer=_signer(), now=NOW)
    path = write_public_seal(run, doc)
    assert path.read_bytes() == canonical_json(doc).encode()


def test_local_seal_public_cli(tmp_path, capsys, monkeypatch):
    from openadapt_flow.__main__ import main

    home = tmp_path / "home"
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(_report()), encoding="utf-8")
    seal_run(run, home=home, now=NOW, enqueue=False)
    code = main(["local-seal", "--home", str(home), "public", str(run)])
    assert code in (0, None)
    printed = json.loads(capsys.readouterr().out)
    assert printed["card"]["program_name"] is None
    assert verify_public_seal((run / "public-seal.json").read_bytes()).ok
    (run / "public-seal.json").unlink()
    code = main(["local-seal", "--home", str(home), "public", str(run), "--names"])
    assert code in (0, None)
    printed = json.loads(capsys.readouterr().out)
    assert printed["card"]["program_name"] == "add-visit-note"


# -- golden vectors, shared with Cloud's TypeScript verifier -------------------


def _resign(doc: dict, signer: SoftwareSigner, **seal_changes) -> dict:
    from openadapt_flow.offline_seal import signing_payload

    seal = {k: v for k, v in doc["seal"].items() if k != "signature"}
    seal.update(seal_changes)
    seal["subject_sha256"] = card_sha256(doc["card"])
    seal["signature"] = base64.b64encode(signer.sign(signing_payload(seal))).decode()
    return {"card": doc["card"], "seal": seal}


def _vectors(tmp_path: Path) -> dict:
    signer = _signer(42)
    plain = _sealed_run(tmp_path, _report(6, stop_at=4), signer, "a")
    base = build_public_seal(
        plain,
        signer=signer,
        now=NOW,
        provenance="synthetic-tutorial",
        flow_version="3.1.0",
    )
    authed = tmp_path / "b"
    authed.mkdir()
    (authed / "report.json").write_text(json.dumps(_report(3)), encoding="utf-8")
    seal = sign_subject(
        "run-report",
        hashlib.sha256((authed / "report.json").read_bytes()).hexdigest(),
        "run-b",
        outcome="VERIFIED",
        now=NOW,
        signer=signer,
        authorization_sha256="c" * 64,
        scope_item="r3",
    )
    (authed / "seal.json").write_text(json.dumps(seal.wire()), encoding="utf-8")
    with_auth = build_public_seal(
        authed, signer=signer, now=NOW, github="octo-cat", flow_version="3.1.0"
    )
    text = lambda doc: public_seal_bytes(doc).decode()  # noqa: E731

    cases = [
        ("valid", text(base), "valid"),
        ("valid_authorization", text(with_auth), "valid"),
    ]

    bad_sig = json.loads(text(base))
    sig = base64.b64decode(bad_sig["seal"]["signature"])
    bad_sig["seal"]["signature"] = base64.b64encode(
        bytes([sig[0] ^ 1]) + sig[1:]
    ).decode()
    cases.append(("bad_signature", canonical_json(bad_sig), "bad_signature"))

    mismatch = json.loads(text(base))
    mismatch["card"]["heals"] += 1
    cases.append(("digest_mismatch", canonical_json(mismatch), "card_mismatch"))

    unknown = json.loads(text(base))
    unknown["card"]["patient"] = "x"
    cases.append(
        ("unknown_field_card", canonical_json(_resign(unknown, signer)), "malformed")
    )

    nested = json.loads(text(base))
    nested["card"]["checks"]["effects"]["note"] = 1
    cases.append(
        ("unknown_field_nested", canonical_json(_resign(nested, signer)), "malformed")
    )

    top = json.loads(text(base))
    top["extra"] = 1
    cases.append(("unknown_field_top", canonical_json(top), "malformed"))

    body = text(base)
    duplicate = body.replace('"heals":', '"heals":0,"heals":', 1)
    cases.append(("duplicate_key", duplicate, "malformed"))

    pattern = json.loads(text(base))
    pattern["card"]["program_name"] = "notes/Jane"
    cases.append(
        ("pattern_miss", canonical_json(_resign(pattern, signer)), "malformed")
    )

    wrong_id = json.loads(text(base))
    resigned = _resign(wrong_id, signer, key_id="seal-ed25519-" + "0" * 16)
    cases.append(("key_id_mismatch", canonical_json(resigned), "key_id_mismatch"))

    kind = json.loads(text(base))
    cases.append(
        (
            "not_public_card",
            canonical_json(_resign(kind, signer, subject_kind="file")),
            "not_public_card",
        )
    )

    unnamed = _sealed_run(tmp_path, _report(2), signer, "c")
    no_names = build_public_seal(unnamed, signer=signer, now=NOW, flow_version="3.1.0")
    assert no_names["card"]["program_name"] is None
    cases.append(("valid_no_names", text(no_names), "valid"))

    long_run = _sealed_run(tmp_path, _report(64), signer, "d")
    stripless = build_public_seal(
        long_run, signer=signer, now=NOW, flow_version="3.1.0"
    )
    assert stripless["card"]["steps"] == []
    cases.append(("valid_64_steps_no_strip", text(stripless), "valid"))

    partial = json.loads(text(base))
    partial["card"]["steps"] = partial["card"]["steps"][:3]
    cases.append(
        ("partial_strip", canonical_json(_resign(partial, signer)), "malformed")
    )

    outcome = json.loads(text(base))
    outcome["seal"]["outcome"] = "VERIFIED"
    resigned_outcome = _resign(outcome, signer)
    cases.append(
        ("seal_outcome_differs", canonical_json(resigned_outcome), "malformed")
    )

    newline_id = json.loads(text(base))
    newline_seal = _resign(newline_id, signer, run_id=base["seal"]["run_id"] + "\n")
    cases.append(("run_id_trailing_newline", canonical_json(newline_seal), "malformed"))

    no_schema = json.loads(text(base))
    del no_schema["card"]["schema"]
    cases.append(
        ("card_schema_missing", canonical_json(_resign(no_schema, signer)), "malformed")
    )

    no_issuer = json.loads(text(base))
    no_issuer_seal = {k: v for k, v in no_issuer["seal"].items() if k != "issuer"}
    cases.append(
        (
            "seal_issuer_missing",
            canonical_json(
                _resign({"card": no_issuer["card"], "seal": no_issuer_seal}, signer)
            ),
            "malformed",
        )
    )

    null_claim = json.loads(text(base))
    null_claim["card"]["identity_claim"] = None
    cases.append(
        (
            "identity_claim_null",
            canonical_json(_resign(null_claim, signer)),
            "malformed",
        )
    )

    huge = json.loads(text(base))
    huge["card"]["duration_ms"] = 2**53
    cases.append(
        ("count_over_2_53", canonical_json(_resign(huge, signer)), "malformed")
    )

    padded = body[:-1] + " " * (MAX_BYTES + 1 - len(body)) + "}"
    assert len(padded.encode()) == MAX_BYTES + 1
    cases.append(("too_large_4097", padded, "too_large"))

    # Where Python and TypeScript could read the same input differently.
    # Each is refused (or accepted) the same way by both verifiers.
    with_app = json.loads(text(base))
    with_app["card"]["app_name"] = "MockMed"
    cases.append(
        ("app_name_present", canonical_json(_resign(with_app, signer)), "malformed")
    )

    cases.append(("not_canonical_spacing", body.replace(",", ", ", 1), "malformed"))
    cases.append(("byte_order_mark", "\ufeff" + body, "malformed"))
    zero = text(base)
    assert '"model_calls":0,' in zero
    cases.append(
        (
            "negative_zero",
            zero.replace('"model_calls":0,', '"model_calls":-0,', 1),
            "malformed",
        )
    )

    digit = json.loads(text(base))
    digit["card"]["flow_version"] = "3.1.\u0663"  # ARABIC-INDIC DIGIT THREE
    cases.append(
        ("non_ascii_digit", canonical_json(_resign(digit, signer)), "malformed")
    )

    null_scope = json.loads(text(with_auth))
    null_scope["seal"]["scope_item"] = None
    cases.append(
        ("seal_field_null", canonical_json(_resign(null_scope, signer)), "malformed")
    )

    # The last base64 character of a 64-byte signature carries 4 unused bits;
    # setting one spells the same bytes differently.
    respelled = json.loads(text(base))
    signature = respelled["seal"]["signature"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    last = alphabet[alphabet.index(signature[-3]) | 1]
    respelled["seal"]["signature"] = signature[:-3] + last + "=="
    assert base64.b64decode(respelled["seal"]["signature"]) == base64.b64decode(
        signature
    )
    cases.append(("signature_base64_respelled", canonical_json(respelled), "malformed"))

    over = json.loads(text(base))
    over["card"]["checks"]["effects"] = {"required": 0, "confirmed": 1}
    cases.append(
        ("confirmed_over_required", canonical_json(_resign(over, signer)), "valid")
    )

    return {
        "about": (
            "Golden vectors for openadapt.public-seal/v1, generated by "
            "packages/flow/tests/test_public_seal.py with a throwaway test key. "
            "Each body is the exact upload bytes; expect is 'valid' or the "
            "verifier's refusal. Regenerate with OPENADAPT_REGEN_VECTORS=1."
        ),
        "signing_domain": "openadapt.local-seal/v1\x00",
        "max_bytes": MAX_BYTES,
        "public_key": base64.b64encode(signer.public_raw()).decode(),
        "key_id": signer.key_id,
        "cases": [{"name": n, "body": b, "expect": e} for n, b, e in cases],
    }


#: SHA-256 of golden_vectors.json. openadapt-cloud keeps a byte copy
#: (tests/fixtures/public-seal/golden_vectors.json) pinned to the same hash.
#: When this changes, copy the file to Cloud and update both hashes.
VECTORS_SHA256 = "3a4876270a6cb75a212305214c31f8fddc0aa27c8a1dec4de56284bb1919ca44"


def test_golden_vectors_verify_as_labelled(tmp_path):
    vectors = _vectors(tmp_path)
    for case in vectors["cases"]:
        result = verify_public_seal(case["body"].encode())
        got = "valid" if result.ok else result.reason
        assert got == case["expect"], case["name"]
    if os.environ.get("OPENADAPT_REGEN_VECTORS"):
        VECTORS.parent.mkdir(parents=True, exist_ok=True)
        VECTORS.write_text(json.dumps(vectors, indent=2) + "\n", encoding="utf-8")
    committed = json.loads(VECTORS.read_text(encoding="utf-8"))
    for case in committed["cases"]:
        result = verify_public_seal(case["body"].encode())
        assert ("valid" if result.ok else result.reason) == case["expect"], case["name"]
    assert {c["name"] for c in committed["cases"]} == {
        c["name"] for c in vectors["cases"]
    }
    assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == VECTORS_SHA256, (
        "golden_vectors.json changed: copy it to openadapt-cloud "
        "tests/fixtures/public-seal/ and update VECTORS_SHA256 in both repos"
    )


def test_a_development_flow_version_is_a_closed_refusal(tmp_path):
    run = _sealed_run(tmp_path, _report(2), _signer(), "dev")
    with pytest.raises(PublicSealError) as caught:
        build_public_seal(run, signer=_signer(), now=NOW, flow_version="3.1.0.dev4")
    assert caught.value.code == "card_invalid"


def test_names_with_a_trailing_newline_are_refused():
    from openadapt_flow.public_seal import login_ok, name_ok

    assert name_ok("MockMed") and not name_ok("MockMed\n")
    assert login_ok("octo-cat") and not login_ok("octo-cat\n")
