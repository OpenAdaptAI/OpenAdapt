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
    return path


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
        # Cache was written with the raw body (no source field).
        assert cache_path.exists()
        cached = json.loads(cache_path.read_text())
        assert cached["policy_version"] == 7
        assert "source" not in cached

    def test_network_failure_falls_back_to_cache(
        self, cache_path: Path, monkeypatch
    ) -> None:
        cache_path.write_text(json.dumps(_full_policy(policy_version=3)))

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
        cache_path.write_text(json.dumps(_full_policy(policy_version=9)))
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

    def test_load_cached_policy_degrades_on_corrupt(
        self, cache_path: Path
    ) -> None:
        cache_path.write_text("{ not json")
        assert policy_mod.load_cached_policy() is None

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
