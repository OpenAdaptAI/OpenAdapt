"""Tests for fail-closed native signing configuration."""

from pathlib import Path

import pytest

from scripts.native_signing import (
    CREDENTIALS,
    WINDOWS_SIGNPATH_CREDENTIALS,
    require_signing_mode,
    signing_method,
    signing_mode,
    windows_plan,
)

NATIVE_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "desktop-release.yml"
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


def test_complete_macos_credentials_enable_developer_id() -> None:
    macos = {name: "configured" for name in CREDENTIALS["macos"]}
    assert signing_mode("macos", macos) == "developer-id-notarized"
    assert signing_method("macos", macos) == "developer-id"


def test_complete_signpath_credentials_select_authenticode_via_signpath() -> None:
    signpath = {name: "configured" for name in WINDOWS_SIGNPATH_CREDENTIALS}
    assert windows_plan(signpath) == ("authenticode", "signpath")
    assert signing_mode("windows", signpath) == "authenticode"
    assert signing_method("windows", signpath) == "signpath"


@pytest.mark.parametrize("missing", WINDOWS_SIGNPATH_CREDENTIALS)
def test_partial_signpath_credentials_fail_closed(missing: str) -> None:
    partial = {name: "configured" for name in WINDOWS_SIGNPATH_CREDENTIALS if name != missing}
    with pytest.raises(ValueError, match=f"partial SignPath.*{missing}"):
        signing_mode("windows", partial)


def test_retired_windows_signing_credentials_are_ignored() -> None:
    """Azure Trusted Signing and the pfx path are gone; their secrets do nothing."""

    retired = {
        "AZURE_TENANT_ID": "configured",
        "TRUSTED_SIGNING_ACCOUNT": "configured",
        "WINDOWS_CERTIFICATE": "configured",
    }
    assert windows_plan(retired) == ("unsigned", "unsigned")


def test_release_gate_can_still_require_a_trust_mode() -> None:
    signpath = {name: "configured" for name in WINDOWS_SIGNPATH_CREDENTIALS}
    assert require_signing_mode("windows", "authenticode", signpath) == (
        "authenticode",
        "signpath",
    )
    with pytest.raises(ValueError, match="release requires signing mode"):
        require_signing_mode("macos", "developer-id-notarized", {})


def test_linux_signing_requires_external_validator_contract() -> None:
    linux = {name: "configured" for name in CREDENTIALS["linux"]}
    with pytest.raises(ValueError, match="pinned external signature validator"):
        signing_mode("linux", linux)


def test_native_release_workflow_signs_windows_through_signpath_only() -> None:
    if not NATIVE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = NATIVE_WORKFLOW.read_text(encoding="utf-8")

    assert "environment: native-release" in workflow
    # No release-time requirement for paid identities: macOS ships ad-hoc and
    # Windows ships unsigned until SignPath is configured, labelled as such.
    assert "--require-mode" not in workflow
    for secret in WINDOWS_SIGNPATH_CREDENTIALS:
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in workflow
    assert "signpath/github-action-submit-signing-request@" in workflow
    assert "wait-for-completion: true" in workflow
    for retired in ("AZURE_", "TRUSTED_SIGNING", "WINDOWS_CERTIFICATE", "trusted-signing-cli"):
        assert retired not in workflow
    assert "--signing github-attested" in workflow
    # `gh attestation verify` refuses --cert-identity together with
    # --signer-workflow, so the exact-identity flag must stand alone.
    assert "--signer-workflow" not in workflow
    assert "--cert-identity" in workflow
    assert '--cert-oidc-issuer "https://token.actions.githubusercontent.com"' in workflow
    assert "--deny-self-hosted-runners" in workflow


SIGNPATH_DIR = Path(__file__).resolve().parents[3] / ".signpath"


def test_one_signpath_request_deep_signs_the_msi_and_signs_the_nsis_setup() -> None:
    import xml.etree.ElementTree as ElementTree

    config = SIGNPATH_DIR / "artifact-configurations" / "openadapt-desktop-installers.xml"
    policy = SIGNPATH_DIR / "policies" / "openadapt-desktop" / "release-signing.yml"
    if not NATIVE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    namespace = {"sp": "http://signpath.io/artifact-configuration/v1"}
    root = ElementTree.parse(config).getroot()
    msi = root.find("sp:zip-file/sp:msi-file", namespace)
    assert msi is not None and msi.get("path") == "*.msi"
    nested = msi.find("sp:pe-file-set/sp:for-each/sp:authenticode-sign", namespace)
    assert nested is not None, "the MSI's program files must be signed too"
    assert msi.find("sp:authenticode-sign", namespace) is not None
    nsis = root.find("sp:zip-file/sp:pe-file", namespace)
    assert nsis is not None and nsis.get("path") == "*-setup.exe"
    assert nsis.find("sp:authenticode-sign", namespace) is not None

    policy_text = policy.read_text(encoding="utf-8")
    assert "require_github_hosted: true" in policy_text
    assert "disallow_reruns: true" in policy_text

    workflow = NATIVE_WORKFLOW.read_text(encoding="utf-8")
    smoke = workflow.split("Smoke-test MSI and NSIS install", 1)[1].split("- name:", 1)[0]
    # The MSI is checked in full; only the NSIS setup is checked as an installer.
    assert smoke.count("--installer-only-signature") == 1
    assert "@Signature" in smoke.split("$Nsis[0].FullName", 1)[0]
    assert "@NsisSignature" in smoke.split("$Nsis[0].FullName", 1)[1]
    assert workflow.count("signpath/github-action-submit-signing-request@") == 1
