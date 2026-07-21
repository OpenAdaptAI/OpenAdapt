"""Structural (DOM / UIA) ACTION rung — the deterministic TOP of the ladder.

The runtime is no longer "vision-only": where a backend owns a structured layer
(a browser DOM, a native UIA/AX tree) the resolution ladder re-finds the
recorded target as an ELEMENT and acts on it deterministically, falling back to
the visual rungs (template/ocr/geometry) only where structure is absent
(pixel-only substrates: RDP/Citrix/canvas). This mirrors the identity ladder,
which already prefers structured text.

Covered here:
  * resolver mechanics — structural tried FIRST, wins when it locates, falls
    through UNCHANGED to the visual ladder otherwise; risk gate treats it as
    the STRONGEST evidence (not below ocr);
  * the record/replay backend split (Windows UIA via a faked WAA session,
    Playwright DOM via importorskip);
  * compile-time capture of the locator;
  * the identity gate still fires on a structurally-resolved point (structure
    makes identity STRONGER, it never bypasses it);
  * the availability probe (structural vs visual under drift).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    Anchor,
    Step,
    StepResult,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.resolver import RUNG_ORDER, is_below_ocr, resolve

VIEWPORT = (300, 200)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _Match:
    def __init__(self, point, region, confidence=0.95):
        self.point = point
        self.region = region
        self.confidence = confidence


class _FakeVision:
    """Scripted find_template / find_text (never imports the real vision)."""

    def __init__(self):
        self.template_results = []
        self.text_results = {}
        self.template_calls = 0
        self.text_calls = 0

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        self.template_calls += 1
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def find_text(
        self,
        screen_png,
        text,
        *,
        region=None,
        min_ratio=0.8,
        raise_on_ambiguity=False,
    ):
        del raise_on_ambiguity
        self.text_calls += 1
        return self.text_results.get(text)


class _FakeStructural:
    """A structural-capable backend stub exposing locate_structural."""

    def __init__(self, handle, *, raises=False):
        self._handle = handle
        self._raises = raises
        self.calls = []

    def locate_structural(self, locator):
        self.calls.append(locator)
        if self._raises:
            raise RuntimeError("uia blew up")
        return self._handle


def _anchor(**kw):
    base = dict(
        template="templates/x.png",
        region=(100, 100, 40, 20),
        click_point=(120, 110),
        ocr_text="Open",
        structural=StructuralLocator(selector="#open-p1"),
    )
    base.update(kw)
    return Anchor(**base)


# ---------------------------------------------------------------------------
# resolver mechanics
# ---------------------------------------------------------------------------


def test_structural_is_top_of_ladder_and_not_below_ocr() -> None:
    assert RUNG_ORDER[0] == "structural"
    # Strongest evidence: an irreversible step is allowed to act on it.
    assert not is_below_ocr("structural")


def test_structural_rung_tried_first_and_wins() -> None:
    vision = _FakeVision()
    # A template match is *also* available — but structural must pre-empt it.
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    backend = _FakeStructural(StructuralHandle(point=(207, 133), confidence=1.0))
    screen = make_png()
    res = resolve(
        _anchor(),
        screen,
        vision,
        template_png=b"tpl",
        viewport=VIEWPORT,
        structural=backend,
    )
    assert res is not None
    resolution, region = res
    assert resolution.rung == "structural"
    assert resolution.point == (207, 133)
    assert resolution.confidence == 1.0
    assert resolution.structural_handle == backend._handle
    # The visual template rung was never consulted.
    assert vision.template_calls == 0
    # A region (for healing / clamping) is returned, sized like the anchor.
    assert region[2] == 40 and region[3] == 20
    assert backend.calls == [_anchor().structural]


def test_structural_miss_falls_through_to_visual_unchanged() -> None:
    vision = _FakeVision()
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    backend = _FakeStructural(None)  # element absent / ambiguous -> None
    res = resolve(
        _anchor(),
        make_png(),
        vision,
        template_png=b"tpl",
        viewport=VIEWPORT,
        structural=backend,
    )
    assert res is not None
    resolution, _ = res
    assert resolution.rung == "template"  # visual floor still works
    assert vision.template_calls == 1
    assert backend.calls  # structural WAS attempted first


def test_structural_exception_falls_through_no_crash() -> None:
    vision = _FakeVision()
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    backend = _FakeStructural(None, raises=True)
    res = resolve(
        _anchor(),
        make_png(),
        vision,
        template_png=b"tpl",
        viewport=VIEWPORT,
        structural=backend,
    )
    assert res is not None and res[0].rung == "template"


def test_structural_ambiguity_refusal_never_falls_through_to_pixels() -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    class Ambiguous:
        def locate_structural(self, locator):
            raise StructuralResolutionRefused("two exact Submit buttons")

    vision = _FakeVision()
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    with pytest.raises(StructuralResolutionRefused, match="two exact Submit"):
        resolve(
            _anchor(),
            make_png(),
            vision,
            template_png=b"tpl",
            viewport=VIEWPORT,
            structural=Ambiguous(),
        )
    assert vision.template_calls == 0


def test_pixel_only_backend_uses_visual_ladder() -> None:
    # structural=None models a pixel-only substrate (RDP/Citrix/canvas): the
    # visual ladder is used exactly as before.
    vision = _FakeVision()
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    res = resolve(
        _anchor(),
        make_png(),
        vision,
        template_png=b"tpl",
        viewport=VIEWPORT,
        structural=None,
    )
    assert res is not None and res[0].rung == "template"


def test_anchor_without_locator_never_calls_structural() -> None:
    vision = _FakeVision()
    vision.template_results = [_Match(point=(120, 110), region=(100, 100, 40, 20))]
    backend = _FakeStructural(StructuralHandle(point=(1, 1)))
    res = resolve(
        _anchor(structural=None),
        make_png(),
        vision,
        template_png=b"tpl",
        viewport=VIEWPORT,
        structural=backend,
    )
    assert res is not None and res[0].rung == "template"
    assert backend.calls == []  # no locator -> structural skipped


# ---------------------------------------------------------------------------
# identity gate STILL fires on a structurally-resolved point
# ---------------------------------------------------------------------------


class _IdentityAndStructuralBackend:
    """Backend that resolves structurally AND exposes structured identity."""

    def __init__(self, point, live_identity):
        self._point = point
        self._live = live_identity
        self.clicks = []

    @property
    def viewport(self):
        return VIEWPORT

    def screenshot(self):
        return make_png()

    def click(self, x, y, *, double=False):
        self.clicks.append((x, y))

    def type_text(self, text): ...

    def press(self, key): ...

    def scroll(self, dx, dy): ...

    def locate_structural(self, locator):
        return StructuralHandle(point=self._point, confidence=1.0)

    def structured_text_at(self, x, y):
        return self._live


def test_structural_resolution_still_faces_identity_gate() -> None:
    from openadapt_flow.runtime.replayer import Replayer

    recorded = "MG4408 Okafor, Philip 1966-01-17"
    sibling = "MG44O8 Okafor, Philip 1966-01-17"  # one-glyph different patient
    backend = _IdentityAndStructuralBackend((207, 133), sibling)
    rp = Replayer(backend, vision=_FakeVision(), poll_interval_s=0.01)
    step = Step(
        id="s1",
        intent="open patient",
        action=ActionKind.CLICK,
        anchor=_anchor(structured_identity=recorded),
    )
    # 1) resolution comes from the structural rung...
    resolution, region, err = rp._resolve_step(
        step, make_png(), Path("."), Workflow(name="wf", steps=[step])
    )
    assert err is None
    assert resolution is not None and resolution.rung == "structural"
    assert resolution.point == (207, 133)
    # 2) ...and the pre-click identity gate STILL runs on that point, catching
    #    the sibling (structure makes identity stronger, never bypasses it).
    check = rp._verify_identity(
        step, resolution, make_png(), {}, Workflow(name="wf"), None
    )
    assert check.status == "mismatch"
    assert check.mode == "structured"


# ---------------------------------------------------------------------------
# Windows UIA backend (faked WAA execute channel)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, text="", json_data=None, status=200):
        self.text = text
        self._json = json_data
        self.status_code = status

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append(json)
        if url.endswith("/execute_windows"):
            return self._resp
        return _Resp(text="not found", status=404)


class _RouteSession:
    def __init__(self, routes):
        self.routes = routes
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        for suffix, response in self.routes.items():
            if url.endswith(suffix):
                return response
        return _Resp(text="not found", status=404)


def _win_backend(resp):
    from openadapt_flow.backends.windows_backend import WindowsBackend

    return WindowsBackend(
        session=_FakeSession(resp), viewport=(800, 600), allow_legacy_exec=True
    )


def test_windows_structural_locator_at_parses_uia_dict() -> None:
    payload = (
        '<<OAFLOW_STRUCTURED>>{"automation_id": "open-p1", '
        '"role": "button", "name": "Open"}<<END_OAFLOW_STRUCTURED>>'
    )
    be = _win_backend(_Resp(text="log\n" + payload))
    loc = be.structural_locator_at(120, 110)
    assert isinstance(loc, StructuralLocator)
    assert loc.automation_id == "open-p1"
    assert loc.role == "button" and loc.name == "Open"


def test_windows_structural_locator_none_when_no_echo() -> None:
    be = _win_backend(_Resp(text=""))
    assert be.structural_locator_at(1, 1) is None


def test_windows_structural_locator_none_on_uia_null() -> None:
    payload = "<<OAFLOW_STRUCTURED>>null<<END_OAFLOW_STRUCTURED>>"
    be = _win_backend(_Resp(text=payload))
    assert be.structural_locator_at(1, 1) is None


def test_windows_locate_structural_parses_point() -> None:
    payload = "<<OAFLOW_STRUCTURED>>[412, 133]<<END_OAFLOW_STRUCTURED>>"
    be = _win_backend(_Resp(text=payload))
    handle = be.locate_structural(StructuralLocator(automation_id="open-p1"))
    assert isinstance(handle, StructuralHandle)
    assert handle.point == (412, 133)


def test_windows_locate_structural_none_on_null() -> None:
    payload = "<<OAFLOW_STRUCTURED>>null<<END_OAFLOW_STRUCTURED>>"
    be = _win_backend(_Resp(text=payload))
    assert be.locate_structural(StructuralLocator(automation_id="x")) is None


def test_windows_locate_structural_skips_server_without_ids() -> None:
    session = _FakeSession(_Resp(text=""))
    from openadapt_flow.backends.windows_backend import WindowsBackend

    be = WindowsBackend(session=session, viewport=(800, 600))
    # A locator with no automation_id and no role+name is unresolvable; the
    # backend must not even hit the server.
    assert be.locate_structural(StructuralLocator(selector="#x")) is None
    assert session.posted == []


def test_windows_typed_uia_unique_candidate_and_native_receipt() -> None:
    fingerprint = "a" * 64
    candidate = {
        "automation_id": "saveButton",
        "role": "button",
        "name": "Save",
        "window_name": "Patient Notes",
        "bounds": [10, 20, 110, 60],
        "point": [60, 40],
        "supported_operations": ["invoke"],
        "fingerprint": fingerprint,
    }
    session = _RouteSession(
        {
            "/uia/find": _Resp(
                json_data={
                    "status": "ok",
                    "match": "unique",
                    "candidate_count": 1,
                    "truncated": False,
                    "candidates": [candidate],
                }
            ),
            "/uia/act": _Resp(
                json_data={
                    "status": "ok",
                    "candidate_count": 1,
                    "receipt": {
                        "status": "delivered",
                        "receipt_id": "receipt-1",
                        "operation": "uia_invoke",
                        "native": True,
                        "target_fingerprint": fingerprint,
                        "delivered_at": "2026-07-17T00:00:00+00:00",
                        "outcome_verified": False,
                    },
                }
            ),
        }
    )
    from openadapt_flow.backends.windows_backend import WindowsBackend

    backend = WindowsBackend(session=session, viewport=(800, 600))
    locator = StructuralLocator(
        automation_id="saveButton",
        role="button",
        name="Save",
        window_name="Patient Notes",
    )
    handle = backend.locate_structural(locator)
    assert handle is not None
    assert handle.point == (60, 40)
    assert handle.region == (10, 20, 100, 40)
    assert handle.target_fingerprint == fingerprint
    receipt = backend.act_structural(locator, handle)
    assert receipt.operation == "uia_invoke"
    assert receipt.native is True
    assert receipt.outcome_verified is False
    assert not any(url.endswith("/execute_windows") for url, _ in session.posted)


def test_windows_typed_uia_ambiguous_candidates_refuse() -> None:
    from openadapt_flow.backend import StructuralResolutionRefused
    from openadapt_flow.backends.windows_backend import WindowsBackend

    session = _RouteSession(
        {
            "/uia/find": _Resp(
                json_data={
                    "status": "ok",
                    "match": "ambiguous",
                    "candidate_count": 2,
                    "truncated": False,
                    "candidates": [{}, {}],
                }
            )
        }
    )
    backend = WindowsBackend(session=session, viewport=(800, 600))
    with pytest.raises(StructuralResolutionRefused, match="candidate_count=2"):
        backend.locate_structural(StructuralLocator(automation_id="submit"))


def test_windows_typed_uia_stale_actuation_is_structural_refusal() -> None:
    from openadapt_flow.backend import StructuralResolutionRefused
    from openadapt_flow.backends.windows_backend import WindowsBackend

    fingerprint = "d" * 64
    session = _RouteSession(
        {
            "/uia/act": _Resp(
                status=409,
                json_data={
                    "status": "error",
                    "code": "stale_target",
                    "error": "UIA target changed after resolution",
                },
            )
        }
    )
    backend = WindowsBackend(session=session, viewport=(800, 600))
    with pytest.raises(StructuralResolutionRefused, match="stale_target"):
        backend.act_structural(
            StructuralLocator(automation_id="saveButton"),
            StructuralHandle(
                point=(60, 40),
                target_fingerprint=fingerprint,
                supported_operations=["invoke"],
            ),
        )


def test_replayer_marks_structural_ambiguity_as_safety_halt(tmp_path) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused
    from openadapt_flow.runtime.replayer import Replayer

    class AmbiguousBackend(_IdentityAndStructuralBackend):
        def locate_structural(self, locator):
            raise StructuralResolutionRefused("two exact Save buttons")

    vision = _FakeVision()
    vision.wait_settled = lambda _backend: make_png()
    backend = AmbiguousBackend((207, 133), "")
    step = Step(
        id="s1",
        intent="save",
        action=ActionKind.CLICK,
        anchor=_anchor(),
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        Workflow(name="wf", steps=[step]),
        bundle_dir=tmp_path,
        run_dir=tmp_path / "run",
    )
    assert report.success is False
    assert report.results[0].safety_halt is True
    assert "Structural safety refusal" in report.results[0].error
    assert backend.clicks == []
    assert vision.template_calls == 0


def test_replayer_records_native_delivery_separately_from_outcome(tmp_path) -> None:
    from openadapt_flow.runtime.replayer import Replayer

    fingerprint = "c" * 64

    class NativeBackend(_IdentityAndStructuralBackend):
        def locate_structural(self, locator):
            return StructuralHandle(
                point=self._point,
                target_fingerprint=fingerprint,
                supported_operations=["invoke"],
            )

        def act_structural(self, locator, handle, *, double=False):
            assert handle.target_fingerprint == fingerprint
            return ActionDeliveryReceipt(
                receipt_id="native-1",
                operation="uia_invoke",
                native=True,
                target_fingerprint=fingerprint,
                delivered_at="2026-07-17T00:00:00+00:00",
            )

    backend = NativeBackend((207, 133), "")
    replayer = Replayer(backend, vision=_FakeVision(), poll_interval_s=0.01)
    step = Step(
        id="s1",
        intent="save",
        action=ActionKind.CLICK,
        anchor=_anchor(),
    )
    resolution = resolve(
        step.anchor,
        make_png(),
        _FakeVision(),
        template_png=None,
        viewport=VIEWPORT,
        structural=backend,
    )[0]
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    error = replayer._act(
        step,
        resolution,
        {},
        workflow=Workflow(name="wf", steps=[step]),
        step_index=0,
        bundle_dir=tmp_path,
        before_png=make_png(),
        result=result,
    )
    assert error is None
    assert result.actuation == "uia"
    assert result.delivery_receipt is not None
    assert result.delivery_receipt.outcome_verified is False
    assert result.postconditions_ok is None
    assert result.effect_verified is None


# ---------------------------------------------------------------------------
# recorder captures the locator; compiler stores it on the anchor
# ---------------------------------------------------------------------------


class _RecordingStructuralBackend:
    def __init__(self):
        self._png = make_png()

    @property
    def viewport(self):
        return VIEWPORT

    def screenshot(self):
        return self._png

    def click(self, x, y, *, double=False): ...

    def type_text(self, text): ...

    def press(self, key): ...

    def scroll(self, dx, dy): ...

    def structural_locator_at(self, x, y):
        return StructuralLocator(selector="#open-p1", role="button", name="Open")


def test_recorder_captures_structural_locator(tmp_path) -> None:
    from openadapt_flow.recorder import Recorder

    rec = Recorder(
        _RecordingStructuralBackend(),
        tmp_path / "rec",
        settle_timeout_s=0.05,
        settle_interval_s=0.01,
    )
    rec.click(120, 110)
    rec.finish()
    lines = (tmp_path / "rec" / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[0])
    assert event["structural"] == {
        "selector": "#open-p1",
        "role": "button",
        "name": "Open",
    }


def test_compiler_stores_structural_locator(tmp_path) -> None:
    import cv2
    import numpy as np

    from openadapt_flow.compiler.compile import compile_recording

    recording = tmp_path / "rec"
    (recording / "frames").mkdir(parents=True)
    before = np.full((200, 300, 3), 240, np.uint8)
    cv2.rectangle(before, (100, 100), (160, 130), (200, 210, 255), -1)
    cv2.putText(
        before,
        "Open",
        (104, 122),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (10, 20, 40),
        1,
        cv2.LINE_AA,
    )
    after = before.copy()
    cv2.putText(
        after,
        "Saved",
        (40, 180),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    for suffix, img in (("before", before), ("after", after)):
        ok, buf = cv2.imencode(".png", img)
        assert ok
        (recording / "frames" / f"0000_{suffix}.png").write_bytes(buf.tobytes())
    event = {
        "i": 0,
        "kind": "click",
        "x": 130,
        "y": 115,
        "t": 1.0,
        "structural": {"selector": "#open-p1", "role": "button", "name": "Open"},
    }
    (recording / "events.jsonl").write_text(json.dumps(event) + "\n")
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-1",
                "created_at": "2026-07-13T00:00:00+00:00",
                "viewport": [300, 200],
                "app_url": "http://x/",
                "params": {},
            }
        )
    )

    wf = compile_recording(recording, tmp_path / "bundle", name="wf")
    anchor = wf.steps[0].anchor
    assert anchor is not None and anchor.structural is not None
    assert anchor.structural.selector == "#open-p1"
    assert anchor.structural.role == "button"


# ---------------------------------------------------------------------------
# Playwright DOM end-to-end (real browser; skipped when unavailable)
# ---------------------------------------------------------------------------


def _pw_backend():
    pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    return PlaywrightBackend


def test_playwright_structural_locator_and_locate_roundtrip() -> None:
    from openadapt_flow.validation.structural_action import build_html

    PlaywrightBackend = _pw_backend()
    backend, close = PlaywrightBackend.launch("about:blank", headless=True)
    try:
        backend.page.set_content(build_html(6, drift=False))
        backend.page.wait_for_timeout(50)
        box = backend.page.locator("#open-p2").bounding_box()
        cx, cy = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        loc = backend.structural_locator_at(cx, cy)
        assert loc is not None and loc.selector == "#open-p2"
        handle = backend.locate_structural(loc)
        assert handle is not None
        px, py = handle.point
        assert box["x"] <= px <= box["x"] + box["width"]
        assert box["y"] <= py <= box["y"] + box["height"]
    finally:
        close()


def test_playwright_structural_survives_drift_where_visual_fails() -> None:
    from openadapt_flow.validation.structural_action import run_probe

    _pw_backend()
    report = run_probe(n=9, headless=True)
    # Structural resolves every target whose id is still in the DOM...
    assert report["structural_ok"] == report["n"]
    # ...and beats the visual ladder under drift (the whole thesis).
    assert report["structural_ok"] > report["visual_ok"]
    drifted = [t for t in report["targets"] if t["drifted"]]
    assert drifted, "probe must include drifted targets"
    assert all(t["structural_ok"] for t in drifted)
    assert not any(t["visual_ok"] for t in drifted)
