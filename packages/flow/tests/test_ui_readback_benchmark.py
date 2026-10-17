"""CI guard for the separate-session UI read-back measurement.

Runs a subset of ``benchmark/ui_readback``'s API-write harness (real record
service, real browser, independent SQLite ground truth) and pins the claims
``benchmark/ui_readback/RESULTS.md`` and ``docs/EFFECT_KIT.md`` publish:

- the same-screen check passes every 2xx-but-wrong save in the subset;
- the UI read-back catches partial saves, wrong-record writes, and lost
  updates, as the API read does;
- both miss a stray write to a table the record page and the API don't show;
- only the API read catches a record page served from a cache the failed write
  filled;
- display probes stop the UI read-back (shortened visible text, a page that
  posts on load) or let it pass (full value read from an attribute), and the
  read-back never lets a page's POST reach the server.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from benchmark.ui_readback import api_harness  # noqa: E402
from benchmark.ui_readback.run import summarize  # noqa: E402

SUBSET = {
    "ok",
    "partial",
    "wrong_record",
    "stale",
    "collateral_unaudited",
    "page_cache",
    "long_note_visible_text",
    "long_note_title_attribute",
    "page_posts_audit_beacon",
}


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    scenarios = tuple(
        s for s in api_harness.FAULTS + api_harness.PROBES if s.name in SUBSET
    )
    return api_harness.run(1, scenarios=scenarios, log=lambda _m: None)


def _cell(rows: list[dict], scenario: str, arm: str) -> dict:
    (row,) = [r for r in rows if r["scenario"] == scenario and r["arm"] == arm]
    return row


def test_same_screen_misses_every_accepted_wrong_save(rows: list[dict]) -> None:
    for scenario in ("partial", "wrong_record", "stale", "page_cache"):
        cell = _cell(rows, scenario, "same_screen")
        assert cell["gt_correct"] is False
        assert cell["silent_wrong"] is True, scenario


def test_ui_readback_catches_what_the_record_page_shows(rows: list[dict]) -> None:
    for scenario in ("partial", "wrong_record", "stale"):
        for arm in ("ui_readback", "api_read"):
            cell = _cell(rows, scenario, arm)
            assert cell["caught"] is True, (scenario, arm)
            assert cell["stopped_by"] == "check"
    # Its evidence is tier 2, never the system-of-record tier.
    assert _cell(rows, "ok", "ui_readback")["evidence_tiers"] == [2]
    assert _cell(rows, "ok", "api_read")["evidence_tiers"] == [1]


def test_the_measured_blind_spots(rows: list[dict]) -> None:
    for arm in ("ui_readback", "api_read"):
        assert _cell(rows, "collateral_unaudited", arm)["silent_wrong"] is True
    assert _cell(rows, "page_cache", "ui_readback")["silent_wrong"] is True
    assert _cell(rows, "page_cache", "api_read")["caught"] is True


def test_display_probes(rows: list[dict]) -> None:
    assert _cell(rows, "long_note_visible_text", "ui_readback")["halted"] is True
    assert _cell(rows, "long_note_title_attribute", "ui_readback")["halted"] is False
    beacon = _cell(rows, "page_posts_audit_beacon", "ui_readback")
    assert beacon["halted"] is True
    assert beacon["beacons_sent_to_server"] == 0
    for probe in ("long_note_visible_text", "page_posts_audit_beacon"):
        assert _cell(rows, probe, "api_read")["halted"] is False


def test_summary_counts(rows: list[dict]) -> None:
    per_arm = summarize(rows)["api_write"]["per_arm"]
    assert per_arm["same_screen"]["caught"] == 0
    assert per_arm["ui_readback"]["caught"] == 3
    assert per_arm["api_read"]["caught"] == 4
    for arm in ("same_screen", "ui_readback", "api_read"):
        assert per_arm[arm]["false_stops"] == []
