from __future__ import annotations

import pytest

from scripts.classify_build_changes import BuildScope, classify_build_scope


def test_pull_request_keeps_one_representative_sidecar() -> None:
    assert classify_build_scope("pull_request", ["src/App.tsx"]) == BuildScope(
        run_sidecar=True,
        run_native=False,
        reason="representative pull-request sidecar",
    )


def test_explicit_release_candidate_keeps_the_complete_matrix() -> None:
    assert classify_build_scope("workflow_dispatch", []) == BuildScope(
        run_sidecar=True,
        run_native=True,
        reason="explicit release-candidate build",
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["src/screens/RecordReview.tsx"],
        ["README.md", "docs/qualification.md"],
        ["tests/test_engine/test_dispatch.py"],
    ],
)
def test_protected_main_uses_cheap_jobs_for_application_only_changes(paths: list[str]) -> None:
    scope = classify_build_scope("push", paths)
    assert scope.run_sidecar is False
    assert scope.run_native is False


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "package-lock.json",
        "src-tauri/src/main.rs",
        "src-tauri/Cargo.lock",
        "scripts/alias_lowercase_x11_sonames.py",
        "scripts/build_frozen_engine.py",
        "scripts/sync_frozen_dependencies.py",
        "scripts/verify_build_artifact.py",
        ".github/workflows/build.yml",
        "engine/__main__.py",
        "engine/dispatch.py",
        "engine/vision-runtime-manifest.json",
        "THIRD_PARTY_NOTICES.md",
    ],
)
def test_artifact_inputs_keep_the_complete_main_matrix(path: str) -> None:
    scope = classify_build_scope("push", [path])
    assert scope.run_sidecar is True
    assert scope.run_native is True
    assert path in scope.reason


def test_unknown_path_fails_toward_the_complete_matrix() -> None:
    scope = classify_build_scope("push", ["new-build-input.conf"])
    assert scope.run_sidecar is True
    assert scope.run_native is True


def test_empty_push_fails_toward_the_complete_matrix() -> None:
    scope = classify_build_scope("push", [])
    assert scope.run_sidecar is True
    assert scope.run_native is True


def test_absolute_or_parent_path_is_refused() -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        classify_build_scope("push", ["../outside"])
