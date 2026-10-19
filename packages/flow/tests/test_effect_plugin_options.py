"""Plugin record-check packs: reviewed options and secret slots in deployment.yaml.

A per-system record check ships as a registered verifier kind. These tests pin
how a deployment configures one without a credential ever entering the config:
``options`` carry non-secret settings, ``option_params`` bind run parameters,
``secret_env`` names the environment variables that hold credentials, and the
built-in kinds refuse all three instead of silently ignoring them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from openadapt_flow.deployment import (
    BUILTIN_EFFECT_KINDS,
    EffectsConfig,
    build_effect_verifier,
    load_deployment,
    plugin_options,
    plugin_secrets,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    ResolvedSecrets,
    ValueExpr,
    VerifierAdapterBase,
    poll_until_settled,
    register_verifier_factory,
)
from openadapt_flow.runtime.effects import adapter as adapter_mod
from openadapt_flow.runtime.effects.adapter import verifier_identity
from openadapt_flow.verification import VerificationTier


class _PackVerifier(VerifierAdapterBase):
    """A minimal pack: records come from an in-memory 'system of record'."""

    substrate = "synthetic-pack"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, options: dict[str, Any], secrets: ResolvedSecrets) -> None:
        self.options = options
        self._secrets = secrets
        self.store: list[dict[str, Any]] = []

    def _fetch(self):
        if self._secrets["password"] != "reader-pass":
            return None
        return [r for r in self.store if r.get("member") == self.options["member"]]

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
        )

    def verify(self, expected, before, context: Any = None):
        return poll_until_settled(
            self._fetch,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=0.01,
        )


BUILT: list[_PackVerifier] = []


def _factory(cfg, params=None):
    verifier = _PackVerifier(plugin_options(cfg, params), plugin_secrets(cfg))
    BUILT.append(verifier)
    return verifier


@pytest.fixture()
def pack(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_REGISTRY", {})
    monkeypatch.setattr(adapter_mod, "_ENTRY_POINTS_LOADED", False)
    register_verifier_factory("synthetic-pack", _factory)
    monkeypatch.setenv("PACK_READER_USER", "reader")
    monkeypatch.setenv("PACK_READER_PASSWORD", "reader-pass")
    BUILT.clear()
    yield


def _pack_config(**overrides) -> EffectsConfig:
    raw = {
        "kind": "synthetic-pack",
        "base_url": "http://127.0.0.1:9",
        "options": {"records": {"path": "/records", "fields": ["member", "note"]}},
        "option_params": {"member": {"param": "member_no"}},
        "secret_env": {
            "username": "PACK_READER_USER",
            "password": "PACK_READER_PASSWORD",
        },
    }
    raw.update(overrides)
    return EffectsConfig.model_validate(raw)


def test_yaml_deployment_builds_a_plugin_with_options_and_secrets(pack, tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(
        "effects:\n"
        "  kind: synthetic-pack\n"
        "  options:\n"
        "    records: {path: /records}\n"
        "  option_params:\n"
        "    member: {param: member_no}\n"
        "  secret_env:\n"
        "    username: PACK_READER_USER\n"
        "    password: PACK_READER_PASSWORD\n"
    )
    deployment = load_deployment(path)
    verifier = build_effect_verifier(deployment.effects, {"member_no": "100200300"})
    assert verifier is BUILT[-1]
    assert verifier.options == {"records": {"path": "/records"}, "member": "100200300"}
    verifier.store.append({"member": "100200300", "note": "first visit"})
    before = EffectState(substrate="synthetic-pack", reachable=True, records=[])
    verdict = verifier.verify(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"member": ValueExpr(literal="100200300")},
            field="note",
            value=ValueExpr(literal="first visit"),
        ),
        before,
    )
    assert verdict.confirmed


def test_options_are_copied_so_a_factory_cannot_mutate_the_config(pack):
    cfg = _pack_config()
    resolved = plugin_options(cfg, {"member_no": "1"})
    resolved["records"]["path"] = "/other"
    assert cfg.options["records"]["path"] == "/records"


def test_unresolved_option_param_fails_loud(pack):
    with pytest.raises(ValueError, match="member_no"):
        build_effect_verifier(_pack_config(), {})


def test_missing_secret_fails_loud_and_names_only_the_variable(pack, monkeypatch):
    monkeypatch.delenv("PACK_READER_PASSWORD")
    with pytest.raises(ValueError) as excinfo:
        build_effect_verifier(_pack_config(), {"member_no": "1"})
    assert "PACK_READER_PASSWORD" in str(excinfo.value)
    assert "reader-pass" not in str(excinfo.value)


def test_resolved_secrets_never_print_their_values():
    secrets = ResolvedSecrets.from_env(
        {"password": "X_PASS"}, env={"X_PASS": "hunter2"}
    )
    assert secrets["password"] == "hunter2"
    assert "hunter2" not in repr(secrets)
    assert "hunter2" not in str(secrets)
    assert "hunter2" not in f"{secrets}"
    with pytest.raises(ValueError, match="X_OTHER"):
        ResolvedSecrets.from_env({"token": "X_OTHER"}, env={})


@pytest.mark.parametrize(
    "options",
    [
        {"password": "hunter2"},
        {"auth": {"client_secret": "abc"}},
        {"items": [{"api_key": "abc"}]},
        {"bearer_token": "abc"},
        {"Client-Secret": "abc"},
        {"auth": {"private_key": "abc"}},
        {"credentials": {"user": "a"}},
    ],
)
def test_credential_named_option_keys_are_refused(options):
    with pytest.raises(ValueError, match="secret_env"):
        EffectsConfig(kind="synthetic-pack", options=options)


@pytest.mark.parametrize(
    "options",
    [
        {"password_env": "X_PASS"},
        {"token_path": "/oauth2/default/token"},
        {"secret_store": {"kind": "vault"}},
        {"idempotency_key_field": "email"},
    ],
)
def test_keys_that_do_not_hold_a_credential_are_allowed(options):
    cfg = EffectsConfig(kind="synthetic-pack", options=options)
    assert cfg.options == options


def test_non_json_option_values_are_refused():
    with pytest.raises(ValueError, match="JSON"):
        EffectsConfig(kind="synthetic-pack", options={"when": object()})


@pytest.mark.parametrize("value", ["hunter 2", "p@ss:word", "", "1ABC"])
def test_secret_env_holds_variable_names_not_values(value):
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        EffectsConfig(kind="synthetic-pack", secret_env={"password": value})


def test_secret_env_slots_are_identifiers():
    with pytest.raises(ValueError, match="slot"):
        EffectsConfig(kind="synthetic-pack", secret_env={"Pass Word": "X_PASS"})


@pytest.mark.parametrize("kind", sorted(BUILTIN_EFFECT_KINDS - {""}))
@pytest.mark.parametrize(
    "field,value",
    [
        ("options", {"path": "/records"}),
        ("option_params", {"member": {"param": "member_no"}}),
        ("secret_env", {"password": "X_PASS"}),
    ],
)
def test_builtin_kinds_refuse_plugin_fields(kind, field, value):
    with pytest.raises(ValueError, match="registered plugin kind"):
        EffectsConfig.model_validate({"kind": kind, field: value})


def test_option_params_may_not_shadow_options():
    with pytest.raises(ValueError, match="must not repeat"):
        EffectsConfig(
            kind="synthetic-pack",
            options={"member": "x"},
            option_params={"member": {"param": "member_no"}},
        )


def test_changed_options_change_the_verifier_identity(pack):
    first = build_effect_verifier(_pack_config(), {"member_no": "1"})
    second = build_effect_verifier(
        _pack_config(options={"records": {"path": "/elsewhere"}}), {"member_no": "1"}
    )
    assert verifier_identity(first) != verifier_identity(second)


def test_identity_of_configs_without_plugin_fields_is_unchanged():
    """Existing deployments keep a byte-identical verifier identity."""
    cfg = EffectsConfig(kind="rest", base_url="http://127.0.0.1:9")
    verifier = build_effect_verifier(cfg)
    legacy_config = cfg.model_dump(mode="json")
    # Neither the plugin fields, ui_readback, smart_backend, surfaces nor
    # records_drop_fields existed when identities were first issued.
    for name in (
        "options",
        "option_params",
        "secret_env",
        "ui_readback",
        "smart_backend",
        "surfaces",
        "records_drop_fields",
    ):
        legacy_config.pop(name, None)
    legacy_config["candidates"] = []

    def sanitized(value, *, key=""):  # the identity's own redaction, verbatim
        lowered = key.lower()
        if not lowered.endswith("_env") and any(
            marker in lowered
            for marker in ("credential", "password", "secret", "token")
        ):
            return "[secret-reference]"
        if isinstance(value, dict):
            return {str(k): sanitized(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitized(item) for item in value]
        return value

    payload = {
        "schema": "openadapt.effect-verifier-identity/v1",
        "implementation": f"{type(verifier).__module__}.{type(verifier).__qualname__}",
        "implementation_version": getattr(verifier, "adapter_version", None),
        "substrate": verifier.substrate,
        "config": sanitized(legacy_config),
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert verifier_identity(verifier) == f"sha256:{expected}"


def test_plugin_candidate_with_options_builds(pack):
    cfg = EffectsConfig.model_validate(
        {
            "candidates": [
                {
                    "kind": "synthetic-pack",
                    "options": {"records": {"path": "/records"}},
                    "option_params": {"member": {"param": "member_no"}},
                    "secret_env": {"password": "PACK_READER_PASSWORD"},
                }
            ]
        }
    )
    verifier = build_effect_verifier(cfg, {"member_no": "7"})
    assert verifier is not None
    assert BUILT[-1].options["member"] == "7"
