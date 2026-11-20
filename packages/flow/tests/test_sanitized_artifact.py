from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Optional

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
    _bundle_string_is_structural,
    _review_content_length,
    _valid_review_host,
    add_manual_image_redaction,
    add_manual_text_redaction,
    approve_derivative,
    approved_archive_path,
    build_ingest_manifest,
    load_and_verify_derivative,
    load_valid_approval,
    materialize_approved_derivative,
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


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("qualification", "cases", 0, "evidence", 0, "sha256"), "a" * 64),
        (("steps", 0, "id"), "step_000"),
        (("qualification", "identity_policies", "step_000", "step_id"), "step_000"),
        (("qualification", "project_id"), "f6d239d0-0b5e-440f-b794-bbe69cc01115"),
        (
            ("qualification", "environment", "environment_observer_id"),
            "backend:PlaywrightBackend",
        ),
        (("steps", 0, "anchor", "landmarks", 0, "relation"), "left_of"),
    ],
)
def test_bundle_machine_contract_strings_are_not_sent_to_phi_ner(path, value):
    assert _bundle_string_is_structural(path, value)


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
        self.value = ""

    @property
    def viewport(self):
        return (8, 8)

    def screenshot(self):
        return _png()

    def type_text(self, value):
        self.actions.append(("type", value))
        self.value += value

    def focused_text_value(self):
        return self.value

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
    source_digest = Workflow.load(bundle).manifest.content_digest

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
    assert (
        Workflow.load(destination).manifest.provenance.sanitized_from_digest
        == source_digest
    )
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
        # The upload file is a private verified snapshot, not this mutable path.
        approved_archive_path(dest).write_bytes(b"unapproved replacement")
        captured["url"] = url
        captured["archive"] = kwargs["files"]["file"][1].read()
        captured["data"] = kwargs["data"]
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


def test_push_refuses_archive_replaced_after_approval_before_snapshot(
    tmp_path, monkeypatch
):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="privacy-officer")
    real_snapshot = hosted._verified_archive_snapshot

    def replace_then_snapshot(archive_path, *, expected_sha256):
        archive_path.write_bytes(b"Jane Doe unapproved replacement")
        return real_snapshot(archive_path, expected_sha256=expected_sha256)

    monkeypatch.setattr(hosted, "_verified_archive_snapshot", replace_then_snapshot)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("changed bytes must not egress"),
    )

    with pytest.raises(hosted.HostedError, match="changed before upload"):
        hosted.push(dest, host=hosted.DEFAULT_HOST, token="token")


def test_upload_filename_and_workflow_name_do_not_leak_phi(tmp_path, monkeypatch):
    source = _recording(tmp_path)
    dest = tmp_path / "Jane Doe sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="privacy-officer")
    captured: dict = {}

    def post(url, **kwargs):
        captured.update(kwargs)
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
    assert result["review_action"] == "review_sanitized"
    assert result["original_path"] == str(source)
    assert "review_command" not in result
    assert re.fullmatch(r"[a-f0-9]{64}", result["review_id"])
    assert result["review_id"] != result["local_binding"]["derivative_tree_sha256"]
    assert result["local_binding"] == {
        "source_tree_sha256": result["local_binding"]["source_tree_sha256"],
        "derivative_tree_sha256": result["local_binding"]["derivative_tree_sha256"],
        "approved_archive_sha256": None,
        "sanitization_policy": "outbound-phi-v1",
    }
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
    assert again["review_id"] == result["review_id"]


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
        "runtime-validation-attestation-v2.json",
        "runtime-validation-attestation-v3.json",
        "push-result-v1.json",
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


def _approved_recording(tmp_path: Path) -> tuple[Path, dict, Path]:
    """Approve a recording derivative and return it with its archive."""
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approval = approve_derivative(dest, source=source, reviewer="alice")
    return dest, approval, approved_archive_path(dest)


