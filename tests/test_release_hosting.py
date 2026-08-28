from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release_hosting.py"
SPEC = importlib.util.spec_from_file_location("verify_release_hosting", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _ruleset(*rule_types: str, enforcement: str = "active") -> dict:
    return {
        "target": "tag",
        "enforcement": enforcement,
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "rules": [{"type": rule_type} for rule_type in rule_types],
    }


def test_release_hosting_accepts_immutable_release_and_closed_tag_rules() -> None:
    MODULE.verify_release_hosting_documents(
        {"enabled": True},
        [_ruleset("creation"), _ruleset("update", "deletion")],
        "v2.0.0",
    )


def test_release_hosting_refuses_disabled_immutable_releases() -> None:
    with pytest.raises(ValueError, match="immutable releases are not enabled"):
        MODULE.verify_release_hosting_documents(
            {"enabled": False},
            [_ruleset("creation", "update", "deletion")],
            "v2.0.0",
        )


def test_release_hosting_refuses_missing_tag_rule() -> None:
    with pytest.raises(ValueError, match="deletion"):
        MODULE.verify_release_hosting_documents(
            {"enabled": True}, [_ruleset("creation", "update")], "v2.0.0"
        )


def test_release_hosting_ignores_disabled_or_nonmatching_rulesets() -> None:
    excluded = _ruleset("creation", "update", "deletion")
    excluded["conditions"]["ref_name"]["exclude"] = ["refs/tags/v2.0.0"]
    with pytest.raises(ValueError, match="creation, deletion, update"):
        MODULE.verify_release_hosting_documents(
            {"enabled": True},
            [
                _ruleset("creation", "update", "deletion", enforcement="disabled"),
                excluded,
            ],
            "v2.0.0",
        )


def test_release_hosting_requires_a_stable_tag() -> None:
    with pytest.raises(ValueError, match="stable vX.Y.Z"):
        MODULE.verify_release_hosting_documents(
            {"enabled": True}, [_ruleset("creation", "update", "deletion")], "v2.0"
        )


def test_release_hosting_does_not_match_a_star_across_ref_segments() -> None:
    broad = _ruleset("creation", "update", "deletion")
    broad["conditions"]["ref_name"]["include"] = ["refs/*"]

    with pytest.raises(ValueError, match="creation, deletion, update"):
        MODULE.verify_release_hosting_documents({"enabled": True}, [broad], "v2.0.0")


def test_release_hosting_fails_closed_on_an_unsupported_exclude_pattern() -> None:
    ambiguous = _ruleset("creation", "update", "deletion")
    ambiguous["conditions"]["ref_name"]["exclude"] = ["refs/tags/v[0-9]*"]

    with pytest.raises(ValueError, match="character sets are unsupported"):
        MODULE.verify_release_hosting_documents(
            {"enabled": True}, [ambiguous], "v2.0.0"
        )
