"""Original-format verification is independent of current model serialization."""

from __future__ import annotations

import copy
import json

import pytest
from PIL import Image

from openadapt_flow import bundle_validation as bundles
from openadapt_flow import crypto, sealed_set
from openadapt_flow.bundle_sealing import seal_bundle
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow


def saved_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(bundle / "templates/button.png")
    workflow = Workflow(
        name="original",
        steps=[
            Step(
                id="click",
                intent="select",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/button.png",
                    region=(0, 0, 20, 20),
                    click_point=(10, 10),
                ),
            )
        ],
    )
    workflow.save(bundle)
    return bundle


@pytest.mark.parametrize("version", [0, -1, 3, True, False, "2", 2.0, None])
def test_unknown_or_malformed_schema_refuses_before_parsing(tmp_path, version):
    (tmp_path / "workflow.json").write_text(
        json.dumps({"schema_version": version, "name": "bad"})
    )
    with pytest.raises(bundles.BundleSchemaError):
        Workflow.load(tmp_path)


def test_upgrade_is_explicit_deterministic_and_does_not_mutate_source(monkeypatch):
    original = {"name": "legacy", "steps": [], "nested": {"schema_version": 77}}
    snapshot = copy.deepcopy(original)
    upgraded = bundles.migrate_bundle_dict(original)
    assert original == snapshot and upgraded["schema_version"] == 2
    assert upgraded["nested"]["schema_version"] == 77
    assert bundles.migrate_bundle_dict(original) == upgraded
    assert bundles.migrate_bundle_dict(upgraded) == upgraded
    monkeypatch.setattr(bundles, "_BUNDLE_UPGRADERS", {})
    with pytest.raises(bundles.BundleSchemaError, match="no upgrader"):
        bundles.migrate_bundle_dict(original)


@pytest.mark.parametrize(
    "asset", ["../outside.png", "/outside.png", "templates\\x.png"]
)
def test_manifest_asset_paths_cannot_escape_bundle(tmp_path, asset):
    with pytest.raises(bundles.BundleIntegrityError, match="unsafe bundle asset path"):
        bundles.bundle_asset_path(tmp_path / "bundle", asset)


def test_manifest_asset_paths_reject_symlink_escape(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (bundle / "templates").symlink_to(outside, target_is_directory=True)

    with pytest.raises(bundles.BundleIntegrityError, match="escapes its directory"):
        bundles.bundle_asset_path(bundle, "templates/crop.png")


def test_original_seal_is_checked_before_current_model_construction(
    tmp_path, monkeypatch
):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw["name"] = "changed"
    (bundle / "workflow.json").write_text(json.dumps(raw))

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "current Workflow constructed before original seal refusal"
        )

    monkeypatch.setattr(Workflow, "model_validate", forbidden)
    with pytest.raises(bundles.BundleIntegrityError, match="before migration"):
        Workflow.load(bundle)


def test_future_serializer_default_does_not_invalidate_original_seal(
    tmp_path, monkeypatch
):
    bundle = saved_bundle(tmp_path)
    original_bytes = (bundle / "workflow.json").read_bytes()
    expected = json.loads(original_bytes)["manifest"]["content_digest"]
    current = bundles._workflow_content

    def future_serializer(workflow):
        return {**current(workflow), "future_empty_default": None}

    monkeypatch.setattr(bundles, "_workflow_content", future_serializer)
    loaded = Workflow.load(bundle)
    assert loaded.manifest.content_digest == expected
    bundles.verify_integrity(loaded, bundle, loaded.manifest)
    assert (bundle / "workflow.json").read_bytes() == original_bytes
    loaded.name = "later mutation"
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.verify_integrity(loaded, bundle, loaded.manifest)


def test_source_projection_omits_only_the_format_owners():
    shadow = {
        "frame_path": [],
        "form_identity": None,
        "new_records": [],
        "identity_field": "id",
    }
    raw = {
        "schema_version": 2,
        "name": "owned",
        "steps": [
            {
                "anchor": {"form_identity": None, "structural": {"frame_path": []}},
                "api_binding": {
                    "body_template": {"nested": shadow},
                    "effects": [
                        {
                            "new_records": [],
                            "identity_field": "id",
                            "forbid_collateral_change": False,
                        }
                    ],
                },
            }
        ],
    }
    before = copy.deepcopy(raw)
    content = json.loads(bundles.source_canonical_payload(raw, {}))["workflow"]
    assert raw == before
    step = content["steps"][0]
    assert step["anchor"] == {"structural": {}}
    assert step["api_binding"]["effects"] == [{}]
    assert step["api_binding"]["body_template"]["nested"] == shadow


