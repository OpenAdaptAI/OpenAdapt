from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_admission_contract.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("release_admission_contract", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _artifact_inventory() -> dict:
    artifacts = [
        {
            "name": "openadapt-2.0.0.tar.gz",
            "kind": "python-sdist",
            "sha256": "sha256:" + "1" * 64,
            "size_bytes": 101,
            "media_type": "application/gzip",
            "publish_destinations": ["github-release", "pypi"],
        },
        {
            "name": "openadapt-2.0.0-py3-none-any.whl",
            "kind": "python-wheel",
            "sha256": "sha256:" + "2" * 64,
            "size_bytes": 202,
            "media_type": "application/zip",
            "publish_destinations": ["github-release", "pypi"],
        },
    ]
    return {
        "schema_version": MODULE.INVENTORY_SCHEMA,
        "target": MODULE.TARGET,
        "claim_scope": MODULE.CLAIM_SCOPE,
        "artifacts": artifacts,
    }


def _admission_reference() -> dict:
    return {
        "schema_version": MODULE.REFERENCE_SCHEMA,
        "repository": "OpenAdaptAI/.github",
        "repository_id": "858454062",
        "repository_owner_id": "132681217",
        "registry_source_commit": "a" * 40,
        "registry_revision": 7,
        "registry_head_sha256": "sha256:" + "b" * 64,
        "registry_entry_sha256": "sha256:" + "c" * 64,
        "kind": "qualification-release",
        "object_schema_version": "openadapt.qualification-release/v1",
        "object_path": (
            "production-evidence/objects/sha256/dd/"
            + "d" * 64
            + ".qualification-release.json"
        ),
        "object_sha256": "sha256:" + "d" * 64,
        "size_bytes": 2048,
        "object_media_type": (
            "application/vnd.openadapt.qualification-release+json;version=1"
        ),
        "semantic_identity_sha256": "sha256:" + "e" * 64,
        "subject_sha256": None,
    }


def test_inventory_digest_uses_the_closed_domain_and_subject() -> None:
    inventory = _artifact_inventory()
    expected_subject = {
        "target": inventory["target"],
        "claim_scope": inventory["claim_scope"],
        "artifacts": inventory["artifacts"],
    }
    expected = (
        "sha256:"
        + hashlib.sha256(
            MODULE.INVENTORY_DOMAIN + MODULE.canonical_json(expected_subject)
        ).hexdigest()
    )

    assert MODULE.artifact_inventory_sha256(inventory) == expected


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extra",), True),
        (("target",), "capture"),
        (("artifacts", 0, "kind"), "wheel"),
        (("artifacts", 0, "sha256"), "not-a-digest"),
        (("artifacts", 0, "size_bytes"), True),
    ],
)
def test_inventory_refuses_open_or_invalid_values(path: tuple, value: object) -> None:
    inventory = _artifact_inventory()
    current: object = inventory
    for key in path[:-1]:
        current = current[key]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValueError):
        MODULE.validate_artifact_inventory(inventory)


def test_inventory_requires_canonical_artifact_order() -> None:
    inventory = _artifact_inventory()
    inventory["artifacts"].reverse()

    with pytest.raises(ValueError, match="canonically sorted"):
        MODULE.validate_artifact_inventory(inventory)


def test_tag_binding_is_one_closed_canonical_json_object() -> None:
    reference = _admission_reference()
    inventory = _artifact_inventory()
    inventory_sha256 = MODULE.artifact_inventory_sha256(inventory)

    binding = MODULE.build_tag_binding(reference, inventory, inventory_sha256)
    encoded = MODULE.canonical_json(binding)

    assert json.loads(encoded) == binding
    assert set(binding) == MODULE.TAG_BINDING_KEYS
    assert binding["admission_reference"] == reference
    assert (
        binding["admission_reference_sha256"]
        == "sha256:"
        + hashlib.sha256(
            MODULE.REFERENCE_DOMAIN + MODULE.canonical_json(reference)
        ).hexdigest()
    )
    assert binding["artifact_inventory_sha256"] == inventory_sha256
    assert MODULE.validate_tag_binding(binding) == binding
    assert MODULE.tag_binding_bytes(binding) == encoded + b"\n"
    assert not MODULE.tag_binding_bytes(binding).endswith(b"\n\n")


