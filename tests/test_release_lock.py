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
        "group": (
            "release-${{ github.event_name == 'workflow_dispatch' && "
            "inputs.operation == 'create' && format('tag-creation-v{0}', "
            "inputs.version) || format('publication-{0}', "
            "github.event_name == 'push' && github.ref_name || "
            "format('v{0}', inputs.version)) }}"
        ),
        "cancel-in-progress": False,
    }
    jobs = document["jobs"]
    assert jobs["prepare-release-candidate"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    for job_name in (
        "verify-release-candidate-admission",
        "verify-tagged-release-admission",
    ):
        assert jobs[job_name]["permissions"] == {
            "contents": "read",
            "attestations": "read",
        }
    assert jobs["create-release-tag"]["permissions"] == {"contents": "read"}
    assert jobs["verify-publication-authority"]["permissions"] == {"contents": "read"}
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
    assert create["needs"] == [
        "prepare-release-candidate",
        "verify-release-candidate-admission",
    ]
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
        "ACTUAL_INSTALLATION_ID": "${{ steps.release-app.outputs.installation-id }}",
        "CONFIGURED_APP_ID": "${{ vars.OPENADAPT_RELEASE_APP_ID }}",
        "EXPECTED_APP_SLUG": "openadapt-release",
        "EXPECTED_APP_ID": "4730708",
        "EXPECTED_INSTALLATION_ID": "156835568",
        "EXPECTED_DISPATCHER_ID": "774615",
    }
    assert 'ACTUAL_APP_SLUG" != "$EXPECTED_APP_SLUG' in identity["run"]
    assert 'CONFIGURED_APP_ID" != "$EXPECTED_APP_ID' in identity["run"]
    assert 'ACTUAL_INSTALLATION_ID" != "$EXPECTED_INSTALLATION_ID' in identity["run"]
    assert 'GITHUB_ACTOR_ID" != "$EXPECTED_DISPATCHER_ID' in identity["run"]
    checkout = next(
        step
        for step in create["steps"]
        if step["name"] == "Checkout the exact dispatched main commit"
    )
    assert checkout["with"]["persist-credentials"] is False
    assert create["steps"].index(identity) < next(
        index
        for index, step in enumerate(create["steps"])
        if step["name"] == "Create and re-read only the admitted annotated release tag"
    )

    candidate = next(step for step in create["steps"] if step.get("id") == "candidate")
    assert 'GITHUB_REF" != "refs/heads/main' in candidate["run"]
    assert 'current_main" != "$GITHUB_SHA' in candidate["run"]
    assert 'REQUESTED_VERSION" != "$project_version' in candidate["run"]
    assert "CHANGELOG.md must start with" in candidate["run"]
    assert "Tag $tag already exists" in candidate["run"]

    tag = next(
        step
        for step in create["steps"]
        if step["name"] == "Create and re-read only the admitted annotated release tag"
    )
    refresh_index = tag["run"].index(
        "git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main"
    )
    compare_index = tag["run"].index('current_main" != "$GITHUB_SHA')
    binding_index = tag["run"].index("release_admission_contract.py tag-binding")
    object_index = tag["run"].index("/git/tags")
    ref_index = tag["run"].index("/git/refs")
    reread_index = tag["run"].rindex("verify-tag-ref")
    assert refresh_index < compare_index < binding_index < object_index < ref_index
    assert ref_index < reread_index
    assert "> /tmp/release-tag-binding.json" in tag["run"]
    assert 'binding_path = Path("/tmp/release-tag-binding.json")' in tag["run"]
    assert "binding_bytes = binding_path.read_bytes()" in tag["run"]
    assert '"message": binding_bytes.decode("utf-8")' in tag["run"]
    assert "binding_bytes != canonical" in tag["run"]
    assert (
        'binding="$(python scripts/release_admission_contract.py tag-binding'
        not in tag["run"]
    )
    assert "git tag" not in tag["run"]
    assert "git push" not in tag["run"]
    controls = next(
        step
        for step in create["steps"]
        if step["name"] == "Require the exact App repository and release controls"
    )["run"]
    assert "/installation/repositories?per_page=100" in controls
    assert "verify-tag-rulesets" in controls
    assert "immutable-releases" in controls


