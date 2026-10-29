"""Live check of #1347 on a real OpenEMR: the duplicate dialog's identity band.

OpenEMR's duplicate check opens "Confirm Create New Patient" in a modal over
the New Patient form. The full-width OCR band around that button also read a
label of the form behind the modal, which one depended on how far the form was
scrolled, so a correct save was refused every 9 to 17 items. This test opens
the real dialog with the form scrolled to several positions and checks that the
band read on the dialog alone (the frame blanked outside
``PlaywrightBackend.dialog_region_at``) is the same every time and holds no
form label.

Bring up the throughput fixture first (see
``benchmark/openemr_throughput/README.md``), then::

    OPENEMR_LIVE_STATE=~/oa/scratch/throughput/fixture/state \\
    THROUGHPUT_UI_PORT=9911 THROUGHPUT_API_PORT=9910 \\
    OPENADAPT_FLOW_BROWSER=bundled \\
    PYTHONPATH=../types:. python -m pytest tests/test_dialog_identity_live_openemr.py -v

Skipped unless ``OPENEMR_LIVE_STATE`` is set. It saves nothing: each dialog is
left unconfirmed and its browser closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENEMR_LIVE_STATE"),
    reason="live OpenEMR: set OPENEMR_LIVE_STATE to the fixture state directory",
)

# Form scroll positions behind the dialog. 0 leaves "County:" beside the
# dialog's button row in the pinned image; the others move other labels there.
SCROLLS = (0, 250, 600, 900)


def _open_dialog(demo, fixture, scroll_y: int):
    backend, close = demo.OpenEMRPlaywrightBackend.launch(
        fixture.ui_base_url, headless=True
    )
    demo._preauthenticate_browser(backend, fixture)
    page = backend.page
    form = demo._form_frame(page)
    form.locator("#form_fname").fill("Wilhelmina")
    form.locator("#form_lname").fill("Okonkwo-Hale")
    form.locator("#form_DOB").fill("1961-04-09")
    form.locator("#form_DOB").press("Escape")  # close the date picker
    form.locator("#form_sex").select_option(label="Female")
    form.locator("#create").scroll_into_view_if_needed()
    form.evaluate(f"() => window.scrollTo(0, {int(scroll_y)})")
    form.locator("#create").evaluate("el => el.click()")
    confirm = demo._confirm_create_locator(page)
    return backend, close, demo._center(confirm)


def test_duplicate_dialog_band_ignores_the_form_behind() -> None:
    from benchmark.openemr_throughput import harness
    from openadapt_flow.runtime import identity as identity_mod
    from openadapt_flow.vision.ocr import ocr

    demo = harness._demo_module()
    fixture = harness.make_fixture(Path(os.environ["OPENEMR_LIVE_STATE"]).expanduser())
    full_bands: list[str] = []
    dialog_bands: list[str] = []
    for scroll_y in SCROLLS:
        backend, close, point = _open_dialog(demo, fixture, scroll_y)
        try:
            status, box = backend.dialog_region_at(*point)
            assert status == "dialog", (scroll_y, status)
            assert box is not None
            frame = backend.screenshot()
            viewport = backend.viewport
            band = identity_mod.band_region(point, 64, viewport)
            # The button's own crop is excluded, as the compiler excludes it.
            exclude = (point[0] - 80, point[1] - 32, 160, 64)

            def read(png: bytes, scope=None) -> str:
                return (
                    identity_mod.context_from_lines(
                        ocr(png),
                        exclude_region=exclude,
                        band=band,
                        point=point,
                        scope=scope,
                    )
                    or ""
                )

            full_bands.append(read(frame))
            masked = identity_mod.mask_outside(frame, box)
            assert masked is not None
            dialog_bands.append(read(masked, box))
        finally:
            close()
    print("full-width bands:", full_bands)
    print("dialog bands:", dialog_bands)
    assert len(set(dialog_bands)) == 1, dialog_bands
    for label in ("County", "Emergency", "Contact", "Phone", "Address"):
        assert label not in dialog_bands[0], dialog_bands[0]
