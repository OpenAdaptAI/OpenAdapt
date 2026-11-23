"""The reference human must see the estimator's value arrive in the form.

Harborline's "Use This Value" button writes the estimate into the parent form
and closes its own window. Playwright's click can raise after that window
closes, so the driver accepts a closed popup. It must still fail when the
popup closed without writing a value to the parent box: otherwise the
reference human would type the answer-key value itself and the selfcheck
would pass on a case no real filler could finish. These tests drive
``run_estimator`` with stand-in page objects; no browser is started.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from benchmark.form_tx_heldout.drivers import harborline
from benchmark.form_tx_heldout.drivers.base import DriverError
from benchmark.form_tx_heldout.portals import PORTALS
from benchmark.form_tx_heldout.reference_human import make_source
from benchmark.form_tx_heldout.scoring import load_answer_key

LABEL = "Dwelling Coverage (A)"


class Box:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def input_value(self) -> str:
        return self.value

    def fill(self, value: str) -> None:
        self.value = value


class Button:
    def __init__(self, on_click: Any) -> None:
        self.on_click = on_click

    def click(self, **_kw: Any) -> None:
        self.on_click()


class Text:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self) -> str:
        return self.text


class Popup:
    """The estimator window: shows ``displayed`` and, on use, hands ``handed``."""

    def __init__(
        self, box: Box, displayed: int, handed: str | None, raise_on_close: bool
    ) -> None:
        self.box = box
        self.displayed = displayed
        self.handed = handed
        self.raise_on_close = raise_on_close
        self.closed = False

    def wait_for_load_state(self) -> None:
        pass

    def is_closed(self) -> bool:
        return self.closed

    @contextmanager
    def expect_event(self, _name: str) -> Iterator[None]:
        yield

    def use_it(self) -> None:
        if self.handed is not None:
            self.box.fill(self.handed)
        self.closed = True
        if self.raise_on_close:
            raise RuntimeError("Target page, context or browser has been closed")

    def locator(self, sel: str) -> Any:
        if sel == "#rce_value":
            return Text(f"${self.displayed:,}")
        if sel == "input[value='Use This Value']":
            return Button(self.use_it)
        return Button(lambda: None)  # Calculate


class Cell:
    def __init__(self, box: Box) -> None:
        self.box = box

    def locator(self, sel: str) -> Any:
        return self.box if sel == "input[type=text]" else Button(lambda: None)


class Page:
    def __init__(self, pop: Popup) -> None:
        self.pop = pop

    @contextmanager
    def expect_popup(self) -> Iterator[Any]:
        yield type("Info", (), {"value": self.pop})()


def harborline_case() -> tuple[Any, int]:
    """A harborline case and its expected dwelling coverage, largest first."""
    cases = load_answer_key()["cases"]
    cid = max(
        PORTALS["harborline"].cases,
        key=lambda c: int(cases[c]["expected_record"]["cov_a"]),
    )
    return make_source("harborline", cid), int(cases[cid]["expected_record"]["cov_a"])


def rounded(value: int) -> str:
    handed = int(round(value / 10000.0)) * 10000 if value >= 500000 else value
    return f"{handed:,}"


def run(
    monkeypatch: pytest.MonkeyPatch, handed: str | None, raise_on_close: bool
) -> tuple[Box, Any, int]:
    monkeypatch.setattr(harborline, "fill_rows", lambda _page, _src: None)
    src, want = harborline_case()
    box = Box()
    pop = Popup(box, want, handed(want) if callable(handed) else handed, raise_on_close)
    harborline.run_estimator(Page(pop), Cell(box), LABEL, src)
    return box, src, want


@pytest.mark.parametrize("raise_on_close", [False, True])
def test_rounded_handoff_is_corrected_to_the_answer(
    monkeypatch: pytest.MonkeyPatch, raise_on_close: bool
) -> None:
    box, src, want = run(monkeypatch, rounded, raise_on_close)
    assert box.value == str(want)
    assert src.problems == []


@pytest.mark.parametrize("raise_on_close", [False, True])
def test_popup_that_closes_without_handing_a_value_fails(
    monkeypatch: pytest.MonkeyPatch, raise_on_close: bool
) -> None:
    with pytest.raises(DriverError, match="closed without handing"):
        run(monkeypatch, None, raise_on_close)


def test_popup_that_hands_a_wrong_value_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DriverError, match="closed without handing"):
        run(monkeypatch, lambda want: f"{want + 50000:,}", True)


def test_popup_that_raises_while_open_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harborline, "fill_rows", lambda _page, _src: None)
    src, want = harborline_case()
    box = Box()
    pop = Popup(box, want, rounded(want), False)

    def broken() -> None:
        raise RuntimeError("element is not attached")

    pop.use_it = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="not attached"):
        harborline.run_estimator(Page(pop), Cell(box), LABEL, src)
