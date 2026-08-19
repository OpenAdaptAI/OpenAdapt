"""Tests for the fail-closed effective-policy fetch + cache (engine.policy).

Covers the sync-contract guarantees:
    (a) network success writes the cache and returns source="network";
    (b) network failure falls back to the cache with source="cache";
    (c) no network AND no cache -> the fully-populated fail-closed default;
    (d) harden_safety fills a MISSING safety key with the safe default;
    (e) a server response that OMITS a safety key is hardened to the safe default.

httpx is monkeypatched (never hits the network); the cache path is redirected to
a tmp dir via the ``OPENADAPT_POLICY_CACHE`` override.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from engine import policy as policy_mod
from engine.config import EngineConfig
from engine.dispatch import EngineDispatcher

from .conftest import FakeResponse


def _full_policy(**overrides) -> dict:
    """A complete, well-formed server policy body."""
    body = {
        "policy_version": 7,
        "baseline_version": "2026.07",
        "org_id": "org_42",
        "resolved_at": "2026-07-20T00:00:00Z",
        "role": "admin",
        "is_admin": True,
        "user": {"theme": "dark"},
        "org": {"retention_days": 30},
        "safety": dict(policy_mod.SAFE_SAFETY_DEFAULTS),
    }
    body.update(overrides)
    return body


@pytest.fixture
def cache_path(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the policy cache to a tmp file for the duration of a test."""
    path = tmp_path / "policy.json"
    monkeypatch.setenv("OPENADAPT_POLICY_CACHE", str(path))
    monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "oai_ingest_policy_test_principal")
    return path


def _write_bound_cache(policy: dict, host: str = "https://app.openadapt.ai") -> None:
    policy_mod._write_cache(policy, host)


