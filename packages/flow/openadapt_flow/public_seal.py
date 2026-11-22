"""Public Seals: a short, signed summary of one run that is safe to post.

An offline Seal (:mod:`openadapt_flow.offline_seal`) signs a run's whole
``report.json``. That report stays on this computer: it holds the run's
inputs, step intents, what the identity check read on screen and why a run
stopped. A public Seal (``public-seal.json``) is what can leave:

* ``card`` (``openadapt.public-card/v1``): counts, outcomes, the step strip
  (each step's ok, resolution rung and strongest check), digests, the
  program name (only when asked for, or for the synthetic tutorial), and the
  hour the run started. Every field is copied
  from the report through an explicit allow-list and checked against a closed
  pattern. Nothing else is copied: no inputs, typed values, intents, URLs,
  screenshots, halt text or identity readings.
* ``seal``: an ``openadapt.local-seal/v1`` Seal with ``subject_kind``
  ``public-card`` over the SHA-256 of the card's canonical JSON, so every
  count shown is signed. Its ``run_id`` is a hash of the key and the private
  run id, never the run id or folder name itself.

``card.report_sha256`` is the private Seal's subject, so the person who holds
the report can show that the card belongs to it without revealing it.

The whole file is at most :data:`MAX_BYTES` bytes of canonical JSON. When
the step strip would push it over, the strip is left empty and only the
counts remain; it is never cut short.

A public Seal shows that the key in it signed this card. It does not show who
holds the key, and it does not show that anyone but the key holder watched
the run. :func:`verify_public_seal` checks the card digest, that the key id
names the public key, and the signature. It pins no key registry: binding a
key to a person (for example through ``github.com/<login>.keys``) is a
separate check.

Nothing here touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Mapping, Optional

from openadapt_types.seal import canonical_json, seal_key_id
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from openadapt_flow.offline_seal import (
    SEAL_FILE,
    LocalSealError,
    LocalSealV1,
    loads_strict,
    sign_subject,
    signing_payload,
)

if TYPE_CHECKING:
    from openadapt_flow.seal_keys import SealSigner

PUBLIC_CARD_SCHEMA: Final = "openadapt.public-card/v1"
PUBLIC_SEAL_SCHEMA: Final = "openadapt.public-seal/v1"
PUBLIC_SEAL_FILE: Final = "public-seal.json"
#: Largest public Seal, in bytes of canonical JSON (what is written and sent).
MAX_BYTES: Final = 4096
MAX_STEPS: Final = 64
#: Largest count a card holds: the largest integer a JavaScript number holds
#: exactly, so Python and a TypeScript verifier agree on every count.
MAX_COUNT: Final = 2**53 - 1

_SHA256_HEX = r"^[a-f0-9]{64}$"
_OUTCOME = r"^[A-Z][A-Z_]{0,31}$"
_NAME_PATTERN = r"^[A-Za-z0-9 ._-]{1,48}$"
_NAME = re.compile(_NAME_PATTERN[1:-1])
_LOGIN_PATTERN = r"^[A-Za-z0-9-]{1,39}$"
_LOGIN = re.compile(_LOGIN_PATTERN[1:-1])
# ``[0-9]``, not ``\d``: pydantic's ``\d`` also matches non-ASCII digits and a
# TypeScript verifier's does not, so the two would disagree.
_SEMVER = r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}$"
_HOUR = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z$"
_PUBLIC_RUN_ID = re.compile(r"p-[a-f0-9]{16}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_OUTCOME_RE = re.compile(r"[A-Z][A-Z_]{0,31}")

Count = Annotated[StrictInt, Field(ge=0, le=MAX_COUNT)]

#: How each step's target was found. ``none``: no resolution was recorded
#: (the step was skipped, or stopped before it found its target).
StepRung = Literal[
    "structural",
    "template",
    "template_global",
    "geometry",
    "ocr",
    "grounder",
    "api",
    "none",
]
#: The strongest check that passed on a step: its effect was read back, its
#: postconditions held, or its target's identity matched. ``none`` otherwise.
StepEvidence = Literal["effect", "postcondition", "identity", "none"]
Provenance = Literal["synthetic-tutorial", "production"]
NetworkObservation = Literal["none", "observed", "unknown"]

_RUNGS: frozenset[str] = frozenset(StepRung.__args__)  # type: ignore[attr-defined]

PublicSealRefusal = Literal[
    "too_large",
    "malformed",
    "not_public_card",
    "card_mismatch",
    "key_id_mismatch",
    "bad_signature",
]


class PublicSealError(LocalSealError):
    """A closed reason a public Seal was not built."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PublicStep(_Closed):
    ok: StrictBool
    rung: StepRung
    evidence_class: StepEvidence


