"""Atomic, non-destructive encryption of a compiled workflow bundle."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from errno import EEXIST, ENOTEMPTY
from pathlib import Path

from openadapt_flow import crypto
from openadapt_flow.ir import Workflow


class BundleSealingError(ValueError):
    """The requested bundle could not be sealed without changing its source."""


@dataclass(frozen=True)
class SealedBundle:
    """Identity of an atomically published encrypted bundle."""

    path: Path
    content_digest: str


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _reject_symlinks(root: Path) -> None:
    """Refuse a symlink at or anywhere below ``root`` without following it."""
    if root.is_symlink():
        raise BundleSealingError(f"source bundle is a symlink: {root}")
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*dirnames, *filenames):
            candidate = current_path / name
            if candidate.is_symlink():
                rel = candidate.relative_to(root)
                raise BundleSealingError(
                    f"source bundle contains a symlink: {rel.as_posix()}"
                )
            mode = candidate.stat(follow_symlinks=False).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                rel = candidate.relative_to(root)
                raise BundleSealingError(
                    f"source bundle contains a non-file entry: {rel.as_posix()}"
                )


def _validate_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    if source.is_symlink():
        raise BundleSealingError(f"source bundle is a symlink: {source}")
    if not source.is_dir():
        raise BundleSealingError(f"source bundle is not a directory: {source}")

    source = source.resolve()
    destination = Path(os.path.abspath(destination))
    if destination == source:
        raise BundleSealingError("source and destination must be different paths")
    if _lexists(destination):
        raise BundleSealingError(f"destination already exists: {destination}")

    destination_parent = destination.parent.resolve(strict=False)
    destination = destination_parent / destination.name
    if destination.is_relative_to(source):
        raise BundleSealingError("destination cannot be inside the source bundle")
    return source, destination


def _verify_sealed_bundle(bundle: Path, key: str) -> Workflow:
    if (bundle / "workflow.json").exists():
        raise BundleSealingError("sealed bundle retained plaintext workflow.json")
    encrypted_workflow = bundle / "workflow.json.enc"
    if not encrypted_workflow.is_file() or not crypto.is_encrypted(
        encrypted_workflow.read_bytes()
    ):
        raise BundleSealingError("sealed bundle is missing encrypted workflow.json")

    templates = bundle / "templates"
    if templates.is_dir():
        for path in templates.rglob("*"):
            if path.is_symlink():
                raise BundleSealingError("sealed bundle contains a template symlink")
            if not path.is_file():
                continue
            if path.suffix != ".enc" or not crypto.is_encrypted(path.read_bytes()):
                raise BundleSealingError(
                    "sealed bundle retained a plaintext template asset: "
                    f"{path.relative_to(bundle).as_posix()}"
                )

    workflow = Workflow.load(bundle, key=key, verify_integrity=True)
    if not workflow.encrypted or workflow.manifest is None:
        raise BundleSealingError("sealed bundle did not retain its encrypted manifest")
    if not workflow.manifest.content_digest:
        raise BundleSealingError("sealed bundle has no integrity content digest")
    return workflow


def _publish_no_replace(staging: Path, destination: Path) -> None:
    """Atomically rename ``staging`` while refusing a concurrent destination.

    ``Path.rename`` is insufficient on POSIX because it can replace an empty
    destination directory created after the caller's final existence check.
    Use each supported platform's native exclusive-rename operation instead.
    """
    if sys.platform == "darwin":
        libc = CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [c_char_p, c_char_p, c_uint]
        renamex_np.restype = c_int
        # <stdio.h>: RENAME_EXCL refuses an existing destination atomically.
        result = renamex_np(
            os.fsencode(staging),
            os.fsencode(destination),
            c_uint(0x00000004),
        )
    elif sys.platform.startswith("linux"):
        libc = CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise BundleSealingError(
                "this Linux runtime lacks atomic no-replace rename support"
            ) from exc
        renameat2.argtypes = [c_int, c_char_p, c_int, c_char_p, c_uint]
        renameat2.restype = c_int
        # Linux renameat2(2): AT_FDCWD + RENAME_NOREPLACE.
        result = renameat2(
            c_int(-100),
            os.fsencode(staging),
            c_int(-100),
            os.fsencode(destination),
            c_uint(1),
        )
    elif os.name == "nt":
        # Python's os.rename uses MoveFileW on Windows and refuses an existing
        # destination rather than replacing it.
        try:
            os.rename(staging, destination)
        except FileExistsError as exc:
            raise BundleSealingError(
                f"destination appeared during sealing: {destination}"
            ) from exc
        return
    else:
        raise BundleSealingError(
            f"atomic no-replace publication is unavailable on {sys.platform}"
        )

    if result == 0:
        return
    error = get_errno()
    if error in {EEXIST, ENOTEMPTY}:
        raise BundleSealingError(f"destination appeared during sealing: {destination}")
    raise OSError(error, os.strerror(error), destination)


def seal_bundle(source: Path | str, destination: Path | str) -> SealedBundle:
    """Copy, validate, encrypt, verify, and atomically publish a bundle.

    The source is never modified. ``OPENADAPT_BUNDLE_KEY`` is the only supported
    key input so the passphrase cannot leak through process arguments.
    """
    try:
        key = crypto.require_key(None)
    except crypto.MissingKeyError as exc:
        raise BundleSealingError(str(exc)) from exc

    source_path, destination_path = _validate_paths(Path(source), Path(destination))
    _reject_symlinks(source_path)
    try:
        Workflow.load(source_path, key=key, verify_integrity=True)
    except Exception as exc:
        raise BundleSealingError(f"source bundle validation failed: {exc}") from exc

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.seal-",
            dir=destination_path.parent,
        )
    )
    staging.chmod(0o700)
    published = False
    try:
        shutil.copytree(
            source_path,
            staging,
            dirs_exist_ok=True,
            symlinks=True,
        )
        # Refuse a symlink introduced between the source preflight and copy.
        _reject_symlinks(staging)
        staged_workflow = Workflow.load(staging, key=key, verify_integrity=True)
        staged_workflow.save(staging, encrypt=True, key=key)
        _reject_symlinks(staging)
        sealed = _verify_sealed_bundle(staging, key)
        manifest = sealed.manifest
        if manifest is None:  # Defensive: _verify_sealed_bundle already refuses this.
            raise BundleSealingError("sealed bundle did not retain its manifest")

        # Never offer an overwrite mode. Recheck immediately before publication.
        if _lexists(destination_path):
            raise BundleSealingError(
                f"destination appeared during sealing: {destination_path}"
            )
        _publish_no_replace(staging, destination_path)
        published = True
        return SealedBundle(
            path=destination_path,
            content_digest=manifest.content_digest,
        )
    except BundleSealingError:
        raise
    except Exception as exc:
        raise BundleSealingError(f"bundle sealing failed: {exc}") from exc
    finally:
        if not published and _lexists(staging):
            shutil.rmtree(staging)
