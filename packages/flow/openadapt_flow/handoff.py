"""Agent-to-agent handoffs: a payload with the run's receipt attached.

One agent hands a task or a result to another. The receipt for the run that
produced it (``record/run/v1``, see :mod:`openadapt_types.receipt`) travels
with it. The receiving agent verifies the receipt against keys *it* pins and
gets a verdict that says exactly what the signature does and does not prove.

The same document works over any channel:

* **Inline**: one JSON document (``openadapt.handoff-document/v1``) holding the payload
  and the receipt. It is the body of an HTTP request or webhook, an MCP tool
  result, or an A2A artifact part.
* **Detached**: the payload stays a file (an email attachment, a shared
  folder), and ``<file>.handoff.json`` beside it holds the payload's SHA-256
  and the receipt.

What the verdict says, and why:

* A valid signature proves **who signed** the receipt (the role comes from
  the verifier's pinned registry, never from the document) and that the
  receipt is unchanged. It does not prove the receipt's claims are true.
* The sender binds the payload: :func:`bind_payload` adds it as the
  receipt's ``payload`` subject and signs again with the key that signed the
  run. A receiver holding the payload refuses a receipt that names other
  bytes (``payload_mismatch``) or none (``payload_not_bound``), so a receipt
  can't be stapled to another payload.
* A confirmation line counts as **independent of the sender** only when it
  was read from the other party's system (``read_method`` ``portal_read_back``
  or ``api``). Everything else is the sender's own word.

The handoff wrapper itself is not signed. The private openings file
(``receipt.private.json``) never travels; :func:`make_handoff` refuses it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA = "openadapt.handoff-document/v1"
VERDICT_SCHEMA = "openadapt.handoff-verdict/v1"
MEDIA_TYPE = "application/vnd.openadapt.handoff+json"
#: The draft extension URI (docs/HANDOFF_RECEIPTS.md). An identifier only.
EXTENSION_URI = "https://openadapt.ai/extensions/handoff-receipt/v1"
DETACHED_SUFFIX = ".handoff.json"
#: Largest inline payload, in bytes. A bigger one goes detached.
MAX_INLINE_BYTES = 1_048_576

WHAT_A_SIGNATURE_PROVES = (
    "A valid signature proves who signed this receipt and that it has not "
    "changed since. It does not prove that what the receipt says is true."
)


class HandoffError(ValueError):
    """A handoff that can't be built (never a verification result)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _loads_strict(text: str | bytes) -> Any:
    from openadapt_types.receipt import loads_strict

    return loads_strict(text)


def _check_receipt_document(receipt: Any) -> dict[str, Any]:
    """Only an envelope or a public card may travel; never private openings."""
    from openadapt_types import receipt as r

    if not isinstance(receipt, Mapping):
        raise HandoffError("the receipt is a JSON object")
    if receipt.get("schema") == r.OPENINGS_SCHEMA or "payload_sha256" in receipt:
        raise HandoffError(
            "that is receipt.private.json, which never leaves this computer; "
            "attach receipt.json or receipt.card.json"
        )
    if not (r.is_receipt_envelope(receipt) or r.is_receipt_card(receipt)):
        raise HandoffError("the receipt is a record/run/v1 envelope or card")
    return dict(receipt)


def payload_bytes(payload: Any) -> tuple[bytes, str]:
    """The exact bytes a payload's digest covers, and their media type.

    ``bytes`` are taken as they are. Any other value is JSON, digested in its
    canonical form (sorted keys, compact separators, UTF-8).
    """
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload), "application/octet-stream"
    return _canonical(payload), "application/json"


def make_handoff(
    payload: Any,
    receipt: Mapping[str, Any],
    *,
    name: Optional[str] = None,
    media_type: Optional[str] = None,
    detached: bool = False,
    signer: Optional[Any] = None,
) -> dict[str, Any]:
    """Attach ``receipt`` to ``payload`` as an ``openadapt.handoff-document/v1`` document.

    ``detached`` leaves the payload out and keeps only its digest, for a file
    sent beside the document. With ``signer`` (the key that signed the run's
    receipt), the receipt is re-signed with the payload as its ``payload``
    subject (:func:`bind_payload`), so the receiver can check that these are
    the bytes the sender handed off. Without it the receipt names no payload
    and a receiver's check refuses it (``payload_not_bound``).
    """
    data, default_type = payload_bytes(payload)
    if signer is not None:
        receipt = bind_payload(receipt, data, media_type or default_type, signer)
    doc_payload: dict[str, Any] = {
        "media_type": media_type or default_type,
        "sha256": _sha256(data),
        "size": len(data),
    }
    if name:
        doc_payload["name"] = Path(name).name
    if not detached:
        if len(data) > MAX_INLINE_BYTES:
            raise HandoffError(
                "the payload is larger than 1 MiB; send it detached instead"
            )
        if isinstance(payload, (bytes, bytearray)):
            doc_payload["content_base64"] = base64.b64encode(data).decode("ascii")
        else:
            doc_payload["content"] = payload
    return {
        "schema": SCHEMA,
        "payload": doc_payload,
        "receipt": _check_receipt_document(receipt),
    }


