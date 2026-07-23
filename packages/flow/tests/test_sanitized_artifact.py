from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import jsonschema
import pytest
from PIL import Image, ImageDraw

from openadapt_flow import hosted, privacy
from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.codegen import render_workflow_py
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    BackendHints,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.policy import lint_workflow
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.sanitized_artifact import (
    APPROVAL_NAME,
    MANIFEST_NAME,
    SanitizationError,
    _review_content_length,
    _valid_review_host,
    add_manual_image_redaction,
    add_manual_text_redaction,
    approve_derivative,
    approved_archive_path,
    build_ingest_manifest,
    load_and_verify_derivative,
    load_valid_approval,
    render_review_html,
    sanitize_artifact,
)


class FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("Jane Doe", "[PERSON]").replace("MRN-123", "[MRN]")

    def scrub_image(self, image, fill_color=None):
        # Tests verify the image handler and exact-byte lifecycle. Detection
        # geometry belongs to openadapt-privacy's own provider tests.
        return image


class SyntaxHostileScrubber:
    """Model current Presidio false positives on serialized runtime data."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        if text.startswith("<") and text.endswith(">"):
            return text
        if text in {"created_at", "steps"}:
            return "<ORGANIZATION>"
        text = re.sub(r'"https?://[^\"]+"', "<URL>", text)
        if text.startswith(("http://", "https://")):
            return "<URL>"
        if text == "0000_after":
            return "<DATE_TIME>"
        text = text.replace("1280", "<DATE_TIME>")
        text = re.sub(r"2026-07-16T[0-9:.+-]+", "<DATE_TIME>", text)
        text = text.replace(".png", "_<URL>g")
        text = text.replace("Jane Doe", "<PERSON>")
        if re.fullmatch(r"[0-9a-f]{32}", text):
            return "<ORGANIZATION>"
        return text

    def scrub_image(self, image, fill_color=None):
        return image


@pytest.fixture(autouse=True)
def _scrubber(tmp_path, monkeypatch):
    privacy.set_text_scrubber(FakeScrubber())
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(hosted.DESTINATION_KIND_ENV, raising=False)
    monkeypatch.delenv(hosted.TRUSTED_HOSTS_ENV, raising=False)
    yield
    privacy.reset_scrubbers()


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


def _recording(tmp_path: Path) -> Path:
    source = tmp_path / "recording"
    (source / "frames").mkdir(parents=True)
    (source / "meta.json").write_text('{"patient":"Jane Doe"}', encoding="utf-8")
    (source / "events.jsonl").write_text('{"text":"MRN-123"}\n', encoding="utf-8")
    (source / "frames" / "before.png").write_bytes(_png())
    return source


class _RuntimeBackend:
    def __init__(self):
        self.actions: list[tuple[str, str]] = []

    @property
    def viewport(self):
        return (8, 8)

    def screenshot(self):
        return _png()

    def type_text(self, value):
        self.actions.append(("type", value))

    def click(self, x, y, *, double=False):
        raise AssertionError("type-only parameter test must not click")

    def press(self, key):
        raise AssertionError("type-only parameter test must not press")

    def scroll(self, dx, dy):
        raise AssertionError("type-only parameter test must not scroll")


class _RuntimeVision:
    def wait_settled(self, backend, **kwargs):
        return backend.screenshot()

    def pixels_changed(self, before, after, *, region=None, **kwargs):
        return True

    def ocr(self, png, *, region=None):
        return []


def test_derivative_inventory_rescan_and_original_immutable(tmp_path):
    source = _recording(tmp_path)
    before = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    dest = tmp_path / "sanitized"

    manifest = sanitize_artifact(source, dest, kind="recording")

    assert manifest["coverage_complete"] is True
    assert manifest["source_file_count"] == manifest["processed_file_count"] == 3
    assert {entry["verification"] for entry in manifest["files"]} == {
        "stable-second-pass"
    }
    assert manifest["runtime_semantics_validated"] is False
    assert manifest["trusted_boundary_required_at_runtime"] is True
    assert "Jane Doe" not in (dest / "meta.json").read_text()
    assert "MRN-123" not in (dest / "events.jsonl").read_text()
    assert before == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert (
        load_and_verify_derivative(dest)["derivative_tree_sha256"]
        == manifest["derivative_tree_sha256"]
    )


def test_recording_json_scrubbing_preserves_syntax_and_scalar_types(tmp_path):
    privacy.set_text_scrubber(SyntaxHostileScrubber())
    source = tmp_path / "recording"
    frames = source / "frames"
    frames.mkdir(parents=True)
    (source / "meta.json").write_text(
        json.dumps(
            {
                "id": "c770c8bdbb6c4864a5e29eec19f4bafd",
                "created_at": "2026-07-16T01:38:54.430400+00:00",
                "viewport": [1280, 800],
                "app_url": "https://the-internet.herokuapp.com/key_presses",
                "params": {"Jane Doe": "Jane Doe"},
                "secret_params": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (source / "events.jsonl").write_text(
        json.dumps(
            {
                "i": 0,
                "kind": "key",
                "key": "Enter",
                "url_before": "https://the-internet.herokuapp.com/key_presses",
                "title_before": "The Internet",
                "pages_before": 1,
                "url_after": "https://the-internet.herokuapp.com/key_presses",
                "title_after": "The Internet",
                "pages_after": 1,
                "t": 0.809,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = Image.new("RGB", (320, 200), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((100, 70, 220, 130), fill="black")
    before.save(frames / "0000_before.png", format="PNG")
    after.save(frames / "0000_after.png", format="PNG")

    destination = tmp_path / "sanitized"
    sanitize_artifact(source, destination, kind="recording")
    sanitized_meta = json.loads((destination / "meta.json").read_text())
    sanitized_event = json.loads((destination / "events.jsonl").read_text())

    assert sanitized_meta["viewport"] == [1280, 800]
    assert "created_at" in sanitized_meta
    assert sanitized_meta["app_url"] == "<URL>"
    assert sanitized_meta["params"] == {"<PERSON>": "<PERSON>"}
    assert sanitized_event["url_before"] == "<URL>"
    assert sanitized_event["url_after"] == "<URL>"
    assert (destination / "frames" / "0000_before.png").is_file()
    assert (destination / "frames" / "0000_after.png").is_file()
    approve_derivative(destination, source=source, reviewer="privacy-reviewer")
    workflow = compile_recording(
        destination, tmp_path / "bundle", name="sanitized-enter"
    )
    assert workflow.steps[0].expect
    assert not any(
        issue.code == "vacuous_postcondition"
        for issue in lint_workflow(workflow).findings
    )


def test_zero_phi_bundle_preserves_exact_executable_bytes(tmp_path):
    privacy.set_text_scrubber(SyntaxHostileScrubber())
    bundle = tmp_path / "bundle"
    template = bundle / "templates" / "step_000_expect.png"
    template.parent.mkdir(parents=True)
    template.write_bytes(_png())
    workflow = Workflow(
        name="public-enter-qualification",
        recording_id="c770c8bdbb6c4864a5e29eec19f4bafd",
        viewport=(1280, 800),
        steps=[
            Step(
                id="step_000",
                intent="press Enter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.REGION_STABLE,
                        region=(0, 0, 8, 8),
                        phash="0000000000000000",
                        template="templates/step_000_expect.png",
                    )
                ],
            )
        ],
    )
    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow), encoding="utf-8")
    source_bytes = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "preserved"
    assert {
        rel: (destination / rel).read_bytes() for rel in source_bytes
    } == source_bytes
    approve_derivative(destination, source=bundle, reviewer="privacy-reviewer")
    Workflow.load(destination)


def test_phi_bearing_canonical_bundle_is_scrubbed_resealed_and_loadable(tmp_path):
    bundle = tmp_path / "bundle"
    template = bundle / "templates" / "step_000.png"
    template.parent.mkdir(parents=True)
    template.write_bytes(_png())
    workflow = Workflow(
        name="Jane Doe intake",
        recording_id="c770c8bdbb6c4864a5e29eec19f4bafd",
        viewport=(8, 8),
        backend_hints=BackendHints(
            backend="citrix",
            rdp_window="wfica32",
            rdp_window_title="Jane Doe - Appointments",
            rdp_readiness_text="MRN-123",
        ),
        steps=[
            Step(
                id="step_000",
                intent="open Jane Doe",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/step_000.png",
                    region=(0, 0, 8, 8),
                    click_point=(4, 4),
                    structural=StructuralLocator(
                        selector="button[data-patient='Jane Doe']",
                        role="button",
                        name="Jane Doe",
                    ),
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.REGION_STABLE,
                        region=(0, 0, 8, 8),
                        phash="0000000000000000",
                        template="templates/step_000.png",
                    )
                ],
            )
        ],
    )
    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow), encoding="utf-8")

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "not-preserved"
    text_bytes = b"".join(
        path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file() and path.suffix in {".json", ".py"}
    )
    assert b"Jane Doe" not in text_bytes
    assert b"MRN-123" not in text_bytes
    assert Workflow.load(destination).steps[0].anchor is not None
    sanitized_hints = Workflow.load(destination).backend_hints
    assert sanitized_hints is not None
    assert sanitized_hints.rdp_window_title == "[PERSON] - Appointments"
    assert sanitized_hints.rdp_readiness_text == "[MRN]"
    approve_derivative(destination, source=bundle, reviewer="privacy-reviewer")


def test_manual_redactions_invalidate_approval_and_reapproval_is_deterministic(
    tmp_path,
):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    first = approve_derivative(dest, source=source, reviewer="alice")
    first_bytes = approved_archive_path(dest).read_bytes()
    assert load_valid_approval(dest)["reviewer"] == "alice"
    with zipfile.ZipFile(approved_archive_path(dest)) as archive:
        embedded = json.loads(archive.read(MANIFEST_NAME))
    assert embedded["approval_verification"] == {
        "verified_at": first["verification"]["verified_at"],
        "method": "full-stable-rescan",
        "file_count": len(embedded["files"]),
        "unresolved": 0,
    }

    add_manual_text_redaction(dest, "meta.json", "[PERSON]", "[REDACTED]")
    assert "approval_verification" not in load_and_verify_derivative(dest)
    assert not (dest / APPROVAL_NAME).exists()
    assert not approved_archive_path(dest).exists()
    with pytest.raises(SanitizationError, match="not been approved"):
        load_valid_approval(dest)
    second = approve_derivative(dest, source=source, reviewer="alice")
    assert second["approved_derivative_sha256"] != first["approved_derivative_sha256"]
    assert approved_archive_path(dest).read_bytes() != first_bytes

    # Re-approving unchanged content produces byte-identical archive output.
    second_bytes = approved_archive_path(dest).read_bytes()
    approve_derivative(dest, source=source, reviewer="bob")
    assert approved_archive_path(dest).read_bytes() == second_bytes


def test_approval_rescan_rejects_phi_introduced_by_manual_replacement(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    add_manual_text_redaction(dest, "meta.json", "[PERSON]", "Jane Doe")
    with pytest.raises(SanitizationError, match="remaining identifier"):
        approve_derivative(dest, source=source, reviewer="alice")


def test_approval_rescan_rejects_manual_replacement_that_breaks_json(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    add_manual_text_redaction(dest, "meta.json", "[PERSON]", 'bad"json')
    with pytest.raises(SanitizationError, match="structured artifact invalid"):
        approve_derivative(dest, source=source, reviewer="alice")


def test_approval_rejects_source_changed_after_sanitization(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    (source / "meta.json").write_text('{"patient":"another patient"}')

    with pytest.raises(SanitizationError, match="Original source changed"):
        approve_derivative(dest, source=source, reviewer="alice")


def test_manual_image_rectangle_and_tamper_detection(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    add_manual_image_redaction(dest, "frames/before.png", x=1, y=1, width=3, height=3)
    approve_derivative(dest, source=source, reviewer="alice")
    (dest / "meta.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(SanitizationError, match="changed|inventoried"):
        load_valid_approval(dest)


def test_derivative_symlink_substitution_is_refused(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    original = dest / "meta.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    try:
        original.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(SanitizationError, match="symlink"):
        approve_derivative(dest, source=source, reviewer="alice")


@pytest.mark.parametrize("rel", ["../../outside.txt", "/tmp/outside.txt"])
def test_manual_redaction_path_traversal_cannot_modify_outside(tmp_path, rel):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    outside = tmp_path / "outside.txt"
    outside.write_text("Jane Doe")
    with pytest.raises(SanitizationError, match="authorized"):
        add_manual_text_redaction(dest, rel, "Jane Doe")
    assert outside.read_text() == "Jane Doe"


def test_review_host_and_content_length_guards():
    assert _valid_review_host("127.0.0.1:43121", 43121)
    assert _valid_review_host("localhost:43121", 43121)
    assert not _valid_review_host("evil.example:43121", 43121)
    assert not _valid_review_host(None, 43121)
    assert _review_content_length("0") == 0
    assert _review_content_length("1000000") == 1_000_000
    for value in (None, "abc", "-1", "1000001"):
        with pytest.raises(SanitizationError, match="Content-Length|length"):
            _review_content_length(value)


def test_ingest_envelope_binds_approved_archive(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approval = approve_derivative(dest, source=source, reviewer="alice")

    envelope = build_ingest_manifest(dest)

    archive = approved_archive_path(dest)
    assert envelope["schema"] == "openadapt.sanitization/v1"
    assert envelope["artifact"]["kind"] == "recording"
    assert (
        envelope["artifact"]["sha256"]
        == hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert envelope["artifact"]["size_bytes"] == archive.stat().st_size
    assert envelope["artifact"]["execution_semantics"] == (
        "requires-parameterization-validation"
    )
    assert envelope["artifact"]["runtime_semantics_validated"] is False
    assert envelope["artifact"]["trusted_boundary_required_at_runtime"] is True
    assert envelope["coverage"] == {
        "complete": True,
        "media_types": ["image", "text"],
    }
    assert envelope["findings"] == {"unresolved": 0}
    assert envelope["approval"]["method"] == "human"
    assert approval["verification"]["source_tree_verified"] is True
    assert (
        envelope["approval"]["artifact_sha256"]
        == approval["approved_derivative_sha256"]
    )


def test_automatic_ingest_approval_requires_and_uses_policy_signing_key(
    tmp_path, monkeypatch
):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(
        dest, source=source, reviewer="policy:outbound-phi-v1", automatic=True
    )
    with pytest.raises(SanitizationError, match="POLICY_KEY_ID"):
        build_ingest_manifest(dest)

    key = b"k" * 32
    monkeypatch.setenv("OPENADAPT_SANITIZATION_POLICY_KEY_ID", "phi-policy-2026-01")
    monkeypatch.setenv(
        "OPENADAPT_SANITIZATION_POLICY_KEY", base64.b64encode(key).decode()
    )
    envelope = build_ingest_manifest(dest)
    assert envelope["approval"]["method"] == "policy"
    assert envelope["approval"]["policy_key_id"] == "phi-policy-2026-01"
    assert re.fullmatch(r"[a-f0-9]{64}", envelope["approval"]["policy_signature"])


def test_local_viewer_is_self_contained_and_warns_about_residual_risk(tmp_path):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")

    page = render_review_html(source, dest, "csrf-token")

    assert "127.0.0.1" in page
    assert "loads no remote assets" in page
    assert "Automated scrubbing can miss" in page
    assert "Add text redaction" in page
    assert "Add rectangle" in page
    assert "Approve exact derivative hash" in page
    assert "https://" not in page

    (source / "meta.json").write_text('{"patient":"changed"}')
    with pytest.raises(SanitizationError, match="Original source changed"):
        render_review_html(source, dest, "csrf-token")


def test_phi_bearing_source_name_is_not_stored_raw(tmp_path):
    source = _recording(tmp_path)
    named = tmp_path / "Jane Doe"
    source.rename(named)
    dest = tmp_path / "sanitized"
    manifest = sanitize_artifact(named, dest, kind="recording")
    assert "source_display_name" not in manifest
    assert len(manifest["source_name_sha256"]) == 64
    assert "Jane Doe" not in (dest / MANIFEST_NAME).read_text()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("recording.sqlite", b"SQLite format 3\x00Jane Doe"),
        ("capture.mp4", b"video-Jane Doe"),
        ("dictation.wav", b"audio-Jane Doe"),
        ("nested.zip", b"PK\x03\x04Jane Doe"),
        ("payload.bin", b"unknown-Jane Doe"),
    ],
)
def test_unsupported_database_media_archive_and_unknown_never_claim_coverage(
    tmp_path, name, data
):
    source = _recording(tmp_path)
    (source / name).write_bytes(data)
    dest = tmp_path / "sanitized"

    with pytest.raises(SanitizationError, match=name):
        sanitize_artifact(source, dest, kind="recording")

    assert not (dest / MANIFEST_NAME).exists()


def test_nested_archive_is_detected_by_content_and_never_traversed_or_copied(tmp_path):
    source = _recording(tmp_path)
    nested = source / "looks-safe.txt"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.writestr("deep/patient.txt", "Jane Doe")
    dest = tmp_path / "sanitized"
    with pytest.raises(SanitizationError, match="ZIP/archive"):
        sanitize_artifact(source, dest, kind="recording")
    assert not dest.exists()


def test_database_magic_cannot_bypass_policy_with_text_extension(tmp_path):
    source = _recording(tmp_path)
    (source / "looks-safe.txt").write_bytes(b"SQLite format 3\x00Jane Doe")
    with pytest.raises(SanitizationError, match="SQLite database"):
        sanitize_artifact(source, tmp_path / "sanitized", kind="recording")


def test_bundle_transform_marks_runtime_semantics_invalid(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text('{"patient":"Jane Doe"}')
    dest = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, dest, kind="bundle")
    assert manifest["execution_semantics"] == "not-preserved"
    assert manifest["runtime_semantics_validated"] is False


def test_unchanged_bundle_is_privacy_stable_but_not_runtime_validated(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text('{"patient":"[PERSON]"}')

    manifest = sanitize_artifact(bundle, tmp_path / "sanitized", kind="bundle")

    assert manifest["execution_semantics"] == "preserved"
    assert manifest["runtime_semantics_validated"] is False
    assert manifest["trusted_boundary_required_at_runtime"] is True


def test_compiled_template_bundle_preserves_exact_verified_image_bytes(tmp_path):
    """A no-op privacy pass must not re-encode load-bearing template evidence."""
    source = tmp_path / "recording"
    frames = source / "frames"
    frames.mkdir(parents=True)

    before = Image.new("RGB", (320, 200), "white")
    draw = ImageDraw.Draw(before)
    draw.rectangle((90, 70, 230, 130), outline="black", width=4)
    after = before.copy()
    ImageDraw.Draw(after).rectangle((250, 20, 290, 50), fill="green")
    before.save(frames / "0000_before.png", format="PNG")
    after.save(frames / "0000_after.png", format="PNG")
    (source / "events.jsonl").write_text(
        json.dumps({"i": 0, "kind": "click", "x": 160, "y": 100, "t": 1.0}) + "\n"
    )
    (source / "meta.json").write_text(
        json.dumps(
            {
                "id": "template-recording",
                "created_at": "2026-07-15T00:00:00+00:00",
                "viewport": [320, 200],
                "app_url": "http://example.test/",
                "params": {},
            }
        )
    )

    recording = tmp_path / "recording-sanitized"
    sanitize_artifact(source, recording, kind="recording")
    approve_derivative(recording, source=source, reviewer="alice")
    bundle = tmp_path / "bundle"
    compile_recording(recording, bundle, name="template-workflow")
    bundle_images = sorted(
        path for path in bundle.rglob("*") if path.suffix.lower() == ".png"
    )
    assert bundle_images, "non-empty click recording must compile template evidence"
    original_bytes = {
        path.relative_to(bundle): path.read_bytes() for path in bundle_images
    }

    derivative = tmp_path / "bundle-sanitized"
    manifest = sanitize_artifact(bundle, derivative, kind="bundle")

    assert manifest["execution_semantics"] == "preserved"
    assert {
        rel: (derivative / rel).read_bytes() for rel in original_bytes
    } == original_bytes


def test_parameterized_sanitized_recording_compiles_and_injects_runtime_value(tmp_path):
    """Privacy approval is distinct from runtime semantics, but a simple
    placeholder-aware TYPE recording remains usable after sanitization."""
    source = tmp_path / "recording"
    source.mkdir()
    (source / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-1",
                "created_at": "2026-07-15T00:00:00+00:00",
                "viewport": [8, 8],
                "params": {"patient": "Jane Doe"},
            }
        )
    )
    (source / "events.jsonl").write_text(
        json.dumps(
            {
                "i": 0,
                "kind": "type",
                "text": "Jane Doe",
                "param": "patient",
                "t": 1.0,
            }
        )
        + "\n"
    )
    derivative = tmp_path / "sanitized"
    manifest = sanitize_artifact(source, derivative, kind="recording")
    approve_derivative(derivative, source=source, reviewer="alice")
    assert manifest["execution_semantics"] == "requires-parameterization-validation"

    bundle = tmp_path / "bundle"
    compile_recording(derivative, bundle, name="patient-search")
    at_rest = b"".join(
        path.read_bytes()
        for path in [*derivative.rglob("*"), *bundle.rglob("*")]
        if path.is_file()
    )
    assert b"Jane Doe" not in at_rest
    assert b"Bob" not in at_rest

    backend = _RuntimeBackend()
    report = Replayer(backend, vision=_RuntimeVision()).run(
        Workflow.load(bundle),
        params={"patient": "Bob"},
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )
    assert report.success is True
    assert backend.actions == [("type", "Bob")]
    assert report.params == {"patient": "Bob"}
    # Runtime PHI belongs in the trusted run boundary, not the design artifact.
    assert "Bob" in (tmp_path / "run" / "report.json").read_text()


def test_destination_policy_is_explicit_and_independent_of_lane(monkeypatch):
    with pytest.raises(hosted.HostedError, match="no recognized trust"):
        hosted.resolve_destination_policy("https://control.customer.test")
    with pytest.raises(hosted.HostedError, match="allowlist"):
        hosted.resolve_destination_policy(
            "https://control.customer.test", destination_kind="customer-managed"
        )
    policy = hosted.resolve_destination_policy(
        "https://control.customer.test",
        destination_kind="customer-managed",
        trusted_hosts=["https://control.customer.test"],
    )
    assert policy.kind == "customer-managed"
    assert policy.trusted is True
    with pytest.raises(hosted.HostedError, match="requires HTTPS"):
        hosted.resolve_destination_policy(
            "http://control.customer.test",
            destination_kind="customer-managed",
            trusted_hosts=["http://control.customer.test"],
        )


def test_byoc_phi_mode_can_upload_exact_approved_sanitized_bytes(tmp_path, monkeypatch):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="privacy-officer")
    expected = approved_archive_path(dest).read_bytes()
    captured: dict = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured["archive"] = kwargs["files"]["file"][1].read()
        captured["data"] = kwargs["data"]
        return httpx.Response(201, json={"ingest": {"workflow_id": "wf_1"}})

    monkeypatch.setattr(httpx, "post", post)
    result = hosted.push(
        dest,
        kind="recording",
        deployment_kind="byoc",
        phi_mode=True,
        destination_kind="customer-managed",
        trusted_hosts=["https://control.customer.test"],
        host="https://control.customer.test",
        token="token",
    )

    assert result["uploaded"] is True
    assert result["destination_kind"] == "customer-managed"
    assert captured["archive"] == expected
    envelope = json.loads(captured["data"]["sanitization_manifest"])
    assert envelope["artifact"]["sha256"] == hashlib.sha256(expected).hexdigest()


def test_upload_filename_and_workflow_name_do_not_leak_phi(tmp_path, monkeypatch):
    source = _recording(tmp_path)
    dest = tmp_path / "Jane Doe sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="privacy-officer")
    captured: dict = {}

    def post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(201, json={"ingest": {"workflow_id": "wf_1"}})

    monkeypatch.setattr(httpx, "post", post)
    hosted.push(dest, name="Jane Doe intake", host=hosted.DEFAULT_HOST, token="token")
    filename = captured["files"]["file"][0]
    assert "Jane Doe" not in filename
    assert "Jane Doe" not in captured["data"]["name"]
    assert captured["data"]["name"] == "[PERSON] intake"


def test_raw_push_creates_derivative_and_pauses_for_review(tmp_path, monkeypatch):
    source = _recording(tmp_path)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))

    result = hosted.push(
        source,
        kind="recording",
        deployment_kind="regulated",
        destination_kind="customer-managed",
        trusted_hosts=["https://control.customer.test"],
        host="https://control.customer.test",
        token="token",
    )

    assert result["pending_review"] is True
    assert Path(result["sanitized_path"], MANIFEST_NAME).is_file()
    assert source.name not in Path(result["sanitized_path"]).name
    again = hosted.push(
        source,
        kind="recording",
        deployment_kind="regulated",
        destination_kind="customer-managed",
        trusted_hosts=["https://control.customer.test"],
        host="https://control.customer.test",
        token="token",
    )
    assert again["sanitized_path"] == result["sanitized_path"]


def test_changed_bundle_is_not_uploaded_as_executable(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text('{"patient":"Jane Doe"}')
    dest = tmp_path / "sanitized"
    sanitize_artifact(bundle, dest, kind="bundle")
    approve_derivative(dest, source=bundle, reviewer="alice")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))

    with pytest.raises(hosted.HostedError, match="runtime-validation attestation"):
        hosted.push(dest, kind="bundle", host=hosted.DEFAULT_HOST, token="token")


def test_push_kind_must_match_reviewed_manifest(tmp_path, monkeypatch):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="alice")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))
    with pytest.raises(hosted.HostedError, match="does not match"):
        hosted.push(dest, kind="bundle", host=hosted.DEFAULT_HOST, token="token")


def test_schema_files_are_valid_json():
    root = Path(__file__).resolve().parents[1] / "schemas"
    for name in (
        "sanitized-artifact-manifest-v1.json",
        "sanitized-artifact-approval-v1.json",
        "sanitization-ingest-v1.json",
        "runtime-validation-attestation-v1.json",
    ):
        schema = json.loads((root / name).read_text())
        assert schema["$schema"].endswith("2020-12/schema")


def test_emitted_manifest_approval_and_ingest_envelope_match_published_schemas(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1] / "schemas"
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    manifest = sanitize_artifact(source, dest, kind="recording")
    approval = approve_derivative(dest, source=source, reviewer="alice")
    envelope = build_ingest_manifest(dest)
    instances = {
        "sanitized-artifact-manifest-v1.json": manifest,
        "sanitized-artifact-approval-v1.json": approval,
        "sanitization-ingest-v1.json": envelope,
    }
    for name, instance in instances.items():
        jsonschema.Draft202012Validator(json.loads((root / name).read_text())).validate(
            instance
        )
