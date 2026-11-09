"""Shared helpers for the adversarial-validation E2E suites.

Used by ``test_perturbation.py`` (Track A: environment/data drift),
``test_chaos.py`` (Track B: mid-run fault injection), and
``test_primitives.py`` (Track C: interaction-primitive taxonomy). See
``docs/validation/VALIDATION.md`` for the experiment matrix these suites
automate and the observed outcomes they pin down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.runtime import Replayer


class ChaosBackend(PlaywrightBackend):
    """Backend wrapper that sabotages the app mid-run.

    Fires ``inject(page)`` exactly once, immediately after the Nth click or
    the Nth ``type_text`` (whichever trigger is configured) — i.e. between
    the triggering step's action and the next step's resolution, which is
    where real-world state changes land.
    """

    def __init__(
        self,
        page,
        *,
        inject: Callable[[object], None],
        after_click: Optional[int] = None,
        after_type: Optional[int] = None,
    ) -> None:
        super().__init__(page)
        self._inject = inject
        self._after_click = after_click
        self._after_type = after_type
        self._clicks = 0
        self._types = 0
        self.fired = False

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        super().click(x, y, double=double)
        self._clicks += 1
        if (
            not self.fired
            and self._after_click is not None
            and self._clicks == self._after_click
        ):
            self.fired = True
            self._inject(self.page)

    def type_text(self, text: str) -> None:
        super().type_text(text)
        self._types += 1
        if (
            not self.fired
            and self._after_type is not None
            and self._types == self._after_type
        ):
            self.fired = True
            self._inject(self.page)


def replay_on_page(
    browser,
    bundle_dir: Path,
    url: str,
    run_dir: Path,
    *,
    params: dict[str, str],
    viewport: tuple[int, int] = (1280, 800),
    device_scale_factor: int = 1,
    backend_factory: Optional[Callable[[object], PlaywrightBackend]] = None,
    use_structural: bool = False,
) -> tuple[RunReport, dict]:
    """Replay ``bundle_dir`` on a fresh page and observe final app state.

    Unlike the ``replay`` fixture in ``conftest.py``, this helper controls
    the page's viewport/device-scale-factor and can wrap the backend (fault
    injection), and it returns ground truth read from the live app AFTER the
    run — which is what lets a test distinguish a *safe halt* from a *wrong
    action that the report never noticed*.

    Returns:
        ``(report, state)`` where ``state`` has ``hash`` (location.hash),
        ``banner`` (text of ``#saved-banner`` or None), ``enc_item`` (text
        of the first ``.enc-item`` or None), and ``status`` (text of
        ``#status`` or None, for the widgets page).
    """
    page = browser.new_page(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=device_scale_factor,
    )
    try:
        page.goto(url)
        backend = backend_factory(page) if backend_factory else PlaywrightBackend(page)
        workflow = Workflow.load(bundle_dir)
        report = Replayer(backend, use_structural=use_structural).run(
            workflow,
            params=params,
            bundle_dir=Path(bundle_dir),
            run_dir=Path(run_dir),
        )
        state = {
            "hash": page.evaluate("location.hash"),
            "banner": page.evaluate(
                "(document.getElementById('saved-banner') || {}).textContent || null"
            ),
            "enc_item": page.evaluate(
                "(document.querySelector('.enc-item') || {}).textContent || null"
            ),
            "status": page.evaluate(
                "(document.getElementById('status') || {}).textContent || null"
            ),
        }
    finally:
        page.close()
    return report, state


# Base font-size (px) of the MockMed elements a font-size perturbation scales
# (read from mockmed/static/styles.css). Used by ``replay_cosmetic`` to reflow
# text the way a user-side font-size preference would.
_BASE_FONT_PX = {
    "html, body": 16,
    "p": 16,
    "button": 16,
    "input": 16,
    "textarea": 16,
    "label": 16,
    "td, th": 15,
    ".seg-btn": 16,
    "h1": 24,
    "h2": 20,
    "#topbar": 20,
    "#patient-banner": 16,
    "#saved-banner": 17,
}

_FONT_FAMILY_SELECTORS = (
    "html, body, button, input, textarea, p, label, td, th, h1, h2, "
    "#topbar, #patient-banner, #saved-banner, .seg-btn"
)


def cosmetic_css(
    *,
    zoom: Optional[float] = None,
    font_scale: Optional[float] = None,
    font_family: Optional[str] = None,
) -> str:
    """Build a ``<head>`` stylesheet realizing a cosmetic-only perturbation.

    Injected after navigation, these selector rules survive MockMed's
    hash-router re-renders (they are not inline styles). ``zoom`` uses the
    CSS ``zoom`` property — the same model MockMed's bundled ``drift=zoom``
    mode uses. Nothing here changes the DOM's text or structure, so the
    target stays present and semantically identical: only rendering drifts.
    """
    parts: list[str] = []
    if zoom is not None and abs(zoom - 1.0) > 1e-9:
        parts.append(f"body {{ zoom: {zoom}; }}")
    if font_scale is not None and abs(font_scale - 1.0) > 1e-9:
        parts.append(
            "\n".join(
                f"{sel} {{ font-size: {round(base * font_scale)}px !important; }}"
                for sel, base in _BASE_FONT_PX.items()
            )
        )
    if font_family:
        parts.append(
            f"{_FONT_FAMILY_SELECTORS} {{ font-family: {font_family} !important; }}"
        )
    return "\n".join(parts)


def replay_cosmetic(
    browser,
    bundle_dir: Path,
    url: str,
    run_dir: Path,
    *,
    params: dict[str, str],
    viewport: tuple[int, int] = (1280, 800),
    device_scale_factor: float = 1,
    zoom: Optional[float] = None,
    font_scale: Optional[float] = None,
    font_family: Optional[str] = None,
    use_structural: bool = False,
) -> tuple[RunReport, dict]:
    """Replay ``bundle_dir`` under a cosmetic-only render perturbation.

    Like :func:`replay_on_page`, but applies browser zoom / font-size /
    font-family drift via an injected stylesheet and DPI via
    ``device_scale_factor``. Returns ``(report, state)`` with the same
    ground-truth ``state`` (``hash`` / ``banner``) read from the live app
    after the run, which is what distinguishes a *safe halt* from a *wrong
    action the report never noticed*.
    """
    page = browser.new_page(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=device_scale_factor,
    )
    try:
        page.goto(url)
        css = cosmetic_css(zoom=zoom, font_scale=font_scale, font_family=font_family)
        if css:
            page.add_style_tag(content=css)
            page.wait_for_timeout(80)  # let the reflow settle
        backend = PlaywrightBackend(page)
        workflow = Workflow.load(bundle_dir)
        report = Replayer(backend, use_structural=use_structural).run(
            workflow,
            params=params,
            bundle_dir=Path(bundle_dir),
            run_dir=Path(run_dir),
        )
        state = {
            "hash": page.evaluate("location.hash"),
            "banner": page.evaluate(
                "(document.getElementById('saved-banner') || {}).textContent || null"
            ),
        }
    finally:
        page.close()
    return report, state


def failing_step(report: RunReport):
    """The first failed StepResult, or None."""
    for result in report.results:
        if not result.ok:
            return result
    return None


def describe(report: RunReport, state: dict) -> str:
    """Compact run description for assertion messages."""
    lines = [
        f"success={report.success} rungs={report.rung_counts} "
        f"heals={report.heal_count} state={state}"
    ]
    for r in report.results:
        rung = r.resolution.rung if r.resolution else "-"
        lines.append(
            f"  {r.step_id} ok={r.ok} rung={rung} pc={r.postconditions_ok} "
            f"err={r.error}"
        )
    return "\n".join(lines)
