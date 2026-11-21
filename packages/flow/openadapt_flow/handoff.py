"""Agent-to-agent handoffs: a payload with the run's receipt attached.

One agent hands a task or a result to another. The receipt for the run that
produced it (``record/run/v1``, see :mod:`openadapt_types.receipt`) travels
with it. The receiving agent verifies the receipt against keys *it* pins and
gets a verdict that says exactly what the signature does and does not prove.

The same document works over any channel:

* **Inline**: one JSON document (``openadapt.handoff/v1``) holding the payload
  and the receipt. It is the body of an HTTP request or webhook, an MCP tool
  result, or an A2A artifact part.
* **Detached**: the payload stays a file (an email attachment, a shared
  folder), and ``<file>.handoff.json`` beside it holds the payload's SHA-256
  and the receipt.

What the verdict says, and why:

* A valid signature proves **who signed** the receipt (the role comes from
  the verifier's pinned registry, never from the document) and that the
  receipt is unchanged. It does not prove the receipt's claims are true.
* The receipt signs the run (its report and bundle), not the handoff payload.
  The payload counts as **bound** only when its SHA-256 equals a signed
  subject digest. Otherwise anyone could staple this receipt to another
  payload, and the verdict says so.
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

SCHEMA = "openadapt.handoff/v1"
VERDICT_SCHEMA = "openadapt.handoff-verdict/v1"
MEDIA_TYPE = "application/vnd.openadapt.handoff+json"
#: The draft extension URI (docs/HANDOFF_RECEIPTS.md). An identifier only.
EXTENSION_URI = "https://openadapt.ai/extensions/handoff-receipt/v1"
DETACHED_SUFFIX = ".handoff.json"
#: Largest inline payload, in bytes. A bigger one goes detached.
MAX_INLINE_BYTES = 1_048_576
#: Read methods that look at the other party's system, not the sender's.
INDEPENDENT_READ_METHODS = frozenset({"portal_read_back", "api"})

WHAT_A_SIGNATURE_PROVES = (
    "A valid signature proves who signed this receipt and that it has not "
    "changed since. It does not prove that what the receipt says is true."
)
NOT_BOUND = (
    "The receipt signs the run's report, not this payload. Nothing ties this "
    "payload to the receipt: anyone could attach the same receipt to another "
    "payload."
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
) -> dict[str, Any]:
    """Attach ``receipt`` to ``payload`` as an ``openadapt.handoff/v1`` document.

    ``detached`` leaves the payload out and keeps only its digest, for a file
    sent beside the document.
    """
    data, default_type = payload_bytes(payload)
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
        }


def _refused(reason: str, detail: str = "", line: Optional[str] = None) -> Verdict:
    from openadapt_types.receipt import refusal_line

    return Verdict(
        integrity="refused",
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


def _confirmations(statement: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each counterparty confirmation, marked independent from its read method.

    Independence is decided here, from the closed ``read_method`` the signed
    statement carries, never from anything else in the handoff.
    """
    out = []
    for item in (statement.get("predicate") or {}).get("confirmations") or []:
        if not isinstance(item, Mapping):
            continue
        state = item.get("state")
        read_method = item.get("read_method")
        independent = read_method in INDEPENDENT_READ_METHODS and state in (
            "recorded_as_intended",
            "recorded_differently",
        )
        out.append(
            {
                "for_step": item.get("for_step"),
                "system_kind": item.get("system_kind"),
                "state": state,
                "read_method": read_method,
                "strength": item.get("strength"),
                "differing_count": item.get("differing_count"),
                "independent": independent,
            }
        )
    return out


def _confirmation_line(item: Mapping[str, Any]) -> str:
    system = str(item.get("system_kind") or "other").replace("_", " ")
    state = item.get("state")
    if state == "recorded_as_intended":
        text = f"Confirmed in the other party's system ({system}): recorded as intended"
    elif state == "recorded_differently":
        text = (
            f"Confirmed in the other party's system ({system}): recorded "
            f"differently ({item.get('differing_count') or 0} field(s) differ)"
        )
    elif state == "acknowledged":
        text = f"The other party ({system}) acknowledged it; not read back"
    else:
        text = f"Sent to the other party ({system}); no answer recorded"
    if not item.get("independent"):
        text += ". Reported by the sender, not read independently"
    return text


def _payload_verdict(
    doc_payload: Mapping[str, Any],
    statement: Mapping[str, Any],
    supplied: Optional[bytes],
) -> tuple[dict[str, Any], Optional[str]]:
    """Whether the payload is what the document says, and whether it is signed."""
    declared = doc_payload.get("sha256")
    actual: Optional[str] = None
    if supplied is not None:
        actual = _sha256(supplied)
    elif "content_base64" in doc_payload:
        try:
            actual = _sha256(
                base64.b64decode(str(doc_payload["content_base64"]), validate=True)
            )
        except ValueError:
            return {}, "the inline payload is not base64"
    elif "content" in doc_payload:
        actual = _sha256(_canonical(doc_payload["content"]))
    if actual is not None and actual != declared:
        return {}, "the payload does not match its digest in the handoff"
    subjects = {
        s.get("digest", {}).get("sha256"): s.get("name")
        for s in statement.get("subject") or []
        if isinstance(s, Mapping)
    }
    subject = subjects.get(actual or declared) if (actual or declared) else None
    result: dict[str, Any] = {
        "sha256": declared,
        "present": actual is not None,
        "bound": subject is not None and actual is not None,
        "bound_to": subject if actual is not None else None,
    }
    if result["bound"]:
        result["note"] = f"The payload is the signed {subject} itself."
    elif actual is None:
        result["note"] = (
            "The payload was not supplied, so it could not be checked. " + NOT_BOUND
        )
    else:
        result["note"] = NOT_BOUND
    return result, None


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
            "not an openadapt.handoff/v1 document",
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
    result = r.verify_receipt(receipt, registry)
    if not result.ok or result.statement is None:
        return _refused(str(result.reason or "malformed"), result.detail)
    statement = result.statement
    payload_result, problem = _payload_verdict(doc_payload, statement, payload)
    if problem:
        return _refused("payload_mismatch", problem, "Not verified: " + problem)
    confirmations = _confirmations(statement)
    lines = list(getattr(result, "confirmation_lines", ()) or ())
    if not lines:
        lines = [_confirmation_line(c) for c in confirmations]
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
    "INDEPENDENT_READ_METHODS",
    "MEDIA_TYPE",
    "SCHEMA",
    "VERDICT_SCHEMA",
    "Verdict",
    "detached_path",
    "load_registry",
    "make_handoff",
    "payload_bytes",
    "receipt_for_run",
    "verify_handoff",
]