class TestFetchAndCache:
    def test_network_success_writes_cache_and_returns_network(
        self, cache_path: Path, monkeypatch
    ) -> None:
        body = _full_policy()
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, body)
        )
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == "network"
        assert result["policy_version"] == 7
        assert result["is_admin"] is True
        assert result["safety"] == policy_mod.SAFE_SAFETY_DEFAULTS
        # The cache uses a closed, principal-bound envelope.
        assert cache_path.exists()
        cached = json.loads(cache_path.read_text())
        assert cached["schema"] == policy_mod.CACHE_SCHEMA
        assert cached["binding"]["host_origin"] == "https://app.openadapt.ai"
        assert cached["binding"]["org_id"] == "org_42"
        assert cached["binding"]["policy_version"] == 7
        assert cached["binding"]["credential_binding_hmac"] == (
            policy_mod._credential_binding_hmac("https://app.openadapt.ai")
        )
        assert "credential_sha256" not in cached["binding"]
        assert cached["binding"]["policy_sha256"] == policy_mod._policy_sha256(
            cached["policy"]
        )
        assert cached["policy"]["policy_version"] == 7
        assert "source" not in cached["policy"]

    def test_network_failure_falls_back_to_cache(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=3))

        def _down(*a, **k):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr("engine.policy.httpx.get", _down)
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == "cache"
        assert result["policy_version"] == 3
        assert result["safety"] == policy_mod.SAFE_SAFETY_DEFAULTS

    def test_no_network_no_cache_returns_fail_closed_default(
        self, cache_path: Path, monkeypatch
    ) -> None:
        assert not cache_path.exists()

        def _down(*a, **k):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr("engine.policy.httpx.get", _down)
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == "fail-closed-default"
        assert result["is_admin"] is False
        assert result["role"] == "member"
        assert result["policy_version"] is None
        assert result["user"] == {}
        assert result["org"] == {}
        # Every safety key present at its safest value.
        assert result["safety"] == policy_mod.SAFE_SAFETY_DEFAULTS
        assert set(result["safety"]) == set(policy_mod.SAFE_SAFETY_DEFAULTS)

    def test_http_error_status_falls_back(self, cache_path: Path, monkeypatch) -> None:
        _write_bound_cache(_full_policy(policy_version=9))
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(500, {})
        )
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == "cache"
        assert result["policy_version"] == 9

    def test_fetch_raises_on_401(self, cache_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(401, {})
        )
        with pytest.raises(policy_mod.PolicyFetchError, match="401"):
            policy_mod.fetch_effective_policy("https://app.openadapt.ai")

    def test_network_policy_requires_a_monotonic_version(
        self, cache_path: Path, monkeypatch
    ) -> None:
        body = _full_policy()
        body.pop("policy_version")
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, body)
        )
        with pytest.raises(policy_mod.PolicyFetchError, match="policy version"):
            policy_mod.fetch_effective_policy("https://app.openadapt.ai")
        assert not cache_path.exists()

    def test_network_policy_cannot_move_a_bound_version_backwards(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=8))
        monkeypatch.setattr(
            "engine.policy.httpx.get",
            lambda *a, **k: FakeResponse(200, _full_policy(policy_version=7)),
        )

        with pytest.raises(policy_mod.PolicyFetchError, match="moved backwards"):
            policy_mod.fetch_effective_policy("https://app.openadapt.ai")
        assert policy_mod.load_cached_policy("https://app.openadapt.ai")[
            "policy_version"
        ] == 8

    def test_network_policy_cannot_change_without_a_new_version(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=8))
        changed = _full_policy(policy_version=8)
        changed["safety"] = {**changed["safety"], "halt_on_ambiguous": False}
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, changed)
        )

        with pytest.raises(policy_mod.PolicyFetchError, match="without a new version"):
            policy_mod.fetch_effective_policy("https://app.openadapt.ai")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("baseline_version", "2026.08"),
            ("org", {"retention_days": 7}),
            ("grounding_model", "reviewed-grounding-v2"),
        ],
    )
    def test_network_policy_authority_cannot_change_without_a_new_version(
        self, cache_path: Path, monkeypatch, field: str, value: object
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=8))
        changed = _full_policy(policy_version=8)
        changed[field] = value
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, changed)
        )

        with pytest.raises(policy_mod.PolicyFetchError, match="without a new version"):
            policy_mod.fetch_effective_policy("https://app.openadapt.ai")

    def test_same_version_allows_request_and_user_projection_changes(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=8))
        refreshed = _full_policy(
            policy_version=8,
            resolved_at="2026-07-21T00:00:00Z",
            role="member",
            is_admin=False,
            user={"theme": "light"},
        )
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, refreshed)
        )

        assert policy_mod.fetch_effective_policy("https://app.openadapt.ai") == refreshed
        assert policy_mod.load_cached_policy("https://app.openadapt.ai") == refreshed

    def test_remote_policy_host_requires_https(
        self, cache_path: Path, monkeypatch
    ) -> None:
        called = False

        def _get(*_args, **_kwargs):
            nonlocal called
            called = True
            return FakeResponse(200, _full_policy())

        monkeypatch.setattr("engine.policy.httpx.get", _get)
        with pytest.raises(policy_mod.PolicyFetchError, match="must use HTTPS"):
            policy_mod.fetch_effective_policy("http://policy.example.test")
        assert called is False

    def test_load_cached_policy_degrades_on_corrupt(
        self, cache_path: Path
    ) -> None:
        cache_path.write_text("{ not json")
        assert policy_mod.load_cached_policy("https://app.openadapt.ai") is None

    def test_offline_cache_rejects_other_host(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=3))
        monkeypatch.setattr(
            "engine.policy.httpx.get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
        )
        result = policy_mod.resolve_effective_policy("https://other.openadapt.ai")
        assert result["source"] == policy_mod.UNCONFIRMED_POLICY_SOURCE
        assert result["policy_version"] is None

    def test_offline_cache_rejects_other_credential(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=3))
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "oai_ingest_other_principal")
        monkeypatch.setattr(
            "engine.policy.httpx.get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
        )
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == policy_mod.UNCONFIRMED_POLICY_SOURCE

    def test_offline_cache_rejects_other_org(
        self, cache_path: Path, monkeypatch, fake_keyring
    ) -> None:
        from engine.auth.provider import Credential
        from engine.auth.store import store_credential

        monkeypatch.delenv("OPENADAPT_INGEST_TOKEN")
        credential: Credential = {
            "kind": "ingest_token",
            "token": "oai_ingest_org_42_token",
            "refresh_token": None,
            "org_id": "org_42",
            "host": "https://app.openadapt.ai",
            "expires_at": None,
        }
        store_credential(credential)
        _write_bound_cache(_full_policy(org_id="org_42"))
        credential["org_id"] = "org_99"
        store_credential(credential)
        monkeypatch.setattr(
            "engine.policy.httpx.get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
        )
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == policy_mod.UNCONFIRMED_POLICY_SOURCE

    def test_offline_cache_expires(
        self, cache_path: Path, monkeypatch
    ) -> None:
        _write_bound_cache(_full_policy())
        envelope = json.loads(cache_path.read_text())
        envelope["fetched_at"] = (
            datetime.now(UTC) - timedelta(seconds=policy_mod.DEFAULT_CACHE_MAX_AGE_S + 1)
        ).isoformat()
        cache_path.write_text(json.dumps(envelope))
        assert policy_mod.load_cached_policy("https://app.openadapt.ai") is None

    def test_legacy_unbound_cache_is_rejected(self, cache_path: Path) -> None:
        cache_path.write_text(json.dumps(_full_policy()))
        assert policy_mod.load_cached_policy("https://app.openadapt.ai") is None

    def test_cache_without_a_policy_version_is_rejected(self, cache_path: Path) -> None:
        policy = _full_policy()
        policy.pop("policy_version")
        cache_path.write_text(
            json.dumps(
                {
                    "schema": policy_mod.CACHE_SCHEMA,
                    "binding": {
                        "host_origin": "https://app.openadapt.ai",
                        "credential_binding_hmac": policy_mod._credential_binding_hmac(
                            "https://app.openadapt.ai"
                        ),
                        "org_id": "org_42",
                        "policy_version": None,
                        "policy_sha256": policy_mod._policy_sha256(policy),
                    },
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "policy": policy,
                }
            )
        )
        assert policy_mod.load_cached_policy("https://app.openadapt.ai") is None

    def test_cache_rejects_a_policy_body_changed_after_binding(
        self, cache_path: Path
    ) -> None:
        _write_bound_cache(_full_policy(policy_version=3))
        envelope = json.loads(cache_path.read_text())
        envelope["policy"]["safety"]["halt_on_ambiguous"] = False
        cache_path.write_text(json.dumps(envelope))

        assert policy_mod.load_cached_policy("https://app.openadapt.ai") is None

    def test_atomic_write_leaves_no_temp_files(
        self, cache_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, _full_policy())
        )
        policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        leftovers = list(cache_path.parent.glob(".policy.*.tmp"))
        assert leftovers == []