def test_tag_binding_cli_emits_exact_canonical_bytes_plus_one_lf() -> None:
    reference = _admission_reference()
    inventory = _artifact_inventory()
    inventory_sha256 = MODULE.artifact_inventory_sha256(inventory)
    expected = MODULE.tag_binding_bytes(
        MODULE.build_tag_binding(reference, inventory, inventory_sha256)
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "tag-binding",
            "--admission-reference-json",
            MODULE.canonical_json(reference).decode(),
            "--artifact-inventory-json",
            MODULE.canonical_json(inventory).decode(),
            "--verified-artifact-inventory-sha256",
            inventory_sha256,
        ],
        check=True,
        capture_output=True,
    )

    assert completed.stdout == expected
    assert completed.stderr == b""


def test_tag_binding_requires_the_central_verified_inventory_digest() -> None:
    with pytest.raises(ValueError, match="does not match"):
        MODULE.build_tag_binding(
            _admission_reference(),
            _artifact_inventory(),
            "sha256:" + "0" * 64,
        )


@pytest.mark.parametrize(
    "mutation",
    ["binding-key", "reference-key", "reference-digest", "inventory-digest"],
)
def test_tag_binding_refuses_open_or_changed_values(mutation: str) -> None:
    inventory = _artifact_inventory()
    binding = MODULE.build_tag_binding(
        _admission_reference(),
        inventory,
        MODULE.artifact_inventory_sha256(inventory),
    )
    if mutation == "binding-key":
        binding["extra"] = True
    elif mutation == "reference-key":
        binding["admission_reference"]["extra"] = True
    elif mutation == "reference-digest":
        binding["admission_reference_sha256"] = "sha256:" + "0" * 64
    else:
        binding["artifact_inventory_sha256"] = "invalid"

    with pytest.raises(ValueError):
        MODULE.validate_tag_binding(binding)


def test_admission_reference_rejects_bool_identifiers_and_invalid_hashes() -> None:
    for key, value in (
        ("schema_version", "openadapt.production-evidence-reference/v2"),
        ("repository_id", True),
        ("registry_revision", False),
        ("object_sha256", "0" * 40),
        ("subject_sha256", "sha256:" + "f" * 64),
    ):
        reference = copy.deepcopy(_admission_reference())
        reference[key] = value
        with pytest.raises(ValueError):
            MODULE.validate_admission_reference(reference)


def _tag_binding() -> dict:
    inventory = _artifact_inventory()
    return MODULE.build_tag_binding(
        _admission_reference(),
        inventory,
        MODULE.artifact_inventory_sha256(inventory),
    )


def _tag_object(*, message: str | None = None) -> dict:
    return {
        "sha": "9" * 40,
        "tag": "v2.0.0",
        "message": message
        if message is not None
        else MODULE.tag_binding_bytes(_tag_binding()).decode(),
        "object": {"type": "commit", "sha": "8" * 40},
        "tagger": {
            "name": "openadapt-release[bot]",
            "email": "openadapt-release[bot]@users.noreply.github.com",
            "date": "2026-08-27T12:00:00Z",
        },
    }


def test_tag_object_requires_one_canonical_binding_and_exact_commit() -> None:
    tag_object_sha, binding = MODULE.validate_tag_object(
        _tag_object(),
        expected_tag="v2.0.0",
        expected_commit="8" * 40,
    )

    assert tag_object_sha == "9" * 40
    assert binding == _tag_binding()


