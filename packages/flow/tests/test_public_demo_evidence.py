"""Retained public-demo packs remain independently verifiable."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.export_public_demo_evidence import EvidencePackError, validate_pack

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "pack_id",
    ["mockmed-triage-v1", "mockmed-triage-v2", "mockmed-triage-v3"],
)
def test_retained_public_demo_pack_validates(pack_id: str) -> None:
    manifest = validate_pack(REPO_ROOT / "public-demo" / "evidence-packs" / pack_id)

    assert manifest["pack"]["id"] == pack_id


def test_legacy_pack_rejects_a_rehashed_rewritten_report(tmp_path: Path) -> None:
    """A self-consistent rewrite cannot enter the non-production migration."""

    pack = tmp_path / "mockmed-triage-v3"
    shutil.copytree(REPO_ROOT / "public-demo" / "evidence-packs" / pack.name, pack)
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_relative = next(
        item["path"]
        for item in manifest["files"]
        if item["path"].endswith("/run/report.json")
    )
    report_path = pack / report_relative
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["retained_tamper"] = "changed"
    report_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    report_path.write_bytes(report_bytes)
    for item in manifest["files"]:
        if item["path"] == report_relative:
            item["bytes"] = len(report_bytes)
            item["sha256"] = hashlib.sha256(report_bytes).hexdigest()
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    (pack / "manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n",
        encoding="ascii",
    )

    with pytest.raises(
        EvidencePackError,
        match="retained legacy pack does not match its exact pinned manifest",
    ):
        validate_pack(pack)
