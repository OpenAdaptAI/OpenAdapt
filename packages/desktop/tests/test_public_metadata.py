"""Keep public package metadata aligned with the current product boundary."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from scripts.check_release_consistency import release_versions, sync_lock_version
from scripts.smoke_test_native_installer import bundled_flow_version
from scripts.verify_build_artifact import bundled_flow_banner

ROOT = Path(__file__).resolve().parents[1]


# Release-governance vocabulary belongs in docs/COMPLIANCE.md. To a developer
# reading the README for the first time, it reads as "this doesn't work".
README_INTERNAL_TERMS = (
    "admission",
    "admitted",
    "signed ledger",
    "lifecycle",
    "evidence authority",
    "product target",
    "governed",
    "Production default",
)
COMPLIANCE_URL = (
    "https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/desktop/docs/COMPLIANCE.md"
)


def test_public_metadata_identifies_component_role_and_admission_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compliance = (ROOT / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))

    assert "Role: installed authoring and operation companion" in compliance
    assert "active Production default" in compliance
    assert "not actively admitted" in compliance
    assert readme.count(COMPLIANCE_URL) == 1
    for term in README_INTERNAL_TERMS:
        assert term.lower() not in readme.lower(), f"README uses internal term {term!r}"
    assert "Experimental" not in readme
    assert "Beta" not in readme
    assert "openadapt-flow" in readme
    assert "AI training data collection" not in readme
    assert "AI training data collection" not in pyproject["project"]["description"]
    assert "AI training data collection" not in package["description"]
    expected_native_description = (
        "Installed companion for OpenAdapt authoring, teaching, and local pairing"
    )
    assert package["description"] == expected_native_description
    assert cargo["package"]["description"] == expected_native_description
    assert pyproject["project"]["readme"] == "README.md"
    assert pyproject["project"]["scripts"] == {"openadapt-desktop": "openadapt_desktop.cli:main"}


def test_target_authoring_uses_detected_availability_vocabulary_and_shared_form() -> None:
    form = (ROOT / "src/ui/ExecutionTargetForm.tsx").read_text(encoding="utf-8")
    record = (ROOT / "src/screens/RecordReview.tsx").read_text(encoding="utf-8")
    watch = (ROOT / "src/screens/WatchRun.tsx").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # Availability is DETECTED (openadapt_desktop/capabilities.py), never hardcoded:
    # "Available" appears only as the pill for the detected-available state.
    assert "availability:" not in form
    assert '"Beta"' not in form
    assert 'label: "Available", tone: "ok"' in form
    for state_label in (
        '"Driver required"',
        '"Permission required"',
        '"Not on this host"',
        '"Checking availability"',
    ):
        assert state_label in form
    assert "get_capabilities" in form or "GET_CAPABILITIES" in form
    for expired in ("Early access", "Exploratory", "Qualification-specific"):
        assert expired not in form
        assert expired not in readme
    assert "ExecutionTargetForm" in record
    assert "ExecutionTargetForm" in watch
    assert "target," in record


def test_rdp_transport_control_uses_native_radio_semantics() -> None:
    form = (ROOT / "src/ui/ExecutionTargetForm.tsx").read_text(encoding="utf-8")

    assert 'type="radio"' in form
    assert "checked={rdpMode === mode}" in form
    assert "switchRdpTransport(target, mode)" in form
    assert '{ backend: "rdp", rdp_host: "" }' in form


def test_record_target_reaches_immediate_execution_without_persistent_storage() -> None:
    app = (ROOT / "src/App.tsx").read_text(encoding="utf-8")
    record = (ROOT / "src/screens/RecordReview.tsx").read_text(encoding="utf-8")
    watch = (ROOT / "src/screens/WatchRun.tsx").read_text(encoding="utf-8")

    assert "initialTarget={route.target}" in app
    assert 'initialTarget ?? { backend: "web" }' in watch
    for persistent_store in ("localStorage", "sessionStorage", "indexedDB"):
        assert persistent_store not in app
        assert persistent_store not in record
        assert persistent_store not in watch


def test_record_lifecycle_uses_workflow_command_timeout() -> None:
    sidecar = (ROOT / "src-tauri/src/sidecar.rs").read_text(encoding="utf-8")
    workflow_match = sidecar.split("let timeout = match cmd {", 1)[1].split(
        "_ => COMMAND_TIMEOUT",
        1,
    )[0]

    assert '"start_recording"' in workflow_match
    assert '"stop_recording"' in workflow_match
    assert '"replay_first_workflow"' in workflow_match
    assert '"export_presentation_video"' in workflow_match
    # Commands that drive the target app through Flow must not time out
    # while the run is still going; the UI would report a lost result.
    for long_running in (
        '"run_item"',
        '"resolve_attention_item"',
        '"run_sample"',
        '"run_qualification_case"',
    ):
        assert long_running in workflow_match


def test_readme_does_not_publish_hard_coded_package_version_claims() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compliance = (ROOT / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    hard_coded_version_claim = re.compile(
        r"\b(?:Python|JavaScript|Tauri)\b.{0,120}\bversions?\b.{0,120}" r"`?v?\d+\.\d+\.\d+`?",
        flags=re.IGNORECASE,
    )

    normalized_compliance = " ".join(compliance.split())
    assert "synchronized to each engine release" in normalized_compliance
    assert "not a separate supported desktop release" in normalized_compliance
    for text in (readme, compliance):
        assert hard_coded_version_claim.search(" ".join(text.split())) is None


def test_release_docs_do_not_claim_installers_before_native_release() -> None:
    policy = (ROOT / "RELEASES.md").read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())
    candidates = (ROOT / "docs" / "RELEASE_CANDIDATE_INSTALLERS.md").read_text(encoding="utf-8")

    assert "It has no native installers" in policy
    assert "there is no trusted candidate now" in policy
    assert "until the matching native release succeeds" in normalized_policy
    assert "After a native release succeeds" in candidates
    assert "vX.Y.Z carries the assets today" not in policy


MONOREPO_WORKFLOWS = ROOT.parents[1] / ".github" / "workflows"


def test_engine_release_has_no_semantic_release_or_bot_commit() -> None:
    """The monorepo releases a reviewed version; nothing pushes a version commit."""

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert "semantic_release" not in pyproject.get("tool", {})
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert not any("semantic-release" in requirement for requirement in dev)
    assert 'name = "python-semantic-release"' not in lock
    assert "click>=8.3.3" in pyproject["tool"]["uv"]["constraint-dependencies"]


def test_monorepo_engine_release_publishes_only_a_green_reviewed_version() -> None:
    workflow_path = MONOREPO_WORKFLOWS / "desktop-release.yml"
    if not workflow_path.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = workflow_path.read_text(encoding="utf-8")
    triggers = workflow[workflow.index("\non:\n") : workflow.index("\njobs:\n")]
    gate = workflow[workflow.index("\n  gate:") : workflow.index("\n  build-python:")]
    state = gate.index("- name: Read the version and the state of its release")
    subject = gate.index("- name: Choose the commit whose checks decide the release")
    wait = gate.index(
        "- name: Require that commit's Packages run and its desktop jobs to have passed"
    )
    unchanged = gate.index("- name: Require that main has not changed Desktop since that commit")

    assert (
        "  workflow_run:\n    workflows: [Packages]\n    types: [completed]\n"
        "    branches: [main]" in triggers
    )
    assert "  push:" not in triggers
    # The subject is chosen first: the newest first-parent commit that changed
    # Desktop, whichever Packages run started the release.
    assert subject < state < wait < unchanged
    assert 'subject="$(git log -1 --first-parent --format=%H "${GITHUB_SHA}" -- .)"' in gate
    assert "--raw-field status=success" in gate
    assert "https://pypi.org/pypi/openadapt-desktop/${version}/json" in gate
    assert "partly published" in gate
    assert "actions/workflows/packages.yml/runs" in gate
    assert '--raw-field head_sha="${SUBJECT}"' in gate
    for prefix in ("desktop test / ", "desktop build / "):
        assert prefix in gate
    assert "git merge-base --is-ancestor" in gate
    jobs = workflow.split("\njobs:\n", 1)[1]
    for forbidden in ("ADMIN_TOKEN", "semantic-release", "release-package.yml"):
        assert forbidden not in jobs
    # Only the attestation and the PyPI upload may mint an OIDC token, and the
    # only push is the release App's tag.
    assert jobs.count("id-token: write") == 2
    assert [line.strip() for line in jobs.splitlines() if "git push" in line] == [
        'git push origin "refs/tags/${TAG}"'
    ]
    uses = re.findall(r"\buses:\s+\S+@([^\s#]+)", workflow)
    assert uses and all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_monorepo_package_release_runs_the_desktop_contract() -> None:
    """Desktop releases its wheel, sdist and installers together, not via release-package."""

    package_release = MONOREPO_WORKFLOWS / "release-package.yml"
    desktop_release = MONOREPO_WORKFLOWS / "desktop-release.yml"
    if not package_release.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    assert not re.search(
        r"options: \[[^\]]*\bdesktop\b", package_release.read_text(encoding="utf-8")
    )
    workflow = desktop_release.read_text(encoding="utf-8")
    build = workflow[workflow.index("\n  build-python:") : workflow.index("\n  build-macos:")]
    for check in (
        "scripts/check_release_consistency.py --require-dist",
        "scripts/check_source_boundary.py --require-dist",
        "twine check --strict",
        "scripts/verify_build_artifact.py python-distribution",
    ):
        assert check in build


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_release_lock_sync_updates_only_editable_root_version(tmp_path: Path) -> None:
    (tmp_path / "openadapt_desktop").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-desktop"\nversion = "0.3.0"\n'
    )
    (tmp_path / "openadapt_desktop/__init__.py").write_text('__version__ = "0.3.0"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "openadapt-desktop"\n'
        'version = "0.2.0"\nsource = { editable = "." }\n'
        '\n[[package]]\nname = "dependency"\nversion = "1.2.3"\n'
    )

    assert sync_lock_version(tmp_path) == "0.3.0"
    lock = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "openadapt-desktop"\nversion = "0.3.0"' in lock
    assert 'name = "dependency"\nversion = "1.2.3"' in lock


def test_pyinstaller_build_floor_matches_the_reviewed_bootloader_pin() -> None:
    """The frozen sidecar must freeze the reviewed PyInstaller, not the old 6.16 floor."""

    from scripts.frozen_notices import PYINSTALLER_NOTICE_SHA256, PYINSTALLER_VERSION

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    build = pyproject["project"]["optional-dependencies"]["build"]

    assert "pyinstaller>=6.22.2,<7" in build
    assert PYINSTALLER_VERSION == "6.22.2"
    assert f'name = "pyinstaller"\nversion = "{PYINSTALLER_VERSION}"' in lock
    assert PYINSTALLER_NOTICE_SHA256 == (
        "dcf75fdb959db1e3b41c0f8505069d2ece781b5ec6b3d0a4d30975cfc6580245"
    )


def test_candidate_release_notes_describe_the_bundled_flow_runtime() -> None:
    notes = (ROOT / "docs/RELEASE_CANDIDATE_INSTALLERS.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    native_release = (MONOREPO_WORKFLOWS / "desktop-release.yml").read_text(encoding="utf-8")
    publication = (ROOT / "scripts" / "release_publication.py").read_text(encoding="utf-8")
    build_dependencies = pyproject["project"]["optional-dependencies"]["build"]
    dependencies = pyproject["project"]["dependencies"]
    classifiers = pyproject["project"]["classifiers"]

    assert not (ROOT / "docs/EXPERIMENTAL_NATIVE_INSTALLERS.md").exists()
    # The release notes state each platform's signing, then this document.
    assert '"RELEASE_CANDIDATE_INSTALLERS.md"' in publication
    assert native_release.count("release_publication.py notes") == 1
    assert native_release.count('--notes-file "${RUNNER_TEMP}/release-notes.md"') == 1
    assert "EXPERIMENTAL_NATIVE_INSTALLERS" not in native_release
    flow_dependencies = [
        dependency for dependency in build_dependencies if dependency.startswith("openadapt-flow")
    ]
    assert flow_dependencies == ["openadapt-flow[browser,console]==2.4.0"]
    # At or above the floor the bundled Flow declares for its ``capture``
    # extra; ``tests/test_capture_runtime_contract.py`` compares the two
    # authoritatively. Never below 1.2.1, because 1.2.0 and every release
    # before it could upload a raw microphone waveform to a hosted recognizer;
    # 1.3.0 because shadow mode needs ``openadapt_capture.shadow``.
    assert "openadapt-capture>=1.3.0" in dependencies
    assert 'name = "openadapt-capture"\nversion = "1.3.0"' in lock
    assert "openadapt-privacy>=1.0.0" in dependencies
    assert not any(item.startswith("Development Status ::") for item in classifiers)
    assert "Development Status :: 2 - Pre-Alpha" not in classifiers
    assert bundled_flow_version() == "2.4.0"
    assert bundled_flow_banner() == "openadapt-flow 2.4.0"
    assert 'name = "playwright"\nversion = "1.61.0"' in lock
    assert flow_dependencies[0] in notes
    assert "playwright==1.61.0" in notes
    assert "without a separate Python" in notes
    assert "not frozen into these installers" not in notes


# The README is also the PyPI page, where relative links and images break, so
# it links to this package in the OpenAdapt monorepo by absolute URL. Those URLs
# must still name files that exist in this checkout.
OWN_BLOB = "https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/desktop/"
OWN_RAW = "https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/packages/desktop/"


def test_readme_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)
    links += re.findall(r'<img[^>]*\ssrc="([^"]+)"', readme)

    checked = 0
    for link in links:
        target = link.split("#", 1)[0]
        for prefix in (OWN_BLOB, OWN_RAW):
            if target.startswith(prefix):
                target = target.removeprefix(prefix)
                break
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (ROOT / target).exists(), f"README link does not exist: {link}"
        checked += 1
    assert checked >= 5


def test_readme_works_on_pypi_without_relative_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)
    links += re.findall(r'<img[^>]*\ssrc="([^"]+)"', readme)

    relative = [link for link in links if not link.startswith(("https://", "#", "mailto:"))]
    assert relative == []
