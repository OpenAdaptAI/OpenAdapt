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

The person signs the file with the SSH key they publish on GitHub, as for a
run authorization (:mod:`openadapt_flow.run_authorization`)::

    ssh-keygen -Y sign -n openadapt-teach-authorization -f ~/.ssh/id_ed25519 \\
        teach-authorization.json

Every authoring path (JIT capture, ``compile-agent`` and its code-as-action
author, teach over MCP, the guarded save and the airbag guard, self-repair)
builds its scope with :func:`bind` from a verified authorization, never from
the agent's own description. Verification for enforcement
(:func:`require`) needs the signer's identity checked against allowed
signers the operator pins (``OPENADAPT_TEACH_AUTHORIZERS`` or a flag) or
against github.com live; the key snapshot stored beside the statement is
written by whoever writes the files, so it never counts. A draft records the
authorization's digest (:func:`record`), and admission re-verifies it
(:func:`check_admission`).

Ed25519 SSH keys only.
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
from typing import Any, Callable, Final, Literal, Mapping, Optional, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_flow import ssh_sig

SCHEMA: Final = "openadapt.teach-authorization/v1"
NAMESPACE: Final = "openadapt-teach-authorization"
STATEMENT_FILE: Final = "teach-authorization.json"
SIGNATURE_FILE: Final = STATEMENT_FILE + ".sig"
SIGNERS_FILE: Final = "teach-authorization.allowed_signers"
#: Where an operator pins who may authorize teaching (an allowed-signers file).
AUTHORIZERS_ENV: Final = "OPENADAPT_TEACH_AUTHORIZERS"
#: The draft and bundle key that records the authorization.
RECORD_KEY: Final = "teach_authorization"
#: Domain prefix for a value digest.
_VALUE_DOMAIN: Final = b"openadapt.teach-value/v1\0"

_HEX64 = r"^[a-f0-9]{64}$"
_NAME = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
_TASK_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_FINGERPRINT = r"^SHA256:[A-Za-z0-9+/]{43}$"
_HANDLE = r"^github:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
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
    "teach_auth_not_on_github",
    "teach_auth_live_unavailable",
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
    authorizer: StrictStr = Field(pattern=_HANDLE)
    signer_fingerprint: StrictStr = Field(pattern=_FINGERPRINT)
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

    def github_user(self) -> str:
        return self.authorizer.split(":", 1)[1]

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
    signer_fingerprint: Optional[str] = None
    signers_source: Optional[str] = None
    #: True only when checked against pinned allowed signers or github.com
    #: live; never from the snapshot stored beside the statement.
    identity_verified: bool = False
    detail: str = field(default="")


def _paths(target: Path) -> tuple[Path, Path, Path]:
    target = Path(target)
    folder = target if target.is_dir() else target.parent
    statement = target if not target.is_dir() else folder / STATEMENT_FILE
    return statement, statement.with_name(statement.name + ".sig"), folder / SIGNERS_FILE


def _utc_seconds(when: Optional[datetime]) -> str:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def signing_command(statement_path: Path) -> str:
    """The one command the authorizer runs, with their own GitHub SSH key."""

    return (
        f"ssh-keygen -Y sign -n {NAMESPACE} -f ~/.ssh/id_ed25519 "
        f"{Path(statement_path).resolve()}"
    )


def dumps(statement: TeachAuthorizationV1) -> str:
    return (
        json.dumps(statement.model_dump(by_alias=True, exclude_none=True), indent=2, sort_keys=True)
        + "\n"
    )


