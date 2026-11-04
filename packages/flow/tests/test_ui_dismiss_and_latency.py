"""A screen-only click gets no record_written (#1377); a slow app's
navigation click is judged once its screen arrives (#1378).

#1377: effect mining attached a placeholder ``record_written`` to MockMed's
What's New "Continue to tasks" click ("continue" is a write word), although
the record store did not change across it. A step whose store snapshots are
equal before, after, and when the next action begins, with no write request
captured, now gets no ``record_written``. A step that changed the store keeps
it.

#1378: under MockMed's ``slow`` change the URL changes at once but the next
screen renders seconds later. The recorder took two identical frames of the
OLD screen as "settled", so the compiler saw no visual change, fell back to a
``url_changed`` check, and the risk inference asked for a reviewed override.
The recorder now keeps waiting (bounded) while a change it started has not
reached the screen.

The live OpenEMR versions are ``tests/test_ui_dismiss_live_openemr.py`` and
``tests/test_latency_settle_live_openemr.py``.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_flow.compiler.effect_mining import (
    MIN_STORE_SETTLE_GAP_S,
    SOR_AFTER_KEY,
    SOR_BEFORE_KEY,
    SOR_T_AFTER_KEY,
    SOR_T_BEFORE_KEY,
    mine_step_effects,
    store_unchanged_through_next,
)
from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator
from openadapt_flow.recorder import Recorder, _phash, _pixels
from openadapt_flow.runtime.effects import EffectKind

CHART = [{"id": "e1", "type": "Encounter", "patient_id": "p1", "note": "x"}]


# -- #1377: the store-unchanged proof ---------------------------------------------------------


def _ev(i: int, before: Any, after: Any, t: float = 0.0) -> dict[str, Any]:
    # t: when the before snapshot was read; the after one 0.2 s later.
    return {
        "i": i,
        SOR_BEFORE_KEY: before,
        SOR_AFTER_KEY: after,
        SOR_T_BEFORE_KEY: t,
        SOR_T_AFTER_KEY: t + 0.2,
    }


def _proof(*events: dict[str, Any]) -> Optional[bool]:
    return store_unchanged_through_next({e["i"]: e for e in events}, 0)


def test_the_store_is_unchanged_only_with_all_three_snapshots_equal():
    assert _proof(_ev(0, [], []), _ev(1, [], CHART, t=3.0))
    # The write landed after the step's own snapshot, before the next action.
    assert _proof(_ev(0, [], []), _ev(1, CHART, CHART, t=3.0)) is False
    assert _proof(_ev(0, [], CHART), _ev(1, CHART, [], t=3.0)) is False


def test_no_proof_without_every_snapshot():
    assert _proof(_ev(0, [], [])) is None
    assert _proof(_ev(0, None, []), _ev(1, [], [], t=3.0)) is None
    assert _proof(_ev(0, [], []), {"i": 1}) is None


def test_no_proof_without_a_real_gap_before_the_next_snapshot():
    # A scripted demonstration acts again at once, and an interactive one
    # reuses the after snapshot: a late write would not show yet.
    assert MIN_STORE_SETTLE_GAP_S >= 1.0
    assert _proof(_ev(0, [], []), _ev(1, [], [], t=0.5)) is None
    untimed = {"i": 1, SOR_BEFORE_KEY: [], SOR_AFTER_KEY: []}
    assert _proof(_ev(0, [], []), untimed) is None


def _dismiss_step(label: str = "Continue to tasks") -> Step:
    return Step(
        id="step_000",
        intent=f"click {label}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/0.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            structural=StructuralLocator(
                selector="#notice-continue", role="button", name=label
            ),
        ),
        risk="irreversible",
    )


def test_a_dismiss_that_left_the_store_alone_gets_no_record_written():
    mined = mine_step_effects(_ev(0, [], []), _dismiss_step(), store_unchanged=True)
    assert mined.disposition == "none" and mined.effects == []
    assert "no network capture" in mined.reason
    assert "no write request" not in mined.reason
    captured = mine_step_effects(
        _ev(0, [], []), _dismiss_step(), store_unchanged=True, write_requests=False
    )
    assert "no write request in the step's window" in captured.reason


@pytest.mark.parametrize(
    "label",
    [
        "Save Encounter",
        "Submit",
        "Sign note",
        "Continue to checkout",
        "Continue and save",
    ],
)
def test_a_write_shaped_step_keeps_its_placeholder_whatever_the_store_shows(label):
    # The reader may not see every write (another table, an older row, a
    # same-value save): only a dismiss may lose its placeholder.
    mined = mine_step_effects(
        _ev(0, [], []), _dismiss_step(label), store_unchanged=True
    )
    assert mined.placeholder
    assert mined.effects[0].kind is EffectKind.RECORD_WRITTEN


@pytest.mark.parametrize(
    "label", ["Close", "OK", "Got it", "Dismiss", "Ask again later", "Skip", "Not now"]
)
def test_dismiss_shaped_labels(label):
    mined = mine_step_effects(
        _ev(0, [], []), _dismiss_step(label), store_unchanged=True
    )
    assert mined.disposition == "none"


def test_without_the_proof_the_placeholder_stays():
    mined = mine_step_effects(_ev(0, [], []), _dismiss_step())
    assert mined.placeholder
    assert mined.effects[0].kind is EffectKind.RECORD_WRITTEN


def test_a_step_that_wrote_keeps_record_written_whatever_the_proof_says():
    mined = mine_step_effects(_ev(0, [], CHART), _dismiss_step(), store_unchanged=True)
    assert mined.derived
    assert mined.effects[0].kind is EffectKind.RECORD_WRITTEN


def _recording(tmp_path: Path, *, late_write: bool, post_at: Optional[float]) -> Path:
    page = {
        "url_before": "http://app/#tasks",
        "url_after": "http://app/#tasks",
        "title_before": "MockMed",
        "title_after": "MockMed",
        "pages_before": 1,
        "pages_after": 1,
    }
    events = [
        {
            "i": 0,
            "kind": "click",
            "x": 129,
            "y": 236,
            "t": 1.0,
            "structural": {
                "selector": "#notice-continue",
                "role": "button",
                "name": "Continue to tasks",
            },
            SOR_BEFORE_KEY: [],
            SOR_AFTER_KEY: [],
            SOR_T_BEFORE_KEY: 0.8,
            SOR_T_AFTER_KEY: 1.2,
            **page,
        },
        {
            "i": 1,
            "kind": "click",
            "x": 300,
            "y": 400,
            "t": 3.0,
            "structural": {
                "selector": "#save-encounter",
                "role": "button",
                "name": "Save Encounter",
            },
            SOR_BEFORE_KEY: CHART if late_write else [],
            SOR_AFTER_KEY: CHART,
            SOR_T_BEFORE_KEY: 2.9,
            SOR_T_AFTER_KEY: 3.3,
            **page,
        },
    ]
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    for i in range(len(events)):
        for side in ("before", "after"):
            Image.new("RGB", (1280, 800), "white").save(
                rec / "frames" / f"{i:04d}_{side}.png"
            )
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (rec / "meta.json").write_text(json.dumps({"params": {}}))
    if post_at is not None:
        lines = [{"method": "POST", "url": "http://app/api/seen", "t": post_at}]
        header = {"schema": "openadapt.network-capture/1", "exchanges": len(lines)}
        (rec / "network.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in [header, *lines])
        )
    return rec


def _compiled(tmp_path: Path, **kw: Any) -> list[Step]:
    from openadapt_flow.compiler.compile import compile_recording

    rec = _recording(tmp_path, **kw)
    wf = compile_recording(rec, tmp_path / "bundle", name="notice", mine_effects=True)
    return list(wf.steps)


def test_compiled_without_network_capture_the_dismiss_gets_no_record_written(tmp_path):
    # The compile-agent's recordings: store snapshots, no network capture.
    dismiss, save = _compiled(tmp_path, late_write=False, post_at=None)
    assert dismiss.effects == []
    assert EffectKind.RECORD_WRITTEN in [e.kind for e in save.effects]


def test_compiled_a_store_change_before_the_next_action_keeps_the_placeholder(tmp_path):
    dismiss, _save = _compiled(tmp_path, late_write=True, post_at=None)
    assert [e.needs_operator_confirmation for e in dismiss.effects] == [True]


def test_compiled_a_captured_write_request_keeps_the_placeholder(tmp_path):
    dismiss, _save = _compiled(tmp_path, late_write=False, post_at=1.1)
    assert dismiss.risk == "irreversible"
    assert [e.needs_operator_confirmation for e in dismiss.effects] == [True]


# -- #1378: the recorder waits for a change it started ----------------------------------------


def _png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


class _SlowRoute:
    """A page whose URL changes on click and whose screen follows ``delay_s``
    later (MockMed's ``slow`` change, without a browser)."""

    def __init__(self, delay_s: float, *, changes_url: bool = True) -> None:
        self.delay_s = delay_s
        self.changes_url = changes_url
        self.url = "http://app/#tasks"
        self.page_title = "MockMed"
        self.viewport = (64, 48)
        self.clicked_at: Optional[float] = None

    def screenshot(self) -> bytes:
        late = self.clicked_at is not None and (
            time.monotonic() - self.clicked_at >= self.delay_s
        )
        return _png("navy" if late else "white")

    def click(self, x: int, y: int) -> None:
        self.clicked_at = time.monotonic()
        if self.changes_url:
            self.url = "http://app/#patient/p1"


def _after_frame(tmp_path: Path, backend: Any, **kw: Any) -> bytes:
    rec = Recorder(backend, tmp_path / "rec", settle_timeout_s=0.5, **kw)
    rec.click(10, 10)
    rec.finish()
    return (tmp_path / "rec" / "frames" / "0000_after.png").read_bytes()


def test_a_late_screen_after_a_route_change_is_waited_for(tmp_path):
    after = _after_frame(tmp_path, _SlowRoute(1.5))
    assert _phash(after) == _phash(_png("navy"))


def test_without_the_wait_the_old_screen_is_kept(tmp_path):
    # The race #1378 hit: two identical frames of the old screen.
    after = _after_frame(tmp_path, _SlowRoute(1.5), settle_pending_timeout_s=0.0)
    assert _pixels(after) == _pixels(_png("white"))


def test_the_wait_is_bounded_and_keeps_the_old_frame_on_timeout(tmp_path):
    started = time.monotonic()
    after = _after_frame(tmp_path, _SlowRoute(30.0), settle_pending_timeout_s=1.0)
    assert time.monotonic() - started < 5.0
    assert _pixels(after) == _pixels(_png("white"))


def test_a_click_that_starts_nothing_settles_at_once(tmp_path):
    started = time.monotonic()
    _after_frame(tmp_path, _SlowRoute(30.0, changes_url=False))
    assert time.monotonic() - started < 2.0


class _BusyPage(_SlowRoute):
    """No URL change; the page reports a request in flight until the screen
    arrives (a slow server behind a same-URL update)."""

    busy_before = False

    def page_quiescent(self, window_s: float) -> Optional[bool]:
        if self.clicked_at is None:
            return not self.busy_before
        return time.monotonic() - self.clicked_at >= self.delay_s


def test_a_request_in_flight_is_waited_for(tmp_path):
    after = _after_frame(tmp_path, _BusyPage(1.5, changes_url=False))
    assert _phash(after) == _phash(_png("navy"))


def test_a_page_busy_before_the_action_does_not_extend_the_wait(tmp_path):
    # Keepalive polling, a spinner, a chat widget: busy before and after.
    page = _BusyPage(1.5, changes_url=False)
    page.busy_before = True
    started = time.monotonic()
    after = _after_frame(tmp_path, page)
    assert time.monotonic() - started < 1.4
    assert _pixels(after) == _pixels(_png("white"))


def test_page_activity_alone_waits_less_than_a_route_change(tmp_path):
    from openadapt_flow.recorder import SETTLE_PENDING_PROBE_ONLY_S

    started = time.monotonic()
    _after_frame(tmp_path, _BusyPage(30.0, changes_url=False))
    took = time.monotonic() - started
    assert SETTLE_PENDING_PROBE_ONLY_S - 0.5 < took < SETTLE_PENDING_PROBE_ONLY_S + 2


class _HashLink(_SlowRoute):
    def click(self, x: int, y: int) -> None:
        self.clicked_at = time.monotonic()
        self.url = self.url.split("#")[0] + "#"


def test_a_bare_hash_link_is_not_a_route_change(tmp_path):
    # href="#": waiting would adopt whatever changes next (a fading toast).
    page = _HashLink(30.0)
    page.url = "http://app/list"
    started = time.monotonic()
    _after_frame(tmp_path, page)
    assert time.monotonic() - started < 2.0


def _png_with_row(row: int) -> bytes:
    image = Image.new("RGB", (640, 480), "white")
    for x in range(20, 620):
        image.putpixel((x, 100 + row), (0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class _SameLayout(_SlowRoute):
    """Next page of a list: same chrome, one line moved, rendered at once."""

    def screenshot(self) -> bytes:
        return _png_with_row(1 if self.clicked_at is not None else 0)


def test_a_small_change_the_phash_misses_is_not_the_old_screen(tmp_path):
    assert _phash(_png_with_row(0)) == _phash(_png_with_row(1))
    started = time.monotonic()
    after = _after_frame(tmp_path, _SameLayout(0.0))
    assert time.monotonic() - started < 2.0
    assert _pixels(after) == _pixels(_png_with_row(1))


# -- MockMed in a real browser ----------------------------------------------------------------


@pytest.fixture()
def mockmed():
    pytest.importorskip("playwright")
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    yield url, db
    stop()


def _launch(url: str):
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    try:
        return PlaywrightBackend.launch(url)
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"chromium unavailable: {exc}")


def _sign_in(page: Any) -> None:
    page.fill("#username", "nurse.demo")
    page.fill("#password", "mockmed-demo-pass")
    page.click("#signin")


def _center(page: Any, selector: str) -> tuple[int, int]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def test_mockmed_slow_open_click_records_the_patient_screen(mockmed, tmp_path):
    url, _db = mockmed
    backend, close = _launch(url + "?drift=slow&slowms=2000")
    try:
        page = backend.page
        _sign_in(page)
        page.wait_for_selector(".open-btn", timeout=10_000)
        rec = Recorder(backend, tmp_path / "rec", settle_timeout_s=1.0)
        rec.click(*_center(page, ".open-btn"))
        rec.finish()
    finally:
        close()
    event = json.loads((tmp_path / "rec" / "events.jsonl").read_text().splitlines()[0])
    before = (tmp_path / "rec" / "frames" / "0000_before.png").read_bytes()
    after = (tmp_path / "rec" / "frames" / "0000_after.png").read_bytes()
    assert event["url_after"] != event["url_before"]
    # The after frame is the patient screen, not the task list two seconds
    # before it rendered: the compiler mines visual checks from it, as it does
    # without the delay, and does not fall back to a bare url_changed.
    assert _pixels(after) != _pixels(before)


def test_mockmed_notice_dismiss_gets_no_record_written(mockmed, tmp_path):
    from openadapt_flow.compiler.compile import compile_recording

    url, db = mockmed
    backend, close = _launch(url + "?drift=notice&fault=ok")
    try:
        page = backend.page
        _sign_in(page)
        page.wait_for_selector("#notice-continue", timeout=10_000)
        rec = Recorder(
            backend,
            tmp_path / "rec",
            settle_timeout_s=1.0,
            system_of_record_reader=lambda: db.snapshot()["records"],
        )
        rec.click(*_center(page, "#notice-continue"))
        # Think time: the next snapshot must come a real gap later.
        time.sleep(1.2)
        rec.click(*_center(page, ".open-btn"))
        page.wait_for_selector("#new-encounter")
        rec.click(*_center(page, "#new-encounter"))
        page.wait_for_selector("#note")
        rec.click(*_center(page, "#note"))
        rec.type_text("Patient stable follow up in two weeks", param="note")
        rec.click(*_center(page, "#save-encounter"))
        rec.finish()
    finally:
        close()
    assert len(db.snapshot()["records"]) == 1
    wf = compile_recording(
        tmp_path / "rec", tmp_path / "bundle", name="notice", mine_effects=True
    )
    dismiss, save = wf.steps[0], wf.steps[-1]
    assert dismiss.effects == []
    assert any(
        e.kind is EffectKind.RECORD_WRITTEN and not e.needs_operator_confirmation
        for e in save.effects
    )
