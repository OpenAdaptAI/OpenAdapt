"""Portable public registry proofs; no execution or qualification authority.

The wire retains exact signed index/attestation bytes and the two public JSON
files needed to derive program identity. Verification is offline and never
loads encrypted bundles, private admission keys, plugins or workflow code.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from openadapt_flow import registry, registry_attest
from openadapt_flow.registry_snapshot import (
    RegistrySnapshot,
    json_object,
    load_snapshot,
    verify_snapshot,
)

PROOF_SCHEMA = "openadapt.registry-catalog-proof/v1"
CATALOG_SCHEMA = "openadapt.registry-catalog/v1"
MAX_PROOF_BYTES = registry.MAX_ARCHIVE_BYTES


def _decode(value: Any, label: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise registry.RegistryError(f"{label} exceeds its byte limit")
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise registry.RegistryError(f"{label} is not base64") from exc
    if len(payload) > limit:
        raise registry.RegistryError(f"{label} exceeds its byte limit")
    return payload


def _checked_file(entry: registry.Entry, name: str, payload: bytes) -> None:
    if hashlib.sha256(payload).hexdigest() != entry.files.get(name):
        raise registry.RegistryError(
            f"{entry.ref}: {name} does not match the signed index"
        )


def _program_identity(
    entry: registry.Entry, material: dict[str, Any]
) -> dict[str, Any]:
    from openadapt_flow import bundle_validation as bv
    from openadapt_flow.ir import BundleManifest, Workflow
    from openadapt_flow.qualification import workflow_contract_sha256

    if (
        set(material) != {"ref", "workflow_base64", "manifest_base64"}
        or material.get("ref") != entry.ref
    ):
        raise registry.RegistryError("program proof does not name its exact entry ref")
    workflow_bytes = _decode(
        material["workflow_base64"], "workflow", registry.MAX_FILE_BYTES
    )
    _checked_file(entry, "workflow.json", workflow_bytes)
    raw = json_object(workflow_bytes, "workflow")
    manifest_bytes = None
    if material["manifest_base64"] is not None:
        manifest_bytes = _decode(
            material["manifest_base64"], "manifest", registry.MAX_FILE_BYTES
        )
        _checked_file(entry, "manifest.json", manifest_bytes)
    persisted = raw.get("manifest")
    if persisted is None and manifest_bytes is not None:
        persisted = json_object(manifest_bytes, "manifest")
    if not isinstance(persisted, dict):
        raise registry.RegistryError(
            "program proof has no persisted integrity manifest"
        )
    try:
        bv.bundle_schema_version(raw)
        bv.bundle_schema_version(persisted)
        manifest = BundleManifest.model_validate(persisted)
        if not manifest.content_digest or manifest.encrypted or raw.get("encrypted"):
            raise registry.RegistryError(
                "catalog programs need a plaintext persisted integrity seal"
            )
        # Existing canonical projection, not a new digest or an execution load.
        bv.verify_original_content(raw, manifest)
        workflow = Workflow.model_validate(bv.migrate_bundle_dict(raw))
        workflow.manifest = manifest
        bv.validate_workflow(workflow).raise_if(categories=("structure",))
        for rel, digest in manifest.file_hashes.items():
            if not registry._safe_rel(rel) or entry.files.get(rel) != digest:
                raise registry.RegistryError(
                    "a sealed asset is absent or differs in the signed index"
                )
        if not bv._referenced_asset_paths(workflow).issubset(manifest.file_hashes):
            raise registry.RegistryError(
                "a referenced program asset is outside its sealed manifest"
            )
        contract = workflow_contract_sha256(workflow)
    except registry.RegistryError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed public data grants no authority
        raise registry.RegistryError(
            "program proof failed canonical integrity or structural validation"
        ) from exc
    return {
        "id": entry.id,
        "version": entry.version,
        "ref": entry.ref,
        "status": entry.status,
        "entry_sha256": registry.entry_sha256(entry.raw),
        "workflow_sha256": hashlib.sha256(workflow_bytes).hexdigest(),
        "bundle_content_digest": manifest.content_digest,
        "workflow_contract_sha256": contract,
    }


def _publisher(snapshot: RegistrySnapshot) -> str:
    san = snapshot.attested_by
    if san is None or registry_attest.REGISTRY_IDENTITY.san.fullmatch(san) is None:
        raise registry.RegistryError(
            "a catalog requires the pinned production registry publisher"
        )
    return san.rsplit("@refs/tags/", 1)[1]


def _summary(
    snapshot: RegistrySnapshot, materials: list[Any], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    snapshot.require_current(now=now)
    release = _publisher(snapshot)
    entries = snapshot.entries
    if any(entry.status == "planned" for entry in entries):
        raise registry.RegistryError(
            "a published catalog cannot contain planned entries"
        )
    if len(materials) != len(entries):
        raise registry.RegistryError(
            "program proof coverage does not match the signed index"
        )
    by_ref = {}
    for material in materials:
        if (
            not isinstance(material, dict)
            or not isinstance(material.get("ref"), str)
            or material["ref"] in by_ref
        ):
            raise registry.RegistryError(
                "program proofs have missing or duplicate refs"
            )
        by_ref[material["ref"]] = material
    if set(by_ref) != {entry.ref for entry in entries}:
        raise registry.RegistryError("program proof refs do not match the signed index")
    for entry in entries:
        archive = entry.raw.get("archive")
        if (
            archive is not None
            and archive.get("sha256") not in snapshot.subjects.values()
        ):
            raise registry.RegistryError(
                "the attestation does not name this entry's archive"
            )
    data = snapshot.data
    return {
        "schema": CATALOG_SCHEMA,
        "index_sha256": snapshot.index_sha256,
        "attested_by": snapshot.attested_by,
        "release_tag": release,
        "issued_at": data["issued_at"],
        "expires_at": data["expires_at"],
        "serial": data["serial"],
        "entries": [_program_identity(entry, by_ref[entry.ref]) for entry in entries],
    }


def verify_catalog(
    proof: dict[str, Any], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Recompute a public catalog projection from proof, never from its claims."""
    if (
        set(proof) != {"schema", "index_base64", "attestation_base64", "programs"}
        or proof.get("schema") != PROOF_SCHEMA
    ):
        raise registry.RegistryError("unsupported registry catalog proof schema")
    materials = proof["programs"]
    if not isinstance(materials, list) or len(materials) > registry.MAX_ARCHIVE_MEMBERS:
        raise registry.RegistryError("program proofs must be a bounded list")
    raw = _decode(proof["index_base64"], "index", registry.MAX_FILE_BYTES)
    attestation = _decode(
        proof["attestation_base64"], "attestation", registry.MAX_ATTESTATION_BYTES
    )
    snapshot = verify_snapshot(
        raw, attestation, index_url="https://catalog-proof.invalid/index.json", now=now
    )
    return _summary(snapshot, materials, now=now)


