"""Unit tests for the Replayer (openadapt_flow.runtime.replayer).

Backend and vision are both faked — no Playwright, no openadapt_flow.vision.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Landmark,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    Workflow,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import AmbiguousOcrMatchError

VIEWPORT = (300, 200)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Match:
    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class FakeVision:
    """Scripted vision namespace covering everything the Replayer touches."""

    def __init__(self):
        self.template_results: list = []
        self.template_calls: list = []
        self.structural_template_results: list = []
        self.structural_template_calls: list = []
        # Template-crop bytes the resolver handed each find_template call
        # (the decrypted in-memory crop for an encrypted bundle).
        self.template_png_calls: list = []
        self.text_results: dict = {}
        self.text_calls: list = []
        self.ocr_lines: list = []
        # Scripted per-call OCR results (popped per call); when exhausted,
        # falls back to the static ocr_lines.
        self.ocr_results: list = []
        self.phash_value = "aa"
        self.phash_dist = 0
        self.settle_count = 0
        # Typed-input verification: scripted pixels_changed results (popped
        # per call); default True = "the typed text visibly landed".
        self.pixels_changed_results: list = []
        self.pixels_changed_calls: list = []

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
        self.template_calls.append(search_region)
        self.template_png_calls.append(template_png)
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def find_structural_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.8,
    ):
        self.structural_template_calls.append(search_region)
        if self.structural_template_results:
            return self.structural_template_results.pop(0)
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
        self.text_calls.append(text)
        result = self.text_results.get(text)
        if isinstance(result, list):
            return result.pop(0) if result else None
        return result

    def text_present(self, screen_png, text, *, region=None, min_ratio=0.8):
        # Same script as find_text (postconditions use the tolerant
        # presence check; tests script both through text_results).
        return (
            self.find_text(screen_png, text, region=region, min_ratio=min_ratio)
            is not None
        )

    def ocr(self, screen_png, *, region=None):
        if self.ocr_results:
            return self.ocr_results.pop(0)
        return self.ocr_lines

    def pixels_changed(
        self, before_png, after_png, *, region=None, threshold=20, min_pixels=4
    ):
        self.pixels_changed_calls.append(region)
        if self.pixels_changed_results:
            return self.pixels_changed_results.pop(0)
        return True

    def phash_png(self, png, region=None):
        return self.phash_value

    def phash_distance(self, a, b):
        return self.phash_dist

    def wait_settled(self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0):
        self.settle_count += 1
        return backend.screenshot()


class FakeBackend:
    def __init__(
        self,
        frame=None,
        viewport=VIEWPORT,
        *,
        text_value_supported=True,
        type_accept_results=None,
    ):
        self._frame = frame if frame is not None else make_png(viewport)
        self._viewport = viewport
        self.actions: list = []
        self._text_value_supported = text_value_supported
        self._text_value = ""
        self._select_all = False
        self._type_accept_results = list(type_accept_results or [])

    @property
    def viewport(self):
        return self._viewport

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))

    def type_text(self, text):
        self.actions.append(("type", text))
        accepted = (
            self._type_accept_results.pop(0) if self._type_accept_results else True
        )
        if accepted:
            self._text_value = text if self._select_all else self._text_value + text
        self._select_all = False

    def press(self, key):
        self.actions.append(("press", key))
        if key == "ControlOrMeta+a":
            self._select_all = True

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy))

    def text_value_at(self, x, y):
        return self._text_value if self._text_value_supported else None

    def focused_text_value(self):
        return self._text_value if self._text_value_supported else None


def click_step(
    step_id="s1",
    *,
    risk="reversible",
    expect=(),
    template="templates/btn.png",
    ocr_text="Save",
    landmarks=(),
) -> Step:
    return Step(
        id=step_id,
        intent=f"click '{ocr_text or step_id}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=template,
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text=ocr_text,
            landmarks=list(landmarks),
        ),
        expect=list(expect),
        risk=risk,
    )


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


def test_happy_path_click_then_param_type(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
                    )
                ]
            ),
            Step(id="s2", intent="type note", action=ActionKind.TYPE, param="note"),
        ],
    )
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    report = replayer.run(
        workflow,
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [
        ("click", 110, 105, False),
        ("type", "hello world"),
    ]
    assert report.rung_counts == {"template": 1}
    assert report.heal_count == 0
    assert report.model_calls == 0
    assert report.total_ms > 0
    assert report.params == {"note": "hello world"}
    # Run directory artifacts.
    assert (run_dir / "report.json").is_file()
    loaded = RunReport.model_validate(json.loads((run_dir / "report.json").read_text()))
    assert loaded.success is True
    for step_id in ("s1", "s2"):
        assert (run_dir / f"steps/{step_id}_before.png").is_file()
        assert (run_dir / f"steps/{step_id}_after.png").is_file()
    assert report.results[0].before_png == "steps/s1_before.png"
    assert report.results[0].after_png == "steps/s1_after.png"
    assert report.results[0].postconditions_ok is True
    assert report.results[0].elapsed_ms > 0


def test_missing_param_fails_step_and_aborts_run(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note"),
            Step(id="k1", intent="press enter", action=ActionKind.KEY, key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, params={}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1  # run aborted; k1 never executed
    assert "note" in report.results[0].error
    assert backend.actions == []  # nothing typed, nothing pressed


def test_ocr_ambiguity_is_an_operator_visible_safety_halt(bundle, run_dir):
    """Repeated OCR targets never become a generic miss/retry or an action."""

    class AmbiguousTargetVision(FakeVision):
        def find_text(
            self,
            screen_png,
            text,
            *,
            region=None,
            min_ratio=0.8,
            raise_on_ambiguity=False,
        ):
            del screen_png, text, region, min_ratio
            if raise_on_ambiguity:
                raise AmbiguousOcrMatchError("two OCR target candidates qualify")
            return None

    backend = FakeBackend()
    report = Replayer(
        backend, vision=AmbiguousTargetVision(), poll_interval_s=0.01
    ).run(
        Workflow(name="wf", steps=[click_step()]),
        bundle_dir=bundle,
        run_dir=run_dir,
    )

    assert report.success is False
    assert report.results[0].safety_halt is True
    assert "OCR safety refusal" in report.results[0].error
    assert "no action was admitted" in report.results[0].error
    assert backend.actions == []


def test_param_overrides_recorded_literal_text(bundle, run_dir):
    """Compiled TYPE steps carry BOTH the recorded literal (step.text) and
    the param name; the runtime param value must win over the literal."""
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        params={"note": "recorded value"},
        steps=[
            Step(
                id="t1",
                intent="type <note>",
                action=ActionKind.TYPE,
                text="recorded value",
                param="note",
            )
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"note": "runtime value"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("type", "runtime value")]
    assert report.params == {"note": "runtime value"}


def test_workflow_params_are_replay_defaults(bundle, run_dir):
    """workflow.params holds recorded example/default values; a replay with
    no explicit params must fall back to them instead of failing."""
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        params={"note": "recorded default"},
        steps=[
            Step(id="t1", intent="type <note>", action=ActionKind.TYPE, param="note")
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("type", "recorded default")]
    assert report.params == {"note": "recorded default"}


def test_literal_text_type_step(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            Step(
                id="t1",
                intent="type literal",
                action=ActionKind.TYPE,
                text="fixed text",
            )
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("type", "fixed text")]


def test_risk_gate_blocks_irreversible_step_below_ocr(bundle, run_dir):
    vision = FakeVision()
    # Template file exists but never matches; no ocr_text; landmark resolves
    # -> geometry rung, which is below ocr.
    vision.text_results = {
        "Note": Match(point=(100, 50), region=(80, 45, 40, 10), confidence=0.7)
    }
    backend = FakeBackend()
    step = click_step(
        step_id="danger",
        risk="irreversible",
        ocr_text=None,
        landmarks=[Landmark(relation="left_of", ocr_text="Note", distance_px=40)],
    )
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    result = report.results[0]
    assert result.ok is False
    assert "human confirmation" in result.error
    assert "danger" in result.error
    assert backend.actions == []  # DID NOT act
    assert result.resolution is not None  # resolution recorded for the report
    assert result.resolution.rung == "geometry"
    assert report.rung_counts == {}  # failed steps don't count
    assert result.heal is None


def test_postcondition_passes_after_resettle_retry(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    # First check fails (timeout_s=0 expires immediately); the single
    # re-settle retry then sees the text.
    vision.text_results = {
        "Done": [None, Match(point=(10, 10), region=(5, 5, 20, 8), confidence=0.9)]
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Done", timeout_s=0.0
                    )
                ]
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].postconditions_ok is True
    # settle before + settle after action + one re-settle retry
    assert vision.settle_count == 3


def test_postcondition_polling_passes_within_timeout(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {
        "Done": [
            None,
            None,
            Match(point=(10, 10), region=(5, 5, 20, 8), confidence=0.9),
        ]
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Done", timeout_s=1.0
                    )
                ]
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # Passed inside the polling loop -> no re-settle retry needed.
    assert vision.settle_count == 2


def test_semantic_drift_aborts_run_with_named_step(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {"Banner": None}  # never appears
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Banner",
                        timeout_s=0.05,
                    )
                ]
            ),
            Step(id="s2", intent="never runs", action=ActionKind.KEY, key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1  # s2 never ran
    result = report.results[0]
    assert result.postconditions_ok is False
    assert "s1" in result.error
    assert "drift" in result.error
    # The report must embed the step's before/after screenshots.
    assert result.before_png == "steps/s1_before.png"
    assert result.after_png == "steps/s1_after.png"
    assert (run_dir / result.before_png).is_file()
    assert (run_dir / result.after_png).is_file()
    assert ("press", "Enter") not in backend.actions
    saved = json.loads((run_dir / "report.json").read_text())
    assert saved["success"] is False


def test_region_stable_postcondition_uses_phash(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.phash_dist = 4  # within tolerance of 8
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.REGION_STABLE,
                        region=(0, 0, 40, 30),
                        phash="deadbeef",
                        phash_tolerance=8,
                        timeout_s=0.2,
                    )
                ]
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True

    # Now exceed the tolerance: the same postcondition must fail.
    vision2 = FakeVision()
    vision2.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision2.phash_dist = 20
    report2 = Replayer(FakeBackend(), vision=vision2, poll_interval_s=0.01).run(
        Workflow(
            name="wf",
            steps=[
                click_step(
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.REGION_STABLE,
                            region=(0, 0, 40, 30),
                            phash="deadbeef",
                            phash_tolerance=8,
                            timeout_s=0.05,
                        )
                    ]
                )
            ],
        ),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report2.success is False


def test_wait_step_only_settles(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[Step(id="w1", intent="wait for app", action=ActionKind.WAIT)],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == []  # no input injected
    assert vision.settle_count == 2  # settle before + settle after


def test_region_stable_template_tolerates_layout_shift(bundle, run_dir):
    """A REGION_STABLE postcondition with a template crop passes when the
    expected content is found near the recorded region, even though the
    exact-position phash misses (small layout shift between runs)."""
    vision = FakeVision()
    vision.phash_dist = 99  # exact-position hash always misses
    vision.template_results = [
        Match(point=(120, 60), region=(80, 48, 100, 40), confidence=0.97)
    ]
    (bundle / "templates" / "pc.png").write_bytes(make_png((100, 40)))
    backend = FakeBackend()
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=(80, 40, 100, 40),
        phash="aa",
        template="templates/pc.png",
        timeout_s=0.2,
    )
    workflow = Workflow(
        name="wf",
        steps=[
            Step(
                id="k1",
                intent="press enter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[pc],
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # The template search was constrained to the padded region.
    assert vision.template_calls, "find_template was not consulted"
    assert vision.template_calls[0] is not None


def test_region_stable_structural_template_tolerates_theme(bundle, run_dir):
    """A strict edge-map match rescues palette drift when grayscale and the
    exact-position pHash miss; the REGION_STABLE evidence remains armed."""
    vision = FakeVision()
    vision.phash_dist = 99
    vision.structural_template_results = [
        Match(point=(120, 60), region=(80, 48, 100, 40), confidence=0.86)
    ]
    (bundle / "templates" / "pc.png").write_bytes(make_png((100, 40)))
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=(80, 40, 100, 40),
        phash="aa",
        template="templates/pc.png",
        timeout_s=0.2,
    )
    workflow = Workflow(
        name="wf",
        steps=[
            Step(
                id="k1",
                intent="press enter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[pc],
            )
        ],
    )

    report = Replayer(FakeBackend(), vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is True
    assert vision.template_calls
    assert vision.structural_template_calls


def test_region_stable_fails_when_template_and_phash_miss(bundle, run_dir):
    vision = FakeVision()
    vision.phash_dist = 99
    (bundle / "templates" / "pc.png").write_bytes(make_png((100, 40)))
    backend = FakeBackend()
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=(80, 40, 100, 40),
        phash="aa",
        template="templates/pc.png",
        timeout_s=0.2,
    )
    workflow = Workflow(
        name="wf",
        steps=[
            Step(
                id="k1",
                intent="press enter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[pc],
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "region_stable" in report.results[0].error


def test_scroll_step_scrolls_backend(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            Step(
                id="sc1",
                intent="scroll by (0, 400)",
                action=ActionKind.SCROLL,
                scroll_dx=0,
                scroll_dy=400,
            ),
            Step(
                id="sc2",
                intent="scroll by (-30, -120)",
                action=ActionKind.SCROLL,
                scroll_dx=-30,
                scroll_dy=-120,
            ),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("scroll", 0, 400), ("scroll", -30, -120)]
    # No anchor -> no resolution, no heal.
    assert report.results[0].resolution is None
    assert report.heal_count == 0


def scroll_step(step_id="sc1", dx=0, dy=400) -> Step:
    return Step(
        id=step_id,
        intent=f"scroll by ({dx}, {dy})",
        action=ActionKind.SCROLL,
        scroll_dx=dx,
        scroll_dy=dy,
    )


def test_closed_loop_scroll_stops_when_next_anchor_resolves(bundle, run_dir):
    """A SCROLL step followed by an anchored step scrolls incrementally by
    the recorded delta until that anchor resolves on a settled frame."""
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    # Probe on the pre-scroll frame misses (local+global), the probe after
    # the first scroll resolves; the click step then resolves for itself.
    vision.template_results = [None, None, target, target]
    backend = FakeBackend()
    workflow = Workflow(name="wf", steps=[scroll_step(), click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [
        ("scroll", 0, 400),
        ("click", 110, 105, False),
    ]
    # The scroll step itself records no resolution (the probe belongs to the
    # next step's anchor); only the click counts a rung.
    assert report.results[0].resolution is None
    assert report.rung_counts == {"template": 1}


def test_closed_loop_scroll_noops_when_anchor_already_in_view(bundle, run_dir):
    """The pre-scroll probe resolving means the target is already on screen:
    the SCROLL step must not scroll at all."""
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    vision.template_results = [target, target]  # probe, then click resolve
    backend = FakeBackend()
    workflow = Workflow(name="wf", steps=[scroll_step(), click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("click", 110, 105, False)]


def test_closed_loop_scroll_requires_armed_target_identity(
    bundle, run_dir, monkeypatch
):
    """A generic crop at the wrong form row cannot make scroll readiness pass."""
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
    # The generic crop resolves both before and after the scroll, then again
    # for the actual click. Identity is what distinguishes wrong-row from ready.
    vision.template_results = [target, target, target]
    backend = FakeBackend()
    click = click_step()
    assert click.anchor is not None
    click.anchor.context_text = "Synthetic Contact Address"
    click.identity_armed = True
    workflow = Workflow(name="wf", steps=[scroll_step(), click])
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    checks = iter(
        [
            IdentityCheck(status="mismatch", expected="target", observed="wrong"),
            IdentityCheck(status="verified", expected="target", observed="target"),
            IdentityCheck(status="verified", expected="target", observed="target"),
        ]
    )
    monkeypatch.setattr(
        replayer, "_verify_identity", lambda *args, **kwargs: next(checks)
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert backend.actions == [
        ("scroll", 0, 400),
        ("click", 110, 105, False),
    ]


def test_closed_loop_scroll_budget_exhaustion_fails_loudly(bundle, run_dir):
    """When the anchor never resolves and no further SCROLL step follows,
    the loop stops at ~2.5x the recorded distance and fails the run,
    naming the anchor that never came into view."""
    vision = FakeVision()  # every probe misses
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[scroll_step(dy=-400), click_step(step_id="pencil")],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1  # aborted at the scroll step
    error = report.results[0].error
    assert "budget" in error
    assert "pencil" in error  # names the anchor that never resolved
    # Budget 2.5 x 400px allows exactly two 400px gestures, both upward
    # (direction comes from the recorded delta).
    assert backend.actions == [("scroll", 0, -400), ("scroll", 0, -400)]


def test_consecutive_scroll_steps_share_the_loop(bundle, run_dir):
    """A SCROLL step exhausting its own budget does NOT fail when the next
    step is another SCROLL step: that step inherits the loop (probe-first),
    so a recorded run of N scrolls has a combined ~2.5x budget."""
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    # 4 failed probes (2 template calls each: local + global), then the
    # second scroll step's first post-scroll probe resolves, then the click
    # resolves for itself.
    vision.template_results = [None] * 8 + [target, target]
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[scroll_step("sc1"), scroll_step("sc2"), click_step()],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # sc1 scrolls twice (budget exhausted, handed over), sc2 scrolls once
    # (probe resolves), then the click acts.
    assert backend.actions == [
        ("scroll", 0, 400),
        ("scroll", 0, 400),
        ("scroll", 0, 400),
        ("click", 110, 105, False),
    ]
    assert report.results[0].ok and report.results[1].ok


def test_scroll_without_later_anchor_stays_open_loop(bundle, run_dir):
    """No later anchored step -> nothing to probe: the recorded delta
    replays once, exactly as recorded (open-loop fallback)."""
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            scroll_step(dx=-30, dy=-120),
            Step(id="k1", intent="press enter", action=ActionKind.KEY, key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("scroll", -30, -120), ("press", "Enter")]
    assert vision.template_calls == []  # no probe without an anchor


def test_key_step_presses_key(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[Step(id="k1", intent="press enter", action=ActionKind.KEY, key="Enter")],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("press", "Enter")]


def test_unresolvable_click_step_fails_without_acting(bundle, run_dir):
    vision = FakeVision()  # everything misses
    backend = FakeBackend()
    step = click_step()
    step.timeout_s = 0.2  # keep the resolution retry budget short in tests
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    assert "resolve" in report.results[0].error.lower()
    assert "s1" in report.results[0].error


def test_resolution_retries_until_target_appears(bundle, run_dir):
    """A ladder miss on a stale frame retries with fresh settled frames
    until step.timeout_s (Step.timeout_s is the resolution retry budget)."""
    vision = FakeVision()
    # First OCR lookups miss (still-loading screen); then the label appears.
    vision.text_results["Save"] = [
        None,
        None,
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95),
    ]
    backend = FakeBackend()
    step = click_step(template="templates/missing.png")  # no template rungs
    step.timeout_s = 5.0
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].resolution.rung == "ocr"
    assert vision.settle_count >= 3  # initial + at least two retries


# -- identity verification (pre-click context band) ---------------------------


class OcrLine:
    # Default region sits on the resolved point's row (resolving_vision
    # resolves to (110, 105)): identity verification reads only the lines
    # of the point's OWN text row (identity.lines_near_point), so fakes
    # must place their lines there to be seen — and OUTSIDE the target's
    # own crop (the anchor region (100, 100, 50, 20) translated to the
    # resolved point), which the replayer excludes from the live band
    # exactly as the compiler excluded it from the recorded band.
    def __init__(self, text, region=(160, 95, 240, 20), confidence=0.9):
        self.text = text
        self.region = region
        self.confidence = confidence


def context_click_step(context, **kwargs) -> Step:
    step = click_step(**kwargs)
    step.anchor.context_text = context
    return step


def resolving_vision() -> FakeVision:
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
    ]
    return vision


def test_identity_mismatch_refuses_to_click(bundle, run_dir):
    """The ladder resolves (pixel-identical imposter at the recorded spot)
    but the live band text names a different entity: the run must halt
    WITHOUT clicking — this is the silent wrong-patient fix."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Taylor Duplicate Knee pain referral High")]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []  # never clicked
    result = report.results[0]
    assert result.identity is not None
    assert result.identity.status == "mismatch"
    assert result.identity.coverage < 0.8
    assert "Identity check failed" in result.error
    assert "refusing to act" in result.error


