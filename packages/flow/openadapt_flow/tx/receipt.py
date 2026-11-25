"""Locally signed COSE receipts with a local Merkle inclusion log.

Local signatures bind this process's evidence. They do not authenticate the
remote server or prove that all server-side effects have been observed.

Public v3 statements bind task and entity labels, snapshots, intended changes
and the private wire ledger through domain-separated HMAC-SHA-256 commitments.
The per-receipt commitment key is encrypted in private ``salt.bin`` state.
Legacy v1/v2 receipts support a separate integrity-only audit; they do not pass
the current privacy schema or export their original payload through that audit.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.receipt_log import (
    ALG,
    CTY,
    EDDSA,
    KID,
    RECEIPTS,
    LocalLog,
    decode_sign1,
    encode_sign1,
    key_id,
    leaf_of,
    receipt_id,
    sig_structure,
    verify_inclusion,
)
from openadapt_flow.tx.types import (
    TX_RECEIPT_SCHEMA,
    OCCStatus,
    TxOutcome,
    WireSessionSummary,
    salted_sha256,
)

TX_RECEIPT_CONTENT_TYPE = "application/vnd.openadapt.tx-receipt+json"
_SIGNER_KEY_FILENAME = "tx-signer-ed25519.pem"
_LOG_KEY_FILENAME = "log-ed25519.pem"
_LOG_LEAVES_FILENAME = "leaves"
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_RESERVED_DIR_NAMES = frozenset({"log", "ledger", ".", ".."})
_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})
_COMMITMENT_PROFILE = "openadapt.tx-public-commitments/v1"
_LEGACY_SCHEMAS = frozenset({"openadapt.tx-receipt/v1", "openadapt.tx-receipt/v2"})
_HMACDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
_CommitmentDomain = Literal[
    "task-id",
    "entity-name",
    "intended-delta",
    "record-before",
    "record-after",
    "wire-ledger",
]


def _public_commitment(key: bytes, domain: _CommitmentDomain, value: Any) -> str:
    """Blind every sensitive public evidence value under one explicit profile."""
    if len(key) != 32:
        raise ValueError("public_commitment_key_must_be_256_bits")
    return salted_sha256(
        key, {"profile": _COMMITMENT_PROFILE, "domain": domain, "value": value}
    )


class TxReceiptPayload(BaseModel):
    """Strict v3 statement; old raw or differently computed commitments refuse."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["openadapt.tx-receipt/v3"]
    commitment_algorithm: Literal["HMAC-SHA-256"]
    commitment_profile: Literal["openadapt.tx-public-commitments/v1"]
    issued_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]
    task_id_hmac_sha256: _HMACDigest
    entity_name_hmac_sha256: _HMACDigest
    operation: Literal["create", "update"]
    outcome: TxOutcome
    occ_status: OCCStatus
    r0_hmac_sha256: Optional[_HMACDigest] = None
    r1_hmac_sha256: Optional[_HMACDigest] = None
    intended_delta_hmac_sha256: _HMACDigest
    wire_ledger_hmac_sha256: Optional[_HMACDigest] = None
    wire_mutations_allowed: Optional[int] = Field(default=None, ge=0)
    wire_collateral_mutations: Optional[int] = Field(default=None, ge=0)
    rows_read: int = Field(ge=0)
    verification_scope: Literal["target_record"] = "target_record"
    collateral_effects_verified: Literal[False] = False
    qualification_state: Literal["unqualified", "atomic_row_enforced"] = "unqualified"
    #: Additive and optional: receipts issued before it existed still verify.
    evidence_scope: Optional[dict[str, Any]] = None

    @field_validator("issued_at")
    @classmethod
    def _canonical_utc_timestamp(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value

    @field_validator("collateral_effects_verified", mode="before")
    @classmethod
    def _exact_unverified_collateral(cls, value: Any) -> bool:
        if value is not False:
            raise ValueError("collateral_effects_verified_must_be_literal_false")
        return False

    @model_validator(mode="after")
    def _coherent_wire_commitment(self) -> "TxReceiptPayload":
        fields = (
            self.wire_ledger_hmac_sha256,
            self.wire_mutations_allowed,
            self.wire_collateral_mutations,
        )
        if any(value is None for value in fields) and not all(
            value is None for value in fields
        ):
            raise ValueError("wire_commitment_and_counts_must_be_present_together")
        if self.evidence_scope is not None:
            from openadapt_types.check_level import CONFIRMED_WORD, VERIFIED_WORD
            from openadapt_types.evidence_scope import evidence_scope_problem

            if evidence_scope_problem(self.evidence_scope) is not None:
                raise ValueError("evidence_scope_invalid")
            word = self.evidence_scope["word"]
            if (word == VERIFIED_WORD) is not (self.outcome == CONFIRMED_WORD):
                # A CONFIRMED commit without a basis is UNCERTAIN, never here.
                raise ValueError("evidence_scope_word_disagrees_with_outcome")
        return self


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def _loads_strict_json(raw: bytes) -> dict[str, Any]:
    def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out = dict(pairs)
        if len(out) != len(pairs):
            raise ValueError("duplicate JSON keys in receipt payload")
        return out

    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(parsed, dict):
        raise ValueError("receipt payload must be a JSON object")
    return parsed


def task_key_for(task_id: str, *, workdir: Path | str) -> str:
    """Opaque request identity bound to durable state, independent of rotating keys."""
    from openadapt_flow.tx.request_index import RequestIndex

    return RequestIndex(workdir).task_key(task_id)


def write_private_bytes(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    """Atomically write ``data`` to ``path`` with strict ``0o600`` permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not overwrite and path.exists():
        return
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
    finally:
        tmp.unlink(missing_ok=True)


def write_private_record(
    path: Path, plaintext: bytes, *, overwrite: bool = True
) -> None:
    """Seal sensitive record/trace state under Flow's existing local at-rest key."""
    from openadapt_flow import at_rest, crypto

    key = crypto.resolve_key(None) or at_rest.local_key()
    sealed = crypto.encrypt_bytes(
        plaintext, key, aad=b"openadapt-flow/tx-private-state"
    )
    write_private_bytes(path, sealed, overwrite=overwrite)


def read_private_record(path: Path) -> bytes:
    """Load private state only after authenticated decryption."""
    from openadapt_flow import crypto

    raw = path.read_bytes()
    if not crypto.is_encrypted(raw):
        raise ValueError("transaction private state must be encrypted")
    return crypto.decrypt_bytes(
        raw, crypto.resolve_read_key(None), aad=b"openadapt-flow/tx-private-state"
    )


def load_or_create_signing_key(workdir: Path | str) -> Ed25519PrivateKey:
    """Load or generate a ``0o600`` Ed25519 receipt signing key in ``workdir``."""
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = root / _SIGNER_KEY_FILENAME
    if key_path.is_file():
        if sys.platform != "win32" and (key_path.stat().st_mode & 0o077):
            raise ValueError(f"{key_path} must have 0o600 permissions")
        loaded = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError(f"{key_path} is not an Ed25519 private key")
        return loaded

    generated = Ed25519PrivateKey.generate()
    pem = generated.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    write_private_bytes(key_path, pem, overwrite=False)
    loaded_final = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )
    if not isinstance(loaded_final, Ed25519PrivateKey):
        raise ValueError(f"{key_path} is not an Ed25519 private key")
    return loaded_final


def _coerce_public_key(
    public_key: Ed25519PublicKey | bytes,
) -> tuple[Ed25519PublicKey, bytes]:
    if isinstance(public_key, Ed25519PublicKey):
        raw = public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return public_key, raw
    if isinstance(public_key, (bytes, bytearray)):
        raw_bytes = bytes(public_key)
        if len(raw_bytes) == 32:
            try:
                return Ed25519PublicKey.from_public_bytes(raw_bytes), raw_bytes
            except Exception as exc:  # noqa: BLE001
                raise ValueError("invalid raw Ed25519 public key") from exc
        try:
            loaded = serialization.load_pem_public_key(raw_bytes)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid PEM Ed25519 public key") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("public key is not Ed25519")
        return (
            loaded,
            loaded.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            ),
        )
    raise ValueError("public_key must be Ed25519PublicKey or bytes")


