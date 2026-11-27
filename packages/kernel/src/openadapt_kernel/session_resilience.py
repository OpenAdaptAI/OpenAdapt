"""Session resilience: atomic Saga checkpoints, non-bypass auth detection, vault redaction, RFC 9421, and post-reauth idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

AuthGateKind = Literal["NONE", "SESSION_EXPIRED", "LOGIN_REDIRECT", "MFA_CHALLENGE", "ANTI_BOT_CHALLENGE"]
ReauthResumeAction = Literal["ALREADY_PERSISTED_SKIP_COMMIT", "SAFE_TO_RESUME_FROM_STEP", "HALT_REAUTH_FAILED"]

_ANTI_BOT_MARKERS: Tuple[str, ...] = (
    "verify you are human",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "just a moment",
    "attention required",
    "checking your browser",
    "bot verification",
    "proof of work",
    "press and hold",
)
_MFA_MARKERS: Tuple[str, ...] = (
    "two-factor",
    "two factor",
    "2fa",
    "multi-factor",
    "multifactor",
    "mfa",
    "authenticator app",
    "one-time passcode",
    "one-time password",
    "verification code",
    "enter the 6-digit code",
    "approve sign-in",
    "security key",
    "passkey",
    "duo push",
)
_SESSION_EXPIRED_MARKERS: Tuple[str, ...] = (
    "session expired",
    "session has expired",
    "session timed out",
    "please log in again",
    "please sign in again",
    "token expired",
    "unauthorized",
)
_LOGIN_PATH_HINTS: Tuple[str, ...] = ("/login", "/signin", "/sign-in", "/sso", "/oauth", "/authorize", "/saml", "/adfs")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SessionCheckpoint:
    """Durable Saga checkpoint persisted atomically before auth handoffs or long pauses."""

    workflow_id: str
    step_idx: int
    dossier: Dict[str, str] = field(default_factory=dict)
    staged_values: Dict[str, str] = field(default_factory=dict)
    ledger_state: Dict[str, Any] = field(default_factory=dict)
    last_url: str = ""
    created_at: str = field(default_factory=_utc_now)

    def save_checkpoint(self, path: Union[str, Path]) -> Path:
        return save_checkpoint(self, path)

    @classmethod
    def load_checkpoint(cls, path: Union[str, Path]) -> "SessionCheckpoint":
        return load_checkpoint(path)


def save_checkpoint(checkpoint: SessionCheckpoint, path: Union[str, Path]) -> Path:
    """Atomically persist a SessionCheckpoint via temp-file write + os.replace."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(checkpoint), indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".oa_ckpt_", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return dest


