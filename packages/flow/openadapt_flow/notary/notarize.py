"""Notarized Seals: a timestamped, countersigned statement about a receipt.

The notary block is an optional extension that travels beside a receipt in
its wire document, under ``extensions["openadapt.notary/v1"]``. It never
edits the receipt or its evidence manifest, so the receipt's Seal signature
and ``evidence_digest`` still verify with the existing code.

The block holds:

* ``statement``: what is being notarized, by digest only. It names the
  receipt (``receipt_sha256``, ``execution_id``, ``nonce``), the evidence
  manifest (``evidence_digest``), the baseline that binds the read to one run
  (``baseline_sha256``, ``baseline_read_at``), and the field commitment block
  (``commitments_sha256``).
* ``receipt_timestamp``: an RFC 3161 token over SHA-256 of the statement.
  Because the statement names the receipt hash, the token proves the receipt
  existed no later than the TSA's ``genTime``, and so did the commitments.
* ``baseline_timestamp`` (optional): a token over SHA-256 of a baseline
  binding (``baseline_sha256``, ``baseline_read_at``, ``nonce``), taken when
  the baseline was read and before the executor ran.
* ``countersignature`` (optional): a Seal signature by a notary issuer over an
  attestation that names the statement and both tokens. It reuses
  ``openadapt.seal-signature/v1``, Cloud's key registry, and pinning
  unchanged; the attestation's own ``schema_version`` sits inside the signed
  hash, which keeps it apart from receipts signed by the same key.

Only digests leave the customer: the TSA sees one 32-byte hash, a notary
issuer sees the attestation (digests and an opaque execution id).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

from openadapt_flow.notary.commitments import (
    CommitmentError,
    commitments_sha256,
    verify_disclosure,
)
from openadapt_flow.notary.tsa import (
    TimestampAuthority,
    TimestampVerification,
    verify_timestamp_token,
)

__all__ = [
    "ATTESTATION_SCHEMA",
    "BASELINE_BINDING_SCHEMA",
    "CloudNotaryIssuer",
    "EXTENSION_KEY",
    "LocalNotaryIssuer",
    "NotaryError",
    "NotaryIssuer",
    "NotaryReport",
    "STATEMENT_SCHEMA",
    "baseline_binding",
    "build_statement",
    "locate",
    "notarize_document",
    "timestamp_baseline",
    "verify_notarized",
]

EXTENSION_KEY = "openadapt.notary/v1"
STATEMENT_SCHEMA = "openadapt.notary-statement/v1"
ATTESTATION_SCHEMA = "openadapt.notary-attestation/v1"
BASELINE_BINDING_SCHEMA = "openadapt.notary-baseline-binding/v1"
EFFECT_RECEIPT_SCHEMA = "openadapt.effect-receipt/v1"
EXECUTE_RECEIPT_SCHEMA = "openadapt.execute-evidence-receipt/v1"
_SEALABLE = {"verified", "rolled_back_verified"}


class NotaryError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Same hash the Seal signs: the receipt without ``seal_signature``."""
    from openadapt_types.seal import receipt_sha256 as _types_receipt_sha256

    return str(_types_receipt_sha256(receipt))


@dataclass(frozen=True)
class Located:
    receipt: Mapping[str, Any]
    evidence: Optional[Mapping[str, Any]]
    notary: Optional[Mapping[str, Any]]


def locate(document: Any) -> Located:
    """Find the receipt, evidence manifest, and notary block in a document.

    Accepts an effect-receipt wire document (``{"receipt", "evidence"}``), a
    bare receipt, a Flow ``report.json`` carrying ``execute_receipt``, or any
    of these with ``extensions``.
    """
    if not isinstance(document, Mapping):
        raise NotaryError("no_receipt")
    extensions = document.get("extensions")
    notary = extensions.get(EXTENSION_KEY) if isinstance(extensions, Mapping) else None
    receipt: Any = None
    if "receipt" not in document and (
        "seal_signature" in document
        or document.get("schema_version")
        in (EXECUTE_RECEIPT_SCHEMA, EFFECT_RECEIPT_SCHEMA)
    ):
        receipt = {k: v for k, v in document.items() if k != "extensions"}
    else:
        for key in ("receipt", "execute_receipt"):
            if isinstance(document.get(key), Mapping):
                receipt = document[key]
                break
    if not isinstance(receipt, Mapping) or not isinstance(
        receipt.get("schema_version"), str
    ):
        raise NotaryError("no_receipt")
    evidence = document.get("evidence")
    return Located(
        receipt=receipt,
        evidence=evidence if isinstance(evidence, Mapping) else None,
        notary=notary if isinstance(notary, Mapping) else None,
    )


