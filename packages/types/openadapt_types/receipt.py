"""Receipts: one signed record per run (``https://openadapt.ai/record/run/v1``).

A receipt is a signed, itemized record of one run: what it changed, how each
change was checked, and who signed it. It doesn't grade the run.

Shape:

* **Statement.** An in-toto Statement v1 whose ``predicateType`` is
  :data:`PREDICATE_TYPE`. The predicate holds only closed values: digests,
  enums, counts and salted commitments. It never holds a screenshot, a typed
  value, a URL or page text; evidence files appear only by SHA-256 digest.
* **Envelope.** DSSE (``payloadType`` :data:`PAYLOAD_TYPE`). Each signature
  signs ``PAE(payloadType, payload)`` over the exact payload bytes. Several
  signers sign the same bytes; OpenAdapt Cloud adds its countersignature to
  the same envelope.
* **Openings.** Each disclosed value is a commitment
  ``sha256(DOMAIN || path || 0x00 || salt || 0x00 || canonical_json(value))``
  in ``predicate.disclosure.commitments``. The salts and values sit in a
  private sidecar (``receipt.private.json``) beside the record.
* **Public card.** The same envelope plus only the openings that
  :data:`PUBLIC_DISCLOSURE_V1` allows. The payload is never re-signed, so a
  card can't say anything the full record doesn't.

Signer roles come only from the verifier's pinned key registry, never from
the envelope. An unknown key, role or schema fails closed. Verifying needs
``cryptography``; without it verification refuses (``verifier_unavailable``).

The Cloud and website verifiers implement the same rules in TypeScript and
run the vectors in ``packages/types/tests/vectors/record-run-v1``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

STATEMENT_TYPE: Final = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE: Final = "https://openadapt.ai/record/run/v1"
PAYLOAD_TYPE: Final = "application/vnd.in-toto+json"
#: The closed, versioned list of values a public card may reveal.
PUBLIC_DISCLOSURE_VERSION: Final = "openadapt.public-disclosure/v1"
OPENINGS_SCHEMA: Final = "openadapt.receipt-openings/v1"
CARD_SCHEMA: Final = "openadapt.receipt-card/v1"
REGISTRY_SCHEMA: Final = "openadapt.receipt-keys/v1"
COMMITMENT_DOMAIN: Final = b"openadapt.receipt-commitment/v1\x00"

#: The record and its private openings, as written in a run folder.
RECEIPT_FILE: Final = "receipt.json"
PRIVATE_FILE: Final = "receipt.private.json"
CARD_FILE: Final = "receipt.card.json"

MAX_LINES: Final = 256
MAX_COUNT: Final = 2**53 - 1
#: Largest payload a verifier reads, in bytes.
MAX_PAYLOAD_BYTES: Final = 65536

_SHA256_HEX = r"^[a-f0-9]{64}$"
_SALT_HEX = re.compile(r"[a-f0-9]{64}")
_HOUR = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z$"
_ISO_SECONDS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_KEY_ID = r"^seal-ed25519-[a-f0-9]{16}$"
_KEY_ID_RE = re.compile(_KEY_ID[1:-1])
_PROGRAM_ID = r"^prog-[a-f0-9]{16}$"
_REASON = r"^[a-z][a-z0-9_]{0,47}$"
_ORG = re.compile(r"[A-Za-z0-9 ._-]{1,48}")

Outcome = Literal["VERIFIED", "STOPPED", "UNKNOWN", "FAILED"]
#: How the run's own outcome maps onto a receipt outcome. Only ``VERIFIED``
#: maps to ``VERIFIED``: signing never upgrades a run.
OUTCOME_FROM_RUN: Final[Mapping[str, str]] = {
    "VERIFIED": "VERIFIED",
    "HALTED": "STOPPED",
    "ROLLED_BACK": "STOPPED",
    "STOPPED": "STOPPED",
    "FAILED": "FAILED",
    "COMPLETED_UNVERIFIED": "UNKNOWN",
    "RECONCILIATION_REQUIRED": "UNKNOWN",
}
TargetKind = Literal["gui", "api"]
Operation = Literal["write", "irreversible_write"]
CheckResult = Literal["confirmed", "refuted", "not_checked"]
#: ``read_back``: the system of record was read after the write.
#: ``same_screen``: only the screen's postconditions held. ``none``: nothing.
Strength = Literal["read_back", "same_screen", "none"]
NotChecked = Literal[
    "effect_not_declared",
    "approved_unverified",
    "field_values",
    "postconditions",
]
SubjectName = Literal["report.json", "bundle", "evidence", "payload"]
#: The media type of a handed-off payload (the ``payload`` subject only).
PayloadMediaType = Literal[
    "application/json",
    "application/cbor",
    "text/plain",
    "text/csv",
    "application/pdf",
    "application/octet-stream",
]
ApprovalMode = Literal["each_item", "problems_only", "not_gated"]
#: Who answered, as Flow's item approval records it (decided_by).
ApproverKind = Literal["person", "agent", "none"]

Role = Literal[
    "this_computer", "customer_key", "openadapt_cloud", "public_log", "demo"
]
ROLES: Final[tuple[str, ...]] = Role.__args__  # type: ignore[attr-defined]

ReceiptRefusal = Literal[
    "malformed",
    "unknown_schema",
    "unsigned",
    "unknown_signer",
    "unknown_role",
    "revoked_key",
    "bad_signature",
    "mixed_demo",
    "embedded_content",
    "opening_mismatch",
    "disclosure_not_allowed",
    "inconsistent",
    "payload_mismatch",
    "payload_not_bound",
    "verifier_unavailable",
]

#: The signer line for each role, as every verifier prints it.
SIGNER_LINES: Final[Mapping[str, str]] = {
    "this_computer": "Signed by this computer",
    "customer_key": "Signed by {org}'s key",
    "openadapt_cloud": "Countersigned by OpenAdapt Cloud",
    "public_log": "Logged publicly",
    "demo": "Demo key: checks only in this demo",
}
UNKNOWN_SIGNER_LINE: Final = "Not verified: unknown signer"
_ROLE_ORDER: Final = ("this_computer", "customer_key", "openadapt_cloud", "public_log")


# -- the statement -------------------------------------------------------------


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


Count = StrictInt


class Digest(_Closed):
    sha256: StrictStr = Field(pattern=_SHA256_HEX)


class Subject(_Closed):
    name: SubjectName
    digest: Digest
    #: Only on ``payload``: the bytes handed to another party (an MCP tool
    #: result, an HTTP body or a file), by digest and media type.
    media_type: Optional[PayloadMediaType] = None


_APPROVAL_KEY = re.compile(r"principal|scope|approved_at")


class Approval(_Closed):
    """Who approved the run's writes, from Flow's item approval binding.

    The principal, the scope and the time are salted commitments; a public
    card shows only the mode and the approver kind.
    """

    mode: ApprovalMode
    approver_kind: ApproverKind
    commitments: dict[StrictStr, StrictStr] = Field(max_length=3)


class Program(_Closed):
    #: ``prog-`` and 16 hex of the program's name hash: never the name itself.
    id: StrictStr = Field(pattern=_PROGRAM_ID)
    version_sha256: StrictStr = Field(pattern=_SHA256_HEX)


class Line(_Closed):
    """One write, its check, how strong the check was, and what it didn't check."""

    step: Count = Field(ge=0, le=10_000)
    target_kind: TargetKind
    operation: Operation
    check: CheckResult
    strength: Strength
    not_checked: list[NotChecked] = Field(max_length=4)


