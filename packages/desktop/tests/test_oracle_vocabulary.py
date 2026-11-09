"""Desktop must present the public Seal oracle ladder to operators."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_operator_surfaces_use_the_seal_oracle_ladder() -> None:
    qualification = (ROOT / "src" / "screens" / "Qualification.tsx").read_text(
        encoding="utf-8"
    )
    overlay = (ROOT / "src" / "overlay" / "state.ts").read_text(encoding="utf-8")
    public_copy = qualification + overlay

    assert "System of record · Oracle tier 2" in qualification
    assert "Independent session · Oracle tier 1" in qualification
    assert "Tier 1 · independent system" not in public_copy
    assert "Tier 2 · independent session" not in public_copy
    assert "(Tier ${tier})" not in public_copy
