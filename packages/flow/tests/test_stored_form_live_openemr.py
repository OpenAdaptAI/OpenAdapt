"""#1811 on a real OpenEMR: the record check expects the form's stored forms.

OpenEMR's New Patient form transforms some fields when they change: a field
with the "U" edit option is saved in capitals, a field with "C" has each word
capitalized and extra spaces dropped (``capitalizeMe``). The OpenEMR
contracts declare these as ``stored: upper`` and ``stored: capitalize_words``,
and the check stays exact.

Two checks against a running OpenEMR, read through its own database:

- every stored form an OpenEMR contract declares is the edit option the app's
  layout gives that field, and every field the layout transforms declares one;
- a patient saved through the real form, typed in mixed case with extra
  spaces, is confirmed by the declared contract, while a contract without
  the stored forms, or with a different value, is not.

Needs a running OpenEMR and Chromium (headless). Made-up patients only; the
save adds one patient with a fresh email. From ``packages/flow``::

    OPENEMR_LIVE_UI=1 OPENEMR_UI_URL=http://127.0.0.1:9301 \\
    OPENEMR_UI_USER=... OPENEMR_UI_PASSWORD=... \\
    OPENEMR_DB_HOST=127.0.0.1 OPENEMR_DB_PORT=3306 \\
    OPENEMR_DB_USER=... OPENEMR_DB_PASSWORD=... \\
    PYTHONPATH=../types:. python -m pytest tests/test_stored_form_live_openemr.py

The database is read with the ``mysql`` client (``OPENEMR_DB_NAME`` defaults
to ``openemr``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("OPENEMR_LIVE_UI") == "1"
        and os.environ.get("OPENEMR_UI_URL")
        and os.environ.get("OPENEMR_DB_USER")
        and os.environ.get("OPENEMR_DB_PASSWORD")
        and shutil.which("mysql")
    ),
    reason=(
        "live OpenEMR: set OPENEMR_LIVE_UI=1, OPENEMR_UI_URL, OPENEMR_UI_USER, "
        "OPENEMR_UI_PASSWORD and OPENEMR_DB_* with an OpenEMR running and the "
        "mysql client installed"
    ),
)

FLOW = Path(__file__).resolve().parents[1]
#: The OpenEMR contracts that take what was typed. The incremental-value
#: contracts are frozen by hash (``incremental_value/run.py``); its harness
#: passes the stored forms as the parameters instead (``arms.py``).
CONTRACTS = [
    FLOW / "benchmark" / "lifecycle_proof" / "verify.yaml",
    FLOW / "benchmark" / "openemr_local" / "verify.yaml",
]
#: The New Patient form's edit options that change what is saved, and the
#: stored form that declares each.
EDIT_OPTION_FORMS = {"U": "upper", "C": "capitalize_words"}


def _sql(query: str) -> list[list[str]]:
    env = dict(os.environ, MYSQL_PWD=os.environ["OPENEMR_DB_PASSWORD"])
    out = subprocess.run(
        [
            "mysql",
            "-h",
            os.environ.get("OPENEMR_DB_HOST", "127.0.0.1"),
            "-P",
            os.environ.get("OPENEMR_DB_PORT", "3306"),
            "-u",
            os.environ["OPENEMR_DB_USER"],
            "--batch",
            "--raw",
            "--skip-column-names",
            os.environ.get("OPENEMR_DB_NAME", "openemr"),
            "-e",
            query,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def _layout_forms() -> dict[str, Optional[str]]:
    """The stored form OpenEMR's demographics layout gives each field."""

    import json

    forms: dict[str, Optional[str]] = {}
    for field_id, options in _sql(
        "SELECT field_id, edit_options FROM layout_options "
        "WHERE form_id = 'DEM' AND data_type = 2"
    ):
        try:
            flags = json.loads(options) if options and options != "NULL" else []
        except json.JSONDecodeError:
            flags = list(options)
        # The form applies "C" before "U" (options.inc.php).
        form = next((EDIT_OPTION_FORMS[f] for f in ("C", "U") if f in flags), None)
        forms[field_id] = form
    return forms


