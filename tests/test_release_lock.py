import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verify_release_lock.py"


def _write_release_files(root: Path, project_version: str, lock_version: str):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "openadapt"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "click"\nversion = "8.1.8"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "openadapt"\n'
        f'version = "{lock_version}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )


def _run_verifier(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    script_dir = root / "scripts"
    script_dir.mkdir(exist_ok=True)
    script = script_dir / VERIFIER.name
    if script.resolve() != VERIFIER.resolve():
        shutil.copyfile(VERIFIER, script)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_lock_matches_project_version():
    result = _run_verifier(ROOT)

    assert result.returncode == 0, result.stderr


def test_release_lock_rejects_version_drift(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")
    result = _run_verifier(tmp_path)

    assert result.returncode == 1
    assert "pyproject.toml=2.0.0, uv.lock=1.6.0" in result.stderr


def test_release_lock_sync_changes_only_editable_root_and_is_idempotent(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")

    first = _run_verifier(tmp_path, "--write")
    assert first.returncode == 0, first.stderr
    after = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert after == before.replace(
        'name = "openadapt"\nversion = "1.6.0"',
        'name = "openadapt"\nversion = "2.0.0"',
    )
    second = _run_verifier(tmp_path, "--write")
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == after


def test_release_workflow_checks_candidate_and_source_boundary_before_build():
    workflow = (ROOT / ".github/workflows/release-and-publish.yml").read_text(
        encoding="utf-8"
    )

    verify_index = workflow.index("python scripts/verify_release_lock.py")
    boundary_index = workflow.index("python scripts/check_source_boundary.py")
    build_index = workflow.index("uv build --wheel --sdist")
    artifact_index = workflow.index("python scripts/verify_release_artifacts.py")
    attest_index = workflow.index("- name: Attest release artifacts")
    transfer_index = workflow.index("- name: Transfer release artifacts")
    assert verify_index < boundary_index < build_index
    assert build_index < artifact_index < attest_index < transfer_index


def test_release_workflow_pins_actions_and_separates_permissions():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    action_refs = re.findall(r"(?m)^\s*uses:\s+\S+@([^\s#]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert 'requires = ["hatchling==1.32.0"]' in metadata

    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"] == {
        "group": "release-publication",
        "cancel-in-progress": False,
    }
    jobs = document["jobs"]
    assert jobs["create-release-tag"]["permissions"] == {"contents": "read"}
    assert jobs["build-and-attest"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["publish-pypi"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert jobs["publish-github"]["permissions"] == {"contents": "read"}
    assert jobs["verify-publication"]["permissions"] == {
        "contents": "read",
        "issues": "write",
    }
    assert jobs["report-release-failure"]["permissions"] == {"issues": "write"}


def test_release_workflow_app_creates_only_an_exact_reviewed_tag():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "ADMIN_TOKEN" not in workflow
    assert "secrets.PYPI_TOKEN" not in workflow
    assert "git push origin HEAD:main" not in workflow
    assert "semantic-release -v version" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "tags:" in workflow
    assert '- "v*"' in workflow

    jobs = document["jobs"]
    create = jobs["create-release-tag"]
    assert create["environment"] == "release-identity"
    assert "github.event_name == 'workflow_dispatch'" in create["if"]
    app = next(step for step in create["steps"] if step.get("id") == "release-app")
    assert app["uses"].startswith("actions/create-github-app-token@")
    assert app["with"] == {
        "app-id": "${{ vars.OPENADAPT_RELEASE_APP_ID }}",
        "private-key": "${{ secrets.OPENADAPT_RELEASE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-administration": "read",
        "permission-contents": "write",
        "permission-metadata": "read",
    }
    identity = next(
        step
        for step in create["steps"]
        if step["name"] == "Require the exact release App identity for tag creation"
    )
    assert identity["env"] == {
        "ACTUAL_APP_SLUG": "${{ steps.release-app.outputs.app-slug }}",
        "EXPECTED_APP_SLUG": "openadapt-release",
    }
    assert 'ACTUAL_APP_SLUG" != "$EXPECTED_APP_SLUG' in identity["run"]
    assert create["steps"].index(identity) < next(
        index
        for index, step in enumerate(create["steps"])
        if step["name"] == "Create and push only the annotated release tag"
    )

    candidate = next(step for step in create["steps"] if step.get("id") == "candidate")
    assert 'GITHUB_REF" != "refs/heads/main' in candidate["run"]
    assert 'current_main" != "$GITHUB_SHA' in candidate["run"]
    assert 'REQUESTED_VERSION" != "$project_version' in candidate["run"]
    assert "CHANGELOG.md must start with" in candidate["run"]
    assert "scripts/verify_release_hosting.py" in candidate["run"]
    assert candidate["env"]["GH_TOKEN"] == "${{ steps.release-app.outputs.token }}"
    assert "Tag $tag already exists" in candidate["run"]

    tag = next(
        step
        for step in create["steps"]
        if step["name"] == "Create and push only the annotated release tag"
    )
    refresh_index = tag["run"].index(
        "git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main"
    )
    compare_index = tag["run"].index('current_main" != "$GITHUB_SHA')
    create_index = tag["run"].index('git tag -a "$RELEASE_TAG" "$GITHUB_SHA"')
    push_index = tag["run"].index('git push origin "refs/tags/$RELEASE_TAG"')
    assert refresh_index < compare_index < create_index < push_index
    assert 'git tag -a "$RELEASE_TAG" "$GITHUB_SHA"' in tag["run"]
    assert 'git push origin "refs/tags/$RELEASE_TAG"' in tag["run"]


def test_release_workflow_rechecks_main_immediately_before_the_app_tag_push():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    create = document["jobs"]["create-release-tag"]

    candidate = next(step for step in create["steps"] if step.get("id") == "candidate")
    tag = next(
        step
        for step in create["steps"]
        if step["name"] == "Create and push only the annotated release tag"
    )

    # The candidate check alone is not sufficient. main can advance before the
    # separate tag-push step starts, so that step must get and compare main too.
    assert "git fetch --no-tags origin" in candidate["run"]
    assert "git fetch --no-tags origin" in tag["run"]
    assert 'current_main="$(git rev-parse refs/remotes/origin/main)"' in tag["run"]
    assert 'current_main" != "$GITHUB_SHA' in tag["run"]
    assert tag["run"].rindex('current_main" != "$GITHUB_SHA') < tag["run"].index(
        'git push origin "refs/tags/$RELEASE_TAG"'
    )


def test_release_workflow_checks_the_platform_selection_before_any_release_write():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = document["jobs"]

    create = jobs["create-release-tag"]
    candidate = next(step for step in create["steps"] if step.get("id") == "candidate")
    tag = next(
        step
        for step in create["steps"]
        if step["name"] == "Create and push only the annotated release tag"
    )
    assert "scripts/release_platform_versions.py" in candidate["run"]
    assert "--manifest platform-manifest.json" in candidate["run"]
    assert '--launcher-version "$project_version"' in candidate["run"]
    assert "scripts/validate_platform_manifest.py" in candidate["run"]
    assert "--offline" in candidate["run"]
    assert "--require-compatible" in candidate["run"]
    assert "scripts/check_source_boundary.py" in candidate["run"]
    assert create["steps"].index(candidate) < create["steps"].index(tag)

    build = jobs["build-and-attest"]
    guard = next(
        step
        for step in build["steps"]
        if step["name"] == "Require the release App tag and exact candidate state"
    )
    artifact_build = next(
        step
        for step in build["steps"]
        if step["name"] == "Build exact release artifacts"
    )
    assert "scripts/release_platform_versions.py" in guard["run"]
    assert "scripts/validate_platform_manifest.py" in guard["run"]
    assert build["steps"].index(guard) < build["steps"].index(artifact_build)


def test_release_workflow_publishes_from_the_exact_app_tag_with_oidc():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = document["jobs"]

    build = jobs["build-and-attest"]
    assert "github.event_name == 'push'" in build["if"]
    guard = next(
        step
        for step in build["steps"]
        if step["name"] == "Require the release App tag and exact candidate state"
    )
    assert guard["env"]["EXPECTED_ACTOR"] == "openadapt-release[bot]"
    assert 'GITHUB_REF_TYPE" != "tag' in guard["run"]
    assert 'GITHUB_ACTOR" != "$EXPECTED_ACTOR' in guard["run"]
    assert 'GITHUB_REF_NAME" != "$expected_tag' in guard["run"]
    assert "git merge-base --is-ancestor HEAD refs/remotes/origin/main" in guard["run"]

    pypi = jobs["publish-pypi"]
    assert pypi["needs"] == ["build-and-attest", "preflight-github"]
    assert pypi["environment"] == "pypi"
    publish = next(
        step for step in pypi["steps"] if step["name"].startswith("Publish to PyPI")
    )
    assert publish["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert publish["with"] == {"skip-existing": True}
    preflight = next(
        step
        for step in pypi["steps"]
        if step["name"] == "Refuse conflicting immutable PyPI files"
    )
    strict = next(
        step
        for step in pypi["steps"]
        if step["name"] == "Verify immutable PyPI publication bytes"
    )
    assert pypi["steps"].index(preflight) < pypi["steps"].index(publish)
    assert pypi["steps"].index(publish) < pypi["steps"].index(strict)
    assert "scripts/verify_pypi_release.py" in preflight["run"]
    assert "--allow-matching-subset" in preflight["run"]
    assert "scripts/verify_pypi_release.py" in strict["run"]
    assert "--allow-matching-subset" not in strict["run"]

    preflight = jobs["preflight-github"]
    assert preflight["needs"] == "build-and-attest"
    assert preflight["environment"] == "release-identity"
    preflight_steps = preflight["steps"]
    preflight_app = next(
        step for step in preflight_steps if step.get("id") == "release-app"
    )
    assert preflight_app["with"] == {
        "app-id": "${{ vars.OPENADAPT_RELEASE_APP_ID }}",
        "private-key": "${{ secrets.OPENADAPT_RELEASE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-administration": "read",
        "permission-contents": "read",
        "permission-metadata": "read",
    }
    preflight_guard = next(
        step
        for step in preflight_steps
        if step["name"]
        == "Require the GitHub publication identity and hosting controls"
    )
    assert "scripts/verify_release_hosting.py" in preflight_guard["run"]
    assert '--repository "$GITHUB_REPOSITORY"' in preflight_guard["run"]
    assert '--tag "$RELEASE_TAG"' in preflight_guard["run"]

    github = jobs["publish-github"]
    assert github["needs"] == [
        "build-and-attest",
        "preflight-github",
        "publish-pypi",
    ]
    assert github["environment"] == "release-identity"
    publish_steps = jobs["publish-github"]["steps"]
    app = next(step for step in publish_steps if step.get("id") == "release-app")
    assert app["uses"].startswith("actions/create-github-app-token@")
    assert app["with"] == {
        "app-id": "${{ vars.OPENADAPT_RELEASE_APP_ID }}",
        "private-key": "${{ secrets.OPENADAPT_RELEASE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-administration": "read",
        "permission-contents": "write",
        "permission-metadata": "read",
    }
    identity = next(
        step
        for step in publish_steps
        if step["name"]
        == "Require the exact release App identity for GitHub publication"
    )
    assert identity["env"] == {
        "ACTUAL_APP_SLUG": "${{ steps.release-app.outputs.app-slug }}",
        "EXPECTED_APP_SLUG": "openadapt-release",
    }
    assert 'ACTUAL_APP_SLUG" != "$EXPECTED_APP_SLUG' in identity["run"]
    publish = next(
        step
        for step in publish_steps
        if step["name"] == "Publish the GitHub Release and exact artifacts"
    )
    assert publish_steps.index(identity) < publish_steps.index(publish)
    assert publish["env"]["GH_TOKEN"] == "${{ steps.release-app.outputs.token }}"
    assert publish["env"]["RELEASE_TAG"] == "${{ github.ref_name }}"
    assert publish["env"]["EXPECTED_AUTHOR"] == "openadapt-release[bot]"
    assert '"$GH_CLI" release create' in publish["run"]
    assert "--verify-tag" in publish["run"]
    assert "--draft" in publish["run"]
    assert "dist/*.whl dist/*.tar.gz" in publish["run"]
    assert '"$GH_CLI" release edit "$RELEASE_TAG"' in publish["run"]
    assert "--draft=false" in publish["run"]
    assert "--latest" not in publish["run"]
    assert (
        '"$GH_CLI" release upload "$RELEASE_TAG" "dist/$missing_asset"'
        in publish["run"]
    )
    assert "--allow-missing" in publish["run"]
    assert "--missing-output /tmp/missing-release-assets.txt" in publish["run"]
    assert "--clobber" not in publish["run"]
    assert "scripts/verify_github_release.py" in publish["run"]
    assert "--downloaded-dir /tmp/github-release-assets" in publish["run"]
    assert 'release_status" = "404"' in publish["run"]
    assert 'release_status" != "200"' in publish["run"]
    assert 'tag_commit" != "$GITHUB_SHA' in publish["run"]
    assert "scripts/verify_release_hosting.py" in publish["run"]
    assert '"$GH_CLI" release verify "$RELEASE_TAG"' in publish["run"]
    assert '"$GH_CLI" release verify-asset "$RELEASE_TAG" "$artifact"' in publish["run"]
    assert "semantic-release" not in publish["run"]

    cli = next(step for step in publish_steps if step.get("id") == "release-cli")
    assert "gh_2.98.0_linux_amd64.tar.gz" in cli["run"]
    assert (
        "3b8ac6b30336802fc1a858d7c084e11cdf24ac1a761ca90b68022d7d729208de" in cli["run"]
    )


def test_release_workflow_publishes_the_attested_bytes_to_both_destinations():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = document["jobs"]

    build_steps = jobs["build-and-attest"]["steps"]
    attest = next(
        step for step in build_steps if step["name"] == "Attest release artifacts"
    )
    transfer = next(
        step for step in build_steps if step["name"] == "Transfer release artifacts"
    )
    build_run = next(
        step["run"]
        for step in build_steps
        if step["name"] == "Build exact release artifacts"
    )
    assert "python scripts/check_source_boundary.py --dist dist" in build_run
    assert build_run.index("uv build --wheel --sdist") < build_run.index(
        "python scripts/check_source_boundary.py --dist dist"
    )
    assert attest["with"]["subject-path"].splitlines() == [
        "dist/*.whl",
        "dist/*.tar.gz",
    ]
    assert transfer["with"]["path"].splitlines() == [
        "dist/*.whl",
        "dist/*.tar.gz",
    ]
    assert transfer["with"]["if-no-files-found"] == "error"
    assert transfer["with"]["retention-days"] == 30

    pypi_steps = jobs["publish-pypi"]["steps"]
    github_steps = jobs["publish-github"]["steps"]
    pypi_download = next(
        step
        for step in pypi_steps
        if step["name"] == "Download attested release artifacts"
    )
    github_download = next(
        step
        for step in github_steps
        if step["name"] == "Download attested release artifacts"
    )
    pypi_publish = next(
        step for step in pypi_steps if step["name"].startswith("Publish to PyPI")
    )
    github_publish = next(
        step
        for step in github_steps
        if step["name"] == "Publish the GitHub Release and exact artifacts"
    )
    assert pypi_download["with"]["name"] == transfer["with"]["name"]
    assert github_download["with"]["name"] == transfer["with"]["name"]
    assert pypi_publish["with"] == {"skip-existing": True}
    assert github_publish["env"]["RELEASE_TAG"] == "${{ github.ref_name }}"

    pypi_checkout = next(
        step for step in pypi_steps if step["name"] == "Checkout the exact release tag"
    )
    assert pypi_checkout["with"] == {
        "ref": "${{ github.ref }}",
        "persist-credentials": False,
    }

    checkout = next(
        step
        for step in github_steps
        if step["name"] == "Checkout the exact release tag"
    )
    assert checkout["with"] == {"ref": "${{ github.ref }}", "fetch-depth": 0}

    verification = jobs["verify-publication"]
    verification_text = "\n".join(
        str(step.get("run", "")) for step in verification["steps"]
    )
    assert "release_platform_versions.py" in verification_text
    assert '"${component_args[@]}"' in verification_text
    assert "generate_platform_manifest.py" in verification_text
    assert "--require-compatible" in verification_text
    assert "--require-network" in verification_text
    assert "urllib.request.urlretrieve" in verification_text
    assert "gh release download" in verification_text
    assert "diff -u /tmp/source.sha256 /tmp/pypi.sha256" in verification_text
    assert "diff -u /tmp/source.sha256 /tmp/github.sha256" in verification_text


def test_release_failure_requires_failed_job_rerun_in_the_same_run():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    report = document["jobs"]["report-release-failure"]
    run = next(
        step["run"]
        for step in report["steps"]
        if step["name"] == "File or update the release failure issue"
    )

    assert "gh run rerun ${{ github.run_id }} --failed" in run
    assert "Do not start a new full run" in run
    assert "or create a recovery tag" in run
