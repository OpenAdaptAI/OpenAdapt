"""The one-time-code handoff in a real headless Chromium page.

The page is synthetic. A fake ``obtain`` plays the person, so no source,
network or sign-in is involved; what is under test is the browser side
(:mod:`openadapt_flow.backends.one_time_field`): the field is bound before
the wait, masked on screen, re-checked before typing, typed once through the
bound element, and never shown to a model's page listing.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator, Optional

import pytest

from openadapt_flow import second_factor as sf
from openadapt_flow.backends.one_time_field import one_time_field_for
from openadapt_flow.runtime.second_factor_step import DeliveryLedger, Handoff

pytest.importorskip("playwright")

PAGE = """<!doctype html><meta charset=utf-8>
<style>body{font:20px sans-serif} input{font:28px monospace;width:12ch}</style>
<form id=f action="/done" onsubmit="return false">
  <label for=code>Code</label>
  <input id=code name=code autocomplete="one-time-code" inputmode=numeric>
  <input id=other name=other>
  <button id=verify>Verify</button>
</form>"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        yield launched
        launched.close()


@pytest.fixture
def backend(browser: Any) -> Iterator[Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    context = browser.new_context(viewport={"width": 640, "height": 240})
    page = context.new_page()
    page.set_content(PAGE)
    page.focus("#code")
    yield PlaywrightBackend(page)
    context.close()


def _person(
    code: str, during: Optional[Callable[[], None]] = None, calls: Optional[list] = None
) -> Any:
    def obtain(kind: str, step_id: str) -> Optional[sf.OneTimeCode]:
        if calls is not None:
            calls.append(kind)
        if during is not None:
            during()
        return sf.OneTimeCode(code)

    return obtain


def _deliver(backend: Any, obtain: Any) -> bool:
    field = one_time_field_for(backend)
    assert field is not None
    return Handoff(field, DeliveryLedger(), obtain=obtain).deliver_code("s", key="s")


def test_the_code_is_typed_once_and_never_rendered(backend: Any) -> None:
    page = backend.page
    assert _deliver(backend, _person("482913")) is True
    assert page.eval_on_selector("#code", "el => el.value") == "482913"
    first = page.locator("#code").screenshot()
    # Another code of the same length renders the same pixels: the screen
    # (and so every screenshot, gate frame or model image) shows dots only.
    page.set_content(PAGE)
    page.focus("#code")
    assert _deliver(backend, _person("917305")) is True
    assert page.locator("#code").screenshot() == first
    # A field that is not masked would differ, so the comparison means something.
    page.fill("#other", "482913")
    plain = page.locator("#other").screenshot()
    page.fill("#other", "917305")
    assert page.locator("#other").screenshot() != plain


def test_a_new_document_masks_its_code_field_too(backend: Any) -> None:
    assert _deliver(backend, _person("482913")) is True
    page = backend.page
    page.goto("data:text/html," + PAGE.replace("#", "%23"))
    security = page.eval_on_selector(
        "#code",
        "el => getComputedStyle(el).getPropertyValue('-webkit-text-security')",
    )
    assert security == "disc"


def test_focus_taken_while_the_person_answers_types_nothing(backend: Any) -> None:
    page = backend.page
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _deliver(backend, _person("482913", during=lambda: page.focus("#other")))
    assert refused.value.code == "second_factor_unbound"
    values = page.eval_on_selector_all("input", "els => els.map((e) => e.value)")
    assert values == ["", ""]


def test_a_prefilled_field_is_refused_before_anyone_is_asked(backend: Any) -> None:
    page = backend.page
    page.fill("#code", "11")
    page.focus("#code")
    calls: list[str] = []
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _deliver(backend, _person("482913", calls=calls))
    assert refused.value.code == "second_factor_unbound" and calls == []
    assert page.eval_on_selector("#code", "el => el.value") == "11"


def test_focus_on_an_ordinary_field_is_refused(backend: Any) -> None:
    backend.page.focus("#other")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _deliver(backend, _person("482913"))
    assert refused.value.code == "second_factor_unbound"


def test_a_code_page_that_submits_itself_is_delivered_once(backend: Any) -> None:
    page = backend.page
    page.evaluate(
        """() => document.getElementById('code').addEventListener('input', (e) => {
            if (e.target.value.length === 6) document.body.innerHTML = '<p>Signed in</p>';
        })"""
    )
    assert _deliver(backend, _person("482913")) is False
    assert page.inner_text("body") == "Signed in"


def test_the_agents_page_listing_never_holds_the_code(backend: Any) -> None:
    from openadapt_flow.compile_agent import observe

    assert _deliver(backend, _person("482913")) is True
    listing = observe.observe(backend.page)
    assert "482913" not in json.dumps(listing, default=str)
