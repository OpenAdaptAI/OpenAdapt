"""One version/scope model across independently installed Python packages."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDES = [
    runpy.run_path(str(ROOT / path))["control_permission_guidance"]
    for path in (
        "openadapt/macos_permissions.py",
        "packages/flow/openadapt_flow/macos_permissions.py",
        "packages/desktop/openadapt_desktop/macos_permissions.py",
        "packages/capture/openadapt_capture/macos_permissions.py",
    )
]


def test_numeric_version_boundary_is_the_same_at_every_entry_point():
    for major in (10, 15, 26, 27, 28, 30):
        for minor in range(13):
            for patch in ("", ".0", ".12"):
                version = f"{major}.{minor}{patch}"
                expected = "app_selection" if (major, minor) >= (27, 3) else "legacy"
                results = [guide(version) for guide in GUIDES]
                assert all(result == results[0] for result in results)
                for result in results:
                    assert result["setup_mode"] == expected
                    if expected == "legacy":
                        assert result["label"] == "Accessibility"
                        assert result["settings_url"].endswith("?Privacy_Accessibility")
                    else:
                        assert result["label"] == "System & App Protection Override"
                        assert "?" not in result["settings_url"]
                        assert "Choose Apps" in result["instructions"]
                        assert "Ask First" in result["instructions"]
                        assert "At Any Time" in result["instructions"]


@pytest.mark.parametrize("version", ["", "27", "27.x", "27.3beta2", "-1.0", "27.3.0.1"])
def test_unknown_version_does_not_invent_a_permission_or_pane(version):
    for guide in GUIDES:
        result = guide(version)
        assert result["setup_mode"] == "unknown"
        assert result["label"] == "macOS app control"
        assert "?" not in result["settings_url"]
        assert "earlier macOS" in result["instructions"]
