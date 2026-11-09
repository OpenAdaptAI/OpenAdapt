"""When a click that reports its page closed counts as delivered.

A popup can close itself from the click that finishes it, and Playwright can
then report the page closed after delivering the click. The click counts as
delivered only when the step expected its popup to close, the click ran on a
popup surface that was open when it started, and the popup is confirmed
closed. Every other "page closed" report leaves delivery uncertain.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.backend import ActionDeliveryUncertain, PageCloseExpectationBackend
from openadapt_flow.backends import playwright_backend as pw
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import ActionKind, Step, SurfaceSpec, Workflow
from openadapt_flow.runtime.replayer import Replayer


class TargetClosedError(Exception):
    """Stands in for Playwright's error of the same name."""


class _Mouse:
    def __init__(self, page: "_Page") -> None:
        self._page = page
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int, **_: Any) -> None:
        self.clicks.append((x, y))
        self._page.on_click()

    dblclick = click


class _Page:
    """A page whose click closes it (or not) and then raises (or not)."""

    def __init__(
        self,
        *,
        closes: bool = True,
        raises: Optional[Exception] = None,
        closed: bool = False,
    ) -> None:
        self.mouse = _Mouse(self)
        self._closes = closes
        self._raises = raises
        self.closed = closed

    def on_click(self) -> None:
        if self._closes:
            self.closed = True
        if self._raises is not None:
            raise self._raises

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_timeout(self, ms: float) -> None:
        if self.closed:
            raise TargetClosedError("closed")


def _backend(page: _Page, *, popup: bool = True) -> PlaywrightBackend:
    return PlaywrightBackend(page, closed_page_frame=popup)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fast_confirm(monkeypatch):
    monkeypatch.setattr(pw, "_PAGE_CLOSE_CONFIRM_S", 0.05)


def test_expected_close_is_delivered() -> None:
    page = _Page(raises=TargetClosedError("Target page has been closed"))
    backend = _backend(page)
    assert isinstance(backend, PageCloseExpectationBackend)
    backend.expect_page_close()
    backend.click(10, 20)
    assert page.mouse.clicks == [(10, 20)]
    assert backend.page_closed()


def test_unexpected_close_stays_uncertain() -> None:
    backend = _backend(_Page(raises=TargetClosedError("closed")))
    with pytest.raises(ActionDeliveryUncertain) as caught:
        backend.click(10, 20)
    assert caught.value.operation == "coordinate_click"
    assert caught.value.cause_type == "TargetClosedError"


def test_expectation_is_honoured_only_on_a_popup_surface() -> None:
    backend = _backend(_Page(raises=TargetClosedError("closed")), popup=False)
    backend.expect_page_close()
    with pytest.raises(ActionDeliveryUncertain):
        backend.click(10, 20)


def test_page_still_open_after_the_error_stays_uncertain() -> None:
    backend = _backend(_Page(closes=False, raises=TargetClosedError("closed")))
    backend.expect_page_close()
    with pytest.raises(ActionDeliveryUncertain):
        backend.click(10, 20)


def test_page_already_closed_before_the_click_stays_uncertain() -> None:
    backend = _backend(_Page(closed=True, raises=TargetClosedError("closed")))
    backend.expect_page_close()
    with pytest.raises(ActionDeliveryUncertain):
        backend.click(10, 20)


def test_expectation_is_one_shot() -> None:
    page = _Page(closes=False, raises=None)
    backend = _backend(page)
    backend.expect_page_close()
    backend.click(1, 1)  # consumes the expectation without closing
    page._closes, page._raises = True, TargetClosedError("closed")
    with pytest.raises(ActionDeliveryUncertain):
        backend.click(2, 2)


def test_cancelled_expectation_is_not_honoured() -> None:
    backend = _backend(_Page(raises=TargetClosedError("closed")))
    backend.expect_page_close()
    backend.cancel_page_close()
    with pytest.raises(ActionDeliveryUncertain):
        backend.click(10, 20)


def test_other_errors_are_not_reinterpreted() -> None:
    backend = _backend(_Page(raises=RuntimeError("boom")))
    backend.expect_page_close()
    with pytest.raises(RuntimeError, match="boom"):
        backend.click(10, 20)


# ---------------------------------------------------------------- the IR


def _surfaces() -> list[SurfaceSpec]:
    return [
        SurfaceSpec(id="main", kind="web"),
        SurfaceSpec(
            id="popup_1",
            kind="web",
            opened_by_step="s0",
            opener="main",
            opener_link="window_opener",
            origin="https://app.test",
            url_pattern=r"https://app\.test/approve",
        ),
    ]


def _steps(closing: Step) -> list[Step]:
    return [
        Step(id="s0", intent="open", action=ActionKind.CLICK, opens=["popup_1"]),
        Step(
            id="s1",
            intent="switch",
            action=ActionKind.SWITCH_SURFACE,
            surface="popup_1",
        ),
        closing,
    ]


def test_closes_surface_is_valid_on_a_popup_click() -> None:
    Workflow(
        name="w",
        surfaces=_surfaces(),
        steps=_steps(
            Step(
                id="s2",
                intent="approve",
                action=ActionKind.CLICK,
                surface="popup_1",
                closes_surface=True,
            )
        ),
    )


def test_closes_surface_needs_a_click() -> None:
    with pytest.raises(ValueError, match="only on a click"):
        Step(
            id="s2",
            intent="type",
            action=ActionKind.TYPE,
            text="x",
            surface="popup_1",
            closes_surface=True,
        )


def test_closes_surface_needs_a_page_surface() -> None:
    with pytest.raises(ValueError, match="does not run on a page a step opened"):
        Workflow(
            name="w",
            surfaces=_surfaces(),
            steps=[
                Step(
                    id="s0",
                    intent="open",
                    action=ActionKind.CLICK,
                    surface="main",
                    opens=["popup_1"],
                    closes_surface=True,
                ),
            ],
        )


# ------------------------------------------------------------ the replayer


class _ExpectingBackend:
    def __init__(self) -> None:
        self.events: list[str] = []

    def expect_page_close(self) -> None:
        self.events.append("expect")

    def cancel_page_close(self) -> None:
        self.events.append("cancel")

    def page_closed(self) -> bool:
        return False


@pytest.mark.parametrize("closes", [True, False])
def test_replayer_arms_the_expectation_only_for_a_closing_step(
    monkeypatch, closes: bool
) -> None:
    backend = _ExpectingBackend()
    replayer = Replayer.__new__(Replayer)
    replayer.backend = backend  # type: ignore[assignment]

    def act(step, resolution, params, **kwargs):
        backend.events.append("act")
        return None

    monkeypatch.setattr(replayer, "_act_answering_dialog", act)
    step = Step(
        id="s2",
        intent="approve",
        action=ActionKind.CLICK,
        surface="popup_1",
        closes_surface=closes,
    )
    assert replayer._act(step, None, {}) is None
    assert backend.events == (["expect", "act", "cancel"] if closes else ["act"])