def load_checkpoint(path: Union[str, Path]) -> SessionCheckpoint:
    """Load a SessionCheckpoint from disk."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SessionCheckpoint(
        workflow_id=str(data["workflow_id"]),
        step_idx=int(data["step_idx"]),
        dossier=dict(data.get("dossier") or {}),
        staged_values=dict(data.get("staged_values") or {}),
        ledger_state=dict(data.get("ledger_state") or {}),
        last_url=str(data.get("last_url") or ""),
        created_at=str(data.get("created_at") or _utc_now()),
    )


@dataclass(frozen=True)
class AuthDetectionResult:
    """Result of auth/MFA/anti-bot gate detection with strict non-bypass invariant."""

    kind: AuthGateKind
    reason: str
    detected_url: str = ""
    bypass_permitted: bool = False

    def __post_init__(self) -> None:
        if self.bypass_permitted is not False:
            raise ValueError("bypass_permitted must always be False (strict non-bypass invariant)")


def _origin_of(url: str) -> str:
    parsed = urlparse((url or "").strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed.scheme and parsed.netloc else ""


def detect_auth_or_challenge_gate(
    *,
    status_code: int = 200,
    current_url: str = "",
    expected_origin: str = "",
    dom_text: str = "",
    challenge_markers: Sequence[str] = (),
) -> AuthDetectionResult:
    """Detect session expiry, login redirects, MFA prompts, or anti-bot challenges without bypassing."""
    url_low = (current_url or "").strip().lower()
    dom_low = " ".join((dom_text or "").lower().split())
    combined = f"{url_low} {dom_low}"

    custom_hits = [m for m in challenge_markers if str(m).strip() and str(m).strip().lower() in combined]
    if custom_hits:
        return AuthDetectionResult("ANTI_BOT_CHALLENGE", f"challenge_marker:{custom_hits[0]}", current_url)
    for marker in _ANTI_BOT_MARKERS:
        if marker in combined:
            return AuthDetectionResult("ANTI_BOT_CHALLENGE", f"anti_bot_marker:{marker}", current_url)

    for marker in _MFA_MARKERS:
        if marker in combined:
            return AuthDetectionResult("MFA_CHALLENGE", f"mfa_marker:{marker}", current_url)

    if status_code in (401, 403, 419, 440):
        return AuthDetectionResult("SESSION_EXPIRED", f"http_{status_code}", current_url)
    for marker in _SESSION_EXPIRED_MARKERS:
        if marker in dom_low:
            return AuthDetectionResult("SESSION_EXPIRED", f"session_marker:{marker}", current_url)

    parsed_path = urlparse(url_low).path
    has_login_path = any(hint in parsed_path for hint in _LOGIN_PATH_HINTS)
    exp_orig = _origin_of(expected_origin)
    cur_orig = _origin_of(current_url)
    cross_origin_login = bool(exp_orig and cur_orig and exp_orig != cur_orig and (has_login_path or "sign in" in dom_low or "log in" in dom_low))
    if status_code in (301, 302, 303, 307, 308) or has_login_path or cross_origin_login:
        reason = f"http_{status_code}_redirect" if status_code in (301, 302, 303, 307, 308) else "login_route_detected"
        return AuthDetectionResult("LOGIN_REDIRECT", reason, current_url)

    return AuthDetectionResult("NONE", "clear", current_url)


@dataclass(frozen=True)
class AuthHandoffCard:
    """PrepareCard-compatible human auth handoff request."""

    card_id: str
    gate_kind: AuthGateKind
    reason: str
    live_url: str
    checkpoint_path: str
    decision: Literal["HALT_FOR_AUTH_HANDOFF"] = "HALT_FOR_AUTH_HANDOFF"
    supports_ciba_push: bool = False
    supports_webauthn_passkey: bool = False
    operator_instruction: str = ""


def build_auth_handoff_card(
    detection: AuthDetectionResult,
    checkpoint: SessionCheckpoint,
    *,
    checkpoint_path: Union[str, Path],
    card_id: str = "",
    supports_ciba_push: bool = False,
    supports_webauthn_passkey: bool = False,
    operator_instruction: str = "",
) -> AuthHandoffCard:
    """Persist checkpoint atomically and emit a HALT_FOR_AUTH_HANDOFF card for human completion."""
    saved = save_checkpoint(checkpoint, checkpoint_path)
    default_instructions: Dict[AuthGateKind, str] = {
        "ANTI_BOT_CHALLENGE": "Complete the human verification directly in the visible browser window, then resume.",
        "MFA_CHALLENGE": "Approve the MFA prompt or passkey/CIBA push in the visible browser window, then resume.",
        "SESSION_EXPIRED": "Re-authenticate the expired session in the visible browser window, then resume.",
        "LOGIN_REDIRECT": "Complete SSO/login in the visible browser window, then resume.",
        "NONE": "Confirm session state before resuming.",
    }
    return AuthHandoffCard(
        card_id=card_id or f"auth-handoff-{checkpoint.workflow_id}-step-{checkpoint.step_idx}",
        gate_kind=detection.kind,
        reason=detection.reason,
        live_url=detection.detected_url or checkpoint.last_url,
        checkpoint_path=str(saved),
        supports_ciba_push=supports_ciba_push,
        supports_webauthn_passkey=supports_webauthn_passkey,
        operator_instruction=operator_instruction or default_instructions[detection.kind],
    )


class VaultCredentialProvider(Protocol):
    def get_secret(self, secret_ref: str) -> str: ...

    def get_totp_code(self, secret_ref: str) -> str: ...


def _generate_rfc6238_totp(base32_seed: str, *, for_time: Optional[int] = None, step_s: int = 30, digits: int = 6) -> str:
    cleaned = re.sub(r"\s+", "", base32_seed).upper()
    pad = "=" * ((8 - len(cleaned) % 8) % 8)
    key = base64.b32decode(cleaned + pad, casefold=True)
    counter = int((for_time if for_time is not None else time.time()) // step_s)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


@dataclass
class EnvVaultAdapter:
    """Environment-backed vault adapter (supports OA_VAULT_* lookups and RFC 6238 TOTP seeds)."""

    prefix: str = "OA_VAULT_"
    environ: Optional[Mapping[str, str]] = None

    def _env(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def get_secret(self, secret_ref: str) -> str:
        env = self._env()
        norm = re.sub(r"[^A-Za-z0-9_]+", "_", secret_ref.strip()).upper()
        for key in (f"{self.prefix}{norm}", secret_ref.strip(), norm):
            if key in env and env[key]:
                return env[key]
        raise KeyError(f"Secret reference not found in vault: {secret_ref!r}")

    def get_totp_code(self, secret_ref: str) -> str:
        raw = self.get_secret(secret_ref)
        if re.fullmatch(r"\d{6,8}", raw.strip()):
            return raw.strip()
        return _generate_rfc6238_totp(raw)


@dataclass
class SubprocessVaultAdapter:
    """Pluggable CLI vault adapter (e.g., 1Password, Bitwarden, OS Keychain) with scrubbed errors."""

    command_template: Sequence[str]
    totp_command_template: Sequence[str] = ()
    timeout_s: float = 10.0

    def _run(self, template: Sequence[str], secret_ref: str) -> str:
        cmd = [part.replace("{ref}", secret_ref) for part in template]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=self.timeout_s)
        out = proc.stdout.strip()
        if proc.returncode != 0 or not out:
            raise RuntimeError(f"Vault CLI failed for ref {secret_ref!r} (exit={proc.returncode})")
        return out

    def get_secret(self, secret_ref: str) -> str:
        return self._run(self.command_template, secret_ref)

    def get_totp_code(self, secret_ref: str) -> str:
        if self.totp_command_template:
            return self._run(self.totp_command_template, secret_ref)
        raw = self.get_secret(secret_ref)
        return raw if re.fullmatch(r"\d{6,8}", raw) else _generate_rfc6238_totp(raw)


def inject_vault_secret_redacted(
    secret_ref: str,
    vault: VaultCredentialProvider,
    sink_fn: Callable[[str], Any],
    *,
    is_totp: bool = False,
    audit_log: Optional[List[str]] = None,
) -> str:
    """Inject a vault secret directly into sink_fn while guaranteeing zero plaintext in logs or exceptions."""
    redacted = f"[REDACTED_VAULT_REF:{secret_ref}]"
    secret = vault.get_totp_code(secret_ref) if is_totp else vault.get_secret(secret_ref)
    try:
        sink_fn(secret)
    except Exception as exc:
        scrubbed_msg = str(exc).replace(secret, redacted) if secret else str(exc)
        if audit_log is not None:
            audit_log.append(f"vault_inject_error:{redacted}:{type(exc).__name__}")
        raise RuntimeError(f"Vault injection failed for {redacted}: {scrubbed_msg}") from None
    if audit_log is not None:
        audit_log.append(f"vault_injected:{redacted}")
    return redacted


@dataclass(frozen=True)
class SessionKeepAliveConfig:
    ping_url: str
    interval_s: float = 300.0
    max_session_duration_s: float = 28800.0
    method: Literal["GET", "HEAD"] = "HEAD"

    def __post_init__(self) -> None:
        if self.method not in ("GET", "HEAD"):
            raise ValueError(f"SessionKeepAliveConfig only permits read-only GET/HEAD, got {self.method!r}")
        if self.interval_s <= 0 or self.max_session_duration_s <= 0:
            raise ValueError("interval_s and max_session_duration_s must be positive")


def keep_session_alive_readonly(
    config: SessionKeepAliveConfig,
    *,
    request_fn: Callable[[str, str], int],
    elapsed_s: float = 0.0,
) -> Dict[str, Any]:
    """Perform a single strictly read-only GET/HEAD keep-alive ping within max_session_duration_s."""
    if config.method not in ("GET", "HEAD"):
        raise ValueError(f"Non-readonly keep-alive method rejected: {config.method!r}")
    if elapsed_s >= config.max_session_duration_s:
        return {"ok": False, "reason": "max_session_duration_exceeded", "elapsed_s": elapsed_s}
    status = int(request_fn(config.method, config.ping_url))
    return {
        "ok": 200 <= status < 400,
        "status_code": status,
        "method": config.method,
        "ping_url": config.ping_url,
    }


def _rfc9421_signature_base(method: str, target_url: str, sig_params: str) -> bytes:
    parsed = urlparse((target_url or "").strip())
    authority = parsed.netloc.lower()
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    lines = [
        f'"@method": {method.strip().upper()}',
        f'"@authority": {authority}',
        f'"@path": {path}',
        f'"@signature-params": {sig_params}',
    ]
    return "\n".join(lines).encode("utf-8")


def build_web_bot_auth_headers(
    method: str,
    target_url: str,
    *,
    private_key_pem: bytes,
    key_id: str,
    created_ts: Optional[int] = None,
    expires_in_s: int = 300,
    nonce: str = "",
) -> Dict[str, str]:
    """Build RFC 9421 Ed25519 HTTP Message Signature headers for sanctioned verified-bot attestation."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("build_web_bot_auth_headers requires an Ed25519 private key")
    created = int(created_ts if created_ts is not None else time.time())
    expires = created + int(expires_in_s)
    params = f'("@method" "@authority" "@path");created={created};expires={expires};keyid="{key_id}";alg="ed25519";tag="web-bot-auth"'
    if nonce:
        params = f'{params};nonce="{nonce}"'
    sig_bytes = key.sign(_rfc9421_signature_base(method, target_url, params))
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
    return {
        "Signature-Input": f"sig1={params}",
        "Signature": f"sig1=:{sig_b64}:",
        "Signature-Agent": f'"{key_id}"',
    }


