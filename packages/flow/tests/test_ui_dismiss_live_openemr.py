"""#1377 on a real OpenEMR: a dismiss that writes nothing gets no
``record_written``; the New Patient save keeps it.

The recording is made through the real recorder against the local OpenEMR
fixture, with the fixture's direct SQL read of ``patient_data`` as the record
store snapshot (what the compile agent's ``record_reader`` is on MockMed).
Step 0 dismisses OpenEMR's product-registration dialog ("Ask again later"),
which the pinned image shows to every fresh browser profile and which writes
no patient row. Its label has no write word, so the test marks it
consequential with a risk override, the position MockMed's "Continue to
tasks" is in ("continue" is a write word): the store evidence, not the label,
must decide whether it gets ``record_written``. The last step confirms the
duplicate check and saves the patient; it must keep a ``record_written``.

Needs the running local fixture (``benchmark/openemr_local``; this test never
starts or stops containers) and Chromium. It resets the fixture database.
From ``packages/flow``::

    OPENEMR_LIVE_UI=1 \\
    OPENEMR_UI_URL=http://127.0.0.1:9301 \\
    OPENEMR_API_URL=https://127.0.0.1:9300 \\
    OPENEMR_FIXTURE_STATE=benchmark/openemr_local/state \\
    PYTHONPATH=../types:. python -m pytest \\
        tests/test_ui_dismiss_live_openemr.py -v -p no:cacheprovider

``OPENEMR_FIXTURE_STATE`` and ``OPENEMR_FIXTURE_LOCK`` are optional (see
``benchmark/openemr_local/qualify_hooks.py``).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENEMR_LIVE_UI") == "1" and os.environ.get("OPENEMR_UI_URL")),
    reason=(
        "live OpenEMR UI: set OPENEMR_LIVE_UI=1 and OPENEMR_UI_URL (+ "
        "OPENEMR_API_URL, OPENEMR_FIXTURE_STATE) with the local fixture running"
    ),
)


def _record(tmp_path: Path) -> tuple[Path, Any]:
    from benchmark.openemr_local.qualify_hooks import fixture
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec
    from openadapt_flow.recorder import Recorder
    from scripts.openemr_local_demo import (
        FORM_FRAME_NAME,
        FORM_PATH,
        SETTLE,
        OpenEMRPlaywrightBackend,
        _center,
        _confirm_create_locator,
        _form_frame,
        _wait_for_patient_write,
    )

    fx = fixture()
    fx.reset()
    values = fx._runtime_values()
    backend, close = OpenEMRPlaywrightBackend.launch(fx.ui_base_url)
    try:
        page = backend.page
        # Unrecorded setup: sign in and open the new-patient form in its tab.
        page.goto(fx.ui_base_url)
        page.locator("#authUser").fill(values["OPENEMR_ACTOR_USER"])
        page.locator("#clearPass").fill(values["OPENEMR_ACTOR_PASSWORD"])
        page.locator("#login-button").click()
        page.wait_for_url("**/interface/main/tabs/main.php**", timeout=60_000)
        page.wait_for_function(
            "() => typeof window.navigateTab === 'function' "
            "&& typeof window.activateTabByName === 'function'",
            timeout=60_000,
        )
        page.evaluate(
            "({path, name}) => { window.navigateTab(path, name); "
            "window.activateTabByName(name, true); }",
            {"path": FORM_PATH, "name": FORM_FRAME_NAME},
        )
        form = _form_frame(page)
        form.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        registration = page.locator(".product-registration-modal")
        registration.wait_for(state="visible", timeout=30_000)
        telemetry = registration.locator("#allowTelemetry")
        if telemetry.count() and telemetry.is_checked():
            telemetry.uncheck()
        assert not (telemetry.count() and telemetry.is_checked())

        recorder = Recorder(
            backend,
            tmp_path / "rec",
            app_url=page.url,
            system_of_record_reader=fx.db_records,
            **SETTLE,
        )
        recorder.click(
            *_center(
                registration.get_by_role("button", name="Ask again later", exact=True)
            )
        )
        registration.wait_for(state="hidden", timeout=30_000)
        spec = SyntheticPatientSpec()

        def text_field(name: str, value: str, *, param: str) -> None:
            recorder.click(*_center(form.locator(f"#form_{name}")))
            recorder.type_text(value, param=param)

        def select_by_label(name: str, label: str, *, param: str) -> None:
            recorder.click(*_center(form.locator(f"#form_{name}")))
            recorder.type_text(label, param=param)
            recorder.press("Enter")

        select_by_label("title", "Ms.", param="title")
        text_field("fname", spec.fname, param="fname")
        text_field("lname", spec.lname, param="lname")
        text_field("DOB", spec.dob, param="DOB")
        select_by_label("sex", "Female", param="sex")
        recorder.scroll(0, 300)
        recorder.click(*_center(form.get_by_text("Contact", exact=True)))
        recorder.scroll(0, 600)
        text_field("street", spec.street, param="street")
        text_field("city", spec.city, param="city")
        select_by_label("state", spec.state_label, param="state_label")
        text_field("postal_code", spec.postal_code, param="postal_code")
        text_field("phone_home", spec.phone_home, param="phone_home")
        text_field("email", spec.email, param="email")
        select_by_label("country_code", "USA", param="country_code")
        recorder.click(*_center(form.locator("#create")))
        recorder.click(*_center(_confirm_create_locator(page)))
        recording = recorder.finish()
        _wait_for_patient_write(fx)
    finally:
        close()
    return recording, fx


def test_openemr_dismiss_gets_no_record_written_and_the_save_keeps_it(tmp_path):
    from openadapt_flow.compiler.compile import compile_recording
    from openadapt_flow.compiler.effect_mining import store_unchanged_through_next
    from openadapt_flow.runtime.effects import EffectKind

    started = time.monotonic()
    recording, fx = _record(tmp_path)
    assert len(fx.db_records()) == 1, "the save wrote no patient row"

    import json

    events = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # The real store did not change across the dismiss.
    assert store_unchanged_through_next(events, 0) is True

    wf = compile_recording(
        recording,
        tmp_path / "bundle",
        name="openemr-dismiss",
        mine_effects=True,
        risk_overrides={"step_000": "irreversible"},
    )
    dismiss, save = wf.steps[0], wf.steps[-1]
    assert dismiss.id == "step_000" and dismiss.risk == "irreversible"
    assert [e.kind for e in dismiss.effects] == [], dismiss.effects
    # Fail closed on the save: it keeps a record_written (derived when the row
    # was in the snapshot after the click, a flagged placeholder otherwise).
    assert EffectKind.RECORD_WRITTEN in [e.kind for e in save.effects]
    assert time.monotonic() - started < 900
