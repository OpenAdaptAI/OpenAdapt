"""Run authorizations: a person, known by their GitHub SSH key, authorizes runs.

An offline Seal says which machine key sealed a run. A run authorization
(``openadapt.run-authorization/v1``) says which person authorized it: a
short JSON statement naming the program (its bundle content digest), the
scope (the items and the execution profile, and the Seal keys allowed to
seal under it), the authorizer's public handle (``github:<user>``), when,
and a random nonce. The person signs the file with the SSH key they publish
on GitHub::

    ssh-keygen -Y sign -n openadapt-run-authorization -f ~/.ssh/id_ed25519 \\
        run-authorization.json

Next to the statement sit its signature (``run-authorization.json.sig``) and
a snapshot of the signer's keys, taken from ``https://github.com/<user>.keys``
when the statement was made, in ``ssh-keygen`` allowed-signers form
(``run-authorization.allowed_signers``). Verification is offline against
that snapshot, or against allowed signers the verifier pins. ``--live``
re-reads the keys from github.com (the host is fixed; nothing in the files
chooses where to fetch from) and refuses a key GitHub no longer lists.

Each Seal sealed under an authorization names its SHA-256 and the scope item
it claims (``authorization_sha256``, ``scope_item``), signed by the Seal key.
A verifier then checks the reference, the person's signature, that the item,
profile and Seal key are in scope, and that the run's bundle digest is the
one the person authorized. The authorization can be anchored in the
transparency log like a Seal (:mod:`openadapt_flow.seal_anchor`), signed by
the runner's Seal key: one entry over the statement's and signature's digests.

Ed25519 SSH keys only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from openadapt_flow import ssh_sig

SCHEMA: Final = "openadapt.run-authorization/v1"
NAMESPACE = "openadapt-run-authorization"
STATEMENT_FILE = "run-authorization.json"
SIGNATURE_FILE = STATEMENT_FILE + ".sig"
SIGNERS_FILE = "run-authorization.allowed_signers"
ANCHOR_FILE = "run-authorization.anchor.json"
_HEX64 = r"^[a-f0-9]{64}$"
_ITEM = r"^[A-Za-z0-9._-]{1,64}$"
_KEY_ID = r"^seal-ed25519-[a-f0-9]{16}$"
_PROFILE = r"^[a-z][a-z0-9_-]{0,31}$"
_HANDLE = r"^github:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
_ISO_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"

AuthorizationRefusal = Literal[
    "auth_missing",
    "auth_malformed",
    "auth_unsigned",
    "auth_bad_signature",
    "auth_unknown_signer",
    "auth_not_on_github",
    "auth_live_unavailable",
    "auth_not_referenced",
    "auth_reference_mismatch",
    "auth_scope_mismatch",
    "auth_bundle_mismatch",
    "auth_anchored_after_seal",
]


class AuthorizationError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[StrictStr] = Field(min_length=1, max_length=1000)
    profile: StrictStr = Field(pattern=_PROFILE)
    seal_key_ids: list[StrictStr] = Field(min_length=1, max_length=100)

    @field_validator("items")
    @classmethod
    def _items(cls, value: list[str]) -> list[str]:
        import re

        if len(set(value)) != len(value) or not all(re.match(_ITEM, v) for v in value):
            raise ValueError("items are unique names of [A-Za-z0-9._-]")
        return value

    @field_validator("seal_key_ids")
    @classmethod
    def _keys(cls, value: list[str]) -> list[str]:
        import re

        if len(set(value)) != len(value) or not all(
            re.match(_KEY_ID, v) for v in value
        ):
            raise ValueError("seal_key_ids are unique seal-ed25519-... key IDs")
        return value


class RunAuthorizationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt.run-authorization/v1"] = Field(
        default=SCHEMA, alias="schema"
    )
    bundle_content_digest: StrictStr = Field(pattern=_HEX64)
    scope: Scope
    authorizer: StrictStr = Field(pattern=_HANDLE)
    issued_at: StrictStr = Field(pattern=_ISO_SECONDS)
    nonce: StrictStr = Field(pattern=_HEX64)

    def github_user(self) -> str:
        return self.authorizer.split(":", 1)[1]


@dataclass(frozen=True)
class AuthorizationVerification:
    ok: bool
    reason: Optional[str] = None
    statement: Optional[RunAuthorizationV1] = None
    sha256: Optional[str] = None  # of run-authorization.json's exact bytes
    signature_sha256: Optional[str] = None
    signer_fingerprint: Optional[str] = None
    signers_source: Optional[str] = None  # what the signer was checked against
    anchor: Any = None
    detail: str = field(default="")


def _paths(target: Path) -> tuple[Path, Path, Path, Path]:
    target = Path(target)
    folder = target if target.is_dir() else target.parent
    statement = target if not target.is_dir() else folder / STATEMENT_FILE
    return (
        statement,
        statement.with_name(statement.name + ".sig"),
        folder / SIGNERS_FILE,
        folder / ANCHOR_FILE,
    )


def _utc_seconds(now: Optional[datetime]) -> str:
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def signing_command(statement_path: Path) -> str:
    """The one command the authorizer runs, with their own GitHub SSH key."""
    return (
        f"ssh-keygen -Y sign -n {NAMESPACE} -f ~/.ssh/id_ed25519 "
        f"{Path(statement_path).resolve()}"
    )


def create(
    out_dir: Path,
    *,
    bundle_content_digest: str,
    items: Sequence[str],
    profile: str,
    authorizer: str,
    seal_key_ids: Sequence[str],
    fetch_keys: Callable[[str], list[str]] = ssh_sig.fetch_github_keys,
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Write the unsigned statement and the GitHub key snapshot; return its path.

    Reads ``https://github.com/<user>.keys`` once, for the snapshot. Refuses
    when GitHub lists no Ed25519 key for the user, in strict no-outbound
    mode, and when a statement already exists in ``out_dir``.
    """
    from openadapt_flow import no_outbound

    statement = RunAuthorizationV1.model_validate(
        {
            "schema": SCHEMA,
            "bundle_content_digest": bundle_content_digest,
            "scope": {
                "items": list(items),
                "profile": profile,
                "seal_key_ids": list(seal_key_ids),
            },
            "authorizer": authorizer,
            "issued_at": _utc_seconds(now),
            "nonce": secrets.token_hex(32),
        }
    )
    out_dir = Path(out_dir)
    path, _, signers_path, _ = _paths(out_dir / STATEMENT_FILE)
    if path.exists() or signers_path.exists():
        raise AuthorizationError("auth_exists", f"{path} already exists")
    if no_outbound.enabled(env):
        raise AuthorizationError(
            "held_no_outbound", "reading GitHub's keys needs the network"
        )
    user = statement.github_user()
    keys = ssh_sig.ed25519_keys(fetch_keys(user))
    if not keys:
        raise AuthorizationError(
            "auth_no_ed25519_key",
            f"{ssh_sig.github_keys_url(user)} lists no ssh-ed25519 key",
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    when = _utc_seconds(now)
    lines = [
        f"# {statement.authorizer}: snapshot of {ssh_sig.github_keys_url(user)} at {when}",
        *(
            ssh_sig.allowed_signers_line(statement.authorizer, NAMESPACE, k)
            for k in keys
        ),
    ]
    signers_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.write_text(
        json.dumps(statement.model_dump(by_alias=True), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def verify(
    target: Path,
    *,
    allowed_signers: Optional[Path] = None,
    live: Optional[Callable[[str], list[str]]] = None,
    registry: Optional[Sequence[Any]] = None,
    require_anchor: bool = False,
    log_keys: Optional[Sequence[Any]] = None,
) -> AuthorizationVerification:
    """Verify a run authorization offline; optionally re-read GitHub and the anchor.

    ``allowed_signers`` pins the verifier's own allowed-signers file instead
    of the snapshot stored next to the statement. ``live`` re-reads the
    authorizer's keys from github.com. With ``require_anchor``, the anchor
    must verify against the pinned Seal-key ``registry``.
    """
    from openadapt_flow.offline_seal import loads_strict

    statement_path, sig_path, snapshot_path, anchor_file = _paths(Path(target))
    try:
        raw = statement_path.read_bytes()
    except FileNotFoundError:
        return AuthorizationVerification(False, reason="auth_missing")
    except OSError:
        return AuthorizationVerification(False, reason="auth_malformed")
    try:
        statement = RunAuthorizationV1.model_validate(loads_strict(raw))
    except Exception:
        return AuthorizationVerification(False, reason="auth_malformed")
    sha = hashlib.sha256(raw).hexdigest()
    base = AuthorizationVerification(False, statement=statement, sha256=sha)
    try:
        sig_bytes = sig_path.read_bytes()
        armored = sig_bytes.decode("ascii")
    except FileNotFoundError:
        return dataclasses.replace(base, reason="auth_unsigned")
    except (OSError, UnicodeDecodeError):
        return dataclasses.replace(base, reason="auth_malformed")
    sig_sha = hashlib.sha256(sig_bytes).hexdigest()
    base = dataclasses.replace(base, signature_sha256=sig_sha)
    try:
        signer = ssh_sig.verify(raw, armored, NAMESPACE)
    except ssh_sig.SshSigError as exc:
        return dataclasses.replace(base, reason="auth_bad_signature", detail=str(exc))
    fp = ssh_sig.fingerprint(signer)
    signers_path = Path(allowed_signers) if allowed_signers else snapshot_path
    try:
        signers = ssh_sig.parse_allowed_signers(
            signers_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ssh_sig.SshSigError) as exc:
        return dataclasses.replace(
            base,
            reason="auth_unknown_signer",
            signer_fingerprint=fp,
            detail=f"no usable allowed signers at {signers_path}: {exc}",
        )
    if not ssh_sig.is_allowed(signers, statement.authorizer, NAMESPACE, signer):
        return dataclasses.replace(
            base,
            reason="auth_unknown_signer",
            signer_fingerprint=fp,
            detail=f"{fp} is not an allowed signer for {statement.authorizer}",
        )
    source = (
        "pinned allowed signers" if allowed_signers else "the stored GitHub snapshot"
    )
    if live is not None:
        user = statement.github_user()
        try:
            listed = ssh_sig.ed25519_keys(live(user))
        except Exception as exc:  # network, HTTP or decoding errors: closed reason
            return dataclasses.replace(
                base,
                reason="auth_live_unavailable",
                signer_fingerprint=fp,
                detail=type(exc).__name__,
            )
        if signer not in listed:
            return dataclasses.replace(
                base,
                reason="auth_not_on_github",
                signer_fingerprint=fp,
                detail=f"{ssh_sig.github_keys_url(user)} no longer lists {fp}",
            )
        source += f" and {ssh_sig.github_keys_url(user)} now"
    anchor = None
    if require_anchor:
        anchor = verify_anchor(
            sha, sig_sha, anchor_file, registry or [], log_keys=log_keys
        )
        if not anchor.ok:
            return dataclasses.replace(
                base,
                reason=anchor.reason,
                signer_fingerprint=fp,
                signers_source=source,
                anchor=anchor,
            )
    return dataclasses.replace(
        base, ok=True, signer_fingerprint=fp, signers_source=source, anchor=anchor
    )


def _record_fields(sha: str, sig_sha: str) -> dict[str, str]:
    return {"run_authorization_sha256": sha, "signature_sha256": sig_sha}


def verify_anchor(
    sha: str,
    sig_sha: str,
    anchor_file: Path,
    registry: Sequence[Any],
    *,
    log_keys: Optional[Sequence[Any]] = None,
) -> Any:
    """The authorization's transparency-log anchor, signed by a pinned Seal key."""
    import base64

    from openadapt_flow.seal_anchor import (
        AnchorVerification,
        _load_anchor_file,
        verify_record_anchor,
    )

    anchor = _load_anchor_file(Path(anchor_file))
    if anchor is None:
        return AnchorVerification(False, reason="anchor_missing")
    key_id = anchor.get("key_id")
    keys = [k for k in registry if k.key_id == key_id and k.status == "active"]
    if len(keys) != 1:
        return AnchorVerification(False, reason="anchor_key_mismatch")
    return verify_record_anchor(
        _record_fields(sha, sig_sha),
        anchor,
        base64.b64decode(keys[0].public_key),
        log_keys=log_keys,
    )


def anchor(
    target: Path,
    signer: Any,
    *,
    log_url: Optional[str] = None,
    log_keys: Optional[Sequence[Any]] = None,
    submit: Any = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Anchor a signed authorization in the transparency log with the Seal key.

    Sends one SHA-512 digest (of the statement's and signature's SHA-256 and
    a nonce), the Seal key's public key and a signature. Refuses an
    authorization that does not verify, or whose scope doesn't name
    ``signer``'s key.
    """
    from openadapt_flow.seal_anchor import REKOR_URL, anchor_record, submit_hashedrekord

    result = verify(target)
    if not result.ok or result.statement is None:
        raise AuthorizationError(str(result.reason), result.detail)
    if signer.key_id not in result.statement.scope.seal_key_ids:
        raise AuthorizationError(
            "auth_scope_mismatch",
            f"{signer.key_id} is not in the authorization's scope",
        )
    _, _, _, anchor_file = _paths(Path(target))
    return anchor_record(
        _record_fields(str(result.sha256), str(result.signature_sha256)),
        anchor_file,
        signer,
        log_url=log_url or REKOR_URL,
        log_keys=log_keys,
        submit=submit or submit_hashedrekord,
        env=env,
    )


def check_run(
    seal: Mapping[str, Any], report: Mapping[str, Any], auth: AuthorizationVerification
) -> Optional[str]:
    """``None`` when a verified Seal and its report fit a verified authorization."""
    if not auth.ok or auth.statement is None:
        return auth.reason or "auth_malformed"
    ref = seal.get("authorization_sha256")
    if ref is None:
        return "auth_not_referenced"
    if ref != auth.sha256:
        return "auth_reference_mismatch"
    scope = auth.statement.scope
    if (
        seal.get("scope_item") not in scope.items
        or seal.get("key_id") not in scope.seal_key_ids
        or report.get("execution_profile") != scope.profile
    ):
        return "auth_scope_mismatch"
    if report.get("bundle_content_digest") != auth.statement.bundle_content_digest:
        return "auth_bundle_mismatch"
    return None


def seal_authorized(
    run_dir: Path,
    target: Path,
    item: str,
    signer: Any,
    *,
    home: Optional[Path] = None,
    replace_unanchored: bool = False,
    now: Optional[datetime] = None,
) -> Any:
    """Seal ``run_dir`` under the authorization at ``target`` as scope ``item``.

    Refuses, and writes nothing, unless the authorization verifies and the
    run's report fits it: the item, profile and Seal key are in scope and
    the bundle digest matches.
    """
    from openadapt_flow.offline_seal import LocalSealError, seal_run

    auth = verify(target)
    if not auth.ok:
        raise LocalSealError(str(auth.reason), auth.detail)
    try:
        report = json.loads((Path(run_dir) / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalSealError("report_unreadable", str(run_dir)) from exc
    if not isinstance(report, dict):
        raise LocalSealError("report_unreadable", str(run_dir))
    claim = {
        "authorization_sha256": auth.sha256,
        "scope_item": item,
        "key_id": signer.key_id,
    }
    problem = check_run(claim, report, auth)
    if problem:
        raise LocalSealError(problem, f"run {Path(run_dir).name} does not fit {target}")
    return seal_run(
        Path(run_dir),
        home=home,
        signer=signer,
        now=now,
        authorization=(str(auth.sha256), item),
        replace_unanchored=replace_unanchored,
    )
