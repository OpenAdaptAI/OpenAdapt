"""silent-saves: the judge, the fault set, the proxy and the runner end to end.

cd packages/flow && python -m pytest benchmark/silent_saves/tests -q
"""

from __future__ import annotations

import csv
import json
import shlex
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

from silent_saves import baselines, judge, protocol, runner  # noqa: E402
from silent_saves.app import MockMed, rewrite_app_js  # noqa: E402

HERE = Path(__file__).resolve().parent
NOTE = "n"


def _row(i: int, patient: str = "p1", type_: str = "Triage", note: str = NOTE) -> dict:
    return {"id": i, "patient_id": patient, "type": type_, "note": note}


SEED = [_row(1, type_="Consult", note="URGENT")]


@pytest.mark.parametrize(
    "after, verdict",
    [
        (SEED + [_row(2)], "correct"),
        (SEED, "missing"),
        (SEED + [_row(2, note="")], "partial"),
        (SEED + [_row(2), _row(3)], "duplicate"),
        (SEED + [_row(2, patient="p2")], "wrong_record"),
        ([_row(2)], "collateral_loss"),
        (SEED + [_row(2), _row(3, patient="p2")], "collateral_write"),
    ],
)
def test_store_verdict(after: list, verdict: str) -> None:
    assert judge.store_verdict(SEED, after, note=NOTE) == verdict


def test_outcome() -> None:
    assert judge.outcome(True, "correct", store_changed=True) == "correct"
    assert judge.outcome(False, "correct", store_changed=True) == "false_alarm"
    assert judge.outcome(True, "partial", store_changed=True) == "silent_wrong"
    assert judge.outcome(False, "missing", store_changed=False) == "safe_stop"
    assert judge.outcome(False, "wrong_record", store_changed=True) == "flagged_wrong"


def test_fault_set_uses_only_published_kinds() -> None:
    effect_e2e = (BENCH / "effect_e2e" / "EFFECT_E2E.md").read_text()
    claims = (BENCH / "scoreboard" / "claims.py").read_text()
    for kind in protocol.FAULTS:
        assert f"`{kind}`" in effect_e2e, kind
        assert f'"{kind}"' in claims, kind


def test_plan_is_round_robin_and_fixed() -> None:
    items = protocol.plan(2)
    assert [it.fault for it in items] == list(protocol.FAULTS) * 2
    assert items[0].params == {"note": "Synthetic follow-up note 1: recheck in 8 days"}
    with pytest.raises(ValueError):
        protocol.plan(1, ("ok", "session"))


def test_app_js_hook_is_still_there() -> None:
    from openadapt_flow.mockmed.fault_server import STATIC_DIR

    out = rewrite_app_js((STATIC_DIR / "app.js").read_text(), "partial")
    assert "var FAULT = 'partial';" in out
    with pytest.raises(ValueError):
        rewrite_app_js("var FAULT = '';", "x'; alert(1); '")


def test_proxy_hides_the_fault_and_blocks_reset() -> None:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    app = MockMed()
    try:
        app.prepare("no_persist")
        assert "fault" not in app.url
        js = urlopen(f"{app.base}/app.js", timeout=10).read().decode()  # noqa: S310
        assert "var FAULT = 'no_persist';" in js
        with pytest.raises(HTTPError) as err:
            urlopen(
                Request(f"{app.base}/api/reset", data=b"{}", method="POST"), timeout=10
            )  # noqa: S310
        assert err.value.code == 403
        app.prepare("stale")
        assert len(app.records()) == 1  # the concurrent clinician's row
    finally:
        app.close()


def test_baselines_json_is_current() -> None:
    assert baselines.main(["--check"]) == 0


def _rows(out: Path) -> dict[str, dict]:
    return {r["fault"]: r for r in csv.DictReader((out / "items.csv").open())}


def test_command_executor_through_the_api(tmp_path: Path) -> None:
    """An executor that skips the page still gets every fault on its save."""

    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'api_executor.py'))}"
    summary = runner.run(f"command:{cmd}", out=tmp_path, per_fault=1)
    rows = _rows(tmp_path)
    assert {f: rows[f]["outcome"] for f in rows} == {
        "ok": "correct",
        "no_persist": "silent_wrong",
        "partial": "silent_wrong",
        "duplicate": "correct",  # the double send is the page's, not the API's
        "wrong_record": "silent_wrong",
        "stale": "silent_wrong",
        "optimistic": "safe_stop",  # the API answers 409
    }
    assert summary["silent_wrong_save_rate"]["k"] == 4
    assert summary["silent_wrong_save_rate"]["n"] == 5
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["version"] == protocol.VERSION


def _has_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _has_chromium(), reason="needs Playwright Chromium")
def test_screen_script_believes_every_bad_save(tmp_path: Path) -> None:
    """The $0 reference: the screen says saved under all six faults."""

    summary = runner.run("screen-script", out=tmp_path, per_fault=1)
    rows = _rows(tmp_path)
    assert rows["ok"]["outcome"] == "correct"
    assert {f: rows[f]["outcome"] for f in rows if f != "ok"} == {
        f: "silent_wrong" for f in protocol.FAULTS if f != "ok"
    }
    assert summary["silent_wrong_save_rate"]["k"] == 6


@pytest.mark.skipif(not _has_chromium(), reason="needs Playwright Chromium")
def test_readback_catches_what_the_screen_missed(tmp_path: Path) -> None:
    summary = runner.run(
        "screen-script",
        out=tmp_path,
        per_fault=1,
        faults=("ok", "no_persist", "wrong_record"),
        readback=True,
    )
    rows = _rows(tmp_path)
    assert rows["ok"]["outcome"] == "correct"
    assert rows["no_persist"]["outcome"] == "safe_stop"
    assert rows["wrong_record"]["outcome"] == "flagged_wrong"
    assert summary["silent_wrong_save_rate"]["k"] == 0
