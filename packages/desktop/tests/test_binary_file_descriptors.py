"""Desktop follows the same descriptor contract as independently released Flow."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from openadapt_desktop.private_fs import open_binary

CONTRACT = Path(__file__).resolve().parents[2] / "flow" / "tests" / "_binary_descriptor_contract.py"
_spec = importlib.util.spec_from_file_location("desktop_byte_contract", CONTRACT)
assert _spec is not None and _spec.loader is not None
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
crt_files = _contract.crt_files


def _payloads() -> list[bytes]:
    random_bytes = random.Random(2303)
    return [b"", bytes(range(256)), b"\r\n\x1a\n"] + [
        random_bytes.randbytes(size) for size in (1, 17, 256, 2048)
    ]


@pytest.mark.parametrize("payload", _payloads(), ids=range(7))
def test_descriptor_model_preserves_arbitrary_bytes(tmp_path, crt_files, payload):
    _contract.assert_descriptor_roundtrip(tmp_path, open_binary, payload)


def test_binary_open_preserves_directory_anchor_and_private_mode(tmp_path, crt_files):
    _contract.assert_directory_anchor_and_private_mode(tmp_path, open_binary)


def test_binary_open_refuses_an_explicit_text_mode(tmp_path, monkeypatch):
    _contract.assert_explicit_text_mode_refused(tmp_path, monkeypatch, open_binary)


def test_raw_descriptor_entry_points_cannot_fall_back_to_text_open():
    source = Path(__file__).resolve().parents[1] / "openadapt_desktop"
    covered = _contract.raw_descriptor_owners(source)
    assert "hosted_runner.py" in covered
    assert "authoring_runner.py" in covered
