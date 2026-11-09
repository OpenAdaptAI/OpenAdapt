"""Desktop recording entry points and the desktop parity metric.

Two ways to produce a compile-ready recording of a **desktop** demonstration,
and one helper to check how close the resulting bundle is to a DOM-armed web
bundle.

1. Live record (``record_desktop_demo``) — PREFERRED, and the path that
   reaches web parity
   -----------------------------------------------------------------------
   Drive a live :class:`~openadapt_flow.backend.Backend` (a ``WindowsBackend``
   pointed at the in-guest agent) through :class:`~openadapt_flow.recorder.Recorder`.
   Because the recorder queries the backend's structured layer at each click
   (``StructuralActionBackend.structural_locator_at``), a Windows backend arms
   every click step with a **UIA ``AutomationId`` / role+name locator** — the
   native-desktop equivalent of the DOM ``#id`` / role+name a browser
   (``dom_arm``) bundle carries. The compiled bundle therefore has the SAME
   shape as a web bundle *including the structural top rung*, so replay resolves
   targets deterministically (the ``structural`` rung) with the visual ladder as
   fallback. This closes the desktop→web parity gap.

2. Offline convert (``openadapt_flow.adapters.capture.convert_capture``) —
   available, with ONE precise gap
   -----------------------------------------------------------------------
   An ``openadapt-capture`` session (recorded with NO live automation backend
   attached) converts into the identical recording layout (``meta.json`` +
   ``events.jsonl`` + ``frames/``) and compiles into the identical bundle
   shape. The ONE thing it cannot carry is the **structural locator**: capture
   records mouse/keyboard/video only — there is no live UIA tree at conversion
   time to read an ``AutomationId`` from — so ``anchor.structural`` is None on
   every step and replay uses the VISUAL ladder (template/ocr/geometry). This is
   an honest limitation of offline capture, not of the compiler: the bundle is
   fully valid and replays; it simply lacks the deterministic top rung.

   FOLLOW-UP to close it (out of this module's file scope): re-arm a
   capture-converted recording against a live UIA tree — replay each recorded
   click point through the native backend's ``structural_locator_at`` on the live app
   and write the returned locator onto the anchor. That belongs next to the
   browser ``dom_arm`` step (``benchmark/dom_arm.py``) as a ``uia_arm`` pass and
   is tracked separately; it is not implemented here.

:func:`structural_armed_coverage` reports the parity fraction (share of click
steps carrying a structural locator) for either path, so a test or the e2e
harness can assert the live path is armed and quantify the offline gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.recorder import Recorder

# The action kinds that carry a click target (and can thus be structurally
# armed). KEY/SCROLL/TYPE steps have no click element.
_CLICK_ACTIONS = ("click", "double_click")


def record_desktop_demo(
    backend: "Backend",
    out_dir: Path | str,
    driver: Callable[["Recorder"], None],
    *,
    app_url: Optional[str] = None,
    identifier_region: Optional[tuple[int, int, int, int]] = None,
    settle_interval_s: float = 0.1,
    settle_stable_frames: int = 2,
    settle_timeout_s: float = 3.0,
) -> Path:
    """Record a live desktop demonstration into a compile-ready directory.

    Constructs a :class:`~openadapt_flow.recorder.Recorder` over ``backend`` and
    hands it to ``driver`` to script the demonstration (clicks, typing, keys).
    When ``backend`` is a structural backend (Windows UIA or Linux AT-SPI),
    every click is armed with a native locator, so the compiled bundle carries
    the deterministic structural rung — matching a DOM-armed web bundle.

    Args:
        backend: A live backend to both observe (screenshots) and drive.
        out_dir: Output recording directory (created if missing).
        driver: Callable that receives the ``Recorder`` and issues the
            demonstrated actions (``r.click(x, y)``, ``r.type_text(v,
            param=...)``, ``r.press("Enter")``, ...). Do NOT call
            ``r.finish()`` inside it — this function does.
        app_url: Optional app identifier written to ``meta.json`` (native
            desktop apps usually leave this None).
        identifier_region: Optional operator-marked record-identifying region
            ``(x, y, width, height)`` in the backend's pixel coordinate space.
            When supplied, the rectangle is validated before recording starts
            and stamped into ``meta.json`` for the compiler to derive identity
            evidence from each event's actual before-frame. No region is
            inferred or selected automatically.
        settle_interval_s: Recorder frame-settle poll interval.
        settle_stable_frames: Consecutive stable frames required to settle.
        settle_timeout_s: Max seconds to wait for the frame to settle.

    Returns:
        The recording directory path (compile-ready).
    """
    marked_region: Optional[tuple[int, int, int, int]] = None
    if identifier_region is not None:
        if (
            not isinstance(identifier_region, tuple)
            or len(identifier_region) != 4
            or any(type(value) is not int for value in identifier_region)
        ):
            raise ValueError(
                "identifier_region must be four integer coordinates "
                "(x, y, width, height)"
            )
        x, y, width, height = identifier_region
        viewport = backend.viewport
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > viewport[0]
            or y + height > viewport[1]
        ):
            raise ValueError("identifier_region must fit inside the backend viewport")
        marked_region = (x, y, width, height)

    from openadapt_flow.recorder import Recorder

    recorder = Recorder(
        backend,
        out_dir,
        app_url=app_url,
        settle_interval_s=settle_interval_s,
        settle_stable_frames=settle_stable_frames,
        settle_timeout_s=settle_timeout_s,
        finalize_on_finish=marked_region is None,
    )
    if marked_region is None:
        driver(recorder)
        return recorder.finish()

    # Keep the writer registered until after the additive metadata update. This
    # lets sealed_set route both operations through an enclosing or recorder-
    # owned sealed writer without ever opening meta.json as plaintext.
    from openadapt_flow import sealed_set

    try:
        driver(recorder)
        recording = recorder.finish()
        meta_path = recording / "meta.json"
        meta = json.loads(sealed_set.read_text(meta_path))
        meta["identifier_region"] = list(marked_region)
        sealed_set.write_text(meta_path, json.dumps(meta, indent=2))
        recorder.finalize_encryption()
    except BaseException:
        try:
            recorder.abandon_encryption()
        except BaseException:
            # Preserve the original driver, recorder, metadata, or finalizer
            # exception. ``abandon_encryption`` only unregisters this
            # Recorder's writer; an enclosing sealed_set writer remains owned
            # by its context manager.
            pass
        raise
    return recording


def structural_armed_coverage(workflow: "Workflow") -> dict:
    """Fraction of click steps in ``workflow`` carrying a structural locator.

    The desktop→web parity metric: a web bundle armed by ``dom_arm`` puts a DOM
    locator on every clickable step; a live-recorded desktop bundle should
    likewise carry a native accessibility locator on every click step, while an offline
    capture-converted bundle carries none (see this module's docstring).

    Args:
        workflow: A compiled :class:`~openadapt_flow.ir.Workflow` (e.g.
            ``Workflow.load(bundle_dir)``).

    Returns:
        ``{"click_steps": int, "armed_clicks": int, "armed_coverage": float}``
        where ``armed_coverage`` is ``armed_clicks / click_steps`` (0.0 when
        there are no click steps).
    """
    click_steps = [
        s
        for s in workflow.steps
        if getattr(s.action, "value", s.action) in _CLICK_ACTIONS
    ]
    armed = [
        s
        for s in click_steps
        if s.anchor is not None and s.anchor.structural is not None
    ]
    n = len(click_steps)
    return {
        "click_steps": n,
        "armed_clicks": len(armed),
        "armed_coverage": round(len(armed) / n, 3) if n else 0.0,
    }