def _subject_media_type(media_type: str) -> str:
    from typing import get_args

    from openadapt_types import receipt as r

    essence = media_type.split(";", 1)[0].strip().lower()
    allowed = get_args(r.PayloadMediaType)
    return essence if essence in allowed else "application/octet-stream"


def _signed_by(envelope: Mapping[str, Any], kid: str, public_raw: bytes) -> bool:
    """True when ``envelope`` carries a valid DSSE signature by ``kid``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from openadapt_types import receipt as r

    if (
        not isinstance(envelope, Mapping)
        or envelope.get("payloadType") != r.PAYLOAD_TYPE
    ):
        return False
    try:
        signed = r.pae(
            r.PAYLOAD_TYPE, base64.b64decode(str(envelope["payload"]), validate=True)
        )
        key = Ed25519PublicKey.from_public_bytes(public_raw)
    except (KeyError, ValueError):
        return False
    for sig in envelope.get("signatures") or []:
        if not isinstance(sig, Mapping) or sig.get("keyid") != kid:
            continue
        try:
            key.verify(base64.b64decode(str(sig.get("sig")), validate=True), signed)
            return True
        except (InvalidSignature, ValueError):
            return False
    return False


def bind_payload(
    receipt: Mapping[str, Any], data: bytes, media_type: str, signer: Any
) -> dict[str, Any]:
    """Re-sign a run's receipt with ``data`` as its ``payload`` subject.

    Only a key that already signed the receipt may bind it, so binding never
    changes who signed. Any other signature (a Cloud countersignature) covers
    the old bytes and is dropped; it can be added again over the new ones.
    A public card keeps its openings, which still match the commitments.
    """
    from openadapt_types import receipt as r

    receipt = _check_receipt_document(receipt)
    envelope = receipt["envelope"] if r.is_receipt_card(receipt) else receipt
    kid = r.key_id(signer.public_raw())
    # Verify this computer's own signature before re-signing: a keyid with a
    # bad sig must never turn into a valid receipt.
    if not _signed_by(envelope, kid, signer.public_raw()):
        raise HandoffError(
            "this computer's key didn't sign that receipt, so it can't bind a "
            "payload to it"
        )
    try:
        statement = r.loads_strict(base64.b64decode(str(envelope["payload"])))
    except ValueError:
        raise HandoffError("the receipt's statement is not readable") from None
    subjects = [s for s in statement.get("subject") or [] if s.get("name") != "payload"]
    subjects.append(
        {
            "name": "payload",
            "digest": {"sha256": _sha256(data)},
            "media_type": _subject_media_type(media_type),
        }
    )
    statement["subject"] = subjects
    try:
        bound = r.sign_statement(statement, [(kid, signer.sign)])
    except ValueError as exc:
        raise HandoffError(f"the payload can't be bound: {exc}") from None
    if r.is_receipt_card(receipt):
        return {**receipt, "envelope": bound}
    return bound


def receipt_for_run(run_dir: Path, *, public: bool = False) -> dict[str, Any]:
    """Read a run's ``receipt.json`` (or its public card) for attaching."""
    from openadapt_types import receipt as r

    path = Path(run_dir) / (r.CARD_FILE if public else r.RECEIPT_FILE)
    try:
        return _check_receipt_document(_loads_strict(path.read_text("utf-8")))
    except FileNotFoundError:
        raise HandoffError(f"no {path.name} in that run folder") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, HandoffError):
            raise
        raise HandoffError(f"{path.name} is not a readable receipt") from None


# -- verification ----------------------------------------------------------------


