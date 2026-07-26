"""Retained public-demo packs remain independently verifiable."""

from pathlib import Path

import pytest

from scripts.export_public_demo_evidence import validate_pack

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "pack_id",
    ["mockmed-triage-v1", "mockmed-triage-v2", "mockmed-triage-v3"],
)
def test_retained_public_demo_pack_validates(pack_id: str) -> None:
    manifest = validate_pack(REPO_ROOT / "public-demo" / "evidence-packs" / pack_id)

    assert manifest["pack"]["id"] == pack_id