@pytest.mark.parametrize(
    "mutation",
    [
        "lightweight",
        "commit",
        "tag",
        "message",
        "space",
        "zero-lf",
        "extra-lf",
        "prefix",
        "suffix",
    ],
)
def test_tag_object_refuses_changed_or_noncanonical_values(mutation: str) -> None:
    document = _tag_object()
    if mutation == "lightweight":
        document["sha"] = "invalid"
    elif mutation == "commit":
        document["object"]["sha"] = "7" * 40
    elif mutation == "tag":
        document["tag"] = "v2.0.1"
    elif mutation == "message":
        document["message"] = "{}\n"
    elif mutation == "space":
        document["message"] = json.dumps(_tag_binding(), sort_keys=True) + "\n"
    elif mutation == "zero-lf":
        document["message"] = MODULE.canonical_json(_tag_binding()).decode()
    elif mutation == "extra-lf":
        document["message"] = MODULE.tag_binding_bytes(_tag_binding()).decode() + "\n"
    elif mutation == "prefix":
        document["message"] = " " + MODULE.tag_binding_bytes(_tag_binding()).decode()
    else:
        document["message"] = MODULE.canonical_json(_tag_binding()).decode() + " \n"

    with pytest.raises(ValueError):
        MODULE.validate_tag_object(
            document,
            expected_tag="v2.0.0",
            expected_commit="8" * 40,
        )


def test_tag_ref_requires_the_exact_annotated_tag_object() -> None:
    document = {
        "ref": "refs/tags/v2.0.0",
        "object": {"type": "tag", "sha": "9" * 40},
    }
    MODULE.validate_tag_ref(
        document,
        expected_tag="v2.0.0",
        expected_tag_object_sha="9" * 40,
    )

    document["object"]["type"] = "commit"
    with pytest.raises(ValueError, match="annotated"):
        MODULE.validate_tag_ref(
            document,
            expected_tag="v2.0.0",
            expected_tag_object_sha="9" * 40,
        )


def _tag_ruleset(*, rules: list[dict], bypass_actors: list[dict]) -> dict:
    return {
        "id": 123,
        "name": MODULE.IMMUTABLE_TAG_RULESET_NAME,
        "target": "tag",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["refs/tags/v*"], "exclude": []},
        },
        "bypass_actors": bypass_actors,
        "rules": rules,
    }


def _tag_rulesets() -> tuple[dict, dict]:
    immutable = _tag_ruleset(
        rules=[
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "update",
                "parameters": {"update_allows_fetch_and_merge": False},
            },
        ],
        bypass_actors=[],
    )
    creation = _tag_ruleset(
        rules=[{"type": "creation"}],
        bypass_actors=[
            {
                "actor_id": MODULE.RELEASE_APP_ID,
                "actor_type": "Integration",
                "bypass_mode": "always",
            }
        ],
    )
    creation["id"] = 456
    creation["name"] = MODULE.CREATION_TAG_RULESET_NAME
    return immutable, creation


def test_two_rulesets_allow_app_creation_but_no_update_or_delete_bypass() -> None:
    assert MODULE.validate_tag_rulesets(*_tag_rulesets(), expected_tag="v2.0.0") == (
        123,
        456,
    )


def test_ruleset_index_selects_one_exact_active_ruleset_of_each_name() -> None:
    immutable, creation = _tag_rulesets()
    assert MODULE.select_tag_ruleset_ids([immutable, creation]) == (123, 456)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "inactive", "name"])
def test_ruleset_index_refuses_an_ambiguous_or_absent_contract(mutation: str) -> None:
    immutable, creation = _tag_rulesets()
    documents = [immutable, creation]
    if mutation == "missing":
        documents.pop()
    elif mutation == "duplicate":
        duplicate = copy.deepcopy(creation)
        duplicate["id"] = 789
        documents.append(duplicate)
    elif mutation == "inactive":
        creation["enforcement"] = "evaluate"
    else:
        creation["name"] = "other"

    with pytest.raises(ValueError):
        MODULE.select_tag_ruleset_ids(documents)


