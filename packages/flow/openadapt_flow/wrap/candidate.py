"""Capture an explicitly untrusted local program for offline registry review.

This does not resolve the wrap cache, inspect admission files, load executable
workflow code, sanitize, qualify, sign, publish or emit an executable server.
Only the caller's explicit plaintext bundle and its sealed image assets are
read. Integrity identifies bytes; it does not confer execution authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from openadapt_flow import bundle_validation as bv
from openadapt_flow import registry
from openadapt_flow.ir import BundleManifest, Workflow

SCHEMA = "openadapt.wrap-candidate/v1"
MANIFEST = "candidate.json"
_MCP_REFUSAL = (
    "A local candidate cannot emit an executable MCP server. Qualify the original "
    "bundle on the intended instance through the existing local certification "
    "path, then use emit-mcp with that qualified bundle. This export does not "
    "establish qualification, admission or permission to execute."
)


class CandidateError(ValueError):
    """A stable refusal without artifact contents or private filenames."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _json_object(data: bytes) -> dict[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> Any:
        raise ValueError("nonfinite JSON number")

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result

    value = json.loads(
        data, object_pairs_hook=pairs, parse_constant=constant, parse_float=number
    )
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _read_file(root: int, rel: str) -> bytes:
    """Bounded regular-file read, refusing links at every relative component."""
    parts = bv.bundle_asset_relative(rel).parts
    directory = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("regular file required")
            data = handle.read(registry.MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        identity = lambda row: (  # noqa: E731 - compare the two observations
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_ctime_ns,
        )
        if len(data) > registry.MAX_FILE_BYTES or identity(before) != identity(after):
            raise ValueError("oversized or changing input")
        return data
    finally:
        os.close(directory)


def _template_files(root: int) -> set[str]:
    """Inventory without reading unknown files or following directory links."""
    result: set[str] = set()
    count = 0

    def walk(directory: int, prefix: str) -> None:
        nonlocal count
        if len(Path(prefix).parts) > 32:
            raise ValueError("asset path too deep")
        for name in os.listdir(directory):
            count += 1
            if count > registry.MAX_ARCHIVE_MEMBERS - 2:
                raise ValueError("too many asset entries")
            rel = f"{prefix}/{name}"
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                try:
                    walk(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode) and registry.public_bundle_file(rel):
                result.add(rel)
                if len(result) > registry.MAX_ARCHIVE_MEMBERS - 2:
                    raise ValueError("too many assets")
            else:
                raise ValueError("unsupported template entry")

    try:
        templates = os.open(
            "templates", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
    except FileNotFoundError:
        return result
    try:
        walk(templates, "templates")
    finally:
        os.close(templates)
    return result


def _capture(bundle: Path) -> tuple[dict[str, bytes], str]:
    """Validate one captured occurrence without granting verified-load authority."""
    root = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Metadata only: never open an encrypted payload or a key.
        for marker in (".openadapt-sealed.json", "workflow.json.enc"):
            try:
                os.stat(marker, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise CandidateError(
                "encrypted_input", "Use an explicitly prepared plaintext local bundle."
            )
        files = {"workflow.json": _read_file(root, "workflow.json")}
        raw = _json_object(files["workflow.json"])
        bv.bundle_schema_version(raw)
        persisted = raw.get("manifest")
        if persisted is None:
            files["manifest.json"] = _read_file(root, "manifest.json")
            persisted = _json_object(files["manifest.json"])
        if not isinstance(persisted, dict):
            raise ValueError("manifest required")
        bv.bundle_schema_version(persisted)
        stored = BundleManifest.model_validate(persisted)
        if not stored.content_digest:
            raise ValueError("persisted integrity seal required")
        if raw.get("encrypted") or stored.encrypted:
            raise CandidateError(
                "encrypted_input", "Use an explicitly prepared plaintext local bundle."
            )
        bv.verify_original_content(raw, stored)
        workflow = Workflow.model_validate(bv.migrate_bundle_dict(raw))
        workflow.manifest = stored
        bv.validate_workflow(workflow).raise_if(categories=("structure",))
        assets = _template_files(root)
        if assets != set(stored.file_hashes):
            raise ValueError("sealed asset inventory differs")
        if not bv._referenced_asset_paths(workflow).issubset(assets):
            raise ValueError("missing referenced asset")
        total = sum(map(len, files.values()))
        for rel in sorted(assets):
            bv.bundle_asset_relative(rel)
            files[rel] = _read_file(root, rel)
            total += len(files[rel])
            if total > registry.MAX_ARCHIVE_BYTES:
                raise ValueError("candidate too large")
            if hashlib.sha256(files[rel]).hexdigest() != stored.file_hashes[rel]:
                raise ValueError("asset digest differs")
        if sum(map(len, files.values())) > registry.MAX_ARCHIVE_BYTES:
            raise ValueError("candidate too large")
        # A concurrent replacement must not turn metadata from one occurrence
        # into the assets of another. Output is built solely from these bytes.
        for rel, data in files.items():
            if _read_file(root, rel) != data:
                raise ValueError("input changed during capture")
        if _template_files(root) != assets:
            raise ValueError("asset inventory changed during capture")
        return files, stored.content_digest
    finally:
        os.close(root)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def export_candidate(bundle: Path | str, out: Path | str) -> dict[str, Any]:
    """Write a new private local handoff; never overwrite or infer trust.

    Only workflow JSON, its effective sidecar manifest (when needed), and the
    exact sealed public image set are copied. Extra source files are not read.
    The nested bundle retains its original bytes and claims as untrusted input;
    this manifest makes no certification/admission/publication decision.
    """
    if (
        not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.listdir not in os.supports_fd
    ):
        raise CandidateError(
            "unsupported_filesystem",
            "Candidate capture requires no-follow directory-relative file access.",
        )
    source, destination = Path(bundle).absolute(), Path(out).absolute()
    try:
        input_root, output_root = source.resolve(), destination.resolve()
    except (OSError, RuntimeError, ValueError):
        raise CandidateError(
            "invalid_paths", "Input and output paths could not be resolved safely."
        ) from None
    if output_root.is_relative_to(input_root) or input_root.is_relative_to(output_root):
        raise CandidateError(
            "overlapping_paths", "Choose disjoint input and output paths."
        )
    try:
        files, digest = _capture(source)
    except CandidateError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        bv.BundleIntegrityError,
        bv.BundleValidationError,
    ):
        raise CandidateError(
            "invalid_bundle",
            "The plaintext bundle or its sealed asset set failed validation.",
        ) from None
    try:
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        raise CandidateError(
            "output_unavailable",
            "The output must be a new directory in an existing parent.",
        ) from None
    created = destination.stat(follow_symlinks=False)
    try:
        payload = destination / "bundle"
        for rel, data in files.items():
            _write(payload / rel, data)
        hashes = registry.build_entry_files(payload)
        if hashes != {
            rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()
        }:
            raise ValueError("staged file identity changed")
        archive = destination / "bundle.tar.gz"
        archive_sha256 = registry.build_archive(payload, archive)
        archive.chmod(0o600)
        if (
            registry._unpack_archive(
                {"url": archive.name, "sha256": archive_sha256},
                expected=set(files),
                local_root=destination,
            )
            != files
            or registry.build_entry_files(payload) != hashes
        ):
            raise ValueError("archive or payload identity changed")
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "state": "untrusted-local-candidate",
            "bundle": {
                "directory": "bundle",
                "content_digest": digest,
                "files": hashes,
            },
            "archive": {"path": "bundle.tar.gz", "sha256": archive_sha256},
            "not_granted": [
                "sanitization",
                "qualification",
                "admission",
                "publication",
                "permission-to-execute",
            ],
            "requires": [
                "privacy-and-license-review",
                "publication-approval",
                "independent-qualification-on-the-intended-instance",
            ],
            "mcp": "not-emitted; use the existing local qualification and emit path",
        }
        _write(
            destination / MANIFEST,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
        return manifest
    except Exception:
        try:
            current = destination.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                shutil.rmtree(destination)
        except OSError:
            pass  # An incomplete private output may remain; never hide the refusal.
        raise CandidateError(
            "export_failed",
            "The local candidate was not produced. Any remaining output is incomplete; "
            "do not use it. No input was written by the exporter.",
        ) from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--bundle", required=True, type=Path, help="Explicit plaintext bundle"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="New local candidate directory"
    )
    parser.add_argument(
        "--emit-mcp",
        action="store_true",
        help="Refuses: a candidate grants no execution authority",
    )
    args = parser.parse_args(argv)
    try:
        if args.emit_mcp:
            raise CandidateError("qualification_required", _MCP_REFUSAL)
        result = export_candidate(args.bundle, args.out)
    except CandidateError as exc:
        print(
            json.dumps({"state": "REFUSED", "code": exc.code, "detail": str(exc)}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