def test_unzipping_an_approved_archive_alone_yields_an_unapproved_derivative(tmp_path):
    """The archive cannot carry the approval computed over its own bytes."""
    dest, _, archive = _approved_recording(tmp_path)
    with zipfile.ZipFile(archive) as zf:
        assert APPROVAL_NAME not in zf.namelist()

    elsewhere = tmp_path / "elsewhere" / "recording"
    elsewhere.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(elsewhere)
    (elsewhere.parent / "recording.approved.zip").write_bytes(archive.read_bytes())

    assert load_and_verify_derivative(elsewhere)["kind"] == "recording"
    with pytest.raises(SanitizationError, match="not been approved"):
        load_valid_approval(elsewhere)


def test_materialize_approved_restores_the_unchanged_approval_gate(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    out = tmp_path / "elsewhere" / "recording"

    materialized = materialize_approved_derivative(
        archive,
        approval=record,
        destination=out,
        expected_archive_sha256=approval["approved_derivative_sha256"],
    )

    assert materialized == approval
    assert load_valid_approval(out) == approval
    assert (out / APPROVAL_NAME).is_file()
    assert approved_archive_path(out).read_bytes() == archive.read_bytes()
    assert {
        str(path.relative_to(out))
        for path in out.rglob("*")
        if path.is_file() and path.name != APPROVAL_NAME
    } == {
        MANIFEST_NAME,
        "meta.json",
        "events.jsonl",
        str(Path("frames") / "before.png"),
    }


def test_materialize_approved_accepts_the_persisted_ingest_envelope(tmp_path):
    """A cloud deployment persists the envelope, not the approval file."""
    dest, approval, archive = _approved_recording(tmp_path)
    envelope = build_ingest_manifest(dest)
    record = tmp_path / "envelope.json"
    record.write_text(json.dumps(envelope), encoding="utf-8")
    out = tmp_path / "elsewhere" / "recording"

    materialized = materialize_approved_derivative(
        archive, approval=record, destination=out
    )

    assert materialized["reviewer"] == approval["reviewer"] == "alice"
    assert materialized["approved_at"] == approval["approved_at"]
    assert materialized["automatic"] is False
    assert (
        materialized["approved_derivative_sha256"]
        == approval["approved_derivative_sha256"]
    )
    assert load_valid_approval(out)["reviewer"] == "alice"


def test_materialized_recording_gives_compile_its_source_provenance(tmp_path):
    """The runner built a bundle with null provenance from a bare archive."""
    source = tmp_path / "recording"
    frames = source / "frames"
    frames.mkdir(parents=True)
    before = Image.new("RGB", (320, 200), "white")
    ImageDraw.Draw(before).rectangle((90, 70, 230, 130), outline="black", width=4)
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
                "id": "materialized-recording",
                "created_at": "2026-07-15T00:00:00+00:00",
                "viewport": [320, 200],
                "app_url": "http://example.test/",
                "params": {},
            }
        )
    )
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approval = approve_derivative(dest, source=source, reviewer="alice")

    bare = tmp_path / "bare" / "recording"
    bare.mkdir(parents=True)
    with zipfile.ZipFile(approved_archive_path(dest)) as zf:
        zf.extractall(bare)
    bare_workflow = compile_recording(bare, tmp_path / "bare-bundle", name="bare")
    assert bare_workflow.manifest is not None
    assert bare_workflow.manifest.provenance.source_recording_sha256 is None

    out = tmp_path / "elsewhere" / "recording"
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(build_ingest_manifest(dest)), encoding="utf-8")
    materialize_approved_derivative(
        approved_archive_path(dest), approval=record, destination=out
    )
    workflow = compile_recording(out, tmp_path / "bundle", name="materialized")

    assert workflow.manifest is not None
    assert (
        workflow.manifest.provenance.source_recording_sha256
        == approval["approved_derivative_sha256"]
    )