class CheckCount(_Closed):
    required: Count = Field(ge=0, le=MAX_COUNT)
    confirmed: Count = Field(ge=0, le=MAX_COUNT)


class Checks(_Closed):
    identity: CheckCount
    postconditions: CheckCount
    effects: CheckCount
    authorization: CheckCount
    lines_confirmed: Count = Field(ge=0, le=MAX_LINES)
    lines_refuted: Count = Field(ge=0, le=MAX_LINES)
    lines_not_checked: Count = Field(ge=0, le=MAX_LINES)


class Steps(_Closed):
    total: Count = Field(ge=0, le=MAX_COUNT)
    ok: Count = Field(ge=0, le=MAX_COUNT)


class Disclosure(_Closed):
    version: Literal["openadapt.public-disclosure/v1"]
    #: Field path to its salted commitment (64 hex).
    commitments: dict[StrictStr, StrictStr] = Field(max_length=16)


SystemKind = Literal[
    "ehr", "payer_portal", "bank", "erp", "crm", "government_portal", "email", "other"
]
ReadMethod = Literal["portal_read_back", "api", "acknowledgment_message"]
ConfirmationState = Literal[
    "sent", "acknowledged", "recorded_as_intended", "recorded_differently"
]
ConfirmationStrength = Literal["read_back", "acknowledgment", "none"]
#: Read methods that look at the other party's own system of record.
INDEPENDENT_READS: Final = ("portal_read_back", "api")
_CONFIRMATION_KEY = re.compile(r"counterparty|observed_at|reason|field\.[0-9]{1,2}")
ConfirmationReason = Literal[
    "rejected_by_counterparty",
    "conflicting_report",
    "ambiguous_key",
    "receiver_unreachable",
    "not_found",
    "no_answer_by_deadline",
]
#: Which reasons each state may carry (no reason is always allowed). The
#: reason is committed, so it shows only where the private openings are read.
REASONS_FOR_STATE: Final[Mapping[str, frozenset[str]]] = {
    "sent": frozenset(
        {
            "rejected_by_counterparty",
            "ambiguous_key",
            "receiver_unreachable",
            "not_found",
            "no_answer_by_deadline",
        }
    ),
    "acknowledged": frozenset({"ambiguous_key", "not_found", "no_answer_by_deadline"}),
    "recorded_as_intended": frozenset({"conflicting_report"}),
    "recorded_differently": frozenset({"conflicting_report"}),
}


class Confirmation(_Closed):
    """Evidence read from another party's system about one handoff.

    Everything that could identify the other party, the time or the fields
    is a salted commitment; the openings stay in the private sidecar.
    """

    kind: Literal["counterparty_confirmation"]
    for_step: Optional[Count] = Field(default=None, ge=0, le=10_000)
    system_kind: SystemKind
    read_method: ReadMethod
    state: ConfirmationState
    strength: ConfirmationStrength
    differing_count: Count = Field(ge=0, le=64)
    commitments: dict[StrictStr, StrictStr] = Field(min_length=2, max_length=34)


