"""The one way a command reads a recording, bundle or run-directory file.

A recording, a bundle and a run directory can each be a plain directory or a
sealed set (docs/phi_at_rest.md). These helpers read either: a plain path
reads like :mod:`pathlib`, a path inside a sealed set decrypts in memory with
the configured key (or the local key). Nothing is decrypted to disk.

They delegate to :mod:`openadapt_flow.sealed_set`; there is no crypto here.
Read a workflow through :meth:`openadapt_flow.ir.Workflow.load`, which
verifies a sealed bundle the same way. Files a person passes in that are not
part of an artifact (configs, CSVs, ``deploy.yaml``) are read directly.

``tests/test_artifact_reads.py`` fails when a converted command module reads
an artifact file with ``pathlib`` or ``open()`` instead.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_flow import sealed_set


def open_read(path: Path | str) -> bytes:
    """The bytes of an artifact file, decrypted in memory when sealed."""
    return sealed_set.read_bytes(path)


def read_text(path: Path | str, encoding: str = "utf-8") -> str:
    return sealed_set.read_text(path, encoding)


def is_file(path: Path | str) -> bool:
    return sealed_set.is_file(path)


def is_dir(path: Path | str) -> bool:
    return sealed_set.is_dir(path)


def exists(path: Path | str) -> bool:
    return sealed_set.exists(path)


def list_files(directory: Path | str, pattern: str = "*") -> list[Path]:
    """Artifact files under ``directory`` whose relative path matches
    ``pattern`` (``fnmatch``), by their logical names."""
    return sealed_set.list_files(directory, pattern)


def is_sealed(path: Path | str) -> bool:
    """Whether ``path`` is, or is inside, a sealed set."""
    return sealed_set.is_sealed_path(path)


__all__ = [
    "exists",
    "is_dir",
    "is_file",
    "is_sealed",
    "list_files",
    "open_read",
    "read_text",
]
