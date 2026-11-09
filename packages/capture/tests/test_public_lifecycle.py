"""Public Capture lifecycle metadata follows the admission contract."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
COMPLIANCE = ROOT / "docs" / "COMPLIANCE.md"


def test_capture_target_has_no_static_lifecycle() -> None:
    public_text = "\n".join(
        [
            README.read_text(encoding="utf-8"),
            COMPLIANCE.read_text(encoding="utf-8"),
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


def test_compliance_page_carries_the_admission_contract() -> None:
    compliance = COMPLIANCE.read_text(encoding="utf-8").casefold()

    assert "not actively admitted" in compliance
    assert "signed production record" in compliance


def test_readme_leaves_governance_language_to_the_compliance_page() -> None:
    """A newcomer reads what Capture does first; release governance lives in docs."""
    readme = README.read_text(encoding="utf-8")

    for term in (
        "admission",
        "admitted",
        "signed ledger",
        "evidence authority",
        "product target",
        "lifecycle",
    ):
        assert term not in readme.casefold(), term
    # One link, absolute so it also works on the PyPI project page.
    assert len(re.findall(r"docs/COMPLIANCE\.md\)", readme)) == 1
    assert (
        "(https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/COMPLIANCE.md)"
        in readme
    )