def _resolve_log_dir(receipt_file: Path, log_dir: Optional[Path | str]) -> Path:
    if log_dir is None:
        return receipt_file.resolve().parent.parent / "log"
    candidate = Path(log_dir).resolve()
    if (candidate / _LOG_KEY_FILENAME).is_file():
        return candidate
    nested = candidate / "log"
    if (nested / _LOG_KEY_FILENAME).is_file():
        return nested
    return candidate


def _read_existing_log_public_raw(resolved_log_dir: Path) -> bytes:
    key_path = resolved_log_dir / _LOG_KEY_FILENAME
    if not key_path.is_file():
        raise ValueError(f"transparency log key not found at {key_path}")
    if sys.platform != "win32" and (key_path.stat().st_mode & 0o077):
        raise ValueError(f"transparency log key {key_path} has unsafe permissions")
    try:
        priv = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("corrupt transparency log key") from exc
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("transparency log key is not Ed25519")
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _allocate_run_dir(root: Path, base_key: str) -> Path:
    candidate = root / base_key
    if not (candidate / "tx-receipt.cose").exists():
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        return candidate
    idx = 2
    while True:
        retry_dir = root / f"{base_key}-{idx}"
        if not (retry_dir / "tx-receipt.cose").exists():
            retry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            return retry_dir
        idx += 1


