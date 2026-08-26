"""Public Capture lifecycle metadata follows the admission contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_capture_target_has_no_static_lifecycle() -> None:
    public_text = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8"),
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        ]
    ).casefold()

    for static_label in (
        "status: experimental",
        "lifecycle is **experimental**",
        "development status :: 2 - pre-alpha",
        "development status :: 4 - beta",
        "early access",
        "exploratory",
        "reference path",
    ):
        assert static_label not in public_text

    assert "not actively admitted" in public_text
    assert "signed production record" in public_text