class TestHardenSafety:
    def test_fills_missing_safety_key(self) -> None:
        # A safety block missing one key must get it back at the safe default.
        partial = dict(policy_mod.SAFE_SAFETY_DEFAULTS)
        del partial["halt_on_ambiguous"]
        hardened = policy_mod.harden_safety({"safety": partial})
        assert hardened["safety"]["halt_on_ambiguous"] is True
        assert set(hardened["safety"]) == set(policy_mod.SAFE_SAFETY_DEFAULTS)

    def test_missing_safety_object_becomes_all_defaults(self) -> None:
        hardened = policy_mod.harden_safety({"user": {}})
        assert hardened["safety"] == policy_mod.SAFE_SAFETY_DEFAULTS

    def test_null_value_fails_closed(self) -> None:
        hardened = policy_mod.harden_safety(
            {"safety": {"unverified_write.allow": None}}
        )
        assert hardened["safety"]["unverified_write.allow"] is False

    def test_server_provided_value_preserved(self) -> None:
        # When the server speaks, it is authoritative -- we only fill gaps.
        hardened = policy_mod.harden_safety(
            {"safety": {"identity_gate.strictness": "medium"}}
        )
        assert hardened["safety"]["identity_gate.strictness"] == "medium"
        # ...but the untouched keys still fail closed.
        assert hardened["safety"]["halt_on_ambiguous"] is True

    def test_does_not_mutate_input(self) -> None:
        original = {"safety": {}}
        policy_mod.harden_safety(original)
        assert original == {"safety": {}}