def issue_tx_receipt(
    workdir: Path | str,
    *,
    task_id: str,
    entity_name: str,
    operation: Literal["create", "update"],
    outcome: TxOutcome,
    occ_status: OCCStatus = "NOT_APPLICABLE",
    intended_delta: Mapping[str, Any] = _EMPTY_MAP,
    r0: Optional[Mapping[str, Any]] = None,
    r1: Optional[Mapping[str, Any]] = None,
    wire_summary: Optional[WireSessionSummary] = None,
    rows_read: int = 0,
    private_key: Optional[Ed25519PrivateKey] = None,
    task_key: Optional[str] = None,
    issued_at: Optional[str] = None,
    qualification_state: Literal["unqualified", "atomic_row_enforced"] = "unqualified",
    evidence_scope: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Path]:
    """Issue a signed COSE_Sign1 + SCITT Merkle-logged ``tx-receipt.cose``.

    Saves the encrypted per-receipt commitment key in ``salt.bin`` and, when
    ``r0`` is present, encrypted ``undo_r0.json`` under the private task directory.
    Both files have ``0o600`` permissions. No key is embedded in public artifacts.
    Returns ``(receipt_id_hex, receipt_path)``.
    """
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)

    resolved_key = (
        task_key if task_key is not None else task_key_for(task_id, workdir=root)
    )
    if not _SAFE_KEY_RE.fullmatch(resolved_key) or resolved_key in _RESERVED_DIR_NAMES:
        raise ValueError(f"unsafe task_key: {resolved_key!r}")

    task_dir = _allocate_run_dir(root, resolved_key)
    salt = secrets.token_bytes(32)
    write_private_record(task_dir / "salt.bin", salt, overwrite=True)

    if r0 is not None:
        undo_bytes = _canonical_json_bytes(dict(r0)) + b"\n"
        write_private_record(task_dir / "undo_r0.json", undo_bytes, overwrite=False)
        primary_dir = root / resolved_key
        if primary_dir != task_dir:
            write_private_record(
                primary_dir / "undo_r0.json", undo_bytes, overwrite=False
            )

    signer = (
        private_key if private_key is not None else load_or_create_signing_key(root)
    )
    public_raw = signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    statement_dict: dict[str, Any] = {
        "schema_version": TX_RECEIPT_SCHEMA,
        "commitment_algorithm": "HMAC-SHA-256",
        "commitment_profile": _COMMITMENT_PROFILE,
        "issued_at": issued_at or _utc_now_iso(),
        "task_id_hmac_sha256": _public_commitment(salt, "task-id", task_id),
        "entity_name_hmac_sha256": _public_commitment(salt, "entity-name", entity_name),
        "operation": operation,
        "outcome": outcome,
        "occ_status": occ_status,
        "intended_delta_hmac_sha256": _public_commitment(
            salt, "intended-delta", dict(intended_delta)
        ),
        "rows_read": rows_read,
        "qualification_state": qualification_state,
    }
    if r0 is not None:
        statement_dict["r0_hmac_sha256"] = _public_commitment(
            salt, "record-before", dict(r0)
        )
    if r1 is not None:
        statement_dict["r1_hmac_sha256"] = _public_commitment(
            salt, "record-after", dict(r1)
        )
    if wire_summary is not None:
        statement_dict["wire_ledger_hmac_sha256"] = _public_commitment(
            salt, "wire-ledger", wire_summary.ledger_sha256
        )
        statement_dict["wire_mutations_allowed"] = wire_summary.mutations_allowed
        statement_dict["wire_collateral_mutations"] = wire_summary.collateral_mutations
    if evidence_scope is not None:
        statement_dict["evidence_scope"] = dict(evidence_scope)

    validated = TxReceiptPayload.model_validate(statement_dict)
    payload_bytes = _canonical_json_bytes(validated.model_dump(exclude_none=True))

    protected = {
        ALG: EDDSA,
        CTY: TX_RECEIPT_CONTENT_TYPE,
        KID: key_id(public_raw),
    }
    bare_statement = encode_sign1(protected, {}, payload_bytes, signer.sign)

    log = LocalLog(home=root)
    inclusion_proof = log.append(bare_statement)

    # Ed25519 (RFC 8032) is deterministic over sig_structure(prot, payload),
    # so attaching the SCITT receipt in the unprotected header preserves the
    # exact signature and RFC 9162 leaf hash logged in LocalLog.
    witnessed_cose = encode_sign1(
        protected,
        {RECEIPTS: [inclusion_proof]},
        payload_bytes,
        signer.sign,
    )

    receipt_path = task_dir / "tx-receipt.cose"
    write_private_bytes(receipt_path, witnessed_cose, overwrite=True)
    return receipt_id(witnessed_cose), receipt_path


