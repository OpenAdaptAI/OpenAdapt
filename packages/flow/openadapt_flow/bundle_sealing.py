"""Atomic, non-destructive encryption of a whole compiled workflow bundle."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from datetime import datetime, timezone
from errno import EEXIST, ENOTEMPTY
from pathlib import Path

from openadapt_flow import crypto, sealed_set
from openadapt_flow.ir import Workflow


class BundleSealingError(ValueError):
    """The requested bundle could not be sealed without changing its source."""


@dataclass(frozen=True)
class SealedBundle:
    """Identity of an atomically published encrypted bundle."""

    path: Path
    content_digest: str
    certification_invalidated: bool
    set_digest: str = ""


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


def _verify_sealed_bundle(bundle: Path, key: str) -> tuple[Workflow, str]:
    """Re-open the staged bundle exactly as a reader would and check that no
    file holding patient data is left in plaintext."""
    try:
        opened = sealed_set.SealedSet.open(bundle, key, expect="bundle")
        opened.verify_all()
    except sealed_set.SealedSetError as exc:
        raise BundleSealingError(f"sealed bundle failed verification: {exc}") from exc
    for rel in opened.files():
        entry = sealed_set.classify("bundle", rel)
        if entry is None or (
            entry.classification != "structural" and opened.codec(rel) != "set"
        ):
            raise BundleSealingError(f"sealed bundle retained plaintext {rel}")
    workflow = Workflow.load(bundle, key=key, verify_integrity=True)
    if not workflow.encrypted or workflow.manifest is None:
        raise BundleSealingError("sealed bundle did not retain its encrypted manifest")
    if not workflow.manifest.content_digest:
        raise BundleSealingError("sealed bundle has no integrity content digest")
    return workflow, opened.set_digest


def _source_files(source: Path, key: str) -> dict[str, bytes]:
    """Every file of the source bundle except the parts ``Workflow`` owns,
    read into memory and classified. An unknown file is refused."""
    handled = {"workflow.json", "workflow.json.enc", "manifest.json"}
    reader: sealed_set.SealedSet | None = None
    if sealed_set.is_sealed(source):
        reader = sealed_set.SealedSet.open(source, key, expect="bundle")
        names = reader.files()
    else:
        names = [
            p.relative_to(source).as_posix()
            for p in sorted(source.rglob("*"))
            if p.is_file()
        ]
    out: dict[str, bytes] = {}
    unknown: list[str] = []
    for rel in names:
        if rel in handled or rel.startswith("templates/"):
            continue
        if sealed_set.classify("bundle", rel) is None:
            unknown.append(rel)
            continue
        out[rel] = reader.read_bytes(rel) if reader else (source / rel).read_bytes()
    if unknown:
        raise BundleSealingError(
            "source bundle contains file(s) Flow does not know, so it cannot "
            "say whether they hold patient data: "
            + ", ".join(unknown)
            + ". Move them out of the bundle (or delete them) and seal again."
        )
    return out


def _template_assets(source: Path, workflow: Workflow) -> dict[str, bytes]:
    """Plaintext template crops: decrypted in memory for an encrypted source,
    read from disk for a plaintext one."""
    assets = workflow.decrypted_templates()
    tdir = source / "templates"
    if tdir.is_dir() and not sealed_set.is_sealed(source):
        for path in sorted(tdir.rglob("*")):
            if path.is_file() and path.suffix != ".enc":
                assets.setdefault(path.relative_to(source).as_posix(), path.read_bytes())
    return assets


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


def _invalidate_prior_certification(
    workflow: Workflow,
    *,
    reason: str = "at-rest sealing changed the workflow contract",
) -> bool:
    """Expire decisions bound to the prior workflow contract.

    ``Workflow.encrypted`` is sealed into the workflow contract and content
    digest.  Turning it on therefore creates a new artifact contract even when
    executable intent is unchanged.  Retain the prior policy and timestamp as
    provenance, but never carry its success bit or qualification decision into
    the new artifact.  Record-check binding reuses this with its own ``reason``.
    """
    provenance = workflow.manifest.provenance if workflow.manifest else None
    qualification = workflow.qualification
    had_certification = bool(
        (qualification is not None and qualification.last_certification is not None)
        or (
            provenance is not None
            and (
                provenance.certified
                or provenance.policy_name is not None
                or provenance.certification_status is not None
                or provenance.certified_at is not None
                or provenance.expires_at is not None
            )
        )
    )
    if not had_certification:
        return False

    if qualification is not None:
        qualification.last_certification = None
    if provenance is not None:
        invalidated_at = datetime.now(timezone.utc).isoformat()
        provenance.certified = False
        provenance.certification_status = "expired"
        provenance.expires_at = invalidated_at
        provenance.certification_invalidated_at = invalidated_at
        provenance.certification_invalidation_reason = reason
    return True


def _cleanup_staging(staging: Path | None, original_error: Exception) -> None:
    if staging is None or not _lexists(staging):
        return
    try:
        shutil.rmtree(staging)
    except OSError as cleanup_error:
        raise BundleSealingError(
            f"{original_error}; private staging cleanup failed: {cleanup_error}"
        ) from original_error


def seal_bundle(source: Path | str, destination: Path | str) -> SealedBundle:
    """Validate, encrypt, verify, and atomically publish a sealed bundle.

    Every file of the source is classified against
    :data:`openadapt_flow.sealed_set.BUNDLE_FILES`; an unknown file refuses
    the seal. Every file that can hold patient data is encrypted in memory,
    and the whole set is bound to one authenticated index, so no file can be
    swapped, added or dropped afterwards (:mod:`openadapt_flow.sealed_set`).
    The source is never modified. ``OPENADAPT_BUNDLE_KEY`` is the only
    supported key input so the passphrase cannot leak through process
    arguments.
    """
    staging: Path | None = None
    try:
        try:
            key = crypto.require_key(None)
        except crypto.MissingKeyError as exc:
            raise BundleSealingError(str(exc)) from exc

        source_path, destination_path = _validate_paths(Path(source), Path(destination))
        _reject_symlinks(source_path)
        try:
            source_workflow = Workflow.load(
                source_path, key=key, verify_integrity=True
            )
        except Exception as exc:
            raise BundleSealingError(f"source bundle validation failed: {exc}") from exc
        others = _source_files(source_path, key)
        assets = _template_assets(source_path, source_workflow)

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination_path.name}.seal-",
                dir=destination_path.parent,
            )
        )
        staging.chmod(0o700)
        # Nothing is copied: every byte is encrypted in memory and only
        # ciphertext (and the structural manifest) is written to staging.
        certification_invalidated = _invalidate_prior_certification(source_workflow)
        source_workflow._decrypted_templates = dict(assets)
        with sealed_set.writing(staging, "bundle", key) as writer:
            source_workflow.save(staging, encrypt=True, key=key)
            for rel, data in others.items():
                writer.write(rel, data)
        _reject_symlinks(staging)
        sealed, set_digest = _verify_sealed_bundle(staging, key)
        manifest = sealed.manifest
        if manifest is None:  # Defensive: _verify_sealed_bundle already refuses this.
            raise BundleSealingError("sealed bundle did not retain its manifest")

        # Never offer an overwrite mode. Recheck immediately before publication.
        if _lexists(destination_path):
            raise BundleSealingError(
                f"destination appeared during sealing: {destination_path}"
            )
        _publish_no_replace(staging, destination_path)
        return SealedBundle(
            path=destination_path,
            content_digest=manifest.content_digest,
            certification_invalidated=certification_invalidated,
            set_digest=set_digest,
        )
    except BundleSealingError as exc:
        _cleanup_staging(staging, exc)
        raise
    except Exception as exc:
        _cleanup_staging(staging, exc)
        raise BundleSealingError(f"bundle sealing failed: {exc}") from exc
