"""Hooks for the OpenEMR OpenEnv environment (environments/openenv/openemr).

Setup only, as the benchmark declares it: restore the verified baseline,
sign in and open the new-patient form through OpenEMR's tab shell, and read
the target rows back with the fixture's direct SQL readback. The agent's
browser never reaches that read.

Run the environment from a repository checkout with ``PYTHONPATH`` set to
``packages/flow``, after the fixture is up (README.md in this directory).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from benchmark.openemr_local.qualify_hooks import fixture


def reset(**_: Any) -> None:
    fixture().reset()


def prepare(page: Any, app_url: str) -> None:
    from scripts.openemr_local_demo import _preauthenticate_browser

    _preauthenticate_browser(SimpleNamespace(page=page), fixture())


def records() -> list[dict[str, str]]:
    return fixture().db_records()