@pytest.mark.parametrize(
    "mutation",
    [
        "inactive",
        "branch",
        "exclude",
        "extra-condition",
        "human",
        "second",
        "update",
        "immutable-bypass",
        "same",
    ],
)
def test_tag_ruleset_refuses_an_open_or_incomplete_boundary(mutation: str) -> None:
    immutable, creation = _tag_rulesets()
    if mutation == "inactive":
        immutable["enforcement"] = "evaluate"
    elif mutation == "branch":
        immutable["target"] = "branch"
    elif mutation == "exclude":
        immutable["conditions"]["ref_name"]["exclude"] = ["refs/tags/v2*"]
    elif mutation == "extra-condition":
        immutable["conditions"]["repository_name"] = {"include": ["~ALL"]}
    elif mutation == "human":
        creation["bypass_actors"][0] = {
            "actor_id": 5,
            "actor_type": "User",
            "bypass_mode": "always",
        }
    elif mutation == "second":
        creation["bypass_actors"].append(
            {"actor_id": 5, "actor_type": "User", "bypass_mode": "always"}
        )
    elif mutation == "update":
        immutable["rules"] = [{"type": "deletion"}]
    elif mutation == "immutable-bypass":
        immutable["bypass_actors"] = creation["bypass_actors"]
    else:
        creation["id"] = immutable["id"]

    with pytest.raises(ValueError):
        MODULE.validate_tag_rulesets(immutable, creation, expected_tag="v2.0.0")


def _normalized_rulesets() -> list[dict]:
    return [
        {
            "schema_version": MODULE.TAG_RULESET_SCHEMA,
            "role": "creation_authority",
            "repository": MODULE.SOURCE_REPOSITORY,
            "repository_id": str(MODULE.SOURCE_REPOSITORY_ID),
            "ruleset_id": "456",
            "name": MODULE.CREATION_TAG_RULESET_NAME,
            "target": "tag",
            "enforcement": "active",
            "bypass_actors": [
                {
                    "actor_id": str(MODULE.RELEASE_APP_ID),
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "creation"}],
        },
        {
            "schema_version": MODULE.TAG_RULESET_SCHEMA,
            "role": "immutability",
            "repository": MODULE.SOURCE_REPOSITORY,
            "repository_id": str(MODULE.SOURCE_REPOSITORY_ID),
            "ruleset_id": "123",
            "name": MODULE.IMMUTABLE_TAG_RULESET_NAME,
            "target": "tag",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "update",
                    "parameters": {"update_allows_fetch_and_merge": False},
                },
            ],
        },
    ]


def _publication_staging() -> dict:
    inventory = _artifact_inventory()
    assets = []
    for index, artifact in enumerate(inventory["artifacts"], start=1001):
        assets.append(
            {
                "asset_id": str(index),
                **artifact,
                "uploader_id": str(MODULE.RELEASE_APP_BOT_ID),
                "uploader_login": "openadapt-release[bot]",
            }
        )
    assets.sort(key=lambda item: (item["name"], item["asset_id"]))
    rulesets = _normalized_rulesets()
    return {
        "schema_version": MODULE.STAGING_SCHEMA,
        "repository": MODULE.SOURCE_REPOSITORY,
        "repository_id": str(MODULE.SOURCE_REPOSITORY_ID),
        "draft_release_id": "9001",
        "tag": "v2.0.0",
        "tag_ref_state": {"ref": "refs/tags/v2.0.0", "exists": False},
        "tag_ref_state_sha256": MODULE._digest(
            {"ref": "refs/tags/v2.0.0", "exists": False},
            MODULE.TAG_REF_STATE_DOMAIN,
        ),
        "target_commitish": "8" * 40,
        "draft": True,
        "prerelease": False,
        "release_app_id": str(MODULE.RELEASE_APP_ID),
        "release_app_installation_id": str(MODULE.RELEASE_APP_INSTALLATION_ID),
        "release_app_bot_user_id": str(MODULE.RELEASE_APP_BOT_ID),
        "release_author_login": "openadapt-release[bot]",
        "assets": assets,
        "immutable_releases": {"enabled": True, "enforced_by_owner": False},
        "immutable_releases_sha256": MODULE._digest(
            {"enabled": True, "enforced_by_owner": False},
            MODULE.IMMUTABLE_RELEASES_DOMAIN,
        ),
        "tag_rulesets": rulesets,
        "tag_rulesets_sha256": MODULE._digest(rulesets, MODULE.TAG_RULESETS_DOMAIN),
        "observed_at": "2026-08-27T12:00:00Z",
    }


