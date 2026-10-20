"""Offline Seals: sign a run, record or file on this machine with a local key.

A Seal says who stated what about one run. The Cloud Seal
(``openadapt.seal-signature/v1``, see :mod:`openadapt_types.seal`) is
issued by OpenAdapt Cloud, which needs a network connection. An offline Seal
(``openadapt.local-seal/v1``) is issued by a key that never leaves the
customer's machine, so a site with no outbound traffic still gets a signed
statement for every run. It is the default: ``openadapt-flow run`` and
``replay`` with ``--run-dir`` write ``seal.json`` next to ``report.json``,
and the on-prem service seals every run it executes. Set
``OPENADAPT_LOCAL_SEAL=off`` to turn it off.

What an offline Seal states:

* the SHA-256 of the exact bytes of the subject (``report.json`` for a run,
  or any record, export or file);
* the run id and the run's recorded outcome, copied from the report;
* when it was issued and which key issued it.

What it does not state: it grants no authority, it does not grade the run
(the outcome is copied, never upgraded), and a statement signed by the same
machine that ran the work is weaker evidence than an independent party's.
For a third-party time and signature, add batched timestamps later with
:mod:`openadapt_flow.seal_batch`. Those are optional.

Verification is fail closed and uses pinned keys only. The public key inside
a Seal is informational; trust comes from the key registry the verifier
pinned, in the same JSON shape as the Cloud Seal key registry
(``key_id``, ``public_key``, ``status``, ``revoked_at``). A revoked key's
Seals are refused whatever their ``issued_at`` says: the holder of a revoked
key can write any ``issued_at``, so only an independent time (a transparency
log anchor) can show a Seal predates the revocation.

By default, signing uses the local Ed25519 key of
:mod:`openadapt_flow.execute.keys` (PKCS#8 PEM, mode 0600, under
``$OPENADAPT_SEAL_HOME/keys``; default ``~/.openadapt/seal``). A production
site names a key it controls with ``OPENADAPT_SEAL_KEY``: a key file, the OS
keychain, or a PKCS#11 token (:mod:`openadapt_flow.seal_keys`). A configured
key that cannot be used stops sealing with ``key_unavailable``; it never falls
back to a generated key. Nothing here touches the network or calls a model.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Mapping, Optional, Sequence

from openadapt_types.seal import (
    SealPublicKey,
    canonical_json,
    parse_key_registry,
    seal_key_id,
)
from pydantic import BaseModel, ConfigDict, Field, StrictStr

if TYPE_CHECKING:
    from openadapt_flow.seal_anchor import AnchorVerification, LogKey
    from openadapt_flow.seal_keys import SealSigner

LOCAL_SEAL_SCHEMA: Final = "openadapt.local-seal/v1"
LOCAL_SEAL_ISSUER: Final = "local"
SEAL_FILE = "seal.json"
ENV_HOME = "OPENADAPT_SEAL_HOME"
ENV_MODE = "OPENADAPT_LOCAL_SEAL"
_DOMAIN = (LOCAL_SEAL_SCHEMA + "\0").encode("utf-8")
_SHA256_HEX = r"^[a-f0-9]{64}$"
_KEY_ID = r"^seal-ed25519-[a-f0-9]{16}$"
_ISO_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_B64 = r"^[A-Za-z0-9+/]+={0,2}$"
_OUTCOME = r"^[A-Z][A-Z_]{0,31}$"
_RUN_ID = re.compile(r"[^A-Za-z0-9._:-]")

SubjectKind = Literal["run-report", "record", "export", "file"]

LocalSealRefusal = Literal[
    "unsigned",
    "malformed",
    "subject_mismatch",
    "unknown_key",
    "revoked_key",
    "key_mismatch",
    "bad_signature",
    "ambiguous_key",
    # With an anchor required (openadapt_flow.seal_anchor):
    "anchor_missing",
    "anchor_malformed",
    "anchor_mismatch",
    "anchor_key_mismatch",
    "anchor_bad_signature",
    "anchor_unknown_log",
    "anchor_bad_timestamp",
    "anchor_bad_inclusion",
    "anchor_bad_checkpoint",
    "anchor_live_mismatch",
    "anchor_live_unavailable",
]


class LocalSealError(RuntimeError):
    """A closed reason a Seal was not written."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class LocalSealV1(BaseModel):
    """A statement about one subject, signed by a key on this machine."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt.local-seal/v1"] = Field(
        default=LOCAL_SEAL_SCHEMA, alias="schema"
    )
    issuer: Literal["local"] = LOCAL_SEAL_ISSUER
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: StrictStr = Field(pattern=_KEY_ID)
    public_key: StrictStr = Field(pattern=_B64)
    subject_kind: SubjectKind
    subject_sha256: StrictStr = Field(pattern=_SHA256_HEX)
    run_id: StrictStr = Field(min_length=1, max_length=128)
    outcome: Optional[StrictStr] = Field(default=None, pattern=_OUTCOME)
    issued_at: StrictStr = Field(pattern=_ISO_SECONDS)
    signature: StrictStr = Field(pattern=_B64)

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class LocalSealVerification:
    ok: bool
    key_id: Optional[str] = None
    reason: Optional[LocalSealRefusal] = None
    subject_file: Optional[str] = None
    anchor: Optional["AnchorVerification"] = None
    # Set with reason="revoked_key" once the signature itself has verified.
    revoked_at: Optional[str] = None


def mode_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Offline Seals are on unless ``OPENADAPT_LOCAL_SEAL`` is ``off``/``0``."""
    env = os.environ if env is None else env
    return str(env.get(ENV_MODE, "on")).strip().lower() not in {
        "off",
        "0",
        "false",
        "no",
    }


