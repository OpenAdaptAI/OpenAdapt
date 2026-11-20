"""Flow evidence files preserve every byte through descriptor-opening paths."""

from __future__ import annotations

import hashlib
import io
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
from tests._binary_descriptor_contract import (
    assert_descriptor_roundtrip,
    assert_directory_anchor_and_private_mode,
    assert_explicit_text_mode_refused,
    crt_files as crt_files,
    raw_descriptor_owners,
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(max_size=2048))
def test_descriptor_model_preserves_arbitrary_bytes(
    tmp_path: Path, crt_files, payload: bytes
) -> None:
    assert_descriptor_roundtrip(tmp_path, open_binary, payload)


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


def test_binary_open_preserves_directory_anchor_and_private_mode(
    tmp_path: Path, crt_files
) -> None:
    assert_directory_anchor_and_private_mode(tmp_path, open_binary)


def test_raw_descriptor_entry_points_cannot_fall_back_to_text_open():
    source = Path(__file__).resolve().parents[1] / "openadapt_flow"
    covered = raw_descriptor_owners(source)
    assert "approval_mode/item_approval.py" in covered
    assert "write_ledger.py" in covered


def test_binary_open_refuses_an_explicit_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_explicit_text_mode_refused(tmp_path, monkeypatch, open_binary)