def verify_web_bot_auth_headers(
    method: str,
    target_url: str,
    headers: Mapping[str, str],
    *,
    public_key_pem: bytes,
    now_ts: Optional[int] = None,
    max_clock_skew_s: int = 60,
) -> bool:
    """Verify RFC 9421 Ed25519 HTTP Message Signature headers and timestamp validity."""
    norm_headers = {k.lower(): v for k, v in headers.items()}
    sig_input = norm_headers.get("signature-input", "")
    sig_header = norm_headers.get("signature", "")
    m_in = re.match(r"^sig1=(.+)$", sig_input.strip())
    m_sig = re.match(r"^sig1=:([A-Za-z0-9+/=]+):$", sig_header.strip())
    if not m_in or not m_sig:
        return False
    params = m_in.group(1).strip()
    m_exp = re.search(r";expires=(\d+)", params)
    m_crt = re.search(r";created=(\d+)", params)
    if not m_exp or not m_crt:
        return False
    now = int(now_ts if now_ts is not None else time.time())
    if now > int(m_exp.group(1)) + max_clock_skew_s or now < int(m_crt.group(1)) - max_clock_skew_s:
        return False
    try:
        pub = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(base64.b64decode(m_sig.group(1)), _rfc9421_signature_base(method, target_url, params))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True)
