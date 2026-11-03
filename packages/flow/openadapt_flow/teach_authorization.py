"""Teach authorizations: a person signs what a teaching agent may do.

A teaching or repairing agent reads untrusted screen content, and what it
does can become a reusable program. :class:`~openadapt_flow.teach_scope.TeachScope`
limits its actions, but a scope the agent (or its harness) writes is only as
trustworthy as the agent. A teach authorization
(``openadapt.teach-authorization/v1``) moves the scope to a person: a short
JSON statement that names

* the **task** (``task_id`` and a one-line summary);
* the **inputs** the agent may type, each as a value or as the SHA-256 of
  the value the operator supplies at teach time (so the statement need not
  hold record data);
* the app **origins** the browser may reach;
* the permitted **writes**: the record kind, the identity rule for the one
  intended record (fields that must match exactly), the fields the save may
  write and their values, how many rows, and the app's write adapter;
* the **forbidden** actions (a closed list) and controls (by label);
* what it covers (``teach``, ``repair`` or both) and when it expires.

**Who signs.** A person on the signed approver roster
(:mod:`openadapt_flow.approval_mode.approver_roster`), with the same
approver key that answers saves. The signature is a
:class:`~openadapt_flow.approval_mode.human_attestation.HumanAttestation`
over :data:`DOMAIN` and the canonical statement, kept beside it in
``teach-authorization.json.sig``. Verification is the approver verifier's
own (:func:`~openadapt_flow.approval_mode.human_attestation.refusal`): the
signer must be on the roster the caller's pinned approver root vouches for,
and the roster must extend every head recorded earlier. So a key that was
never added, or was removed since (revoked), refuses, and so does a key
that holds another role or a principal that names an agent or model.

Keys: a person's encrypted approver key file (``openadapt-flow jit keygen``,
added to the roster with ``openadapt-flow jit approver add``), signed at a
terminal with ``openadapt-flow teach-authorization sign``. Desktop's Touch ID
key answers only item approvals, so it doesn't sign these. The attestation's
``method`` label is not itself signed; what keeps another approval's
signature from counting here is the separate signing domain (:data:`DOMAIN`).

Each authoring path that can be bound builds its scope with :func:`bind`
from a verified authorization, never from the agent's own description; the
paths that can't be bound are refused (docs/TEACH_AUTHORIZATION.md). A
draft records the authorization's digest (:func:`record`) and keeps a copy
of the statement; admission verifies that copy again against the roster as
it is then (:func:`check_admission`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

SCHEMA: Final = "openadapt.teach-authorization/v1"
#: What an approver key signs for a teach authorization: this domain, then
#: the canonical statement (the approver verifier's message format).
DOMAIN: Final = b"openadapt.teach-authorization/v1\x00"
STATEMENT_FILE: Final = "teach-authorization.json"
SIGNATURE_FILE: Final = STATEMENT_FILE + ".sig"
#: The draft and bundle key that records the authorization.
RECORD_KEY: Final = "teach_authorization"
#: Where a draft, a bundle or a taught tool keeps its copy of the statement.
KEPT_DIR: Final = "teach-authorization"
#: The approver signing methods a teach authorization accepts.
SIGNING_METHODS: Final = ("desktop_key", "person_key")
#: Domain prefix for a value digest.
_VALUE_DOMAIN: Final = b"openadapt.teach-value/v1\0"

_HEX64 = r"^[a-f0-9]{64}$"
_NAME = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
_TASK_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_PRINCIPAL = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_ISO_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_ADAPTER = r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"

#: Actions an authorization can forbid. Paste is always refused by the guard.
FORBIDDEN_ACTIONS: Final = (
    "paste",
    "drag",
    "right_click",
    "delete_request",
    "new_window",
    "download",
    "file_upload",
)
ForbiddenAction = Literal[
    "paste",
    "drag",
    "right_click",
    "delete_request",
    "new_window",
    "download",
    "file_upload",
]
Purpose = Literal["teach", "repair"]

#: Closed refusal codes. None of them carries a value.
TeachAuthorizationRefusal = Literal[
    "teach_auth_missing",
    "teach_auth_malformed",
    "teach_auth_unsigned",
    "teach_auth_bad_signature",
    "teach_auth_signer_mismatch",
    "teach_auth_unknown_signer",
    "teach_auth_identity_unverified",
    "teach_auth_method",
    "teach_auth_agent_signer",
    "teach_auth_expired",
    "teach_auth_not_yet_valid",
    "teach_auth_purpose",
    "teach_auth_task_mismatch",
    "teach_auth_input_missing",
    "teach_auth_input_mismatch",
    "teach_auth_input_not_declared",
    "teach_auth_scope_widened",
    "teach_auth_adapter_unloadable",
    "teach_auth_not_recorded",
    "teach_auth_digest_mismatch",
]


class TeachAuthorizationError(RuntimeError):
    """A closed ``code`` and a detail without record values."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def value_sha256(value: str) -> str:
    """The digest an authorization names for a value it does not spell out."""

    return hashlib.sha256(_VALUE_DOMAIN + str(value).encode("utf-8")).hexdigest()