def create(
    out_dir: Path,
    spec: Mapping[str, Any],
    *,
    authorizer: str,
    fetch_keys: Callable[[str], list[str]] = ssh_sig.fetch_github_keys,
    signer_fingerprint: Optional[str] = None,
    now: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Write the unsigned statement and a GitHub key snapshot; return its path.

    ``spec`` holds ``task``, ``purposes``, ``origins``, ``inputs``,
    ``constants``, ``writes`` and ``forbidden`` as in the statement. Reads
    ``https://github.com/<user>.keys`` once. Refuses in strict no-outbound
    mode, when GitHub lists no Ed25519 key, and over an existing statement.
    """

    from openadapt_flow import no_outbound

    if not str(authorizer).startswith("github:"):
        raise TeachAuthorizationError("teach_auth_malformed", "authorizer is github:<user>")
    user = str(authorizer).split(":", 1)[1]
    path, _, signers_path = _paths(Path(out_dir) / STATEMENT_FILE)
    if path.exists() or signers_path.exists():
        raise TeachAuthorizationError("teach_auth_exists", f"{path} already exists")
    if no_outbound.enabled(env):
        raise TeachAuthorizationError(
            "held_no_outbound", "reading GitHub's keys needs the network"
        )
    keys = ssh_sig.ed25519_keys(fetch_keys(user))
    if not keys:
        raise TeachAuthorizationError(
            "teach_auth_no_ed25519_key",
            f"{ssh_sig.github_keys_url(user)} lists no ssh-ed25519 key",
        )
    prints = [ssh_sig.fingerprint(k) for k in keys]
    if signer_fingerprint is None:
        if len(prints) != 1:
            raise TeachAuthorizationError(
                "teach_auth_signer_ambiguous",
                f"GitHub lists {len(prints)} Ed25519 keys; name one "
                f"({', '.join(prints)})",
            )
        signer_fingerprint = prints[0]
    elif signer_fingerprint not in prints:
        raise TeachAuthorizationError(
            "teach_auth_not_on_github",
            f"{signer_fingerprint} is not among {ssh_sig.github_keys_url(user)}",
        )
    statement = TeachAuthorizationV1.model_validate(
        {
            **dict(spec),
            "schema": SCHEMA,
            "authorizer": authorizer,
            "signer_fingerprint": signer_fingerprint,
            "issued_at": _utc_seconds(now),
            **({"expires_at": _utc_seconds(expires_at)} if expires_at else {}),
            "nonce": secrets.token_hex(32),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        "\n".join(
            [
                f"# {authorizer}: snapshot of {ssh_sig.github_keys_url(user)} at "
                f"{_utc_seconds(now)}",
                *(ssh_sig.allowed_signers_line(authorizer, NAMESPACE, k) for k in keys),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.write_text(dumps(statement), encoding="utf-8")
    return path


def verify(
    target: Path,
    *,
    allowed_signers: Optional[Path] = None,
    live: Optional[Callable[[str], list[str]]] = None,
) -> Verification:
    """Verify a teach authorization; say plainly whether the identity was checked."""

    from openadapt_flow.offline_seal import loads_strict

    statement_path, sig_path, _ = _paths(Path(target))
    try:
        raw = statement_path.read_bytes()
    except FileNotFoundError:
        return Verification(False, reason="teach_auth_missing")
    except OSError:
        return Verification(False, reason="teach_auth_malformed")
    try:
        statement = TeachAuthorizationV1.model_validate(loads_strict(raw))
    except Exception as exc:
        return Verification(
            False, reason="teach_auth_malformed", detail=type(exc).__name__
        )
    base = Verification(
        False, statement=statement, sha256=hashlib.sha256(raw).hexdigest()
    )
    try:
        sig_bytes = sig_path.read_bytes()
        armored = sig_bytes.decode("ascii")
    except FileNotFoundError:
        return dataclasses.replace(base, reason="teach_auth_unsigned")
    except (OSError, UnicodeDecodeError):
        return dataclasses.replace(base, reason="teach_auth_malformed")
    base = dataclasses.replace(
        base, signature_sha256=hashlib.sha256(sig_bytes).hexdigest()
    )
    try:
        signer = ssh_sig.verify(raw, armored, NAMESPACE)
    except ssh_sig.SshSigError as exc:
        return dataclasses.replace(
            base, reason="teach_auth_bad_signature", detail=str(exc)
        )
    fp = ssh_sig.fingerprint(signer)
    base = dataclasses.replace(base, signer_fingerprint=fp)
    if fp != statement.signer_fingerprint:
        return dataclasses.replace(
            base,
            reason="teach_auth_signer_mismatch",
            detail=f"signed by {fp}, the statement names {statement.signer_fingerprint}",
        )
    verified, source = False, f"not verified (self-asserted {statement.authorizer})"
    if allowed_signers is not None:
        try:
            pinned = ssh_sig.parse_allowed_signers(
                Path(allowed_signers).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, ssh_sig.SshSigError) as exc:
            return dataclasses.replace(
                base,
                reason="teach_auth_unknown_signer",
                detail=f"no usable allowed signers at {allowed_signers}: {exc}",
            )
        if not ssh_sig.is_allowed(pinned, statement.authorizer, NAMESPACE, signer):
            return dataclasses.replace(
                base,
                reason="teach_auth_unknown_signer",
                detail=f"{fp} is not a pinned teach authorizer for {statement.authorizer}",
            )
        verified, source = True, f"pinned allowed signers {allowed_signers}"
    if live is not None:
        user = statement.github_user()
        try:
            listed = ssh_sig.ed25519_keys(live(user))
        except Exception as exc:  # network, HTTP or decoding errors: closed reason
            return dataclasses.replace(
                base, reason="teach_auth_live_unavailable", detail=type(exc).__name__
            )
        if signer not in listed:
            return dataclasses.replace(
                base,
                reason="teach_auth_not_on_github",
                detail=f"{ssh_sig.github_keys_url(user)} does not list {fp}",
            )
        live_source = f"{ssh_sig.github_keys_url(user)} now"
        source = f"{source} and {live_source}" if verified else live_source
        verified = True
    return dataclasses.replace(
        base, ok=True, signers_source=source, identity_verified=verified
    )


def pinned_authorizers(
    explicit: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[Path]:
    """The operator's pinned allowed-signers file: the flag, else the env var."""

    if explicit is not None:
        return Path(explicit).expanduser()
    value = (env if env is not None else os.environ).get(AUTHORIZERS_ENV, "").strip()
    return Path(value).expanduser() if value else None


def require(
    target: Path,
    *,
    purpose: Purpose,
    authorizers: Optional[Path] = None,
    live: Optional[Callable[[str], list[str]]] = None,
    task_id: Optional[str] = None,
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Verification:
    """A verified authorization fit for ``purpose``, or :class:`TeachAuthorizationError`.

    Unlike :func:`verify`, the signer's identity must be checked: against
    the operator's pinned authorizers (``authorizers`` or
    ``OPENADAPT_TEACH_AUTHORIZERS``), or against github.com with ``live``.
    """

    pinned = pinned_authorizers(authorizers, env)
    result = verify(target, allowed_signers=pinned, live=live)
    if not result.ok or result.statement is None:
        raise TeachAuthorizationError(str(result.reason), result.detail)
    if not result.identity_verified:
        raise TeachAuthorizationError(
            "teach_auth_identity_unverified",
            f"pin the authorizer's key ({AUTHORIZERS_ENV} or --teach-authorizers) "
            "or check it against github.com live",
        )
    st = result.statement
    stamp = _utc_seconds(now)
    if st.issued_at > stamp:
        raise TeachAuthorizationError("teach_auth_not_yet_valid", st.issued_at)
    if st.expires_at is not None and stamp >= st.expires_at:
        raise TeachAuthorizationError("teach_auth_expired", st.expires_at)
    if purpose not in st.purposes:
        raise TeachAuthorizationError(
            "teach_auth_purpose", f"it covers {', '.join(st.purposes)}, not {purpose}"
        )
    if task_id is not None and task_id != st.task.task_id:
        raise TeachAuthorizationError(
            "teach_auth_task_mismatch",
            f"authorized task {st.task.task_id}, asked for {task_id}",
        )
    return result


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
    if st is None or verification.sha256 is None:
        raise TeachAuthorizationError("teach_auth_malformed")
    return {
        "schema": SCHEMA,
        "sha256": verification.sha256,
        "signature_sha256": verification.signature_sha256,
        "task_id": st.task.task_id,
        "authorizer": st.authorizer,
        "signer_fingerprint": verification.signer_fingerprint,
        "identity": verification.signers_source,
        "record_kind": st.writes.record_kind,
        "purposes": list(st.purposes),
        "expires_at": st.expires_at,
    }


def check_admission(
    recorded: Optional[Mapping[str, Any]],
    target: Optional[Path],
    *,
    authorizers: Optional[Path] = None,
    live: Optional[Callable[[str], list[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Verification:
    """Re-verify the authorization a draft names, for admission.

    ``recorded`` is the draft's ``teach_authorization`` entry. ``target`` is
    the statement (the draft keeps a copy; see :func:`copy_into`). The
    statement's digest must equal the recorded one and its signer's identity
    must verify against the operator's pinned authorizers or github.com.
    Expiry is not checked again: it bounds when teaching may happen.
    """

    if not recorded or not isinstance(recorded, Mapping):
        raise TeachAuthorizationError(
            "teach_auth_not_recorded",
            "the draft names no teach authorization; an agent-taught draft needs one",
        )
    if target is None:
        raise TeachAuthorizationError("teach_auth_missing", "no statement beside the draft")
    pinned = pinned_authorizers(authorizers, env)
    result = verify(target, allowed_signers=pinned, live=live)
    if not result.ok:
        raise TeachAuthorizationError(str(result.reason), result.detail)
    if not result.identity_verified:
        raise TeachAuthorizationError(
            "teach_auth_identity_unverified",
            f"pin the authorizer's key ({AUTHORIZERS_ENV}) or check it live",
        )
    if result.sha256 != recorded.get("sha256"):
        raise TeachAuthorizationError(
            "teach_auth_digest_mismatch",
            "the statement beside the draft is not the one the draft recorded",
        )
    return result


def copy_into(source: Path, folder: Path) -> Path:
    """Copy the statement and its signature into ``folder``; return the statement path."""

    statement, sig, _ = _paths(Path(source))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / STATEMENT_FILE
    out.write_bytes(statement.read_bytes())
    (folder / SIGNATURE_FILE).write_bytes(sig.read_bytes())
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
        f"  Authorizer   {st.authorizer} (key {st.signer_fingerprint})",
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
    return "\n".join(lines)


def sign(
    statement_path: Path,
    key_path: Path,
    *,
    dry_run: bool = False,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    run: Optional[Callable[..., Any]] = None,
) -> int:
    """Show what the statement allows, ask, then sign with ``ssh-keygen``.

    0 signed (or dry run), 1 not signed.
    """

    import subprocess

    from openadapt_flow.authorization_summary import key_public_raw
    from openadapt_flow.offline_seal import loads_strict

    run = run or subprocess.run
    statement_path, sig_path, signers_path = _paths(Path(statement_path))
    try:
        raw = statement_path.read_bytes()
        statement = TeachAuthorizationV1.model_validate(loads_strict(raw))
    except Exception:
        say(f"not signed: teach_auth_malformed: {statement_path}")
        return 1
    if sig_path.exists():
        say(f"not signed: teach_auth_already_signed: {sig_path} exists")
        return 1
    try:
        key_raw = key_public_raw(Path(key_path), run=run)
    except Exception as exc:
        say(f"not signed: {exc}")
        return 1
    if ssh_sig.fingerprint(key_raw) != statement.signer_fingerprint:
        say(
            "not signed: teach_auth_signer_mismatch: the statement names "
            f"{statement.signer_fingerprint}; {key_path} is {ssh_sig.fingerprint(key_raw)}"
        )
        return 1
    try:
        snapshot = ssh_sig.parse_allowed_signers(signers_path.read_text("utf-8"))
    except (OSError, ssh_sig.SshSigError):
        snapshot = []
    on_github = ssh_sig.is_allowed(snapshot, statement.authorizer, NAMESPACE, key_raw)
    say(
        render(
            Verification(
                False, statement=statement, sha256=hashlib.sha256(raw).hexdigest()
            )
        )
    )
    if dry_run:
        say("\n(dry run: nothing signed)")
        return 0
    if on_github:
        agreed = ask("\nSign this teach authorization? [y/N] ").strip().lower() in (
            "y",
            "yes",
        )
    else:
        say(
            "\nWARNING: the copy of the GitHub keys next to this statement does not "
            "list this key for the authorizer."
        )
        agreed = ask('Type "yes" to sign anyway: ').strip() == "yes"
    if not agreed:
        say("not signed")
        return 1
    if statement_path.read_bytes() != raw:
        say("not signed: the statement changed while it was shown")
        return 1
    out = run(
        ["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", str(key_path), str(statement_path)]
    )
    if getattr(out, "returncode", 1) != 0 or not sig_path.is_file():
        say("not signed: ssh-keygen failed")
        return 1
    say(f"signed: {sig_path}")
    return 0


@dataclass(frozen=True)
class TeachAuthority:
    """Where an authoring path gets its scope: a signed statement and who may sign.

    Args:
        statement: The ``teach-authorization.json`` (or its folder); its
            ``.sig`` sits beside it.
        authorizers: The operator's pinned allowed-signers file. Defaults to
            ``OPENADAPT_TEACH_AUTHORIZERS``.
        live: Check the signer against github.com now (e.g.
            ``ssh_sig.fetch_github_keys``) instead of, or as well as, a pin.
        write_adapter: An adapter object to use instead of loading the
            statement's ``write_adapter`` reference.
    """

    statement: Path
    authorizers: Optional[Path] = None
    live: Optional[Callable[[str], list[str]]] = None
    write_adapter: Any = None

    def kept_in(self, folder: Path) -> "TeachAuthority":
        """Copy the statement into ``folder`` and return an authority over the copy.

        The copy is what gets verified, bound and kept beside the draft, so
        a file swapped after the check can't be the one the draft names.
        """

        return dataclasses.replace(self, statement=copy_into(self.statement, folder))

    def verify(self, *, purpose: Purpose, task_id: Optional[str] = None) -> Verification:
        return require(
            self.statement,
            purpose=purpose,
            authorizers=self.authorizers,
            live=self.live,
            task_id=task_id,
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
        write_adapter=scope.write_adapter or authorized.write_adapter,
        forbidden_actions=frozenset(scope.forbidden_actions)
        | frozenset(authorized.forbidden_actions),
        forbidden_controls=tuple(
            dict.fromkeys([*scope.forbidden_controls, *authorized.forbidden_controls])
        ),
        authorization=authorized.authorization,
    )
