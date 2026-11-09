"""Day-1 partner kit: invoke Flow, keep the MockMed oracle honest.

Launcher unit tests mock ``_invoke_flow``. That is OpenAdapt#1122-class
coverage: it proves argv forwarding, not that Flow ran. These tests call
the installed engine. The weekly ``quickstart-lifecycle`` job still owns
the full Playwright ``VERIFIED`` path.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from openadapt.cli import _invoke_flow


def test_invoke_flow_runs_the_installed_tutorial_help() -> None:
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        try:
            code = _invoke_flow(["tutorial", "--help"])
        except SystemExit as exc:
            code = int(exc.code or 0)
    text = buffer.getvalue()
    assert code == 0, text
    assert "--break-it" in text


def test_mockmed_optimistic_fault_leaves_the_store_unchanged() -> None:
    """--break-it injects this fault. The independent GET /api/db must not move."""
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    try:
        before = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert before["records"] == []

        request = Request(
            url + "api/encounter?fault=optimistic",
            data=json.dumps(
                {"patient_id": "p1", "type": "Triage", "note": "synthetic"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(request, timeout=2)
        assert raised.value.code == 409

        after = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert after["records"] == []
        assert db.rejected_writes == 1
        control = Request(
            url + "api/encounter?fault=ok",
            data=json.dumps(
                {"patient_id": "p1", "type": "Triage", "note": "synthetic"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        landed = json.loads(urlopen(control, timeout=2).read().decode("utf-8"))
        assert landed.get("ok") is True
        stored = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert len(stored["records"]) == 1
    finally:
        stop()