def test_release_workflow_requires_the_central_admission_before_each_write():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    callers = [
        jobs["verify-release-candidate-admission"],
        jobs["verify-tagged-release-admission"],
    ]
    refs = {caller["uses"].rsplit("@", 1)[-1] for caller in callers}
    assert len(refs) == 1
    assert re.fullmatch(r"[0-9a-f]{40}", refs.pop())
    for caller in callers:
        assert caller["uses"].startswith(
            "OpenAdaptAI/.github/.github/workflows/"
            "verify-production-release-admission.yml@"
        )
        assert set(caller["with"]) == {
            "admission_reference_json",
            "artifact_inventory_json",
            "candidate_artifact_name",
            "expected_target",
            "expected_repository",
            "expected_repository_id",
            "expected_source_commit",
            "expected_version",
            "expected_tag",
        }
        assert caller["with"]["expected_target"] == "openadapt"
        assert caller["with"]["expected_repository"] == "OpenAdaptAI/OpenAdapt"
        assert caller["with"]["expected_repository_id"] == "627024850"

    assert jobs["create-release-tag"]["needs"] == [
        "prepare-release-candidate",
        "verify-release-candidate-admission",
    ]
    assert "verify-tagged-release-admission" in jobs["publish-pypi"]["needs"]
    assert "verify-tagged-release-admission" in jobs["publish-github"]["needs"]
    assert "verify-publication-authority" in jobs["publish-pypi"]["needs"]
    authority = jobs["verify-publication-authority"]
    assert authority["environment"] == "release-identity"
    authority_run = next(
        step["run"]
        for step in authority["steps"]
        if step["name"] == "Require the exact release authority before any publication"
    )
    assert "EXPECTED_APP_ID" in authority_run
    assert "EXPECTED_INSTALLATION_ID" in authority_run
    assert "verify-tag-rulesets" in authority_run
    assert "immutable-releases" in authority_run
    assert "releases/$EXPECTED_RELEASE_ID" in authority_run
    assert "releases/assets/$asset_id" in authority_run
    assert "scripts/verify_github_release.py" in authority_run
    staging = next(
        step
        for step in authority["steps"]
        if step["name"] == "Materialize the admitted App-authored publication staging"
    )
    assert "verify-publication-staging" in staging["run"]
    assert authority["steps"].index(staging) < authority["steps"].index(
        next(step for step in authority["steps"] if step.get("id") == "release-app")
    )