def _verify_receipt_envelope(
    receipt_path: Path | str,
    public_key: Ed25519PublicKey | bytes,
    log_dir: Optional[Path | str],
) -> tuple[dict[str, Any], bytes]:
    """Verify canonical signed bytes and inclusion without trusting payload claims."""
    path = Path(receipt_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read receipt at {path}: {exc}") from exc
    if not data:
        raise ValueError("empty receipt file")

    pub_key, pub_raw = _coerce_public_key(public_key)

    try:
        prot, headers, unprot, payload_bytes, signature = decode_sign1(data)
    except Exception as exc:  # noqa: BLE001 - cbor2 may raise CBORDecodeError
        raise ValueError(f"malformed COSE_Sign1 receipt: {exc}") from exc

    expected_headers = {
        ALG: EDDSA,
        CTY: TX_RECEIPT_CONTENT_TYPE,
        KID: key_id(pub_raw),
    }
    if headers != expected_headers:
        raise ValueError("unexpected or mismatched COSE protected headers")
    if payload_bytes is None:
        raise ValueError("detached payload is not valid for tx-receipt")

    try:
        pub_key.verify(signature, sig_structure(prot, payload_bytes))
    except InvalidSignature as exc:
        raise ValueError("Ed25519 signature verification failed") from exc

    try:
        raw_obj = _loads_strict_json(payload_bytes)
        if _canonical_json_bytes(raw_obj) != payload_bytes:
            raise ValueError("receipt payload is not canonical JSON")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid tx-receipt payload: {exc}") from exc

    if set(unprot) != {RECEIPTS}:
        raise ValueError("unexpected COSE unprotected header keys")
    receipts = unprot.get(RECEIPTS)
    if (
        not isinstance(receipts, list)
        or len(receipts) != 1
        or not isinstance(receipts[0], bytes)
    ):
        raise ValueError("missing single SCITT transparency receipt")

    resolved_log_dir = _resolve_log_dir(path, log_dir)
    log_pub_raw = _read_existing_log_public_raw(resolved_log_dir)

    try:
        leaf_idx = verify_inclusion(receipts[0], data, log_pub_raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"SCITT Merkle inclusion verification failed: {exc}") from exc

    leaves_file = resolved_log_dir / _LOG_LEAVES_FILENAME
    if leaves_file.is_file():
        lines = leaves_file.read_text(encoding="ascii").splitlines()
        if leaf_idx < 0 or leaf_idx >= len(lines):
            raise ValueError("inclusion index out of bounds for local transparency log")
        if lines[leaf_idx] != leaf_of(data).hex():
            raise ValueError("receipt leaf hash does not match local transparency log")

    return raw_obj, data


def verify_tx_receipt(
    receipt_path: Path | str,
    public_key: Ed25519PublicKey | bytes,
    log_dir: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Verify the strict current public schema, signature and inclusion proof.

    Legacy v1/v2 statements refuse current verification. Their original files
    remain intact and can be checked with ``audit_legacy_tx_receipt`` instead.
    No private commitment key is needed to verify public artifact integrity.
    """
    raw_obj, _ = _verify_receipt_envelope(receipt_path, public_key, log_dir)
    source_schema = raw_obj.get("schema_version")
    if isinstance(source_schema, str) and source_schema in _LEGACY_SCHEMAS:
        raise ValueError("legacy_tx_receipt_is_integrity_audit_only")
    try:
        validated = TxReceiptPayload.model_validate(raw_obj)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid current tx-receipt payload: {exc}") from exc
    return validated.model_dump(exclude_none=True)


def audit_legacy_tx_receipt(
    receipt_path: Path | str,
    public_key: Ed25519PublicKey | bytes,
    log_dir: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Audit legacy integrity without exporting old payload or current trust labels.

    This checks only original Ed25519 bytes and their local inclusion proof.
    It neither converts an old receipt to v3 nor marks its commitments private.
    The original signed artifact and any old private files are left unchanged.
    """
    raw_obj, data = _verify_receipt_envelope(receipt_path, public_key, log_dir)
    source_schema = raw_obj.get("schema_version")
    if not isinstance(source_schema, str) or source_schema not in _LEGACY_SCHEMAS:
        raise ValueError("legacy_tx_receipt_audit_requires_known_legacy_schema")
    return {
        "schema_version": "openadapt.legacy-tx-receipt-audit/v1",
        "source_schema": source_schema,
        "receipt_id": receipt_id(data),
        "signature_valid": True,
        "inclusion_verified": True,
        "privacy_safe": False,
        "current_schema_accepted": False,
    }