class CheckCount(_Closed):
    required: Count
    confirmed: Count


class PublicChecks(_Closed):
    identity: CheckCount
    postconditions: CheckCount
    effects: CheckCount
    authorization: CheckCount


class IdentityClaim(_Closed):
    kind: Literal["github"]
    login: StrictStr = Field(pattern=_LOGIN_PATTERN)


class PublicCardV1(_Closed):
    """What a public Seal shows. Closed: an unknown field is malformed."""

    schema_: Literal["openadapt.public-card/v1"] = Field(
        default=PUBLIC_CARD_SCHEMA, alias="schema"
    )
    outcome: StrictStr = Field(pattern=_OUTCOME)
    report_sha256: StrictStr = Field(pattern=_SHA256_HEX)
    bundle_digest: StrictStr = Field(pattern=_SHA256_HEX)
    steps: list[PublicStep] = Field(max_length=MAX_STEPS)
    steps_total: Count
    steps_ok: Count
    heals: Count
    model_calls: Count
    duration_ms: Count
    external_network_calls: NetworkObservation
    checks: PublicChecks
    provenance: Provenance
    flow_version: StrictStr = Field(pattern=_SEMVER)
    program_name: Optional[StrictStr] = Field(pattern=_NAME_PATTERN)
    generated_at: StrictStr = Field(pattern=_HOUR)
    identity_claim: Optional[IdentityClaim] = None

    @model_validator(mode="after")
    def _consistent(self) -> "PublicCardV1":
        if self.steps_ok > self.steps_total:
            raise ValueError("steps_ok is more than steps_total")
        if self.steps:
            # A strip is the whole run or nothing: never a chosen part of it.
            if len(self.steps) != self.steps_total:
                raise ValueError("steps lists some of the run's steps, not all")
            if sum(1 for step in self.steps if step.ok) != self.steps_ok:
                raise ValueError("steps_ok disagrees with the step strip")
        return self

    def wire(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", by_alias=True)
        if out.get("identity_claim") is None:
            out.pop("identity_claim", None)
        return out


class PublicSealV1(_Closed):
    """``public-seal.json``: the card and an offline Seal over it."""

    card: PublicCardV1
    seal: LocalSealV1

    @model_validator(mode="after")
    def _seal_names_the_card(self) -> "PublicSealV1":
        if self.seal.subject_kind != "public-card":
            raise ValueError("the Seal is not over a public card")
        if not _PUBLIC_RUN_ID.fullmatch(self.seal.run_id):
            raise ValueError("a public Seal's run_id is a hash, p-<16 hex>")
        if self.seal.outcome != self.card.outcome:
            raise ValueError("the Seal and the card state different outcomes")
        return self

    def wire(self) -> dict[str, Any]:
        return {"card": self.card.wire(), "seal": self.seal.wire()}


@dataclass(frozen=True)
class PublicSealVerification:
    ok: bool
    reason: Optional[PublicSealRefusal] = None
    key_id: Optional[str] = None
    detail: str = ""


def card_sha256(card: Mapping[str, Any]) -> str:
    """SHA-256 of the card's canonical JSON: the public Seal's subject."""
    return hashlib.sha256(canonical_json(dict(card)).encode("utf-8")).hexdigest()


def public_run_id(key_id: str, run_id: str) -> str:
    """``p-`` and 16 hex of unsalted SHA-256(key_id + run_id).

    This identifier is deterministic: repeated inputs produce linkable IDs.
    When key_id is known, guesses of run_id can be checked against the ID.
    """
    digest = hashlib.sha256((key_id + run_id).encode("utf-8")).hexdigest()
    return "p-" + digest[:16]


def public_seal_bytes(doc: Mapping[str, Any]) -> bytes:
    """The exact bytes written to ``public-seal.json`` and uploaded."""
    return canonical_json(dict(doc)).encode("utf-8")


def name_ok(value: str) -> bool:
    return bool(_NAME.fullmatch(value))


def login_ok(value: str) -> bool:
    return bool(_LOGIN.fullmatch(value))


# -- reading the run --------------------------------------------------------


def _verified_private_seal(run_dir: Path) -> tuple[LocalSealV1, dict[str, Any]]:
    """The run's own Seal and the report it covers, both checked.

    The Seal must cover the exact bytes of ``report.json`` (or
    ``report.pre-seal.json`` after a Cloud receipt was attached), and its
    signature must verify under the key it names.
    """
    try:
        raw_seal = loads_strict((run_dir / SEAL_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PublicSealError(
            "unsealed", f"{run_dir} has no {SEAL_FILE}; seal the run first"
        ) from exc
    except (OSError, ValueError) as exc:
        raise PublicSealError("seal_unreadable", str(run_dir / SEAL_FILE)) from exc
    try:
        seal = LocalSealV1.model_validate(raw_seal)
    except Exception as exc:
        raise PublicSealError("seal_unreadable", str(run_dir / SEAL_FILE)) from exc
    if seal.subject_kind != "run-report":
        raise PublicSealError("seal_unreadable", "seal.json is not over a run report")
    if seal_key_id(base64.b64decode(seal.public_key)) != seal.key_id:
        raise PublicSealError("seal_unreadable", "seal.json names the wrong key id")
    _check_signature(seal)
    for name in ("report.json", "report.pre-seal.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != seal.subject_sha256:
            continue
        try:
            report = loads_strict(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicSealError("report_unreadable", str(path)) from exc
        if not isinstance(report, dict):
            raise PublicSealError("report_unreadable", f"{name} is not an object")
        return seal, report
    raise PublicSealError(
        "subject_mismatch", "seal.json does not cover this run's report.json"
    )


def _check_signature(seal: LocalSealV1) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(seal.public_key)).verify(
            base64.b64decode(seal.signature), signing_payload(seal.wire())
        )
    except (InvalidSignature, ValueError) as exc:
        raise PublicSealError("bad_signature", "seal.json's signature") from exc


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return min(MAX_COUNT, max(0, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _step(result: Mapping[str, Any]) -> dict[str, Any]:
    resolution = result.get("resolution")
    rung = resolution.get("rung") if isinstance(resolution, Mapping) else None
    if rung is None and result.get("actuation") == "api":
        rung = "api"
    identity = result.get("identity")
    if result.get("effect_verified") is True:
        evidence = "effect"
    elif result.get("postconditions_ok") is True:
        evidence = "postcondition"
    elif isinstance(identity, Mapping) and identity.get("status") == "verified":
        evidence = "identity"
    else:
        evidence = "none"
    return {
        "ok": result.get("ok") is True,
        "rung": rung if isinstance(rung, str) and rung in _RUNGS else "none",
        "evidence_class": evidence,
    }


def _checks(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    envelope = report.get("outcome_envelope")
    envelope = envelope if isinstance(envelope, Mapping) else {}
    required = envelope.get("required_contracts")
    passed = envelope.get("passed_contracts")
    required = required if isinstance(required, Mapping) else {}
    passed = passed if isinstance(passed, Mapping) else {}
    names = {
        "identity": "identity",
        "postconditions": "postcondition",
        "effects": "effect",
        "authorization": "authorization",
    }
    return {
        shown: {
            "required": _int(required.get(source)),
            "confirmed": _int(passed.get(source)),
        }
        for shown, source in names.items()
    }


def _hour(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def _name(value: Any, what: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not name_ok(value):
        raise PublicSealError(
            "name_not_allowed",
            f"the {what} name is not 1-48 letters, digits, spaces, dots, dashes "
            "or underscores; publish without --names to leave it out",
        )
    return value


def _provenance(run_dir: Path) -> Provenance:
    """``synthetic-tutorial`` only when the run's own receipt says so."""
    from openadapt_flow.receipt import read_run_summary

    receipt = read_run_summary(run_dir)
    if isinstance(receipt, dict) and receipt.get("provenance") == "synthetic-tutorial":
        return "synthetic-tutorial"
    return "production"


def build_card(
    run_dir: Path,
    *,
    names: Optional[bool] = None,
    github: Optional[str] = None,
    provenance: Optional[Provenance] = None,
    flow_version: Optional[str] = None,
) -> tuple[dict[str, Any], LocalSealV1]:
    """The public card for a sealed run, and the run's own Seal.

    A pure function of the run's files and the flags, so rebuilding gives
    the same card. Works for stopped runs: it reads ``report.json``, not
    ``receipt.json``.

    ``names``: whether ``program_name`` is filled in. A program name can be a
    person's name, so by default (None) only the synthetic tutorial's runs
    carry it; a real run carries it only with ``names=True`` (``--names``).
    """
    from openadapt_flow import __version__

    run_dir = Path(run_dir)
    private, report = _verified_private_seal(run_dir)
    bundle_digest = report.get("bundle_content_digest")
    if not (isinstance(bundle_digest, str) and _SHA256.fullmatch(bundle_digest)):
        raise PublicSealError("no_bundle_digest", "the report names no bundle digest")
    if github is not None and not login_ok(github):
        raise PublicSealError(
            "bad_login", "a GitHub login is 1-39 letters, digits or dashes"
        )
    outcome = report.get("execution_outcome")
    if not (isinstance(outcome, str) and _OUTCOME_RE.fullmatch(outcome)):
        outcome = "UNKNOWN"
    results = report.get("results")
    results = (
        [r for r in results if isinstance(r, Mapping)]
        if isinstance(results, list)
        else []
    )
    steps = [_step(result) for result in results]
    shown_provenance: Provenance = provenance or _provenance(run_dir)
    if names is None:
        names = shown_provenance == "synthetic-tutorial"
    program = _name(report.get("workflow_name"), "program") if names else None
    card: dict[str, Any] = {
        "schema": PUBLIC_CARD_SCHEMA,
        "outcome": outcome,
        "report_sha256": private.subject_sha256,
        "bundle_digest": bundle_digest,
        "steps": steps if len(steps) <= MAX_STEPS else [],
        "steps_total": len(steps),
        "steps_ok": sum(1 for step in steps if step["ok"]),
        "heals": _int(report.get("heal_count")),
        "model_calls": _int(report.get("model_calls")),
        "duration_ms": _int(report.get("total_ms")),
        "external_network_calls": (
            report.get("external_network_calls")
            if report.get("external_network_calls") in ("none", "observed")
            else "unknown"
        ),
        "checks": _checks(report),
        "provenance": shown_provenance,
        "flow_version": flow_version or __version__,
        "program_name": program,
        "generated_at": _hour(report.get("started_at"))
        or _hour(private.issued_at)
        or "1970-01-01T00:00:00Z",
    }
    if github is not None:
        card["identity_claim"] = {"kind": "github", "login": github}
    try:
        checked = PublicCardV1.model_validate(card)
    except ValidationError as exc:
        # For example a development Flow version that is not plain semver.
        raise PublicSealError("card_invalid", str(exc).splitlines()[0]) from exc
    return checked.wire(), private


# -- signing ------------------------------------------------------------------


def _existing(run_dir: Path, digest: str, key_id: str) -> Optional[dict[str, Any]]:
    """The public Seal already on disk for this exact card and key, if any.

    Reusing it keeps ``issued_at`` (and so the Seal's digest) the same across
    rebuilds, so publishing the same card twice is recognised as a repeat.
    """
    path = run_dir / PUBLIC_SEAL_FILE
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    result = verify_public_seal(raw)
    if not result.ok or result.key_id != key_id:
        return None
    doc = loads_strict(raw)
    if card_sha256(doc["card"]) != digest:
        return None
    return doc


def build_public_seal(
    run_dir: Path,
    *,
    signer: "SealSigner",
    names: Optional[bool] = None,
    github: Optional[str] = None,
    provenance: Optional[Provenance] = None,
    now: Optional[datetime] = None,
    flow_version: Optional[str] = None,
) -> dict[str, Any]:
    """Build (or reuse) the run's public Seal, signed by ``signer``.

    ``signer`` must be the key that sealed the run: a card signed by another
    key would claim a report that key never signed. Raises
    :class:`PublicSealError` with a closed code when it cannot be built.
    """
    run_dir = Path(run_dir)
    card, private = build_card(
        run_dir,
        names=names,
        github=github,
        provenance=provenance,
        flow_version=flow_version,
    )
    if signer.key_id != private.key_id:
        raise PublicSealError(
            "key_mismatch",
            f"the run was sealed by {private.key_id}, not this key ({signer.key_id})",
        )
    # The full strip when it fits; otherwise the counts alone, never part of it.
    attempts = [card, {**card, "steps": []}] if card["steps"] else [card]
    for attempt in attempts:
        digest = card_sha256(attempt)
        doc = _existing(run_dir, digest, signer.key_id)
        if doc is None:
            seal = sign_subject(
                "public-card",
                digest,
                public_run_id(private.key_id, private.run_id),
                outcome=attempt["outcome"],
                now=now,
                signer=signer,
                authorization_sha256=private.authorization_sha256,
                scope_item=private.scope_item,
            )
            doc = PublicSealV1.model_validate(
                {"card": attempt, "seal": seal.wire()}
            ).wire()
        if len(public_seal_bytes(doc)) <= MAX_BYTES:
            return doc
    raise PublicSealError("too_large", f"the public Seal is over {MAX_BYTES} bytes")


def write_public_seal(run_dir: Path, doc: Mapping[str, Any]) -> Path:
    """Write ``RUN_DIR/public-seal.json`` as the exact bytes that are uploaded."""
    path = Path(run_dir) / PUBLIC_SEAL_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(public_seal_bytes(doc))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


# -- checking -----------------------------------------------------------------


def _canonical_b64(value: str) -> bool:
    """True when ``value`` is the one standard base64 spelling of its bytes.

    Python decodes ``AB==`` and ``AA==`` to the same byte; a second spelling
    of the same signature would give the same Seal a second digest.
    """
    try:
        return (
            base64.b64encode(base64.b64decode(value, validate=True)).decode() == value
        )
    except ValueError:
        return False


def verify_public_seal(data: bytes | str | Mapping[str, Any]) -> PublicSealVerification:
    """Check a public Seal on its own: size, shape, card digest, key id, signature.

    Fail closed. A duplicate JSON key, an unknown field at any level, a
    pattern miss, or bytes that are not the document's canonical JSON is
    ``malformed``. This proves the key in the Seal signed this card; it says
    nothing about who holds the key.

    Cloud's TypeScript verifier (openadapt-cloud ``src/lib/publicSeal.ts``)
    makes the same checks in the same order, so both give the same answer for
    every input; ``tests/fixtures/public_seal/golden_vectors.json`` pins that.
    """
    if isinstance(data, Mapping):
        raw = public_seal_bytes(data)
        parsed: Any = dict(data)
    else:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(raw) > MAX_BYTES:
            return PublicSealVerification(False, "too_large")
        try:
            parsed = loads_strict(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return PublicSealVerification(False, "malformed", detail=str(exc))
    if len(raw) > MAX_BYTES:
        return PublicSealVerification(False, "too_large")
    if not isinstance(parsed, dict) or set(parsed) != {"card", "seal"}:
        return PublicSealVerification(False, "malformed", detail="not {card, seal}")
    # The exact bytes the engine writes, and nothing else: no other key
    # order, spacing, escape, BOM or spelling of a number (``-0``). Two
    # parsers can then only disagree about the values, not the text.
    try:
        canonical = public_seal_bytes(parsed) == raw
    except (TypeError, ValueError):  # a lone surrogate cannot be UTF-8
        canonical = False
    if not canonical:
        return PublicSealVerification(False, "malformed", detail="not canonical JSON")
    seal_raw = parsed.get("seal")
    if isinstance(seal_raw, dict) and seal_raw.get("subject_kind") != "public-card":
        return PublicSealVerification(False, "not_public_card")
    try:
        doc = PublicSealV1.model_validate(parsed)
    except Exception as exc:
        return PublicSealVerification(False, "malformed", detail=str(exc)[:200])
    # Defaults are for building, not reading: a received card or Seal must
    # spell out every field it signs, and leave out (not null) what it lacks.
    card_in, seal_in = parsed["card"], parsed["seal"]
    if "schema" not in card_in or not {"schema", "issuer", "algorithm"} <= set(seal_in):
        return PublicSealVerification(False, "malformed", detail="missing field")
    if "identity_claim" in card_in and card_in["identity_claim"] is None:
        return PublicSealVerification(
            False, "malformed", detail="identity_claim is null; leave it out"
        )
    if any(value is None for value in seal_in.values()):
        return PublicSealVerification(
            False, "malformed", detail="a Seal field is null; leave it out"
        )
    if not (_canonical_b64(doc.seal.public_key) and _canonical_b64(doc.seal.signature)):
        return PublicSealVerification(False, "malformed", detail="base64 spelling")
    # Hash the card as it arrived, not as re-serialized, so nothing is dropped.
    if card_sha256(parsed["card"]) != doc.seal.subject_sha256:
        return PublicSealVerification(False, "card_mismatch")
    key_raw = base64.b64decode(doc.seal.public_key, validate=True)
    if len(key_raw) != 32 or seal_key_id(key_raw) != doc.seal.key_id:
        return PublicSealVerification(False, "key_id_mismatch")
    try:
        _check_signature(doc.seal)
    except PublicSealError:
        return PublicSealVerification(False, "bad_signature", key_id=doc.seal.key_id)
    return PublicSealVerification(True, key_id=doc.seal.key_id)


__all__ = [
    "MAX_BYTES",
    "MAX_COUNT",
    "MAX_STEPS",
    "PUBLIC_CARD_SCHEMA",
    "PUBLIC_SEAL_FILE",
    "PUBLIC_SEAL_SCHEMA",
    "PublicCardV1",
    "PublicSealError",
    "PublicSealV1",
    "PublicSealVerification",
    "build_card",
    "build_public_seal",
    "card_sha256",
    "login_ok",
    "name_ok",
    "public_run_id",
    "public_seal_bytes",
    "verify_public_seal",
    "write_public_seal",
]