def _program_material(
    snapshot: RegistrySnapshot, entry: registry.Entry
) -> dict[str, Any]:
    remote = registry._remote(snapshot.index_url)
    local_root = None if remote else Path(snapshot.index_url).resolve().parent
    archive = entry.raw.get("archive")
    unpacked = None
    if archive:
        if archive.get("sha256") not in snapshot.subjects.values():
            raise registry.RegistryError(
                "the attestation does not name this entry's archive"
            )
        unpacked = registry._unpack_archive(
            archive, expected=set(entry.files), local_root=local_root
        )
    base = registry._join(
        snapshot.index_url, str(entry.raw.get("path", "")).rstrip("/") + "/"
    )
    files = {}
    for name in ("workflow.json", "manifest.json"):
        if name not in entry.files:
            continue
        if unpacked is not None:
            if name not in unpacked:
                raise registry.RegistryError(
                    "a program identity file is missing from the archive"
                )
            payload = unpacked[name]
        else:
            location = urljoin(base, name) if remote else str(Path(base) / name)
            payload = registry._read(location, local_root=local_root)
        _checked_file(entry, name, payload)
        files[name] = base64.b64encode(payload).decode("ascii")
    if "workflow.json" not in files:
        raise registry.RegistryError("a catalog entry needs its exact workflow.json")
    return {
        "ref": entry.ref,
        "workflow_base64": files["workflow.json"],
        "manifest_base64": files.get("manifest.json"),
    }


def export_catalog(index_url: str = registry.DEFAULT_INDEX_URL) -> dict[str, Any]:
    snapshot = load_snapshot(index_url)
    _publisher(snapshot)
    materials = [_program_material(snapshot, entry) for entry in snapshot.entries]
    _summary(snapshot, materials)
    assert snapshot.attestation_bytes is not None
    return {
        "schema": PROOF_SCHEMA,
        "index_base64": base64.b64encode(snapshot.index_bytes).decode("ascii"),
        "attestation_base64": base64.b64encode(snapshot.attestation_bytes).decode(
            "ascii"
        ),
        "programs": materials,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", allow_abbrev=False)
    export.add_argument("--index", default=registry.DEFAULT_INDEX_URL)
    export.add_argument("--out", type=Path, required=True)
    verify = commands.add_parser("verify", allow_abbrev=False)
    verify.add_argument("proof", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            proof = export_catalog(args.index)
            payload = (json.dumps(proof, indent=2) + "\n").encode()
            if len(payload) > MAX_PROOF_BYTES:
                raise registry.RegistryError("catalog proof exceeds its byte limit")
            with args.out.open("xb") as stream:
                stream.write(payload)
        else:
            if args.proof.stat().st_size > MAX_PROOF_BYTES:
                raise registry.RegistryError("catalog proof exceeds its byte limit")
            with args.proof.open("rb") as stream:
                payload = stream.read(MAX_PROOF_BYTES + 1)
            if len(payload) > MAX_PROOF_BYTES:
                raise registry.RegistryError("catalog proof exceeds its byte limit")
            proof = json_object(payload, "catalog proof")
            print(
                json.dumps(verify_catalog(proof), sort_keys=True, separators=(",", ":"))
            )
        return 0
    except (registry.RegistryError, OSError) as exc:
        print(f"registry-catalog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
