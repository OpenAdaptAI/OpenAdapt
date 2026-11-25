"""Open manifest questions block only the write kinds they concern (#1947)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.generate import verify_contract as gen


def _manifest_with_open_questions(
    questions: list[dict],
    questions_open: list[str],
) -> dict:
    return {
        "schema_version": gen.MANIFEST_SCHEMA,
        "questions": questions,
        "questions_open": questions_open,
        "kinds": {
            "create_order": {"mode": "create", "variants": []},
            "update_order": {"mode": "update", "variants": []},
            "create_voucher": {"mode": "create", "variants": []},
        },
    }


def test_open_question_for_one_kind_does_not_block_unrelated_kinds() -> None:
    manifest = _manifest_with_open_questions(
        questions=[
            {
                "number": 1,
                "key": "locations.create_order",
                "text": "Kind `create_order`: ambiguous",
                "proposal": None,
                "answered": False,
                "kinds": ["create_order"],
            }
        ],
        questions_open=["locations.create_order"],
    )
    assert gen.open_questions_for_kind(manifest, "create_order") == [
        "locations.create_order"
    ]
    assert gen.open_questions_for_kind(manifest, "update_order") == []
    assert gen.open_questions_for_kind(manifest, "create_voucher") == []


def test_legacy_manifest_location_question_still_scopes_by_kind_prefix() -> None:
    # Even without an explicit `kinds` list on the question entry, a
    # `locations.<declared_kind>` key scopes only to that kind.
    manifest = _manifest_with_open_questions(
        questions=[
            {
                "number": 1,
                "key": "locations.create_order",
                "text": "Kind `create_order`: ambiguous",
                "proposal": None,
                "answered": False,
            }
        ],
        questions_open=["locations.create_order"],
    )
    assert gen.open_questions_for_kind(manifest, "create_order") == [
        "locations.create_order"
    ]
    assert gen.open_questions_for_kind(manifest, "update_order") == []


def test_shared_or_ambiguous_open_question_blocks_every_kind() -> None:
    manifest = _manifest_with_open_questions(
        questions=[
            {
                "number": 1,
                "key": "bookkeeping",
                "text": "Shared bookkeeping?",
                "proposal": "accept",
                "answered": False,
            },
            {
                "number": 2,
                "key": "locations.unknown_shared",
                "text": "Ambiguous location",
                "proposal": None,
                "answered": False,
            },
        ],
        questions_open=["bookkeeping", "locations.unknown_shared"],
    )
    for kind in ("create_order", "update_order", "create_voucher"):
        assert gen.open_questions_for_kind(manifest, kind) == [
            "bookkeeping",
            "locations.unknown_shared",
        ]


def test_choose_refuses_dependent_kind_but_allows_unrelated_kind(
    tmp_path: Path,
) -> None:
    import yaml

    manifest = _manifest_with_open_questions(
        questions=[
            {
                "number": 1,
                "key": "locations.create_order",
                "text": "Kind `create_order`: ambiguous",
                "proposal": None,
                "answered": False,
                "kinds": ["create_order"],
            }
        ],
        questions_open=["locations.create_order"],
    )
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    # Dependent kind is blocked by its open question.
    with pytest.raises(gen.GenerateError, match="locations.create_order"):
        gen.choose(manifest_path, "create_order", {})

    # Unrelated kind passes the open-question check (and only stops because
    # this minimal fixture has no variants).
    with pytest.raises(gen.GenerateError, match="no contract for kind 'update_order'"):
        gen.choose(manifest_path, "update_order", {})
