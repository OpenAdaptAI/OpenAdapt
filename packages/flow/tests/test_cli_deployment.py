"""Deployment config (``openadapt_flow.deployment``): load + build verifiers.

One ``deployment.yaml`` configures backend / actuation / effects / runtime /
policy for the whole CLI. These tests pin the schema, the shipped example, and
the object-construction seams the CLI injects — no browser, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.deployment import (
    ActuationConfig,
    DeploymentConfig,
    EffectsConfig,
    build_api_actuator,
    build_effect_verifier,
    load_deployment,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "deployment.example.yaml"


def test_shipped_example_loads_and_is_coherent() -> None:
    cfg = load_deployment(EXAMPLE)
    assert isinstance(cfg, DeploymentConfig)
    assert cfg.effects.kind == "rest"
    assert cfg.runtime.durable is True
    assert cfg.policy.policy == "clinical-write"
    # The example must construct a real verifier (guards schema drift).
    verifier = build_effect_verifier(cfg.effects)
    assert type(verifier).__name__ == "RestRecordVerifier"


def test_empty_config_is_all_default_local() -> None:
    cfg = DeploymentConfig()
    assert build_effect_verifier(cfg.effects) is None
    assert build_api_actuator(cfg.actuation) is None
    assert cfg.runtime.durable is False
    assert cfg.runtime.allow_model_grounding is False


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_deployment(tmp_path / "nope.yaml")


def test_load_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_deployment(bad)


@pytest.mark.parametrize(
    "kind,kwargs,expected",
    [
        ("rest", {"base_url": "http://sor"}, "RestRecordVerifier"),
        ("fhir", {"base_url": "http://sor/fhir"}, "FhirEffectVerifier"),
        ("document-hash", {"root": "/tmp/store"}, "DocumentHashVerifier"),
    ],
)
def test_build_each_verifier_kind(kind, kwargs, expected) -> None:
    verifier = build_effect_verifier(EffectsConfig(kind=kind, **kwargs))
    assert type(verifier).__name__ == expected


def test_build_verifier_none_kind_is_none() -> None:
    assert build_effect_verifier(EffectsConfig(kind="none")) is None


def test_rest_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        build_effect_verifier(EffectsConfig(kind="rest"))


def test_document_hash_requires_root() -> None:
    with pytest.raises(ValueError, match="root"):
        build_effect_verifier(EffectsConfig(kind="document-hash"))


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown effects.kind"):
        build_effect_verifier(EffectsConfig(kind="bogus"))


def test_api_actuator_off_by_default_on_when_enabled() -> None:
    assert build_api_actuator(ActuationConfig(api=False)) is None
    act = build_api_actuator(ActuationConfig(api=True, base_url="http://api"))
    assert type(act).__name__ == "ApiActuator"
    assert act.base_url == "http://api"