def seal_home(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    value = env.get(ENV_HOME)
    return Path(value).expanduser() if value else Path.home() / ".openadapt" / "seal"


def signer_for(
    home: Optional[Path] = None,
    *,
    spec: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> "SealSigner":
    """The Seal key to sign with: ``spec``, else ``OPENADAPT_SEAL_KEY``, else generated.

    Raises :class:`LocalSealError` (``key_unavailable`` and friends) when a
    configured key cannot be opened. Never substitutes another key.
    """
    from openadapt_flow.seal_keys import KeyUnavailable, configured_spec, resolve_signer

    try:
        chosen = spec if spec is not None else configured_spec(env)
        return resolve_signer(chosen, home=home or seal_home(env), env=env)
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc


def public_key_entry(
    home: Optional[Path] = None, *, signer: Optional["SealSigner"] = None
) -> dict[str, str]:
    """The Seal key as one key-registry entry, for a verifier to pin."""
    from openadapt_flow.seal_keys import KeyUnavailable

    try:
        return (signer or signer_for(home)).registry_entry()
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc


def signing_payload(seal: Mapping[str, Any]) -> bytes:
    unsigned = {k: v for k, v in seal.items() if k != "signature"}
    return _DOMAIN + canonical_json(unsigned).encode("utf-8")


def seal_sha256(seal: Mapping[str, Any]) -> str:
    """Digest of a whole Seal (signature included); the batch leaf value."""
    return hashlib.sha256(canonical_json(dict(seal)).encode("utf-8")).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def loads_strict(text: str | bytes) -> Any:
    """``json.loads`` that refuses an object with a repeated key.

    ``{"outcome": "FAILED", "outcome": "VERIFIED"}`` means different things
    to different parsers, so a Seal or registry written that way is malformed.
    """
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def _utc_seconds(now: Optional[datetime]) -> str:
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_subject(
    subject_kind: SubjectKind,
    subject_sha256: str,
    run_id: str,
    *,
    outcome: Optional[str] = None,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    signer: Optional["SealSigner"] = None,
) -> LocalSealV1:
    """Sign a statement about one subject digest with the configured Seal key."""
    from openadapt_flow.seal_keys import KeyUnavailable

    signer = signer or signer_for(home)
    try:
        raw = signer.public_raw()
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc
    unsigned = {
        "schema": LOCAL_SEAL_SCHEMA,
        "issuer": LOCAL_SEAL_ISSUER,
        "algorithm": "ed25519",
        "key_id": seal_key_id(raw),
        "public_key": base64.b64encode(raw).decode("ascii"),
        "subject_kind": subject_kind,
        "subject_sha256": subject_sha256,
        "run_id": run_id,
        "outcome": outcome,
        "issued_at": _utc_seconds(now),
    }
    # Validate before signing so a malformed statement is never signed.
    LocalSealV1.model_validate({**unsigned, "signature": "AA=="})
    try:
        signed = signer.sign(signing_payload(unsigned))
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc
    signature = base64.b64encode(signed).decode("ascii")
    return LocalSealV1.model_validate({**unsigned, "signature": signature})


def sign_file(
    path: Path,
    *,
    subject_kind: SubjectKind = "file",
    run_id: Optional[str] = None,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    signer: Optional["SealSigner"] = None,
) -> LocalSealV1:
    """Seal the exact bytes of ``path`` (a record, an export, an audit report)."""
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return sign_subject(
        subject_kind,
        digest,
        _safe_run_id(run_id or Path(path).name),
        home=home,
        now=now,
        signer=signer,
    )


def _safe_run_id(value: str) -> str:
    cleaned = _RUN_ID.sub("_", value)[:128]
    return cleaned or "run"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def enqueue_for_batch(seal: LocalSealV1, home: Optional[Path] = None) -> Path:
    """Queue a Seal for the next optional timestamp batch. Local disk only."""
    pending = (home or seal_home()) / "batch" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    wire = seal.wire()
    path = pending / f"{seal_sha256(wire)}.json"
    if not path.exists():
        _atomic_write_json(path, wire)
    return path


def _refuse_unredacted(report: Mapping[str, Any]) -> None:
    from openadapt_flow import report_redaction

    try:
        policy = report_redaction.load_policy()
    except report_redaction.RedactionPolicyError as exc:
        raise LocalSealError("redaction_policy_unreadable", str(exc)) from exc
    found = report_redaction.find_unredacted(report, policy)
    if found:
        from openadapt_flow.cli_hints import command

        raise LocalSealError(
            "unredacted",
            f"report.json still holds {len(found)} session token or credential "
            f"value(s): {report_redaction.describe(found)}. Run "
            f"`{command('redact-report RUN_DIR')}` and seal again, or pass "
            "--allow-unredacted to seal the report as it is",
        )


def seal_run(
    run_dir: Path,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    enqueue: bool = True,
    signer: Optional["SealSigner"] = None,
    allow_unredacted: bool = False,
) -> LocalSealV1:
    """Write ``run_dir/seal.json`` over the exact bytes of ``report.json``.

    Idempotent for an unchanged report. Refuses to overwrite a Seal that
    covers different bytes: a report that changed after it was sealed is a
    question for a person, not something to re-sign silently.

    Refuses (``unredacted``) a report that still holds a session token or
    credential (:mod:`openadapt_flow.report_redaction`), unless
    ``allow_unredacted``: a Seal makes a report shareable.
    """
    run_dir = Path(run_dir)
    report_path = run_dir / "report.json"
    try:
        raw = report_path.read_bytes()
        report = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LocalSealError("report_unreadable", str(report_path)) from exc
    if not isinstance(report, dict):
        raise LocalSealError("report_unreadable", "report.json is not an object")
    if not allow_unredacted:
        _refuse_unredacted(report)
    digest = hashlib.sha256(raw).hexdigest()
    seal_path = run_dir / SEAL_FILE
    if seal_path.exists():
        try:
            existing = LocalSealV1.model_validate(
                loads_strict(seal_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            raise LocalSealError("existing_seal_unreadable", str(seal_path)) from exc
        if existing.subject_sha256 == digest:
            return existing
        raise LocalSealError(
            "already_sealed", "seal.json covers different report bytes"
        )
    outcome = report.get("execution_outcome")
    if not (isinstance(outcome, str) and re.match(_OUTCOME, outcome)):
        outcome = None
    run_id = report.get("run_id")
    seal = sign_subject(
        "run-report",
        digest,
        _safe_run_id(run_id if isinstance(run_id, str) and run_id else run_dir.name),
        outcome=outcome,
        home=home,
        now=now,
        signer=signer,
    )
    _atomic_write_json(seal_path, seal.wire())
    if enqueue:
        enqueue_for_batch(seal, home)
    return seal


def seal_after_run(
    run_dir: Optional[Path], env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """CLI hook: seal a finished run by default. Never raises, never changes the exit code.

    Returns a one-line status for stderr, or ``None`` when there is nothing to do.
    """
    if run_dir is None or not mode_enabled(env):
        return None
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed(Path(run_dir)):
        # The offline Seal rewrites report.json in place; that is not built
        # for an encrypted run directory yet, so say so instead of skipping.
        return "offline seal not written: the run directory is encrypted at rest"
    if not (Path(run_dir) / "report.json").is_file():
        return None
    try:
        home = seal_home(env)
        seal = seal_run(Path(run_dir), home=home, signer=signer_for(home, env=env))
    except LocalSealError as exc:
        if exc.code == "unredacted":
            return f"offline seal not written: {exc}"
        return f"offline seal not written: {exc.code}"
    except Exception as exc:  # a Seal failure must never change the run's result
        return f"offline seal not written: {type(exc).__name__}"
    return f"offline seal: {Path(run_dir) / SEAL_FILE} ({seal.key_id})"


def load_registry(path: Path) -> list[SealPublicKey]:
    """Load pinned keys: a JSON list, or an object with a ``keys`` list.

    Refuses a registry that lists one key ID twice, so "active" in one entry
    can't outvote "revoked" in another.
    """
    data = loads_strict(Path(path).read_text(encoding="utf-8"))
    entries = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError('a key registry is a list of keys or {"keys": [...]}')
    keys = parse_key_registry(entries)
    ids = [k.key_id for k in keys]
    if len(set(ids)) != len(ids):
        raise ValueError("a key registry lists the same key_id more than once")
    return keys


def verify_local_seal(
    seal: Any, subject_sha256: str, registry: Sequence[SealPublicKey]
) -> LocalSealVerification:
    """Verify an offline Seal over ``subject_sha256`` against pinned keys."""
    if seal is None:
        return LocalSealVerification(False, reason="unsigned")
    try:
        parsed = (
            seal if isinstance(seal, LocalSealV1) else LocalSealV1.model_validate(seal)
        )
    except Exception:
        return LocalSealVerification(False, reason="malformed")
    if parsed.subject_sha256 != subject_sha256:
        return LocalSealVerification(False, reason="subject_mismatch")
    matches = [k for k in registry if k.key_id == parsed.key_id]
    if not matches:
        return LocalSealVerification(False, reason="unknown_key")
    if len(matches) > 1:
        return LocalSealVerification(False, reason="ambiguous_key")
    key = matches[0]
    if parsed.public_key != key.public_key:
        return LocalSealVerification(False, reason="key_mismatch")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key.public_key)).verify(
            base64.b64decode(parsed.signature), signing_payload(parsed.wire())
        )
    except (InvalidSignature, ValueError):
        return LocalSealVerification(False, reason="bad_signature")
    if key.status == "revoked":
        # issued_at is the signer's own claim; a revoked key can backdate it.
        # Only a transparency-log time before revoked_at (check_anchor) helps.
        return LocalSealVerification(
            False, key_id=key.key_id, reason="revoked_key", revoked_at=key.revoked_at
        )
    return LocalSealVerification(True, key_id=key.key_id)


def _epoch(iso_seconds: str) -> int:
    when = datetime.strptime(iso_seconds, "%Y-%m-%dT%H:%M:%SZ")
    return int(when.replace(tzinfo=timezone.utc).timestamp())


def check_anchor(
    seal: Mapping[str, Any],
    seal_path: Path,
    result: LocalSealVerification,
    registry: Sequence[SealPublicKey],
    *,
    log_keys: Optional[Sequence["LogKey"]] = None,
    log_url: Optional[str] = None,
    fetch: Any = None,
) -> LocalSealVerification:
    """Require a transparency-log anchor next to a Seal that already verified.

    The anchor must be signed by the same pinned key that verified the Seal.
    A Seal from a revoked key passes only when the log recorded it before the
    key's ``revoked_at``: the log's time is independent of the key's holder.
    ``fetch`` reads the entry back from ``log_url``, the verifier's pinned
    log, never from a URL in the anchor file.
    """
    from openadapt_flow.seal_anchor import load_anchor, verify_anchor

    revoked = result.reason == "revoked_key" and result.revoked_at is not None
    if not (result.ok or revoked):
        return result
    key = next(k for k in registry if k.key_id == result.key_id)
    anchor = verify_anchor(
        LocalSealV1.model_validate(seal).wire(),
        load_anchor(seal_path),
        base64.b64decode(key.public_key),
        log_keys=log_keys,
        log_url=log_url,
        fetch=fetch,
    )
    refused = LocalSealVerification(
        False,
        key_id=result.key_id,
        reason=anchor.reason or "revoked_key",
        subject_file=result.subject_file,
        anchor=anchor,
        revoked_at=result.revoked_at,
    )
    if not anchor.ok:
        return refused
    if revoked and not (
        anchor.integrated_time is not None
        and anchor.integrated_time < _epoch(str(result.revoked_at))
    ):
        return refused
    return LocalSealVerification(
        True, key_id=result.key_id, subject_file=result.subject_file, anchor=anchor
    )


def verify_run(
    run_dir: Path,
    registry: Sequence[SealPublicKey],
    *,
    strict: bool = False,
    require_anchor: bool = False,
    log_keys: Optional[Sequence["LogKey"]] = None,
    log_url: Optional[str] = None,
    fetch: Any = None,
) -> LocalSealVerification:
    """Verify ``run_dir/seal.json`` against the run's report.

    The Seal must match ``report.json``, or ``report.pre-seal.json`` when a
    Cloud receipt was attached afterwards (``attach-seal`` keeps the report
    as it was before attaching). ``subject_file`` names the file that
    matched. With ``strict``, only ``report.json`` counts: a run whose
    ``report.json`` changed after sealing is refused even if a
    ``report.pre-seal.json`` matches.
    With ``require_anchor``, ``seal.anchor.json`` must also prove the Seal
    was logged in the transparency log, and ``strict`` is implied.
    """
    run_dir = Path(run_dir)
    try:
        seal = loads_strict((run_dir / SEAL_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LocalSealVerification(False, reason="unsigned")
    except (OSError, ValueError):
        return LocalSealVerification(False, reason="malformed")
    result = LocalSealVerification(False, reason="subject_mismatch")
    strict = strict or require_anchor
    for name in ("report.json",) if strict else ("report.json", "report.pre-seal.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = verify_local_seal(seal, digest, registry)
        if result.reason == "subject_mismatch":
            continue
        found = dataclasses.replace(result, subject_file=name)
        if not require_anchor:
            return found
        return check_anchor(
            seal,
            run_dir / SEAL_FILE,
            found,
            registry,
            log_keys=log_keys,
            log_url=log_url,
            fetch=fetch,
        )
    return result
