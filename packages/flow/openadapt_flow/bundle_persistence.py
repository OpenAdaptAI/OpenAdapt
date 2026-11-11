"""Persist verified workflow derivatives without changing source authority."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from openadapt_flow.ir import Workflow


def stage_verified_template_replacement(
    workflow: Workflow, rel: str, data: bytes
) -> None:
    """Stage bytes for an existing template on a verified loaded bundle.

    The original bundle and its in-memory decrypted asset cache are never
    changed here. ``save`` verifies the source again before applying the
    staged bytes to the destination and sealing the new content.
    """
    from openadapt_flow import bundle_validation as _bv

    if not isinstance(data, bytes):
        raise _bv.BundleIntegrityError("template replacement data must be bytes")
    if (
        workflow._unverified_load
        or workflow.manifest is None
        or not workflow.manifest.content_digest
        or workflow._loaded_seal is None
        or not _bv.has_verified_load_capability(workflow)
    ):
        raise _bv.BundleIntegrityError(
            "template replacement requires a verified loaded bundle"
        )
    if not isinstance(rel, str) or not rel.startswith("templates/"):
        raise _bv.BundleIntegrityError(
            "template replacement must name a manifest-listed template asset"
        )
    source = workflow._loaded_seal.source_bundle_dir
    source_asset = _bv.bundle_asset_path(source, rel)
    source_has_asset = (
        rel in workflow._decrypted_templates
        if workflow._loaded_seal.encrypted
        else source_asset.is_file()
    )
    if rel not in workflow.manifest.file_hashes or not source_has_asset:
        raise _bv.BundleIntegrityError(
            "template replacement must name an existing manifest-listed asset"
        )
    _bv.verify_loaded_source_integrity(
        workflow,
        source,
        workflow.manifest,
        decrypted_assets=(
            workflow._decrypted_templates if workflow._loaded_seal.encrypted else None
        ),
    )
    workflow._pending_template_replacements = {
        **workflow._pending_template_replacements,
        rel: bytes(data),
    }
    workflow._pending_derived_assets.pop(rel, None)


def _verified_source_assets(workflow: Workflow) -> dict[str, bytes]:
    from openadapt_flow import bundle_validation as _bv

    source_assets: dict[str, bytes] = {}
    if workflow._loaded_seal is not None:
        if workflow.manifest is None:
            raise _bv.BundleIntegrityError(
                "loaded bundle lost its original integrity manifest"
            )
        source = workflow._loaded_seal.source_bundle_dir
        _bv.verify_loaded_source_integrity(
            workflow,
            source,
            workflow.manifest,
            decrypted_assets=(
                workflow._decrypted_templates if workflow.encrypted else None
            ),
        )
        # Original verified bytes also belong to an in-place destination.
        if not workflow._loaded_seal.encrypted:
            for rel, expected in workflow._loaded_seal.file_hashes:
                source_asset = _bv.bundle_asset_path(source, rel)
                data = source_asset.read_bytes() if source_asset.is_file() else None
                if data is None or _bv._sha256_bytes(data) != expected:
                    raise _bv.BundleIntegrityError(
                        f"verified source asset {rel!r} changed before save-as"
                    )
                source_assets[rel] = data

    return source_assets


def _check_destination_assets(
    workflow: Workflow,
    bundle: Path,
    source_assets: dict[str, bytes],
    template_replacements: dict[str, bytes],
) -> None:
    from openadapt_flow import bundle_validation as _bv

    # A save-as destination may already contain assets. Never let a
    # destination-owned file silently replace the verified source asset in
    # the new manifest. Check every collision before creating directories,
    # writing bundle files, or sealing the derivative.
    accepted_asset_bytes: dict[str, set[bytes]] = {}
    for asset_map in (
        source_assets,
        workflow._decrypted_templates,
        template_replacements,
    ):
        for rel, data in asset_map.items():
            accepted_asset_bytes.setdefault(rel, set()).add(data)

    for rel, expected_bytes in accepted_asset_bytes.items():
        target_asset = _bv.bundle_asset_path(bundle, rel)
        if not target_asset.exists():
            continue
        try:
            matches_source = (
                target_asset.is_file() and target_asset.read_bytes() in expected_bytes
            )
        except OSError:
            matches_source = False
        if not matches_source:
            raise _bv.BundleIntegrityError(
                f"save-as destination asset {rel!r} conflicts with verified source"
            )


def _asset_authority(workflow: Workflow) -> None:
    from openadapt_flow import bundle_validation as _bv

    if workflow._unverified_load:
        raise _bv.BundleIntegrityError(
            "an unchecked load cannot stage derivative assets"
        )
    if workflow.manifest is None and workflow._loaded_seal is None:
        return  # Fresh construction grants no verified-source authority.
    if workflow.manifest is None or workflow._loaded_seal is None:
        raise _bv.BundleIntegrityError(
            "asset staging requires a verified source or fresh construction"
        )
    _bv.verify_loaded_source_integrity(
        workflow,
        workflow._loaded_seal.source_bundle_dir,
        workflow.manifest,
        decrypted_assets=workflow._decrypted_templates
        if workflow._loaded_seal.encrypted
        else None,
    )


def _checked_derived_assets(workflow: Workflow) -> dict[str, bytes]:
    from openadapt_flow import bundle_validation as _bv

    changes = dict(workflow._pending_derived_assets)
    if not changes:
        return {}
    references = _bv._referenced_asset_paths(workflow)
    original = workflow._loaded_seal
    checked = {}
    for rel, change in changes.items():
        if (
            not isinstance(rel, str)
            or rel not in references
            or type(change) is not tuple
            or len(change) != 2
            or type(change[0]) is not str
            or change[0] not in {"add", "replace"}
            or not isinstance(change[1], bytes)
        ):
            raise _bv.BundleIntegrityError(
                "derived assets require canonical referenced paths and bytes"
            )
        _bv.bundle_asset_relative(rel)
    _asset_authority(workflow)
    for rel, (mode, data) in changes.items():
        if original is None:
            if mode != "add":
                raise _bv.BundleIntegrityError(
                    "fresh construction cannot replace a verified source asset"
                )
        else:
            in_source = rel in dict(original.file_hashes)
            source = _bv.bundle_asset_path(original.source_bundle_dir, rel)
            if (mode == "replace") != in_source:
                raise _bv.BundleIntegrityError(
                    "derived asset mode does not match its verified source"
                )
            if mode == "add" and source.exists():
                raise _bv.BundleIntegrityError(
                    "new asset already exists outside the verified source manifest"
                )
        checked[rel] = data
    return checked


def _ensure_output_layout(
    workflow: Workflow,
    destination: Path,
    output: Literal["plain", "encrypted", "sealed"],
    edits: Iterable[str] = (),
) -> None:
    from openadapt_flow import bundle_validation as _bv
    from openadapt_flow import sealed_set as _sealed

    if type(output) is not str or output not in {"plain", "encrypted", "sealed"}:
        raise _bv.BundleIntegrityError("unknown derivative output layout")
    if (
        output != "plain"
        or _sealed.active_writer(destination) is not None
        or _sealed.is_sealed(destination)
    ):
        # An absent reference is not emitted or included in the manifest.
        # Actual carried, staged, or existing referenced bytes must stay within
        # the layout covered by the native encrypted writers.
        assets = set(edits)
        assets.update(workflow._pending_derived_assets)
        assets.update(workflow._decrypted_templates)
        if workflow._loaded_seal is not None:
            assets.update(dict(workflow._loaded_seal.file_hashes))
        assets.update(
            rel
            for rel in _bv._referenced_asset_paths(workflow)
            if not rel.startswith("templates/")
            and _bv.bundle_asset_path(destination, rel).is_file()
        )
        if any(not rel.startswith("templates/") for rel in assets):
            raise _bv.BundleIntegrityError(
                "encrypted derivative assets must remain under templates/"
            )


def validate_derived_asset_paths(workflow: Workflow, names: Iterable[str]) -> Workflow:
    """Validate edit names against the full derived shape before asset I/O.

    This parsed model has no verified-load capability. Only preparation with
    the original source object can transfer that context.
    """
    from openadapt_flow import bundle_validation as _bv

    candidate = type(workflow).model_validate(workflow.model_dump(mode="python"))
    references = _bv._referenced_asset_paths(candidate)
    for rel in names:
        _bv.bundle_asset_relative(rel)
        if rel not in references:
            raise _bv.BundleIntegrityError(
                "derivative assets must reference validated workflow paths"
            )
    return candidate


def prepare_derived_assets(
    workflow: Workflow,
    destination: Path | str,
    changes: dict[str, bytes],
    *,
    output: Literal["plain", "encrypted", "sealed"] = "plain",
) -> None:
    """Stage declared asset edits atomically before any destination effects."""
    from openadapt_flow import bundle_validation as _bv

    if type(changes) is not dict:
        raise _bv.BundleIntegrityError(
            "derivative assets must be an explicit path-to-bytes map"
        )
    candidate = validate_derived_asset_paths(workflow, changes)
    if any(not isinstance(data, bytes) for data in changes.values()):
        raise _bv.BundleIntegrityError("derivative assets must contain bytes")
    # Transfer authority only from the actual process-bound verified source.
    # Fresh construction carries edit data, never a loaded-source capability.
    if _bv.has_verified_load_capability(workflow):
        _bv.transfer_verified_load_capability(workflow, candidate)
    elif (
        workflow.manifest is None
        and workflow._loaded_seal is None
        and not workflow._unverified_load
    ):
        candidate._pending_derived_assets = dict(workflow._pending_derived_assets)
    else:
        raise _bv.BundleIntegrityError(
            "derivative assets require verified source authority or fresh construction"
        )
    _ensure_output_layout(candidate, Path(destination), output, changes)
    _asset_authority(candidate)
    original_paths = (
        dict(candidate._loaded_seal.file_hashes)
        if candidate._loaded_seal is not None
        else {}
    )
    for rel, data in changes.items():
        candidate._pending_template_replacements.pop(rel, None)
        candidate._pending_derived_assets = {
            **candidate._pending_derived_assets,
            rel: ("replace" if rel in original_paths else "add", data),
        }
    assets = {
        **candidate._checked_template_replacements(),
        **_checked_derived_assets(candidate),
    }
    source_assets = _verified_source_assets(candidate)
    _check_destination_assets(candidate, Path(destination), source_assets, assets)
    if changes:
        _bv.invalidate_derivative_certification(candidate)
    workflow.manifest = candidate.manifest
    workflow.qualification = candidate.qualification
    workflow._pending_template_replacements = dict(
        candidate._pending_template_replacements
    )
    workflow._pending_derived_assets = dict(candidate._pending_derived_assets)


def copy_genesis_template_inputs(
    workflow: Workflow, source: Path | str, destination: Path | str
) -> None:
    """Copy explicit fresh-build inputs without claiming verified-source authority."""
    from openadapt_flow import bundle_validation as _bv

    _asset_authority(workflow)
    if workflow._loaded_seal is not None:
        return  # Verified saves carry only their hash-admitted original assets.
    templates = Path(source) / "templates"
    inputs = {}
    if templates.is_dir():
        for path in sorted(templates.rglob("*")):
            if path.is_file():
                rel = "templates/" + path.relative_to(templates).as_posix()
                inputs[rel] = _bv.bundle_asset_path(source, rel).read_bytes()
    _check_destination_assets(
        workflow, Path(destination), inputs, _checked_derived_assets(workflow)
    )
    for rel, data in inputs.items():
        target = _bv.bundle_asset_path(destination, rel)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def save_workflow(
    workflow: Workflow,
    bundle_dir: Path | str,
    *,
    seal_manifest: bool = True,
    encrypt: bool = False,
    key: Optional[str] = None,
) -> Path:
    """Write workflow.json into bundle_dir (templates are written by the
    compiler / healer, which own the crop images).

    Schema v2: unless ``seal_manifest=False``, (re)computes and seals the
    integrity/provenance manifest (per-asset hashes + whole-bundle content
    digest + compiler version, carrying over any prior certification) and
    also writes it to a standalone ``manifest.json`` sidecar for external
    tooling. The ``schema_version`` is bumped to the current version.

    Encryption-at-rest (opt-in, OFF by default): when ``encrypt=True`` (or a
    ``key`` is supplied), the serialized ``workflow.json`` is sealed with
    AES-256-GCM (``openadapt_flow.crypto``) and written as
    ``workflow.json.enc`` instead of plaintext ``workflow.json``, AND every
    ``templates/*.png`` image crop -- pixels of the recorded screen, i.e.
    image PHI -- is sealed the same way (under the distinct
    :data:`TEMPLATE_AAD` domain) as ``templates/*.png.enc`` with its
    plaintext removed, so an encrypted bundle leaves NO cleartext
    PHI-bearing screenshot on disk. The passphrase comes from ``key`` or the
    ``OPENADAPT_BUNDLE_KEY`` environment variable (a missing key raises
    ``crypto.MissingKeyError`` -- an encrypt request never silently degrades
    to plaintext). The integrity manifest is sealed over the PLAINTEXT
    content (workflow AND crop digests) BEFORE encryption, so an encrypted
    bundle keeps every schema-v2 guarantee (content digest, asset hashes,
    provenance) once decrypted at load. The ``manifest.json`` sidecar stays
    plaintext (it carries only hashes + provenance, no PHI) so a compliance
    inventory can read ``encrypted: true`` without the key. When
    ``encrypt=False`` and no key is given, behavior is unchanged: a plaintext
    ``workflow.json`` and plaintext ``templates/*.png`` crops are written
    exactly as before.

    Returns the path actually written (``workflow.json`` or, when encrypted,
    ``workflow.json.enc``).
    """
    from openadapt_flow import bundle_validation as _bv
    from openadapt_flow import ir as _ir

    _bv.bundle_schema_version({"schema_version": workflow.schema_version})
    template_replacements = {
        **_checked_derived_assets(workflow),
        **workflow._checked_template_replacements(),
    }
    if template_replacements and not seal_manifest:
        raise _bv.BundleIntegrityError(
            "template replacements require a newly sealed manifest"
        )
    if workflow._unverified_load:
        raise _bv.BundleIntegrityError(
            "an unchecked bundle load cannot be saved or resealed"
        )
    if (
        workflow.manifest is not None
        and workflow.manifest.content_digest
        and not _bv.has_verified_load_capability(workflow)
    ):
        raise _bv.BundleIntegrityError(
            "a manifest-bearing workflow needs its non-serializable "
            "verified-load capability before it can be saved"
        )
    if workflow._loaded_seal is not None and not seal_manifest:
        raise _bv.BundleIntegrityError(
            "a loaded sealed bundle requires explicit resealing before serialization"
        )
    do_encrypt = encrypt or key is not None
    bundle = _ir.Path(bundle_dir)
    from openadapt_flow import sealed_set as _sealed

    _ensure_output_layout(
        workflow, bundle, "encrypted" if do_encrypt else "plain", template_replacements
    )
    source_assets = _verified_source_assets(workflow)
    _check_destination_assets(workflow, bundle, source_assets, template_replacements)

    if _sealed.active_writer(bundle) is not None or _sealed.is_sealed(bundle):
        # A sealed bundle stays sealed whatever ``encrypt`` says: many
        # commands re-save a bundle with a bare ``save(bundle)``, and that
        # must never turn a sealed bundle back into plaintext.
        path = workflow._save_sealed_set(
            bundle,
            key,
            seal_manifest=seal_manifest,
            source_assets=source_assets,
            template_replacements=template_replacements,
        )
        workflow._pending_template_replacements = {}
        workflow._pending_derived_assets = {}
        return path
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "templates").mkdir(exist_ok=True)
    for rel, data in source_assets.items():
        target_asset = _bv.bundle_asset_path(bundle, rel)
        if not target_asset.exists():
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            target_asset.write_bytes(data)
    # Restore any in-memory crops (from a prior encrypted load/save) to disk
    # as plaintext BEFORE the manifest hashes them; harmless no-op for a
    # freshly-compiled bundle whose crops are already on disk.
    workflow._sync_disk_templates(bundle)
    for rel, data in template_replacements.items():
        target_asset = _bv.bundle_asset_path(bundle, rel)
        target_asset.parent.mkdir(parents=True, exist_ok=True)
        target_asset.write_bytes(data)
    if workflow.schema_version < _ir.SCHEMA_VERSION:
        workflow.schema_version = _ir.SCHEMA_VERSION
    # Reflect the at-rest state in the workflow BEFORE the manifest is sealed,
    # so the sealed content digest (and the mirrored manifest.encrypted flag)
    # cover the true value and integrity re-verifies after a decrypt.
    workflow.encrypted = do_encrypt
    if seal_manifest:
        from openadapt_flow import bundle_validation as _bv

        workflow.manifest = _bv.build_manifest(workflow, bundle)
    serialized = workflow.model_dump_json(indent=2)
    resealed_payload = None
    if seal_manifest and workflow.manifest is not None:
        resealed_payload = _bv.verify_original_content(
            _ir.json.loads(serialized), workflow.manifest
        )
    plaintext_path = bundle / "workflow.json"
    encrypted_path = bundle / "workflow.json.enc"
    if do_encrypt:
        from openadapt_flow import crypto as _crypto

        encrypted_path.write_bytes(
            _crypto.encrypt_bytes(
                serialized.encode("utf-8"), key, aad=_crypto.BUNDLE_AAD
            )
        )
        # Never leave a stale plaintext copy alongside the ciphertext.
        if plaintext_path.exists():
            plaintext_path.unlink()
        # Seal the image crops too (the manifest already hashed their
        # plaintext just above), so no cleartext PHI-bearing screenshot is
        # left on disk. Caches the plaintext into ``_decrypted_templates``.
        _ir._seal_template_assets(bundle, key, workflow._decrypted_templates)
        path = encrypted_path
    else:
        # Bytes, not write_text: the platform default codec (cp1252 on
        # Windows) cannot encode every character OCR records, and text
        # mode would turn newlines into CRLF and change the bundle digest.
        plaintext_path.write_bytes(serialized.encode("utf-8"))
        if encrypted_path.exists():
            encrypted_path.unlink()
        path = plaintext_path
    if workflow.manifest is not None:
        (bundle / "manifest.json").write_bytes(
            workflow.manifest.model_dump_json(indent=2).encode("utf-8")
        )
    if resealed_payload is not None and workflow.manifest is not None:
        _bv.remember_original_content(
            workflow,
            resealed_payload,
            workflow.manifest,
            source_bundle_dir=bundle,
        )
    workflow._pending_template_replacements = {}
    workflow._pending_derived_assets = {}
    return path


def save_sealed_set(
    workflow: Workflow,
    bundle: Path,
    key: Optional[str],
    *,
    seal_manifest: bool,
    source_assets: Optional[dict[str, bytes]] = None,
    template_replacements: Optional[dict[str, bytes]] = None,
) -> Path:
    """Save into a sealed bundle (see :mod:`openadapt_flow.sealed_set`).

    Every asset, ``workflow.json`` and the manifest go through the set's
    writer, so no plaintext of this workflow touches the disk. Assets come
    from the in-memory crop map and from crops already sealed in the set.
    """
    from openadapt_flow import bundle_validation as _bv
    from openadapt_flow import ir as _ir
    from openadapt_flow import sealed_set as _sealed

    _ensure_output_layout(workflow, bundle, "sealed", template_replacements or {})
    writer = _sealed.active_writer(bundle)
    owned = writer is None
    if writer is None:
        passphrase = _sealed.passphrase_for("bundle", key)
        if not passphrase:
            raise _sealed.SealedSetKeyError(
                _sealed.missing_key_message(bundle, "bundle")
            )
        writer = _sealed.SealedSetWriter(bundle, "bundle", passphrase, resume=True)
    assets = {rel: writer.read_bytes(rel) for rel in writer.files("templates/")}
    accepted_asset_bytes: dict[str, set[bytes]] = {}
    for asset_map in (
        source_assets or {},
        workflow._decrypted_templates,
        template_replacements or {},
    ):
        for rel, data in asset_map.items():
            accepted_asset_bytes.setdefault(rel, set()).add(data)
    for rel, existing_bytes in assets.items():
        expected_bytes = accepted_asset_bytes.get(rel)
        if expected_bytes is not None and existing_bytes not in expected_bytes:
            raise _bv.BundleIntegrityError(
                f"save-as destination asset {rel!r} conflicts with verified source"
            )
    for rel, data in (source_assets or {}).items():
        if assets.get(rel) != data:
            writer.write(rel, data)
        assets[rel] = data
    for rel, data in workflow._decrypted_templates.items():
        if assets.get(rel) != data:
            writer.write(rel, data)
        assets[rel] = data
    for rel, data in (template_replacements or {}).items():
        if assets.get(rel) != data:
            writer.write(rel, data)
        assets[rel] = data
    if workflow.schema_version < _ir.SCHEMA_VERSION:
        workflow.schema_version = _ir.SCHEMA_VERSION
    workflow.encrypted = True
    if seal_manifest:
        from openadapt_flow import bundle_validation as _bv

        workflow.manifest = _bv.build_manifest(workflow, bundle, assets=assets)
    serialized = workflow.model_dump_json(indent=2).encode("utf-8")
    resealed_payload = None
    if seal_manifest and workflow.manifest is not None:
        from openadapt_flow import bundle_validation as _bv

        resealed_payload = _bv.verify_original_content(
            _ir.json.loads(serialized), workflow.manifest
        )
    writer.write("workflow.json", serialized)
    if workflow.manifest is not None:
        writer.write(
            "manifest.json", workflow.manifest.model_dump_json(indent=2).encode("utf-8")
        )
    workflow._decrypted_templates = dict(assets)
    if owned:
        writer.finalize()
    if resealed_payload is not None and workflow.manifest is not None:
        _bv.remember_original_content(
            workflow,
            resealed_payload,
            workflow.manifest,
            source_bundle_dir=bundle,
        )
    return bundle / ("workflow.json" + _sealed.SEALED_SUFFIX)
