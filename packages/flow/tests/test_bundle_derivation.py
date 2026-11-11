"""Verified derivation preserves source assets and stages explicit edits."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow import bundle_validation as bundles
from openadapt_flow import crypto, ir, sealed_set
from openadapt_flow.bundle_persistence import prepare_derived_assets
from openadapt_flow.bundle_sealing import seal_bundle
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.runtime.heal import write_healed_bundle
from tests import test_bundle_original_format as controls

ORIGINAL = "templates/button.png"
ADDED = "templates/added.png"
OUTSIDE = "assets/inserted.png"
KEY = "synthetic verified derivative test key"
BUILDERS = ("copy", "validated", "authoring")


def _clone(source: Workflow, builder: str) -> Workflow:
    if builder == "copy":
        return source.model_copy(deep=True)
    if builder == "validated":
        return bundles.derive_verified_workflow(source, update={"name": "derived"})
    return bundles.validate_verified_derivative(
        source, Workflow, ValueError, "the synthetic authoring operation"
    )


def _add_reference(workflow: Workflow, rel: str) -> None:
    workflow.steps.append(
        Step(
            id="added",
            intent="select a declared second target",
            action=ActionKind.CLICK,
            anchor=Anchor(template=rel, region=(0, 0, 20, 20), click_point=(10, 10)),
        )
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "fault",
    (
        "none",
        "foreign",
        "tampered",
        "late-tamper",
        "raw-clone",
        "unchecked",
        "unreferenced",
        "unsafe-name",
    ),
)
@given(
    builder=st.sampled_from(BUILDERS),
    rel=st.sampled_from((ORIGINAL, ADDED, OUTSIDE)),
    data=st.binary(min_size=1, max_size=48),
)
def test_derivative_asset_model(fault, builder, rel, data):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = controls.saved_bundle(root)
        loaded = Workflow.load(source)
        candidate = _clone(loaded, builder)
        if rel != ORIGINAL:
            _add_reference(candidate, rel)
        destination = root / "derived"
        changes = {rel: data}
        if fault == "foreign":
            target = destination / rel
            target.parent.mkdir(parents=True)
            target.write_bytes(b"foreign:" + data)
        elif fault == "tampered":
            (source / ORIGINAL).write_bytes(b"changed source after load")
        elif fault == "raw-clone":
            candidate = Workflow.model_validate(candidate.model_dump())
            assert not bundles.has_verified_load_capability(candidate)
        elif fault == "unchecked":
            candidate = Workflow.load(source, verify_integrity=False)
            if rel != ORIGINAL:
                _add_reference(candidate, rel)
            assert not bundles.has_verified_load_capability(candidate)
        elif fault == "unreferenced":
            changes = {"templates/unreferenced.png": data}
        elif fault == "unsafe-name":
            changes = {"templates/../outside.png": data}
        source_before = controls._bundle_files(source)
        destination_before = controls._bundle_files(destination)
        if fault not in {"none", "late-tamper"}:
            with pytest.raises(bundles.BundleIntegrityError):
                prepare_derived_assets(candidate, destination, changes)
            assert controls._bundle_files(source) == source_before
            assert controls._bundle_files(destination) == destination_before
            assert destination.exists() is (fault == "foreign")
            return
        prepare_derived_assets(candidate, destination, changes)
        assert not destination.exists()
        assert "_pending_derived_assets" not in candidate.model_dump()
        # Revalidation after staging must carry intent and the same original
        # source, not just a token or an empty fresh asset cache.
        candidate = _clone(candidate, "validated")
        if fault == "late-tamper":
            (source / ORIGINAL).write_bytes(b"changed source after staging")
            with pytest.raises(bundles.BundleIntegrityError):
                candidate.save(destination)
            assert not destination.exists()
            assert (source / ORIGINAL).read_bytes() == b"changed source after staging"
            return
        candidate.save(destination)
        reopened = Workflow.load(destination)
        assert reopened.manifest is not None
        assert (destination / rel).read_bytes() == data
        assert reopened.manifest.file_hashes[rel] == _digest(data)
        assert controls._bundle_files(source) == source_before
        if rel != ORIGINAL:
            assert (destination / ORIGINAL).read_bytes() == (source / ORIGINAL).read_bytes()


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("storage", ("plain", "encrypted", "sealed"))
@pytest.mark.parametrize("in_place", (False, True))
def test_verified_source_and_staged_assets_survive_each_save_layout(
    tmp_path, monkeypatch, builder, storage, in_place
):
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    plain = controls.saved_bundle(tmp_path / "source")
    source = plain
    if storage == "encrypted":
        source = tmp_path / "encrypted-source"
        Workflow.load(plain).save(source, encrypt=True, key=KEY)
    elif storage == "sealed":
        source = tmp_path / "sealed-source"
        seal_bundle(plain, source)
    loaded = Workflow.load(source, key=KEY if storage != "plain" else None)
    original = controls._bundle_files(source)
    candidate = _clone(loaded, builder)
    _add_reference(candidate, ADDED)
    destination = source if in_place else tmp_path / "destination"
    changes = {ORIGINAL: b"approved replacement", ADDED: b"approved addition"}
    prepare_derived_assets(candidate, destination, changes, output=storage)
    assert controls._bundle_files(source) == original
    if storage == "sealed" and not in_place:
        with sealed_set.writing(destination, "bundle", KEY):
            candidate.save(destination)
    else:
        candidate.save(destination, encrypt=storage == "encrypted", key=KEY if storage == "encrypted" else None)
    reopened = Workflow.load(destination, key=KEY if storage != "plain" else None)
    assert reopened.manifest is not None
    for rel, data in changes.items():
        assert reopened.manifest.file_hashes[rel] == _digest(data)
        if storage == "plain":
            assert (destination / rel).read_bytes() == data
        else:
            assert reopened.decrypted_template(rel) == data
            assert not (destination / rel).exists()
    if not in_place:
        assert controls._bundle_files(source) == original


@pytest.mark.parametrize("output", ("encrypted", "sealed"))
def test_unsupported_crypto_layout_refuses_before_destination_effects(tmp_path, output):
    source = controls.saved_bundle(tmp_path)
    workflow = Workflow.load(source)
    _add_reference(workflow, OUTSIDE)
    destination = tmp_path / "unsupported"
    before = controls._bundle_files(source)
    with pytest.raises(bundles.BundleIntegrityError, match="under templates/"):
        prepare_derived_assets(workflow, destination, {OUTSIDE: b"asset"}, output=output)
    assert not destination.exists()
    assert controls._bundle_files(source) == before
    if output == "encrypted":
        prepare_derived_assets(workflow, destination, {OUTSIDE: b"asset"})
        with pytest.raises(bundles.BundleIntegrityError, match="under templates/"):
            workflow.save(destination, encrypt=True, key=KEY)
        assert not destination.exists()


def test_healer_never_copies_over_a_foreign_destination(tmp_path):
    source = controls.saved_bundle(tmp_path / "original")
    destination = controls.saved_bundle(tmp_path / "foreign", template_color="black")
    workflow = Workflow.load(source)
    source_before = controls._bundle_files(source)
    destination_before = controls._bundle_files(destination)
    with pytest.raises(bundles.BundleIntegrityError, match="conflicts with verified source"):
        write_healed_bundle(workflow, source, destination, {"click": b"healed"})
    assert controls._bundle_files(source) == source_before
    assert controls._bundle_files(destination) == destination_before


def test_fresh_staging_does_not_invent_verified_authority(tmp_path):
    workflow = Workflow(name="fresh", steps=[])
    _add_reference(workflow, ADDED)
    destination = tmp_path / "fresh"
    prepare_derived_assets(workflow, destination, {ADDED: b"fresh bytes"})
    assert not bundles.has_verified_load_capability(workflow)
    assert not destination.exists()
    workflow.save(destination)
    reopened = Workflow.load(destination)
    assert bundles.has_verified_load_capability(reopened)
    assert (destination / ADDED).read_bytes() == b"fresh bytes"


@pytest.mark.parametrize("legacy_last", (False, True))
def test_latest_explicit_staging_intent_survives_other_staging_api(tmp_path, legacy_last):
    source = controls.saved_bundle(tmp_path)
    workflow = Workflow.load(source)
    destination = tmp_path / "restaged"
    before = controls._bundle_files(source)
    if legacy_last:
        prepare_derived_assets(workflow, destination, {ORIGINAL: b"earlier"})
        workflow.stage_verified_template_replacement(ORIGINAL, b"latest")
    else:
        workflow.stage_verified_template_replacement(ORIGINAL, b"earlier")
        prepare_derived_assets(workflow, destination, {ORIGINAL: b"latest"})
    workflow.save(destination)
    assert (destination / ORIGINAL).read_bytes() == b"latest"
    assert controls._bundle_files(source) == before


@pytest.mark.parametrize("operation", ("model-edit", "asset-edit"))
def test_verified_source_context_does_not_carry_a_stale_certification_flag(tmp_path, operation):
    source = controls.saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    assert loaded.manifest is not None
    # Deliberately plant stale metadata, not a real certificate or approval.
    loaded.manifest.provenance.certified = True
    destination = tmp_path / "derived"
    if operation == "model-edit":
        candidate = bundles.derive_verified_workflow(loaded, update={"name": "derived"})
    else:
        candidate = loaded.model_copy(deep=True)
        prepare_derived_assets(candidate, destination, {ORIGINAL: b"changed"})
    assert candidate.manifest is not None and not candidate.manifest.provenance.certified
    assert loaded.manifest.provenance.certified
    candidate.save(destination)
    reopened = Workflow.load(destination)
    assert reopened.manifest is not None and not reopened.manifest.provenance.certified


@pytest.mark.parametrize("storage", ("encrypted", "sealed"))
def test_verified_asset_cache_requires_manifest_membership(tmp_path, monkeypatch, storage):
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    plain = controls.saved_bundle(tmp_path / "original")
    source = tmp_path / "protected"
    if storage == "encrypted":
        Workflow.load(plain).save(source, encrypt=True, key=KEY)
    else:
        seal_bundle(plain, source)
    loaded = Workflow.load(source, key=KEY)
    manifest = loaded.manifest
    assert manifest is not None
    original_cache = loaded.decrypted_templates()
    assert set(original_cache) == set(manifest.file_hashes)
    assert manifest.file_hashes[ORIGINAL] == _digest(original_cache[ORIGINAL])
    original_files = controls._bundle_files(source)

    positive = tmp_path / "positive"
    loaded.model_copy(deep=True).save(positive)
    assert (positive / ORIGINAL).read_bytes() == original_cache[ORIGINAL]

    # Model an extra cached name without changing either native storage format.
    extra = "templates/unlisted.png"
    assert extra not in manifest.file_hashes
    bad_cache = {**original_cache, extra: b"synthetic extra asset"}
    with pytest.raises(bundles.BundleIntegrityError, match="outside the verified manifest"):
        ir._verify_sealed_template_integrity(loaded, source, manifest, bad_cache)
    candidate = loaded.model_copy(deep=True)
    candidate._decrypted_templates = bad_cache
    destination = tmp_path / "refused"
    with pytest.raises(bundles.BundleIntegrityError, match="outside the verified manifest"):
        candidate.save(destination)
    assert not destination.exists()
    assert loaded.decrypted_templates() == original_cache
    assert controls._bundle_files(source) == original_files