def test_publication_staging_binds_exact_draft_assets_and_controls() -> None:
    staging = _publication_staging()
    digest = MODULE._digest(staging, MODULE.STAGING_DOMAIN)

    assert (
        MODULE.validate_publication_staging(
            staging,
            artifact_inventory=_artifact_inventory(),
            verified_staging_sha256=digest,
            expected_tag="v2.0.0",
            expected_commit="8" * 40,
        )
        == staging
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "release-id",
        "asset-id",
        "asset-bytes",
        "asset-uploader",
        "ruleset-name",
        "ruleset-digest",
        "immutable-setting",
        "immutable-digest",
        "tag-ref",
        "tag-ref-digest",
        "source",
        "state",
        "digest",
    ],
)
def test_publication_staging_refuses_changed_bound_state(mutation: str) -> None:
    staging = _publication_staging()
    digest = MODULE._digest(staging, MODULE.STAGING_DOMAIN)
    if mutation == "release-id":
        staging["draft_release_id"] = "0"
    elif mutation == "asset-id":
        staging["assets"][0]["asset_id"] = "0"
    elif mutation == "asset-bytes":
        staging["assets"][0]["sha256"] = "sha256:" + "0" * 64
    elif mutation == "asset-uploader":
        staging["assets"][0]["uploader_id"] = "774615"
    elif mutation == "ruleset-name":
        staging["tag_rulesets"][0]["name"] = "other"
    elif mutation == "ruleset-digest":
        staging["tag_rulesets_sha256"] = "sha256:" + "0" * 64
    elif mutation == "immutable-setting":
        staging["immutable_releases"]["enabled"] = False
    elif mutation == "immutable-digest":
        staging["immutable_releases_sha256"] = "sha256:" + "0" * 64
    elif mutation == "tag-ref":
        staging["tag_ref_state"]["exists"] = True
    elif mutation == "tag-ref-digest":
        staging["tag_ref_state_sha256"] = "sha256:" + "0" * 64
    elif mutation == "source":
        staging["target_commitish"] = "7" * 40
    elif mutation == "state":
        staging["draft"] = False
    else:
        digest = "sha256:" + "0" * 64

    with pytest.raises(ValueError):
        MODULE.validate_publication_staging(
            staging,
            artifact_inventory=_artifact_inventory(),
            verified_staging_sha256=digest,
            expected_tag="v2.0.0",
            expected_commit="8" * 40,
        )


@pytest.mark.parametrize("enforced_by_owner", [False, True])
def test_immutable_releases_requires_the_exact_enabled_response(
    enforced_by_owner: bool,
) -> None:
    document = {"enabled": True, "enforced_by_owner": enforced_by_owner}
    assert MODULE.validate_immutable_releases(document) == document


@pytest.mark.parametrize(
    "document",
    [
        {"enabled": True},
        {"enabled": True, "enforced_by_owner": False, "extra": None},
        {"enabled": False, "enforced_by_owner": False},
        {"enabled": True, "enforced_by_owner": 0},
    ],
)
def test_immutable_releases_refuses_incomplete_or_open_responses(
    document: dict,
) -> None:
    with pytest.raises(ValueError):
        MODULE.validate_immutable_releases(document)


def test_ruleset_pages_form_one_complete_unique_index(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    page_one = tmp_path / "page-one.json"
    page_two = tmp_path / "page-two.json"
    immutable, creation = _tag_rulesets()
    page_one.write_text(json.dumps([immutable]), encoding="utf-8")
    page_two.write_text(json.dumps([creation]), encoding="utf-8")

    assert MODULE.merge_tag_ruleset_page(index_path, page_one) == 1
    assert MODULE.merge_tag_ruleset_page(index_path, page_two) == 1
    assert MODULE.select_tag_ruleset_ids(json.loads(index_path.read_text())) == (
        123,
        456,
    )

    page_two.write_text(json.dumps([immutable]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate ID"):
        MODULE.merge_tag_ruleset_page(index_path, page_two)
