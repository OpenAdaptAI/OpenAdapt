"""The release guard reads the rendered policy, fails closed, and never weakens.

`scripts/check_release_consistency.py` used to carry a hand-copied denylist of
the crown-jewel categories. It now derives those rules from
`source-policy.public.json`, which is rendered from the canonical private
manifest. Two properties are pinned here:

1. Fail closed. A missing, unparseable, incomplete, or unknown-schema policy
   raises `SourcePolicyError`, and the script exits non-zero rather than
   validating an artifact with no rules.
2. Never weaker. `LEGACY_*` below is exactly what this guard enforced on
   2026-07-28, before the move. It is a one-way ratchet: rules may be added,
   but nothing that once failed may start passing.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_consistency.py"
sys.path.insert(0, str(ROOT / "scripts"))

import check_release_consistency as guard  # noqa: E402

LEGACY_PRIVATE_DISTRIBUTION_PATH_TOKENS = (
    "openadapt-corpus",
    "adversary_corpus",
    "identity_roc",
    "grown_corpus",
    "tuned_adversary",
    "deployment_corpus",
    "deployment_thresholds",
    "effect_oracle_recipe",
    "held_out_corpus",
    "oracle_recipe",
    "pixel_verify_cert",
    "real_emr",
    "enterprise_productionized",
    "control_plane",
    "paid_agent_evidence",
    "agent-arm/",
    "rows.jsonl",
    "cost_ledger",
    "frappe_agent_arm.py",
    "openemr_agent_arm.py",
    "openimis_agent_arm.py",
)
LEGACY_PRIVATE_DISTRIBUTION_PATH_SEGMENTS = frozenset({"private", ".private"})
LEGACY_PUBLIC_SOURCE_FORBIDDEN_CATEGORIES = frozenset(
    {
        "control_plane",
        "deployment_thresholds",
        "enterprise_productionized",
        "grown_corpus",
        "oracle_recipes",
        "real_emr_datasets",
        "tuned_adversary_params",
    }
)
LEGACY_PRIVATE_CORPUS_CONTENT_SIGNATURE = (
    b"OPENADAPT-CORPUS" + b"-PRIVATE-DO-NOT-PACKAGE"
)


def _policy_document() -> dict:
    return json.loads(guard.SOURCE_POLICY_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "source-policy.public.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Never weaker than the hand-copied denylist it replaced.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token", LEGACY_PRIVATE_DISTRIBUTION_PATH_TOKENS)
def test_legacy_token_is_still_enforced(token: str) -> None:
    assert token in guard.PRIVATE_DISTRIBUTION_PATH_TOKENS
    member = f"openadapt_flow/{token}/payload.json".replace("//", "/")
    assert guard._private_distribution_hits({member}, set()) == {member}


def test_legacy_path_segments_are_still_enforced() -> None:
    assert LEGACY_PRIVATE_DISTRIBUTION_PATH_SEGMENTS <= (
        guard.PRIVATE_DISTRIBUTION_PATH_SEGMENTS
    )
    member = "private/notes.json"
    assert guard._private_distribution_hits({member}, set()) == {member}


def test_legacy_forbidden_categories_are_unchanged() -> None:
    assert LEGACY_PUBLIC_SOURCE_FORBIDDEN_CATEGORIES == (
        guard.PUBLIC_SOURCE_FORBIDDEN_CATEGORIES
    )


def test_legacy_content_signature_is_still_enforced() -> None:
    assert LEGACY_PRIVATE_CORPUS_CONTENT_SIGNATURE in (
        guard.PRIVATE_CORPUS_CONTENT_SIGNATURES
    )


def test_policy_adds_rules_rather_than_removing_them() -> None:
    # The manifest binds every public repository, so this guard also gained the
    # proprietary system-of-record tokens the tree guard already had.
    assert {"powerchart", "cerner", "meditech"} <= set(
        guard.PRIVATE_DISTRIBUTION_PATH_TOKENS
    )
    member = "benchmark/powerchart_recipe/steps.json"
    assert guard._private_distribution_hits({member}, set()) == {member}


def test_policy_path_prefixes_are_enforced() -> None:
    assert guard.PRIVATE_DISTRIBUTION_PATH_PREFIXES
    for prefix in guard.PRIVATE_DISTRIBUTION_PATH_PREFIXES:
        member = f"{prefix}_v9/manifest.json"
        assert guard._private_distribution_hits({member}, set()) == {member}


def test_a_public_member_still_passes() -> None:
    assert (
        guard._private_distribution_hits({"openadapt_flow/compiler.py"}, set()) == set()
    )


# --------------------------------------------------------------------------
# Fail closed: no rules, no release.
# --------------------------------------------------------------------------


def test_missing_policy_raises(tmp_path: Path) -> None:
    with pytest.raises(guard.SourcePolicyError, match="cannot read the rendered"):
        guard.load_source_policy(tmp_path / "absent.json")


def test_unparseable_policy_raises(tmp_path: Path) -> None:
    path = tmp_path / "source-policy.public.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(guard.SourcePolicyError, match="not valid JSON"):
        guard.load_source_policy(path)


def test_unknown_schema_raises(tmp_path: Path) -> None:
    document = _policy_document()
    document["schema_version"] = 99
    with pytest.raises(guard.SourcePolicyError, match="unknown schema"):
        guard.load_source_policy(_write_policy(tmp_path, document))


def test_empty_rules_raise(tmp_path: Path) -> None:
    document = _policy_document()
    document["enforcement"]["path_tokens"] = []
    with pytest.raises(guard.SourcePolicyError, match="path_tokens"):
        guard.load_source_policy(_write_policy(tmp_path, document))


def test_missing_enforcement_raises(tmp_path: Path) -> None:
    document = _policy_document()
    del document["enforcement"]
    with pytest.raises(guard.SourcePolicyError, match="enforcement"):
        guard.load_source_policy(_write_policy(tmp_path, document))


def test_missing_categories_raise(tmp_path: Path) -> None:
    document = _policy_document()
    document["crown_jewel_categories"] = []
    with pytest.raises(guard.SourcePolicyError, match="crown_jewel_categories"):
        guard.load_source_policy(_write_policy(tmp_path, document))


def test_signature_parts_must_be_present(tmp_path: Path) -> None:
    document = _policy_document()
    document["enforcement"]["content_signature_parts"] = []
    with pytest.raises(guard.SourcePolicyError, match="content_signature_parts"):
        guard.load_source_policy(_write_policy(tmp_path, document))


def test_the_script_refuses_to_run_without_the_policy(tmp_path: Path) -> None:
    """A checkout with no rendered policy must stop, not validate silently."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    completed = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "cannot read the rendered source policy" in completed.stderr


