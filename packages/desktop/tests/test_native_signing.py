"""Tests for fail-closed native signing configuration."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.native_signing import (
    CREDENTIALS,
    SIGNPATH_RELEASE_POLICY,
    SIGNPATH_TEST_POLICY,
    WINDOWS_SIGNPATH_CREDENTIALS,
    require_signing_mode,
    require_test_signing,
    signing_method,
    signing_mode,
    windows_plan,
)

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
NATIVE_WORKFLOW = WORKFLOWS / "desktop-release.yml"
TEST_SIGN_WORKFLOW = WORKFLOWS / "signpath-test-sign.yml"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "native_signing.py"


def _signpath(policy: str = SIGNPATH_RELEASE_POLICY) -> dict[str, str]:
    environment = {name: "configured" for name in WINDOWS_SIGNPATH_CREDENTIALS}
    environment["SIGNPATH_SIGNING_POLICY_SLUG"] = policy
    return environment


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
    signpath = _signpath()
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


@pytest.mark.parametrize(
    "policy",
    [
        SIGNPATH_TEST_POLICY,
        "Test-Signing",
        " test-signing ",
        "configured",
        "release",
        "Release-Signing",
        " release-signing",
        "release-signing ",
        "release-signing\n",
    ],
)
def test_release_with_any_policy_but_release_signing_fails_loudly(policy: str) -> None:
    """A test certificate never ships, and a configured set never falls back to unsigned."""

    signpath = _signpath(policy)
    for plan in (
        lambda: windows_plan(signpath),
        lambda: signing_mode("windows", signpath),
        lambda: signing_method("windows", signpath),
        lambda: require_signing_mode("windows", "authenticode", signpath),
    ):
        with pytest.raises(ValueError, match="not allowed in a release"):
            plan()


@pytest.mark.parametrize("missing", WINDOWS_SIGNPATH_CREDENTIALS)
def test_partial_set_fails_even_with_the_test_policy(missing: str) -> None:
    partial = {k: v for k, v in _signpath(SIGNPATH_TEST_POLICY).items() if k != missing}
    with pytest.raises(ValueError, match="partial SignPath"):
        windows_plan(partial)
    with pytest.raises(ValueError, match="partial SignPath"):
        require_test_signing(partial)


def test_test_sign_preflight_requires_exactly_the_test_policy() -> None:
    require_test_signing(_signpath(SIGNPATH_TEST_POLICY))
    for policy in (SIGNPATH_RELEASE_POLICY, "Test-Signing", "configured"):
        with pytest.raises(ValueError, match="signs only with the 'test-signing' policy"):
            require_test_signing(_signpath(policy))
    with pytest.raises(ValueError, match="partial SignPath"):
        require_test_signing({})


def _run_cli(*args: str, environment: dict[str, str]) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIGNPATH_")}
    env.update(environment)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_cli_preflight_fails_the_test_policy_and_signs_only_release_signing(
    tmp_path: Path,
) -> None:
    output = tmp_path / "github_output"
    code, text = _run_cli(
        "preflight",
        "--platform",
        "windows",
        "--github-output",
        str(output),
        environment=_signpath(SIGNPATH_TEST_POLICY),
    )
    assert code == 2 and "not allowed in a release" in text
    assert not output.exists(), "a refused plan must write no mode or method"

    code, _ = _run_cli(
        "preflight", "--platform", "windows", "--github-output", str(output), environment={}
    )
    assert code == 0
    assert output.read_text(encoding="utf-8") == "mode=unsigned\nmethod=unsigned\n"

    output.unlink()
    code, _ = _run_cli(
        "preflight",
        "--platform",
        "windows",
        "--github-output",
        str(output),
        environment=_signpath(),
    )
    assert code == 0
    assert output.read_text(encoding="utf-8") == "mode=authenticode\nmethod=signpath\n"


def test_cli_test_sign_preflight_refuses_the_release_policy() -> None:
    code, _ = _run_cli("test-sign-preflight", environment=_signpath(SIGNPATH_TEST_POLICY))
    assert code == 0
    code, text = _run_cli("test-sign-preflight", environment=_signpath())
    assert code == 2 and "test-signing" in text


def test_release_gate_can_still_require_a_trust_mode() -> None:
    signpath = _signpath()
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
    policies = [
        SIGNPATH_DIR / "policies" / "OpenAdapt" / f"{slug}.yml"
        for slug in ("test-signing", "release-signing")
    ]
    if not NATIVE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    namespace = {"sp": "http://signpath.io/artifact-configuration/v1"}
    root = ElementTree.parse(config).getroot()
    msi = root.find("sp:zip-file/sp:msi-file", namespace)
    assert msi is not None and msi.get("path") == "*.msi"
    nested = msi.find("sp:pe-file-set/sp:for-each/sp:authenticode-sign", namespace)
    assert nested is not None, "the MSI's program files must be signed too"
    # The program files sit under the MSI's install directory, and SignPath's
    # "*" does not cross a directory separator: a root-only "*.exe" matches
    # nothing and fails the signing request.
    includes = msi.findall("sp:pe-file-set/sp:include", namespace)
    assert includes and all(i.get("path", "").startswith("**/") for i in includes)
    assert msi.find("sp:authenticode-sign", namespace) is not None
    nsis = root.find("sp:zip-file/sp:pe-file", namespace)
    assert nsis is not None and nsis.get("path") == "*-setup.exe"
    assert nsis.find("sp:authenticode-sign", namespace) is not None

    for policy in policies:
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


BUILD_ACTION_PATH = "./.github/actions/build-desktop-windows-installers"
BUILD_ACTION = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "actions"
    / "build-desktop-windows-installers"
    / "action.yml"
)


def _load(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build_steps(steps: list[dict]) -> list[dict]:
    """The steps from checkout through the shared build, without the preflight."""

    start = next(i for i, step in enumerate(steps) if "actions/checkout@" in step.get("uses", ""))
    end = next(i for i, step in enumerate(steps) if step.get("uses") == BUILD_ACTION_PATH)
    return [step for step in steps[start : end + 1] if step.get("id") != "signing"]


def test_release_still_requires_a_valid_signature_from_signpath() -> None:
    if not NATIVE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = NATIVE_WORKFLOW.read_text(encoding="utf-8")
    replace = workflow.split("Replace the built installers with the signed ones", 1)[1]
    replace = replace.split("- name:", 1)[0]
    assert 'if ($Signature.Status -ne "Valid")' in replace


def test_both_windows_callers_run_one_shared_cache_free_build() -> None:
    if not BUILD_ACTION.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    release = _load(NATIVE_WORKFLOW)["jobs"]["build-windows"]
    test_sign = _load(TEST_SIGN_WORKFLOW)["jobs"]["test-sign-windows"]
    assert release["environment"] == "native-release"
    assert test_sign["environment"] == "signpath-test"
    assert release["runs-on"] == test_sign["runs-on"] == "windows-2022"

    # Checkout, Python 3.12 and the shared action, identically in both.
    shared = _build_steps(release["steps"])
    assert [step.get("uses", "").split("@")[0] for step in shared] == [
        "actions/checkout",
        "actions/setup-python",
        BUILD_ACTION_PATH,
    ]
    assert _build_steps(test_sign["steps"]) == shared
    for job in (release, test_sign):
        steps = job["steps"]
        assert sum(step.get("uses") == BUILD_ACTION_PATH for step in steps) == 1
        preflight = next(i for i, step in enumerate(steps) if step.get("id") == "signing")
        build = next(i for i, step in enumerate(steps) if step.get("uses") == BUILD_ACTION_PATH)
        assert preflight < build, "the signing preflight must refuse before the build"
        # Nothing the build used to do is left in a caller.
        text = "\n".join(str(step) for step in steps)
        for moved in ("setup-node@", "rust-toolchain@", "npm ci", "tauri build", "uv=="):
            assert moved not in text, moved

    action = _load(BUILD_ACTION)
    assert action["runs"]["using"] == "composite"
    action_steps = action["runs"]["steps"]
    action_text = BUILD_ACTION.read_text(encoding="utf-8")
    assert "actions/cache" not in action_text and "rust-cache" not in action_text
    node = next(step for step in action_steps if "setup-node@" in step.get("uses", ""))
    assert node["with"]["package-manager-cache"] is False
    assert "secrets." not in action_text, "the shared build sees no signing secret"
    for step in action_steps:
        if "run" in step:
            assert step["working-directory"] == "packages/desktop"
            assert step["shell"] in {"pwsh", "bash"}
    runs = [step["run"] for step in action_steps if "run" in step]
    assert runs[0] == "npm ci --ignore-scripts"
    assert "uv run --locked python scripts/build_frozen_engine.py" in runs[1]
    assert "--bundles msi,nsis" in runs[2] and "--target x86_64-pc-windows-msvc" in runs[2]


def test_test_sign_workflow_runs_only_by_hand_on_main_on_github_hosted_windows() -> None:
    if not TEST_SIGN_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = _load(TEST_SIGN_WORKFLOW)
    # PyYAML reads the bare key ``on`` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert not (triggers["workflow_dispatch"] or {}).get("inputs")
    assert workflow["permissions"] == {"contents": "read"}
    assert list(workflow["jobs"]) == ["test-sign-windows"]
    job = workflow["jobs"]["test-sign-windows"]
    assert job["runs-on"] == "windows-2022"
    assert job["environment"] == "signpath-test"
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    guard = job["steps"][0]
    assert "refs/heads/main" in guard["run"] and "throw" in guard["run"]


def test_test_sign_workflow_signs_only_with_test_signing() -> None:
    if not TEST_SIGN_WORKFLOW.is_file() or not NATIVE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    release = _load(NATIVE_WORKFLOW)["jobs"]["build-windows"]
    steps = _load(TEST_SIGN_WORKFLOW)["jobs"]["test-sign-windows"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "signing")
    assert preflight["run"] == "python scripts/native_signing.py test-sign-preflight"
    for secret in WINDOWS_SIGNPATH_CREDENTIALS:
        assert preflight["env"][secret] == f"${{{{ secrets.{secret} }}}}"

    signers = [step for step in steps if "submit-signing-request" in step.get("uses", "")]
    assert len(signers) == 1
    assert steps.index(preflight) < steps.index(signers[0])
    release_signer = next(
        step for step in release["steps"] if "submit-signing-request" in step.get("uses", "")
    )
    assert signers[0]["uses"] == release_signer["uses"]
    sign = signers[0]["with"]
    assert sign["signing-policy-slug"] == "${{ secrets.SIGNPATH_SIGNING_POLICY_SLUG }}"
    assert sign["wait-for-completion"] is True

    uploads = [step for step in steps if "actions/upload-artifact@" in step.get("uses", "")]
    assert len(uploads) == 2
    assert all(int(step["with"]["retention-days"]) <= 7 for step in uploads)


FORBIDDEN_IN_TEST_SIGN = (
    "contents: write",
    "id-token",
    "attestations:",
    "native-release",
    "gh release",
    "gh api",
    "git" + " tag",
    "git" + " push",
    "softprops/",
    "pypa/gh-action-pypi-publish",
    "twine",
    "native_release.py",
    "release_publication.py",
    "actions/cache",
    "self-hosted",
)


def test_test_sign_workflow_publishes_nothing() -> None:
    if not TEST_SIGN_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    text = TEST_SIGN_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_IN_TEST_SIGN:
        assert forbidden not in text, forbidden
    used = {
        step["uses"].split("@", 1)[0]
        for step in _load(TEST_SIGN_WORKFLOW)["jobs"]["test-sign-windows"]["steps"]
        if "uses" in step
    }
    assert used == {
        "actions/checkout",
        "actions/setup-python",
        BUILD_ACTION_PATH,
        "actions/upload-artifact",
        "signpath/github-action-submit-signing-request",
    }
