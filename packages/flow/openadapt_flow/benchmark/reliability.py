"""Compile-reliability study harness across diverse public web apps.

The commercial question this answers: does "record once, it generalizes"
hold across *varied real apps*, or is every workflow bespoke? The e2e suite
only exercises N=1-2 workflows on apps we control (MockMed, OpenEMR). This
harness records -> compiles -> replays-on-unchanged-UI a corpus of diverse
public web apps and measures the distribution of outcomes.

It deliberately reuses the SAME programmatic record path the e2e tests use
(`openadapt_flow.demo_driver` pattern): drive a scripted Playwright flow
through `Recorder` (so frames + events are captured exactly as a human demo
would be), `compile_recording` it, then `Replayer.run` it against the same
URL moments later.

No Anthropic API is used: replay runs with the default `grounder=None`, so
resolution never reaches the model-calling rung. This is compiled replay +
OCR only.

Outcome model (per replay), using an ARM-INDEPENDENT ground-truth DOM/URL
assertion where one is available (else the replayer's self-reported
success, labelled):

- ``success``       report.success AND ground truth reached.
- ``wrong_action``  report.success but ground truth NOT reached — the
                    dangerous silent-failure mode (claimed success, wrong
                    end state).
- ``safe_halt``     replay halted (report.success False) and ground truth
                    not reached — the honest failure mode (named step +
                    postcondition, no wrong write).
- ``false_halt``    replay halted but ground truth WAS reached anyway — an
                    over-conservative halt (a reliability cost, but safe).
- ``crash``         an exception escaped record/compile/replay.

``self_reported=True`` marks runs whose verify kind is ``self_reported``
(no independent ground truth): ``success``/``safe_halt`` there come purely
from the replayer's own postconditions.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.backends.playwright_backend import VIEWPORT, PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.recorder import Recorder
from openadapt_flow.runtime import Replayer

# Record/replay locator timeout (real remote apps are slower than MockMed).
LOCATE_TIMEOUT_MS = 15000
GOTO_TIMEOUT_MS = 40000
# Small settle after navigation before the first frame is captured.
POST_GOTO_SETTLE_S = 1.0


# --------------------------------------------------------------------------
# Corpus data model (JSON-serializable so the manifest is fully reproducible)
# --------------------------------------------------------------------------


@dataclass
class Step:
    """One recorded action in a workflow.

    ``action`` is one of ``click``, ``double_click``, ``type``, ``press``,
    ``scroll``. ``selector`` locates the click target (CSS; the first match
    is used). A click target outside the viewport is scrolled into view
    first, and that scroll is RECORDED as a real ``scroll`` step so replay
    reproduces it via the closed-loop scroller.
    """

    action: str
    selector: Optional[str] = None
    text: Optional[str] = None
    param: Optional[str] = None
    key: Optional[str] = None
    dx: int = 0
    dy: int = 0
    description: str = ""


@dataclass
class Verify:
    """Arm-independent ground-truth check on the live replay page.

    Kinds:
      - ``url_contains``        page.url contains ``value``.
      - ``dom_text_contains``   ``selector``'s text (first match, or any
                                match) contains ``value`` (case-insensitive).
      - ``dom_visible``         ``selector`` first match is visible.
      - ``dom_count``           ``selector`` match count >= ``count``.
      - ``dom_value_equals``    ``selector``'s input value == ``value``.
      - ``dom_value_nonempty``  ``selector``'s input value is non-empty.
      - ``dom_checked``         ``selector`` first match is checked.
      - ``self_reported``       no independent ground truth; outcome comes
                                from the replayer's own postconditions.
    """

    kind: str
    selector: Optional[str] = None
    value: Optional[str] = None
    count: int = 1


@dataclass
class AppSpec:
    """A corpus entry: an app + a short real multi-step workflow."""

    id: str
    name: str
    url: str
    category: str
    framework: str
    description: str
    steps: list[Step]
    verify: Verify
    params: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_manifest(self) -> dict[str, Any]:
        """JSON-serializable manifest entry (URLs, steps, params, verify)."""
        return asdict(self)


# --------------------------------------------------------------------------
# Recording driver (mirrors openadapt_flow.demo_driver, generalized)
# --------------------------------------------------------------------------


def _element_center(box: dict) -> tuple[int, int]:
    return (
        int(box["x"] + box["width"] / 2),
        int(box["y"] + box["height"] / 2),
    )


def _locate_and_maybe_scroll(
    page: Any, recorder: Recorder, selector: str, viewport_h: int
) -> tuple[int, int]:
    """Center pixel coords of ``selector``'s first match, scrolling it into
    view (as a RECORDED scroll step) when it is off-screen.

    Record time may use Playwright locators to find pixel coordinates
    (exactly as the demo driver does); replay never uses selectors.
    """
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=LOCATE_TIMEOUT_MS)
    box = loc.bounding_box()
    if box is None:
        raise RuntimeError(f"no bounding box for {selector!r}")
    cy = box["y"] + box["height"] / 2
    if not (0 <= cy <= viewport_h):
        # Record a real scroll so the target lands near mid-viewport; replay
        # reproduces it via the closed-loop scroller.
        dy = int(cy - viewport_h / 2)
        recorder.scroll(0, dy)
        box = loc.bounding_box()
        if box is None:  # pragma: no cover - visible => box exists
            raise RuntimeError(f"no bounding box for {selector!r} after scroll")
    return _element_center(box)


def record_app(app: AppSpec, out_dir: Path, browser: Any) -> Path:
    """Record ``app``'s scripted workflow into a recording directory.

    Uses a fresh Playwright page from ``browser`` (viewport 1280x800,
    deviceScaleFactor=1). Returns the recording directory (meta.json,
    events.jsonl, frames/). Raises on record-time failure (selector never
    visible, navigation timeout, anti-bot block) — the caller records that
    as a ``record_error`` outcome.
    """
    page = browser.new_page(
        viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
        device_scale_factor=1,
    )
    try:
        page.goto(app.url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        page.wait_for_timeout(int(POST_GOTO_SETTLE_S * 1000))
        backend = PlaywrightBackend(page)
        recorder = Recorder(backend, out_dir, app_url=app.url)
        _, viewport_h = backend.viewport
        for step in app.steps:
            if step.action in ("click", "double_click"):
                if not step.selector:
                    raise ValueError(f"{step.action} step needs a selector")
                x, y = _locate_and_maybe_scroll(
                    page, recorder, step.selector, viewport_h
                )
                if step.action == "double_click":
                    recorder.double_click(x, y)
                else:
                    recorder.click(x, y)
            elif step.action == "type":
                recorder.type_text(step.text or "", param=step.param)
            elif step.action == "press":
                if not step.key:
                    raise ValueError("press step needs a key")
                recorder.press(step.key)
            elif step.action == "scroll":
                recorder.scroll(step.dx, step.dy)
            else:
                raise ValueError(f"unknown step action {step.action!r}")
        return recorder.finish()
    finally:
        page.close()


# --------------------------------------------------------------------------
# Ground-truth evaluation (arm-independent)
# --------------------------------------------------------------------------


def evaluate_verify(verify: Verify, page: Any) -> Optional[bool]:
    """Evaluate a ground-truth check against the live replay page.

    Returns True/False for a real check, or None for ``self_reported``
    (no independent ground truth). Never raises — a locator error is
    treated as "not reached" (False).
    """
    try:
        if verify.kind == "self_reported":
            return None
        if verify.kind == "url_contains":
            return (verify.value or "") in (page.url or "")
        if verify.kind == "dom_visible":
            return bool(page.locator(verify.selector).first.is_visible())
        if verify.kind == "dom_count":
            return page.locator(verify.selector).count() >= verify.count
        if verify.kind == "dom_value_equals":
            return page.locator(verify.selector).first.input_value(timeout=2000) == (
                verify.value or ""
            )
        if verify.kind == "dom_value_nonempty":
            return bool(page.locator(verify.selector).first.input_value(timeout=2000))
        if verify.kind == "dom_checked":
            return bool(page.locator(verify.selector).first.is_checked())
        if verify.kind == "dom_text_contains":
            loc = page.locator(verify.selector)
            n = loc.count()
            needle = (verify.value or "").lower()
            for i in range(min(n, 25)):
                try:
                    txt = loc.nth(i).inner_text(timeout=2000)
                except Exception:
                    continue
                if needle in (txt or "").lower():
                    return True
            return False
        return None
    except Exception:
        return False


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------


def _first_failing(report: RunReport):
    for r in report.results:
        if not r.ok:
            return r
    return None


def classify_failure(report: Optional[RunReport], error: Optional[str]) -> dict:
    """Classify WHY a run failed into a (category, root_cause_hint).

    ``category`` names the pipeline stage/mechanism that failed;
    ``root_cause_hint`` guesses the underlying cause from the error text
    (timing, anti-bot, dynamic content, ...). Both are heuristic and meant
    for aggregate taxonomy counts, not per-run certainty.
    """
    if report is None:
        # Failure before/around a report existed (record or compile).
        text = (error or "").lower()
        if "compile" in text:
            category = "compile_error"
        else:
            category = "record_error"
        return {"category": category, "root_cause_hint": _root_cause(text)}

    failing = _first_failing(report)
    if failing is None:
        return {"category": "none", "root_cause_hint": None}

    err = (failing.error or "").lower()
    if failing.resolution is None and "all resolution rungs failed" in err:
        category = "resolution_failed"
    elif "identity check failed" in err:
        category = "identity_mismatch"
    elif "postconditions failed" in err:
        category = "postcondition_failed"
    elif "typed input could not be verified" in err:
        category = "typed_input_unverified"
    elif "closed-loop scroll exhausted" in err:
        category = "scroll_exhausted"
    elif "irreversible" in err and "refusing to act" in err:
        category = "risk_gate"
    elif "raised" in err:
        category = "step_exception"
    else:
        category = "other_halt"
    return {"category": category, "root_cause_hint": _root_cause(err)}


def _root_cause(text: str) -> Optional[str]:
    """Best-effort underlying-cause guess from error/log text."""
    if not text:
        return None
    text = text.lower()
    checks = [
        (
            "anti_bot",
            ("403", "429", "captcha", "forbidden", "blocked", "access denied"),
        ),
        ("iframe", ("iframe", "frame")),
        ("canvas", ("canvas",)),
        ("timing", ("timeout", "timed out", "still-loading", "settle")),
        ("navigation", ("navigation", "net::err", "goto", "connection")),
        ("dynamic_content", ("postconditions failed", "text_present", "region")),
        ("resolution", ("resolution rungs",)),
    ]
    for name, needles in checks:
        if any(n in text for n in needles):
            return name
    return None


# --------------------------------------------------------------------------
# Per-app run: record -> compile -> replay -> ground truth
# --------------------------------------------------------------------------


def run_target(app: AppSpec, workdir: Path, browser: Any) -> dict:
    """Run the full record -> compile -> replay-on-unchanged-UI for one app.

    Returns a result dict (JSON-serializable) capturing compile success,
    the replay outcome, the per-step rung distribution, heal count, the
    failure taxonomy, and timing. Never raises: any exception is captured
    as a ``crash`` (or ``record_error`` / ``compile_error``) outcome.
    """
    workdir = Path(workdir)
    rec_dir = workdir / "recording"
    bundle_dir = workdir / "bundle"
    run_dir = workdir / "run"
    t0 = time.monotonic()

    result: dict[str, Any] = {
        "id": app.id,
        "name": app.name,
        "url": app.url,
        "category": app.category,
        "framework": app.framework,
        "n_steps": len(app.steps),
        "compile_success": False,
        "compile_error": None,
        "compiled_steps": None,
        "outcome": None,
        "self_reported": app.verify.kind == "self_reported",
        "report_success": None,
        "ground_truth": None,
        "rung_counts": {},
        "heal_count": 0,
        "failing_step": None,
        "failing_intent": None,
        "error": None,
        "failure_category": None,
        "root_cause_hint": None,
        "elapsed_s": None,
    }

    # -- record -------------------------------------------------------------
    try:
        record_app(app, rec_dir, browser)
    except Exception as exc:  # record-time failure
        result["outcome"] = "record_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["failure_category"] = "record_error"
        result["root_cause_hint"] = _root_cause(result["error"].lower())
        result["elapsed_s"] = round(time.monotonic() - t0, 1)
        return result

    # -- compile ------------------------------------------------------------
    try:
        wf = compile_recording(rec_dir, bundle_dir, name=app.id)
        result["compile_success"] = True
        result["compiled_steps"] = len(wf.steps)
    except Exception as exc:
        result["compile_error"] = f"{type(exc).__name__}: {exc}"
        result["outcome"] = "compile_error"
        result["failure_category"] = "compile_error"
        result["root_cause_hint"] = _root_cause(result["compile_error"].lower())
        result["elapsed_s"] = round(time.monotonic() - t0, 1)
        return result

    # -- replay on unchanged UI --------------------------------------------
    page = browser.new_page(
        viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
        device_scale_factor=1,
    )
    try:
        page.goto(app.url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        page.wait_for_timeout(int(POST_GOTO_SETTLE_S * 1000))
        backend = PlaywrightBackend(page)
        # grounder=None (default): compiled replay + OCR only, zero model calls.
        report = Replayer(backend).run(
            Workflow.load(bundle_dir),
            params=dict(app.params),
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        ground_truth = evaluate_verify(app.verify, page)

        result["report_success"] = report.success
        result["ground_truth"] = ground_truth
        result["rung_counts"] = dict(report.rung_counts)
        result["heal_count"] = report.heal_count
        result["outcome"] = _outcome(report.success, ground_truth)
        if not report.success:
            failing = _first_failing(report)
            if failing is not None:
                result["failing_step"] = failing.step_id
                result["failing_intent"] = failing.intent
                result["error"] = failing.error
            tax = classify_failure(report, None)
            result["failure_category"] = tax["category"]
            result["root_cause_hint"] = tax["root_cause_hint"]
        elif result["outcome"] == "wrong_action":
            result["failure_category"] = "wrong_action_silent"
            result["root_cause_hint"] = "ground_truth_disagrees"
    except Exception as exc:
        result["outcome"] = "crash"
        result["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        result["failure_category"] = "crash"
        result["root_cause_hint"] = _root_cause(result["error"].lower())
    finally:
        page.close()

    result["elapsed_s"] = round(time.monotonic() - t0, 1)
    return result


def _outcome(report_success: bool, ground_truth: Optional[bool]) -> str:
    """Map (replayer verdict, ground truth) to the outcome label."""
    if ground_truth is None:  # self-reported only
        return "success" if report_success else "safe_halt"
    if report_success and ground_truth:
        return "success"
    if report_success and not ground_truth:
        return "wrong_action"
    if not report_success and ground_truth:
        return "false_halt"
    return "safe_halt"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def aggregate(results: list[dict]) -> dict:
    """Aggregate per-app results into the study-level distribution."""
    n = len(results)
    compiled = [r for r in results if r["compile_success"]]
    replayed = [
        r
        for r in results
        if r["outcome"]
        in ("success", "wrong_action", "safe_halt", "false_halt", "crash")
    ]

    def count(pred) -> int:
        return sum(1 for r in results if pred(r))

    n_recorded = count(lambda r: r["outcome"] != "record_error")

    outcomes: dict[str, int] = {}
    for r in results:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1

    categories: dict[str, int] = {}
    for r in results:
        cat = r.get("failure_category")
        if cat and cat not in ("none", None):
            categories[cat] = categories.get(cat, 0) + 1

    root_causes: dict[str, int] = {}
    for r in results:
        rc = r.get("root_cause_hint")
        if rc:
            root_causes[rc] = root_causes.get(rc, 0) + 1

    def rate(k: int, d: int) -> Optional[float]:
        return round(k / d, 3) if d else None

    return {
        "n_apps": n,
        "n_recorded": n_recorded,
        "n_compiled": len(compiled),
        "n_replayed": len(replayed),
        "compile_success_rate_over_recorded": rate(len(compiled), n_recorded),
        "compile_success_rate_over_all": rate(len(compiled), n),
        "outcomes": outcomes,
        "replay_success_rate_over_replayed": rate(
            count(lambda r: r["outcome"] == "success"), len(replayed)
        ),
        "replay_success_rate_over_compiled": rate(
            count(lambda r: r["outcome"] == "success"), len(compiled)
        ),
        "wrong_action_count": count(lambda r: r["outcome"] == "wrong_action"),
        "safe_halt_count": count(lambda r: r["outcome"] == "safe_halt"),
        "false_halt_count": count(lambda r: r["outcome"] == "false_halt"),
        "crash_count": count(lambda r: r["outcome"] == "crash"),
        "record_error_count": count(lambda r: r["outcome"] == "record_error"),
        "failure_categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "root_cause_hints": dict(sorted(root_causes.items(), key=lambda kv: -kv[1])),
    }