def test_the_rendered_policy_is_a_generated_file() -> None:
    document = _policy_document()
    assert "DO NOT EDIT BY HAND" in document["_comment"]
    assert document["generated_from"].endswith("source-policy.yaml")
    assert document["policy_digest"].startswith("sha256:")


def test_the_policy_module_loads_the_committed_copy() -> None:
    assert guard.SOURCE_POLICY_PATH == ROOT / "source-policy.public.json"
    assert guard.SOURCE_POLICY.policy_digest.startswith("sha256:")
    spec = importlib.util.find_spec("check_release_consistency")
    assert spec is not None


def test_flow_is_classified_public() -> None:
    document = _policy_document()
    assert (
        document["public_repositories"]["openadapt-flow"]["classification"] == "public"
    )


def test_mutating_the_policy_changes_what_is_blocked(tmp_path: Path) -> None:
    """The rules really do come from the file, not from this module."""
    document = copy.deepcopy(_policy_document())
    document["enforcement"]["path_tokens"] = ["a-token-that-appears-nowhere"]
    policy = guard.load_source_policy(_write_policy(tmp_path, document))
    assert policy.path_tokens == ("a-token-that-appears-nowhere",)
    assert "adversary_corpus" not in policy.path_tokens
    # ...and the committed policy, which is what the guard actually enforces,
    # still carries the real rules.
    assert "adversary_corpus" in guard.PRIVATE_DISTRIBUTION_PATH_TOKENS