def test_identity_verified_clicks_normally(bundle, run_dir):
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Jane Sample Knee pain referral High")]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].identity.status == "verified"
    assert report.results[0].identity.mode == "context"


def test_identity_param_mode_reanchors_on_run_value(bundle, run_dir):
    """When the recorded band embeds a parameter's demo value (a
    parameterized TARGET, e.g. the patient row), the run's value is
    substituted into the recorded band and the WHOLE substituted band is
    verified — the recorded row text describes the demo's entity, but its
    non-param residue must still match."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Open chart for Susan (active)")]
    backend = FakeBackend()
    step = context_click_step("Open chart for Phil (active)")
    workflow = Workflow(name="wf", params={"patient": "Phil"}, steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"patient": "Susan"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    identity = report.results[0].identity
    assert identity.status == "verified"
    assert identity.mode == "param"
    assert identity.param == "patient"


def test_identity_param_mode_value_alone_does_not_verify(bundle, run_dir):
    """FLIPPED 2026-07-09 (adversarial review, B2/P1a): previously ANY band
    containing the run's value verified — a messages row mentioning 'Susan'
    passed for patient 'Susan', and the value-only rule let a short param
    demo value disarm the whole check. Now the band's non-param residue
    must match too; when the entity's own row text varies with the entity
    (a search result carries the surname), the run halts — disclosed in
    LIMITS.md as availability cost, never a wrong click."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Underwood, Susan Ardmore")]
    backend = FakeBackend()
    step = context_click_step("Belford, Phil MRN A12")
    workflow = Workflow(name="wf", params={"patient": "Phil"}, steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"patient": "Susan"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert backend.actions == []  # never clicked
    identity = report.results[0].identity
    assert identity.status == "mismatch"
    assert identity.mode == "param"


def test_identity_one_row_off_resolution_mismatches(bundle, run_dir):
    """The 64px band spans 2-3 dense-table rows: a resolution one row off
    must be judged by ITS row's text, not verified on text bleed from the
    adjacent true row. The fake resolves to y=105; the recorded row's text
    sits one row up (y~75) and the resolved row is a different entity."""
    vision = resolving_vision()
    vision.ocr_lines = [
        OcrLine("Jane Sample Knee pain referral High", region=(160, 65, 240, 20)),
        OcrLine("Taylor Duplicate Knee pain referral High", region=(160, 95, 240, 20)),
    ]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    assert report.results[0].identity.status == "mismatch"


def test_identity_gates_anchored_type_focusing_click(bundle, run_dir):
    """An anchored TYPE step's focusing click is a click like any other:
    a wrong-entity band must refuse before the focusing click fires (and
    before anything is typed)."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Taylor Duplicate Knee pain referral High")]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    step.action = ActionKind.TYPE
    step.text = "hello"
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []  # no focusing click, nothing typed
    assert report.results[0].identity.status == "mismatch"


def test_identity_param_mode_mismatch_halts(bundle, run_dir):
    """Param mode: the live band names NEITHER the recorded nor the run's
    entity — halt without clicking."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Getting, Robert Third")]
    backend = FakeBackend()
    step = context_click_step("Belford, Phil MRN A12")
    workflow = Workflow(name="wf", params={"patient": "Phil"}, steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"patient": "Susan"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert backend.actions == []
    identity = report.results[0].identity
    assert identity.status == "mismatch"
    assert identity.mode == "param"
    assert "patient" in report.results[0].error


def test_identity_abstain_halts_irreversible_step(bundle, run_dir):
    """8th wrong-patient reopening: the live band's name+DOB match but it
    rests on a glyph-confusable MRN (MG4408) OCR may have collapsed. The OCR
    tier ABSTAINS (cannot certify SAME nor assert DIFFERENT), so an
    IRREVERSIBLE step HALTS without clicking -- the wrong patient is never
    opened, and the reason names the collapse."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("MG4408 Okafor, Philip 1966-01-17 M Active")]
    backend = FakeBackend()
    step = context_click_step(
        "MG4408 Okafor, Philip 1966-01-17 M Active", risk="irreversible"
    )
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []  # never clicked
    result = report.results[0]
    assert result.identity.status == "abstain"
    assert "irreversible" in result.error
    assert "human confirmation" in result.error
    assert "glyph-confusable" in result.error


def test_identity_abstain_proceeds_flagged_when_reversible(bundle, run_dir):
    """The abstain is disclosed, never silent: a REVERSIBLE step proceeds on
    positional evidence but the result carries the abstain flag (recoverable,
    and the report shows the id ⚠ marker)."""
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("MG4408 Okafor, Philip 1966-01-17 M Active")]
    backend = FakeBackend()
    step = context_click_step(
        "MG4408 Okafor, Philip 1966-01-17 M Active", risk="reversible"
    )
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].identity.status == "abstain"


def test_identity_unreadable_proceeds_flagged_when_reversible(bundle, run_dir):
    """No usable text in the live band: fall back to current behavior for
    reversible steps, but the result carries the unreadable flag (the
    residual gap is disclosed, never silent)."""
    vision = resolving_vision()
    vision.ocr_lines = []  # band OCR finds nothing
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].identity.status == "unreadable"


@pytest.mark.parametrize(
    ("live_text", "status"),
    [
        ([], "unreadable"),
        ([OcrLine("MG4408 Okafor, Philip 1966-01-17 M Active")], "abstain"),
    ],
)
def test_governed_run_requires_affirmative_identity_for_reversible_navigation(
    bundle, run_dir, live_text, status
):
    """An armed entity-navigation step must verify, not merely be reversible."""
    context = (
        "MG4408 Okafor, Philip 1966-01-17 M Active"
        if status == "abstain"
        else "Jane Sample Knee pain referral High"
    )
    vision = resolving_vision()
    vision.ocr_lines = live_text
    backend = FakeBackend()
    step = context_click_step(context)
    workflow = Workflow(name="governed_identity", steps=[step])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="test",
        required_identity_step_ids=[step.id],
    )

    report = Replayer(
        backend,
        vision=vision,
        governed_authorization=authorization,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].identity.status == status
    assert "governed run policy" in (report.results[0].error or "")


def test_identity_band_excludes_targets_own_label(bundle, run_dir):
    """The live band must be extracted like the recorded band: the
    target's own label (a line inside the anchor crop translated to the
    resolved point) is excluded, so it neither verifies by itself nor
    trips the unexplained-name budget as an observed-side extra."""
    vision = resolving_vision()
    vision.ocr_lines = [
        # The label itself: inside the anchor crop (100, 100, 50, 20)
        # at the resolved point — must be excluded from the band.
        OcrLine("Belford,", region=(102, 98, 46, 16)),
        OcrLine("Jane Sample Knee pain referral High"),
    ]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].identity.status == "verified"


def test_identity_band_excludes_volatile_lines_at_replay(bundle, run_dir):
    """A live clock/date cell on the resolved row (volatile relative to
    the replay date) is dropped from the observed band, mirroring the
    compiler's record-time volatility filter — it must not register as
    unexplained observed tokens ('Jul' is name-shaped to OCR)."""
    from datetime import date

    today = date.today()
    vision = resolving_vision()
    vision.ocr_lines = [
        OcrLine("Jane Sample Knee pain referral High"),
        OcrLine(
            f"{today.strftime('%b')} {today.day}, {today.year} 3:01",
            region=(160, 96, 100, 18),
        ),
    ]
    backend = FakeBackend()
    step = context_click_step("Jane Sample Knee pain referral High")
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].identity.status == "verified"


def test_identity_unreadable_blocks_irreversible_step(bundle, run_dir):
    vision = resolving_vision()
    vision.ocr_lines = []
    backend = FakeBackend()
    step = context_click_step(
        "Jane Sample Knee pain referral High", risk="irreversible"
    )
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    assert report.results[0].identity.status == "unreadable"
    assert "identity could not be read" in report.results[0].error


def test_no_identity_check_without_recorded_context(bundle, run_dir):
    """Anchors without context_text (older bundles, targets with no row
    text) behave exactly as before — no check, no flag."""
    vision = resolving_vision()
    backend = FakeBackend()
    report = Replayer(backend, vision=vision).run(
        Workflow(name="wf", steps=[click_step()]),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[0].identity is None


# -- typed-input verification ---------------------------------------------------


def test_type_verification_passes_with_exact_field_readback(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(),
            Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[1].input_verified is True
    assert report.results[1].input_retried is False
    # Exact structural readback is authoritative; no OCR/pixel guess is needed.
    assert vision.pixels_changed_calls == []


def test_type_verification_prefers_exact_structural_field_region() -> None:
    """Wide native text fields must be observed across their full UIA bounds.

    A fixed crop around the center can exclude text rendered at the field's
    left edge (the real WinForms search-box failure). UIA bounds are padded,
    clamped, and used directly when available.
    """
    backend = FakeBackend(viewport=(1200, 800))
    replayer = Replayer(backend, vision=FakeVision())
    assert replayer._field_region((520, 109), structural_region=(40, 87, 960, 44)) == (
        24,
        71,
        992,
        76,
    )


def test_type_verification_refocuses_and_retypes_once(bundle, run_dir):
    """Focus theft: the first attempt lands nowhere visible; the retry
    re-clicks the field, selects all (replace, never append), retypes, and
    the run recovers — the silent empty-note fix."""
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend(type_accept_results=[False, True])
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(),
            Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [
        ("click", 110, 105, False),
        ("type", "hello world"),
        ("click", 110, 105, False),  # refocus
        ("press", "ControlOrMeta+a"),  # replace, don't append
        ("type", "hello world"),  # retype
    ]
    assert report.results[1].input_verified is True
    assert report.results[1].input_retried is True


def test_type_verification_failure_halts_run(bundle, run_dir):
    """Nothing landed even after the retry: the run must halt with an
    accurate reason instead of reporting success with lost input."""
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend(type_accept_results=[False, False])
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(),
            Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note"),
            Step(id="k1", intent="press enter", action=ActionKind.KEY, key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow,
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert len(report.results) == 2  # k1 never ran
    result = report.results[1]
    assert result.ok is False
    assert result.input_verified is False
    assert result.input_retried is True
    assert "Typed input could not be verified" in result.error
    assert ("press", "Enter") not in backend.actions


def _type_workflow() -> Workflow:
    return Workflow(
        name="wf",
        steps=[
            click_step(),
            Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note"),
        ],
    )


def _type_vision() -> FakeVision:
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    return vision


def test_type_verification_ocr_reads_the_value(bundle, run_dir):
    """The OCR layer is the decider for OCR-able values: the typed text is
    readable in the field region — verified, no retry."""
    vision = _type_vision()
    vision.ocr_results = [[OcrLine("hello world")]]
    backend = FakeBackend(text_value_supported=False)
    report = Replayer(backend, vision=vision).run(
        _type_workflow(),
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[1].input_verified is True
    assert report.results[1].input_retried is False


def test_type_dialog_over_field_halts_without_retyping(bundle, run_dir):
    """ADDED 2026-07-09 (adversarial review, P2a/P2b): a dialog rendering
    over the field region changes pixels — under the old diff-alone rule
    that false-verified while the keystrokes fell elsewhere. Now an
    OCR-able value must be READ; pixels-changed-but-value-unreadable (the
    region gained other readable text) halts immediately WITHOUT the
    select-all retype, which could destroy pre-existing field content."""
    vision = _type_vision()
    dialog = [OcrLine("Are you sure you want to discard this draft?")]
    # attempt 1: after-OCR (1x), after-OCR (2x upscale), then the masked
    # heuristic re-reads the after and baseline regions.
    vision.ocr_results = [dialog, dialog, dialog, []]
    vision.pixels_changed_results = [True]
    backend = FakeBackend(text_value_supported=False)
    report = Replayer(backend, vision=vision).run(
        _type_workflow(),
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    result = report.results[1]
    assert result.input_verified is False
    assert result.input_retried is False  # retype is unsafe here
    assert "retyping is unsafe" in result.error
    # Exactly one type action — never retyped, never selected-all.
    assert backend.actions.count(("type", "hello world")) == 1
    assert ("press", "ControlOrMeta+a") not in backend.actions


def test_ordinary_field_never_uses_masked_pixel_acceptance(bundle, run_dir):
    """Unreadable ordinary text is not a password-dot success shape.

    This is the macOS focus-steal regression: pixels changed after the retry,
    OCR read no value, and the old generic masked heuristic returned success
    even though the field remained empty.
    """
    vision = _type_vision()
    vision.ocr_results = [[], [], [], []]
    vision.pixels_changed_results = [True]
    backend = FakeBackend(text_value_supported=False)
    report = Replayer(backend, vision=vision).run(
        _type_workflow(),
        params={"note": "hello world"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    result = report.results[1]
    assert result.input_verified is False
    assert result.input_retried is False
    assert "retyping is unsafe" in (result.error or "")


def test_type_masked_field_accepts_diff_without_new_text(bundle, run_dir, monkeypatch):
    """Masked fields (password dots) render pixels but no readable text:
    the diff plus an unchanged-OCR region is the accepted masked shape."""
    vision = _type_vision()
    vision.ocr_results = [[], [], [], []]  # nothing readable before/after
    vision.pixels_changed_results = [True]
    backend = FakeBackend(text_value_supported=False)
    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_NOTE", "hunter2secret")
    report = Replayer(backend, vision=vision).run(
        Workflow(
            name="wf",
            steps=[
                click_step(),
                Step(
                    id="t1",
                    intent="type secret",
                    action=ActionKind.TYPE,
                    param="note",
                    secret=True,
                ),
            ],
        ),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[1].input_verified is True
    assert report.results[1].input_retried is False


def test_type_masked_dots_reading_as_noise_still_accepts(bundle, run_dir, monkeypatch):
    """FIXED 2026-07-09 (CI regression): on some platform renderers the
    password dots OCR not as nothing but as punctuation runs,
    low-confidence glyph noise, or even CONFIDENT homogeneous digit runs
    (measured on the Linux renderer: 17 bullets -> '0000000000006' at
    0.81) — a raw text-length comparison then read that as 'new readable
    text' and false-halted every login. The masked heuristic counts only
    confident, non-homogeneous ALPHANUMERIC characters, which is also
    invariant to OCR re-segmentation between frames."""
    vision = _type_vision()
    dots = [
        OcrLine("................."),  # confident punctuation run
        OcrLine("0000000000006", confidence=0.81),  # verbatim Linux misread
        OcrLine("mockmed demo pass", confidence=0.3),  # sub-threshold noise
    ]
    vision.ocr_results = [dots, dots, dots, []]
    vision.pixels_changed_results = [True]
    backend = FakeBackend(text_value_supported=False)
    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_NOTE", "mockmed-demo-pass")
    report = Replayer(backend, vision=vision).run(
        Workflow(
            name="wf",
            steps=[
                click_step(),
                Step(
                    id="t1",
                    intent="type secret",
                    action=ActionKind.TYPE,
                    param="note",
                    secret=True,
                ),
            ],
        ),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[1].input_verified is True
    assert report.results[1].input_retried is False


def test_type_without_known_field_diffs_full_frame_and_cannot_refocus(bundle, run_dir):
    """A TYPE step not preceded by a click (keyboard-only focus moves) has
    no field point: verification diffs the whole frame, and the retry
    retypes without a refocus click."""
    vision = FakeVision()
    vision.pixels_changed_results = [False, False]
    backend = FakeBackend(
        text_value_supported=False, type_accept_results=[False, False]
    )
    workflow = Workflow(
        name="wf",
        steps=[
            Step(id="t1", intent="type literal", action=ActionKind.TYPE, text="North")
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert vision.pixels_changed_calls[0] is None  # full-frame diff
    # Retry typed again but never clicked or selected-all.
    assert backend.actions == [("type", "North"), ("type", "North")]


# -- structural postconditions (URL/title change, new tab) --------------------


class StructuralFakeBackend(FakeBackend):
    """FakeBackend that exposes StructuralBackend observations and mutates
    them when scripted actions fire."""

    def __init__(self, *, url="http://app/", title="Inbox", pages=1, **kw):
        super().__init__(**kw)
        self._url = url
        self._title = title
        self._pages = pages
        self.on_click = None  # callable(self) fired after each click

    @property
    def url(self):
        return self._url

    @property
    def page_title(self):
        return self._title

    @property
    def page_count(self):
        return self._pages

    def click(self, x, y, *, double=False):
        super().click(x, y, double=double)
        if self.on_click is not None:
            self.on_click(self)


def _structural_workflow(kind: PostconditionKind) -> Workflow:
    return Workflow(
        name="wf",
        steps=[click_step(expect=[Postcondition(kind=kind, timeout_s=0.2)])],
    )


def _resolving_vision() -> "FakeVision":
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    return vision


def test_url_changed_passes_when_url_differs_from_step_start(bundle, run_dir):
    backend = StructuralFakeBackend()
    backend.on_click = lambda b: setattr(b, "_url", "http://app/#report")
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.URL_CHANGED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert report.results[0].postconditions_ok is True


def test_url_changed_fails_when_url_static(bundle, run_dir):
    backend = StructuralFakeBackend()  # click changes nothing
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.URL_CHANGED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert "url_changed" in (report.results[0].error or "")


def test_new_tab_opened_passes_when_page_count_grows(bundle, run_dir):
    backend = StructuralFakeBackend()
    backend.on_click = lambda b: setattr(b, "_pages", 2)
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.NEW_TAB_OPENED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True


def test_new_tab_opened_fails_when_no_tab_appears(bundle, run_dir):
    backend = StructuralFakeBackend()
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.NEW_TAB_OPENED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert "new_tab_opened" in (report.results[0].error or "")


def test_title_changed_postcondition(bundle, run_dir):
    backend = StructuralFakeBackend()
    backend.on_click = lambda b: setattr(b, "_title", "Report")
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.TITLE_CHANGED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True


def test_structural_postcondition_passes_unverified_on_plain_backend(bundle, run_dir):
    """A backend without structural observations cannot arbitrate a
    structural postcondition: the step passes, honestly unverified
    (docs/LIMITS.md) — it must never false-halt a native-backend replay."""
    backend = FakeBackend()  # no url/page_title/page_count
    report = Replayer(backend, vision=_resolving_vision(), poll_interval_s=0.01).run(
        _structural_workflow(PostconditionKind.URL_CHANGED),
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True


# -- identity-protection coverage audit (run start) ---------------------------


def _coverage_workflow() -> Workflow:
    armed = click_step("s_armed")
    armed.anchor.context_text = "Belford, Phil 1985-03-12 M"
    armed.identity_armed = True
    unarmed = click_step("s_unarmed", ocr_text="")
    unarmed.identity_armed = False
    unarmed.identity_unarmed_reason = (
        "no readable text in the target's row band at compile time "
        "(icon-only or unlabeled row)"
    )
    legacy_unarmed = click_step("s_legacy")  # pre-metric bundle: fields None
    keyboard = Step(
        id="s_key", intent="press Enter", action=ActionKind.KEY, key="Enter"
    )
    return Workflow(name="coverage", steps=[armed, unarmed, legacy_unarmed, keyboard])


def test_identity_coverage_recorded_on_report():
    """The report states N of M applicable steps armed and lists every
    unarmed click by id with its reason — computed from the whole bundle
    at run start, before any step executes."""
    report = RunReport(workflow_name="coverage", started_at="t")
    Replayer._record_identity_coverage(_coverage_workflow(), report)
    assert report.identity_applicable_steps == 3  # keyboard step excluded
    assert report.identity_armed_steps == 1
    ids = [u.step_id for u in report.identity_unarmed]
    assert ids == ["s_unarmed", "s_legacy"]
    assert "icon-only" in report.identity_unarmed[0].reason
    # A pre-metric bundle still lists the step, with an honest reason.
    assert "predates" in report.identity_unarmed[1].reason


def test_identity_coverage_counts_anchored_type_steps():
    type_step = Step(
        id="s_type",
        intent="type note",
        action=ActionKind.TYPE,
        text="hello",
        anchor=Anchor(
            template="templates/btn.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            context_text="Notes field row text here",
        ),
    )
    report = RunReport(workflow_name="coverage", started_at="t")
    Replayer._record_identity_coverage(Workflow(name="w", steps=[type_step]), report)
    assert report.identity_applicable_steps == 1
    assert report.identity_armed_steps == 1
    assert report.identity_unarmed == []


def test_encrypted_bundle_replays_from_in_memory_templates(tmp_path):
    """An ENCRYPTED bundle (openadapt-flow#113) has no cleartext
    ``templates/*.png`` on disk — only sealed ``.enc`` ciphertext — so the
    replayer must resolve the ``template`` rung from the crops
    ``Workflow.load(key=...)`` decrypted in memory (``decrypted_template``),
    not from a disk read that would find nothing. This proves the resolver
    receives those exact decrypted bytes and the click still lands.
    """
    key = "correct horse battery staple"
    crop_png = make_png((50, 20), color=(12, 34, 56))

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(crop_png)

    workflow = Workflow(name="wf-enc", steps=[click_step()])
    workflow.save(bundle_dir, encrypt=True, key=key)

    # After sealing, no cleartext crop remains — only the .enc ciphertext.
    assert not (bundle_dir / "templates" / "btn.png").is_file()
    assert (bundle_dir / "templates" / "btn.png.enc").is_file()

    loaded = Workflow.load(bundle_dir, key=key)
    assert loaded.encrypted is True
    assert loaded.decrypted_template("templates/btn.png") == crop_png

    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        loaded,
        params={},
        bundle_dir=bundle_dir,
        run_dir=tmp_path / "run",
    )

    assert report.success is True
    assert report.rung_counts == {"template": 1}
    assert backend.actions == [("click", 110, 105, False)]
    # The resolver was handed the DECRYPTED in-memory crop, not None (which is
    # all a disk read of the .enc-only bundle could have produced).
    assert crop_png in vision.template_png_calls


def test_unencrypted_bundle_still_reads_template_from_disk(tmp_path):
    """The plaintext path is unchanged: a non-encrypted bundle's crop is read
    straight from ``templates/*.png`` on disk (``decrypted_template`` is never
    consulted), so the resolver gets the on-disk bytes exactly as before."""
    crop_png = make_png((50, 20), color=(9, 9, 9))
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(crop_png)

    workflow = Workflow(name="wf-plain", steps=[click_step()])
    assert workflow.encrypted is False

    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow,
        params={},
        bundle_dir=bundle_dir,
        run_dir=tmp_path / "run",
    )

    assert report.success is True
    assert crop_png in vision.template_png_calls
