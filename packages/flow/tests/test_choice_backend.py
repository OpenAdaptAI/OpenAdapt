"""The dropdown choice actuator on the real widget families, in headless Chromium.

Each page of the synthetic registration app (benchmark/choice_widgets) is a
real widget: vendored Select2, Chosen and Tom Select, a native select, an ARIA
listbox, a ``<div>`` list that chooses on press, and a type-ahead whose
options load from the server. The actuator must choose by label or value on
every one, wait for late options, and refuse instead of guessing when the
option is missing, duplicated or disabled. The app's stored record is never
involved here: this is the actuator alone (test_choice_widgets_e2e covers
record, compile and run).
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from benchmark.choice_widgets.app import ChoiceApp, Server
from openadapt_flow.backends.choice import ChoiceRefused
from openadapt_flow.ir import ChoiceOptionShape, OptionChoice, Step

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(300)

ROLE = ChoiceOptionShape(role="option", tag="li")
#: kind -> (control selector, contract fields, field read back from the page)
WIDGETS: dict[str, tuple[str, dict[str, Any]]] = {
    "native": ("#department", {"kind": "native_select", "readback": "select"}),
    "aria_listbox": (
        "#department-btn",
        {"kind": "listed_option", "option": ROLE, "readback": "control_text"},
    ),
    "div_dropdown": (
        ".dd",
        {
            "kind": "listed_option",
            "option": ChoiceOptionShape(tag="div", classes=("dd-item",)),
            "readback": "control_text",
        },
    ),
    "select2": (
        "#select2-department-container",
        {
            "kind": "listed_option",
            "option": ROLE,
            "query": "label",
            "readback": "control_text",
        },
    ),
    "chosen": (
        ".chosen-single span",
        {
            "kind": "listed_option",
            "option": ChoiceOptionShape(tag="li", classes=("active-result",)),
            "query": "label",
            "readback": "control_text",
        },
    ),
    "tom_select": (
        ".ts-control",
        {
            "kind": "listed_option",
            "option": ChoiceOptionShape(role="option", tag="div"),
            "query": "label",
            "readback": "control_text",
        },
    ),
    "async_combobox": (
        "#department-input",
        {
            "kind": "listed_option",
            "option": ROLE,
            "query": "label",
            "readback": "control_value",
        },
    ),
}


@pytest.fixture(scope="module")
def server() -> Iterator[Server]:
    srv = ChoiceApp().serve()
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        b = playwright.chromium.launch(headless=True)
        yield b
        b.close()


def _choose(
    server: Server, browser: Any, kind: str, wanted: str, query: str = ""
) -> tuple[dict[str, Any] | ChoiceRefused, str, bool]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    selector, fields = WIDGETS[kind]
    contract = OptionChoice(label="Cardiology", settle_s=4.0, **fields)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{server.base}/w/{kind}" + (f"?{query}" if query else ""))
        page.wait_for_selector("#patient_name", state="visible")
        page.wait_for_timeout(200)
        box = page.locator(selector).first.bounding_box()
        assert box is not None
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        backend = PlaywrightBackend(page)
        try:
            outcome: dict[str, Any] | ChoiceRefused = backend.choose_option(
                contract, wanted, point=point
            )
        except ChoiceRefused as exc:
            outcome = exc
        page.wait_for_timeout(300)
        confirmed = backend.confirm_choice()
        value = page.evaluate("document.querySelector('[name=department]').value")
        return outcome, value, confirmed
    finally:
        page.close()


@pytest.mark.parametrize("kind", sorted(WIDGETS))
def test_chooses_a_held_out_option_by_its_label(
    server: Server, browser: Any, kind: str
) -> None:
    outcome, value, confirmed = _choose(server, browser, kind, "Orthopaedics")
    assert isinstance(outcome, dict), outcome
    assert outcome["label"] == "orthopaedics"
    assert value == "ORTH"
    assert confirmed


@pytest.mark.parametrize(
    "kind", ["native", "aria_listbox", "async_combobox", "select2"]
)
def test_reordered_late_options_are_found_by_label_not_position(
    server: Server, browser: Any, kind: str
) -> None:
    outcome, value, _ = _choose(
        server, browser, kind, "Cardiology", "order=rev&delay=800"
    )
    assert isinstance(outcome, dict), outcome
    assert value == "CARD"


def test_native_select_matches_the_option_value_too(
    server: Server, browser: Any
) -> None:
    outcome, value, _ = _choose(server, browser, "native", "DERM")
    assert isinstance(outcome, dict), outcome
    assert value == "DERM"


@pytest.mark.parametrize(
    "kind", ["native", "aria_listbox", "div_dropdown", "select2", "async_combobox"]
)
@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("missing=CARD", "choice_missing"),
        ("dup=CARD", "choice_ambiguous"),
        ("disabled=CARD", "choice_disabled"),
    ],
)
def test_refuses_instead_of_guessing(
    server: Server, browser: Any, kind: str, query: str, code: str
) -> None:
    if kind == "select2" and code == "choice_disabled":
        pytest.skip(
            "Select2 hides disabled options from a search; that is choice_missing"
        )
    outcome, value, confirmed = _choose(server, browser, kind, "Cardiology", query)
    assert isinstance(outcome, ChoiceRefused), outcome
    assert outcome.code == code
    assert "Cardiology" not in str(outcome)
    # Nothing was chosen.
    assert value in ("", None)
    assert not confirmed


def test_a_choice_the_page_reverts_is_not_confirmed(
    server: Server, browser: Any
) -> None:
    # The page asks an extra confirm() and Playwright dismisses it, so the
    # page keeps its old value: the read-back must notice.
    outcome, value, _ = _choose(
        server, browser, "aria_listbox", "Cardiology", "surprise=confirm"
    )
    assert isinstance(outcome, ChoiceRefused)
    assert outcome.code == "choice_unverified"
    assert value == ""


def test_choice_contract_validation() -> None:
    with pytest.raises(ValueError):
        OptionChoice(kind="listed_option", label="A", readback="control_text")
    with pytest.raises(ValueError):
        OptionChoice(kind="native_select", label="A", readback="control_text")
    with pytest.raises(ValueError):
        ChoiceOptionShape(tag="li", classes=("a b",))
    with pytest.raises(ValueError):
        OptionChoice(
            kind="listed_option",
            label="15",
            option=ROLE,
            readback="control_text",
            shows="2026-11-15",
        )
    contract = OptionChoice(
        kind="listed_option", label="A", option=ROLE, readback="control_text"
    )
    anchor = {"region": [0, 0, 10, 10], "template": "t.png", "click_point": [5, 5]}
    step = Step.model_validate(
        {
            "id": "s1",
            "intent": "choose",
            "action": "select_option",
            "anchor": anchor,
            "param": "department",
            "choice": contract.model_dump(mode="json"),
        }
    )
    assert step.choice == contract
    # A literal choice must choose its demonstrated label.
    with pytest.raises(ValueError):
        Step.model_validate(
            {
                "id": "s1",
                "intent": "choose",
                "action": "select_option",
                "anchor": anchor,
                "text": "B",
                "choice": contract.model_dump(mode="json"),
            }
        )
    # A step without a choice serializes exactly as before.
    plain = Step.model_validate(
        {"id": "s2", "intent": "type", "action": "type", "text": "x"}
    )
    assert "choice" not in plain.model_dump(mode="json")