class TestServerOmitsSafetyKeyIsFailClosed:
    def test_server_omitting_a_safety_key_is_hardened(
        self, cache_path: Path, monkeypatch
    ) -> None:
        # (e) A live server response that OMITS a safety key must be hardened to
        # the safe default, not left absent.
        body = _full_policy()
        del body["safety"]["model_calls.allowed_in_healthy_run"]
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, body)
        )
        result = policy_mod.resolve_effective_policy("https://app.openadapt.ai")
        assert result["source"] == "network"
        assert result["safety"]["model_calls.allowed_in_healthy_run"] is False
        assert set(result["safety"]) == set(policy_mod.SAFE_SAFETY_DEFAULTS)


class TestBindingSafety:
    """The gate a governed run passes before any action (fail-closed)."""

    def test_authoritative_policy_binds(self) -> None:
        safety = policy_mod.binding_safety(_full_policy(source="network"))
        assert safety == policy_mod.SAFE_SAFETY_DEFAULTS

    def test_cached_policy_binds(self) -> None:
        # Offline-but-cached is a real, authoritative posture: the org's own
        # last-known values still govern the run.
        safety = policy_mod.binding_safety(_full_policy(source="cache"))
        assert safety == policy_mod.SAFE_SAFETY_DEFAULTS

    def test_unconfirmed_policy_refuses(self) -> None:
        with pytest.raises(
            policy_mod.PolicyEnforcementError, match="no authoritative safety policy"
        ):
            policy_mod.binding_safety(
                _full_policy(source=policy_mod.UNCONFIRMED_POLICY_SOURCE)
            )

    def test_missing_safety_block_refuses(self) -> None:
        body = _full_policy(source="network")
        del body["safety"]
        with pytest.raises(policy_mod.PolicyEnforcementError, match="no safety block"):
            policy_mod.binding_safety(body)

    def test_missing_key_refuses(self) -> None:
        body = _full_policy(source="network")
        del body["safety"]["unverified_write.allow"]
        with pytest.raises(policy_mod.PolicyEnforcementError, match="is missing"):
            policy_mod.binding_safety(body)

    def test_unknown_enum_value_refuses(self) -> None:
        # harden_safety deliberately preserves whatever the server said; the
        # BINDING step is where an unknown posture must stop a run.
        body = _full_policy(source="network")
        body["safety"]["identity_gate.strictness"] = "medium"
        with pytest.raises(policy_mod.PolicyEnforcementError, match="unknown value"):
            policy_mod.binding_safety(body)

    def test_non_boolean_truthy_value_refuses(self) -> None:
        body = _full_policy(source="network")
        body["safety"]["halt_on_ambiguous"] = 1
        with pytest.raises(policy_mod.PolicyEnforcementError, match="unknown value"):
            policy_mod.binding_safety(body)

    def test_every_safe_default_is_inside_its_domain(self) -> None:
        # The fail-closed defaults must themselves be bindable, or the offline
        # path could never run at all.
        assert set(policy_mod.SAFETY_VALUE_DOMAINS) == set(
            policy_mod.SAFE_SAFETY_DEFAULTS
        )
        for key, value in policy_mod.SAFE_SAFETY_DEFAULTS.items():
            assert policy_mod._in_domain(key, value)