# -- the statement --------------------------------------------------------------


class Bound(BaseModel):
    """One permitted value: spelled out, by digest, or a declared input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Optional[StrictStr] = Field(default=None, min_length=1, max_length=4000)
    sha256: Optional[StrictStr] = Field(default=None, pattern=_HEX64)
    input: Optional[StrictStr] = Field(default=None, pattern=_NAME)

    @model_validator(mode="after")
    def _one(self) -> "Bound":
        given = [x for x in (self.value, self.sha256, self.input) if x is not None]
        if len(given) != 1:
            raise ValueError("a bound value is exactly one of value, sha256, input")
        return self


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: StrictStr = Field(pattern=_TASK_ID)
    summary: StrictStr = Field(min_length=1, max_length=500)


class Writes(BaseModel):
    """The one write the task may make."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: StrictStr = Field(pattern=_NAME)
    #: The identity rule: these fields of the written row must equal these
    #: values (after whitespace and case normalization), and nothing else is
    #: the intended record.
    identity: dict[StrictStr, Bound] = Field(min_length=1, max_length=16)
    #: The fields the save may write and the value each must have.
    fields: dict[StrictStr, Bound] = Field(default_factory=dict, max_length=64)
    expected_rows: StrictInt = Field(default=1, ge=1, le=100)
    write_adapter: Optional[StrictStr] = Field(default=None, pattern=_ADAPTER)
    allowed_write_paths: list[StrictStr] = Field(default_factory=list, max_length=32)

    @field_validator("identity", "fields")
    @classmethod
    def _names(cls, value: dict[str, Bound]) -> dict[str, Bound]:
        for name in value:
            if not re.match(_NAME, name):
                raise ValueError(f"not a field name: {name!r}")
        return value

    @field_validator("allowed_write_paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if not path.startswith("/") or "?" in path or "#" in path:
                raise ValueError(f"a write path is an exact URL path: {path!r}")
        return value

    @model_validator(mode="after")
    def _disjoint(self) -> "Writes":
        overlap = set(self.identity) & set(self.fields)
        if overlap:
            raise ValueError("a name is both identity and field: " + ", ".join(overlap))
        return self


class Forbidden(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: list[ForbiddenAction] = Field(default_factory=list)
    #: Labels of controls the agent may never click (exact, case and
    #: whitespace normalized): "Delete encounter", "E-sign".
    controls: list[StrictStr] = Field(default_factory=list, max_length=64)


class TeachAuthorizationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt.teach-authorization/v1"] = Field(
        default=SCHEMA, alias="schema"
    )
    task: Task
    purposes: list[Purpose] = Field(min_length=1, max_length=2)
    origins: list[StrictStr] = Field(min_length=1, max_length=16)
    inputs: dict[StrictStr, Bound] = Field(default_factory=dict, max_length=64)
    #: Other text the agent may type (a test account's user name).
    constants: list[Bound] = Field(default_factory=list, max_length=64)
    writes: Writes
    forbidden: Forbidden = Field(default_factory=Forbidden)
    #: The approver who signs: their principal id on the approver roster.
    authorizer: StrictStr = Field(pattern=_PRINCIPAL)
    #: Their approver public key (64 hex). The signature must be this key's,
    #: and the roster must hold this key for ``authorizer`` when it is checked.
    authorizer_public_key: StrictStr = Field(pattern=_HEX64)
    issued_at: StrictStr = Field(pattern=_ISO_SECONDS)
    expires_at: Optional[StrictStr] = Field(default=None, pattern=_ISO_SECONDS)
    nonce: StrictStr = Field(pattern=_HEX64)

    @field_validator("origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        from openadapt_flow.teach_scope import origin_of

        out = []
        for raw in value:
            origin = origin_of(raw)
            if origin is None or origin != raw.rstrip("/"):
                raise ValueError(f"an origin is scheme://host[:port]: {raw!r}")
            out.append(origin)
        if len(set(out)) != len(out):
            raise ValueError("origins are unique")
        return out

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: dict[str, Bound]) -> dict[str, Bound]:
        for name, bound in value.items():
            if not re.match(_NAME, name):
                raise ValueError(f"not an input name: {name!r}")
            if bound.input is not None:
                raise ValueError("an input is a value or a digest, not another input")
        return value

    @model_validator(mode="after")
    def _references(self) -> "TeachAuthorizationV1":
        for where, bound in [
            *self.writes.identity.items(),
            *self.writes.fields.items(),
            *(("constant", c) for c in self.constants),
        ]:
            if bound.input is not None and bound.input not in self.inputs:
                raise ValueError(f"{where} names undeclared input {bound.input!r}")
        if len(set(self.purposes)) != len(self.purposes):
            raise ValueError("purposes are unique")
        if any(c.sha256 is not None for c in self.constants):
            raise ValueError("a constant is a value or an input, not a digest")
        return self

    def summary_line(self) -> str:
        """What this authorization allows, without values."""

        w = self.writes
        return (
            f"{self.task.task_id}: one {w.record_kind} write "
            f"(identity {', '.join(sorted(w.identity))}; fields "
            f"{', '.join(sorted(w.fields)) or 'none'}) on "
            f"{', '.join(self.origins)}; authorized by {self.authorizer}"
        )


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: Optional[str] = None
    statement: Optional[TeachAuthorizationV1] = None
    sha256: Optional[str] = None  # of the statement's exact bytes
    signature_sha256: Optional[str] = None
    #: The roster principal whose approver key signed, once verified.
    signer: Optional[str] = None
    signer_public_key: Optional[str] = None
    method: Optional[str] = None
    #: The approver root the roster was checked under.
    approver_root: Optional[str] = None
    detail: str = field(default="")


def _paths(target: Path) -> tuple[Path, Path]:
    target = Path(target)
    statement = target / STATEMENT_FILE if target.is_dir() else target
    return statement, statement.with_name(statement.name + ".sig")


def _utc_seconds(when: Optional[datetime]) -> str:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(statement: TeachAuthorizationV1) -> str:
    return (
        json.dumps(
            statement.model_dump(by_alias=True, exclude_none=True),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def create(
    out_dir: Path,
    spec: Mapping[str, Any],
    *,
    authorizer: str,
    authorizer_public_key: str,
    now: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> Path:
    """Write the unsigned statement for ``spec`` in ``out_dir``; return its path.

    ``spec`` holds ``task``, ``purposes``, ``origins``, ``inputs``,
    ``constants``, ``writes`` and ``forbidden`` as in the statement.
    ``authorizer`` is the approver principal who will sign and
    ``authorizer_public_key`` their approver key (64 hex). Refuses over an
    existing statement. ``expires_at`` defaults to 24 hours from now and is
    at most 90 days on.
    """

    path = Path(out_dir) / STATEMENT_FILE
    if path.exists() or path.with_name(SIGNATURE_FILE).exists():
        raise TeachAuthorizationError("teach_auth_exists", f"{path} already exists")
    from datetime import timedelta

    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires_at = expires_at or issued + timedelta(hours=24)
    if not issued < expires_at <= issued + timedelta(days=90):
        raise TeachAuthorizationError(
            "teach_auth_malformed", "expires_at is after now and at most 90 days on"
        )
    statement = TeachAuthorizationV1.model_validate(
        {
            **dict(spec),
            "schema": SCHEMA,
            "authorizer": authorizer,
            "authorizer_public_key": str(authorizer_public_key).lower(),
            "issued_at": _utc_seconds(now),
            "expires_at": _utc_seconds(expires_at),
            "nonce": secrets.token_hex(32),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(statement), encoding="utf-8")
    return path


def _load(statement_path: Path) -> tuple[bytes, dict[str, Any], TeachAuthorizationV1]:
    from openadapt_flow.offline_seal import loads_strict

    try:
        raw = Path(statement_path).read_bytes()
    except FileNotFoundError as exc:
        raise TeachAuthorizationError(
            "teach_auth_missing", str(statement_path)
        ) from exc
    except OSError as exc:
        raise TeachAuthorizationError(
            "teach_auth_malformed", type(exc).__name__
        ) from exc
    try:
        data = loads_strict(raw)
        statement = TeachAuthorizationV1.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - any parse error is malformed
        raise TeachAuthorizationError(
            "teach_auth_malformed", type(exc).__name__
        ) from exc
    if not isinstance(data, dict):
        raise TeachAuthorizationError("teach_auth_malformed", "not an object")
    return raw, data, statement


def statement_task_id(target: Path) -> str:
    """The task a statement names, unverified (for naming the task only)."""

    statement_path, _sig = _paths(Path(target))
    return _load(statement_path)[2].task.task_id


def sign(statement_path: Path, signer: Any) -> Path:
    """Sign the statement with an approver key; return the signature's path.

    ``signer`` is an
    :class:`~openadapt_flow.approval_mode.human_attestation.ApproverSigner`
    (``load_signer`` for a key file, ``stdio_signer`` for Desktop's key),
    which names its principal from the roster. It must be the statement's
    ``authorizer``, sign with ``authorizer_public_key``, and use a method in
    :data:`SIGNING_METHODS`. Writes nothing on any mismatch.
    """

    from openadapt_flow.jit.authority import _verify

    statement_path, sig_path = _paths(Path(statement_path))
    _raw, data, statement = _load(statement_path)
    if sig_path.exists():
        raise TeachAuthorizationError("teach_auth_already_signed", str(sig_path))
    if signer.method not in SIGNING_METHODS:
        raise TeachAuthorizationError(
            "teach_auth_method", f"{signer.method} can't sign a teach authorization"
        )
    if signer.principal_id != statement.authorizer:
        raise TeachAuthorizationError(
            "teach_auth_signer_mismatch",
            f"the statement names {statement.authorizer}; this key is "
            f"{signer.principal_id} on the roster",
        )
    attestation = signer.attest(DOMAIN, data)
    if not _verify(
        statement.authorizer_public_key, DOMAIN, data, attestation.signature
    ):
        raise TeachAuthorizationError(
            "teach_auth_signer_mismatch",
            "the signature isn't by the statement's authorizer_public_key",
        )
    fd = os.open(sig_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(attestation.model_dump_json() + "\n")
    return sig_path


_ROSTER_REFUSALS = {
    "human_answer_unsigned": "teach_auth_unsigned",
    "human_approver_unregistered": "teach_auth_unknown_signer",
    "human_answer_signature_invalid": "teach_auth_bad_signature",
    "attestation_method_mismatch": "teach_auth_method",
}


def verify(
    target: Path,
    *,
    key_dir: Optional[Path],
    root: Optional[str],
    at_least: Any = None,
) -> Verification:
    """Verify a teach authorization against the approver roster.

    ``root`` is the caller's pinned approver root and ``key_dir`` the key
    directory holding the roster; nothing here reads the environment. The
    signer must be on that roster now (so a removed key refuses), must be
    the statement's ``authorizer`` with its ``authorizer_public_key``, and
    must have signed exactly these statement bytes.
    """

    from openadapt_flow.approval_mode.human_attestation import (
        AttestationRefused,
        HumanAttestation,
        approvers,
        refusal,
    )

    statement_path, sig_path = _paths(Path(target))
    try:
        raw, data, statement = _load(statement_path)
    except TeachAuthorizationError as exc:
        return Verification(False, reason=exc.code, detail=str(exc))
    base = Verification(
        False,
        statement=statement,
        sha256=hashlib.sha256(raw).hexdigest(),
        approver_root=root,
    )
    try:
        sig_bytes = sig_path.read_bytes()
    except FileNotFoundError:
        return dataclasses.replace(base, reason="teach_auth_unsigned")
    except OSError:
        return dataclasses.replace(base, reason="teach_auth_malformed")
    try:
        attestation = HumanAttestation.model_validate_json(sig_bytes)
    except ValueError:
        return dataclasses.replace(
            base, reason="teach_auth_malformed", detail="the signature file"
        )
    base = dataclasses.replace(
        base,
        signature_sha256=hashlib.sha256(sig_bytes).hexdigest(),
        method=attestation.method,
    )
    if attestation.method not in SIGNING_METHODS:
        return dataclasses.replace(
            base,
            reason="teach_auth_method",
            detail=f"{attestation.method} can't sign a teach authorization",
        )
    if attestation.signer != statement.authorizer:
        return dataclasses.replace(
            base,
            reason="teach_auth_signer_mismatch",
            detail="the signer isn't the statement's authorizer",
        )
    code = refusal(
        attestation,
        domain=DOMAIN,
        payload=data,
        key_dir=Path(key_dir) if key_dir is not None else None,
        root=root,
        at_least=at_least,
    )
    if code is not None:
        return dataclasses.replace(
            base,
            reason=_ROSTER_REFUSALS.get(code, "teach_auth_identity_unverified"),
            detail=code,
        )
    try:
        public = approvers(Path(key_dir), root, at_least=at_least).get(  # type: ignore[arg-type]
            attestation.signer
        )
    except AttestationRefused as exc:
        return dataclasses.replace(
            base, reason="teach_auth_identity_unverified", detail=exc.code
        )
    if public != statement.authorizer_public_key:
        return dataclasses.replace(
            base,
            reason="teach_auth_signer_mismatch",
            detail="the roster holds another key for the authorizer",
        )
    return dataclasses.replace(
        base, ok=True, signer=attestation.signer, signer_public_key=public
    )


def require(
    target: Path,
    *,
    purpose: Purpose,
    key_dir: Optional[Path],
    root: Optional[str],
    task_id: Optional[str] = None,
    now: Optional[datetime] = None,
    at_least: Any = None,
) -> Verification:
    """A verified authorization fit for ``purpose`` now, or :class:`TeachAuthorizationError`."""

    result = verify(target, key_dir=key_dir, root=root, at_least=at_least)
    if not result.ok or result.statement is None:
        raise TeachAuthorizationError(str(result.reason), result.detail)
    st = result.statement
    stamp = _utc_seconds(now)
    if st.issued_at > stamp:
        raise TeachAuthorizationError("teach_auth_not_yet_valid", st.issued_at)
    if st.expires_at is not None and stamp >= st.expires_at:
        raise TeachAuthorizationError("teach_auth_expired", st.expires_at)
    _fits(result, purpose=purpose, task_id=task_id)
    return result


def _fits(
    result: Verification, *, purpose: Optional[str], task_id: Optional[str]
) -> None:
    st = result.statement
    assert st is not None
    if purpose is not None and purpose not in st.purposes:
        raise TeachAuthorizationError(
            "teach_auth_purpose", f"it covers {', '.join(st.purposes)}, not {purpose}"
        )
    if task_id is not None and task_id != st.task.task_id:
        raise TeachAuthorizationError(
            "teach_auth_task_mismatch",
            f"authorized task {st.task.task_id}, asked for {task_id}",
        )


def pinned_trust(
    key_dir: Optional[Path] = None,
    approver_root: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    config_only: bool = False,
) -> tuple[Path, Optional[str]]:
    """The key directory and approver root a teach path checks against.

    ``key_dir`` defaults to ``$OPENADAPT_JIT_KEY_DIR`` (or
    ``~/.openadapt/jit``). The root is ``approver_root``, else
    ``OPENADAPT_APPROVER_ROOT`` (:func:`approver_roster.pinned_root`); two
    that disagree refuse. ``None`` means unpinned, and verification refuses.
    ``config_only`` (a process an agent runs, such as ``openadapt-agent
    serve``): ``approver_root`` must come from the deployment config, and the
    environment never counts.
    """

    from openadapt_flow.approval_mode import approver_roster as roster
    from openadapt_flow.jit.lifecycle import default_key_dir

    try:
        root = (
            roster.pinned_root(config_root=approver_root, config_only=True)
            if config_only
            else roster.pinned_root(approver_root, env=env)
        )
    except roster.RosterError as exc:
        raise TeachAuthorizationError(
            "teach_auth_identity_unverified", exc.code
        ) from exc
    return (Path(key_dir) if key_dir is not None else default_key_dir()), root


# -- binding to a scope -----------------------------------------------------------


def resolve_inputs(
    statement: TeachAuthorizationV1, supplied: Optional[Mapping[str, Any]]
) -> dict[str, str]:
    """The input values the run may use, checked against the statement.

    A spelled-out input needs no supplied value (a supplied one must equal
    it). A digest-bound input needs a supplied value whose digest matches.
    A supplied name the statement does not declare is refused.
    """

    supplied = {str(k): v for k, v in (supplied or {}).items() if v is not None}
    unknown = sorted(set(supplied) - set(statement.inputs))
    if unknown:
        raise TeachAuthorizationError(
            "teach_auth_input_not_declared", ", ".join(unknown)
        )
    out: dict[str, str] = {}
    for name, bound in statement.inputs.items():
        if bound.value is not None:
            if name in supplied and str(supplied[name]) != bound.value:
                raise TeachAuthorizationError("teach_auth_input_mismatch", name)
            out[name] = bound.value
            continue
        if name not in supplied:
            raise TeachAuthorizationError("teach_auth_input_missing", name)
        if value_sha256(str(supplied[name])) != bound.sha256:
            raise TeachAuthorizationError("teach_auth_input_mismatch", name)
        out[name] = str(supplied[name])
    return out


def _resolve(
    bound: Bound, inputs: Mapping[str, str], supplied: Mapping[str, Any], name: str
) -> str:
    if bound.value is not None:
        return bound.value
    if bound.input is not None:
        return inputs[bound.input]
    value = supplied.get(name)
    if value is None or value_sha256(str(value)) != bound.sha256:
        raise TeachAuthorizationError("teach_auth_input_mismatch", name)
    return str(value)


def bind(
    verification: Verification,
    params: Optional[Mapping[str, Any]] = None,
    *,
    record_values: Optional[Mapping[str, Any]] = None,
    write_adapter: Any = None,
) -> Any:
    """The :class:`~openadapt_flow.teach_scope.TeachScope` a verified authorization allows.

    ``params`` are the run's input values (needed for digest-bound inputs);
    ``record_values`` supply digest-bound identity, field and constant values
    by name. ``write_adapter`` overrides loading the statement's adapter
    reference (tests, an adapter object already built for the same app).
    """

    from openadapt_flow.teach_scope import TeachScope, load_write_adapter

    if not verification.ok or verification.statement is None:
        raise TeachAuthorizationError(verification.reason or "teach_auth_malformed")
    st = verification.statement
    inputs = resolve_inputs(st, params)
    extra = dict(record_values or {})
    identity = {
        name: _resolve(b, inputs, extra, name) for name, b in st.writes.identity.items()
    }
    fields = {
        name: _resolve(b, inputs, extra, name) for name, b in st.writes.fields.items()
    }
    constants = [_resolve(b, inputs, extra, "constant") for b in st.constants]
    used = set(identity.values()) | set(fields.values())
    adapter = write_adapter
    if adapter is None and st.writes.write_adapter:
        try:
            adapter = load_write_adapter(st.writes.write_adapter)
        except Exception as exc:  # import or construction errors
            raise TeachAuthorizationError(
                "teach_auth_adapter_unloadable", st.writes.write_adapter
            ) from exc
    return TeachScope(
        task_id=st.task.task_id,
        origins=tuple(st.origins),
        record=identity,
        fields=fields,
        constants=tuple(
            dict.fromkeys([*(v for v in inputs.values() if v not in used), *constants])
        ),
        expected_rows=st.writes.expected_rows,
        write_adapter=adapter,
        allowed_write_paths=tuple(st.writes.allowed_write_paths),
        forbidden_actions=frozenset(st.forbidden.actions),
        forbidden_controls=tuple(st.forbidden.controls),
        authorization=record(verification),
    )


def check_within(scope: Any, authorized: Any) -> None:
    """Refuse a scope that allows anything the authorized scope does not.

    For paths where a scope is also assembled from flags or a task file: it
    may narrow the authorization (fewer origins, fewer typeable strings), but
    the record and field values must be the authorized ones.
    """

    from openadapt_flow.teach_scope import _same

    problems = []
    if not set(scope.origins) <= set(authorized.origins):
        problems.append("origins")
    if set(scope.record) != set(authorized.record) or any(
        not _same(scope.record[k], authorized.record[k]) for k in scope.record
    ):
        problems.append("record")
    if set(scope.fields) != set(authorized.fields) or any(
        not _same(scope.fields[k], authorized.fields[k]) for k in scope.fields
    ):
        problems.append("fields")
    if not set(scope.typeable) <= set(authorized.typeable):
        problems.append("typeable text")
    if scope.expected_rows != authorized.expected_rows:
        problems.append("expected_rows")
    if not set(scope.allowed_write_paths) <= set(authorized.allowed_write_paths):
        problems.append("write paths")
    if problems:
        raise TeachAuthorizationError(
            "teach_auth_scope_widened",
            "the scope goes beyond the signed authorization: " + ", ".join(problems),
        )


def record(verification: Verification) -> dict[str, Any]:
    """What a draft, candidate and bundle record about the authorization. No values."""

    st = verification.statement
    if st is None or verification.sha256 is None or not verification.ok:
        raise TeachAuthorizationError("teach_auth_malformed")
    return {
        "schema": SCHEMA,
        "sha256": verification.sha256,
        "signature_sha256": verification.signature_sha256,
        "task_id": st.task.task_id,
        "authorizer": st.authorizer,
        "authorizer_public_key": st.authorizer_public_key,
        "method": verification.method,
        "approver_root": verification.approver_root,
        "record_kind": st.writes.record_kind,
        "purposes": list(st.purposes),
        "expires_at": st.expires_at,
    }


def check_admission(
    recorded: Optional[Mapping[str, Any]],
    target: Optional[Path],
    *,
    key_dir: Optional[Path],
    root: Optional[str],
    purpose: Optional[Purpose] = None,
    task_id: Optional[str] = None,
    not_signed_by: Optional[str] = None,
    at_least: Any = None,
) -> Verification:
    """Verify, for admission, the authorization a draft names.

    ``recorded`` is the draft's ``teach_authorization`` entry. ``target`` is
    the statement the draft kept (:func:`copy_into`). The statement's digest
    must equal the recorded one, and its signature must verify against the
    approver roster as it is now: a key removed since teaching refuses.
    ``purpose`` and ``task_id``, when given, must be covered.
    ``not_signed_by`` names a principal that may not be the signer (the
    agent that recorded the draft). Expiry is not checked again: it bounds
    when teaching may happen.
    """

    if not recorded or not isinstance(recorded, Mapping):
        raise TeachAuthorizationError(
            "teach_auth_not_recorded",
            "the draft names no teach authorization; an agent-taught draft needs one",
        )
    if target is None or not Path(target).is_file():
        raise TeachAuthorizationError("teach_auth_missing", "no statement kept with it")
    result = verify(target, key_dir=key_dir, root=root, at_least=at_least)
    if not result.ok:
        raise TeachAuthorizationError(str(result.reason), result.detail)
    if result.sha256 != recorded.get("sha256"):
        raise TeachAuthorizationError(
            "teach_auth_digest_mismatch",
            "the statement kept with it is not the one it recorded",
        )
    if not_signed_by and result.signer == not_signed_by:
        raise TeachAuthorizationError(
            "teach_auth_agent_signer",
            "the recording agent signed its own authorization",
        )
    _fits(result, purpose=purpose, task_id=task_id)
    return result


def copy_into(source: Path, folder: Path) -> Path:
    """Copy the statement and its signature into ``folder``; return the statement path.

    Refuses to replace a different statement already there.
    """

    statement, sig = _paths(Path(source))
    folder = Path(folder)
    out = folder / STATEMENT_FILE
    data, signature = statement.read_bytes(), sig.read_bytes()
    if out.exists():
        if (
            out.read_bytes() != data
            or (folder / SIGNATURE_FILE).read_bytes() != signature
        ):
            raise TeachAuthorizationError(
                "teach_auth_digest_mismatch", f"{folder} holds another authorization"
            )
        return out
    folder.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    (folder / SIGNATURE_FILE).write_bytes(signature)
    return out


def render(verification: Verification) -> str:
    """The plain-text summary a person reads before signing or admitting."""

    from openadapt_flow.authorization_summary import clean

    st = verification.statement
    if st is None:
        return "no statement"

    def show(name: str, b: Bound) -> str:
        if b.value is not None:
            return f"{clean(name)} = {clean(b.value)!r}"
        if b.input is not None:
            return f"{clean(name)} = input {b.input}"
        return f"{clean(name)} = the value with sha256 {b.sha256[:12]}… (supplied at teach time)"  # type: ignore[index]

    w = st.writes
    lines = [
        "You are authorizing an agent to teach (or repair) this task",
        f"  Authorizer   {clean(st.authorizer)} (approver key {st.authorizer_public_key[:16]}…)",
        f"  Task         {clean(st.task.task_id)}: {clean(st.task.summary)}",
        f"  Covers       {', '.join(st.purposes)}",
        f"  Valid        from {st.issued_at}"
        + (f" until {st.expires_at}" if st.expires_at else " (no expiry)"),
        "",
        "The agent may",
        f"  reach only   {', '.join(st.origins)}",
        *(f"  type input   {show(n, b)}" for n, b in st.inputs.items()),
        *(f"  type         {show('constant', b)}" for b in st.constants),
        "",
        f"Exactly {w.expected_rows} {clean(w.record_kind)} write"
        + ("s" if w.expected_rows != 1 else ""),
        *(f"  identity     {show(n, b)}" for n, b in w.identity.items()),
        *(f"  field        {show(n, b)}" for n, b in w.fields.items()),
        f"  checked by   {clean(w.write_adapter or 'the record read-back only')}",
        "",
        "Never",
        "  any other origin, record, field, value or typed text; pasting",
        *(f"  {a.replace('_', ' ')}" for a in st.forbidden.actions),
        *(f"  click {clean(c)!r}" for c in st.forbidden.controls),
        "",
        f"Statement      sha256 {verification.sha256}",
    ]
    if st.task.task_id.startswith("taught."):
        # A browser-teach write tool: what the person signs here goes further.
        lines[-1:-1] = [
            "This teaches a reusable write tool. These values bind its first run",
            "only: until the statement expires, the agent may call the tool with",
            "other records and values, each checked by the write contract.",
            "",
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class TeachAuthority:
    """Where an authoring path gets its scope: a signed statement and the roster.

    Args:
        statement: The ``teach-authorization.json`` (or its folder); its
            ``.sig`` sits beside it.
        key_dir: The key directory holding the approver roster. Defaults to
            ``$OPENADAPT_JIT_KEY_DIR`` (or ``~/.openadapt/jit``).
        approver_root: The approver root to pin. Defaults to
            ``OPENADAPT_APPROVER_ROOT``; without one, verification refuses.
        write_adapter: An adapter object to use instead of loading the
            statement's ``write_adapter`` reference.
        config_only: ``approver_root`` came from the deployment config and
            the environment never counts (a process an agent runs).
        at_least: Roster heads recorded outside the key directory (the
            deployment config's ``approvers.head``); the roster must extend
            them.
    """

    statement: Path
    key_dir: Optional[Path] = None
    approver_root: Optional[str] = None
    write_adapter: Any = None
    config_only: bool = False
    at_least: Any = None

    def kept_in(self, folder: Path) -> "TeachAuthority":
        """Copy the statement into ``folder`` and return an authority over the copy.

        The copy is what gets verified, bound and kept beside the draft, so
        a file swapped after the check can't be the one the draft names.
        """

        return dataclasses.replace(self, statement=copy_into(self.statement, folder))

    def trust(self) -> tuple[Path, Optional[str]]:
        return pinned_trust(
            self.key_dir, self.approver_root, config_only=self.config_only
        )

    def verify(
        self, *, purpose: Purpose, task_id: Optional[str] = None
    ) -> Verification:
        key_dir, root = self.trust()
        return require(
            self.statement,
            purpose=purpose,
            key_dir=key_dir,
            root=root,
            task_id=task_id,
            at_least=self.at_least,
        )

    def scope(
        self,
        *,
        purpose: Purpose,
        task_id: Optional[str] = None,
        params: Optional[Mapping[str, Any]] = None,
        record_values: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """The bound :class:`~openadapt_flow.teach_scope.TeachScope`, or an error."""

        return bind(
            self.verify(purpose=purpose, task_id=task_id),
            params,
            record_values=record_values,
            write_adapter=self.write_adapter,
        )


def narrowed(scope: Any, authorized: Any) -> Any:
    """``scope`` (from flags or a task file) under ``authorized``'s signature.

    Refuses a scope wider than the authorization (:func:`check_within`); the
    result keeps the narrower scope's limits, adds the authorization's
    forbidden list and records the authorization.
    """

    check_within(scope, authorized)
    return dataclasses.replace(
        scope,
        # The adapter the person signed for wins; a caller's may only add one.
        write_adapter=authorized.write_adapter
        if authorized.write_adapter is not None
        else scope.write_adapter,
        forbidden_actions=frozenset(scope.forbidden_actions)
        | frozenset(authorized.forbidden_actions),
        forbidden_controls=tuple(
            dict.fromkeys([*scope.forbidden_controls, *authorized.forbidden_controls])
        ),
        authorization=authorized.authorization,
    )