class RunPredicateV1(_Closed):
    program: Program
    started_hour: StrictStr = Field(pattern=_HOUR)
    outcome: Outcome
    reason_code: Optional[StrictStr] = Field(default=None, pattern=_REASON)
    steps: Steps
    lines: list[Line] = Field(max_length=MAX_LINES)
    checks: Checks
    model_calls: Count = Field(ge=0, le=MAX_COUNT)
    disclosure: Disclosure
    #: Counterparty confirmations; omitted from the payload when empty, so a
    #: receipt without one is byte-identical to one made before they existed.
    confirmations: list[Confirmation] = Field(default_factory=list, max_length=64)
    #: The approval binding; omitted when the run recorded none.
    approval: Optional[Approval] = None


class RunStatementV1(_Closed):
    type_: Literal["https://in-toto.io/Statement/v1"] = Field(alias="_type")
    subject: list[Subject] = Field(min_length=1, max_length=4)
    predicateType: Literal["https://openadapt.ai/record/run/v1"]
    predicate: RunPredicateV1

    def wire(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", by_alias=True)
        if not out["predicate"]["confirmations"]:
            del out["predicate"]["confirmations"]
        if out["predicate"]["approval"] is None:
            del out["predicate"]["approval"]
        for subject in out["subject"]:
            if subject["media_type"] is None:
                del subject["media_type"]
        return out


def _check_confirmation(c: Confirmation, steps: set[int]) -> Optional[str]:
    if c.for_step is not None and c.for_step not in steps:
        return "a confirmation names a step with no line"
    if c.state == "sent" and c.strength != "none":
        return "a sent handoff has no confirmation strength"
    if c.state == "acknowledged" and c.strength != "acknowledgment":
        return "an acknowledgment has acknowledgment strength"
    if c.state.startswith("recorded_") and (
        c.read_method not in INDEPENDENT_READS or c.strength != "read_back"
    ):
        return "only a portal or API read-back can say how the record was recorded"
    if (c.state == "recorded_differently") != (c.differing_count > 0):
        return "differing_count disagrees with the state"
    if not {"counterparty", "observed_at"} <= set(c.commitments):
        return "a confirmation commits to its counterparty and observed_at"
    for key, value in c.commitments.items():
        if not _CONFIRMATION_KEY.fullmatch(key) or not re.fullmatch(
            _SHA256_HEX[1:-1], value
        ):
            return f"confirmation commitment {key!r} is not a known field"
    fields = sum(1 for key in c.commitments if key.startswith("field."))
    if c.differing_count > fields:
        return "more differing fields than observed fields"
    return None


def _check_approval(a: Approval) -> Optional[str]:
    for key, value in a.commitments.items():
        if not _APPROVAL_KEY.fullmatch(key) or not re.fullmatch(_SHA256_HEX[1:-1], value):
            return f"approval commitment {key!r} is not a known field"
    if a.mode == "not_gated":
        if a.approver_kind != "none":
            return "a not_gated run names no approver"
    elif a.approver_kind == "none" or set(a.commitments) != {
        "principal",
        "scope",
        "approved_at",
    }:
        return "a gated approval names its approver kind, principal, scope and time"
    return None


def _check_consistent(statement: RunStatementV1) -> Optional[str]:
    """A closed statement that contradicts itself is refused, never repaired."""
    p = statement.predicate
    names = [s.name for s in statement.subject]
    if len(set(names)) != len(names) or "report.json" not in names:
        return "subject names repeat, or the report is missing"
    for subject in statement.subject:
        if (subject.name == "payload") != (subject.media_type is not None):
            return "only the payload subject has a media type, and it always has one"
    if p.approval is not None:
        problem = _check_approval(p.approval)
        if problem:
            return problem
    for path, value in p.disclosure.commitments.items():
        if path not in DISCLOSURE_FIELDS or not re.fullmatch(_SHA256_HEX[1:-1], value):
            return f"disclosure path {path!r} is not a known field"
    if p.steps.ok > p.steps.total:
        return "steps.ok is more than steps.total"
    for name in ("identity", "postconditions", "effects", "authorization"):
        count = getattr(p.checks, name)
        if count.confirmed > count.required:
            return f"checks.{name} confirms more than it requires"
    tally = {"confirmed": 0, "refuted": 0, "not_checked": 0}
    for line in p.lines:
        tally[line.check] += 1
        if line.check == "confirmed" and line.strength == "none":
            return "a confirmed line has no strength"
        if len(set(line.not_checked)) != len(line.not_checked):
            return "a line repeats a not_checked code"
    if (
        p.checks.lines_confirmed,
        p.checks.lines_refuted,
        p.checks.lines_not_checked,
    ) != (tally["confirmed"], tally["refuted"], tally["not_checked"]):
        return "check counts disagree with the lines"
    if p.outcome == "VERIFIED" and tally["refuted"]:
        return "a VERIFIED receipt has a refuted line"
    steps = [line.step for line in p.lines]
    if steps != sorted(set(steps)):
        return "lines are not in step order, or repeat a step"
    for c in p.confirmations:
        problem = _check_confirmation(c, set(steps))
        if problem:
            return problem
    return None


# -- disclosure ---------------------------------------------------------------

#: Every field a statement may commit to, with the check its value must pass.
#: Private-only fields (``run_id``) are committed but never revealed publicly.
_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,48}")
_SEMVER = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")
_RUN_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
DISCLOSURE_FIELDS: Final[Mapping[str, re.Pattern[str]]] = {
    "program_name": _NAME,
    "flow_version": _SEMVER,
    "github_login": _LOGIN,
    "run_id": _RUN_ID,
}
#: ``openadapt.public-disclosure/v1``: what a public card may reveal. Each
#: entry names the field it comes from in today's ``public-card/v1`` or
#: ``public-evidence-receipt/v1``; a test keeps this list inside their union.
#: Widening it needs a new, reviewed version.
PUBLIC_DISCLOSURE_V1: Final[Mapping[str, str]] = {
    "program_name": "public-card/v1:program_name",
    "flow_version": "public-card/v1:flow_version",
    "github_login": "public-card/v1:identity_claim.login",
}
PUBLIC_DISCLOSURE: Final[Mapping[str, Mapping[str, str]]] = {
    PUBLIC_DISCLOSURE_VERSION: PUBLIC_DISCLOSURE_V1,
}