@dataclass
class Verdict:
    """What a receiving agent may conclude from a handoff."""

    #: ``intact``: every signature checked against a pinned key and the
    #: receipt is well formed. ``refused``: anything else.
    integrity: str
    reason: Optional[str] = None
    detail: str = ""
    signer_roles: list[str] = field(default_factory=list)
    signer_lines: list[str] = field(default_factory=list)
    demo: bool = False
    #: The receipt's outcome, copied, never upgraded.
    outcome: Optional[str] = None
    checks: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    confirmation_lines: list[str] = field(default_factory=list)
    independent_confirmations: int = 0
    what_this_proves: list[str] = field(default_factory=list)
    #: The receiver's own read of its own record, when it made one
    #: (:func:`decide`). ``None``: the receiver didn't check.
    receiver_check: Optional[dict[str, Any]] = None
    #: ``accept`` only when the receipt is intact and the receiver's own
    #: record shows the claim; ``reject``; or ``unchecked`` (no own read).
    decision: str = "unchecked"

    @property
    def ok(self) -> bool:
        return self.integrity == "intact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": VERDICT_SCHEMA,
            "integrity": self.integrity,
            "reason": self.reason,
            "detail": self.detail,
            "signer_roles": list(self.signer_roles),
            "signer_lines": list(self.signer_lines),
            "demo": self.demo,
            "outcome": self.outcome,
            "checks": self.checks,
            "payload": self.payload,
            "confirmations": self.confirmations,
            "confirmation_lines": list(self.confirmation_lines),
            "independent_confirmations": self.independent_confirmations,
            "what_this_proves": list(self.what_this_proves),
            "receiver_check": self.receiver_check,
            "decision": self.decision,
        }


def _refused(reason: str, detail: str = "", line: Optional[str] = None) -> Verdict:
    from openadapt_types.receipt import refusal_line

    return Verdict(
        integrity="refused",
        decision="reject",
        reason=reason,
        detail=detail,
        signer_lines=[line or refusal_line(reason)],
        what_this_proves=[
            "Nothing: the receipt did not verify, so treat the handoff as an "
            "unchecked claim."
        ],
    )


_CHECK_NAMES = ("identity", "postconditions", "effects", "authorization")


