"""Exclusive-time profiler for one replay run.

Wraps a fixed list of Replayer, backend and vision functions. Each wrapped
call's time is split into its own (exclusive) time and the time spent in
other wrapped calls it made, so the buckets add up to the wall time of the
run with no double counting. Time not inside any wrapped call is engine
bookkeeping ("engine other").

The profiler only measures. It changes no behavior: every wrapper calls the
original function with the same arguments and returns its result.

Usage::

    prof = RunProfiler()
    prof.install(backend)           # wraps this backend instance + classes
    with prof.span("task"):
        report = replayer.run(...)
    prof.uninstall()
    prof.summary()                  # {name: {"n":..., "excl_s":..., "incl_s":...}}
"""

from __future__ import annotations

import functools
import inspect
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable, Iterator

# Replayer methods worth naming. Each is wrapped on the class.
REPLAYER_METHODS = (
    "_run_step",
    "_resolve_step",
    "_apply_step_gates",
    "_wait_starting_state_settled",
    "_verify_identity",
    "_verify_identity_ocr",
    "_verify_dedicated_identity_signal",
    "_surface_identity_check",
    "_act",
    "_wait_for_dialog",
    "_handle_interstitials",
    "_verify_typed_input",
    "_verify_selected_option",
    "_check_postconditions",
    "_poll_postconditions",
    "_verify_effects",
    "_settle_seeded",
    "_save_step_png",
    "_wait_for_scroll_transition_and_settle",
    "_revalidate_consequential_actuation",
    "_finalize_report",
)

# Backend methods. Wrapped on the instance so other backends are untouched.
BACKEND_METHODS = (
    "screenshot",
    "page_quiescent",
    "page_change_marker",
    "click",
    "right_click",
    "type_text",
    "type_text_guarded",
    "press",
    "press_guarded",
    "scroll",
    "locate_structural",
    "act_structural",
    "act_guarded_coordinate",
    "structured_text_at",
    "text_value_at",
    "displayed_text_at",
    "focused_text_value",
    "focused_field_label",
    "focused_select_label_at",
    "visible_text_present",
    "take_dialogs",
    "arm_dialog",
    "structural_locator_at",
    "submit_binding_at",
    "workflow_state_identity",
    "session_identity",
    "application_identity",
)

# (module, function) pairs in the vision layer.
VISION_FUNCS = (
    ("openadapt_flow.vision.settle", "wait_settled"),
    ("openadapt_flow.vision.settle", "wait_settled_result"),
    ("openadapt_flow.vision.settle", "wait_settled_seeded"),
    ("openadapt_flow.vision.ocr", "ocr"),
    ("openadapt_flow.vision.match", "find_template"),
    ("openadapt_flow.vision.match", "find_text"),
    ("openadapt_flow.vision.match", "find_text_candidates"),
    ("openadapt_flow.vision.hashing", "phash_png"),
)

# Report buckets: which named calls count toward each.
BUCKETS: dict[str, tuple[str, ...]] = {
    "waits: fixed sleeps (poll intervals)": ("time.sleep",),
    "waits: page signals (DOM/network quiet)": (
        "be.page_quiescent",
        "be.page_change_marker",
    ),
    "waits: screen settle (frame compare)": (
        "vision.wait_settled",
        "vision.wait_settled_result",
        "vision.wait_settled_seeded",
        "rp._settle_seeded",
        "rp._wait_starting_state_settled",
        "rp._wait_for_scroll_transition_and_settle",
        "vision.phash_png",
    ),
    "screenshots (capture)": ("be.screenshot",),
    "step evidence PNGs (write)": ("rp._save_step_png",),
    "identity: structure (DOM)": (
        "rp._verify_identity",
        "rp._verify_dedicated_identity_signal",
        "rp._surface_identity_check",
        "be.structured_text_at",
        "be.workflow_state_identity",
        "be.session_identity",
        "be.application_identity",
    ),
    "identity: OCR": ("rp._verify_identity_ocr", "vision.ocr@identity"),
    "OCR (other)": ("vision.ocr",),
    "target resolution": (
        "rp._resolve_step",
        "be.locate_structural",
        "be.structural_locator_at",
        "be.submit_binding_at",
        "vision.find_template",
        "vision.find_text",
        "vision.find_text_candidates",
    ),
    "typing and keys": (
        "be.type_text",
        "be.type_text_guarded",
        "be.press",
        "be.press_guarded",
    ),
    "clicks and scrolls": (
        "be.click",
        "be.right_click",
        "be.act_structural",
        "be.act_guarded_coordinate",
        "be.scroll",
    ),
    "input read-back (field value)": (
        "rp._verify_typed_input",
        "rp._verify_selected_option",
        "be.text_value_at",
        "be.displayed_text_at",
        "be.focused_text_value",
        "be.focused_field_label",
        "be.focused_select_label_at",
    ),
    "postconditions and dialogs": (
        "rp._check_postconditions",
        "rp._poll_postconditions",
        "be.visible_text_present",
        "rp._wait_for_dialog",
        "be.take_dialogs",
        "be.arm_dialog",
        "rp._handle_interstitials",
    ),
    "save read-back (system of record)": ("rp._verify_effects",),
    "engine other": (
        "rp._run_step",
        "rp._act",
        "rp._apply_step_gates",
        "rp._revalidate_consequential_actuation",
        "rp._finalize_report",
        "span.task",
    ),
}


