from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from render_platform_versions import render_markdown, version_rows  # noqa: E402


def test_version_display_is_derived_from_the_platform_bom() -> None:
    manifest = json.loads((REPO_ROOT / "platform-manifest.json").read_text())
    rows = version_rows(manifest)

    assert [row["role"] for row in rows] == [
        "launcher",
        "flow",
        "capture",
        "privacy",
        "types",
        "desktop",
        "agent",
        "launcher_environment",
        "customer_runner",
        "desktop_sidecar",
        "agent_bridge",
    ]
    by_role = {row["role"]: row for row in rows}
    assert by_role["flow"]["version"] == manifest["components"]["flow"]["version"]
    assert by_role["desktop_sidecar"]["resolved"] == ", ".join(
        f"{package}=={version}"
        for package, version in sorted(
            manifest["runtime_units"]["desktop_sidecar"]["resolved"].items()
        )
    )


def test_version_display_changes_when_the_manifest_changes() -> None:
    manifest = json.loads((REPO_ROOT / "platform-manifest.json").read_text())
    manifest["components"]["flow"]["version"] = "9.8.7"

    by_role = {row["role"]: row for row in version_rows(manifest)}
    assert by_role["flow"]["version"] == "9.8.7"


def test_human_report_is_generated_from_the_exact_bom() -> None:
    manifest = json.loads((REPO_ROOT / "platform-manifest.json").read_text())
    report = REPO_ROOT / "docs" / "platform-compatibility-report.md"

    text = report.read_text()
    assert text == render_markdown(manifest)
    assert f"`{manifest['components']['agent']['version']}`" in text
    assert all(row["name"] in text for row in manifest["substrate_drivers"])
    assert all(edge["target_package"] in text for edge in manifest["dependency_edges"])
