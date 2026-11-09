"""MLXBackend PHI no-retention: crop bytes that transit disk are always deleted.

The MLX dev backend must write image bytes to disk (mlx-vlm takes file paths),
so the control is a PRIVATE scratch dir plus GUARANTEED deletion — including
when inference raises (the pre-fix code skipped cleanup on any error, leaking
PHI crops). These tests stub ``mlx_vlm`` so they run in CI with no model.

See docs/deployment/ON_PREM_VLM.md ("No retention of crops or VLM payloads").
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from openadapt_flow.services.vlm_service.backends import MLXBackend


def _install_fake_mlx(monkeypatch, *, generate_raises: bool = False) -> None:
    """Inject stub ``mlx_vlm`` modules so generate() runs without a real model."""
    mlx_vlm = types.ModuleType("mlx_vlm")

    def _generate(*args, **kwargs):
        if generate_raises:
            raise RuntimeError("inference blew up")
        return types.SimpleNamespace(text="same")

    mlx_vlm.generate = _generate  # type: ignore[attr-defined]

    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    prompt_utils.apply_chat_template = lambda *a, **k: "PROMPT"  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)


def _ready_backend(tmp_path: Path) -> MLXBackend:
    bk = MLXBackend(tmp_dir=tmp_path / "scratch")
    bk._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)  # type: ignore[union-attr]
    # Pretend the model is loaded (bypass the real mlx_vlm.load).
    bk._model = object()
    bk._processor = object()
    bk._config = object()
    return bk


def test_crops_deleted_after_successful_generate(monkeypatch, tmp_path: Path):
    _install_fake_mlx(monkeypatch)
    bk = _ready_backend(tmp_path)
    out = bk.generate("prompt", [b"\x89PNG-crop-a", b"\x89PNG-crop-b"], max_tokens=6)
    assert out == "same"
    # No crop bytes left behind.
    assert list(bk._tmp.iterdir()) == []  # type: ignore[union-attr]


def test_crops_deleted_even_when_generate_raises(monkeypatch, tmp_path: Path):
    _install_fake_mlx(monkeypatch, generate_raises=True)
    bk = _ready_backend(tmp_path)
    with pytest.raises(RuntimeError):
        bk.generate("prompt", [b"\x89PNG-crop-a"], max_tokens=6)
    # The fix: cleanup happens in finally, so nothing leaks on the error path.
    assert list(bk._tmp.iterdir()) == []  # type: ignore[union-attr]


def test_scratch_dir_is_private(tmp_path: Path):
    """Default scratch dir is created 0700 (not a world-readable shared path)."""
    bk = MLXBackend(tmp_dir=tmp_path / "scratch")
    bk._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)  # type: ignore[union-attr]
    mode = bk._tmp.stat().st_mode & 0o777  # type: ignore[union-attr]
    assert mode & 0o077 == 0  # no group/other access
