"""Tests for fail-closed native signing configuration."""

import json
from pathlib import Path

import pytest

from scripts.native_signing import CREDENTIALS, signing_mode, write_windows_config


def test_empty_credentials_select_explicit_non_identity_modes() -> None:
    assert signing_mode("macos", {}) == "adhoc"
    assert signing_mode("windows", {}) == "unsigned"
    assert signing_mode("linux", {}) == "unsigned"


@pytest.mark.parametrize("platform", sorted(CREDENTIALS))
def test_partial_credentials_fail_closed(platform: str) -> None:
    with pytest.raises(ValueError, match="partial"):
        signing_mode(platform, {CREDENTIALS[platform][0]: "configured"})


def test_complete_macos_and_windows_credentials_enable_signing() -> None:
    macos = {name: "configured" for name in CREDENTIALS["macos"]}
    windows = {name: "configured" for name in CREDENTIALS["windows"]}
    assert signing_mode("macos", macos) == "developer-id-notarized"
    assert signing_mode("windows", windows) == "authenticode"


def test_linux_signing_requires_external_validator_contract() -> None:
    linux = {name: "configured" for name in CREDENTIALS["linux"]}
    with pytest.raises(ValueError, match="pinned external signature validator"):
        signing_mode("linux", linux)


def test_windows_config_is_sha256_timestamped_and_normalized(tmp_path: Path) -> None:
    output = tmp_path / "windows-signing.json"
    write_windows_config(output, "ab cd " * 10)
    config = json.loads(output.read_text())
    windows = config["bundle"]["windows"]
    assert windows["certificateThumbprint"] == "ABCD" * 10
    assert windows["digestAlgorithm"] == "sha256"
    assert windows["timestampUrl"].startswith("http://timestamp.")
    assert windows["tsp"] is True


def test_windows_config_rejects_non_hex_thumbprint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        write_windows_config(tmp_path / "bad.json", "not-a-thumbprint")
