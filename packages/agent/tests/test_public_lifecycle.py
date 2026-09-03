"""Public Agent lifecycle metadata follows the admission contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_target_has_no_static_lifecycle() -> None:
    public_text = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "DISTRIBUTION.md").read_text(encoding="utf-8"),
            (ROOT / "llms.txt").read_text(encoding="utf-8"),
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
            (ROOT / "server.json").read_text(encoding="utf-8"),
        ]
    ).casefold()

    for static_label in (
        "lifecycle: beta",
        "status: beta",
        "development status :: 4 - beta",
        "early access",
        "exploratory",
        "reference path",
    ):
        assert static_label not in public_text

    assert "not actively admitted" in public_text
    assert "signed production record" in public_text
    assert "https://openadapt.ai/production-lifecycle.json" in public_text
    assert "https://openadapt.ai/production-workflow-admissions.json" in public_text
    assert "remote-safe-synthetic" in public_text
    assert "0.0.0-synthetic" in public_text