def canonical_json(value: Any) -> str:
    """Sorted-key compact JSON with no ASCII escaping (Cloud's canonical form)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def commitment(path: str, salt_hex: str, value: str) -> str:
    """The salted commitment to one disclosed value."""
    if not _SALT_HEX.fullmatch(salt_hex):
        raise ValueError("a salt is 32 bytes, as 64 lower-case hex")
    data = (
        COMMITMENT_DOMAIN
        + path.encode("utf-8")
        + b"\x00"
        + bytes.fromhex(salt_hex)
        + b"\x00"
        + canonical_json(value).encode("utf-8")
    )
    return hashlib.sha256(data).hexdigest()


def make_openings(
    values: Mapping[str, Optional[str]], *, salt: Callable[[], str] | None = None
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Commitments for ``values`` and their private openings (salt and value).

    ``None`` values are left out. Each value must pass its field's check, so
    a screenshot, URL or free text is refused before anything is committed.
    """
    make_salt = salt or (lambda: secrets.token_hex(32))
    commitments: dict[str, str] = {}
    openings: dict[str, dict[str, str]] = {}
    for path, value in values.items():
        if value is None:
            continue
        problem = _value_problem(path, value)
        if problem:
            raise ValueError(problem)
        s = make_salt()
        commitments[path] = commitment(path, s, value)
        openings[path] = {"salt": s, "value": value}
    return commitments, openings


_APPROVAL_PATH = re.compile(r"approval\.(principal|scope|approved_at)")
_APPROVAL_VALUES: Final[Mapping[str, re.Pattern[str]]] = {
    "principal": re.compile(r"(person|agent):pr-[a-f0-9]{16}"),
    "scope": re.compile(r"[a-f0-9]{64}"),
    "approved_at": _ISO_SECONDS,
}
_CONFIRMATION_PATH = re.compile(
    r"confirmations\.([0-9]{1,2})\.(counterparty|observed_at|reason|field\.[0-9]{1,2})"
)
_CONFIRMATION_VALUES: Final[Mapping[str, re.Pattern[str]]] = {
    "counterparty": re.compile(
        r"(ehr|payer_portal|bank|erp|crm|government_portal|email|other):cp-[a-f0-9]{16}"
    ),
    "observed_at": _ISO_SECONDS,
    "reason": re.compile(
        r"rejected_by_counterparty|conflicting_report|ambiguous_key"
        r"|receiver_unreachable|not_found|no_answer_by_deadline"
    ),
    "field": re.compile(r"[a-z][a-z0-9_]{0,31}:[a-f0-9]{64}(:differs)?"),
}


def confirmation_path(index: int, key: str) -> str:
    return f"confirmations.{index}.{key}"


def _value_problem(path: str, value: Any) -> Optional[str]:
    approval = _APPROVAL_PATH.fullmatch(path)
    if approval:
        if not (isinstance(value, str) and _APPROVAL_VALUES[approval.group(1)].fullmatch(value)):
            return f"{path!r} does not match its field's pattern"
        return None
    found = _CONFIRMATION_PATH.fullmatch(path)
    if found:
        if not isinstance(value, str):
            return f"{path!r} is not a string"
        kind = found.group(2).split(".")[0]
        if not _CONFIRMATION_VALUES[kind].fullmatch(value):
            return f"{path!r} does not match its field's pattern"
        return None
    pattern = DISCLOSURE_FIELDS.get(path)
    if pattern is None:
        return f"{path!r} is not a disclosure field"
    if not isinstance(value, str):
        return f"{path!r} is not a string"
    if _embedded(value):
        return f"{path!r} holds embedded content"
    if not pattern.fullmatch(value):
        return f"{path!r} does not match its field's pattern"
    return None


# -- embedded content --------------------------------------------------------

_DATA_URI = re.compile(r"data:[a-z]+/", re.IGNORECASE)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{96,}={0,2}")
_IMAGE_MAGIC = ("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR", "PHN2Zy")


def _embedded(text: str) -> bool:
    return bool(
        len(text) > 256
        or _DATA_URI.search(text)
        or _URL.search(text)
        or _BASE64_RUN.search(text)
        or any(text.startswith(magic) for magic in _IMAGE_MAGIC)
        or "<svg" in text.lower()
        or "<img" in text.lower()
    )


