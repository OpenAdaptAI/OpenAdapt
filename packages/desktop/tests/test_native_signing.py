"""Tests for fail-closed native signing configuration."""

import json
from pathlib import Path

import pytest

from scripts.native_signing import (
    CREDENTIALS,
    WINDOWS_PFX_CREDENTIALS,
    WINDOWS_TRUSTED_SIGNING_CREDENTIALS,
    require_signing_mode,
    signing_method,
    signing_mode,
    windows_plan,
    write_trusted_signing_config,
    write_windows_config,
)


def test_empty_credentials_select_explicit_non_identity_modes() -> None:
    assert signing_mode("macos", {}) == "adhoc"
    assert signing_mode("windows", {}) == "unsigned"
    assert signing_mode("linux", {}) == "unsigned"


def test_empty_credentials_report_non_identity_methods() -> None:
    assert signing_method("macos", {}) == "adhoc"
    assert signing_method("windows", {}) == "unsigned"
    assert signing_method("linux", {}) == "unsigned"


@pytest.mark.parametrize("platform", sorted(CREDENTIALS))
def test_partial_credentials_fail_closed(platform: str) -> None:
    with pytest.raises(ValueError, match="partial"):
        signing_mode(platform, {CREDENTIALS[platform][0]: "configured"})


def test_complete_macos_and_windows_credentials_enable_signing() -> None:
    macos = {name: "configured" for name in CREDENTIALS["macos"]}
    windows = {name: "configured" for name in CREDENTIALS["windows"]}
    assert signing_mode("macos", macos) == "developer-id-notarized"
    assert signing_mode("macos", macos) and signing_method("macos", macos) == "developer-id"
    assert signing_mode("windows", windows) == "authenticode"


def test_complete_pfx_credentials_select_authenticode_via_pfx() -> None:
    pfx = {name: "configured" for name in WINDOWS_PFX_CREDENTIALS}
    assert windows_plan(pfx) == ("authenticode", "pfx")
    assert signing_method("windows", pfx) == "pfx"


def test_complete_trusted_signing_credentials_select_authenticode_via_azure() -> None:
    azure = {name: "configured" for name in WINDOWS_TRUSTED_SIGNING_CREDENTIALS}
    assert windows_plan(azure) == ("authenticode", "trusted-signing")
    assert signing_mode("windows", azure) == "authenticode"
    assert signing_method("windows", azure) == "trusted-signing"


def test_release_gate_requires_macos_and_windows_trust_modes() -> None:
    macos = {name: "configured" for name in CREDENTIALS["macos"]}
    azure = {name: "configured" for name in WINDOWS_TRUSTED_SIGNING_CREDENTIALS}

    assert require_signing_mode("macos", "developer-id-notarized", macos) == (
        "developer-id-notarized",
        "developer-id",
    )
    assert require_signing_mode("windows", "authenticode", azure) == (
        "authenticode",
        "trusted-signing",
    )


@pytest.mark.parametrize(
    ("platform", "required"),
    (("macos", "developer-id-notarized"), ("windows", "authenticode")),
)
def test_release_gate_refuses_missing_signing_credentials(platform: str, required: str) -> None:
    with pytest.raises(ValueError, match="release requires signing mode"):
        require_signing_mode(platform, required, {})


def test_native_release_workflow_requires_protected_signing_environment() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/native-release.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("environment: native-release") == 3
    assert "--require-mode developer-id-notarized" in workflow
    assert "--require-mode authenticode" in workflow
    assert "--signing github-attested" in workflow
    assert "openadapt-desktop-native-release-provenance.json" in workflow
    # `gh attestation verify` refuses --cert-identity together with
    # --signer-workflow, so the exact-identity flag must stand alone.
    assert "--signer-workflow" not in workflow
    assert "--cert-identity" in workflow
    assert '--cert-oidc-issuer "https://token.actions.githubusercontent.com"' in workflow
    assert "--deny-self-hosted-runners" in workflow
    assert "validate-attestation" in workflow
    for secret in WINDOWS_TRUSTED_SIGNING_CREDENTIALS:
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in workflow


def test_partial_trusted_signing_credentials_fail_closed() -> None:
    azure = {WINDOWS_TRUSTED_SIGNING_CREDENTIALS[0]: "configured"}
    with pytest.raises(ValueError, match="partial Azure Trusted Signing"):
        signing_mode("windows", azure)


def test_both_windows_credential_sets_are_ambiguous() -> None:
    both = {name: "configured" for name in WINDOWS_PFX_CREDENTIALS}
    both.update({name: "configured" for name in WINDOWS_TRUSTED_SIGNING_CREDENTIALS})
    with pytest.raises(ValueError, match="ambiguous"):
        signing_mode("windows", both)


def test_trusted_signing_config_uses_signcommand_without_thumbprint(tmp_path: Path) -> None:
    output = tmp_path / "trusted-signing.json"
    write_trusted_signing_config(
        output,
        endpoint="https://eus.codesigning.azure.net",
        account="openadapt",
        certificate_profile="openadapt-public",
    )
    windows = json.loads(output.read_text())["bundle"]["windows"]
    assert "certificateThumbprint" not in windows
    assert windows["signCommand"]["cmd"] == "trusted-signing-cli"
    assert windows["signCommand"]["args"][-1] == "%1"
    assert "openadapt-public" in windows["signCommand"]["args"]


def test_trusted_signing_config_rejects_empty_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        write_trusted_signing_config(
            tmp_path / "bad.json", endpoint="", account="a", certificate_profile="p"
        )


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