class RunProfiler:
    """Stack-based exclusive-time profiler (main thread only)."""

    def __init__(self) -> None:
        self._thread = threading.get_ident()
        self._stack: list[list[Any]] = []  # [name, start, child_time]
        self.stats: dict[str, dict[str, float]] = defaultdict(
            lambda: {"n": 0, "excl_s": 0.0, "incl_s": 0.0}
        )
        self.steps: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.by_parent: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._current_step: str | None = None
        self._undo: list[Callable[[], None]] = []

    # -- core -------------------------------------------------------------

    def _enter(self, name: str) -> None:
        self._stack.append([name, time.perf_counter(), 0.0])

    def _exit(self) -> None:
        name, start, child = self._stack.pop()
        incl = time.perf_counter() - start
        excl = incl - child
        if name == "vision.ocr" and any(
            f[0] in ("rp._verify_identity_ocr", "rp._verify_identity")
            for f in self._stack
        ):
            name = "vision.ocr@identity"
        parent = next(
            (f[0] for f in reversed(self._stack) if f[0].startswith("rp.")), "-"
        )
        self.by_parent[name][parent] += excl
        st = self.stats[name]
        st["n"] += 1
        st["excl_s"] += excl
        st["incl_s"] += incl
        if self._current_step:
            self.steps[self._current_step][name] += excl
        if self._stack:
            self._stack[-1][2] += incl

    def _wrap(
        self, name: str, fn: Callable[..., Any], *, static: bool = False
    ) -> Callable[..., Any]:
        prof = self

        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> Any:
            if threading.get_ident() != prof._thread:
                return fn(*a, **k)
            prev_step = prof._current_step
            if name == "rp._run_step":
                step = (
                    a[0 if static else 1]
                    if len(a) > (0 if static else 1)
                    else k.get("step")
                )
                prof._current_step = getattr(step, "id", None) or prev_step
            prof._enter(name)
            try:
                return fn(*a, **k)
            finally:
                prof._exit()
                if name == "rp._run_step":
                    prof._current_step = prev_step

        return wrapper

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        self._enter(f"span.{name}")
        try:
            yield
        finally:
            self._exit()

    # -- install ----------------------------------------------------------

    def install(self, backend: Any) -> None:
        import importlib

        from openadapt_flow.runtime.replayer import Replayer

        for m in REPLAYER_METHODS:
            raw = inspect.getattr_static(Replayer, m, None)
            if raw is None:
                continue
            if isinstance(raw, staticmethod):
                wrapped: Any = staticmethod(
                    self._wrap(f"rp.{m}", raw.__func__, static=True)
                )
            else:
                wrapped = self._wrap(f"rp.{m}", raw)
            setattr(Replayer, m, wrapped)
            self._undo.append(lambda m=m, raw=raw: setattr(Replayer, m, raw))
        for m in BACKEND_METHODS:
            orig = getattr(backend, m, None)
            if orig is None or not callable(orig):
                continue
            setattr(backend, m, self._wrap(f"be.{m}", orig))
            self._undo.append(lambda m=m: backend.__dict__.pop(m, None))
        for mod_name, fn_name in VISION_FUNCS:
            mod = importlib.import_module(mod_name)
            orig = getattr(mod, fn_name, None)
            if orig is None:
                continue
            wrapped = self._wrap(f"vision.{fn_name}", orig)
            setattr(mod, fn_name, wrapped)
            self._undo.append(
                lambda mod=mod, fn_name=fn_name, orig=orig: setattr(mod, fn_name, orig)
            )
            # Modules that imported the function by name keep their own
            # reference; patch those too.
            for other in ("openadapt_flow.runtime.replayer", "openadapt_flow.vision"):
                om = importlib.import_module(other)
                if getattr(om, fn_name, None) is orig:
                    setattr(om, fn_name, wrapped)
                    self._undo.append(
                        lambda om=om, fn_name=fn_name, orig=orig: setattr(
                            om, fn_name, orig
                        )
                    )
        orig_sleep = time.sleep
        time.sleep = self._wrap("time.sleep", orig_sleep)
        self._undo.append(lambda: setattr(time, "sleep", orig_sleep))

    def uninstall(self) -> None:
        while self._undo:
            self._undo.pop()()

    # -- output -----------------------------------------------------------

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in self.stats.items()
        }

    def parents(self) -> dict[str, dict[str, float]]:
        """Exclusive time of each call, split by the nearest Replayer caller."""
        return {
            n: {p: round(v, 4) for p, v in sorted(per.items(), key=lambda x: -x[1])}
            for n, per in self.by_parent.items()
        }

    def buckets(self) -> dict[str, float]:
        out: dict[str, float] = {}
        named = set()
        for bucket, names in BUCKETS.items():
            out[bucket] = round(
                sum(self.stats[n]["excl_s"] for n in names if n in self.stats), 4
            )
            named.update(names)
        unnamed = [n for n in self.stats if n not in named]
        out["unassigned"] = round(sum(self.stats[n]["excl_s"] for n in unnamed), 4)
        return out

    def step_buckets(self) -> dict[str, dict[str, float]]:
        rev = {n: b for b, names in BUCKETS.items() for n in names}
        out: dict[str, dict[str, float]] = {}
        for step, per in self.steps.items():
            row: dict[str, float] = defaultdict(float)
            for n, v in per.items():
                row[rev.get(n, "unassigned")] += v
            out[step] = {k: round(v, 4) for k, v in row.items() if v > 0.0005}
        return out