def test_decision_revalidation_uses_its_owned_predicate_projection():
    raw = {
        "schema_version": 2,
        "name": "decision",
        "program": {
            "states": {
                "choose": {
                    "decision": {
                        "revalidation": [
                            {
                                "anchor": {
                                    "form_identity": None,
                                    "structural": {"frame_path": []},
                                }
                            }
                        ]
                    }
                }
            }
        },
    }
    content = json.loads(bundles.source_canonical_payload(raw, {}))["workflow"]
    predicate = content["program"]["states"]["choose"]["decision"]["revalidation"][0]
    assert predicate["anchor"] == {"structural": {}}


@pytest.mark.parametrize(
    "mutation",
    ["name", "point", "hashes", "empty_digest", "digest", "schema", "encrypted"],
)
def test_loaded_integrity_catches_model_and_manifest_mutation(tmp_path, mutation):
    bundle = saved_bundle(tmp_path)
    loaded = Workflow.load(bundle)
    if mutation == "name":
        loaded.name = "changed"
    elif mutation == "point":
        loaded.steps[0].anchor.click_point = (11, 10)
    elif mutation == "hashes":
        loaded.manifest.file_hashes = {}
    elif mutation == "empty_digest":
        loaded.manifest.content_digest = ""
    elif mutation == "digest":
        loaded.manifest.content_digest = "0" * 64
    elif mutation == "schema":
        loaded.manifest.schema_version = 3
    else:
        loaded.manifest.encrypted = not loaded.manifest.encrypted
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.verify_integrity(loaded, bundle, loaded.manifest)


def test_loaded_asset_tamper_refuses(tmp_path):
    bundle = saved_bundle(tmp_path)
    loaded = Workflow.load(bundle)
    (bundle / "templates/button.png").write_bytes(b"changed synthetic asset")
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.verify_integrity(loaded, bundle, loaded.manifest)


def _bundle_files(bundle):
    return {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }


def test_verified_template_replacement_saves_plaintext_derivative(tmp_path):
    source = saved_bundle(tmp_path)
    original_asset = (source / "templates/button.png").read_bytes()
    replacement = b"replacement crop bytes"
    loaded = Workflow.load(source)
    derivative = loaded.model_copy()
    derivative.stage_verified_template_replacement("templates/button.png", replacement)

    assert (source / "templates/button.png").read_bytes() == original_asset
    assert "_pending_template_replacements" not in derivative.model_dump()
    derivative.save(tmp_path / "derivative")
    loaded.save(tmp_path / "unchanged")

    assert (source / "templates/button.png").read_bytes() == original_asset
    assert (tmp_path / "derivative/templates/button.png").read_bytes() == replacement
    assert (tmp_path / "unchanged/templates/button.png").read_bytes() == original_asset
    assert Workflow.load(tmp_path / "derivative").manifest.content_digest


