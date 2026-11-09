"""The structural patch format: contributed, then re-signed for distribution.

A :class:`StructuralPatchPayload` maps a step's OLD structural anchor (the
DOM selector, frame path, ARIA or UIA role, and native automation id it was
compiled against) to the NEW one after the application changed. It carries
the fingerprints of the application version before and after the change
(:mod:`openadapt_flow.fleet.fingerprint`) and, for each map, which identity
fields the source repair also changed on that step. Never the evidence
itself.

A patch travels in two envelopes:

* a :class:`PatchContribution`, which the source tenant signs with its
  ``contributor`` certificate and sends to the fleet hub only. It adds the
  exact prior and proposed bundle digests of the approved repair, for the
  hub's audit log;
* a :class:`SignedStructuralPatch`, which the hub signs with its
  ``distribution`` certificate after it verified the contribution
  (:mod:`openadapt_flow.fleet.hub`). Receivers get only this. It names no
  source tenant, no tenant key, and no tenant bundle digest, so two patches
  from the same source cannot be linked by any field.

What a payload never carries, by schema: accessible names, OCR text,
identity bands, identity templates or their salts, parameter values, typed
text, effect match values, window titles, value digests, step ids, workflow
names, file paths, or the tenant's application address. Every string field is
a digest, a closed literal, a validated id, or a selector or automation id
that passed :func:`sanitize_selector` / :func:`sanitize_automation_id`.

:func:`find_leaks` is a second, independent check run at export: it refuses
the patch if any value from the source bundles (or the digest of one) appears
anywhere in the serialized payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.fleet.fingerprint import APP_FAMILY_PATTERN, AppVersionFingerprint
from openadapt_flow.fleet.signing import (
    CertificateChain,
    FleetTrust,
    KeyCustody,
    PatchSignatureError,
    sign_document,
    verify_chain,
    verify_document,
)
from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.runtime.healing.patch import IDENTITY_FIELDS

PATCH_SCHEMA: Final[str] = "openadapt.fleet.structural-patch/v2"
PATCH_DOMAIN: Final[bytes] = b"openadapt.fleet.structural-patch/v2\x00"
CONTRIBUTION_DOMAIN: Final[bytes] = b"openadapt.fleet.patch-contribution/v1\x00"
_SHA256 = r"^[a-f0-9]{64}$"
_PATCH_ID_DOMAIN = b"openadapt.fleet.structural-patch-id/v2\x00"


class PatchRedactionError(ValueError):
    """The patch would carry a value that must stay with the tenant."""


# --------------------------------------------------------------------------- #
# Selector and id sanitizers
# --------------------------------------------------------------------------- #

#: Test-hook attributes. Developers set their values for test automation,
#: so a value is allowed unless it looks dynamic
#: (see :func:`is_static_hook_value`). Every other ``data-*`` value is refused.
TEST_HOOK_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"data-testid", "data-test", "data-test-id", "data-cy", "data-qa"}
)
#: Attribute names a selector may test. ``aria-label``, ``title``, ``value``,
#: ``href``, ``src``, ``alt`` and ``placeholder`` are excluded: they carry
#: text or addresses.
SELECTOR_ATTRIBUTES: Final[frozenset[str]] = TEST_HOOK_ATTRIBUTES | frozenset(
    {"name", "type", "role", "for"}
)
_IDENT = r"-?[A-Za-z_][A-Za-z0-9_-]{0,63}"
_SIMPLE = re.compile(
    rf"""
    (?P<tag>[a-z][a-z0-9]{{0,15}})
    | \#(?P<id>{_IDENT})
    | \.(?P<cls>{_IDENT})
    | \[(?P<attr>[a-z][a-z0-9-]{{0,31}})(?:=(?P<q>["']?)(?P<val>{_IDENT})(?P=q))?\]
    | :(?P<pseudo>nth-of-type|nth-child|first-of-type|last-of-type|first-child|last-child)
        (?:\((?P<n>[0-9]{{1,3}})\))?
    | (?P<comb>\s*[>+~]\s*|\s+)
    """,
    re.VERBOSE,
)
_DIGIT_RUN = re.compile(r"[0-9]{4,}")
_UPPER_HEX_OR_UUID = re.compile(r"[0-9a-fA-F]{8,}")
#: A URL, query string, or fragment anywhere in a selector or frame path.
_URL_LIKE = re.compile(r"[?&%]|://|^/|=/")
#: Class and id names a build tool generated, not a developer:
#: CSS modules (``Button_primary__3xK9a``), emotion (``css-1q2w3e``),
#: styled-components (``sc-bdVaJa``), JSS (``jss12``, ``makeStyles-root-12``),
#: Svelte (``svelte-1x2y3z``), and any trailing hash-like segment that mixes
#: letters and digits (``card-a1b2c``).
_GENERATED_NAME = re.compile(
    r"""
    ^(?:css|sc|svelte|emotion|jsx|tw)-[A-Za-z0-9]{3,}$
    | ^jss[0-9]+$
    | ^makeStyles-.*-[0-9]+$
    | ^Mui[A-Za-z]+-[A-Za-z]+-[0-9]+$
    | __[A-Za-z0-9_-]{5,}$
    """,
    re.VERBOSE,
)
_HASH_SEGMENT = re.compile(r"(?:^|[-_])([A-Za-z0-9]{5,12})$")
#: A UUID, with or without hyphens, anywhere in a value.
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
#: A capitalised word (``Smith``, ``Mary``), as a person's name is written.
_CAPITALISED_WORD = re.compile(r"^[A-Z][a-z]+$")


def _token_ok(value: str) -> bool:
    """Developer-assigned names, not record keys.

    A run of four or more digits (``row-00012345``) or a long hex run (a
    UUID fragment) is treated as a possible record identifier and refused.
    """
    return not _DIGIT_RUN.search(value) and not _UPPER_HEX_OR_UUID.search(value)


def looks_name_like(value: str) -> bool:
    """Whether a test-hook value looks like it holds a person's name.

    Test-hook values are written in kebab, snake or camel case
    (``save-note``, ``save_note``, ``saveNote``, ``SaveNote``). A value
    split by ``-`` or ``_`` with any capitalised word in it
    (``patient-Jane-Doe``, ``row_Smith``) is how a name rendered into the
    attribute looks, so it is refused. A single PascalCase word cannot be
    told apart from a developer's name for a control and is allowed.
    """
    segments = [part for part in re.split(r"[-_]+", value) if part]
    if len(segments) < 2:
        return False
    return any(_CAPITALISED_WORD.match(part) for part in segments)


def is_static_hook_value(value: str) -> bool:
    """Whether a test-hook attribute value is static, not data.

    Refused: a run of four or more digits, eight or more hex characters, a
    UUID, or a name-like value (:func:`looks_name_like`).
    """
    return _token_ok(value) and not _UUID.search(value) and not looks_name_like(value)


def is_generated_name(value: str) -> bool:
    """Whether a class or id name looks generated by a CSS build tool.

    Generated suffixes change with every build, so a patch that carries one
    breaks on the next deploy, and the suffix identifies one build of one
    tenant's copy of the application.
    """
    if _GENERATED_NAME.search(value):
        return True
    match = _HASH_SEGMENT.search(value)
    if match is None or match.start(1) == 0:
        return False
    segment = match.group(1)
    return any(c.isdigit() for c in segment) and any(c.isalpha() for c in segment)


def sanitize_selector(selector: str, *, what: str = "selector") -> str:
    """Return ``selector`` if it is purely structural; raise otherwise.

    Accepts tag names, ``#id``, ``.class``, ``[attr]`` and ``[attr=value]``
    for :data:`SELECTOR_ATTRIBUTES`, the positional pseudo-classes, and the
    descendant / child / sibling combinators. Refuses:

    * text engines (``text=``, ``:has-text()``, ``>>``) and quoted free text;
    * any token that looks like a record identifier or a person's name
      (:func:`looks_name_like`);
    * a value on any ``data-*`` attribute other than the test hooks in
      :data:`TEST_HOOK_ATTRIBUTES`, because applications put record keys and
      names in ``data-*`` values; a test-hook value that looks dynamic
      (:func:`is_static_hook_value`);
    * a URL, query string, or fragment, as an iframe or frame selector would
      carry in ``src``;
    * class and id names with a build-generated suffix.
    """
    if not isinstance(selector, str) or not selector.strip() or len(selector) > 256:
        raise PatchRedactionError(f"{what} is empty or too long")
    text = selector.strip()
    if _URL_LIKE.search(text):
        raise PatchRedactionError(f"{what} carries a URL or query string")
    position = 0
    while position < len(text):
        match = _SIMPLE.match(text, position)
        if match is None or match.end() == position:
            raise PatchRedactionError(
                f"{what} uses a construct a structural patch cannot carry"
            )
        attr = match.group("attr")
        if attr is not None and attr not in SELECTOR_ATTRIBUTES:
            raise PatchRedactionError(
                f"{what} tests attribute {attr!r}, which can carry text"
            )
        value = match.group("val")
        if attr is not None and attr.startswith("data-") and value:
            if attr not in TEST_HOOK_ATTRIBUTES:
                raise PatchRedactionError(
                    f"{what} tests the value of {attr!r}; data-* values can "
                    "carry record data"
                )
            if not is_static_hook_value(value):
                raise PatchRedactionError(
                    f"{what} tests a {attr!r} value that looks dynamic or name-like"
                )
        for group in ("id", "cls", "val"):
            value = match.group(group)
            if value is not None and not _token_ok(value):
                raise PatchRedactionError(
                    f"{what} token looks like a record identifier"
                )
            if value is not None and looks_name_like(value):
                raise PatchRedactionError(f"{what} token looks like a person's name")
        for group in ("id", "cls"):
            value = match.group(group)
            if value is not None and is_generated_name(value):
                raise PatchRedactionError(
                    f"{what} uses a build-generated class or id name"
                )
        position = match.end()
    return text


def sanitize_frame_selector(selector: str) -> str:
    """A frame path entry: the same rules as :func:`sanitize_selector`."""
    return sanitize_selector(selector, what="frame path")


_AUTOMATION_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_ -]{0,31}$")


def sanitize_automation_id(value: str) -> str:
    if not _AUTOMATION_ID.match(value) or not _token_ok(value):
        raise PatchRedactionError(
            "automation id is not a developer-assigned identifier"
        )
    return value


def sanitize_role(value: str) -> str:
    role = value.strip().lower()
    if not _ROLE.match(role):
        raise PatchRedactionError("role is not an ARIA or UIA role name")
    return role


# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #


class StructuralAnchorRef(BaseModel):
    """The text-free part of a :class:`~openadapt_flow.ir.StructuralLocator`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: Optional[str] = None
    frame_path: Optional[tuple[str, ...]] = Field(default=None, max_length=8)
    role: Optional[str] = None
    automation_id: Optional[str] = None

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else sanitize_selector(value)

    @field_validator("frame_path")
    @classmethod
    def _frames(cls, value: Optional[tuple[str, ...]]) -> Optional[tuple[str, ...]]:
        return (
            None if value is None else tuple(sanitize_frame_selector(v) for v in value)
        )

    @field_validator("role")
    @classmethod
    def _role(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else sanitize_role(value)

    @field_validator("automation_id")
    @classmethod
    def _automation(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else sanitize_automation_id(value)

    @model_validator(mode="after")
    def _addressable(self) -> "StructuralAnchorRef":
        if not (self.selector or self.automation_id):
            raise ValueError(
                "a structural anchor needs a selector or an automation id; "
                "a role alone is matched by its accessible name, which a "
                "patch never carries"
            )
        return self

    def matches(self, locator: Any) -> bool:
        """Whether a tenant's recorded locator is this anchor.

        Every field set here must equal the locator's. Fields left unset here
        must also be unset there, so a patch never rewrites a locator it
        describes only in part.
        """
        if locator is None:
            return False
        frame = tuple(locator.frame_path) if locator.frame_path else None
        return (
            self.selector == locator.selector
            and self.frame_path == frame
            and self.automation_id == locator.automation_id
            and (self.role is None or self.role == (locator.role or "").lower())
        )


class AnchorMap(BaseModel):
    """One old structural anchor and the anchor that replaces it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    old: StructuralAnchorRef
    new: StructuralAnchorRef
    #: True when the source step kept the same accessible name. When false,
    #: the receiving tenant's recorded name is cleared rather than guessed.
    name_unchanged: bool
    #: Identity fields (see ``IDENTITY_FIELDS``) the source repair changed on
    #: this step. Names only; the evidence stays with the source tenant.
    identity_fields_changed: tuple[str, ...] = ()

    @field_validator("identity_fields_changed")
    @classmethod
    def _identity(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)) or any(
            v not in IDENTITY_FIELDS for v in value
        ):
            raise ValueError("identity fields must be sorted known field names")
        return value

    @property
    def touches_identity(self) -> bool:
        return bool(self.identity_fields_changed)


class StructuralPatchPayload(BaseModel):
    """What receivers apply. Names no source tenant, key, or bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.structural-patch/v2"] = (
        "openadapt.fleet.structural-patch/v2"
    )
    #: A registry workflow id (``availity/eligibility-check``), never the
    #: tenant's own bundle name.
    workflow_family: str = Field(pattern=APP_FAMILY_PATTERN)
    app_before: AppVersionFingerprint
    app_after: AppVersionFingerprint
    maps: tuple[AnchorMap, ...] = Field(min_length=1, max_length=256)
    #: Set by the hub at distribution, truncated to the hour, so the time a
    #: tenant contributed does not identify it.
    created_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _consistent(self) -> "StructuralPatchPayload":
        if self.app_before.app_family != self.app_after.app_family:
            raise ValueError("a patch stays within one application family")
        if self.app_before.fingerprint_sha256() == self.app_after.fingerprint_sha256():
            raise ValueError("the application did not change between fingerprints")
        olds = [item.old for item in self.maps]
        if len(olds) != len(set(olds)):
            raise ValueError("each old anchor is mapped once")
        return self

    @property
    def identity_evidence_changed(self) -> bool:
        return any(item.touches_identity for item in self.maps)

    def free_strings(self) -> list[str]:
        """Every string field that is not a digest, timestamp, or literal.

        These are the only fields that could carry a tenant value, so the
        export leak check compares data values against exactly these.
        """
        out = [self.workflow_family]
        for item in self.maps:
            for ref in (item.old, item.new):
                out.extend(v for v in (ref.selector, ref.role, ref.automation_id) if v)
                out.extend(ref.frame_path or ())
        return out

    def patch_id(self) -> str:
        return hashlib.sha256(_PATCH_ID_DOMAIN + canonical_json(self)).hexdigest()


class SignedStructuralPatch(BaseModel):
    """A patch as receivers get it: signed by the hub's distribution key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: StructuralPatchPayload
    patch_id: str = Field(pattern=_SHA256)
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SignedStructuralPatch":
        return cls.model_validate_json(text)


class ContributionPayload(BaseModel):
    """What a source tenant sends the hub. Never distributed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.patch-contribution/v1"] = (
        "openadapt.fleet.patch-contribution/v1"
    )
    patch: StructuralPatchPayload
    #: The approved repair's exact bundle digests. For the hub's audit log.
    source_prior_bundle_sha256: str = Field(pattern=_SHA256)
    source_bundle_sha256: str = Field(pattern=_SHA256)
    contributed_at: str = Field(min_length=20, max_length=64)


class PatchContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: ContributionPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain

    @property
    def contribution_id(self) -> str:
        return hashlib.sha256(
            CONTRIBUTION_DOMAIN + canonical_json(self.payload)
        ).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)


def _require_signer(signer: KeyCustody, chain: CertificateChain, purpose: str) -> None:
    if signer.public_key_b64() != chain.signer.payload.signer_public_key:
        raise PatchSignatureError("the signing key does not match its certificate")
    if chain.signer.payload.purpose != purpose:
        raise PatchSignatureError(f"this needs a {purpose} certificate")


def sign_contribution(
    payload: ContributionPayload, signer: KeyCustody, chain: CertificateChain
) -> PatchContribution:
    _require_signer(signer, chain, "contributor")
    return PatchContribution(
        payload=payload,
        signature=sign_document(signer, CONTRIBUTION_DOMAIN, payload),
        chain=chain,
    )


def verify_contribution(
    contribution: PatchContribution, trust: FleetTrust, *, now: Any = None
) -> None:
    verify_chain(
        contribution.chain,
        trust,
        purpose="contributor",
        app_family=contribution.payload.patch.app_after.app_family,
        now=now,
    )
    verify_document(
        contribution.chain.signer.payload.signer_public_key,
        CONTRIBUTION_DOMAIN,
        contribution.payload,
        contribution.signature,
        "contribution",
    )


def sign_patch(
    payload: StructuralPatchPayload, signer: KeyCustody, chain: CertificateChain
) -> SignedStructuralPatch:
    """Sign a payload for distribution. Only the hub holds such a key."""
    _require_signer(signer, chain, "distribution")
    return SignedStructuralPatch(
        payload=payload,
        patch_id=payload.patch_id(),
        signature=sign_document(signer, PATCH_DOMAIN, payload),
        chain=chain,
    )


def verify_patch(
    patch: SignedStructuralPatch,
    trust: FleetTrust,
    *,
    now: Any = None,
) -> None:
    """Verify root -> intermediate -> distribution signer -> patch. Raises."""
    verify_chain(
        patch.chain,
        trust,
        purpose="distribution",
        app_family=patch.payload.app_after.app_family,
        now=now,
    )
    if patch.patch_id != patch.payload.patch_id():
        raise PatchSignatureError("patch id does not match its payload")
    verify_document(
        patch.chain.signer.payload.signer_public_key,
        PATCH_DOMAIN,
        patch.payload,
        patch.signature,
        "patch",
    )


# --------------------------------------------------------------------------- #
# Leak check
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    # Split camelCase first, so ``rowJaneDoe`` reads as ``row jane doe``.
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return " ".join(re.sub(r"[^0-9a-z]+", " ", split.lower()).split())


def find_leaks(
    serialized: str, values: Iterable[str], *, digests: Iterable[str] = ()
) -> list[str]:
    """Return a redacted description of each forbidden value found.

    ``values`` are compared after splitting camelCase, lowercasing and
    collapsing punctuation, as whole word runs, so ``Jane Q. Sample`` is found
    in ``jane-q-sample`` and ``rowJaneQSample``. A value of two or more words
    is also looked for with the spaces removed, anywhere, so it is found in
    ``rowjaneqsample``.
    ``digests`` are compared verbatim. Values shorter than three characters
    after normalization are skipped (they would match everywhere).
    """
    haystack = f" {_normalize(serialized)} "
    compact_haystack = haystack.replace(" ", "")
    raw = serialized.lower()
    found: list[str] = []
    for value in values:
        needle = _normalize(value)
        if len(needle.replace(" ", "")) < 3:
            continue
        compact = needle.replace(" ", "")
        if f" {needle} " in haystack or (
            " " in needle and len(compact) >= 6 and compact in compact_haystack
        ):
            found.append(f"value of length {len(value)}")
    for digest in digests:
        if digest and digest.lower() in raw:
            found.append(f"digest {digest[:8]}...")
    return found


__all__ = [
    "AnchorMap",
    "ContributionPayload",
    "PATCH_SCHEMA",
    "PatchContribution",
    "PatchRedactionError",
    "SELECTOR_ATTRIBUTES",
    "SignedStructuralPatch",
    "StructuralAnchorRef",
    "StructuralPatchPayload",
    "find_leaks",
    "TEST_HOOK_ATTRIBUTES",
    "is_generated_name",
    "is_static_hook_value",
    "looks_name_like",
    "sanitize_automation_id",
    "sanitize_frame_selector",
    "sanitize_role",
    "sanitize_selector",
    "sign_contribution",
    "sign_patch",
    "verify_contribution",
    "verify_patch",
]
