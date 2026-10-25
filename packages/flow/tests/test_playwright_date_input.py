"""An ISO date typed into a native date input lands as that date.

Chromium splits ``<input type=date>`` into locale-ordered segments: a typed
``1984-03-07`` leaves it empty, because ``-`` doesn't move to the next
segment. The backend sets an ISO date with ``fill`` instead, which takes the
ISO value whatever the locale. Other text is typed as before.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

PAGE = (
    "data:text/html,<input id=d type=date><input id=t type=text>"
    "<script>window.changes=0;"
    "document.getElementById('d').addEventListener('change',()=>window.changes++)"
    "</script>"
)


@pytest.fixture
def backend():
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    try:
        backend, close = PlaywrightBackend.launch(PAGE)
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"chromium unavailable: {exc}")
    yield backend
    close()


def test_an_iso_date_lands_in_a_date_input(backend):
    backend.page.focus("#d")
    backend.type_text("1984-03-07")
    assert backend.page.input_value("#d") == "1984-03-07"
    assert backend.page.evaluate("window.changes") >= 1


def test_other_text_and_other_fields_are_typed(backend):
    backend.page.focus("#t")
    backend.type_text("1984-03-07")
    assert backend.page.input_value("#t") == "1984-03-07"
    backend.page.focus("#d")
    backend.type_text("not a date")
    assert backend.page.input_value("#d") == ""