def test_release_workflow_rechecks_main_immediately_before_the_app_tag_api_write():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    create = document["jobs"]["create-release-tag"]

    candidate = next(step for step in create["steps"] if step.get("id") == "candidate")
    tag = next(
        step
        for step in create["steps"]
        if step["name"] == "Create and re-read only the admitted annotated release tag"
    )

    # The candidate check alone is not sufficient. main can advance before the
    # separate tag-push step starts, so that step must get and compare main too.
    assert "git fetch --no-tags origin" in candidate["run"]
    assert "git fetch --no-tags origin" in tag["run"]
    assert 'current_main="$(git rev-parse refs/remotes/origin/main)"' in tag["run"]
    assert 'current_main" != "$GITHUB_SHA' in tag["run"]
    assert tag["run"].rindex('current_main" != "$GITHUB_SHA') < tag["run"].index(
        "/git/tags"
    )


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
    assert guard["env"]["EXPECTED_BOT_ID"] == "321543906"
    assert guard["env"]["EXPECTED_DISPATCHER_ID"] == "774615"
    assert 'GITHUB_REF_TYPE" != "tag' in guard["run"]
    assert 'GITHUB_ACTOR_ID" != "$EXPECTED_BOT_ID' in guard["run"]
    assert 'GITHUB_ACTOR_ID" != "$EXPECTED_DISPATCHER_ID' in guard["run"]
    assert 'GITHUB_REF_TYPE" != "tag' in guard["run"]
    assert 'GITHUB_REF" != "refs/tags/$RELEASE_TAG' in guard["run"]
    assert 'current_main" != "$GITHUB_SHA' not in guard["run"]
    assert 'RELEASE_TAG" != "$expected_tag' in guard["run"]
    assert "git merge-base --is-ancestor HEAD refs/remotes/origin/main" in guard["run"]

    pypi = jobs["publish-pypi"]
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

    assert jobs["publish-github"]["environment"] == "release-identity"
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
        "ACTUAL_INSTALLATION_ID": "${{ steps.release-app.outputs.installation-id }}",
        "CONFIGURED_APP_ID": "${{ vars.OPENADAPT_RELEASE_APP_ID }}",
        "EXPECTED_APP_SLUG": "openadapt-release",
        "EXPECTED_APP_ID": "4730708",
        "EXPECTED_INSTALLATION_ID": "156835568",
    }
    assert 'ACTUAL_APP_SLUG" != "$EXPECTED_APP_SLUG' in identity["run"]
    publish = next(
        step
        for step in publish_steps
        if step["name"] == "Publish the GitHub Release and exact artifacts"
    )
    assert publish_steps.index(identity) < publish_steps.index(publish)
    assert publish["env"]["GH_TOKEN"] == "${{ steps.release-app.outputs.token }}"
    assert publish["env"]["RELEASE_TAG"] == (
        "v${{ needs.build-and-attest.outputs.version }}"
    )
    assert publish["env"]["EXPECTED_AUTHOR"] == "openadapt-release[bot]"
    assert publish["env"]["EXPECTED_AUTHOR_ID"] == "321543906"
    assert "verify-publication-staging" in publish["run"]
    assert "releases/$EXPECTED_DRAFT_RELEASE_ID" in publish["run"]
    assert "releases/assets/$asset_id" in publish["run"]
    assert "releases/tags/$RELEASE_TAG" in publish["run"]
    assert "verify-tag-rulesets" in publish["run"]
    assert '--release-id "$EXPECTED_DRAFT_RELEASE_ID"' in publish["run"]
    assert "--asset-ids /tmp/release-asset-ids.json" in publish["run"]
    assert '--target-commitish "$EXPECTED_SOURCE_COMMIT"' in publish["run"]
    assert '{"draft":false,"make_latest":"%s"}' in publish["run"]
    assert "/tmp/set-latest.json" not in publish["run"]
    assert "latest_update_status" not in publish["run"]
    assert publish["run"].index('make_latest="$(python') < publish["run"].index(
        'if [ "$release_state" = "draft" ]'
    )
    assert "release create" not in publish["run"]
    assert "release upload" not in publish["run"]
    assert "release edit" not in publish["run"]
    assert "--allow-missing" not in publish["run"]
    assert "--missing-output" not in publish["run"]
    assert "--clobber" not in publish["run"]
    assert "scripts/verify_github_release.py" in publish["run"]
    assert "--downloaded-dir /tmp/staged-github-release-assets" in publish["run"]
    assert 'release_status" != "200"' in publish["run"]
    assert 'tag_commit" != "$EXPECTED_SOURCE_COMMIT' in publish["run"]
    assert "immutable-releases" in publish["run"]
    assert "verify-immutable-releases" in publish["run"]
    assert '"$GH_CLI" release verify "$RELEASE_TAG"' in publish["run"]
    assert '"$GH_CLI" release verify-asset "$RELEASE_TAG" "$artifact"' in publish["run"]
    assert '--author-id "$EXPECTED_AUTHOR_ID"' in publish["run"]
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
    expected_artifact = "${{ needs.build-and-attest.outputs.artifact-name }}"
    assert pypi_download["with"]["name"] == expected_artifact
    assert github_download["with"]["name"] == expected_artifact
    assert transfer["with"]["name"] == "${{ steps.inventory.outputs.artifact-name }}"
    assert pypi_publish["with"] == {"skip-existing": True}
    assert github_publish["env"]["RELEASE_TAG"] == (
        "v${{ needs.build-and-attest.outputs.version }}"
    )

    pypi_checkout = next(
        step for step in pypi_steps if step["name"] == "Checkout the exact release tag"
    )
    assert pypi_checkout["with"] == {
        "ref": "refs/tags/v${{ needs.build-and-attest.outputs.version }}",
        "persist-credentials": False,
    }

    checkout = next(
        step
        for step in github_steps
        if step["name"] == "Checkout the exact release tag"
    )
    assert checkout["with"] == {
        "ref": "refs/tags/v${{ needs.build-and-attest.outputs.version }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }

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
    assert "use the reviewed recover operation" in run
    assert "Never create a recovery tag" in run
    assert "export TITLE" in run
