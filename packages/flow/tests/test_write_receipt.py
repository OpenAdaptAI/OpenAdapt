"""Signed write receipts: issue, verify offline, refuse to overclaim.

The SQL cases run the real verifier on SQLite and shadow mode's real proxy
(the fixtures of test_verifier_api.py and test_shadow.py). Records are made
up. See docs/WRITE_RECEIPTS.md.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import cbor2
import pytest

from openadapt_flow import write_receipt as wr
from openadapt_flow.receipt_log import (
    LocalLog,
    decode_sign1,
    encode_sign1,
    inclusion_path,
    leaf_hash,
    receipt_id,
    tree_root,
)
from openadapt_flow.seal_anchor import root_from_inclusion
from openadapt_flow.verifier import MemoryLedger, begin_write, verify_write
from tests import test_shadow as shadow_fx
from tests import test_verifier_api as api_fx
from tests.test_shadow_fhir import _allergy, _send, env  # noqa: F401 - fixture


@pytest.fixture(autouse=True)
def _homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_RECEIPT_HOME", str(tmp_path / "receipts-home"))
    monkeypatch.delenv("OPENADAPT_SEAL_KEY", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_KEY_FILE", raising=False)


SCOPE: dict[str, Any] = {
    "read_paths": ["database"],
    "stores": ["sql", "frame.database"],
    "unread": [],
    "window": {
        "baseline_read_at": "2026-11-02T10:00:00Z",
        "read_at": "2026-11-02T10:00:05Z",
        "max_execution_window_s": 60,
    },
    "strength": "independent_system_of_record",
    "not_covered": ["writes that land after read_at (delayed or queued writes)"],
}
HELD = {"delta": "held", "frame": "held", "source": "instruction"}


def _issue(tmp_path: Path, name: str = "w", **kw: Any) -> wr.Issued:
    fields: dict[str, Any] = {
        "source": "verifier",
        "verdict": "CONFIRMED",
        "scope": SCOPE,
        "checked": HELD,
        "write_id": f"write-{name}",
    }
    fields.update(kw)
    return wr.issue(tmp_path / f"{name}.receipt.cbor", evidence={"n": name}, **fields)


def _keys() -> tuple[bytes, bytes]:
    return wr.default_signer().public_raw(), LocalLog().public_raw()


def _verify(data: bytes, **kw: Any) -> wr.Verification:
    issuer, log = _keys()
    return wr.verify(data, issuer_public=issuer, log_public=log, **kw)


def test_a_receipt_verifies_offline_with_its_evidence(tmp_path: Path) -> None:
    issued = [_issue(tmp_path, str(i)) for i in range(3)]
    for i, one in enumerate(issued):
        v = _verify(one.path.read_bytes(), evidence=one.evidence_path.read_bytes())
        assert v.payload.verdict == "CONFIRMED" and v.log_index == i
        assert v.evidence_opened is True
        assert v.sha256 == receipt_id(one.path.read_bytes())
    # The evidence (with its salt) is private; the payload holds no values.
    assert issued[0].evidence_path.stat().st_mode & 0o077 == 0
    payload = json.loads(decode_sign1(issued[0].path.read_bytes())[3])
    assert "evidence" not in payload and '"n"' not in json.dumps(payload)


def test_a_receipt_always_says_what_it_did_not_check(tmp_path: Path) -> None:
    p = _issue(tmp_path).payload
    for line in wr.ALWAYS_NOT_CHECKED + tuple(SCOPE["not_covered"]):
        assert line in p.not_checked
    unread = {**SCOPE, "unread": ["audit"]}
    q = _issue(tmp_path, "u", verdict="UNCERTAIN", scope=unread).payload
    assert "store not read: audit" in q.not_checked


@pytest.mark.parametrize(
    "change",
    [
        {"scope": {**SCOPE, "strength": "unchecked"}},
        {"scope": {**SCOPE, "stores": [], "read_paths": []}},
        {"scope": {**SCOPE, "unread": ["audit"]}},
        {"scope": {**SCOPE, "window": {**SCOPE["window"], "read_at": None}}},
        {"checked": {**HELD, "frame": "refuted"}},
        {"scope": {**SCOPE, "stores": ["SQL; DROP"]}},
        {"scope": {**SCOPE, "not_covered": ["line\nbreak"]}},
        {"verdict": "VERIFIED"},
        {"write_id": "has spaces"},
    ],
)
def test_a_receipt_that_would_claim_more_than_the_scope_is_not_issued(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    with pytest.raises(wr.ReceiptError):
        _issue(tmp_path, **change)
    assert not (tmp_path / "w.receipt.cbor").exists()
    assert not (tmp_path / "w.receipt.cbor.evidence.json").exists()


def test_a_refuted_or_uncertain_receipt_can_have_read_nothing(tmp_path: Path) -> None:
    nothing = {
        **SCOPE,
        "read_paths": [],
        "stores": [],
        "strength": "unchecked",
        "window": {
            "baseline_read_at": None,
            "read_at": None,
            "max_execution_window_s": None,
        },
    }
    v = _verify(_issue(tmp_path, verdict="UNCERTAIN", scope=nothing).path.read_bytes())
    assert v.payload.verdict == "UNCERTAIN"


def test_a_provisional_confirmation_says_so(tmp_path: Path) -> None:
    p = _issue(tmp_path, recheck_due_at="2026-11-02T10:05:05Z").payload
    assert p.provisional and p.recheck_due_at == "2026-11-02T10:05:05Z"
    # Only a CONFIRMED carries a due recheck.
    q = _issue(tmp_path, "r", verdict="REFUTED", recheck_due_at="2026-11-02T10:05:05Z")
    assert not q.payload.provisional and q.payload.recheck_due_at is None


def _resign(data: bytes, payload: dict[str, Any], **header: Any) -> bytes:
    """What a holder of the issuer key could sign by hand, bypassing issue()."""
    from openadapt_flow.receipt_log import CWT_CLAIMS

    _, headers, unprot, _, _ = decode_sign1(data)
    headers.update(header)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if CWT_CLAIMS in headers:
        headers[CWT_CLAIMS][2] = payload["subject"]["write_id"]
    return encode_sign1(headers, unprot, body, wr.default_signer().sign)


def test_verify_rejects_an_overclaim_even_when_the_issuer_signed_it(
    tmp_path: Path,
) -> None:
    data = _issue(tmp_path).path.read_bytes()
    payload = json.loads(decode_sign1(data)[3])
    for edit in (
        {"not_checked": []},
        {"scope": {**payload["scope"], "strength": "unchecked"}},
        {"provisional": True},
        {"extra": 1},
    ):
        forged = _resign(data, {**payload, **edit})
        with pytest.raises(wr.ReceiptError):
            _verify(forged)


def test_verify_rejects_tampering_and_the_wrong_keys(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    one, two = _issue(tmp_path, "a"), _issue(tmp_path, "b")
    data = one.path.read_bytes()
    issuer, log = _keys()
    other = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    # A changed verdict breaks the signature.
    flipped = data.replace(b'"verdict":"CONFIRMED"', b'"verdict":"UNCERTAIN"')
    assert flipped != data
    codes = []
    for bad, keys in (
        (flipped, (issuer, log)),
        (data, (other, log)),
        (data, (issuer, other)),
    ):
        with pytest.raises(wr.ReceiptError) as err:
            wr.verify(bad, issuer_public=keys[0], log_public=keys[1])
        codes.append(err.value.code)
    assert codes == ["bad_signature", "wrong_issuer", "bad_inclusion"]
    # Another receipt's log entry doesn't fit this one.
    prot, _, _, body, sig = decode_sign1(data)
    theirs = decode_sign1(two.path.read_bytes())[2]
    swapped = cbor2.dumps(cbor2.CBORTag(18, [prot, theirs, body, sig]), canonical=True)
    with pytest.raises(wr.ReceiptError, match="bad_inclusion"):
        _verify(swapped)
    # A receipt with no log entry needs --no-log, and says so.
    bare = cbor2.dumps(cbor2.CBORTag(18, [prot, {}, body, sig]), canonical=True)
    with pytest.raises(wr.ReceiptError, match="not_logged"):
        _verify(bare)
    v = wr.verify(bare, issuer_public=issuer, log_public=None)
    assert v.notes == ["log inclusion not checked (--no-log)"]
    # The other receipt's evidence doesn't open this one's commitment.
    with pytest.raises(wr.ReceiptError, match="evidence_mismatch"):
        _verify(data, evidence=two.evidence_path.read_bytes())


def test_a_non_canonical_header_is_rejected(tmp_path: Path) -> None:
    data = _issue(tmp_path).path.read_bytes()
    msg = cbor2.loads(data)
    prot = msg.value[0]
    # The same header, keys in another order: two parsers, one meaning only.
    reordered = cbor2.dumps(dict(reversed(list(cbor2.loads(prot).items()))))
    assert reordered != prot
    msg.value[0] = reordered
    with pytest.raises(wr.ReceiptError, match="malformed"):
        _verify(cbor2.dumps(msg))


@pytest.mark.parametrize("size", range(1, 18))
def test_inclusion_proofs_match_rfc_9162(size: int) -> None:
    leaves = [leaf_hash(bytes([i])) for i in range(size)]
    root = tree_root(leaves)
    for i in range(size):
        assert (
            root_from_inclusion(i, size, leaves[i], inclusion_path(i, leaves)) == root
        )


def test_the_verifier_api_issues_a_receipt_for_its_result(tmp_path: Path) -> None:
    db = api_fx._store(tmp_path)
    pending = begin_write(
        api_fx._contract(db), api_fx.PARAMS, actor=api_fx.BOT, ledger=MemoryLedger()
    )
    api_fx._write(db, api_fx.SAVE)
    result = verify_write(pending, write_receipt=True)
    assert result.verified and result.write_receipt == "issued"
    v = _verify(Path(str(result.write_receipt_path)).read_bytes())
    p = v.payload
    assert p.verdict == "CONFIRMED" and p.source == "verifier"
    assert (
        p.scope.stores == ["sql"] and p.scope.strength == "independent_system_of_record"
    )
    assert p.subject.verify_run_receipt_sha256 == result.receipt_sha256
    assert p.subject.write_id == result.write_id
    assert list(p.scope.not_covered) == list(result.scope.not_covered)
    assert result.to_dict()["write_receipt"] == "issued"


def test_a_wrong_write_gets_a_refuted_receipt(tmp_path: Path) -> None:
    db = api_fx._store(tmp_path)
    pending = begin_write(
        api_fx._contract(db), api_fx.PARAMS, actor=api_fx.BOT, ledger=MemoryLedger()
    )
    api_fx._write(db, api_fx.SAVE.replace("'Ada'", "'Eda'"))
    result = verify_write(pending, write_receipt=True)
    assert result.write_receipt == "issued"
    v = _verify(Path(str(result.write_receipt_path)).read_bytes())
    assert v.payload.verdict == result.outcome.value != "CONFIRMED"


def test_a_receipt_that_cant_be_signed_never_changes_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENADAPT_SEAL_KEY", "file:" + str(tmp_path / "missing.pem"))
    db = api_fx._store(tmp_path)
    pending = begin_write(
        api_fx._contract(db), api_fx.PARAMS, actor=api_fx.BOT, ledger=MemoryLedger()
    )
    api_fx._write(db, api_fx.SAVE)
    result = verify_write(pending, write_receipt=True, seal=False)
    assert result.verified
    assert result.write_receipt.startswith("not issued: key_unavailable")
    assert result.write_receipt_path is None


def test_shadow_mode_signs_each_verdict_and_its_recheck(tmp_path: Path) -> None:
    db = shadow_fx._db(tmp_path)
    app = shadow_fx.App(db)
    sh = shadow_fx._shadow(tmp_path, app, db, receipts=True)
    sh.routes[0].contract.config.frame.database.recheck_after_s = 1
    assert shadow_fx._post(sh.url, shadow_fx.BODY)[0] == 201
    sh.flush()
    (ep,) = sh.episodes
    first = _verify(Path(ep.receipts[0]).read_bytes()).payload
    assert first.verdict == "CONFIRMED" and first.provisional
    assert first.source == "shadow.sql" and "frame.database" in first.scope.stores
    con = sqlite3.connect(db)  # a background job, after the check
    con.execute("UPDATE patient SET phone='9' WHERE id=1")
    con.commit()
    con.close()
    sh.run_rechecks(now=True)
    sh.stop()
    assert ep.late_verdict == "REFUTED" and len(ep.receipts) == 2
    late = _verify(Path(ep.receipts[1]).read_bytes())
    assert late.payload.verdict == "REFUTED" and not late.payload.provisional
    first_sha = receipt_id(Path(ep.receipts[0]).read_bytes())
    assert late.payload.supersedes == first_sha
    # The private evidence file holds the values; the receipt doesn't.
    assert b"Quill" not in Path(ep.receipts[0]).read_bytes()
    assert b"Quill" in Path(ep.receipts[0] + ".evidence.json").read_bytes()


def test_a_shadow_write_nothing_checked_says_so(tmp_path: Path) -> None:
    db = shadow_fx._db(tmp_path)
    sh = shadow_fx._shadow(tmp_path, shadow_fx.App(db), db, receipts=True)
    assert shadow_fx._post(sh.url, shadow_fx.BODY, path="/api/other")[0] == 201
    sh.stop()
    (ep,) = sh.episodes
    p = _verify(Path(ep.receipts[0]).read_bytes()).payload
    assert p.verdict == "UNCERTAIN" and p.scope.stores == []
    assert any("nothing about it was checked" in line for line in p.not_checked)


def test_fhir_scope_never_confirms_an_unreadable_read() -> None:
    scope = wr.fhir_scope(
        unreadable=True, started="2026-11-02T10:00:00Z", read_at=None, audit=True
    )
    with pytest.raises(wr.ReceiptError, match="overclaim"):
        wr.build_payload(
            source="shadow.fhir",
            verdict="CONFIRMED",
            scope=scope,
            checked={},
            write_id="shadow-x-1",
            evidence_commitment="0" * 64,
            issuer="seal-ed25519-0000000000000000",
        )


def _cli(*args: str, environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "receipt", *args],
        capture_output=True,
        text=True,
        env=environ,
        timeout=120,
    )


def test_the_cli_verifies_offline_and_prints_what_was_not_checked(
    tmp_path: Path,
) -> None:
    import os

    one = _issue(tmp_path)
    environ = dict(os.environ)
    keys = tmp_path / "keys"
    out = _cli("keys", "export", str(keys), environ=environ)
    assert out.returncode == 0, out.stderr
    assert "PRIVATE" not in out.stdout + (keys / "issuer.pub.pem").read_text()
    ok = _cli(
        "verify",
        str(one.path),
        "--issuer-key",
        str(keys / "issuer.pub.pem"),
        "--log-key",
        str(keys / "log.pub.pem"),
        "--evidence",
        str(one.evidence_path),
        environ=environ,
    )
    assert ok.returncode == 0, ok.stderr
    assert "VALID" in ok.stdout and "NOT checked:" in ok.stdout
    for line in wr.ALWAYS_NOT_CHECKED:
        assert line in ok.stdout
    one.path.write_bytes(one.path.read_bytes().replace(b"CONFIRMED", b"CONFIRMEE"))
    bad = _cli(
        "verify",
        str(one.path),
        "--issuer-key",
        str(keys / "issuer.pub.pem"),
        "--log-key",
        str(keys / "log.pub.pem"),
        environ=environ,
    )
    assert bad.returncode == 1 and "INVALID" in bad.stderr
    no_log = _cli(
        "verify",
        str(one.path),
        "--issuer-key",
        str(keys / "issuer.pub.pem"),
        environ=environ,
    )
    assert no_log.returncode == 2


def test_anchoring_is_refused_in_no_outbound_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.seal_anchor import AnchorError

    one = _issue(tmp_path)
    monkeypatch.setenv("OPENADAPT_NO_OUTBOUND", "strict")
    calls: list[Any] = []
    with pytest.raises(AnchorError, match="held_no_outbound"):
        wr.anchor(one.path, submit=lambda *a: calls.append(a))
    assert calls == []


# FHIR shadow episodes, with test_shadow_fhir.py's stubbed ensure_record.


def test_a_fhir_shadow_confirmation_is_provisional_until_its_recheck(
    env: dict[str, Any],  # noqa: F811 - the imported fixture
) -> None:
    from datetime import datetime, timezone

    sh = env["sh"]
    sh.receipts = True
    env["calls"]["ev"] = {"window_until": datetime.now(timezone.utc).isoformat()}
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    (ep,) = sh.episodes
    assert ep.verdict == "CONFIRMED" and ep.recheck_at, ep.receipt_note
    p = _verify(Path(ep.receipts[0]).read_bytes()).payload
    assert p.source == "shadow.fhir" and p.provisional and p.recheck_due_at
    assert p.scope.stores[0] == "fhir" and p.scope.window.read_at
    for line in wr.FHIR_NOT_COVERED:
        assert line in p.not_checked


def test_a_fhir_confirmation_with_no_read_window_gets_no_receipt(
    env: dict[str, Any],  # noqa: F811 - the imported fixture
) -> None:
    sh = env["sh"]
    sh.receipts = True
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    (ep,) = sh.episodes
    # The verdict stands; the receipt that couldn't state a window isn't made.
    assert ep.verdict == "CONFIRMED" and ep.receipts == []
    assert ep.receipt_note.startswith("not issued: overclaim")


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: d + b"\x00",
        lambda d: cbor2.dumps(cbor2.CBORTag(55799, cbor2.loads(d))),
        lambda d: (
            d[:1]
            + b"\x84\x5b"
            + len(cbor2.loads(d).value[0]).to_bytes(8, "big")
            + d[3:]
        ),
    ],
)
def test_a_re_encoded_receipt_is_rejected(tmp_path: Path, mangle: Any) -> None:
    data = _issue(tmp_path).path.read_bytes()
    with pytest.raises(wr.ReceiptError, match="malformed"):
        _verify(mangle(data))


@pytest.mark.parametrize(
    "edit",
    [
        {"scope": {**SCOPE, "window": {**SCOPE["window"], "baseline_read_at": None}}},
        {
            "scope": {
                **SCOPE,
                "window": {**SCOPE["window"], "read_at": "2026-11-02T09:00:00Z"},
            }
        },
        {"checked": {}},
        {"recheck_due_at": "2026-11-02T10:00:01Z"},
        {"source": "shadow.fhir", "checked": {}},
        {"issued_at": "2026-11-01T00:00:00Z"},
    ],
)
def test_more_overclaims_are_refused(tmp_path: Path, edit: dict[str, Any]) -> None:
    with pytest.raises(wr.ReceiptError, match="overclaim"):
        _issue(tmp_path, **edit)


def test_a_receipt_is_never_written_over(tmp_path: Path) -> None:
    first = _issue(tmp_path)
    before = first.path.read_bytes(), first.evidence_path.read_bytes()
    with pytest.raises(wr.ReceiptError, match="exists"):
        _issue(tmp_path)
    assert (first.path.read_bytes(), first.evidence_path.read_bytes()) == before


def test_a_past_due_provisional_receipt_is_flagged(tmp_path: Path) -> None:
    one = _issue(tmp_path, recheck_due_at="2026-11-02T10:05:05Z")
    v = _verify(one.path.read_bytes())
    assert v.payload.provisional and "supersedes this one" in v.notes[0]


def test_a_verifier_recheck_issues_a_superseding_receipt(tmp_path: Path) -> None:
    from openadapt_flow.verifier import issue_recheck_receipt, recheck

    db = shadow_fx._db(tmp_path)
    contract = shadow_fx._contract(db)
    contract.config.frame.database.recheck_after_s = 1
    params = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}
    pending = begin_write(contract, params, actor=api_fx.BOT, ledger=MemoryLedger())
    api_fx._write(db, api_fx.SAVE)
    result = verify_write(pending, write_receipt=True)
    assert result.provisional and result.write_receipt == "issued"
    api_fx._write(db, "UPDATE patient SET phone='9' WHERE id=1")
    late = recheck(contract, result, params=params, actor=api_fx.BOT, wait=True)
    state, path = issue_recheck_receipt(result, late)
    assert late.outcome == "REFUTED" and state == "issued", state
    p = _verify(Path(str(path)).read_bytes()).payload
    first = Path(str(result.write_receipt_path)).read_bytes()
    assert p.verdict == "REFUTED" and p.supersedes == receipt_id(first)
    # The window stays the check's; a line says what was read again.
    assert p.scope.window.read_at == result.scope.window.read_at
    assert any("only frame.database was read again" in x for x in p.not_checked)


def test_unsigned_bytes_cant_rename_a_receipt(tmp_path: Path) -> None:
    from openadapt_flow.receipt_log import RECEIPTS, VDP

    data = _issue(tmp_path).path.read_bytes()
    prot, _, unprot, body, sig = decode_sign1(data)
    log_msg = cbor2.loads(unprot[RECEIPTS][0])
    proof = log_msg.value[1][VDP][-1][0]
    size, index, path = cbor2.loads(proof)
    for bad in (proof + b"\x00\xff", b"\x83\x18" + bytes([size]) + proof[2:]):
        log_msg.value[1] = {VDP: {-1: [bad]}}
        inner = cbor2.dumps(log_msg, canonical=True)
        forged = cbor2.dumps(
            cbor2.CBORTag(18, [prot, {RECEIPTS: [inner]}, body, sig]), canonical=True
        )
        with pytest.raises(wr.ReceiptError, match="bad_inclusion"):
            _verify(forged)
    # Whatever the unsigned header holds, the id covers only signed bytes.
    bare = cbor2.dumps(cbor2.CBORTag(18, [prot, {}, body, sig]), canonical=True)
    assert receipt_id(bare) == receipt_id(data)


def test_a_recheck_of_another_write_gets_no_receipt(tmp_path: Path) -> None:
    from openadapt_flow.verifier import RecheckResult, issue_recheck_receipt

    db = api_fx._store(tmp_path)
    pending = begin_write(
        api_fx._contract(db), api_fx.PARAMS, actor=api_fx.BOT, ledger=MemoryLedger()
    )
    api_fx._write(db, api_fx.SAVE)
    result = verify_write(pending, write_receipt=True)
    other = RecheckResult(
        "CONFIRMED", "held", receipt_sha256="0" * 64, read_at="2099-01-01T00:00:00Z"
    )
    state, path = issue_recheck_receipt(result, other)
    assert state.startswith("not issued: recheck_mismatch") and path is None
