"""State transitions for one transformation identity across all publication paths."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openadapt_flow import hosted, privacy, sanitization_policy
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
    with pytest.raises(
        sa.SanitizationError, match="declare complete non-secret"
    ) as exc:
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


def _freeze_source_file_stats(engine, monkeypatch):
    """Model a filesystem tick without depending on the host's clock."""
    stats = {path: path.stat() for path in engine.rglob("*.py")}
    real_stat = Path.stat

    def frozen_stat(path, *args, **kwargs):
        if path in stats:
            return stats[path]
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", frozen_stat)
    return sanitization_policy._source_inventory(engine)


def _content_identity_adapter(kind, engine, source, monkeypatch):
    if kind == "engine":
        monkeypatch.setattr(sanitization_policy, "_engine_source_root", lambda: engine)
        _freeze_source_file_stats(engine, monkeypatch)
        return (
            sanitization_policy._engine_source_sha256,
            lambda: sanitization_policy._source_inventory(engine),
        )

    # Export only the two pure production definitions and their constants.
    # No Windows module, process, provider or real executable is loaded.
    path = Path(__file__).parents[1] / "openadapt_flow/backends/win_agent/server.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {"_IMAGE_DIGESTS", "_IMAGE_DIGEST_CACHE_SIZE", "_IMAGE_DIGEST_MAX_BYTES"}
    functions = {"_file_key", "_image_file_digest"}
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            or isinstance(node, ast.FunctionDef)
            and node.name in functions
            or isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in names
            or isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
        )
    ]
    assert {n.name for n in selected if isinstance(n, ast.FunctionDef)} == functions
    frozen = source.stat()

    def observed_stat(path):
        assert Path(path) == source
        return frozen

    def read_owned(path, mode):
        assert Path(path) == source and mode == "rb"
        return source.open(mode)

    scope = {
        "__name__": "owned_content_identity_export",
        "os": SimpleNamespace(stat=observed_stat, path=os.path),
        "hashlib": hashlib,
        "open": read_owned,
    }
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(path), "exec", dont_inherit=True), scope)
    return (
        lambda: scope["_image_file_digest"](str(source)),
        lambda: scope["_file_key"](str(source)),
    )


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("engine", "__init__.py"),
        ("engine", "runtime/future_policy_helper.py"),
        ("image", "ordinary.bin"),
    ],
)
def test_content_identity_reads_current_bytes_with_identical_stats(
    tmp_path, monkeypatch, kind, relative
):
    """One fresh-content model across both confirmed metadata-cache callers."""
    engine = tmp_path / "engine"
    source = engine / relative
    source.parent.mkdir(parents=True)
    original, changed = b"POLICY = 1\n", b"POLICY = 2\n"
    assert len(original) == len(changed)
    source.write_bytes(original)
    identity, metadata = _content_identity_adapter(kind, engine, source, monkeypatch)
    before = metadata()
    first = identity()
    assert first is not None

    source.write_bytes(changed)
    assert source.read_bytes() == changed
    assert metadata() == before
    assert identity() != first

    source.write_bytes(original)
    assert metadata() == before
    assert identity() == first

    source.unlink()
    if kind == "engine":
        with pytest.raises(ValueError, match="source is unavailable"):
            identity()
    else:
        assert identity() is None


def test_engine_source_closure_binds_future_helpers_and_is_location_independent(
    tmp_path,
    monkeypatch,
):
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "__init__.py").write_text("POLICY = 1\n")
    monkeypatch.setattr(sanitization_policy, "_engine_source_root", lambda: engine)
    first = sanitization_policy._engine_source_sha256()
    clone = tmp_path / "clone"
    shutil.copytree(engine, clone)
    monkeypatch.setattr(sanitization_policy, "_engine_source_root", lambda: clone)
    assert sanitization_policy._engine_source_sha256() == first
    helper = clone / "runtime" / "future_policy_helper.py"
    helper.parent.mkdir()
    helper.write_text("PROTECTED_CAPTURE = True\n")
    assert sanitization_policy._engine_source_sha256() != first
    helper.unlink()
    assert sanitization_policy._engine_source_sha256() == first
    (clone / "__init__.py").write_text("POLICY = 2\n")
    assert sanitization_policy._engine_source_sha256() != first


@pytest.mark.parametrize("entrypoint", ["sanitize", "wrap", "hosted"])
@pytest.mark.parametrize("change", ["new_helper", "same_stat_rewrite"])
def test_engine_source_change_invalidates_all_reuse_and_approval_paths(
    tmp_path,
    monkeypatch,
    entrypoint,
    change,
):
    engine = tmp_path / "engine"
    engine.mkdir()
    policy = engine / "__init__.py"
    policy.write_bytes(b"POLICY = 1\n")
    monkeypatch.setattr(sanitization_policy, "_engine_source_root", lambda: engine)
    if change == "same_stat_rewrite":
        inventory = _freeze_source_file_stats(engine, monkeypatch)
    source = source_tree(tmp_path)
    base = tmp_path / "derivative"
    old, before = derive(entrypoint, source, base, "recording")
    sa.approve_derivative(old, source=source, reviewer="synthetic reviewer")
    preserved = snapshot(old)
    if change == "new_helper":
        helper = engine / "runtime" / "new_capture_policy.py"
        helper.parent.mkdir()
        helper.write_text("PROTECTED_CAPTURE = True\n")
    else:
        policy.write_bytes(b"POLICY = 2\n")
        assert policy.read_bytes() == b"POLICY = 2\n"
        assert sanitization_policy._source_inventory(engine) == inventory
    with pytest.raises(sa.SanitizationError, match="identity changed"):
        sa.load_valid_approval(old)
    current, after = derive(entrypoint, source, base, "recording")
    assert current != old
    assert after["derivative_tree_sha256"] == before["derivative_tree_sha256"]
    assert after["transformation_sha256"] != before["transformation_sha256"]
    assert not sa.approval_path(current).exists()
    assert snapshot(old) == preserved


def test_whole_scan_and_approval_rescan_enter_provider_policy_context(tmp_path):
    class BoundProvider(ConfiguredScrubber):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.operations = 0

        @contextmanager
        def sanitization_operation(self):
            self.active += 1
            self.operations += 1
            try:
                yield
            finally:
                self.active -= 1

        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            assert self.active > 0, "the whole scan must bind the provider policy"
            return text

    provider = BoundProvider()
    privacy.set_text_scrubber(provider)
    source = source_tree(tmp_path)
    destination = tmp_path / "bound-operation"
    sa.sanitize_artifact(source, destination, kind="recording")
    assert provider.active == 0 and provider.operations == 1
    sa.approve_derivative(destination, source=source, reviewer="synthetic reviewer")
    assert provider.active == 0 and provider.operations == 2
