"""Signed, portable write receipts. See ``docs/WRITE_RECEIPTS.md``.

A write receipt signs one verifier verdict with its scope: what was read,
when, how strongly, and what wasn't checked. It is a COSE_Sign1 (RFC 9052)
shaped as a SCITT Signed Statement, with the local log's COSE Receipt in its
unprotected header.

Fail closed: :func:`check_claims` refuses a receipt that claims more than
its scope, both when issuing and when verifying. Issuing never changes a
verdict; the caller reports why a receipt wasn't issued.

The payload holds closed values, timestamps and digests only. Record values,
reasons, URLs and parameters stay out; the evidence is bound by a salted
commitment whose salt stays in a private file beside the receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from openadapt_types.seal import canonical_json
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.receipt_log import (
    ALG,
    CTY,
    CWT_CLAIMS,
    EDDSA,
    KID,
    RECEIPTS,
    LocalLog,
    decode_sign1,
    encode_sign1,
    receipt_id,
    verify_inclusion,
)

SCHEMA = "openadapt.write-receipt/v1"
CONTENT_TYPE = "application/vnd.openadapt.write-receipt+json"
EVIDENCE_SCHEMA = "openadapt.write-receipt-evidence/v1"
_COMMIT_DOMAIN = b"openadapt.write-receipt-evidence/v1\0"

#: Lines every receipt's ``not_checked`` carries, whatever the verifier said.
ALWAYS_NOT_CHECKED = (
    "writes that land after the read-back (or after the recheck, when one ran)",
    "stores and systems not listed in scope.stores",
    "whether the write was authorized or clinically correct",
    "anything the agent did that didn't go through the checked write",
)

Verdict = Literal["CONFIRMED", "REFUTED", "UNCERTAIN"]
Strength = Literal[
    "independent_system_of_record",
    "independent_session",
    "persisted_state_reacquisition",
    "immediate_screen_confirmation",
    "unchecked",
]
Part = Literal["held", "refuted", "not_checked", "instruction"]
ReadPath = Literal["database", "api", "fhir", "audit_log", "screen", "file", "email"]

_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)
_TEXT = re.compile(r"^[\x20-\x7e]{1,400}$")


class ReceiptError(ValueError):
    """A receipt can't be issued, or doesn't verify. ``code`` is closed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _time(value: Optional[str]) -> Optional[str]:
    if value is not None and not _TIME.match(value):
        raise ValueError(f"not an ISO 8601 time: {value!r}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReceiptWindow(_Strict):
    baseline_read_at: Optional[str]
    read_at: Optional[str]
    max_execution_window_s: Optional[int] = Field(default=None, ge=0)

    _t = field_validator("baseline_read_at", "read_at")(_time)


class ReceiptScope(_Strict):
    read_paths: list[ReadPath]
    stores: list[str]
    unread: list[str]
    window: ReceiptWindow
    strength: Strength
    not_covered: list[str]

    @field_validator("stores", "unread")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        if any(not _NAME.match(s) for s in v) or len(set(v)) != len(v):
            raise ValueError("store names are short lower-case identifiers, each once")
        return v

    @field_validator("not_covered")
    @classmethod
    def _lines(cls, v: list[str]) -> list[str]:
        if any(not _TEXT.match(s) for s in v):
            raise ValueError("not_covered lines are printable ASCII, at most 400 chars")
        return v


class ReceiptSubject(_Strict):
    write_id: str
    contract_sha256: Optional[str]
    verify_run_receipt_sha256: Optional[str]

    @field_validator("write_id")
    @classmethod
    def _opaque(cls, v: str) -> str:
        if not _OPAQUE.match(v):
            raise ValueError("write_id is an opaque identifier")
        return v

    @field_validator("contract_sha256", "verify_run_receipt_sha256")
    @classmethod
    def _hex(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _HEX64.match(v):
            raise ValueError("not a SHA-256 hex digest")
        return v


class ReceiptPayload(_Strict):
    schema_: Literal["openadapt.write-receipt/v1"] = Field(alias="schema")
    source: Literal["verifier", "shadow.sql", "shadow.fhir"]
    verdict: Verdict
    provisional: bool
    recheck_due_at: Optional[str]
    checked: dict[Literal["delta", "frame", "source"], Part]
    scope: ReceiptScope
    not_checked: list[str]
    subject: ReceiptSubject
    evidence_commitment: str
    supersedes: Optional[str]
    issued_at: str
    issuer: str

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True
    )

    _t = field_validator("recheck_due_at", "issued_at")(_time)

    @field_validator("evidence_commitment", "supersedes")
    @classmethod
    def _hex(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _HEX64.match(v):
            raise ValueError("not a SHA-256 hex digest")
        return v

    @field_validator("not_checked")
    @classmethod
    def _lines(cls, v: list[str]) -> list[str]:
        if any(not _TEXT.match(s) for s in v):
            raise ValueError("not_checked lines are printable ASCII, at most 400 chars")
        return v

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def check_claims(p: ReceiptPayload) -> None:
    """Refuse a receipt that claims more than its scope. Raises ReceiptError."""

    s = p.scope
    if p.provisional != (p.recheck_due_at is not None):
        raise ReceiptError("overclaim", "provisional and recheck_due_at disagree")
    if p.provisional and p.verdict != "CONFIRMED":
        raise ReceiptError("overclaim", "only a CONFIRMED can be provisional")
    missing = [line for line in ALWAYS_NOT_CHECKED if line not in p.not_checked]
    if missing:
        raise ReceiptError("overclaim", "not_checked lacks: " + "; ".join(missing))
    if any(line not in p.not_checked for line in s.not_covered):
        raise ReceiptError("overclaim", "not_checked drops a line of scope.not_covered")
    if any(f"store not read: {name}" not in p.not_checked for name in s.unread):
        raise ReceiptError("overclaim", "not_checked doesn't name every unread store")
    if p.source == "shadow.fhir" and any(
        line not in p.not_checked for line in FHIR_NOT_COVERED
    ):
        raise ReceiptError(
            "overclaim", "a FHIR receipt lacks the FHIR read path's gaps"
        )
    w = s.window
    issued = _parse(p.issued_at)
    for name, value in (
        ("baseline_read_at", w.baseline_read_at),
        ("read_at", w.read_at),
    ):
        if value is not None and _parse(value) > issued + _SKEW:
            raise ReceiptError("overclaim", f"{name} is after the receipt was issued")
    if (
        w.baseline_read_at
        and w.read_at
        and _parse(w.read_at) < _parse(w.baseline_read_at)
    ):
        raise ReceiptError("overclaim", "read_at is before baseline_read_at")
    if p.recheck_due_at and w.read_at and _parse(p.recheck_due_at) <= _parse(w.read_at):
        raise ReceiptError("overclaim", "the recheck is due before the read-back")
    if p.verdict != "CONFIRMED":
        return
    if s.strength == "unchecked":
        raise ReceiptError("overclaim", "CONFIRMED at strength unchecked")
    if not s.stores or not s.read_paths:
        raise ReceiptError("overclaim", "CONFIRMED with no store read")
    if s.unread:
        raise ReceiptError("overclaim", "CONFIRMED with a contract store unread")
    if s.window.read_at is None:
        raise ReceiptError("overclaim", "CONFIRMED with no read-back time")
    if s.window.baseline_read_at is None:
        raise ReceiptError("overclaim", "CONFIRMED with no baseline read")
    if "refuted" in p.checked.values():
        raise ReceiptError("overclaim", "CONFIRMED with a refuted part")
    if p.source != "shadow.fhir" and p.checked.get("delta") != "held":
        raise ReceiptError("overclaim", "CONFIRMED without the declared write held")


#: Clock skew allowed between the store's clock and the issuer's.
_SKEW = timedelta(seconds=300)


def _parse(value: str) -> datetime:
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# --- building a payload ---------------------------------------------------------


def iso(when: Optional[datetime]) -> Optional[str]:
    """A time as whole-second UTC, as receipts write it."""
    if when is None:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return str(iso(datetime.now(timezone.utc)))


def commit(evidence: Any, salt: bytes) -> str:
    return hashlib.sha256(
        _COMMIT_DOMAIN + salt + canonical_json(evidence).encode("utf-8")
    ).hexdigest()


def _not_checked(scope: Mapping[str, Any]) -> list[str]:
    lines = list(scope.get("not_covered") or [])
    lines += [f"store not read: {name}" for name in scope.get("unread") or []]
    for line in ALWAYS_NOT_CHECKED:
        if line not in lines:
            lines.append(line)
    return lines


def build_payload(
    *,
    source: str,
    verdict: str,
    scope: Mapping[str, Any],
    checked: Mapping[str, Any],
    write_id: str,
    evidence_commitment: str,
    issuer: str,
    recheck_due_at: Optional[str] = None,
    contract_sha256: Optional[str] = None,
    verify_run_receipt_sha256: Optional[str] = None,
    supersedes: Optional[str] = None,
    issued_at: Optional[str] = None,
) -> ReceiptPayload:
    """Validate and claim-check a payload. Raises ReceiptError."""

    raw = {
        "schema": SCHEMA,
        "source": source,
        "verdict": verdict,
        "provisional": verdict == "CONFIRMED" and recheck_due_at is not None,
        "recheck_due_at": recheck_due_at if verdict == "CONFIRMED" else None,
        # The parts verify-run reports; "coverage" and anything else stay out.
        "checked": {
            k: checked[k] for k in ("delta", "frame", "source") if k in checked
        },
        "scope": dict(scope),
        "not_checked": _not_checked(scope),
        "subject": {
            "write_id": write_id,
            "contract_sha256": contract_sha256,
            "verify_run_receipt_sha256": verify_run_receipt_sha256,
        },
        "evidence_commitment": evidence_commitment,
        "supersedes": supersedes,
        "issued_at": issued_at or _utc_now(),
        "issuer": issuer,
    }
    try:
        payload = ReceiptPayload.model_validate(json.loads(json.dumps(raw)))
    except Exception as exc:  # pydantic ValidationError, or a value json can't hold
        raise ReceiptError("payload_invalid", str(exc).splitlines()[0]) from exc
    check_claims(payload)
    return payload


def scope_of_result(result: Any) -> dict[str, Any]:
    return dict(result.scope.to_dict())


# --- issuing ----------------------------------------------------------------------


@dataclass
class Issued:
    """``sha256`` is the receipt's id (:func:`receipt_id`), not the file's."""

    path: Path
    evidence_path: Path
    sha256: str
    payload: ReceiptPayload


def _write_private(path: Path, data: bytes) -> None:
    """Write ``path`` (mode 0600) whole, and never over an existing file: a
    receipt already handed out must keep the evidence that opens it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def default_signer(env: Optional[Mapping[str, str]] = None) -> Any:
    """The offline Seal key (``OPENADAPT_SEAL_KEY``, else the generated one)."""
    from openadapt_flow import offline_seal

    return offline_seal.signer_for(env=env)


def issue(
    out: Path,
    *,
    evidence: Any,
    signer: Any = None,
    log: Optional[LocalLog] = None,
    **fields: Any,
) -> Issued:
    """Build, check, sign and log one receipt; write ``out`` and its private
    evidence file (``out`` + ``.evidence.json``, mode 0600). ``fields`` are
    :func:`build_payload`'s, without ``evidence_commitment`` and ``issuer``.
    Raises ReceiptError; writes nothing unless every step succeeds."""

    try:
        signer = signer if signer is not None else default_signer()
    except Exception as exc:  # noqa: BLE001 - LocalSealError, KeyUnavailable
        raise ReceiptError("key_unavailable", type(exc).__name__) from exc
    log = log if log is not None else LocalLog()
    out = Path(out)
    evidence_path = out.with_name(out.name + ".evidence.json")
    if out.exists() or evidence_path.exists():
        raise ReceiptError("exists", f"{out.name} is already issued")
    salt = secrets.token_bytes(32)
    try:
        evidence_json = json.loads(canonical_json(evidence))
    except (TypeError, ValueError) as exc:
        raise ReceiptError("evidence_unserializable", type(exc).__name__) from exc
    payload = build_payload(
        evidence_commitment=commit(evidence_json, salt),
        issuer=signer.key_id,
        **fields,
    )
    body = canonical_json(payload.wire()).encode("utf-8")
    issued = datetime.fromisoformat(payload.issued_at.replace("Z", "+00:00"))
    claims = {1: signer.key_id, 2: payload.subject.write_id, 6: int(issued.timestamp())}
    protected = {
        ALG: EDDSA,
        CTY: CONTENT_TYPE,
        KID: signer.key_id.encode("ascii"),
        CWT_CLAIMS: claims,
    }
    try:
        statement = encode_sign1(protected, {}, body, signer.sign)
        inclusion = log.append(statement)
    except Exception as exc:  # noqa: BLE001 - key, token or log unavailable
        raise ReceiptError("not_signed", type(exc).__name__) from exc
    import cbor2

    prot, _, _, _, signature = decode_sign1(statement)
    final = cbor2.dumps(
        cbor2.CBORTag(18, [prot, {RECEIPTS: [inclusion]}, body, signature]),
        canonical=True,
    )
    _write_private(
        evidence_path,
        (
            canonical_json(
                {
                    "schema": EVIDENCE_SCHEMA,
                    "salt": salt.hex(),
                    "evidence": evidence_json,
                }
            )
            + "\n"
        ).encode("utf-8"),
    )
    _write_private(out, final)
    return Issued(out, evidence_path, receipt_id(final), payload)


# --- verifying --------------------------------------------------------------------


@dataclass
class Verification:
    payload: ReceiptPayload
    sha256: str
    log_index: Optional[int] = None
    evidence_opened: Optional[bool] = None
    anchor: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def load_public_key(path: Path) -> bytes:
    """A raw Ed25519 public key from a PEM file or a key-registry JSON entry."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    data = Path(path).read_bytes()
    if data.lstrip().startswith(b"{"):
        raw = base64.b64decode(json.loads(data)["public_key"], validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
        return raw
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ReceiptError("key_invalid", f"{path} is not an Ed25519 public key")
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _loads_strict(body: bytes) -> Any:
    from openadapt_flow.offline_seal import loads_strict

    return loads_strict(body.decode("utf-8"))


def verify(
    data: bytes,
    *,
    issuer_public: bytes,
    log_public: Optional[bytes],
    evidence: Optional[bytes] = None,
) -> Verification:
    """Check a receipt offline against pinned keys. Raises ReceiptError.

    ``log_public`` None skips the log check and says so in ``notes``; the
    CLI only allows that with ``--no-log``."""

    from cwt import COSE, COSEKey
    from openadapt_types.seal import seal_key_id

    try:
        _, headers, unprot, body, _ = decode_sign1(data)
    except Exception as exc:  # noqa: BLE001 - cbor2 raises several types
        raise ReceiptError("malformed", str(exc)) from exc
    issuer_id = seal_key_id(issuer_public)
    if set(headers) != {ALG, CTY, KID, CWT_CLAIMS} or headers[ALG] != EDDSA:
        raise ReceiptError("malformed", "unexpected protected header")
    if headers[CTY] != CONTENT_TYPE:
        raise ReceiptError("malformed", "not a write receipt")
    if headers[KID] != issuer_id.encode("ascii"):
        raise ReceiptError("wrong_issuer", "signed by another key than the pinned one")
    if body is None:
        raise ReceiptError("malformed", "the payload is detached")
    key = COSEKey.new(
        {1: 1, 2: issuer_id.encode("ascii"), -1: 6, -2: issuer_public, 3: EDDSA}
    )
    try:
        COSE.new().decode(data, key)
    except Exception as exc:  # noqa: BLE001 - cwt's VerifyError / DecodeError
        raise ReceiptError("bad_signature", type(exc).__name__) from exc
    try:
        raw = _loads_strict(body)
        if canonical_json(raw).encode("utf-8") != body:
            raise ValueError("payload is not canonical JSON")
        payload = ReceiptPayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise ReceiptError("payload_invalid", str(exc).splitlines()[0]) from exc
    claims = headers[CWT_CLAIMS]
    if not (
        isinstance(claims, dict)
        and claims.get(1) == issuer_id
        and claims.get(2) == payload.subject.write_id
        and payload.issuer == issuer_id
        and set(claims) == {1, 2, 6}
        and claims[6] == int(_parse(payload.issued_at).timestamp())
    ):
        raise ReceiptError("malformed", "CWT claims don't match the payload")
    check_claims(payload)
    out = Verification(payload=payload, sha256=receipt_id(data))
    if payload.recheck_due_at and _parse(payload.recheck_due_at) < datetime.now(
        timezone.utc
    ):
        out.notes.append(
            f"provisional, and its recheck was due at {payload.recheck_due_at}: "
            "ask the issuer for the receipt that supersedes this one; on its own "
            "this one doesn't show the verdict held"
        )
    if set(unprot) - {RECEIPTS}:
        raise ReceiptError("malformed", "unexpected unprotected header")
    receipts = unprot.get(RECEIPTS) or []
    if log_public is None:
        out.notes.append("log inclusion not checked (--no-log)")
    else:
        if not (isinstance(receipts, list) and len(receipts) == 1):
            raise ReceiptError("not_logged", "no single log receipt")
        try:
            out.log_index = verify_inclusion(receipts[0], data, log_public)
        except Exception as exc:  # noqa: BLE001
            raise ReceiptError("bad_inclusion", str(exc)) from exc
    if evidence is not None:
        try:
            doc = _loads_strict(evidence)
            if (
                set(doc) != {"schema", "salt", "evidence"}
                or doc["schema"] != EVIDENCE_SCHEMA
            ):
                raise ValueError("not an evidence file")
            salt = bytes.fromhex(doc["salt"])
            if len(salt) != 32:
                raise ValueError("bad salt")
        except Exception as exc:  # noqa: BLE001
            raise ReceiptError("evidence_invalid", str(exc)) from exc
        out.evidence_opened = (
            commit(doc["evidence"], salt) == payload.evidence_commitment
        )
        if not out.evidence_opened:
            raise ReceiptError(
                "evidence_mismatch", "the evidence file isn't this receipt's"
            )
    return out


# --- Rekor anchoring (opt-in) ------------------------------------------------------


def anchor_path(receipt_path: Path) -> Path:
    return Path(receipt_path).with_name(Path(receipt_path).name + ".anchor.json")


def anchor(receipt_path: Path, signer: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Anchor the receipt's SHA-256 in Rekor with the issuer key, through the
    Seal anchoring code. Sends only a digest, the public key and a signature."""
    from openadapt_flow.seal_anchor import anchor_record

    data = Path(receipt_path).read_bytes()
    signer = signer if signer is not None else default_signer()
    _, headers, _, _, _ = decode_sign1(data)
    if headers.get(KID) != signer.key_id.encode("ascii"):
        raise ReceiptError("anchor_key_mismatch", "anchor with the key that issued it")
    return anchor_record(
        {"write_receipt_sha256": receipt_id(data)},
        anchor_path(receipt_path),
        signer,
        **kwargs,
    )


def verify_anchor(
    data: bytes, anchor_doc: Any, issuer_public: bytes, **kwargs: Any
) -> Any:
    from openadapt_flow.seal_anchor import verify_record_anchor

    return verify_record_anchor(
        {"write_receipt_sha256": receipt_id(data)},
        anchor_doc,
        issuer_public,
        **kwargs,
    )


# --- sources ------------------------------------------------------------------------


def issue_for_result(
    result: Any,
    out: Path,
    *,
    source: str = "verifier",
    supersedes: Optional[str] = None,
    **kwargs: Any,
) -> Issued:
    """A receipt for a verifier ``WriteResult``. Raises ReceiptError."""

    receipt = getattr(result, "receipt", None) or {}
    # verify-run names the contract's digest workflow_digest ("sha256:...").
    value = str(receipt.get("workflow_digest") or "").removeprefix("sha256:")
    contract_sha = value if _HEX64.match(value) else None
    return issue(
        out,
        evidence={"receipt": receipt, "evidence": result.evidence},
        source=source,
        verdict=result.outcome.value,
        scope=scope_of_result(result),
        checked=result.checked,
        write_id=result.write_id,
        recheck_due_at=result.recheck_due_at,
        contract_sha256=contract_sha,
        verify_run_receipt_sha256=result.receipt_sha256 or None,
        supersedes=supersedes,
        **kwargs,
    )


def recheck_scope(
    scope: Mapping[str, Any], reread: list[str], read_at: Optional[str]
) -> dict[str, Any]:
    """A recheck's scope: the first check's, window unchanged, plus a line
    naming what the recheck read again and until when. A recheck reads
    fewer stores than the check, so the window can't move forward for all."""

    line = (
        f"after read_at only {', '.join(reread) or 'nothing'} was read again, "
        f"by a recheck until {read_at}; other stores were not"
        if read_at
        else "after read_at nothing was read again"
    )
    return {**scope, "not_covered": [*scope.get("not_covered", []), line]}


def issue_for_recheck(
    result: Any, late: Any, out: Path, *, supersedes: Optional[str], **kwargs: Any
) -> Issued:
    """A receipt for a verifier ``RecheckResult`` that supersedes the earlier
    CONFIRMED's. Only the frame's database is read again."""

    outcome = getattr(result.outcome, "value", result.outcome)
    if outcome != "CONFIRMED":
        raise ReceiptError("recheck_mismatch", "only a CONFIRMED write is rechecked")
    if not late.receipt_sha256 or late.receipt_sha256 != result.receipt_sha256:
        raise ReceiptError("recheck_mismatch", "the recheck is of another write")
    verdict = str(late.outcome)
    read_at = str(late.read_at or "") or None
    if verdict == "CONFIRMED" and (not read_at or late.frame != "held"):
        verdict = "UNCERTAIN"
    when = iso(_parse(read_at)) if read_at else None
    return issue(
        out,
        evidence={
            "recheck": {
                "outcome": late.outcome,
                "frame": late.frame,
                "reasons": late.reasons,
                "read_at": late.read_at,
                "excused": late.excused,
                "evidence": late.evidence,
                "recheck_claim_sha256": getattr(late, "recheck_claim_sha256", "")
                or None,
                "excuse_receipt_sha256s": list(late.excuse_receipt_sha256s)
                if getattr(late, "excuse_receipt_sha256s", None) is not None
                else None,
            }
        },
        source="verifier",
        verdict=verdict,
        scope=recheck_scope(scope_of_result(result), ["frame.database"], when),
        checked={**result.checked, "frame": late.frame},
        write_id=result.write_id,
        verify_run_receipt_sha256=result.receipt_sha256 or None,
        supersedes=supersedes,
        **kwargs,
    )


#: What a FHIR shadow check can't see (``docs/ENSURE_RECORD.md``).
FHIR_NOT_COVERED = (
    "deletes the audit trail didn't log (a _lastUpdated sweep can't see a delete)",
    "resource types outside the contract's change feed",
    "writes by a credential the audit trail names as another actor, beyond the record checked",
)


def fhir_scope(
    *,
    unreadable: bool,
    started: Optional[str],
    read_at: Optional[str],
    audit: bool,
) -> dict[str, Any]:
    """The scope of an ``ensure_record`` check through a FHIR read path."""

    stores = [] if unreadable else ["fhir"] + (["audit"] if audit else [])
    return {
        "read_paths": [] if unreadable else ["fhir"] + (["audit_log"] if audit else []),
        "stores": stores,
        "unread": ["fhir"] if unreadable else [],
        "window": {
            "baseline_read_at": started,
            "read_at": None if unreadable else read_at,
            "max_execution_window_s": None,
        },
        # Searches, _history and AuditEvent read from the server itself, not
        # the agent's reply.
        "strength": "unchecked" if unreadable else "independent_system_of_record",
        "not_covered": list(FHIR_NOT_COVERED),
    }
