"""Unit tests for session resilience: atomic checkpoints, non-bypass auth detection, vault redaction, RFC 9421, and post-reauth idempotency."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

from openadapt_kernel.copilot import generate_ephemeral_ed25519_keypair
from openadapt_kernel.session_resilience import (
    AuthDetectionResult,
    EnvVaultAdapter,
    SessionCheckpoint,
    SessionKeepAliveConfig,
    SessionResilienceGuard,
    SubprocessVaultAdapter,
    build_auth_handoff_card,
    build_web_bot_auth_headers,
    detect_auth_or_challenge_gate,
    inject_vault_secret_redacted,
    keep_session_alive_readonly,
    load_checkpoint,
    save_checkpoint,
    verify_web_bot_auth_headers,
)


def test_atomic_saga_checkpoint_save_and_load(tmp_path: Path) -> None:
    ckpt_path = tmp_path / "checkpoints" / "wf_101.json"
    ckpt = SessionCheckpoint(
        workflow_id="wf_101",
        step_idx=4,
        dossier={"invoice_id": "INV-9001", "amount": "$420.00"},
        staged_values={"invoice_field": "INV-9001"},
        ledger_state={"status": "UNVERIFIED", "pending": ["commit_invoice"]},
        last_url="https://erp.example/invoices/INV-9001",
    )
    saved = save_checkpoint(ckpt, ckpt_path)
    assert saved.exists()
    loaded = load_checkpoint(saved)
    assert loaded.workflow_id == "wf_101"
    assert loaded.step_idx == 4
    assert loaded.dossier == ckpt.dossier
    assert loaded.staged_values == ckpt.staged_values
    assert loaded.ledger_state == ckpt.ledger_state
    assert loaded.last_url == ckpt.last_url


def test_auth_mfa_antibot_detection_and_strict_non_bypass(tmp_path: Path) -> None:
    # Strict non-bypass invariant: bypass_permitted=True is structurally forbidden
    with pytest.raises(ValueError, match="bypass_permitted"):
        AuthDetectionResult("ANTI_BOT_CHALLENGE", "captcha", bypass_permitted=True)

    r_401 = detect_auth_or_challenge_gate(status_code=401, current_url="https://portal.example/api/save")
    assert r_401.kind == "SESSION_EXPIRED"
    assert r_401.bypass_permitted is False

    r_302 = detect_auth_or_challenge_gate(
        status_code=302,
        current_url="https://sso.example/login?next=/claims",
        expected_origin="https://portal.example",
    )
    assert r_302.kind == "LOGIN_REDIRECT"
    assert r_302.bypass_permitted is False

    r_mfa = detect_auth_or_challenge_gate(
        status_code=200,
        current_url="https://sso.example/verify",
        dom_text="Please enter the 6-digit code from your authenticator app.",
    )
    assert r_mfa.kind == "MFA_CHALLENGE"
    assert r_mfa.bypass_permitted is False

    r_bot = detect_auth_or_challenge_gate(
        status_code=200,
        current_url="https://portal.example/checkout",
        dom_text="Just a moment... Verify you are human via Cloudflare Turnstile.",
    )
    assert r_bot.kind == "ANTI_BOT_CHALLENGE"
    assert r_bot.bypass_permitted is False

    r_clear = detect_auth_or_challenge_gate(
        status_code=200,
        current_url="https://portal.example/orders/55",
        expected_origin="https://portal.example",
        dom_text="Order #55 details ready for review.",
    )
    assert r_clear.kind == "NONE"

    # Handoff card emits HALT_FOR_AUTH_HANDOFF and persists checkpoint atomically
    ckpt = SessionCheckpoint("wf_auth", step_idx=3, last_url="https://portal.example/orders/55")
    card = build_auth_handoff_card(
        r_mfa,
        ckpt,
        checkpoint_path=tmp_path / "auth_ckpt.json",
        supports_ciba_push=True,
        supports_webauthn_passkey=True,
    )
    assert card.decision == "HALT_FOR_AUTH_HANDOFF"
    assert card.gate_kind == "MFA_CHALLENGE"
    assert card.supports_ciba_push is True
    assert card.supports_webauthn_passkey is True
    assert Path(card.checkpoint_path).exists()


def test_vault_zero_leakage_and_readonly_keepalive() -> None:
    secret_val = "SuperSecretProdPassword!9988"
    totp_seed = "JBSWY3DPEHPK3PXP"
    vault = EnvVaultAdapter(environ={"OA_VAULT_PORTAL_PASS": secret_val, "OA_VAULT_PORTAL_TOTP": totp_seed})

    captured_sink: List[str] = []
    audit_log: List[str] = []
    token = inject_vault_secret_redacted("portal_pass", vault, captured_sink.append, audit_log=audit_log)
    assert captured_sink == [secret_val]
    assert token == "[REDACTED_VAULT_REF:portal_pass]"
    assert all(secret_val not in entry for entry in audit_log)

    # TOTP injection generates a 6-digit code and logs zero raw digits/seed
    totp_sink: List[str] = []
    totp_token = inject_vault_secret_redacted("portal_totp", vault, totp_sink.append, is_totp=True, audit_log=audit_log)
    assert len(totp_sink[0]) == 6 and totp_sink[0].isdigit()
    assert totp_token == "[REDACTED_VAULT_REF:portal_totp]"
    assert all(totp_seed not in entry and totp_sink[0] not in entry for entry in audit_log)

    # Exceptions from sink_fn are scrubbed so raw secrets never leak in tracebacks/logs
    def failing_sink(val: str) -> None:
        raise ValueError(f"Element rejected input {val}")

    with pytest.raises(RuntimeError) as exc_info:
        inject_vault_secret_redacted("portal_pass", vault, failing_sink, audit_log=audit_log)
    assert secret_val not in str(exc_info.value)
    assert "[REDACTED_VAULT_REF:portal_pass]" in str(exc_info.value)

    # SubprocessVaultAdapter works with a pluggable CLI command
    sub_vault = SubprocessVaultAdapter(command_template=[sys.executable, "-c", "print('cli_secret_42')"])
    assert sub_vault.get_secret("any_ref") == "cli_secret_42"

    # Keep-alive enforces strictly read-only GET/HEAD and max_session_duration_s
    with pytest.raises(ValueError, match="read-only"):
        SessionKeepAliveConfig(ping_url="https://portal.example/ping", method="POST")  # type: ignore[arg-type]

    cfg = SessionKeepAliveConfig(ping_url="https://portal.example/health", method="HEAD", max_session_duration_s=3600.0)
    ok_ping = keep_session_alive_readonly(cfg, request_fn=lambda m, _u: 200 if m == "HEAD" else 405, elapsed_s=120.0)
    assert ok_ping["ok"] is True
    assert ok_ping["status_code"] == 200

    expired_ping = keep_session_alive_readonly(cfg, request_fn=lambda _m, _u: 200, elapsed_s=3600.0)
    assert expired_ping["ok"] is False
    assert expired_ping["reason"] == "max_session_duration_exceeded"


def test_rfc9421_web_bot_auth_ed25519_roundtrip() -> None:
    priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
    ts = 1_750_000_000
    headers = build_web_bot_auth_headers(
        "POST",
        "https://portal.example/api/v1/claims?mode=verify",
        private_key_pem=priv_pem,
        key_id="oa-agent-key-1",
        created_ts=ts,
        expires_in_s=300,
        nonce="n-1234",
    )
    assert headers["Signature-Input"].startswith('sig1=("@method" "@authority" "@path");')
    assert headers["Signature"].startswith("sig1=:")
    assert verify_web_bot_auth_headers(
        "POST",
        "https://portal.example/api/v1/claims?mode=verify",
        headers,
        public_key_pem=pub_pem,
        now_ts=ts + 10,
    ) is True

    # Tampered URL or expired timestamp fails verification
    assert verify_web_bot_auth_headers(
        "POST",
        "https://portal.example/api/v1/claims?mode=tampered",
        headers,
        public_key_pem=pub_pem,
        now_ts=ts + 10,
    ) is False
    assert verify_web_bot_auth_headers(
        "POST",
        "https://portal.example/api/v1/claims?mode=verify",
        headers,
        public_key_pem=pub_pem,
        now_ts=ts + 1000,
    ) is False


def test_post_reauth_idempotency_guard_prevents_double_billing() -> None:
    guard = SessionResilienceGuard()
    ckpt = SessionCheckpoint(
        workflow_id="bill_claim_77",
        step_idx=5,
        dossier={"claim_id": "CLM-77", "amount": "$1,500.00"},
        last_url="https://portal.example/claims/CLM-77",
    )

    # Unverified re-auth halts cleanly
    halted = guard.resume_after_reauth(ckpt, auth_verified=False, readback_probe_fn=lambda _c: False)
    assert halted.action == "HALT_REAUTH_FAILED"
    assert halted.duplicate_prevented is False

    # Interrupted submit already landed before disconnect -> skip commit, zero double writes!
    skipped = guard.resume_after_reauth(ckpt, auth_verified=True, readback_probe_fn=lambda _c: True)
    assert skipped.action == "ALREADY_PERSISTED_SKIP_COMMIT"
    assert skipped.resume_step_idx == 6
    assert skipped.duplicate_prevented is True

    # Read-back confirms commit never landed -> safe to resume at step 5
    resumed = guard.resume_after_reauth(ckpt, auth_verified=True, readback_probe_fn=lambda _c: False)
    assert resumed.action == "SAFE_TO_RESUME_FROM_STEP"
    assert resumed.resume_step_idx == 5
    assert resumed.duplicate_prevented is False
