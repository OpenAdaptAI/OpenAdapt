"""The outbound provider contract is tested in its owning package's CI lane."""

import json

import pytest

from openadapt_privacy.config import ScrubbingPolicyChanged, config
from openadapt_privacy.providers.presidio import PresidioScrubbingProvider


def test_sanitization_identity_tracks_complete_effective_configuration(monkeypatch):
    provider = PresidioScrubbingProvider()
    before = provider.sanitization_config()
    monkeypatch.setattr(config, "SCRUB_FILL_COLOR", config.SCRUB_FILL_COLOR ^ 0x000001)
    after = provider.sanitization_config()
    assert before["policy_sha256"] != after["policy_sha256"]
    assert before["dependencies"] == after["dependencies"]
    assert set(before) == {"policy_sha256", "dependencies"}
    json.dumps(after, allow_nan=False)


def test_sanitization_operation_refuses_transient_policy_mutation():
    provider = PresidioScrubbingProvider()
    original = config.SCRUB_FILL_COLOR
    with provider.sanitization_operation():
        with pytest.raises(ScrubbingPolicyChanged):
            config.SCRUB_FILL_COLOR = original ^ 0x000001
        assert config.SCRUB_FILL_COLOR == original
    assert config.SCRUB_FILL_COLOR == original
    assert not config.__dict__.get("_policy_locked", False)


def test_sanitization_operation_keeps_one_nested_configuration_identity():
    provider = PresidioScrubbingProvider()
    with provider.sanitization_operation():
        before = provider.sanitization_config()
        with provider.sanitization_operation():
            assert provider.sanitization_config() == before
        assert provider.sanitization_config() == before