def _baseline_fields(
    evidence: Optional[Mapping[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """``baseline_sha256`` and ``baseline_read_at`` from the evidence manifest.

    RFC-0002 is under revision; this reads the names the draft uses
    (``readback.baseline_sha256``) and the revised rule's ``baseline_read_at``
    under ``readback`` or ``baseline``.
    """
    if evidence is None:
        return None, None
    rb, bl = evidence.get("readback"), evidence.get("baseline")
    readback: Mapping[str, Any] = rb if isinstance(rb, Mapping) else {}
    baseline: Mapping[str, Any] = bl if isinstance(bl, Mapping) else {}
    sha = (
        readback.get("baseline_sha256")
        or baseline.get("sha256")
        or baseline.get("baseline_sha256")
    )
    read_at = (
        readback.get("baseline_read_at")
        or baseline.get("read_at")
        or baseline.get("baseline_read_at")
    )
    return (
        sha if isinstance(sha, str) else None,
        read_at if isinstance(read_at, str) else None,
    )


def baseline_binding(
    *, baseline_sha256: str, baseline_read_at: str, nonce: str
) -> dict[str, str]:
    """What a baseline timestamp covers: the snapshot, when it was read, which run."""
    return {
        "schema_version": BASELINE_BINDING_SCHEMA,
        "baseline_sha256": baseline_sha256,
        "baseline_read_at": baseline_read_at,
        "nonce": nonce,
    }


def build_statement(
    receipt: Mapping[str, Any],
    evidence: Optional[Mapping[str, Any]],
    commitments: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline_sha, baseline_read_at = _baseline_fields(evidence)
    return {
        "schema_version": STATEMENT_SCHEMA,
        "receipt_schema": receipt.get("schema_version"),
        "receipt_sha256": receipt_sha256(receipt),
        "execution_id": receipt.get("execution_id"),
        "nonce": receipt.get("nonce"),
        "evidence_digest": receipt.get("evidence_digest"),
        "baseline_sha256": baseline_sha,
        "baseline_read_at": baseline_read_at,
        "commitments_sha256": commitments_sha256(commitments) if commitments else None,
    }


def _token_entry(
    tsa: TimestampAuthority, digest: bytes, tsa_pem: Optional[bytes]
) -> dict[str, Any]:
    token = tsa.timestamp(digest)
    entry: dict[str, Any] = {
        "tsa": tsa.name,
        "imprint_sha256": digest.hex(),
        "token": base64.b64encode(token).decode("ascii"),
    }
    if tsa_pem:
        entry["tsa_certificates_pem"] = tsa_pem.decode("ascii")
    return entry


def timestamp_baseline(
    tsa: TimestampAuthority,
    *,
    baseline_sha256: str,
    baseline_read_at: str,
    nonce: str,
    tsa_pem: Optional[bytes] = None,
) -> dict[str, Any]:
    """Timestamp a baseline at read time, before the executor runs."""
    binding = baseline_binding(
        baseline_sha256=baseline_sha256, baseline_read_at=baseline_read_at, nonce=nonce
    )
    entry = _token_entry(tsa, hashlib.sha256(_canonical(binding)).digest(), tsa_pem)
    entry["binding"] = binding
    return entry


# -- notary issuer -----------------------------------------------------------


class NotaryIssuer(Protocol):
    """Countersigns an attestation with a Seal key. Sees digests only."""

    def countersign(self, attestation: Mapping[str, Any]) -> dict[str, Any]: ...


class LocalNotaryIssuer:
    """Countersigns with a local Ed25519 key using the Seal signature scheme.

    For tests and self-hosted trials. A holder trusts it only after pinning
    its key the same way as Cloud's (``registry_entry()`` gives the entry).
    """

    def __init__(self, private_key: Any, *, clock: Any = None) -> None:
        self._key = private_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _public_raw(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return bytes(
            self._key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def registry_entry(self) -> dict[str, str]:
        from openadapt_types.seal import seal_key_id

        raw = self._public_raw()
        return {
            "key_id": seal_key_id(raw),
            "public_key": base64.b64encode(raw).decode("ascii"),
            "status": "active",
        }

    def countersign(self, attestation: Mapping[str, Any]) -> dict[str, Any]:
        from openadapt_types.seal import (
            SEAL_ISSUER,
            SEAL_SIGNATURE_SCHEMA,
            seal_key_id,
            signing_payload,
        )
        from openadapt_types.seal import (
            receipt_sha256 as types_sha,
        )

        unsigned = {
            "schema": SEAL_SIGNATURE_SCHEMA,
            "issuer": SEAL_ISSUER,
            "algorithm": "ed25519",
            "key_id": seal_key_id(self._public_raw()),
            "receipt_sha256": types_sha(attestation),
            "run_id": str(attestation["execution_id"]),
            "issued_at": self._clock()
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        signature = base64.b64encode(self._key.sign(signing_payload(unsigned))).decode()
        return {**unsigned, "signature": signature}


class CloudNotaryIssuer:
    """Interface stub for a Cloud notary endpoint. No server exists yet.

    Proposed contract: ``POST /api/v1/notary/countersign`` with the
    attestation JSON (digests and an opaque execution id only). Cloud would
    recompute ``receipt_sha256`` of the attestation, optionally fetch its own
    RFC 3161 token over ``statement_sha256``, and return a
    ``openadapt.seal-signature/v1`` object. Calling this raises.
    """

    def __init__(self, base_url: str = "https://app.openadapt.ai") -> None:
        self.base_url = base_url

    def countersign(self, attestation: Mapping[str, Any]) -> dict[str, Any]:
        raise NotaryError(
            "issuer_unavailable", "Cloud has no notary endpoint yet; use a local issuer"
        )


def _attestation(
    statement: Mapping[str, Any],
    receipt_timestamp: Optional[Mapping[str, Any]],
    baseline_timestamp: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    def token_sha(entry: Optional[Mapping[str, Any]]) -> Optional[str]:
        if not entry:
            return None
        return hashlib.sha256(base64.b64decode(str(entry["token"]))).hexdigest()

    return {
        "schema_version": ATTESTATION_SCHEMA,
        "execution_id": statement["execution_id"],
        "statement_sha256": _sha256_hex(statement),
        "receipt_timestamp_token_sha256": token_sha(receipt_timestamp),
        "baseline_timestamp_token_sha256": token_sha(baseline_timestamp),
    }


def notarize_document(
    document: Mapping[str, Any],
    *,
    tsa: Optional[TimestampAuthority] = None,
    tsa_pem: Optional[bytes] = None,
    commitments: Optional[Mapping[str, Any]] = None,
    baseline_timestamp: Optional[Mapping[str, Any]] = None,
    issuer: Optional[NotaryIssuer] = None,
) -> dict[str, Any]:
    """Return a copy of ``document`` with a notary block. The receipt is untouched."""
    located = locate(document)
    if located.notary is not None:
        raise NotaryError("already_notarized")
    if not isinstance(located.receipt.get("execution_id"), str):
        raise NotaryError("malformed", "the receipt names no execution_id")
    statement = build_statement(located.receipt, located.evidence, commitments)
    block: dict[str, Any] = {"schema": EXTENSION_KEY, "statement": statement}
    if commitments:
        block["commitments"] = dict(commitments)
    receipt_ts = None
    if tsa is not None:
        receipt_ts = _token_entry(
            tsa, hashlib.sha256(_canonical(statement)).digest(), tsa_pem
        )
        block["receipt_timestamp"] = receipt_ts
    if baseline_timestamp is not None:
        block["baseline_timestamp"] = dict(baseline_timestamp)
    if issuer is not None:
        attestation = _attestation(statement, receipt_ts, baseline_timestamp)
        block["countersignature"] = {
            "attestation": attestation,
            "seal_signature": issuer.countersign(attestation),
        }
    if "receipt" not in document and "execute_receipt" not in document:
        # A bare receipt: wrap it, because a key added to the receipt itself
        # would change its hash and break the Seal for every verifier.
        out: dict[str, Any] = {"receipt": dict(located.receipt)}
    else:
        out = dict(document)
    extensions = dict(out.get("extensions") or {})
    extensions[EXTENSION_KEY] = block
    out["extensions"] = extensions
    return out


# -- verification ------------------------------------------------------------


@dataclass
class NotaryReport:
    """Everything ``openadapt-flow notary verify`` prints. ``exit_code`` decides."""

    receipt_schema: Optional[str]
    execution_id: Optional[str]
    receipt_sha256: Optional[str]
    executor_kind: Optional[str]
    executor_kind_source: str
    verification_tier: Optional[str]
    effect_verdict: Optional[str]
    outcome: Optional[str]
    oracle_tier: Optional[int]
    seal: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    statement: dict[str, Any] = field(default_factory=dict)
    receipt_timestamp: dict[str, Any] = field(default_factory=dict)
    baseline_timestamp: dict[str, Any] = field(default_factory=dict)
    countersignature: dict[str, Any] = field(default_factory=dict)
    commitments: dict[str, Any] = field(default_factory=dict)
    disclosures: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    accept_non_flow: bool = False

    @property
    def signature_genuine(self) -> bool:
        return self.seal.get("status") == "verified"

    @property
    def outcome_verified(self) -> bool:
        return self.outcome in _SEALABLE

    @property
    def non_flow(self) -> bool:
        return (
            self.executor_kind != "flow_replay" or self.executor_kind_source != "issuer"
        )

    @property
    def exit_code(self) -> int:
        """0 only for a genuine Seal on a verified outcome with no failed check.

        A Seal on a REFUTED or INDETERMINATE outcome is genuine but exits 1,
        as ``openadapt seal verify`` does. A notary part that is present but
        could not be checked (no pinned TSA certificate) also exits 1. An
        effect receipt from anything but an issuer-attested Flow replay exits 1
        unless the caller passed ``--accept-non-flow``.
        """
        if not self.signature_genuine or not self.outcome_verified or self.problems:
            return 1
        if (
            self.receipt_schema == EFFECT_RECEIPT_SCHEMA
            and self.non_flow
            and not self.accept_non_flow
        ):
            return 1
        return 0

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items()}
        out["exit_code"] = self.exit_code
        return out


def _check_token(
    entry: Any, expected: bytes, tsa_certs: Sequence[Any]
) -> dict[str, Any]:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("token"), str):
        return {"status": "malformed"}
    try:
        token = base64.b64decode(entry["token"], validate=True)
    except ValueError:
        return {"status": "malformed"}
    result: TimestampVerification = verify_timestamp_token(token, expected, tsa_certs)
    out = result.as_dict()
    out["status"] = (
        "verified"
        if result.ok
        else ("not_checked" if result.reason == "no_pinned_tsa" else result.reason)
    )
    out.pop("reason", None)
    out.pop("ok", None)
    out["tsa"] = entry.get("tsa")
    return out


def verify_notarized(
    document: Any,
    *,
    registry: Sequence[Mapping[str, Any]],
    tsa_certificates: Sequence[Any] = (),
    disclosures: Sequence[Mapping[str, Any]] = (),
    accept_non_flow: bool = False,
) -> NotaryReport:
    """Offline check of a (possibly notarized) Seal document. Pinned trust only.

    ``registry`` is the holder's pinned Seal key registry; ``tsa_certificates``
    are TSA certificates the holder pinned. Certificates carried inside the
    document are never trusted on their own.
    """
    from openadapt_types.seal import parse_key_registry, verify_seal_signature

    try:
        located = locate(document)
    except NotaryError:
        report = NotaryReport(None, None, None, None, "absent", None, None, None, None)
        report.seal = {"status": "no_receipt"}
        report.problems.append("no_receipt")
        return report
    receipt = located.receipt
    schema = receipt.get("schema_version")
    kind: Any
    if schema == EXECUTE_RECEIPT_SCHEMA:
        kind, source = "flow_replay", "issuer"
    elif schema == EFFECT_RECEIPT_SCHEMA:
        kind, source = receipt.get("executor_kind"), "declared"
    else:
        kind, source = None, "absent"
    rsha = receipt_sha256(receipt)
    report = NotaryReport(
        receipt_schema=str(schema),
        execution_id=receipt.get("execution_id"),
        receipt_sha256=rsha,
        executor_kind=kind if isinstance(kind, str) else None,
        executor_kind_source=source,
        verification_tier=receipt.get("verification_tier"),
        effect_verdict=receipt.get("effect_verdict"),
        outcome=receipt.get("outcome"),
        oracle_tier=receipt.get("oracle_tier"),
        accept_non_flow=accept_non_flow,
    )
    if schema not in (EXECUTE_RECEIPT_SCHEMA, EFFECT_RECEIPT_SCHEMA):
        report.problems.append("unknown_schema")
    if (
        schema == EFFECT_RECEIPT_SCHEMA
        and receipt.get("outcome") in _SEALABLE
        and (
            receipt.get("verification_tier") != "independent_readback"
            or receipt.get("effect_verdict") != "CONFIRMED"
        )
    ):
        report.problems.append("verified_without_independent_readback")
    # Seal signature: openadapt_types.seal, unchanged.
    try:
        keys = parse_key_registry(list(registry))
    except (ValueError, TypeError, KeyError):
        keys = []
    if not keys:
        report.seal = {"status": "no_pinned_keys"}
    else:
        seal = verify_seal_signature(receipt.get("seal_signature"), rsha, keys)
        signature = receipt.get("seal_signature") or {}
        if seal.ok and signature.get("run_id") != receipt.get("execution_id"):
            report.seal = {"status": "receipt_mismatch"}
        elif seal.ok:
            report.seal = {
                "status": "verified",
                "key_id": seal.key_id,
                "issued_at": signature.get("issued_at"),
            }
        else:
            report.seal = {"status": seal.reason}
    # Evidence manifest binding (RFC-0002 offline step 7).
    if located.evidence is None:
        report.evidence = {"status": "absent"}
    else:
        ev = located.evidence
        digest = "sha256:" + _sha256_hex(dict(ev))
        mismatch = digest != receipt.get("evidence_digest")
        if schema == EFFECT_RECEIPT_SCHEMA:
            mismatch = mismatch or (
                ev.get("effect_verdict") != receipt.get("effect_verdict")
                or (ev.get("executor") or {}).get("kind")
                != receipt.get("executor_kind")
                or (ev.get("readback") or {}).get("verification_tier")
                != receipt.get("verification_tier")
            )
        report.evidence = {
            "status": "evidence_mismatch" if mismatch else "bound",
            "digest": digest,
        }
        if mismatch:
            report.problems.append("evidence_mismatch")
    notary = located.notary
    if notary is None:
        for name in (
            "statement",
            "receipt_timestamp",
            "baseline_timestamp",
            "countersignature",
            "commitments",
        ):
            getattr(report, name)["status"] = "absent"
        if disclosures:
            report.problems.append("disclosure_without_commitments")
            report.disclosures = [{"status": "no_commitments"} for _ in disclosures]
        return report
    statement = notary.get("statement")
    commitments = notary.get("commitments")
    try:
        expected = build_statement(receipt, located.evidence, commitments)
    except CommitmentError:
        expected = None
    if expected is None or statement != expected:
        if (
            located.evidence is None
            and isinstance(statement, Mapping)
            and expected is not None
        ):
            # Without the manifest, the baseline fields cannot be recomputed;
            # accept the stated ones only if everything else matches.
            relaxed = {
                **expected,
                "baseline_sha256": statement.get("baseline_sha256"),
                "baseline_read_at": statement.get("baseline_read_at"),
            }
            ok = statement == relaxed
        else:
            ok = False
        report.statement = {"status": "matches_receipt" if ok else "statement_mismatch"}
        if not ok:
            report.problems.append("statement_mismatch")
    else:
        report.statement = {"status": "matches_receipt"}
    statement_ok = report.statement["status"] == "matches_receipt"
    statement_digest = (
        hashlib.sha256(_canonical(statement)).digest()
        if isinstance(statement, Mapping)
        else b""
    )
    # Receipt timestamp.
    rts = notary.get("receipt_timestamp")
    if rts is None:
        report.receipt_timestamp = {"status": "absent"}
    else:
        report.receipt_timestamp = _check_token(rts, statement_digest, tsa_certificates)
        if report.receipt_timestamp["status"] != "verified":
            report.problems.append(
                "receipt_timestamp_" + report.receipt_timestamp["status"]
            )
    # Baseline timestamp: must name this run's nonce and the stated baseline.
    bts = notary.get("baseline_timestamp")
    if bts is None:
        report.baseline_timestamp = {"status": "absent"}
    else:
        binding = bts.get("binding") if isinstance(bts, Mapping) else None
        if (
            not isinstance(binding, Mapping)
            or binding.get("schema_version") != BASELINE_BINDING_SCHEMA
            or binding.get("nonce") != receipt.get("nonce")
            or not isinstance(statement, Mapping)
            or binding.get("baseline_sha256") != statement.get("baseline_sha256")
            or binding.get("baseline_read_at") != statement.get("baseline_read_at")
        ):
            report.baseline_timestamp = {"status": "binding_mismatch"}
        else:
            report.baseline_timestamp = _check_token(
                bts,
                hashlib.sha256(_canonical(dict(binding))).digest(),
                tsa_certificates,
            )
            if (
                report.baseline_timestamp["status"] == "verified"
                and report.receipt_timestamp.get("status") == "verified"
                and report.baseline_timestamp["gen_time"]
                > report.receipt_timestamp["gen_time"]
            ):
                report.baseline_timestamp["status"] = "after_receipt"
        if report.baseline_timestamp["status"] != "verified":
            report.problems.append(
                "baseline_timestamp_" + report.baseline_timestamp["status"]
            )
    # Countersignature: Seal scheme over the attestation.
    cs = notary.get("countersignature")
    if cs is None:
        report.countersignature = {"status": "absent"}
    elif not keys:
        report.countersignature = {"status": "no_pinned_keys"}
        report.problems.append("countersignature_no_pinned_keys")
    else:
        attestation = cs.get("attestation") if isinstance(cs, Mapping) else None
        expected_att = (
            _attestation(statement, rts, bts)
            if isinstance(statement, Mapping)
            else None
        )
        if not isinstance(attestation, Mapping) or attestation != expected_att:
            report.countersignature = {"status": "attestation_mismatch"}
        else:
            from openadapt_types.seal import receipt_sha256 as types_sha

            sig = cs.get("seal_signature")
            result = verify_seal_signature(sig, types_sha(attestation), keys)
            if result.ok and (sig or {}).get("run_id") != attestation.get(
                "execution_id"
            ):
                report.countersignature = {"status": "receipt_mismatch"}
            elif result.ok:
                report.countersignature = {
                    "status": "verified",
                    "key_id": result.key_id,
                    "issued_at": (sig or {}).get("issued_at"),
                }
            else:
                report.countersignature = {"status": result.reason}
        if report.countersignature["status"] != "verified":
            report.problems.append(
                "countersignature_" + report.countersignature["status"]
            )
    # Commitments: bound only by a check that covers the statement.
    if commitments is None:
        report.commitments = {"status": "absent"}
    else:
        bound_by = []
        if statement_ok and report.receipt_timestamp.get("status") == "verified":
            bound_by.append("timestamp")
        if statement_ok and report.countersignature.get("status") == "verified":
            bound_by.append("countersignature")
        fields = commitments.get("fields") if isinstance(commitments, Mapping) else None
        report.commitments = {
            "status": "bound" if bound_by else "unbound",
            "bound_by": bound_by,
            "count": len(fields) if isinstance(fields, Mapping) else 0,
        }
    if disclosures and report.commitments.get("status") != "bound":
        # A value that matches an unbound commitment proves nothing: anyone
        # could have written that commitment after the fact.
        report.problems.append("commitments_unbound")
    for disclosure in disclosures:
        check = verify_disclosure(disclosure, commitments, receipt_sha256=rsha)
        entry = {
            "status": "matches" if check.ok else check.reason,
            "fields": [
                {"label": f.label, "name": f.name, "status": f.reason}
                for f in check.fields
            ],
        }
        report.disclosures.append(entry)
        if not check.ok:
            report.problems.append("disclosure_" + check.reason)
    return report
