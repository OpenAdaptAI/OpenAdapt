"""The environment's hooks (``environments/openenv/dolibarr_erp/env.yaml``).

``reset`` restores the baseline, ``prepare`` signs the agent's user in through
Dolibarr's own sign-in page (unmeasured setup; the agent never sees the
password), ``read`` returns the whole record for the grader, and ``noise``
is the calibrated profile of columns Dolibarr rewrites on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmark.dolibarr_erp import fixture

NOISE_FILE = Path(__file__).resolve().parent / "noise.json"


def reset(**_: Any) -> None:
    fixture.reset()


def prepare(page: Any, app_url: str) -> None:
    page.goto(app_url + "/index.php", wait_until="load")
    if page.locator("#username").count():
        page.fill("#username", fixture.ADMIN_LOGIN)
        page.fill("#password", fixture.ADMIN_PASS)
        page.locator("input[type=submit]").first.click()
        page.wait_for_load_state("load")
    if page.locator("#username").count():
        raise RuntimeError("the harness could not sign in to Dolibarr")


def read() -> dict[str, Any]:
    return fixture.read_record()


def noise() -> dict[str, Any]:
    return json.loads(NOISE_FILE.read_text()) if NOISE_FILE.exists() else {}