class TestApplySafetyPolicy:
    """Projection onto the Flow deployment config: strengthen only, never relax."""

    BASE = {"name": "d", "backend": {"kind": "web"}}

    def _apply(self, deployment: dict, **overrides) -> dict:
        safety = dict(policy_mod.SAFE_SAFETY_DEFAULTS)
        safety.update(overrides)
        return policy_mod.apply_safety_policy(deployment, safety)

    def test_pixel_verify_required_arms_the_check(self) -> None:
        bound = self._apply(
            self.BASE, **{"pixel_verify.consequential_policy": "required"}
        )
        assert bound["runtime"]["pixel_verify_enabled"] is True

    def test_pixel_verify_disabled_does_not_disarm_a_stricter_config(self) -> None:
        # `disabled` is the platform BASELINE, not a prohibition: an operator
        # who armed the check locally keeps it.
        deployment = {**self.BASE, "runtime": {"pixel_verify_enabled": True}}
        bound = self._apply(
            deployment, **{"pixel_verify.consequential_policy": "disabled"}
        )
        assert bound["runtime"]["pixel_verify_enabled"] is True

    def test_model_calls_prohibited_forces_local_grounding(self) -> None:
        deployment = {**self.BASE, "runtime": {"allow_model_grounding": True}}
        bound = self._apply(deployment)
        assert bound["runtime"]["allow_model_grounding"] is False

    def test_model_calls_permitted_is_not_an_instruction_to_enable(self) -> None:
        bound = self._apply(
            self.BASE, **{"model_calls.allowed_in_healthy_run": True}
        )
        assert "allow_model_grounding" not in bound["runtime"]

    def test_demo_profile_escalates_to_standard(self) -> None:
        bound = self._apply({**self.BASE, "runtime": {"profile": "demo"}})
        assert bound["runtime"]["profile"] == "standard"

    def test_regulated_profile_is_never_lowered(self) -> None:
        bound = self._apply({**self.BASE, "runtime": {"profile": "regulated"}})
        assert bound["runtime"]["profile"] == "regulated"

    def test_absent_profile_is_left_absent(self) -> None:
        bound = self._apply(self.BASE)
        assert "profile" not in bound["runtime"]

    def test_fully_permissive_policy_leaves_the_profile_alone(self) -> None:
        bound = self._apply(
            {**self.BASE, "runtime": {"profile": "demo"}},
            **{
                "effect_verification.required_for_consequential": False,
                "unverified_write.allow": True,
                "identity_gate.strictness": "standard",
                "halt_on_ambiguous": False,
            },
        )
        assert bound["runtime"]["profile"] == "demo"

    def test_unrankable_profile_refuses(self) -> None:
        with pytest.raises(
            policy_mod.PolicyEnforcementError, match="unrankable execution profile"
        ):
            self._apply({**self.BASE, "runtime": {"profile": "yolo"}})

    def test_non_object_runtime_refuses(self) -> None:
        with pytest.raises(policy_mod.PolicyEnforcementError, match="must be an object"):
            self._apply({**self.BASE, "runtime": "regulated"})

    def test_null_runtime_is_treated_as_empty(self) -> None:
        bound = self._apply({**self.BASE, "runtime": None})
        assert isinstance(bound["runtime"], dict)

    def test_backend_and_other_sections_survive(self) -> None:
        bound = self._apply({**self.BASE, "effects": {"kind": "fhir"}})
        assert bound["backend"] == {"kind": "web"}
        assert bound["effects"] == {"kind": "fhir"}

    def test_does_not_mutate_the_operator_config(self) -> None:
        deployment = {**self.BASE, "runtime": {"profile": "demo"}}
        self._apply(deployment)
        assert deployment["runtime"] == {"profile": "demo"}


class TestDispatcherCommand:
    def _dispatcher(self, tmp_path: Path) -> EngineDispatcher:
        config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
        return EngineDispatcher(config)

    def test_get_effective_policy_registered(self, tmp_path: Path) -> None:
        disp = self._dispatcher(tmp_path)
        assert "get_effective_policy" in disp.commands
        assert "refresh_policy" in disp.commands

    def test_get_effective_policy_never_raises_fail_closed(
        self, tmp_path: Path, cache_path: Path, monkeypatch
    ) -> None:
        def _down(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr("engine.policy.httpx.get", _down)
        disp = self._dispatcher(tmp_path)
        result = disp.dispatch("get_effective_policy", {})
        assert result["source"] == "fail-closed-default"
        assert result["safety"] == policy_mod.SAFE_SAFETY_DEFAULTS
        assert result["is_admin"] is False

    def test_get_effective_policy_network(
        self, tmp_path: Path, cache_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, _full_policy())
        )
        disp = self._dispatcher(tmp_path)
        result = disp.dispatch("get_effective_policy", {})
        assert result["source"] == "network"
        assert result["is_admin"] is True

    def test_refresh_policy_forces_fetch(
        self, tmp_path: Path, cache_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "engine.policy.httpx.get", lambda *a, **k: FakeResponse(200, _full_policy())
        )
        disp = self._dispatcher(tmp_path)
        result = disp.dispatch("refresh_policy", {})
        assert result["source"] == "network"
        assert cache_path.exists()
