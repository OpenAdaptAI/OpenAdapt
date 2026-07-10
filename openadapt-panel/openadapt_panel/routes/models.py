"""Models route — browse trained checkpoints on the filesystem.

Read-only; imports no sibling. Looks under settings.model_cache_dir (or a
supplied path) for checkpoint files/dirs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

router = APIRouter(tags=["models"])

_CHECKPOINT_SUFFIXES = {".pt", ".safetensors", ".bin", ".ckpt"}


def _default_model_root() -> str:
    try:
        from openadapt.config import settings

        return str(settings.model_cache_dir)
    except Exception:
        return "training_output"


@router.get("/models")
def list_models(path: str | None = None) -> dict:
    root = Path(path or _default_model_root())
    models: list[dict] = []
    if root.exists():
        for entry in sorted(root.iterdir()):
            is_ckpt_dir = entry.is_dir() and (entry / "config.json").exists()
            is_ckpt_file = entry.is_file() and entry.suffix in _CHECKPOINT_SUFFIXES
            if not (is_ckpt_dir or is_ckpt_file):
                continue
            stat = entry.stat()
            models.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "kind": "dir" if entry.is_dir() else "file",
                    "size_bytes": stat.st_size if entry.is_file() else None,
                    "modified": stat.st_mtime,
                }
            )
    return {"root": str(root), "models": models}
