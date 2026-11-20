"""Raw evidence files preserve every byte through all descriptor-opening paths."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import os
from pathlib import Path
from typing import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from openadapt_flow.approval_mode import item_approval
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.eligibility import artifact
from openadapt_flow.private_file import open_binary

PACKAGES = Path(__file__).resolve().parents[2]
FLOW_SOURCE = PACKAGES / "flow" / "openadapt_flow"
DESKTOP_SOURCE = PACKAGES / "desktop" / "openadapt_desktop"
_spec = importlib.util.spec_from_file_location(
    "binary_descriptor_desktop_private_fs", DESKTOP_SOURCE / "private_fs.py"
)
assert _spec is not None and _spec.loader is not None
_desktop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_desktop)
OPENERS = [open_binary, _desktop.open_binary]


@pytest.fixture
def crt_files(monkeypatch: pytest.MonkeyPatch):
    """Expose CRT text translation on POSIX; use the real CRT on Windows.

    A text writer can appear to round-trip when a text reader undoes CRLF
    expansion. Tests therefore also read the physical file through read_bytes.
    """
    if os.name == "nt":
        yield
        return
    binary = 1 << 29
    original_open, original_read = os.open, os.read
    original_write, original_close = os.write, os.close
    descriptors: dict[int, bool] = {}
    eof: set[int] = set()

    def crt_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags & ~binary, *args, **kwargs)
        descriptors[descriptor] = bool(flags & binary)
        return descriptor

    def crt_write(descriptor, data):
        if descriptors.get(descriptor, True):
            return original_write(descriptor, data)
        raw = bytes(data)
        original_write(descriptor, raw.replace(b"\n", b"\r\n"))
        return len(raw)

    def crt_read(descriptor, count):
        if descriptor in eof:
            return b""
        raw = original_read(descriptor, count)
        if descriptors.get(descriptor, True):
            return raw
        if b"\x1a" in raw:
            raw = raw.split(b"\x1a", 1)[0]
            eof.add(descriptor)
        return raw.replace(b"\r\n", b"\n")

    def crt_close(descriptor):
        descriptors.pop(descriptor, None)
        eof.discard(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
    monkeypatch.setattr(os, "open", crt_open)
    monkeypatch.setattr(os, "read", crt_read)
    monkeypatch.setattr(os, "write", crt_write)
    monkeypatch.setattr(os, "close", crt_close)
    yield


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = os.write(descriptor, view)
        assert count > 0
        view = view[count:]


def _read_all(descriptor: int) -> bytes:
    chunks = []
    while chunk := os.read(descriptor, 17):
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("opener", OPENERS, ids=["flow", "desktop"])
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(max_size=2048))
def test_descriptor_model_preserves_arbitrary_bytes(
    tmp_path: Path, crt_files, opener: Callable, payload: bytes
) -> None:
    path = tmp_path / "arbitrary.bin"
    path.unlink(missing_ok=True)
    descriptor = opener(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
    finally:
        os.close(descriptor)
    assert path.read_bytes() == payload
    descriptor = opener(path, os.O_RDONLY)
    try:
        assert _read_all(descriptor) == payload
    finally:
        os.close(descriptor)
    descriptor = opener(path, os.O_WRONLY | os.O_APPEND)
    try:
        _write_all(descriptor, b"\r\n\x1a\n")
    finally:
        os.close(descriptor)
    assert path.read_bytes() == payload + b"\r\n\x1a\n"
    with pytest.raises(FileExistsError):
        opener(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


@pytest.mark.parametrize(
    "writer", [item_approval._write_exclusive, artifact._write_exclusive]
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(max_size=2048))
def test_exclusive_evidence_writers_share_exact_bytes(
    tmp_path: Path, crt_files, writer: Callable, payload: bytes
) -> None:
    path = tmp_path / "evidence.bin"
    path.unlink(missing_ok=True)
    writer(path, payload)
    assert path.read_bytes() == payload
    assert item_approval._read_regular(path) == payload
    with pytest.raises(FileExistsError):
        writer(path, b"replacement")
    assert path.read_bytes() == payload


def test_approval_frame_remains_decodable_and_hash_bound(
    tmp_path: Path, crt_files, approver
):
    image = Image.new("RGB", (19, 13), (20, 70, 160))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    payload = encoded.getvalue()
    store = item_approval.ItemApprovalStore(tmp_path / "run")
    request = store.open_request(
        run_id="binary-test",
        workflow_name="synthetic",
        step_id="save",
        step_index=1,
        step_intent="Save",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=payload,
        form_state=("dom", "sha256:" + "1" * 64),
    )
    physical = (store.run_dir / request.screenshot).read_bytes()
    assert hashlib.sha256(physical).hexdigest() == request.screenshot_sha256
    assert store.read_frame(request) == payload
    with Image.open(io.BytesIO(physical)) as frame:
        frame.verify()
    (store.run_dir / request.screenshot).write_bytes(payload + b"\n")
    with pytest.raises(item_approval.ItemApprovalRefused) as refused:
        store.read_frame(request)
    assert refused.value.code == "frame_digest_mismatch"


@pytest.mark.parametrize("opener", OPENERS, ids=["flow", "desktop"])
def test_binary_open_preserves_directory_anchor_and_private_mode(
    tmp_path: Path, crt_files, opener: Callable
) -> None:
    if os.open not in os.supports_dir_fd and os.name == "nt":
        pytest.skip("this platform has no descriptor-relative file open")
    directory = opener(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = opener(
            "anchored.bin",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            _write_all(descriptor, bytes(range(256)))
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    path = tmp_path / "anchored.bin"
    assert path.read_bytes() == bytes(range(256))
    assert path.stat().st_mode & 0o777 == 0o600


def test_raw_descriptor_entry_points_cannot_fall_back_to_text_open():
    """Enforce the byte model across every caller, including future siblings."""
    covered = []
    for root in (FLOW_SOURCE, DESKTOP_SOURCE):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ]
            if not any(node.func.attr in {"read", "write"} for node in calls):
                continue
            assert not any(node.func.attr == "open" for node in calls), str(path)
            covered.append(path.relative_to(PACKAGES).as_posix())
    assert "flow/openadapt_flow/approval_mode/item_approval.py" in covered
    assert "desktop/openadapt_desktop/hosted_runner.py" in covered


@pytest.mark.parametrize("opener", OPENERS, ids=["flow", "desktop"])
def test_binary_open_refuses_an_explicit_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: Callable
) -> None:
    text_flag = getattr(os, "O_TEXT", 1 << 28)
    monkeypatch.setattr(os, "O_TEXT", text_flag, raising=False)
    path = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="text mode"):
        opener(path, os.O_WRONLY | os.O_CREAT | text_flag, 0o600)
    assert not path.exists()
