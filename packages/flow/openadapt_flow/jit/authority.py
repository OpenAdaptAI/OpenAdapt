"""Who can act on a JIT draft without a person at a terminal.

Admission has three modes, and all of them write the same signed
``admission.json`` (:mod:`openadapt_flow.jit.lifecycle`):

* **person**: a named person at an interactive terminal (``jit admit``).
* **admitter key**: a CI job or script admits with an Ed25519 key that a
  person provisioned and registered. The key file is encrypted; its
  passphrase comes from :data:`KEY_PASSPHRASE_ENV`. The principal is the
  name the key is registered under, never a name the caller passes.
* **agent under policy**: a person signs an :class:`AdmissionPolicy` once. A
  registered agent, holding its own key, can then run canaries and admit any
  draft that satisfies the policy. Flow re-verifies the policy's signature,
  expiry and scope, and the canary evidence, at each step itself
  (:func:`verify_policy`, :func:`scope_problems`). A draft outside the policy
  goes to the ``needs_person`` queue.

The same design as the fleet promotion policy (``openadapt_flow.fleet.policy``
on the fleet branch): a person-to-key registry, domain-separated Ed25519
signatures over canonical JSON, and a policy digest bound into what it
approves. Nothing here imports that code.

Trust root. The registry (``principals.json``) and the local admission key
live in the key directory (``$OPENADAPT_JIT_KEY_DIR`` or ``~/.openadapt/jit``).
Whoever can write that directory can register a key or re-sign an
admission, so keep it away from the agents it governs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AdmissionPolicy",
    "AuthorityError",
    "KEY_PASSPHRASE_ENV",
    "POLICY_SCHEMA",
    "PRINCIPALS_FILE",
    "Principal",
    "PrincipalRegistry",
    "SignedAdmissionPolicy",
    "generate_encrypted_key",
    "load_encrypted_key",
    "principal_from_key",
    "public_hex",
    "register_principal",
    "scope_problems",
    "sign_admitter",
    "sign_policy",
    "verify_admitter",
    "verify_policy",
]

PRINCIPALS_FILE = "principals.json"
PRINCIPALS_SCHEMA = "openadapt-flow/jit-principals/v1"
POLICY_SCHEMA = "openadapt-flow/jit-admission-policy/v1"
POLICY_DOMAIN = b"openadapt-flow.jit.admission-policy/v1\x00"
ADMITTER_DOMAIN = b"openadapt-flow.jit.admitter/v1\x00"
#: Passphrase for an encrypted admitter, agent or policy-signer key. Read
#: from the environment so it stays out of shell history.
KEY_PASSPHRASE_ENV = "OPENADAPT_JIT_KEY_PASSPHRASE"

Role = Literal["policy_signer", "admitter_key", "agent"]
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")


class AuthorityError(RuntimeError):
    """A key, registry entry or policy does not give the authority claimed."""


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone; use UTC (…Z)")
    return parsed.astimezone(timezone.utc)


# -- keys ------------------------------------------------------------------------


def public_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


def _passphrase(env: Mapping[str, str]) -> bytes:
    value = env.get(KEY_PASSPHRASE_ENV)
    if not value:
        raise AuthorityError(
            f"set {KEY_PASSPHRASE_ENV} to the key's passphrase; an admitter key "
            "is always encrypted"
        )
    return value.encode("utf-8")


def load_encrypted_key(
    path: Path | str, *, env: Optional[Mapping[str, str]] = None
) -> Ed25519PrivateKey:
    """Open an encrypted Ed25519 key with the passphrase from the environment.

    Raises:
        AuthorityError: the key is not encrypted, the passphrase is missing or
            wrong, or it is not an Ed25519 key.
    """

    data = Path(path).read_bytes()
    try:
        serialization.load_pem_private_key(data, password=None)
    except TypeError:
        pass  # encrypted, as required
    except ValueError as exc:
        raise AuthorityError(f"{path} is not a PEM private key") from exc
    else:
        raise AuthorityError(
            f"{path} is not encrypted; an admitter key must be (keygen with "
            f"{KEY_PASSPHRASE_ENV} set)"
        )
    try:
        key = serialization.load_pem_private_key(
            data, password=_passphrase(os.environ if env is None else env)
        )
    except ValueError as exc:
        raise AuthorityError(f"can't open {path}: wrong passphrase?") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AuthorityError(f"{path} is not an Ed25519 key")
    return key


def generate_encrypted_key(
    path: Path | str, *, env: Optional[Mapping[str, str]] = None
) -> str:
    """Write a new encrypted Ed25519 key (mode 0600) and return its public key
    in hex, for :func:`register_principal`. Refuses to overwrite."""

    out = Path(path)
    if out.exists():
        raise AuthorityError(f"{out} exists; refusing to overwrite a key")
    passphrase = _passphrase(os.environ if env is None else env)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return public_hex(key)


def _public(hex_key: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))


def _verify(hex_key: str, domain: bytes, payload: Any, signature: str) -> bool:
    from openadapt_flow.execute.keys import verify_signature

    try:
        public = _public(hex_key)
    except ValueError:
        return False
    return verify_signature(public, domain + _canonical(payload), signature)


def _sign(key: Ed25519PrivateKey, domain: bytes, payload: Any) -> str:
    from openadapt_flow.execute.keys import sign_bytes

    return sign_bytes(key, domain + _canonical(payload))


# -- registry --------------------------------------------------------------------


def _is_unregistered_agent_name(name: str) -> bool:
    from openadapt_flow.jit.lifecycle import is_not_a_person_name

    return is_not_a_person_name(name)


class PrincipalRegistry(BaseModel):
    """Keys a person provisioned, by role. Kept in the key directory."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt-flow/jit-principals/v1"] = Field(
        default="openadapt-flow/jit-principals/v1", alias="schema"
    )
    #: person id -> public key (hex) that person signs admission policy with.
    policy_signers: dict[str, str] = Field(default_factory=dict)
    #: principal id -> public key of a CI or script admitter key.
    admitter_keys: dict[str, str] = Field(default_factory=dict)
    #: agent principal id -> public key the agent signs with.
    agents: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "PrincipalRegistry":
        seen: dict[str, str] = {}
        keys: dict[str, str] = {}
        for role in ("policy_signers", "admitter_keys", "agents"):
            for pid, pub in getattr(self, role).items():
                if not re.match(_ID, pid):
                    raise ValueError(f"invalid principal id {pid!r}")
                if not _HEX_KEY.match(pub):
                    raise ValueError(f"{pid}'s public key is not 32 bytes of hex")
                if pid.lower() in seen:
                    raise ValueError(f"{pid!r} is registered twice")
                if pub in keys:
                    raise ValueError(
                        f"{pid!r} and {keys[pub]!r} share a key; one key, one role"
                    )
                seen[pid.lower()] = role
                keys[pub] = pid
        for pid in self.policy_signers:
            if _is_unregistered_agent_name(pid):
                raise ValueError(
                    f"{pid!r} names an agent or model; only a person signs policy"
                )
        return self

    @classmethod
    def load(cls, key_dir: Path) -> "PrincipalRegistry":
        path = Path(key_dir) / PRINCIPALS_FILE
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text())

    def save(self, key_dir: Path) -> Path:
        path = Path(key_dir) / PRINCIPALS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2, by_alias=True) + "\n")
        os.replace(tmp, path)
        return path

    def lookup(self, public_key_hex: str) -> Optional[tuple[Role, str]]:
        roles: tuple[tuple[Role, dict[str, str]], ...] = (
            ("policy_signer", self.policy_signers),
            ("admitter_key", self.admitter_keys),
            ("agent", self.agents),
        )
        for role, entries in roles:
            for pid, pub in entries.items():
                if pub == public_key_hex:
                    return role, pid
        return None


