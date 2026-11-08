"""API step save shapes on real applications (opt-in, needs the lab fixtures).

Each workflow is recorded fresh on its fixture, compiled, and then run with
the API step on: once normally, and once with the save refused by the
fixture's proxy (503, nothing forwarded). The app's own database is the
judge (``benchmark/api_generalization``, PROTOCOL.md).

- Every run must be correct: the expected record and nothing else after the
  normal run, and no claimed success after the refused save.
- When the API step is proposed and admitted, the normal run saves through
  it (``actuation: api``) with one save request, unless a value the server
  fills in on every page load (a load time, a per-record code) differs, in
  which case it says so and the GUI steps save.
- When it isn't proposed, the reason must be one of the known ones.

Run with the fixtures up (``benchmark/api_generalization/run.sh up --app X``):

    APIGEN_FIXTURES=redmine,trac pytest tests/test_api_step_shapes_fixtures.py
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

APPS = [a for a in os.environ.get("APIGEN_FIXTURES", "").split(",") if a]

pytestmark = pytest.mark.skipif(
    not APPS, reason="set APIGEN_FIXTURES to the running lab fixtures"
)

#: Why each workflow may stay on the GUI, from the fields its form sends.
KNOWN_REFUSALS = (
    "form's default changed",  # a per-load value: _init_time, a customer code
    "not a recorded value, a form default or a session value",
)


@pytest.mark.parametrize("name", APPS)
def test_api_step_on_a_real_app(name: str, tmp_path: Path) -> None:
    from benchmark.api_generalization import harness

    os.environ.setdefault("APIGEN_STATE", str(tmp_path / "state"))
    app = harness.load_app(name)
    rec, bundle = tmp_path / "rec", tmp_path / "bundle"
    harness.cmd_record(argparse.Namespace(out=str(rec)), app)
    harness.cmd_compile(argparse.Namespace(recording=str(rec), out=str(bundle)), app)
    compiled = json.loads((bundle / "apigen-compile.json").read_text())

    env = harness._env()
    try:
        cases = {c.kind: c for c in app.cases()}
        normal = harness.run_one(
            env, app, app.unique(cases["normal"], "fxn"), "api", bundle, tmp_path / "n"
        )
        rejected = harness.run_one(
            env,
            app,
            app.unique(cases["fault_reject"], "fxr"),
            "api",
            bundle,
            tmp_path / "r",
        )
    finally:
        env["browser"].close()
        env["pw"].stop()

    assert normal["correct"], normal
    assert not normal["incorrect_write"] and not normal["false_success"], normal
    assert not rejected["false_success"] and not rejected["incorrect_write"], rejected
    if not compiled["proposed"]:
        assert any(r in " ".join(compiled["reasons"]) for r in KNOWN_REFUSALS), compiled
        return
    assert compiled.get("admitted"), compiled
    if normal["save_actuation"] == "api":
        assert normal["save_posts"] == 1, normal
    else:
        # Proposed from one page load, but a value the server fills in on
        # every load differs at run time: the GUI steps saved instead.
        assert any(r in str(normal["api_fallback"]) for r in KNOWN_REFUSALS), normal
