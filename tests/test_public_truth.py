import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_hero_accessibility_copy_uses_current_substrate_availability() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hero = re.search(r'<img[^>]+openadapt-hero\.svg[^>]+alt="([^"]+)"', readme)
    assert hero is not None
    copy = hero.group(1)
    for substrate in ("Browser", "Windows", "macOS", "Linux", "RDP", "Citrix/VDI"):
        assert substrate in copy
    for stale_label in ("early access", "research", "exploratory"):
        assert stale_label not in copy.lower()
    assert "available" in copy.lower()
