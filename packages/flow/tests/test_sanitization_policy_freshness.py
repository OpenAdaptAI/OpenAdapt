"""State transitions for one transformation identity across all publication paths."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from openadapt_flow import hosted, privacy
from openadapt_flow import sanitized_artifact as sa
from openadapt_flow.wrap import publish


class ConfiguredScrubber:
    def __init__(self, revision: int = 1):
        self.revision = revision

    def sanitization_config(self):
        return {"semantic_revision": self.revision}

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text

    def scrub_image(self, image, fill_color=None):
        return image


class AlternateScrubber(ConfiguredScrubber):
    pass


def changed_behavior(self, text: str, is_separated: bool = False) -> str:
    """A distinct implementation with identical output on this held-out input."""
    return str(text)


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch, tmp_path):
    privacy.set_text_scrubber(ConfiguredScrubber())
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(hosted.AUTO_APPROVE_ENV, raising=False)
    monkeypatch.delenv(hosted.DESTINATION_KIND_ENV, raising=False)
    monkeypatch.delenv(hosted.TRUSTED_HOSTS_ENV, raising=False)
    yield
    privacy.reset_scrubbers()


def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    # A minimal structured sibling supports both artifact kinds. It is not a
    # compiled or admitted program, and none of these tests claims execution.
    (source / "workflow.json").write_text('{"description":"synthetic note"}')
    (source / "notes.txt").write_text("synthetic note")
    return source


def derive(entrypoint, source, destination, kind):
    if entrypoint == "sanitize":
        return sa.ensure_current_derivative(source, destination, kind=kind)
    if entrypoint == "wrap":
        return publish._derive(source, destination, kind)
    result = hosted.push(
        source,
        sanitized_out=destination,
        kind=kind,
        host=hosted.DEFAULT_HOST,
        token="test-only-token",
    )
    assert result["pending_review"] is True
    path = Path(result["sanitized_path"])
    return path, sa.load_and_verify_derivative(path)


def snapshot(path):
    result = {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in path.rglob("*")
        if p.is_file()
    }
    archive = sa.approved_archive_path(path)
    if archive.exists():
        result["<approved-archive>"] = archive.read_bytes()
    return result


@pytest.mark.parametrize("entrypoint", ["sanitize", "wrap", "hosted"])
def test_transformation_state_machine_preserves_generations_and_approvals(
    tmp_path,
    monkeypatch,
    entrypoint,
):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("review cannot upload")
    )
    source = source_tree(tmp_path)
    destination = tmp_path / "derivative"
    kind = "recording"
    current, manifest = derive(entrypoint, source, destination, kind)
    sa.approve_derivative(current, source=source, reviewer="synthetic reviewer")
    generations = [(current, snapshot(current))]
    assert derive(entrypoint, source, destination, kind) == (
        current,
        sa.load_and_verify_derivative(current),
    )
    # Distinct transitions all invalidate the same identity even where the
    # transformed payload remains byte-identical. Kind/config are held-out
    # dimensions beyond the original policy-change defect.
    for transition in ("source", "config", "policy", "kind", "provider", "behavior"):
        old, old_manifest = current, manifest
        if transition == "source":
            (source / "notes.txt").write_text("another synthetic note")
        elif transition == "config":
            privacy.set_text_scrubber(ConfiguredScrubber(2))
        elif transition == "policy":
            monkeypatch.setattr(sa, "POLICY_VERSION", "outbound-phi-test-next")
        elif transition == "kind":
            kind = "bundle"
        elif transition == "provider":
            privacy.set_text_scrubber(AlternateScrubber(2))
        else:
            monkeypatch.setattr(ConfiguredScrubber, "scrub_text", changed_behavior)
        current, manifest = derive(entrypoint, source, destination, kind)
        assert current != old
        assert (
            manifest["transformation_sha256"] != old_manifest["transformation_sha256"]
        )
        if transition != "source":
            assert (
                manifest["derivative_tree_sha256"]
                == old_manifest["derivative_tree_sha256"]
            )
        assert not sa.approval_path(current).exists()
        # A rescan checks the current generation; it does not approve it.
        sa._rescan_derivative(current)
        with pytest.raises(sa.SanitizationError, match="not been approved"):
            sa.load_valid_approval(current)
        sa.approve_derivative(current, source=source, reviewer="synthetic reviewer")
        assert (
            sa.load_valid_approval(current)["transformation_sha256"]
            == manifest["transformation_sha256"]
        )
        assert derive(entrypoint, source, destination, kind)[0] == current
        generations.append((current, snapshot(current)))
        for prior, expected in generations:
            assert snapshot(prior) == expected
        if transition in {"config", "policy", "provider", "behavior"}:
            with pytest.raises(sa.SanitizationError, match="identity changed"):
                sa.load_valid_approval(old)


@pytest.mark.parametrize("entrypoint", ["sanitize", "wrap", "hosted"])
def test_legacy_identity_cannot_be_reused_or_approved(tmp_path, entrypoint):
    source = source_tree(tmp_path)
    destination = tmp_path / "legacy"
    sa.sanitize_artifact(source, destination, kind="recording")
    sa.approve_derivative(destination, source=source, reviewer="synthetic reviewer")
    manifest_path = destination / sa.MANIFEST_NAME
    legacy = json.loads(manifest_path.read_text())
    legacy.pop("transformation")
    legacy.pop("transformation_sha256")
    manifest_path.write_text(json.dumps(legacy))
    before = snapshot(destination)
    for action in (
        sa.load_and_verify_derivative,
        sa.load_valid_approval,
        sa._rescan_derivative,
    ):
        with pytest.raises(sa.SanitizationError, match="identity is stale or missing"):
            action(destination)
    fresh, _ = derive(entrypoint, source, destination, "recording")
    assert fresh != destination
    assert snapshot(destination) == before
    assert not sa.approval_path(fresh).exists()


def test_approval_must_explicitly_bind_current_transformation(tmp_path):
    source = source_tree(tmp_path)
    destination, manifest = sa.ensure_current_derivative(
        source, tmp_path / "current", kind="recording"
    )
    approval = sa.approve_derivative(
        destination, source=source, reviewer="synthetic reviewer"
    )
    assert approval["transformation_sha256"] == manifest["transformation_sha256"]
    approval.pop("transformation_sha256")
    sa.approval_path(destination).write_text(json.dumps(approval))
    with pytest.raises(sa.SanitizationError, match="transformation identity changed"):
        sa.load_valid_approval(destination)


def test_provider_change_during_transform_cannot_create_completed_manifest(tmp_path):
    class MutatingScrubber(ConfiguredScrubber):
        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            self.revision += 1
            return text

    privacy.set_text_scrubber(MutatingScrubber())
    source = source_tree(tmp_path)
    destination = tmp_path / "changed-during-transform"
    with pytest.raises(
        sa.SanitizationError, match="identity changed during transformation"
    ):
        sa.sanitize_artifact(source, destination, kind="recording")
    assert not destination.exists()


def test_unknown_provider_state_is_never_exported_as_config(tmp_path):
    class UndeclaredProvider:
        def __init__(self):
            self.credential = "private-test-value-never-export"

        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            return text

    privacy.set_text_scrubber(UndeclaredProvider())
    source = source_tree(tmp_path)
    destination = tmp_path / "unknown-config"
    with pytest.raises(sa.SanitizationError, match="declare complete non-secret") as exc:
        sa.sanitize_artifact(source, destination, kind="recording")
    assert "private-test-value-never-export" not in str(exc.value)
    assert not destination.exists()


def test_manual_override_has_one_reviewable_generation_per_top_level_request(
    tmp_path,
    monkeypatch,
):
    source = source_tree(tmp_path)
    overrides = tmp_path / "local-redactions.json"
    literal = "synthetic note"
    overrides.write_text(
        json.dumps({"text": [{"find": literal, "replacement": "[LOCAL]"}]})
    )
    base = tmp_path / "manual"
    first, manifest = sa.ensure_current_derivative(
        source, base, kind="recording", redactions_file=overrides
    )
    assert manifest["transformation"]["manual_overrides"] is True
    assert literal not in json.dumps(manifest["transformation"])
    sa._rescan_derivative(first)
    assert not sa.approval_path(first).exists()
    approved = sa.approve_derivative(
        first, source=source, reviewer="synthetic reviewer"
    )
    before = snapshot(first)
    uploaded = []

    def post(_url, **kwargs):
        uploaded.append(kwargs["files"]["file"][1].read())
        envelope = json.loads(kwargs["data"]["sanitization_manifest"])
        return httpx.Response(
            201,
            json={
                "ingest": {
                    "workflow_id": None,
                    "artifact_ingest_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "kind": "recording",
                    "artifact_sha256": envelope["artifact"]["sha256"],
                    "status": "needs_parameterization",
                }
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    result = hosted.push(
        first, kind="recording", host=hosted.DEFAULT_HOST, token="test-only-token"
    )
    assert result["uploaded"] is True
    assert uploaded == [sa.read_approved_archive(first)]
    assert sa.load_valid_approval(first) == approved
    assert snapshot(first) == before
    second, _ = sa.ensure_current_derivative(
        source, base, kind="recording", redactions_file=overrides
    )
    assert second != first
    assert not sa.approval_path(second).exists()
    assert snapshot(first) == before


def test_builtin_provider_identity_tracks_complete_effective_configuration(
    tmp_path,
    monkeypatch,
):
    from openadapt_privacy.config import config
    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

    # Identity admission reads configuration and installed versions only. It
    # neither starts NLP/OCR nor reads the source through a remote provider.
    privacy.set_text_scrubber(PresidioScrubbingProvider())
    digest = sa.source_tree_sha256(source_tree(tmp_path))
    before = sa.current_transformation_identity(digest, "recording")
    monkeypatch.setattr(config, "SCRUB_FILL_COLOR", config.SCRUB_FILL_COLOR ^ 0x000001)
    after = sa.current_transformation_identity(digest, "recording")
    assert before["scrubber"]["config_sha256"] != after["scrubber"]["config_sha256"]
    assert before["source_tree_sha256"] == after["source_tree_sha256"]


def test_concurrent_generation_install_never_changes_existing_evidence(
    tmp_path,
    monkeypatch,
):
    source = source_tree(tmp_path)
    destination = tmp_path / "contended"
    real_write = sa._write_manifest

    def another_request_wins(staging, manifest):
        real_write(staging, manifest)
        destination.mkdir()
        (destination / "existing-evidence.txt").write_text("preserve this generation")

    monkeypatch.setattr(sa, "_write_manifest", another_request_wins)
    with pytest.raises(OSError):
        sa.sanitize_artifact(source, destination, kind="recording")
    assert snapshot(destination) == {
        "existing-evidence.txt": b"preserve this generation"
    }


def test_builtin_policy_cannot_change_and_reset_mid_scan(tmp_path, monkeypatch):
    from openadapt_privacy.config import ScrubbingPolicyChanged, config
    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

    def attempted_policy_mix(self, text: str, is_separated: bool = False) -> str:
        original = config.SCRUB_FILL_COLOR
        try:
            config.SCRUB_FILL_COLOR = original ^ 0x000001
        finally:
            # Only restore after a successful mutation. Under the whole-scan
            # immutable context the first assignment must already refuse.
            if config.SCRUB_FILL_COLOR != original:
                config.SCRUB_FILL_COLOR = original
        return text

    monkeypatch.setattr(PresidioScrubbingProvider, "scrub_text", attempted_policy_mix)
    privacy.set_text_scrubber(PresidioScrubbingProvider())
    source = source_tree(tmp_path)
    destination = tmp_path / "mixed-policy"
    with pytest.raises(ScrubbingPolicyChanged):
        sa.sanitize_artifact(source, destination, kind="recording")
    assert not destination.exists()
