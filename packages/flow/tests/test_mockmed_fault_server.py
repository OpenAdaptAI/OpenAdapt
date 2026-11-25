"""Regression tests for MockMed fault_server patient validation (#2524)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from openadapt_flow.mockmed.fault_server import KNOWN_PATIENTS, serve


def _post_encounter(
    base_url: str, payload: dict, *, fault: str = ""
) -> tuple[int, dict]:
    suffix = f"?fault={fault}" if fault else ""
    req = urllib.request.Request(
        f"{base_url}api/encounter{suffix}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_db(base_url: str) -> dict:
    with urllib.request.urlopen(f"{base_url}api/db", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.mark.parametrize("bad_patient_id", ["NONEXISTENT-999", "", "p999"])
def test_unknown_patient_encounter_rejected_and_not_persisted(
    bad_patient_id: str,
) -> None:
    url, _db, stop = serve(port=0)
    try:
        status, body = _post_encounter(
            url,
            {"patient_id": bad_patient_id, "type": "visit", "note": "orphan"},
        )
        assert 400 <= status < 500
        assert body["ok"] is False
        assert "unknown patient" in body["error"]

        snapshot = _get_db(url)
        assert snapshot["records"] == []
        assert snapshot["rejected_writes"] == 1
    finally:
        stop()


def test_known_patient_encounter_accepted() -> None:
    url, _db, stop = serve(port=0)
    try:
        patient_id = sorted(KNOWN_PATIENTS)[0]
        status, body = _post_encounter(
            url,
            {"patient_id": patient_id, "type": "Triage", "note": "registered patient"},
        )
        assert status == 200
        assert body["ok"] is True
        snapshot = _get_db(url)
        assert len(snapshot["records"]) == 1
        assert snapshot["records"][0]["patient_id"] == patient_id
    finally:
        stop()