class ReauthResumeDecision:
    action: ReauthResumeAction
    resume_step_idx: int
    reason: str
    duplicate_prevented: bool = False


class SessionResilienceGuard:
    """Post-reauth idempotency guard that probes read-back before resuming to prevent double writes."""

    def resume_after_reauth(
        self,
        checkpoint: SessionCheckpoint,
        *,
        auth_verified: bool,
        readback_probe_fn: Callable[[SessionCheckpoint], bool],
    ) -> ReauthResumeDecision:
        if not auth_verified:
            return ReauthResumeDecision("HALT_REAUTH_FAILED", checkpoint.step_idx, "reauth_not_verified")
        if bool(readback_probe_fn(checkpoint)):
            return ReauthResumeDecision(
                "ALREADY_PERSISTED_SKIP_COMMIT",
                checkpoint.step_idx + 1,
                "interrupted_commit_already_persisted_on_readback",
                duplicate_prevented=True,
            )
        return ReauthResumeDecision(
            "SAFE_TO_RESUME_FROM_STEP",
            checkpoint.step_idx,
            "readback_confirmed_not_persisted_safe_to_resume",
            duplicate_prevented=False,
        )


@dataclass(frozen=True)
class BatchSagaCheckpoint:
    """Multi-item batch Saga checkpoint for exact-once recovery across mid-batch session drops."""

    batch_id: str
    items: Tuple[str, ...]
    committed_item_ids: Tuple[str, ...] = ()
    current_item_idx: int = 0

    @property
    def remaining_items(self) -> Tuple[str, ...]:
        done = set(self.committed_item_ids)
        return tuple(item for item in self.items if item not in done)

    @property
    def is_complete(self) -> bool:
        return not self.remaining_items


def reconcile_batch_after_reauth(
    checkpoint: BatchSagaCheckpoint,
    ui_readback_present_fn: Callable[[str], bool],
) -> BatchSagaCheckpoint:
    """Probe uncommitted batch items via read-only UI read-back after reauth and return exact remaining slice."""
    committed_set = set(checkpoint.committed_item_ids)
    committed_list = list(checkpoint.committed_item_ids)
    for item_id in checkpoint.items:
        if item_id in committed_set:
            continue
        if bool(ui_readback_present_fn(item_id)):
            committed_set.add(item_id)
            committed_list.append(item_id)
    next_idx = next(
        (i for i, item_id in enumerate(checkpoint.items) if item_id not in committed_set),
        len(checkpoint.items),
    )
    return BatchSagaCheckpoint(
        batch_id=checkpoint.batch_id,
        items=tuple(checkpoint.items),
        committed_item_ids=tuple(committed_list),
        current_item_idx=next_idx,
    )