def test_verified_template_replacement_saves_encrypted_derivative(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(crypto.ENV_KEY, "correct horse battery staple")
    source = saved_bundle(tmp_path)
    encrypted_source = tmp_path / "encrypted-source"
    seal_bundle(source, encrypted_source)
    source_snapshot = _bundle_files(encrypted_source)
    replacement = b"encrypted replacement crop bytes"
    loaded = Workflow.load(encrypted_source)
    loaded.stage_verified_template_replacement("templates/button.png", replacement)

    destination = tmp_path / "encrypted-derivative"
    with sealed_set.writing(destination, "bundle", "correct horse battery staple"):
        loaded.save(destination, key="correct horse battery staple")

    assert _bundle_files(encrypted_source) == source_snapshot
    assert not (destination / "templates/button.png").exists()
    sealed_set.open_set(destination, "correct horse battery staple").verify_all()
    reopened = Workflow.load(destination, key="correct horse battery staple")
    assert reopened.decrypted_template("templates/button.png") == replacement
    assert reopened.manifest.content_digest


def test_staged_template_replacement_refuses_changed_source(tmp_path):
    source = saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    loaded.stage_verified_template_replacement("templates/button.png", b"replacement")
    (source / "templates/button.png").write_bytes(b"tampered after staging")

    destination = tmp_path / "must-not-be-created"
    with pytest.raises(bundles.BundleIntegrityError):
        loaded.save(destination)
    assert not destination.exists()


@pytest.mark.parametrize("data", [7, "replacement", bytearray(b"replacement")])
def test_template_replacement_requires_bytes(tmp_path, data):
    source = saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    with pytest.raises(bundles.BundleIntegrityError, match="data must be bytes"):
        loaded.stage_verified_template_replacement("templates/button.png", data)


def test_template_replacement_refuses_save_without_new_seal(tmp_path):
    source = saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    loaded.stage_verified_template_replacement("templates/button.png", b"replacement")
    destination = tmp_path / "must-not-be-created"

    with pytest.raises(
        bundles.BundleIntegrityError, match="require a newly sealed manifest"
    ):
        loaded.save(destination, seal_manifest=False)
    assert not destination.exists()


@pytest.mark.parametrize(
    "rel", ["workflow.json", "templates/missing.png", "templates/../workflow.json"]
)
def test_template_replacement_requires_existing_safe_template(tmp_path, rel):
    source = saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    with pytest.raises(bundles.BundleIntegrityError):
        loaded.stage_verified_template_replacement(rel, b"replacement")


def test_template_replacement_refuses_new_and_unchecked_workflows(tmp_path):
    source = saved_bundle(tmp_path)
    with pytest.raises(bundles.BundleIntegrityError):
        Workflow(name="new", steps=[]).stage_verified_template_replacement(
            "templates/button.png", b"replacement"
        )
    unchecked = Workflow.load(source, verify_integrity=False)
    with pytest.raises(bundles.BundleIntegrityError):
        unchecked.stage_verified_template_replacement(
            "templates/button.png", b"replacement"
        )


def test_loaded_serialization_requires_explicit_reseal(tmp_path):
    bundle = saved_bundle(tmp_path)
    loaded = Workflow.load(bundle)
    original = (bundle / "workflow.json").read_bytes()
    loaded.name = "approved new content"
    with pytest.raises(bundles.BundleIntegrityError, match="explicit resealing"):
        loaded.save(bundle, seal_manifest=False)
    assert (bundle / "workflow.json").read_bytes() == original
    loaded.save(bundle)
    bundles.verify_integrity(loaded, bundle, loaded.manifest)
    assert Workflow.load(bundle).name == loaded.name


def test_unknown_manifest_version_refuses(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw["manifest"]["schema_version"] = 3
    (bundle / "workflow.json").write_text(json.dumps(raw))
    with pytest.raises(bundles.BundleSchemaError):
        Workflow.load(bundle)


def test_unknown_unsealed_extension_cannot_hide_in_current_model_defaults(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw["future_behavior"] = "changed"
    (bundle / "workflow.json").write_text(json.dumps(raw))
    with pytest.raises(bundles.BundleIntegrityError, match="before migration"):
        Workflow.load(bundle)


def test_unverified_load_cannot_launder_a_broken_original_seal(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw["name"] = "tampered before unchecked load"
    (bundle / "workflow.json").write_text(json.dumps(raw))
    loaded = Workflow.load(bundle, verify_integrity=False)
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.verify_integrity(loaded, bundle, loaded.manifest)
    with pytest.raises(bundles.BundleIntegrityError, match="unchecked bundle load"):
        loaded.save(bundle)
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.content_identity_digest(loaded, loaded.manifest.file_hashes)


@pytest.mark.parametrize("attack", ["workflow", "asset"])
@pytest.mark.parametrize("copy_loaded", [False, True])
def test_unverified_load_cannot_reseal_tampered_source(tmp_path, attack, copy_loaded):
    bundle = saved_bundle(tmp_path)
    if attack == "workflow":
        raw = json.loads((bundle / "workflow.json").read_bytes())
        raw["name"] = "tampered before unchecked load"
        (bundle / "workflow.json").write_text(json.dumps(raw))
    else:
        (bundle / "templates/button.png").write_bytes(b"tampered asset")
    before = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }

    loaded = Workflow.load(bundle, verify_integrity=False)
    candidate = loaded.model_copy(deep=True) if copy_loaded else loaded
    with pytest.raises(bundles.BundleIntegrityError):
        candidate.save(bundle)

    after = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_authorization_summary_refuses_manifestless_bundle(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw.pop("manifest")
    (bundle / "workflow.json").write_text(json.dumps(raw))
    (bundle / "manifest.json").unlink()

    from openadapt_flow.authorization_summary import bundle_digest

    with pytest.raises(
        bundles.BundleIntegrityError, match="persisted bundle integrity seal"
    ):
        bundle_digest(bundle)


def test_missing_seal_cannot_be_migrated_by_loading_and_resaving(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw.pop("manifest")
    (bundle / "workflow.json").write_text(json.dumps(raw))
    (bundle / "manifest.json").unlink()

    with pytest.raises(
        bundles.BundleIntegrityError, match="no persisted integrity seal"
    ):
        Workflow.load(bundle)


def test_model_validate_dump_does_not_recover_verified_load_capability(tmp_path):
    bundle = saved_bundle(tmp_path)
    loaded = Workflow.load(bundle)
    clone = Workflow.model_validate(loaded.model_dump())

    with pytest.raises(bundles.BundleIntegrityError, match="non-serializable"):
        clone.save(bundle)


def test_authorization_display_paths_verify_bundle_before_reading(tmp_path):
    bundle = saved_bundle(tmp_path)
    raw = json.loads((bundle / "workflow.json").read_bytes())
    raw["name"] = "tampered display input"
    (bundle / "workflow.json").write_text(json.dumps(raw))

    from openadapt_flow.authorization_summary import _covered_by_digest, outline

    with pytest.raises(bundles.BundleIntegrityError):
        outline(bundle)
    report = bundle / "QUALIFICATION.md"
    report.write_text("synthetic report")
    assert not _covered_by_digest(bundle, report)


def test_verified_load_can_save_as_and_carries_its_assets(tmp_path):
    source = saved_bundle(tmp_path)
    loaded = Workflow.load(source)
    target = tmp_path / "save-as"

    loaded.save(target)

    assert (target / "templates/button.png").read_bytes() == (
        source / "templates/button.png"
    ).read_bytes()
    reloaded = Workflow.load(target)
    bundles.verify_integrity(reloaded, target, reloaded.manifest)


def test_loaded_identity_refuses_replaced_asset_hashes_or_missing_manifest(tmp_path):
    bundle = saved_bundle(tmp_path)
    loaded = Workflow.load(bundle)
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.content_identity_digest(loaded, {})
    loaded.manifest = None
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.content_identity_digest(loaded, {})


@pytest.mark.parametrize("consumer", ["pipeline", "summary", "composition"])
def test_digest_consumers_retain_verified_original_identity(
    tmp_path, monkeypatch, consumer
):
    bundle = saved_bundle(tmp_path)
    expected = json.loads((bundle / "workflow.json").read_bytes())["manifest"][
        "content_digest"
    ]
    current = bundles._workflow_content
    monkeypatch.setattr(
        bundles,
        "_workflow_content",
        lambda workflow: {
            **current(workflow),
            "future_empty_default": None,
        },
    )
    if consumer == "pipeline":
        from openadapt_flow.admit_pipeline import _bundle_digest

        actual = _bundle_digest(bundle)
    elif consumer == "summary":
        from openadapt_flow.authorization_summary import bundle_digest

        actual = bundle_digest(bundle)
    else:
        from openadapt_flow.admitted_composition import live_bundle_content_digest

        actual = live_bundle_content_digest(Workflow.load(bundle), bundle)
    assert actual == expected


def test_governed_identity_retains_original_seal_and_refuses_later_mutation(
    tmp_path, monkeypatch
):
    from openadapt_flow.run_gate import build_runtime_authorization
    from tests.test_run_gate import _KEY, _good_workflow, _run, _seal

    _, bundle = _seal(_good_workflow(), tmp_path)
    current = bundles._workflow_content
    monkeypatch.setattr(
        bundles,
        "_workflow_content",
        lambda workflow: {
            **current(workflow),
            "future_empty_default": None,
        },
    )
    loaded = Workflow.load(bundle, key=_KEY)
    report = _run(loaded, bundle)
    assert report.passed, report.render()
    authorization = build_runtime_authorization(loaded, report)
    assert authorization.validate_workflow(loaded) is None
    loaded.name = "later unauthorized change"
    assert authorization.validate_workflow(loaded) is not None
    with pytest.raises(ValueError, match="workflow changed"):
        build_runtime_authorization(loaded, report)
