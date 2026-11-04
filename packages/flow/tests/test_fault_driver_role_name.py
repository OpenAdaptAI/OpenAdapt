"""The web fault driver finds a save control by role and name.

A React app seldom gives its save button an ``id``, so the recorder keeps the
button's role and accessible name instead of a CSS selector. The fault driver
takes that locator when it finds exactly one element, and declines when it
finds none or several. The qualification precondition accepts it too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator, Workflow
from openadapt_flow.qualify import WebFaultDriver
from openadapt_flow.qualify.campaign import apply_verify_config, check_ready
from tests.test_qualify_declarative import _config


def _workflow(structural: StructuralLocator) -> Workflow:
    save = Step(
        id="s3",
        intent="Create patient",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/s3.png",
            region=(10, 10, 80, 20),
            click_point=(20, 20),
            structural=structural,
            context_text="Create New Patient",
        ),
        identity_armed=True,
        risk="irreversible",
    )
    return Workflow(
        name="register",
        steps=[
            Step(id="s1", intent="email", action=ActionKind.TYPE, param="email"),
            Step(id="s2", intent="name", action=ActionKind.TYPE, param="lname"),
            save,
        ],
        params={"email": "a@example.test", "lname": "Doe"},
    )


BY_ROLE = StructuralLocator(role="button", name="Create")


def test_the_precondition_accepts_a_role_and_a_name():
    workflow = _workflow(BY_ROLE)
    config = _config()
    save, _ = apply_verify_config(workflow, config)
    assert check_ready(workflow, config, save, dict(workflow.params)) == []


def test_the_precondition_refuses_a_role_alone():
    workflow = _workflow(StructuralLocator(role="button"))
    config = _config()
    save, _ = apply_verify_config(workflow, config)
    problems = " | ".join(check_ready(workflow, config, save, dict(workflow.params)))
    assert "structural locator" in problems


@pytest.fixture
def page_with():
    pytest.importorskip("playwright")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    opened = []

    def open_page(buttons: int):
        html = (
            "data:text/html,<form><input name=a>" + "<button>Create</button>" * buttons
        )
        html += "<button>Cancel</button></form>"
        try:
            backend, close = PlaywrightBackend.launch(html)
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        opened.append(close)
        return backend

    yield open_page
    for close in opened:
        close()


@pytest.mark.parametrize("buttons, found", [(1, True), (0, False), (2, False)])
def test_the_driver_needs_exactly_one_match(page_with, buttons, found):
    backend = page_with(buttons)
    driver = WebFaultDriver(_workflow(BY_ROLE))
    target = driver._save_target(SimpleNamespace(step_id="s3", backend=backend))
    assert (target is not None) is found
    if found:
        assert target.inner_text() == "Create"