def test_materialize_approved_refuses_a_tampered_archive(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    tampered = tmp_path / "tampered.approved.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            payload = src.read(info.filename)
            if info.filename == "events.jsonl":
                payload = b'{"text":"substituted"}\n'
            dst.writestr(info, payload)

    with pytest.raises(SanitizationError):
        materialize_approved_derivative(
            tampered, approval=record, destination=tmp_path / "out"
        )
    assert not (tmp_path / "out" / APPROVAL_NAME).exists()


def test_materialize_approved_refuses_an_unexpected_archive_digest(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")

    with pytest.raises(SanitizationError, match="expected sha256"):
        materialize_approved_derivative(
            archive,
            approval=record,
            destination=tmp_path / "out",
            expected_archive_sha256="0" * 64,
        )
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_materialize_approved_refuses_an_approval_for_another_artifact(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    other = dict(approval)
    other["approved_derivative_sha256"] = "f" * 64
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(other), encoding="utf-8")

    with pytest.raises(SanitizationError, match="archive"):
        materialize_approved_derivative(
            archive, approval=record, destination=tmp_path / "out"
        )


def test_materialize_approved_refuses_a_traversing_archive_member(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    hostile = tmp_path / "hostile.approved.zip"
    with zipfile.ZipFile(hostile, "w") as zf:
        zf.writestr("../escaped.json", "{}")

    with pytest.raises(SanitizationError, match="unsafe path"):
        materialize_approved_derivative(
            hostile, approval=record, destination=tmp_path / "out"
        )
    assert not (tmp_path / "escaped.json").exists()


def test_materialize_approved_refuses_an_archive_carrying_its_own_approval(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    hostile = tmp_path / "hostile.approved.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(hostile, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(APPROVAL_NAME, json.dumps(approval))

    with pytest.raises(SanitizationError, match="own approval"):
        materialize_approved_derivative(
            hostile, approval=record, destination=tmp_path / "out"
        )


def test_materialize_approved_refuses_a_non_empty_destination(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "prior.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SanitizationError, match="not empty"):
        materialize_approved_derivative(archive, approval=record, destination=out)
    assert (out / "prior.json").read_text() == "{}"


def test_materialize_approved_refuses_a_policy_envelope_without_its_signature(tmp_path):
    dest, approval, archive = _approved_recording(tmp_path)
    envelope = build_ingest_manifest(dest)
    envelope["approval"]["method"] = "policy"
    record = tmp_path / "envelope.json"
    record.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(SanitizationError, match="policy"):
        materialize_approved_derivative(
            archive, approval=record, destination=tmp_path / "out"
        )


def test_materialize_approved_verifies_a_policy_signature_when_the_key_is_present(
    tmp_path, monkeypatch
):
    source = _recording(tmp_path)
    dest = tmp_path / "sanitized"
    sanitize_artifact(source, dest, kind="recording")
    approve_derivative(dest, source=source, reviewer="policy:x", automatic=True)
    key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("OPENADAPT_SANITIZATION_POLICY_KEY_ID", "policy-key-1")
    monkeypatch.setenv("OPENADAPT_SANITIZATION_POLICY_KEY", key)
    envelope = build_ingest_manifest(dest)
    record = tmp_path / "envelope.json"
    record.write_text(json.dumps(envelope), encoding="utf-8")

    materialized = materialize_approved_derivative(
        approved_archive_path(dest), approval=record, destination=tmp_path / "good"
    )
    assert materialized["automatic"] is True

    envelope["approval"]["policy_signature"] = "a" * 64
    record.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SanitizationError, match="policy approval signature"):
        materialize_approved_derivative(
            approved_archive_path(dest), approval=record, destination=tmp_path / "bad"
        )


def test_materialize_approved_cli_round_trips(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    dest, approval, archive = _approved_recording(tmp_path)
    record = tmp_path / "approval.json"
    record.write_text(json.dumps(approval), encoding="utf-8")
    out = tmp_path / "elsewhere" / "recording"

    code = main(
        [
            "materialize-approved",
            "--archive",
            str(archive),
            "--approval",
            str(record),
            "--out",
            str(out),
            "--expect-archive-sha256",
            approval["approved_derivative_sha256"],
        ]
    )

    assert code == 0
    assert "alice" in capsys.readouterr().out
    assert load_valid_approval(out)["reviewer"] == "alice"


# -- wrap programs: declared generated files -----------------------------------


class PresidioLikeScrubber:
    """The false positives the real NER scrub produced on a wrap program.

    Seen on 2026-11-14 with ``en_core_web_sm``: version tokens, step ids,
    hex digests, operator handles, dotted names and loopback addresses were
    all rewritten. Real PHI is replaced too. Placeholders are left alone, so a
    second pass is stable.
    """

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        text = text.replace("Jane Doe", "<PERSON>").replace("MRN-123", "<MRN>")
        text = text.replace("DOM/UIA/AX", "<ORGANIZATION>")
        text = re.sub(r"\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b", "<IP_ADDRESS>", text)
        text = re.sub(r"\bv[0-9]+\b", "<US_DRIVER_LICENSE>", text)
        text = re.sub(r"\bstep_[0-9]+\b", "<LOCATION>", text)
        text = re.sub(r"\b[0-9a-f]{16,}\b", "<PERSON>", text)
        text = re.sub(r"\b[a-z]+:[a-z][a-z-]*\b", "<PERSON>", text)
        text = re.sub(r"\b[a-z]+\.[a-z]+\b", "<URL>", text)
        return text

    def scrub_image(self, image, fill_color=None):
        return image


# What ``openadapt wrap publish`` stages for a person: the keyed HMAC of the
# name (openadapt_flow.jit.principal_ids).
_REVIEWER = "hmac:" + "1f" * 32


def _wrap_files(
    bundle: Path,
    *,
    keys: Path,
    title: str = "Notes",
    literal: str = "demo",
    reviewer: str = _REVIEWER,
    admitter: str = "ci:rehearsal",
    probe: Optional[str] = None,
    extra_admission: Optional[dict] = None,
) -> None:
    """Write the generated files ``openadapt wrap publish`` stages."""
    from openadapt_flow.compiler.compile import IDCROP_REASON_STRUCTURED
    from openadapt_flow.execute.keys import (
        fingerprint_of,
        load_or_create_private_key,
        sign_bytes,
    )
    from openadapt_flow.identity_signals import signal_hash_key
    from openadapt_flow.ir import IdentityTemplate
    from openadapt_flow.jit import lifecycle
    from openadapt_flow.verify_run import new_record_probe
    from openadapt_flow.wrap import publish

    template = bundle / "templates" / "step_002.png"
    template.parent.mkdir(parents=True)
    template.write_bytes(_png())
    identity = IdentityTemplate(
        salt="646cd87caf2a01a84eda161c4aa63a40",
        punctuation_policy="strict-lexeme-punctuation-v2",
        structured="cde5be965c7069e69e1bd3306c35f998ad39af9df99c6fbdd7e14bf51c012fff",
        signal_hashes={
            signal_hash_key("structured", "exact"): "a" * 64,
            signal_hash_key(
                "captured_context",
                "normalized",
                ("unicode_nfkc", "casefold", "collapse_whitespace"),
            ): "b" * 64,
        },
    )
    workflow = Workflow(
        name="save-note",
        recording_id="c770c8bdbb6c4864a5e29eec19f4bafd",
        viewport=(8, 8),
        steps=[
            Step(
                id="step_000",
                intent="type the note",
                action=ActionKind.TYPE,
                text="{note}",
            ),
            Step(
                id="step_002",
                intent="click Save",
                action=ActionKind.CLICK,
                identifier_crop_missing_reason=IDCROP_REASON_STRUCTURED,
                anchor=Anchor(
                    template="templates/step_002.png",
                    region=(0, 0, 8, 8),
                    click_point=(4, 4),
                    identity_template=identity,
                ),
            ),
        ],
    )
    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow), encoding="utf-8")
    sealed = Workflow.load(bundle).manifest
    assert sealed is not None
    digest = sealed.content_digest

    private = load_or_create_private_key(keys)
    body = {
        "schema": lifecycle.ADMISSION_SCHEMA,
        "admission_mode": "admitter_key",
        "state": "ADMITTED",
        "workflow_id": "wf_d6611004c69b4376aea0636278e48e74",
        "digest": digest,
        "task": {
            "key": "skill-48a0f397cb2a242d-f3a612c83abd",  # gitleaks:allow (synthetic task key)
            "task_id": "skill-48a0f397cb2a242d",
            "params": {"note": "string"},
        },
        "params": ["note"],
        "policy": "clinical-write",
        "admitted_by_principal_id": admitter,
        "admitted_at": "2026-11-14T07:57:00Z",
        "reviewed_by_principal_id": None,
        "reviewed_at": None,
        "reviewed_steps": None,
        "canary": {
            "principal_id": admitter,
            "ran_at": "2026-11-14T07:57:00Z",
            "target_origin": "https://127-0-0-1.nip.io:8443",
            "outcome": "VERIFIED",
            "model_calls": 0,
            "environment": "https://127-0-0-1.nip.io:8443",
        },
        "record_check_digest": "e" * 64,
        "readback": [
            {
                "step_id": "step_002",
                "ok": True,
                "effects": [
                    {
                        "kind": "record_written",
                        "tier": "INDEPENDENT_SYSTEM",
                        "evidence": "independent_readback",
                    }
                ],
            }
        ],
        "recorded_by": "agent:generic",
        "agent_principal_id": "agent:scripted-rehearsal",
        "dialect": "generic",
        "model_key": {
            "provider": "anthropic",
            "held_by": "customer",
            "source": "env:TEST_ONLY",
            "fingerprint": "sha256:fa77d7bdb2ae64fb",
        },
        "signing_key": fingerprint_of(private.public_key()),
        **(extra_admission or {}),
    }
    admission = {**body, "signature": sign_bytes(private, lifecycle._canonical(body))}
    (bundle / lifecycle.ADMISSION_FILE).write_text(
        json.dumps(admission, indent=2), encoding="utf-8"
    )
    (bundle / lifecycle.DRAFT_MARKER).write_text(
        json.dumps(
            {"note": "compiled from a computer-use agent run", "state": "DRAFT"}
        ),
        encoding="utf-8",
    )
    section = {
        "id": "fields-step_000",
        "title": title,
        "title_source": "ui",
        "kind": "fields",
        "steps": ["step_000"],
        "fields": ["note"],
        "params": ["note"],
    }
    (bundle / "sections.json").write_text(
        json.dumps(
            {
                "schema": "openadapt.flow.sections/v1",
                "note": "Display metadata: named groups of steps for program maps.",
                "workflow_contract_sha256": "c" * 64,
                "sections": [section],
                "compiled": [section],
                "edits": [],
                "context": {
                    "step_002": {"container": title, "kind": "legend", "header": False}
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (bundle / "templates" / "layout_fingerprint.json").write_text(
        json.dumps(
            {
                "schema": "openadapt.layout-fingerprints/v1",
                "scale_landmarks": {
                    "salt": "646cd87caf2a01a84eda161c4aa63a40",
                    "steps": {"step_002": [["35009404e49e6eb9", 52.0, 122.0, 40, 24]]},
                },
                "steps": {
                    "step_002": {
                        "char_width": 8.4,
                        "landmarks": {},
                        "lines": 7,
                        "profile_sha256": None,
                        "text_height": 19,
                        "viewport": [1280, 800],
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    match = {"list": {"literal": literal}, "note": {"param": "note"}}
    import yaml

    checks = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": "http://127.0.0.1:8917",
            "records_path": "/api/notes",
            "records_key": "records",
            "records_complete": True,
        },
        "effects": [
            {
                "kind": "record_written",
                "match": match,
                "composite_key": match,
                "count_new_only": True,
                "probe": probe
                if probe is not None
                else new_record_probe(match, reviewer),
            }
        ],
    }
    readback = bundle / publish.READBACK_PATH
    readback.parent.mkdir(parents=True)
    readback.write_text(
        f"# Confirmed by {reviewer} at 2026-11-14T07:56:48Z from verify.proposed.yaml.\n"
        + yaml.safe_dump(checks, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (bundle / publish.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": publish.PUBLISH_SCHEMA,
                "parameters": ["note"],
                "input_schema": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
                "workflow_digest": digest,
                "record_check_sha256": "e" * 64,
                "saves": 1,
                "readback": {
                    "path": publish.READBACK_PATH,
                    "sha256": hashlib.sha256(readback.read_bytes()).hexdigest(),
                },
                "run_authorization": {
                    "schema_version": "openadapt.admitted-program-run-shape/v1",
                    "admission_sha256": "f" * 64,
                    "admitted_policy_name": "clinical-write",
                    "bundle_content_digest": digest,
                    "execution_profile": "standard",
                    "minimum_effect_tier": 3,
                    "qualified_effect_requirements": [],
                    "required_identity_step_ids": ["step_000", "step_002"],
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }


def _admission_verifies(bundle: Path, keys: Path) -> bool:
    from openadapt_flow.execute.keys import load_or_create_private_key, verify_signature
    from openadapt_flow.jit import lifecycle

    admission = json.loads((bundle / lifecycle.ADMISSION_FILE).read_text())
    body = {k: v for k, v in admission.items() if k != "signature"}
    public = load_or_create_private_key(keys).public_key()
    return verify_signature(
        public, lifecycle._canonical(body), admission["signature"]
    ) and body["digest"] == lifecycle._digest(Workflow.load(bundle))


def test_wrap_program_structure_survives_scrubbing_byte_for_byte(tmp_path):
    """Schema ids, step ids, digests, signatures and handles are not PHI."""
    from openadapt_flow.wrap import publish

    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    _wrap_files(bundle, keys=keys)
    source = _tree(bundle)

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "preserved"
    assert _tree(destination) == source
    # The signed admission and the bundle's own integrity manifest still verify.
    assert _admission_verifies(destination, keys)
    Workflow.load(destination)
    published = json.loads((destination / publish.MANIFEST_NAME).read_text())
    readback = (destination / publish.READBACK_PATH).read_bytes()
    assert published["readback"]["sha256"] == hashlib.sha256(readback).hexdigest()
    assert (
        published["workflow_digest"]
        == Workflow.load(destination).manifest.content_digest
    )
    # The approval rescan reads the same declarations and agrees.
    approve_derivative(destination, source=bundle, reviewer="privacy-reviewer")


def test_wrap_program_content_fields_are_still_scrubbed(tmp_path):
    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    _wrap_files(bundle, keys=keys, title="Jane Doe", literal="MRN-123")

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "not-preserved"
    for rel, data in _tree(destination).items():
        if rel.endswith((".json", ".yaml", ".py")):
            assert b"Jane Doe" not in data, rel
            assert b"MRN-123" not in data, rel
    sections = json.loads((destination / "sections.json").read_text())
    assert sections["sections"][0]["title"] == "<PERSON>"
    assert sections["context"]["step_002"]["container"] == "<PERSON>"
    # Structure around the scrubbed content is untouched.
    assert sections["schema"] == "openadapt.flow.sections/v1"
    assert sections["sections"][0]["steps"] == ["step_000"]
    import yaml

    checks = yaml.safe_load((destination / "readback" / "verify.yaml").read_text())
    effect = checks["effects"][0]
    assert effect["match"]["list"] == {"literal": "<MRN>"}
    # The probe spelled out the literal, so it is no longer the rendering of a
    # scrub-stable match and is scrubbed with it.
    assert "MRN-123" not in effect["probe"]
    assert checks["schema_version"] == "openadapt.verify/v1"
    assert checks["verifier"]["base_url"] == "http://127.0.0.1:8917"
    approve_derivative(destination, source=bundle, reviewer="privacy-reviewer")


@pytest.mark.parametrize(
    "case",
    ["free_text_principal", "undeclared_field", "hand_written_probe", "free_comment"],
)
def test_free_text_in_a_wrap_structural_or_unknown_field_is_scrubbed(tmp_path, case):
    """A structural field keeps only machine-shaped values; unknown fields scrub."""
    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    options: dict = {}
    if case == "free_text_principal":
        options["admitter"] = "Jane Doe"
    elif case == "undeclared_field":
        options["extra_admission"] = {"operator_note": "called Jane Doe"}
    elif case == "hand_written_probe":
        options["probe"] = "new note for Jane Doe (confirmed by rehearsal.reviewer)"
    else:
        options["reviewer"] = "Jane Doe"
    _wrap_files(bundle, keys=keys, **options)

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "not-preserved"
    for rel, data in _tree(destination).items():
        if rel.endswith((".json", ".yaml", ".py")):
            assert b"Jane Doe" not in data, rel


@pytest.mark.parametrize(
    "value", ["jane.doe", "user:jane.doe", "4165550123", "MRN-0012345"]
)
def test_a_principal_that_is_not_a_machine_handle_never_leaves(tmp_path, value):
    """Names and record numbers pass a scrubber; the principal shape stops them."""
    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    _wrap_files(bundle, keys=keys, admitter=value, reviewer=value)

    destination = tmp_path / "sanitized"
    manifest = sanitize_artifact(bundle, destination, kind="bundle")

    assert manifest["execution_semantics"] == "not-preserved"
    for rel, data in _tree(destination).items():
        if rel.endswith((".json", ".yaml", ".py")):
            assert value.encode() not in data, rel
    from openadapt_flow.jit import lifecycle

    admission = json.loads((destination / lifecycle.ADMISSION_FILE).read_text())
    assert admission["admitted_by_principal_id"] == "<PRINCIPAL>"
    assert admission["canary"]["principal_id"] == "<PRINCIPAL>"


@pytest.mark.parametrize(
    "value",
    [
        "ci:rehearsal",
        "agent:scripted-rehearsal",
        "service:intake",
        "sha256:fa77d7bdb2ae64fb",
        "hmac:" + "ab" * 32,
    ],
)
def test_machine_handles_and_hmacs_stay_plaintext(tmp_path, value):
    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    _wrap_files(bundle, keys=keys, admitter=value, reviewer=value)

    manifest = sanitize_artifact(bundle, tmp_path / "sanitized", kind="bundle")

    assert manifest["execution_semantics"] == "preserved"
    assert _admission_verifies(tmp_path / "sanitized", keys)


def test_the_principal_shape_matches_the_publish_allowlist():
    from openadapt_flow.jit.principal_ids import DISCLOSABLE_PRINCIPAL
    from openadapt_flow.sanitized_artifact import _SHAPES

    for value in (
        "ci:rehearsal",
        "agent:x",
        "service:a.b-c_d",
        "ed25519:" + "0" * 64,
        "standard-change:" + "a" * 64,
        "self-repair:probe",
        "hmac:" + "f" * 64,
        "jane.doe",
        "user:jane.doe",
        "CI:Release",
        "4165550123",
        "MRN-0012345",
        "hmac:abc",
    ):
        assert bool(_SHAPES["principal"].fullmatch(value)) == bool(
            DISCLOSABLE_PRINCIPAL.fullmatch(value)
        ), value


def test_a_wrap_file_with_another_schema_falls_back_to_general_scrubbing(tmp_path):
    privacy.set_text_scrubber(PresidioLikeScrubber())
    bundle, keys = tmp_path / "bundle", tmp_path / "keys"
    _wrap_files(bundle, keys=keys)
    sections = json.loads((bundle / "sections.json").read_text())
    sections["schema"] = "openadapt.flow.sections/v9"
    (bundle / "sections.json").write_text(json.dumps(sections), encoding="utf-8")

    manifest = sanitize_artifact(bundle, tmp_path / "sanitized", kind="bundle")

    assert manifest["execution_semantics"] == "not-preserved"


def test_declared_wrap_file_names_and_schemas_match_their_owners():
    from openadapt_flow.jit import lifecycle
    from openadapt_flow.runtime.layout_drift import (
        FINGERPRINT_ASSET,
        FINGERPRINT_SCHEMA,
    )
    from openadapt_flow.sanitized_artifact import _declared_bundle_files
    from openadapt_flow.sections import ProgramSections
    from openadapt_flow.verify_run import VERIFY_CONFIG_SCHEMA
    from openadapt_flow.wrap import publish

    declared = _declared_bundle_files()
    expected = {
        lifecycle.ADMISSION_FILE: lifecycle.ADMISSION_SCHEMA,
        publish.MANIFEST_NAME: publish.PUBLISH_SCHEMA,
        publish.READBACK_PATH: VERIFY_CONFIG_SCHEMA,
        "sections.json": ProgramSections.model_fields["schema_"].default,
        FINGERPRINT_ASSET: FINGERPRINT_SCHEMA,
        lifecycle.DRAFT_MARKER: None,
    }
    assert set(declared) == set(expected)
    for rel, schema in expected.items():
        rule = declared[rel].schema
        assert (rule[1] if rule else None) == (
            frozenset({schema}) if schema else None
        ), rel
