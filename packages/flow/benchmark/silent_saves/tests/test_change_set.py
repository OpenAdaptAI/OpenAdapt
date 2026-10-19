"""silent-saves fault set 1.1.0: the schedule, the page rewrites, the metrics.

cd packages/flow && python -m pytest benchmark/silent_saves/tests -q
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

from silent_saves import metrics, protocol  # noqa: E402
from silent_saves.app import MockMed, rewrite_app_js  # noqa: E402

FS = protocol.load("1.1.0")
ITEMS = protocol.plan_change(FS)


def test_schedule_is_fixed() -> None:
    assert FS["version"] == "1.1.0"
    assert len(ITEMS) == 100
    assert Counter(i.kind for i in ITEMS) == {
        "control": 15,
        "change": 55,
        "trap": 25,
        "fault": 5,
    }
    assert [i.item for i in ITEMS if i.expect == "stop"] == [96, 97, 98, 99, 100]
    assert {i.item: i.fault for i in ITEMS if i.fault != "ok"} == {
        20: "no_persist",
        30: "wrong_record",
        40: "partial",
        60: "duplicate",
        80: "optimistic",
    }
    # Each block has only its own change.
    assert ITEMS[0].drift == ()
    assert ITEMS[19].drift == ("rename",)
    assert ITEMS[65].drift == ("sort",)
    assert ITEMS[99].drift == ("missing",)
    assert all(i.idempotency_key for i in ITEMS)


def test_only_existing_drifts_and_published_faults() -> None:
    from openadapt_flow.mockmed.fault_server import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    for change in FS["changes"]:
        assert f"DRIFT.has('{change}')" in source, change
    published = set(protocol.FAULTS)
    assert set(FS["faults"].values()) <= published


def test_rewrite_sets_changes_latency_and_key() -> None:
    from openadapt_flow.mockmed.fault_server import STATIC_DIR

    out = rewrite_app_js(
        (STATIC_DIR / "app.js").read_text(),
        "ok",
        ("rename", "slow"),
        2000,
        True,
    )
    assert "'rename,slow'" in out
    assert "parseInt('2000' || '4000', 10)" in out
    assert "var DEMO_IDEMPOTENCY =\n  true;" in out
    assert "location.search).get('drift')" not in out


def test_proxy_serves_the_items_changes() -> None:
    from urllib.request import urlopen

    app = MockMed()
    try:
        it = ITEMS[65]  # item 66: sort released
        app.prepare(it.fault, it.drift, it.slow_ms, it.idempotency_key)
        js = urlopen(f"{app.base}/app.js", timeout=10).read().decode()  # noqa: S310
        assert "sort" in js.split("var DRIFT")[1].split(";")[0]
        assert "drift" not in app.url
    finally:
        app.close()


def _row(kind: str, expect: str, outcome: str, verdict: str, **kw: object) -> dict:
    return {
        "kind": kind,
        "expect": expect,
        "release": kw.pop("release", "r"),
        "outcome": outcome,
        "store_verdict": verdict,
        "reported_success": outcome in ("correct", "silent_wrong"),
        "cost_usd": kw.pop("cost", 0.0),
        "model_calls": 0,
        "item_s": 1.0,
        **kw,
    }


def test_change_metrics_keep_detection_and_prevention_apart() -> None:
    rows = [
        _row("control", "save", "correct", "correct", cost=0.1),
        _row("change", "save", "safe_stop", "missing"),
        _row("trap", "save", "silent_wrong", "wrong_record"),
        _row("trap", "save", "correct", "correct", cost=0.1),
        _row("trap", "stop", "safe_stop", "missing"),
        _row("fault", "save", "silent_wrong", "missing"),
        _row(
            "change",
            "save",
            "correct",
            "correct",
            human_events=json.dumps([{"kind": "repair", "minutes": 3, "by": "person"}]),
        ),
    ]
    s = metrics.summarize_change(rows)
    assert (s["verified_saves"]["k"], s["verified_saves"]["n"]) == (3, 5)
    assert (s["silent_wrong_saves"]["k"], s["silent_wrong_saves"]["n"]) == (2, 7)
    assert (s["false_stops"]["k"], s["false_stops"]["n"]) == (1, 5)
    assert s["detection"]["silent_wrong"]["k"] == 1
    assert s["prevention"]["wrong_saves_landed"]["k"] == 1
    assert s["prevention"]["wrong_saves_landed"]["n"] == 3
    assert s["prevention"]["stopped_when_no_correct_save"]["k"] == 1
    assert s["human"]["minutes_logged"] == 3
    assert s["human"]["items_left_for_a_person"] == 2
    assert s["cost_per_verified_item_usd"] == pytest.approx(0.2 / 3, rel=1e-3)


def test_duplicate_lands_twice_despite_the_key() -> None:
    from urllib.request import Request, urlopen

    app = MockMed()
    try:
        app.prepare("duplicate", (), 0, True)
        js = urlopen(f"{app.base}/app.js", timeout=10).read().decode()  # noqa: S310
        assert "var FAULT = 'ok';" in js  # one send from the page
        body = json.dumps(
            {"patient_id": "p1", "type": "Triage", "note": "n", "key": "k"}
        ).encode()
        urlopen(  # noqa: S310
            Request(
                f"{app.base}/api/encounter",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            ),
            timeout=10,
        ).read()
        rows = app.records()
        assert len(rows) == 2 and all(r["key"] is None for r in rows)
        app.prepare("ok", (), 0, True)
        assert app.records() == []
    finally:
        app.close()