def _contract_fields(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())["record"]["fields"]


def test_every_contract_declares_the_layouts_stored_forms() -> None:
    layout = _layout_forms()
    assert layout.get("street") == "upper", layout  # the app still does this
    for path in CONTRACTS:
        for field, value in _contract_fields(path).items():
            if field not in layout:
                continue  # not a text field the form transforms
            declared = value.get("stored") if isinstance(value, dict) else None
            assert declared == layout[field], (path.name, field, declared)


def _save_patient(values: dict[str, str]) -> dict[str, str]:
    """Save ``values`` through the New Patient form; return the stored row."""

    from playwright.sync_api import sync_playwright

    from scripts.openemr_local_demo import (
        FORM_FRAME_NAME,
        FORM_PATH,
        _confirm_create_locator,
        _form_frame,
    )

    base = os.environ["OPENEMR_UI_URL"].rstrip("/")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(base + "/interface/login/login.php?site=default")
            page.locator("#authUser").fill(os.environ["OPENEMR_UI_USER"])
            page.locator("#clearPass").fill(os.environ["OPENEMR_UI_PASSWORD"])
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
            try:
                registration.wait_for(state="visible", timeout=10_000)
                registration.get_by_role(
                    "button", name="Ask again later", exact=True
                ).click()
                registration.wait_for(state="hidden", timeout=30_000)
            except Exception:  # noqa: BLE001 - not every profile shows it
                pass

            def fill(name: str, value: str) -> None:
                field = form.locator(f"#form_{name}")
                field.fill(value)
                field.press("Tab")  # the form transforms on change, as typing does

            for name in ("fname", "lname"):
                fill(name, values[name])
            fill("DOB", "1985-02-03")
            form.locator("#form_sex").select_option(label="Female")
            form.get_by_text("Contact", exact=True).click()
            for name in ("street", "city", "email"):
                fill(name, values[name])
            form.locator("#create").click()
            _confirm_create_locator(page).click()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                rows = _sql(
                    "SELECT fname, lname, street, city, email FROM patient_data "
                    f"WHERE email = '{values['email']}'"
                )
                if rows:
                    keys = ("fname", "lname", "street", "city", "email")
                    return dict(zip(keys, rows[0]))
                time.sleep(0.3)
            raise AssertionError("the save wrote no patient row")
        finally:
            browser.close()


def test_a_form_save_is_confirmed_only_in_its_stored_form() -> None:
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects._common import judge_records
    from openadapt_flow.runtime.effects.effect import EffectState

    typed = {
        "fname": "ada  grace",
        "lname": "mcQuill",
        "street": "41 Larch Way",
        "city": "Exampleton",
        "email": f"stored-form-{uuid.uuid4().hex[:10]}@example.invalid",
    }
    row = _save_patient(typed)
    # What OpenEMR stored: the form's own transforms, measured.
    assert row["street"] == "41 LARCH WAY"
    assert row["city"] == "EXAMPLETON"
    assert row["fname"] == "Ada Grace"
    assert row["lname"] == "McQuill"

    fields = _contract_fields(CONTRACTS[0])
    before = EffectState(substrate="t", reachable=True, records=[])

    def verdict(field: str, value: Any, params: dict[str, str]) -> str:
        effect = Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"email": {"param": "email"}},
            field=field,
            value=value,
        ).resolve(params)
        return judge_records(effect, before, [row], substrate="t").verdict.value

    for field in ("fname", "lname", "street", "city"):
        declared = fields[field]
        assert verdict(field, declared, typed) == "confirmed", field
        # Without the stored form the exact check refutes a correct save.
        assert verdict(field, {"param": field}, typed) != "confirmed", field
        # A declared stored form still refutes a different value.
        other = dict(typed, **{field: typed[field] + "x"})
        assert verdict(field, declared, other) != "confirmed", field