def register_principal(
    key_dir: Path, *, role: Role, principal_id: str, public_key_hex: str
) -> PrincipalRegistry:
    """Add a key to the registry. A person does this (``jit register``)."""

    registry = PrincipalRegistry.load(key_dir)
    field_name = {
        "policy_signer": "policy_signers",
        "admitter_key": "admitter_keys",
        "agent": "agents",
    }.get(role)
    if field_name is None:
        raise AuthorityError(f"unknown role {role!r}")
    data = registry.model_dump(by_alias=True)
    data[field_name] = {**data[field_name], principal_id: public_key_hex.lower()}
    try:
        updated = PrincipalRegistry.model_validate(data)
    except ValueError as exc:
        raise AuthorityError(str(exc)) from exc
    updated.save(key_dir)
    return updated


# -- admission policy ------------------------------------------------------------


class AdmissionPolicy(BaseModel):
    """What a person lets registered agents admit without them."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt-flow/jit-admission-policy/v1"] = Field(
        default="openadapt-flow/jit-admission-policy/v1", alias="schema"
    )
    policy_id: str = Field(pattern=_ID)
    #: The person who signs it, as registered under ``policy_signers``.
    signer: str = Field(pattern=_ID)
    signed_at: str = Field(min_length=10, max_length=64)
    expires_at: str = Field(min_length=10, max_length=64)
    #: ``host`` or ``host:port`` of the target app's origin.
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    #: Step actions (``openadapt_flow.ir.ActionKind`` values).
    allowed_actions: tuple[str, ...] = Field(min_length=1)
    #: Declared effect kinds (``openadapt_flow.runtime.effects.EffectKind``).
    allowed_effects: tuple[str, ...] = ()
    #: Every write step must be read back from the system of record (tier 1).
    required_readback: Literal["independent_readback"] = "independent_readback"
    min_clean_canaries: int = Field(default=3, ge=1, le=100)
    #: Registered agent principals this policy authorizes.
    agent_principals: tuple[str, ...] = Field(min_length=1)
    #: Flow risk policies a draft may name; empty means any.
    flow_policies: tuple[str, ...] = ()

    @field_validator("allowed_actions")
    @classmethod
    def _actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from openadapt_flow.ir import ActionKind

        known = {a.value for a in ActionKind}
        bad = [a for a in value if a not in known]
        if bad:
            raise ValueError(f"unknown action {bad[0]!r}")
        return value

    @field_validator("allowed_effects")
    @classmethod
    def _effects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from openadapt_flow.runtime.effects.effect import EffectKind

        known = {e.value for e in EffectKind}
        bad = [e for e in value if e not in known]
        if bad:
            raise ValueError(f"unknown effect kind {bad[0]!r}")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(h.strip().lower() for h in value)

    @model_validator(mode="after")
    def _check(self) -> "AdmissionPolicy":
        _utc(self.signed_at)
        if _utc(self.expires_at) <= _utc(self.signed_at):
            raise ValueError("the policy expires before it was signed")
        if self.signer.lower() in {a.lower() for a in self.agent_principals}:
            raise ValueError("the policy's signer can't be one of its agents")
        return self

    def digest(self) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                _canonical(self.model_dump(mode="json", by_alias=True))
            ).hexdigest()
        )


class SignedAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: AdmissionPolicy
    signer_public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1, max_length=200)

    @classmethod
    def load(cls, path: Path | str) -> "SignedAdmissionPolicy":
        try:
            return cls.model_validate_json(Path(path).read_text())
        except ValueError as exc:
            raise AuthorityError(f"{path} is not a signed admission policy: {exc}")

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.write_text(self.model_dump_json(indent=2, by_alias=True) + "\n")
        return out


def sign_policy(
    policy: AdmissionPolicy, key: Ed25519PrivateKey
) -> SignedAdmissionPolicy:
    """A person signs ``policy`` with their registered policy-signer key."""

    payload = policy.model_dump(mode="json", by_alias=True)
    return SignedAdmissionPolicy(
        policy=policy,
        signer_public_key=public_hex(key),
        signature=_sign(key, POLICY_DOMAIN, payload),
    )


def verify_policy(
    signed: SignedAdmissionPolicy,
    registry: PrincipalRegistry,
    *,
    now: Optional[datetime] = None,
    check_expiry: bool = True,
) -> AdmissionPolicy:
    """Return the policy if its registered signer signed it and it has not
    expired; raise :class:`AuthorityError` otherwise."""

    policy = signed.policy
    registered = registry.policy_signers.get(policy.signer)
    if registered is None:
        raise AuthorityError(f"{policy.signer!r} is not a registered policy signer")
    if registered != signed.signer_public_key:
        raise AuthorityError("the policy was signed with another key")
    payload = policy.model_dump(mode="json", by_alias=True)
    if not _verify(registered, POLICY_DOMAIN, payload, signed.signature):
        raise AuthorityError("the policy's signature does not verify")
    if check_expiry:
        at = now or datetime.now(timezone.utc)
        if _utc(policy.expires_at) <= at:
            raise AuthorityError(f"the policy expired at {policy.expires_at}")
        if _utc(policy.signed_at) > at:
            raise AuthorityError("the policy is signed in the future")
    return policy


def scope_problems(
    policy: AdmissionPolicy,
    workflow: Any,
    candidate: Mapping[str, Any],
    target_origin: Optional[str],
) -> list[str]:
    """Why this draft, on this app, is outside ``policy``. Empty when inside."""

    problems: list[str] = []
    if target_origin is not None:
        host = (urlsplit(target_origin).netloc or "").lower()
        if host not in policy.allowed_hosts:
            problems.append(f"the app at {host or target_origin!r} is not allowed")
    for step in workflow.steps:
        if step.action.value not in policy.allowed_actions:
            problems.append(f"{step.id}: action {step.action.value!r} is not allowed")
        for effect in step.effects:
            if effect.kind.value not in policy.allowed_effects:
                problems.append(
                    f"{step.id}: effect {effect.kind.value!r} is not allowed"
                )
    flow_policy = str(candidate.get("policy") or "")
    if policy.flow_policies and flow_policy not in policy.flow_policies:
        problems.append(f"flow policy {flow_policy!r} is not allowed")
    return problems


# -- principals ------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """A registered key acting on a draft. Build with :func:`principal_from_key`."""

    principal_id: str
    kind: Literal["admitter_key", "agent"]
    public_key: str
    policy: Optional[SignedAdmissionPolicy] = None
    _key: Optional[Ed25519PrivateKey] = field(default=None, repr=False, compare=False)

    def sign(self, payload: Mapping[str, Any]) -> str:
        if self._key is None:
            raise AuthorityError("this principal holds no key to sign with")
        return _sign(self._key, ADMITTER_DOMAIN, payload)


def principal_from_key(
    key: Ed25519PrivateKey,
    key_dir: Path,
    *,
    policy: Optional[SignedAdmissionPolicy] = None,
    now: Optional[datetime] = None,
) -> Principal:
    """The principal ``key`` is registered as. An agent needs a policy that a
    registered person signed, that has not expired, and that names it.

    Raises:
        AuthorityError: an unregistered key, a policy-signer key, an agent with
            no valid policy, or an admitter key offered with a policy.
    """

    registry = PrincipalRegistry.load(key_dir)
    pub = public_hex(key)
    found = registry.lookup(pub)
    if found is None:
        raise AuthorityError("this key is not registered; a person registers it")
    role, pid = found
    if role == "policy_signer":
        raise AuthorityError(
            f"{pid!r} signs admission policy; a person admits at a terminal"
        )
    if role == "admitter_key":
        if policy is not None:
            raise AuthorityError(
                f"{pid!r} is an admitter key; a policy is for registered agents"
            )
        return Principal(pid, "admitter_key", pub, None, key)
    if policy is None:
        raise AuthorityError(
            f"{pid!r} is a registered agent; it acts only under a signed admission "
            "policy"
        )
    verified = verify_policy(policy, registry, now=now)
    if pid not in verified.agent_principals:
        raise AuthorityError(f"the policy does not authorize agent {pid!r}")
    return Principal(pid, "agent", pub, policy, key)


def recheck(principal: Principal, key_dir: Path) -> None:
    """Re-verify a :class:`Principal` against the registry and its policy, so
    a hand-built one gives no authority."""

    registry = PrincipalRegistry.load(key_dir)
    found = registry.lookup(principal.public_key)
    if (
        found is None
        or found[1] != principal.principal_id
        or found[0] != (principal.kind)
    ):
        raise AuthorityError(
            f"{principal.principal_id!r} is not registered with this key"
        )
    if principal._key is None or public_hex(principal._key) != principal.public_key:
        raise AuthorityError("the principal does not hold its registered key")
    if principal.kind == "agent":
        if principal.policy is None:
            raise AuthorityError("an agent acts only under a signed admission policy")
        policy = verify_policy(principal.policy, registry)
        if principal.principal_id not in policy.agent_principals:
            raise AuthorityError(
                f"the policy does not authorize agent {principal.principal_id!r}"
            )


def sign_admitter(principal: Principal, body: Mapping[str, Any]) -> str:
    return principal.sign(body)


def verify_admitter(
    body: Mapping[str, Any], signature: str, key_dir: Path
) -> tuple[Role, str]:
    """Check an admission body's admitter signature against the registry."""

    registry = PrincipalRegistry.load(key_dir)
    pub = str(body.get("admitter_public_key") or "")
    found = registry.lookup(pub)
    if found is None or found[1] != body.get("admitted_by_principal_id"):
        raise AuthorityError("the admitting key is not registered as the admitter")
    if not _verify(pub, ADMITTER_DOMAIN, dict(body), signature):
        raise AuthorityError("the admitter's signature does not verify")
    return found


def canary_runs(path: Path, digest: str) -> list[dict[str, Any]]:
    """Every canary recorded for ``digest`` (``canaries.jsonl``)."""

    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("digest") == digest]


def describe(principal: Any) -> str:
    return (
        principal.principal_id if isinstance(principal, Principal) else str(principal)
    )