def find_embedded_content(value: Any, path: str = "$") -> Optional[str]:
    """The path of the first screenshot, base64 blob, URL or long text in ``value``."""
    if isinstance(value, str):
        return path if _embedded(value) else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _embedded(key):
                return f"{path}.<key>"
            found = find_embedded_content(item, f"{path}.{key}")
            if found:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_embedded_content(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _statement_embedded(statement: Mapping[str, Any]) -> Optional[str]:
    """Scan everything but the two fixed type URIs, which are checked exactly."""
    rest = {k: v for k, v in statement.items() if k not in ("_type", "predicateType")}
    return find_embedded_content(rest)


# -- envelope ------------------------------------------------------------------


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE's pre-authentication encoding: what each signature signs."""
    kind = payload_type.encode("utf-8")
    return b"DSSEv1 %d %b %d %b" % (len(kind), kind, len(payload), payload)


def key_id(public_key_raw: bytes) -> str:
    """A key's id: the same id form Seal keys and Cloud issuer keys use."""
    return "seal-ed25519-" + hashlib.sha256(public_key_raw).hexdigest()[:16]


Signer = Callable[[bytes], bytes]


def statement_payload(statement: Mapping[str, Any]) -> bytes:
    """Validate a statement and return its canonical payload bytes."""
    parsed = RunStatementV1.model_validate(statement)
    problem = _check_consistent(parsed)
    if problem:
        raise ValueError(problem)
    found = _statement_embedded(parsed.wire())
    if found:
        raise ValueError(f"embedded content at {found}")
    return canonical_json(parsed.wire()).encode("utf-8")


def sign_statement(
    statement: Mapping[str, Any], signers: Sequence[tuple[str, Signer]]
) -> dict[str, Any]:
    """A DSSE envelope over ``statement``, signed by each ``(key_id, sign)``."""
    payload = statement_payload(statement)
    envelope = {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [],
    }
    for kid, sign in signers:
        envelope = add_signature(envelope, kid, sign)
    return envelope


def add_signature(envelope: Mapping[str, Any], kid: str, sign: Signer) -> dict[str, Any]:
    """Add one signature (a countersignature) over the same payload bytes."""
    if not _KEY_ID_RE.fullmatch(kid):
        raise ValueError("a key id is seal-ed25519-<16 hex>")
    payload = base64.b64decode(str(envelope["payload"]), validate=True)
    signatures = list(envelope.get("signatures") or [])
    if any(s.get("keyid") == kid for s in signatures):
        raise ValueError("this key already signed the envelope")
    sig = sign(pae(str(envelope["payloadType"]), payload))
    signatures.append({"keyid": kid, "sig": base64.b64encode(sig).decode("ascii")})
    return {
        "payloadType": envelope["payloadType"],
        "payload": envelope["payload"],
        "signatures": signatures,
    }


def public_card(
    envelope: Mapping[str, Any],
    openings: Mapping[str, Mapping[str, str]],
    *,
    reveal: Sequence[str] = (),
    version: str = PUBLIC_DISCLOSURE_VERSION,
) -> dict[str, Any]:
    """The public card: the same envelope with only allow-listed openings."""
    allowed = PUBLIC_DISCLOSURE[version]
    shown: dict[str, dict[str, str]] = {}
    for path in reveal:
        if path not in allowed:
            raise ValueError(f"{path!r} is not on the {version} allow-list")
        if path in openings:
            shown[path] = {
                "salt": str(openings[path]["salt"]),
                "value": str(openings[path]["value"]),
            }
    return {
        "schema": CARD_SCHEMA,
        "disclosure_version": version,
        "envelope": dict(envelope),
        "openings": shown,
    }


def private_openings(
    envelope: Mapping[str, Any], openings: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """The private sidecar, bound to the envelope's payload digest."""
    payload = base64.b64decode(str(envelope["payload"]), validate=True)
    return {
        "schema": OPENINGS_SCHEMA,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "openings": {k: dict(v) for k, v in openings.items()},
    }


# -- key registry ----------------------------------------------------------------


@dataclass(frozen=True)
class RoleKey:
    key_id: str
    public_key: str  # base64 of the raw 32 bytes
    role: str
    status: Literal["active", "revoked"] = "active"
    revoked_at: Optional[str] = None
    org: Optional[str] = None


class RegistryError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def parse_registry(data: Any) -> list[RoleKey]:
    """Parse a pinned key registry. Every problem is a refusal, never a default.

    A registry is ``{"schema": "openadapt.receipt-keys/v1", "keys": [...]}``.
    Each key names its role; an unknown role refuses the whole registry
    (``unknown_role``), so a typo can't make a key trusted.
    """
    if not isinstance(data, Mapping) or data.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError("unknown_schema", "not an openadapt.receipt-keys/v1 registry")
    entries = data.get("keys")
    if not isinstance(entries, list):
        raise RegistryError("malformed", "a registry has a keys list")
    keys: list[RoleKey] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RegistryError("malformed", "a registry entry is an object")
        if set(entry) - {"key_id", "public_key", "role", "status", "revoked_at", "org"}:
            raise RegistryError("malformed", "a registry entry has an unknown field")
        role = entry.get("role")
        if role not in ROLES:
            raise RegistryError("unknown_role", f"unknown signer role {role!r}")
        try:
            raw = base64.b64decode(str(entry.get("public_key", "")), validate=True)
        except ValueError as exc:
            raise RegistryError("malformed", "a public key is base64") from exc
        if len(raw) != 32 or entry.get("key_id") != key_id(raw):
            raise RegistryError("malformed", "a key id doesn't match its public key")
        status = entry.get("status", "active")
        revoked_at = entry.get("revoked_at")
        if status not in ("active", "revoked") or (
            status == "revoked"
            and not (isinstance(revoked_at, str) and _ISO_SECONDS.fullmatch(revoked_at))
        ):
            raise RegistryError("malformed", "a key is active, or revoked with revoked_at")
        org = entry.get("org")
        if role == "customer_key" and not (isinstance(org, str) and _ORG.fullmatch(org)):
            raise RegistryError("malformed", "a customer key names its org")
        keys.append(
            RoleKey(
                key_id=str(entry["key_id"]),
                public_key=base64.b64encode(raw).decode("ascii"),
                role=str(role),
                status=status,
                revoked_at=revoked_at if status == "revoked" else None,
                org=org if role == "customer_key" else None,
            )
        )
    ids = [k.key_id for k in keys]
    if len(set(ids)) != len(ids):
        raise RegistryError("malformed", "a registry lists one key id twice")
    return keys


# -- verification ----------------------------------------------------------------


@dataclass(frozen=True)
class SignerCheck:
    key_id: str
    role: str
    line: str


@dataclass(frozen=True)
class ReceiptVerification:
    ok: bool
    reason: Optional[ReceiptRefusal] = None
    detail: str = ""
    signers: tuple[SignerCheck, ...] = ()
    #: One line per verified role, strongest last; or the refusal line.
    signer_lines: tuple[str, ...] = ()
    demo: bool = False
    #: The statement's outcome, copied, never upgraded. ``None`` when refused.
    outcome: Optional[str] = None
    statement: Optional[dict[str, Any]] = None
    disclosed: Mapping[str, str] = field(default_factory=dict)
    #: True when a full private openings file was checked.
    private: bool = False
    #: One line per counterparty confirmation. Never merged with, and never
    #: upgrading, ``signer_lines``.
    confirmation_lines: tuple[str, ...] = ()
    #: Per confirmation: read from the other party's own system (portal or
    #: API). Decided only from the read method; the signer's role comes
    #: from the pinned registry.
    independent: tuple[bool, ...] = ()
    #: "No approval recorded", or how the run's writes were approved.
    approval_line: str = ""
    #: True when ``payload`` bytes were given and match the payload subject.
    payload_bound: Optional[bool] = None


_REFUSAL_LINES: Final[Mapping[str, str]] = {
    "unknown_signer": UNKNOWN_SIGNER_LINE,
    "unknown_role": "Not verified: unknown signer role",
    "unknown_schema": "Not verified: unknown receipt format",
    "unsigned": "Not verified: no signature",
    "revoked_key": "Not verified: the signing key was revoked",
    "bad_signature": "Not verified: the receipt was changed after it was signed",
    "mixed_demo": "Not verified: a demo key signed alongside a real one",
    "embedded_content": "Not verified: the receipt holds embedded content",
    "opening_mismatch": "Not verified: a disclosed value doesn't match the receipt",
    "disclosure_not_allowed": "Not verified: the card reveals a field it may not",
    "inconsistent": "Not verified: the receipt contradicts itself",
    "payload_mismatch": "Not verified: these bytes aren't the payload the receipt signed",
    "payload_not_bound": "Not verified: the receipt isn't bound to a payload",
    "malformed": "Not verified: malformed receipt",
    "verifier_unavailable": "Not verified: install cryptography to check signatures",
}


def refusal_line(reason: str) -> str:
    return _REFUSAL_LINES.get(reason, "Not verified")


def _refuse(reason: ReceiptRefusal, detail: str = "") -> ReceiptVerification:
    return ReceiptVerification(
        False, reason=reason, detail=detail, signer_lines=(refusal_line(reason),)
    )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def loads_strict(text: str | bytes) -> Any:
    """JSON with duplicate keys refused (two readers must never disagree)."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def is_receipt_envelope(value: Any) -> bool:
    return isinstance(value, Mapping) and "payloadType" in value and "payload" in value


def is_receipt_card(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("schema") == CARD_SCHEMA


def verify_receipt(
    document: Mapping[str, Any],
    registry: Sequence[RoleKey],
    *,
    openings: Optional[Mapping[str, Any]] = None,
    received: Optional[bytes] = None,
) -> ReceiptVerification:
    """Verify a receipt (a DSSE envelope) or a public card against pinned keys.

    ``document`` is the envelope (``receipt.json``) or a card
    (``openadapt.receipt-card/v1``). ``openings`` is the private sidecar for
    an envelope; its values are checked against the commitments.
    ``received`` is the bytes a receiver got in a handoff; the receipt must
    name them as its ``payload`` subject (``payload_not_bound`` or
    ``payload_mismatch`` otherwise). A card's
    openings must be on its disclosure version's allow-list.

    Fails closed: an unknown key, role or schema, any bad signature, a demo
    key next to a real one, or embedded content is a refusal.
    """
    card_openings: Optional[Mapping[str, Any]] = None
    allowed: Optional[Mapping[str, str]] = None
    if is_receipt_card(document):
        if set(document) != {"schema", "disclosure_version", "envelope", "openings"}:
            return _refuse("malformed", "a card has exactly four fields")
        allowed = PUBLIC_DISCLOSURE.get(str(document.get("disclosure_version")))
        if allowed is None:
            return _refuse("unknown_schema", "unknown public disclosure version")
        card_openings = document.get("openings")
        if not isinstance(card_openings, Mapping):
            return _refuse("malformed", "a card's openings are an object")
        envelope = document.get("envelope")
        if openings is not None:
            return _refuse("malformed", "a card carries its own openings")
    else:
        envelope = document
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "payloadType",
        "payload",
        "signatures",
    }:
        return _refuse("malformed", "a DSSE envelope has payloadType, payload, signatures")
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        return _refuse("unknown_schema", "the payload is not an in-toto statement")
    try:
        payload = base64.b64decode(str(envelope.get("payload")), validate=True)
    except ValueError:
        return _refuse("malformed", "the payload is not base64")
    if len(payload) > MAX_PAYLOAD_BYTES:
        return _refuse("malformed", "the payload is too large")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        return _refuse("unsigned")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return _refuse("verifier_unavailable")
    by_id = {k.key_id: k for k in registry}
    signed = pae(PAYLOAD_TYPE, payload)
    checks: list[SignerCheck] = []
    seen: set[str] = set()
    for sig in signatures:
        if not isinstance(sig, Mapping) or set(sig) != {"keyid", "sig"}:
            return _refuse("malformed", "a signature has keyid and sig")
        kid = sig.get("keyid")
        if not isinstance(kid, str) or kid in seen:
            return _refuse("malformed", "a signature's keyid is missing or repeated")
        seen.add(kid)
        key = by_id.get(kid)
        if key is None:
            return _refuse("unknown_signer", f"{kid} is not in the pinned registry")
        if key.role not in ROLES:
            return _refuse("unknown_role", f"{kid} has role {key.role!r}")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key.public_key)).verify(
                base64.b64decode(str(sig.get("sig")), validate=True), signed
            )
        except (InvalidSignature, ValueError):
            return _refuse("bad_signature", f"{kid}'s signature")
        if key.status == "revoked":
            return _refuse("revoked_key", f"{kid} was revoked at {key.revoked_at}")
        line = SIGNER_LINES[key.role].format(org=key.org or "the customer")
        checks.append(SignerCheck(kid, key.role, line))
    roles = {c.role for c in checks}
    demo = "demo" in roles
    if demo and roles != {"demo"}:
        return _refuse("mixed_demo")
    try:
        text = payload.decode("utf-8")
        raw = loads_strict(text)
    except (UnicodeDecodeError, ValueError):
        return _refuse("malformed", "the payload is not JSON")
    if not isinstance(raw, Mapping):
        return _refuse("malformed", "the payload is not an object")
    if raw.get("_type") != STATEMENT_TYPE or raw.get("predicateType") != PREDICATE_TYPE:
        return _refuse("unknown_schema", "not a record/run/v1 statement")
    found = _statement_embedded(raw)
    if found:
        return _refuse("embedded_content", found)
    try:
        statement = RunStatementV1.model_validate(raw)
    except ValidationError as exc:
        return _refuse("malformed", str(exc).splitlines()[0])
    if canonical_json(statement.wire()).encode("utf-8") != payload:
        return _refuse("malformed", "the payload is not canonical JSON")
    problem = _check_consistent(statement)
    if problem:
        return _refuse("inconsistent", problem)
    commitments = dict(statement.predicate.disclosure.commitments)
    for index, c in enumerate(statement.predicate.confirmations):
        for key, value in c.commitments.items():
            commitments[confirmation_path(index, key)] = value
    if statement.predicate.approval is not None:
        for key, value in statement.predicate.approval.commitments.items():
            commitments[f"approval.{key}"] = value
    disclosed: dict[str, str] = {}
    private = False
    if card_openings is not None:
        result = _check_openings(card_openings, commitments, allowed)
        if isinstance(result, ReceiptVerification):
            return result
        disclosed = result
    elif openings is not None:
        if not isinstance(openings, Mapping) or set(openings) != {
            "schema",
            "payload_sha256",
            "openings",
        }:
            return _refuse("malformed", "a private openings file has three fields")
        if openings.get("schema") != OPENINGS_SCHEMA:
            return _refuse("unknown_schema", "not an openadapt.receipt-openings/v1 file")
        if openings.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
            return _refuse("opening_mismatch", "the openings belong to another receipt")
        result = _check_openings(openings.get("openings"), commitments, None)
        if isinstance(result, ReceiptVerification):
            return result
        disclosed = result
        private = True
        for index, c in enumerate(statement.predicate.confirmations):
            reason = disclosed.get(confirmation_path(index, "reason"))
            if reason is not None and reason not in REASONS_FOR_STATE[c.state]:
                return _refuse(
                    "inconsistent", f"a {c.state} confirmation can't have reason {reason}"
                )
    ordered = sorted(
        checks,
        key=lambda c: _ROLE_ORDER.index(c.role) if c.role in _ROLE_ORDER else -1,
    )
    lines: list[str] = []
    for c in ordered:
        if c.line not in lines:
            lines.append(c.line)
    payload_bound: Optional[bool] = None
    if received is not None:
        bound = next((x for x in statement.subject if x.name == "payload"), None)
        if bound is None:
            return _refuse("payload_not_bound", "the receipt names no payload")
        if bound.digest.sha256 != hashlib.sha256(received).hexdigest():
            return _refuse("payload_mismatch", "the bytes received aren't the payload signed")
        payload_bound = True
    confirmations = statement.predicate.confirmations
    return ReceiptVerification(
        True,
        approval_line=approval_line(statement.predicate.approval),
        payload_bound=payload_bound,
        confirmation_lines=tuple(
            confirmation_line(c, disclosed.get(confirmation_path(i, "observed_at")))
            for i, c in enumerate(confirmations)
        ),
        independent=tuple(c.read_method in INDEPENDENT_READS for c in confirmations),
        signers=tuple(ordered),
        signer_lines=tuple(lines),
        demo=demo,
        outcome=statement.predicate.outcome,
        statement=statement.wire(),
        disclosed=disclosed,
        private=private,
    )


def approval_line(a: Optional[Approval]) -> str:
    """The verifier's approval line, as every verifier prints it."""
    if a is None:
        return "No approval recorded"
    if a.mode == "not_gated":
        return "Not gated: no approval was required for these writes"
    who = "a person" if a.approver_kind == "person" else "an agent delegate"
    how = "each item" if a.mode == "each_item" else "items that failed a check"
    return f"Approved by {who}: {how}"


def confirmation_line(c: Confirmation, observed_at: Optional[str] = None) -> str:
    """The verifier's line for one confirmation, as every verifier prints it."""
    at = f" at {observed_at}" if observed_at else ""
    if c.state == "recorded_as_intended":
        return f"Confirmed in the other party's system{at}"
    if c.state == "recorded_differently":
        noun = "field differs" if c.differing_count == 1 else "fields differ"
        return (
            f"Recorded differently in the other party's system{at}: "
            f"{c.differing_count} {noun}"
        )
    if c.state == "acknowledged":
        return f"Acknowledged by the other party{at}"
    return f"Sent to the other party{at}, not confirmed"


def _check_openings(
    openings: Any,
    commitments: Mapping[str, str],
    allowed: Optional[Mapping[str, str]],
) -> dict[str, str] | ReceiptVerification:
    if not isinstance(openings, Mapping):
        return _refuse("malformed", "openings are an object")
    found = find_embedded_content(dict(openings))
    if found:
        return _refuse("embedded_content", found)
    disclosed: dict[str, str] = {}
    for path, opening in openings.items():
        if allowed is not None and path not in allowed:
            return _refuse("disclosure_not_allowed", f"{path!r}")
        if not isinstance(opening, Mapping) or set(opening) != {"salt", "value"}:
            return _refuse("malformed", f"the opening for {path!r}")
        value, salt = opening.get("value"), opening.get("salt")
        problem = _value_problem(str(path), value)
        if problem:
            return _refuse("malformed", problem)
        if not (isinstance(salt, str) and _SALT_HEX.fullmatch(salt)):
            return _refuse("malformed", f"the salt for {path!r}")
        if commitments.get(str(path)) != commitment(str(path), salt, str(value)):
            return _refuse("opening_mismatch", f"{path!r}")
        disclosed[str(path)] = str(value)
    return disclosed


# -- building a statement from a run report ---------------------------------------


def receipt_outcome(run_outcome: Any) -> tuple[str, Optional[str]]:
    """The receipt outcome and reason code for a run's own outcome. Never upgrades."""
    if not isinstance(run_outcome, str) or not run_outcome:
        return "UNKNOWN", "no_outcome"
    outcome = OUTCOME_FROM_RUN.get(run_outcome, "UNKNOWN")
    reason = run_outcome.lower()
    return outcome, reason if re.fullmatch(_REASON[1:-1], reason) else "other"


def program_id(name: Any, version_sha256: str) -> str:
    """A program id that never shows the program's name."""
    basis = name if isinstance(name, str) and name else version_sha256
    return "prog-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CARD_FILE",
    "CARD_SCHEMA",
    "DISCLOSURE_FIELDS",
    "OPENINGS_SCHEMA",
    "PAYLOAD_TYPE",
    "PREDICATE_TYPE",
    "PRIVATE_FILE",
    "PUBLIC_DISCLOSURE",
    "PUBLIC_DISCLOSURE_V1",
    "PUBLIC_DISCLOSURE_VERSION",
    "RECEIPT_FILE",
    "REGISTRY_SCHEMA",
    "ROLES",
    "SIGNER_LINES",
    "STATEMENT_TYPE",
    "UNKNOWN_SIGNER_LINE",
    "ReceiptVerification",
    "RegistryError",
    "RoleKey",
    "RunStatementV1",
    "SignerCheck",
    "add_signature",
    "canonical_json",
    "commitment",
    "Approval",
    "approval_line",
    "Confirmation",
    "confirmation_line",
    "confirmation_path",
    "find_embedded_content",
    "is_receipt_card",
    "is_receipt_envelope",
    "key_id",
    "loads_strict",
    "make_openings",
    "pae",
    "parse_registry",
    "private_openings",
    "program_id",
    "public_card",
    "receipt_outcome",
    "refusal_line",
    "sign_statement",
    "statement_payload",
    "verify_receipt",
]
