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

from pathlib import Path
from typing import Any, Iterator

import pytest

from benchmark.choice_widgets.app import ChoiceApp, Server
from openadapt_flow.backend import ActionDeliveryUncertain
from openadapt_flow.backends.choice import BrowserChoice, ChoiceRefused
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
    server: Server,
    browser: Any,
    kind: str,
    wanted: str,
    query: str = "",
    prepare: Any = None,
    check: Any = None,
) -> tuple[dict[str, Any] | ChoiceRefused | ActionDeliveryUncertain, str, bool]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    selector, fields = WIDGETS[kind]
    contract = OptionChoice(
        label="Cardiology",
        value="CARD",
        settle_s=4.0,
        commit_field="department",
        **fields,
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{server.base}/w/{kind}" + (f"?{query}" if query else ""))
        page.wait_for_selector("#patient_name", state="visible")
        page.wait_for_timeout(200)
        if prepare is not None:
            prepare(page)
        box = page.locator(selector).first.bounding_box()
        assert box is not None
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        backend = PlaywrightBackend(page)
        try:
            outcome: dict[str, Any] | ChoiceRefused | ActionDeliveryUncertain = (
                backend.choose_option(contract, wanted, point=point)
            )
        except (ChoiceRefused, ActionDeliveryUncertain) as exc:
            outcome = exc
        page.wait_for_timeout(300)
        confirmed = backend.confirm_choice()
        value = page.evaluate("document.querySelector('[name=department]').value")
        if check is not None:
            check(page)
        return outcome, value, confirmed
    finally:
        page.close()


@pytest.mark.parametrize("kind", sorted(WIDGETS))
def test_chooses_a_held_out_option_by_its_label(
    server: Server, browser: Any, kind: str
) -> None:
    outcome, value, confirmed = _choose(server, browser, kind, "Orthopaedics")
    if kind in {"aria_listbox", "div_dropdown", "async_combobox"}:
        # No label-to-code mapping is exposed by these plain ARIA lists.
        # A held-out label must refuse before selection, not guess its code.
        assert isinstance(outcome, ActionDeliveryUncertain), outcome
        assert outcome.operation == "choice_open"
        assert outcome.cause_type == "choice_commit_unknown"
        assert value == ""
        assert not confirmed
        return
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
    if kind == "native":
        assert isinstance(outcome, ChoiceRefused), outcome
        assert outcome.code == code
    else:
        assert isinstance(outcome, ActionDeliveryUncertain), outcome
        assert outcome.operation == "choice_open"
        assert outcome.cause_type == code
    assert "Cardiology" not in str(outcome)
    # Nothing was chosen.
    assert value in ("", None)
    assert not confirmed


@pytest.mark.parametrize("kind", ["aria_listbox", "div_dropdown", "chosen"])
@pytest.mark.parametrize(
    "fault",
    [
        "reorder",
        "duplicate",
        "disabled",
        "removed",
        "ancestor_disabled",
        "fieldset_disabled",
    ],
)
def test_rechecks_the_semantic_option_after_the_list_settles(
    server: Server,
    browser: Any,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    fault: str,
) -> None:
    # Mutate the real DOM in the gap between the census and actuation. A
    # reordered list still chooses the intended element; an invalidated
    # choice never dispatches a pointer event to an option.
    original = BrowserChoice._settled_matches

    def mutate(self: Any, scope: Any, shape: Any, *args: Any) -> Any:
        result = original(self, scope, shape, *args)
        scope.evaluate(
            """([shape, fault]) => {
              const selector = shape.role ? '[role="' + shape.role + '"]'
                : shape.tag + shape.classes.map(c => '.' + CSS.escape(c)).join('');
              const items = Array.from(document.querySelectorAll(selector));
              const target = items.find(el => el.textContent.trim() === 'Cardiology');
              if (fault === 'reorder') {
                for (const el of items.reverse()) el.parentElement.appendChild(el);
              } else if (fault === 'duplicate') {
                target.parentElement.appendChild(target.cloneNode(true));
              } else if (fault === 'disabled') {
                target.setAttribute('aria-disabled', 'true');
              } else if (fault === 'ancestor_disabled') {
                target.parentElement.classList.add('disabled');
              } else if (fault === 'fieldset_disabled') {
                const group = document.createElement('fieldset'); group.disabled = true;
                target.parentElement.appendChild(group); group.appendChild(target);
              } else {
                target.remove();
              }
              window.optionPresses = 0;
              for (const el of document.querySelectorAll(selector)) {
                el.addEventListener('mousedown', () => window.optionPresses++);
              }
            }""",
            [shape, fault],
        )
        return result

    monkeypatch.setattr(BrowserChoice, "_settled_matches", mutate)
    presses: list[int] = []
    outcome, value, confirmed = _choose(
        server,
        browser,
        kind,
        "Cardiology",
        check=lambda page: presses.append(page.evaluate("window.optionPresses")),
    )
    if fault == "reorder":
        assert presses == [1]
        assert isinstance(outcome, dict), outcome
        assert value == "CARD"
        assert confirmed
    else:
        assert isinstance(outcome, ActionDeliveryUncertain), outcome
        assert outcome.operation == "choice_open"
        assert outcome.cause_type == "choice_changed"
        assert presses == [0]
        assert value in ("", None)
        assert not confirmed


@pytest.mark.parametrize("container", ["optgroup", "fieldset"])
def test_native_select_respects_inherited_disabled_state(
    server: Server, browser: Any, container: str
) -> None:
    def prepare(page: Any) -> None:
        page.evaluate(
            """(container) => {
              const select = document.querySelector('#department');
              const group = document.createElement(container);
              group.disabled = true;
              if (container === 'optgroup') {
                select.appendChild(group);
                group.appendChild(select.querySelector('[value=CARD]'));
              } else {
                select.parentElement.appendChild(group);
                group.appendChild(select);
              }
            }""",
            container,
        )

    outcome, value, confirmed = _choose(
        server, browser, "native", "Cardiology", prepare=prepare
    )
    assert isinstance(outcome, ChoiceRefused), outcome
    assert outcome.code == "choice_disabled"
    assert value in ("", None)
    assert not confirmed


@pytest.mark.parametrize(
    "fault", ["reorder", "relabel", "duplicate", "disabled", "removed", "shared_value"]
)
def test_native_semantics_are_rechecked_before_dispatching_change(
    server: Server, browser: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    original = BrowserChoice._commit_native

    def mutate(handle: Any, chosen: Any, want: str, wanted: str) -> bool:
        handle.evaluate(
            """(el, fault) => {
              const select = el.closest('select');
              const target = Array.from(select.options).find(o => o.text === 'Cardiology');
              window.nativeEffects = [];
              select.addEventListener('change', () =>
                window.nativeEffects.push(select.selectedOptions[0].text));
              if (fault === 'reorder') {
                for (const option of Array.from(select.options).reverse()) select.appendChild(option);
              } else if (fault === 'relabel') {
                target.text = 'Wrong department';
              } else if (fault === 'duplicate') {
                select.appendChild(target.cloneNode(true));
              } else if (fault === 'disabled') {
                const group = document.createElement('optgroup');
                group.disabled = true; select.appendChild(group); group.appendChild(target);
              } else if (fault === 'shared_value') {
                const other = target.cloneNode(true); other.text = 'Different label';
                select.insertBefore(other, target);
              } else {
                target.remove();
              }
            }""",
            fault,
        )
        return original(handle, chosen, want, wanted)

    monkeypatch.setattr(BrowserChoice, "_commit_native", staticmethod(mutate))
    effects: list[list[str]] = []
    outcome, value, confirmed = _choose(
        server,
        browser,
        "native",
        "Cardiology",
        check=lambda page: effects.append(page.evaluate("window.nativeEffects")),
    )
    if fault in ("reorder", "shared_value"):
        assert isinstance(outcome, dict), outcome
        assert effects == [["Cardiology"]]
        assert value == "CARD"
        assert confirmed
    else:
        assert isinstance(outcome, ChoiceRefused), outcome
        assert outcome.code == "choice_changed"
        assert effects == [[]]
        assert value in ("", None)
        assert not confirmed


@pytest.mark.parametrize("kind", ["alert", "confirm", "beforeunload"])
def test_all_unexpected_browser_dialogs_are_dismissed_and_retained(
    server: Server, browser: Any, kind: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page()
    try:
        page.goto(server.base + "/w/native")
        page.locator("#patient_name").click()
        page.keyboard.type("Synthetic unsaved edits")
        backend = PlaywrightBackend(page)
        backend.watch_dialogs()
        if kind == "beforeunload":
            page.evaluate("""() => window.addEventListener('beforeunload', e => {
              e.preventDefault(); e.returnValue = '';
            })""")
            with pytest.raises(sync_api.Error, match="ERR_ABORTED"):
                page.goto(server.base + "/w/chosen")
            assert page.url.endswith("/w/native")
            assert (
                page.locator("#patient_name").input_value() == "Synthetic unsaved edits"
            )
        else:
            page.evaluate("kind => window[kind]('Synthetic warning')", kind)
        seen = backend.take_unexpected_dialogs()
        assert len(seen) == 1
        assert seen[0]["type"] == kind
        assert seen[0]["response"] == "dismiss"
        assert seen[0]["matched"] is False
    finally:
        page.close()


def test_a_choice_the_page_reverts_is_not_confirmed(
    server: Server, browser: Any
) -> None:
    # The page asks an extra confirm() and Playwright dismisses it, so the
    # page keeps its old value: the read-back must notice.
    outcome, value, _ = _choose(
        server, browser, "aria_listbox", "Cardiology", "surprise=confirm"
    )
    assert isinstance(outcome, ActionDeliveryUncertain)
    assert outcome.cause_type == "choice_unverified"
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


@pytest.mark.parametrize("kind", ["native", "aria_listbox"])
def test_a_changing_option_census_is_never_accepted_at_its_deadline(
    server: Server, browser: Any, kind: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    selector, fields = WIDGETS[kind]
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(server.base + "/w/" + kind)
        page.evaluate(
            """kind => {
          window.choiceEffects = 0;
          document.querySelector('[name=department]').addEventListener('change', () => window.choiceEffects++);
              document.addEventListener('pointerdown', event => {
                if (event.target.closest('[role=option]')) window.choiceEffects++;
              }, true);
          let n = 0;
          window.changingOptions = setInterval(() => {
            const options = document.querySelectorAll(kind === 'native' ? 'select option' : '[role=option]');
            for (const option of options) {
              if (option.textContent.startsWith('Dermatology')) option.textContent = 'Dermatology ' + (++n);
            }
          }, 35);
        }""",
            kind,
        )
        box = page.locator(selector).first.bounding_box()
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        backend = PlaywrightBackend(page)
        with pytest.raises(
            ChoiceRefused if kind == "native" else ActionDeliveryUncertain
        ) as error:
            backend.choose_option(
                OptionChoice(
                    label="Cardiology",
                    settle_s=0.6,
                    commit_field="department",
                    **fields,
                ),
                "Cardiology",
                point=point,
            )
        assert (
            error.value.code
            if isinstance(error.value, ChoiceRefused)
            else error.value.cause_type
        ) == "choice_unsettled"
        assert page.locator("[name=department]").input_value() == ""
        assert page.evaluate("window.choiceEffects") == 0
    finally:
        page.close()


@pytest.mark.parametrize("kind", ["native", "aria_listbox"])
@pytest.mark.parametrize(
    "context", ["ordinary", "governed", "fault", "production", "environment"]
)
def test_receipt_bound_choices_refuse_before_real_browser_input(
    server: Server, browser: Any, kind: str, context: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import (
        Anchor,
        Resolution,
        StepResult,
        StructuralLocator,
        Workflow,
    )
    from openadapt_flow.runtime import Replayer

    selector, fields = WIDGETS[kind]
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(server.base + "/w/" + kind)
        page.evaluate("""() => {
          window.choiceEffects = 0;
          document.querySelector('[name=department]').addEventListener('change', () => window.choiceEffects++);
              document.addEventListener('pointerdown', event => {
                if (event.target.closest('[role=option]')) window.choiceEffects++;
              }, true);
        }""")
        box = page.locator(selector).first.bounding_box()
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        step = Step(
            id="select",
            intent="choose department",
            action="select_option",
            text="Cardiology",
            anchor=Anchor(
                template="t.png",
                region=(0, 0, 1280, 800),
                click_point=point,
                structural=StructuralLocator(
                    selector=selector, tag="select" if kind == "native" else "button"
                ),
            ),
            choice=OptionChoice(
                label="Cardiology", value="CARD", commit_field="department", **fields
            ),
        )
        workflow = Workflow(name="choice", steps=[step])
        replay = Replayer(PlaywrightBackend(page))
        attribute = {
            "governed": "governed_authorization",
            "fault": "qualification_fault_driver",
            "production": "production_qualification_guard",
            "environment": "qualification_environment_observer",
        }.get(context)
        if attribute is not None:
            # An unsupported action must refuse before consulting authority
            # or environment capabilities, let alone invoking the backend.
            setattr(replay, attribute, object())
        result = StepResult(step_id=step.id, intent=step.intent, ok=False)
        error = replay._act_choice(
            step,
            Resolution(rung="structural", point=point, confidence=1.0, elapsed_ms=0),
            {},
            workflow,
            result,
        )
        if context == "ordinary":
            assert error is None
            assert page.locator("[name=department]").input_value() == "CARD"
            assert page.evaluate("window.choiceEffects") == 1
        else:
            assert error is not None and "refusing before input" in error
            assert page.locator("[name=department]").input_value() == ""
            assert page.evaluate("window.choiceEffects") == 0
            assert result.delivery_attempted in (None, False)
            assert result.safety_halt
    finally:
        page.close()


@pytest.mark.parametrize(
    ("kind", "fault"),
    [
        ("aria_listbox", "wrong_code"),
        ("div_dropdown", "wrong_code"),
        ("async_combobox", "wrong_code"),
        ("async_combobox", "ignored_option"),
    ],
)
def test_label_or_query_echo_cannot_hide_an_uncommitted_or_wrong_value(
    server: Server, browser: Any, kind: str, fault: str
) -> None:
    def prepare(page: Any) -> None:
        page.evaluate(
            """fault => {
          document.addEventListener('mousedown', event => {
            if (!event.target.closest('[role=option],.dd-item')) return;
            if (fault === 'ignored_option') {
              event.preventDefault(); event.stopImmediatePropagation();
            } else {
              setTimeout(() => document.querySelector('[name=department]').value = 'WRONG', 0);
            }
          }, true);
          if (fault === 'ignored_option') document.addEventListener('click', event => {
            if (event.target.closest('[role=option]')) {
              event.preventDefault(); event.stopImmediatePropagation();
            }
          }, true);
        }""",
            fault,
        )

    observed: list[dict[str, str]] = []
    outcome, value, confirmed = _choose(
        server,
        browser,
        kind,
        "Cardiology",
        prepare=prepare,
        check=lambda page: observed.append(
            page.evaluate("""() => ({
          shown: document.querySelector('#department-input')?.value
            || document.querySelector('#department-btn')?.textContent
            || document.querySelector('.dd-value')?.textContent || '',
          committed: document.querySelector('[name=department]').value
        })""")
        ),
    )
    assert isinstance(outcome, ActionDeliveryUncertain), outcome
    assert outcome.operation == "choice_click"
    assert outcome.cause_type == "choice_unverified"
    assert value in ("", "WRONG")
    assert not confirmed
    if kind == "async_combobox":
        assert observed[0]["shown"] == "Cardiology"
    assert observed[0]["committed"] != "CARD"


@pytest.mark.parametrize("kind", ["native", "aria_listbox"])
def test_replaced_controls_do_not_inherit_an_old_choice_proof(
    server: Server, browser: Any, kind: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    selector, fields = WIDGETS[kind]
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(server.base + "/w/" + kind)
        box = page.locator(selector).first.bounding_box()
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        backend = PlaywrightBackend(page)
        outcome = backend.choose_option(
            OptionChoice(
                label="Cardiology", value="CARD", commit_field="department", **fields
            ),
            "Cardiology",
            point=point,
        )
        assert outcome["label"] == "cardiology"
        page.locator(selector).evaluate("el => el.replaceWith(el.cloneNode(true))")
        from openadapt_flow.ir import (
            ActionKind,
            Anchor,
            StepResult,
            StructuralLocator,
            Workflow,
        )
        from openadapt_flow.runtime import Replayer

        replay = Replayer.__new__(Replayer)
        replay.backend = backend
        step = Step(
            id="choice",
            intent="choose",
            action=ActionKind.SELECT_OPTION,
            text="Cardiology",
            anchor=Anchor(
                template="unused-confirmation.png",
                structural=StructuralLocator(selector=selector),
                region=(
                    int(box["x"]),
                    int(box["y"]),
                    int(box["width"]),
                    int(box["height"]),
                ),
                click_point=point,
            ),
            choice=OptionChoice(
                label="Cardiology", value="CARD", commit_field="department", **fields
            ),
        )
        result = StepResult(
            step_id="choice", intent="choose", ok=False, delivery_attempted=True
        )
        error = replay._verify_selected_option(
            step,
            "Cardiology",
            b"",
            Path("."),
            Workflow(name="test", steps=[step]),
            result,
        )
        assert "choice_unverified" in error
        assert result.input_verified is False
        assert result.delivery_uncertainty is not None
        assert result.delivery_uncertainty.operation == (
            "choice_select" if kind == "native" else "choice_click"
        )
        assert result.delivery_uncertainty.cause_type == "choice_unverified"
        assert not backend.confirm_choice()
    finally:
        page.close()


@pytest.mark.parametrize("actual", ["alert", "prompt"])
def test_same_message_wrong_dialog_kind_is_dismissed(
    server: Server, browser: Any, actual: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import DialogResponse
    from openadapt_flow.runtime import Replayer

    page = browser.new_page()
    try:
        page.goto(server.base + "/w/native")
        backend = PlaywrightBackend(page)
        Replayer._arm_dialog(
            backend.arm_dialog,
            DialogResponse(
                response="accept", message="Synthetic confirmation", kind="confirm"
            ),
        )
        page.evaluate("kind => window[kind]('Synthetic confirmation')", actual)
        seen = backend.take_dialogs()
        assert len(seen) == 1
        assert seen[0]["type"] == actual
        assert seen[0]["matched"] is False
        assert seen[0]["response"] == "dismiss"
    finally:
        page.close()


def test_legacy_dialog_serialization_keeps_its_original_shape() -> None:
    from openadapt_flow.ir import DialogResponse

    legacy = DialogResponse(response="accept", message="Continue?")
    assert "kind" not in legacy.model_dump(mode="json")


@pytest.mark.parametrize("fault", ["frame_attached", "new_document"])
def test_dialog_authority_does_not_survive_a_document_scope_change(
    server: Server, browser: Any, fault: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page()
    try:
        page.goto(server.base + "/w/native")
        backend = PlaywrightBackend(page)
        backend.arm_dialog("accept", "Synthetic confirmation", kind="confirm")
        if fault == "frame_attached":
            page.evaluate("""() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<p>Another document</p>'; document.body.appendChild(frame);
            }""")
            page.frame_locator("iframe").locator("p").wait_for()
        else:
            page.goto(server.base + "/w/native?new-document=1")
        assert page.evaluate("confirm('Synthetic confirmation')") is False
        seen = backend.take_dialogs()
        assert len(seen) == 1
        assert seen[0]["matched"] is False
        assert seen[0]["response"] == "dismiss"
    finally:
        page.close()


@pytest.mark.parametrize(
    "fault", ["unchanged_document", "frame_attached", "new_document"]
)
def test_dialog_wait_keeps_the_original_producer_arm(
    server: Server, browser: Any, fault: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import DialogResponse
    from openadapt_flow.runtime import Replayer

    page = browser.new_page()
    try:
        page.goto(server.base + "/w/native")
        backend = PlaywrightBackend(page)
        dialog = DialogResponse(
            response="accept", message="Synthetic confirmation", kind="confirm"
        )
        Replayer._arm_dialog(backend.arm_dialog, dialog)
        if fault == "frame_attached":
            page.evaluate("""() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<p>Another document</p>'; document.body.appendChild(frame);
            }""")
            page.frame_locator("iframe").locator("p").wait_for()
        elif fault == "new_document":
            page.goto(server.base + "/w/native?new-document=1")
        page.evaluate("""() => {
            setTimeout(() => {
                window.answer = confirm('Synthetic confirmation');
            }, 250);
        }""")
        replay = Replayer.__new__(Replayer)
        replay.backend = backend
        seen = replay._wait_for_dialog(backend.take_dialogs, dialog)
        expected = fault == "unchanged_document"
        assert page.evaluate("window.answer") is expected
        assert len(seen) == 1
        assert seen[0]["matched"] is expected
        assert seen[0]["response"] == ("accept" if expected else "dismiss")
    finally:
        page.close()


def test_an_ambiguous_dialog_scope_refuses_the_step_before_input(
    server: Server, browser: Any
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import ActionKind, DialogResponse, Step
    from openadapt_flow.runtime import Replayer

    page = browser.new_page()
    try:
        page.goto(server.base + "/w/native")
        page.evaluate("""() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<p>Another document</p>'; document.body.appendChild(frame);
        }""")
        page.frame_locator("iframe").locator("p").wait_for()
        page.evaluate(
            "window.inputEdges = 0; addEventListener('keydown', () => inputEdges++)"
        )
        replay = Replayer.__new__(Replayer)
        replay.backend = PlaywrightBackend(page)
        error = replay._act(
            Step(
                id="s",
                intent="confirm",
                action=ActionKind.KEY,
                key="Enter",
                dialog=DialogResponse(
                    response="accept", message="Continue?", kind="confirm"
                ),
            ),
            None,
            {},
        )
        assert "dialog_scope_unverified" in error
        assert page.evaluate("inputEdges") == 0
    finally:
        page.close()


@pytest.mark.parametrize("trigger", ["focus", "blur"])
def test_native_dom_selection_does_not_invoke_unrelated_focus_callbacks(
    server: Server, browser: Any, trigger: str
) -> None:
    def prepare(page: Any) -> None:
        page.evaluate(
            """trigger => {
          const select = document.querySelector('#department');
          const prior = document.querySelector('#patient_name');
          window.callbackCount = 0; window.nativeChanges = [];
          const mutate = () => {
            callbackCount++;
            select.querySelector('[value=CARD]').value = 'WRONG';
          };
          (trigger === 'focus' ? select : prior).addEventListener(trigger, mutate);
          select.addEventListener('change', () => nativeChanges.push(select.value));
          if (trigger === 'blur') prior.focus();
        }""",
            trigger,
        )

    observations: list[dict[str, Any]] = []
    outcome, value, confirmed = _choose(
        server,
        browser,
        "native",
        "Cardiology",
        prepare=prepare,
        check=lambda page: observations.append(
            page.evaluate("""() => ({
          callbacks:callbackCount, changes:nativeChanges
        })""")
        ),
    )
    assert isinstance(outcome, dict), outcome
    assert value == "CARD"
    assert confirmed
    assert observations == [{"callbacks": 0, "changes": ["CARD"]}]


@pytest.mark.parametrize(
    "fault", ["value", "label", "disabled", "duplicate", "replaced"]
)
def test_native_input_callback_drift_stops_before_change_with_uncertain_delivery(
    server: Server, browser: Any, fault: str
) -> None:
    from openadapt_flow.backend import ActionDeliveryUncertain
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(server.base + "/w/native")
        page.evaluate(
            """fault => {
          const select = document.querySelector('#department');
          window.nativeInputs = []; window.nativeChanges = [];
          select.addEventListener('input', () => {
            nativeInputs.push(select.value);
            const option = select.selectedOptions[0];
            if (fault === 'value') option.value = 'WRONG';
            else if (fault === 'label') option.label = 'Wrong department';
            else if (fault === 'disabled') option.disabled = true;
            else if (fault === 'duplicate') select.appendChild(option.cloneNode(true));
            else select.replaceWith(select.cloneNode(true));
          });
          select.addEventListener('change', () => nativeChanges.push(select.value));
        }""",
            fault,
        )
        box = page.locator("#department").bounding_box()
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        backend = PlaywrightBackend(page)
        with pytest.raises(ActionDeliveryUncertain) as error:
            backend.choose_option(
                OptionChoice(
                    kind="native_select",
                    label="Cardiology",
                    value="CARD",
                    readback="select",
                ),
                "Cardiology",
                point=point,
            )
        assert error.value.operation == "choice_select"
        assert error.value.cause_type == "NativeChoiceChangedAfterInput"
        assert page.evaluate("nativeInputs") == ["CARD"]
        assert page.evaluate("nativeChanges") == []
        assert not backend.confirm_choice()
    finally:
        page.close()


@pytest.mark.parametrize(
    ("kind", "fault", "delay"),
    [
        ("native", "value", 0),
        ("native", "label", 0),
        ("native", "replaced", 0),
        ("native", "value", 50),
        ("aria_listbox", "value", 0),
        ("div_dropdown", "value", 0),
        ("async_combobox", "value", 0),
        ("select2", "value", 0),
    ],
)
def test_post_delivery_drift_is_uncertain_across_native_and_listed_choices(
    server: Server, browser: Any, kind: str, fault: str, delay: int
) -> None:
    def prepare(page: Any) -> None:
        page.evaluate(
            """([kind, fault, delay]) => {
          const field = document.querySelector('[name=department]');
          window.commitEdges = 0;
          const mutate = () => {
            if (fault === 'replaced') field.replaceWith(field.cloneNode(true));
            else if (fault === 'label') field.selectedOptions[0].label = 'Wrong label';
            else if (kind === 'native') field.selectedOptions[0].value = 'WRONG';
            else field.value = 'WRONG';
          };
          if (kind === 'native') field.addEventListener('change', () => {
            commitEdges++;
            if (delay) setTimeout(mutate, delay); else mutate();
          });
          else document.addEventListener('mousedown', event => {
            if (!event.target.closest('[role=option],.dd-item')) return;
            commitEdges++; setTimeout(mutate, delay);
          }, true);
        }""",
            [kind, fault, delay],
        )

    edges: list[int] = []
    outcome, _value, confirmed = _choose(
        server,
        browser,
        kind,
        "Cardiology",
        prepare=prepare,
        check=lambda page: edges.append(page.evaluate("commitEdges")),
    )
    assert isinstance(outcome, ActionDeliveryUncertain), outcome
    assert outcome.operation == (
        "choice_select" if kind == "native" else "choice_click"
    )
    assert outcome.cause_type == "choice_unverified"
    assert edges[0] >= 1
    assert not confirmed


@pytest.mark.parametrize("kind", ["native", "aria_listbox"])
def test_recovered_readback_must_restart_its_stability_window(
    server: Server, browser: Any, kind: str
) -> None:
    def prepare(page: Any) -> None:
        page.evaluate(
            """kind => {
          const field = document.querySelector('[name=department]');
          const drift = () => {
            window.commitAt = performance.now();
            const target = kind === 'native' ? field.selectedOptions[0] : field;
            setTimeout(() => target.value = 'WRONG', 60);
            setTimeout(() => target.value = 'CARD', 180);
          };
          if (kind === 'native') field.addEventListener('change', drift);
          else document.addEventListener('mousedown', event => {
            if (event.target.closest('[role=option]')) drift();
          }, true);
        }""",
            kind,
        )

    elapsed: list[float] = []
    outcome, value, confirmed = _choose(
        server,
        browser,
        kind,
        "Cardiology",
        prepare=prepare,
        check=lambda page: elapsed.append(page.evaluate("performance.now()-commitAt")),
    )
    assert isinstance(outcome, dict), outcome
    assert value == "CARD"
    assert confirmed
    # _choose includes its existing300ms final confirm delay; the choice
    # itself must wait beyond the recovered state's full stable interval.
    assert elapsed[0] >= 800


@pytest.mark.parametrize("prior", [None, False, True])
def test_pre_mutation_choice_refusal_preserves_earlier_delivery_scope(prior) -> None:
    from openadapt_flow.ir import StepResult
    from openadapt_flow.runtime import Replayer

    replay = Replayer.__new__(Replayer)
    replay._active_delivery_acknowledgers = ()
    result = StepResult(
        step_id="choice", intent="choose", ok=False, delivery_attempted=prior
    )

    def refuse():
        raise ChoiceRefused("choice_control_missing")

    with pytest.raises(ChoiceRefused):
        replay._deliver_backend_call(result, refuse)
    assert result.delivery_attempted is (prior if prior is not None else False)
    assert result.delivery_uncertainty is None
