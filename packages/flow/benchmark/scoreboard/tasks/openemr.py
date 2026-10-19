"""OpenEMR referral: register a referred patient on the New Patient form.

Uses the pinned OpenEMR fixture (``benchmark/openemr_local``) through the copy
the throughput session runs as its own podman project (``OPENEMR_UI_PORT`` /
``OPENEMR_API_PORT``, default 9911/9910, state in ``OPENEMR_FIXTURE_STATE``).
Sign-in and opening the blank form are unmeasured setup, from
``scripts/openemr_local_demo.py``, as in the October timing study.

The read-back is SQL on ``patient_data`` (the fixture's own ``_db``), with the
study's rule: exactly one new chart, and all 12 referral fields match.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

FLOW = Path(__file__).resolve().parents[3]
if str(FLOW) not in sys.path:
    sys.path.insert(0, str(FLOW))

NAME = "openemr_referral"
APP = "a clinic's electronic medical record (OpenEMR)"
TASK_ID = "openemr.register_referral"
PARAMS = (
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state_label",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
INSTRUCTION = (
    "You are already signed in to a local, synthetic-only OpenEMR test site and "
    "are looking at a blank patient-registration form. Create and save exactly "
    "one patient with these values:\n"
    "- Title: {title}\n- First name: {fname}\n- Last name: {lname}\n"
    "- Date of birth: {DOB}\n- Sex: {sex}\n- Street: {street}\n- City: {city}\n"
    "- State: {state_label}\n- Postal code: {postal_code}\n"
    "- Home phone: {phone_home}\n- Email: {email}\n- Country: {country_code}\n"
    "Use only the visible interface. Stop after the patient is saved. If OpenEMR "
    "says a matching patient may already exist, do not create a duplicate; stop "
    "and say so."
)
SQL_FIELDS = (
    "id",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
STATE = Path(
    os.environ.get(
        "OPENEMR_FIXTURE_STATE",
        str(Path.home() / "oa" / "scratch" / "throughput" / "fixture" / "state"),
    )
)
UI_PORT = os.environ.get("OPENEMR_UI_PORT", "9911")
API_PORT = os.environ.get("OPENEMR_API_PORT", "9910")


def _demo() -> Any:
    mod = sys.modules.get("openemr_local_demo")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "openemr_local_demo", FLOW / "scripts" / "openemr_local_demo.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["openemr_local_demo"] = mod
        spec.loader.exec_module(mod)
    return mod


_FIXTURE: Any = None


def fixture() -> Any:
    global _FIXTURE
    if _FIXTURE is not None:
        return _FIXTURE
    from benchmark.openemr_local import fixture as fx

    fx.LOCK_PATH = STATE.parent / "environment.lock.json"

    class PortFixture(fx.OpenEMRFixture):
        """Same pinned images, compose file and baseline; its own ports."""

        @property
        def ui_base_url(self) -> str:
            return f"http://127.0.0.1:{UI_PORT}"

        @property
        def api_base_url(self) -> str:
            return f"https://127.0.0.1:{API_PORT}"

        def _validate_runtime_env(self) -> None:
            v = self._runtime_values()
            assert v.get("OPENEMR_HTTP_PORT") == UI_PORT
            assert v.get("OPENEMR_HTTPS_PORT") == API_PORT
            self.runtime_env.chmod(0o600)

    _FIXTURE = PortFixture(STATE)
    return _FIXTURE


class _PageOnly:
    def __init__(self, page: Any) -> None:
        self.page = page


def setup(page: Any) -> None:
    """Sign in and open the blank New Patient form (unmeasured)."""

    _demo()._preauthenticate_browser(_PageOnly(page), fixture())


def entry_url() -> str:
    return f"{fixture().ui_base_url}/interface/main/tabs/main.php"


def patients() -> list[dict[str, str]]:
    out = (
        fixture()
        ._db(
            "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id"
        )
        .decode()
    )
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
    ]


def item_values(i: int) -> dict[str, str]:
    """A made-up referral; 555-01xx phones and example.invalid emails."""

    first = ["Adela", "Barnaby", "Clarice", "Desmond", "Edwina", "Fitzroy"]
    sex = "Female" if i % 2 == 0 else "Male"
    fname = first[i % len(first)]
    return {
        "title": "Ms." if sex == "Female" else "Mr.",
        "fname": fname,
        "lname": f"Scoreboard{chr(65 + i % 26)}{i:03d}",
        "DOB": f"19{50 + i % 45:02d}-{1 + i % 12:02d}-{1 + i % 27:02d}",
        "sex": sex,
        "street": f"{10 + i} SYNTHETIC WAY",
        "city": "EXAMPLETON",
        "state_label": "Massachusetts",
        "postal_code": "02139",
        "phone_home": f"555-01{i % 100:02d}",
        "email": f"{fname.lower()}.scoreboard.{i}@example.invalid",
        "country_code": "USA",
    }


def instruction(values: dict[str, str]) -> str:
    return INSTRUCTION.format(**values)


def check(values: dict[str, str], before_ids: set[str]) -> str:
    """``correct`` | ``missing`` | ``duplicate`` | ``wrong`` (12 of 12 fields)."""

    new = [p for p in patients() if p["id"] not in before_ids]
    if not new:
        return "missing"
    if len(new) > 1:
        return "duplicate"
    p = new[0]
    want = {**values, "state": "MA"}
    fields = (
        "title",
        "fname",
        "lname",
        "DOB",
        "sex",
        "street",
        "city",
        "state",
        "postal_code",
        "phone_home",
        "email",
        "country_code",
    )
    ok = all(
        str(p.get(f, "")) == str(want.get(f if f != "DOB" else "DOB", ""))
        for f in fields
    )
    return "correct" if ok else "wrong"


def oracle_verifier(values: dict[str, str]) -> Any:
    from openadapt_flow.benchmark.openemr_local import (
        OpenEMRPatientOracle,
        SyntheticPatientSpec,
    )

    spec = SyntheticPatientSpec(
        title=values["title"],
        fname=values["fname"],
        lname=values["lname"],
        dob=values["DOB"],
        sex=values["sex"],
        street=values["street"],
        city=values["city"],
        state="MA",
        state_label=values["state_label"],
        postal_code=values["postal_code"],
        phone_home=values["phone_home"],
        email=values["email"],
        country_code=values["country_code"],
    )
    fx = fixture()
    return OpenEMRPatientOracle(
        fx.api_base_url, fx.token_session("oracle"), spec
    ).verifier