def _checks(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Which checks passed and which were not checked, straight from the receipt."""
    predicate = statement.get("predicate") or {}
    checks = predicate.get("checks") or {}
    passed: list[str] = []
    incomplete: list[str] = []
    for name in _CHECK_NAMES:
        count = checks.get(name) or {}
        required, confirmed = count.get("required", 0), count.get("confirmed", 0)
        if required and confirmed >= required:
            passed.append(name)
        elif required:
            incomplete.append(name)
    not_checked: list[str] = []
    for line in predicate.get("lines") or []:
        for code in line.get("not_checked") or []:
            if code not in not_checked:
                not_checked.append(code)
    return {
        "passed": passed,
        "incomplete": incomplete,
        "not_checked": not_checked,
        "writes": {
            "confirmed": checks.get("lines_confirmed", 0),
            "refuted": checks.get("lines_refuted", 0),
            "not_checked": checks.get("lines_not_checked", 0),
        },
        "counts": {name: dict(checks.get(name) or {}) for name in _CHECK_NAMES},
    }


def _confirmations(statement: Mapping[str, Any], result: Any) -> list[dict[str, Any]]:
    """Each counterparty confirmation, with the verifier's own independence flag.

    :func:`openadapt_types.receipt.verify_receipt` decides independence from
    the signed, closed ``read_method`` alone (``portal_read_back`` or
    ``api``), never from anything else in the handoff.
    """
    items = [
        c
        for c in (statement.get("predicate") or {}).get("confirmations") or []
        if isinstance(c, Mapping)
    ]
    flags = tuple(getattr(result, "independent", ()) or ())
    lines = tuple(getattr(result, "confirmation_lines", ()) or ())
    out = []
    for index, item in enumerate(items):
        out.append(
            {
                "for_step": item.get("for_step"),
                "system_kind": item.get("system_kind"),
                "state": item.get("state"),
                "read_method": item.get("read_method"),
                "strength": item.get("strength"),
                "differing_count": item.get("differing_count"),
                # Fail closed: a confirmation the verifier gave no flag for
                # is not independent.
                "independent": bool(flags[index]) if index < len(flags) else False,
                "line": lines[index] if index < len(lines) else None,
            }
        )
    return out


def _received_bytes(
    doc_payload: Mapping[str, Any], supplied: Optional[bytes]
) -> tuple[Optional[bytes], Optional[str]]:
    """The payload bytes the receiver has, checked against the handoff's digest."""
    data: Optional[bytes] = None
    if supplied is not None:
        data = supplied
    elif "content_base64" in doc_payload:
        try:
            data = base64.b64decode(str(doc_payload["content_base64"]), validate=True)
        except ValueError:
            return None, "the inline payload is not base64"
    elif "content" in doc_payload:
        data = _canonical(doc_payload["content"])
    if data is not None and _sha256(data) != doc_payload.get("sha256"):
        return None, "the payload does not match its digest in the handoff"
    return data, None


def _payload_verdict(
    doc_payload: Mapping[str, Any], statement: Mapping[str, Any], data: Optional[bytes]
) -> dict[str, Any]:
    bound = next(
        (s for s in statement.get("subject") or [] if s.get("name") == "payload"), None
    )
    result: dict[str, Any] = {
        "sha256": doc_payload.get("sha256"),
        "present": data is not None,
        "bound": data is not None,
        "media_type": (bound or {}).get("media_type"),
    }
    if data is not None:
        result["note"] = (
            "The receipt's signer bound these exact payload bytes: they are what "
            "the sender handed off."
        )
    else:
        result["note"] = (
            "The payload was not supplied, so it could not be checked against "
            "the receipt."
        )
    return result


def verify_handoff(
    document: Any,
    registry: Sequence[Any],
    *,
    payload: Optional[bytes] = None,
) -> Verdict:
    """Verify a handoff document's receipt against the receiver's pinned keys.

    ``registry`` is the receiver's own key list
    (:func:`openadapt_types.receipt.parse_registry`). ``payload`` is the
    detached file's bytes. Fails closed: anything unexpected is a refusal.
    """
    from openadapt_types import receipt as r

    if isinstance(document, (bytes, str)):
        try:
            document = _loads_strict(document)
        except (UnicodeDecodeError, ValueError):
            return _refused("malformed", "the handoff is not JSON")
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        return _refused(
            "unknown_schema",
            "not an openadapt.handoff-document/v1 document",
            "Not verified: not a handoff document",
        )
    if set(document) - {"schema", "payload", "receipt"}:
        return _refused("malformed", "a handoff has schema, payload and receipt")
    doc_payload = document.get("payload")
    receipt = document.get("receipt")
    if not isinstance(doc_payload, Mapping) or not isinstance(receipt, Mapping):
        return _refused("malformed", "a handoff has a payload and a receipt object")
    if receipt.get("schema") == r.OPENINGS_SCHEMA:
        return _refused("malformed", "a handoff never carries private openings")
    data, problem = _received_bytes(doc_payload, payload)
    if problem:
        return _refused("payload_mismatch", problem, "Not verified: " + problem)
    # With the payload in hand, the receipt must name exactly these bytes as
    # its payload subject (payload_not_bound, payload_mismatch otherwise).
    result = r.verify_receipt(receipt, registry, received=data)
    if not result.ok or result.statement is None:
        return _refused(str(result.reason or "malformed"), result.detail)
    statement = result.statement
    payload_result = _payload_verdict(doc_payload, statement, data)
    confirmations = _confirmations(statement, result)
    lines = [c["line"] for c in confirmations if c["line"]]
    independent = sum(1 for c in confirmations if c["independent"])
    proves = [WHAT_A_SIGNATURE_PROVES]
    if result.demo:
        proves.append("A demo key signed it: it checks only inside that demo.")
    if independent:
        proves.append(
            f"{independent} confirmation(s) were read back from the other "
            "party's system, so they do not rest on the sender's word alone."
        )
    else:
        proves.append(
            "No confirmation was read from the other party's system, so every "
            "claim here rests on the signer's word."
        )
    proves.append(payload_result["note"])
    return Verdict(
        integrity="intact",
        signer_roles=[s.role for s in result.signers],
        signer_lines=list(result.signer_lines),
        demo=result.demo,
        outcome=result.outcome,
        checks=_checks(statement),
        payload=payload_result,
        confirmations=confirmations,
        confirmation_lines=lines,
        independent_confirmations=independent,
        what_this_proves=proves,
    )


# -- the receiver's own record ------------------------------------------------------

#: States a receiver's own read can return, as in a counterparty confirmation.
RECEIVER_STATES = (
    "recorded_as_intended",
    "recorded_differently",
    "not_found",
    "unreadable",
)

ACCEPT_LINE = (
    "Accepted: your own system shows the record the handoff describes. That "
    "read, not the sender's signature, is what confirms the claim."
)
UNCHECKED_LINE = (
    "Not accepted: the signature shows who sent this, but a sender-signed "
    "receipt can't prove the claim. Read your own record before acting on it."
)


@dataclass(frozen=True)
class ReceiverRecord:
    """The receiver's own read of its own system for one handoff's claim."""

    state: str
    #: Closed field ids that differ, never their values.
    differing_fields: tuple[str, ...] = ()
    #: How the receiver read it, e.g. ``api`` or ``portal_read_back``.
    read_method: str = "api"

    def __post_init__(self) -> None:
        if self.state not in RECEIVER_STATES:
            raise ValueError(f"unknown receiver state {self.state!r}")


def _payload_value(document: Mapping[str, Any], supplied: Optional[bytes]) -> Any:
    doc_payload = document.get("payload") or {}
    if "content" in doc_payload:
        return doc_payload["content"]
    data = supplied
    if data is None and "content_base64" in doc_payload:
        data = base64.b64decode(str(doc_payload["content_base64"]), validate=True)
    if data is None:
        raise HandoffError("the payload is not available to check")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return data


def decide(
    document: Any,
    registry: Sequence[Any],
    receiver_check: Optional[Any] = None,
    *,
    payload: Optional[bytes] = None,
) -> Verdict:
    """Verify a handoff, then confirm its claim in the receiver's own record.

    ``receiver_check(claim) -> ReceiverRecord`` reads the receiver's own
    system for the payload's claim. The handoff is accepted only when the
    receipt is intact and that read is ``recorded_as_intended``. A fabricated
    or differing claim is rejected even with a valid signature from a
    trusted sender. Without a ``receiver_check`` the decision is
    ``unchecked``: the receipt alone never accepts a claim. Any error in the
    read rejects (fails closed).
    """
    if isinstance(document, (bytes, str)):
        try:
            document = _loads_strict(document)
        except (UnicodeDecodeError, ValueError):
            document = None
    verdict = verify_handoff(document, registry, payload=payload)
    if not verdict.ok:
        verdict.decision = "reject"
        return verdict
    if receiver_check is None:
        verdict.what_this_proves.append(UNCHECKED_LINE)
        return verdict
    try:
        record = receiver_check(_payload_value(document, payload))
        if not isinstance(record, ReceiverRecord):
            raise TypeError("receiver_check returns a ReceiverRecord")
    except Exception as exc:  # noqa: BLE001 - an unreadable record never accepts
        record = ReceiverRecord("unreadable")
        verdict.detail = f"the receiver's own read failed: {type(exc).__name__}"
    verdict.receiver_check = {
        "state": record.state,
        "differing_fields": list(record.differing_fields),
        "read_method": record.read_method,
    }
    if record.state == "recorded_as_intended":
        verdict.decision = "accept"
        verdict.what_this_proves.append(ACCEPT_LINE)
    else:
        verdict.decision = "reject"
        verdict.what_this_proves.append(
            {
                "recorded_differently": (
                    "Rejected: your own system has this record with different "
                    f"values ({len(record.differing_fields)} field(s) differ)."
                ),
                "not_found": "Rejected: your own system has no such record.",
                "unreadable": "Rejected: your own record could not be read.",
            }[record.state]
        )
    return verdict


def load_registry(path: Optional[Path] = None) -> list[Any]:
    """The receiver's pinned keys: a registry file, or this computer's defaults."""
    if path is not None:
        from openadapt_flow.receipt_record import load_role_registry

        return load_role_registry(Path(path))
    from openadapt_flow.receipt_record import local_registry

    return local_registry()


def detached_path(payload_path: Path) -> Path:
    return Path(str(payload_path) + DETACHED_SUFFIX)


__all__ = [
    "DETACHED_SUFFIX",
    "EXTENSION_URI",
    "HandoffError",
    "MEDIA_TYPE",
    "SCHEMA",
    "VERDICT_SCHEMA",
    "RECEIVER_STATES",
    "ReceiverRecord",
    "Verdict",
    "bind_payload",
    "decide",
    "detached_path",
    "load_registry",
    "make_handoff",
    "payload_bytes",
    "receipt_for_run",
    "verify_handoff",
]
