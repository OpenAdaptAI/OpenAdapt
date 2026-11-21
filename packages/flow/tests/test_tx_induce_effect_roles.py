"""Lens induction takes route roles from effects, not method names (#2390).

The class: induction treated ``POST`` as create and ``PUT``/``PATCH`` as
update, and the write-semantics probe only ran on ``PUT``/``PATCH``. An app
that creates with ``PUT`` and updates with ``POST``, and whose update replaces
the whole record, got a lens with swapped roles and was never probed. That is
the replace-route finding from the blind E5 run, where a body with only one
field returned 200 and cleared seven others.

A second class surfaced on the same app: a field that merely looked like a
timestamp (``due_date``) was marked volatile by name. Volatile fields leave
the frame, so a replace write that cleared the date was not reported.

Evidence here comes from a real application:

- ``fixtures/tx/real_task_api_demo.har.json`` is traffic recorded through
  ``SessionWireTap`` from a local Vikunja 2.7.1 (synthetic records, token
  masked by the tap). Regenerate it with
  ``python tests/test_tx_induce_effect_roles.py <base_url> <out.har.json>``.
- The live tests run against a local Vikunja when
  ``OAFLOW_TX_LIVE_VIKUNJA_URL`` is set. They register a fresh synthetic
  user, so no credential is read from anywhere. They are skipped otherwise.

Engine code never names the app; only this test does.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest

from openadapt_flow.tx.induce import induce_lens_from_har, probe_put_semantics
from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

FIXTURE = Path(__file__).parent / "fixtures" / "tx" / "real_task_api_demo.har.json"
LIVE_URL = os.environ.get("OAFLOW_TX_LIVE_VIKUNJA_URL", "").strip().rstrip("/")

SEED_TASK = {
    "title": "Synthetic task A",
    "description": "seed description",
    "priority": 1,
    "percent_done": 0.25,
    "hex_color": "1973ff",
    "due_date": "2026-12-24T10:00:00Z",
}


def _call(
    base: str,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
) -> tuple[int, Any]:
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        conn.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers=headers,
        )
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw or b"null")
    except ValueError:
        return resp.status, None


def _synthetic_session(base: str) -> tuple[str, int]:
    """Register a fresh synthetic user on the local app; return token, project."""
    user = "lens" + secrets.token_hex(4)
    password = secrets.token_urlsafe(18)
    status, _ = _call(
        base,
        "POST",
        "/api/v1/register",
        {"username": user, "email": f"{user}@example.test", "password": password},
    )
    assert status == 200, f"register failed: {status}"
    status, reply = _call(
        base,
        "POST",
        "/api/v1/login",
        {"username": user, "password": password, "long_token": True},
    )
    assert status == 200 and reply.get("token")
    token = reply["token"]
    status, project = _call(
        base, "PUT", "/api/v1/projects", {"title": "Synthetic project"}, token
    )
    assert status in (200, 201)
    return token, int(project["id"])


def _record_demo(base: str) -> tuple[dict[str, Any], str, int]:
    """Demonstrate create, read, update and delete through the tap."""
    token, project_id = _synthetic_session(base)
    policy = WirePolicy(max_mutations=None, secrets=(token,))
    with SessionWireTap(base, policy) as tap:
        b = tap.base_url
        _call(b, "GET", f"/api/v1/projects/{project_id}", token=token)
        status, task = _call(
            b, "PUT", f"/api/v1/projects/{project_id}/tasks", SEED_TASK, token
        )
        assert status in (200, 201)
        tid = task["id"]
        _call(b, "GET", f"/api/v1/tasks/{tid}", token=token)
        _call(b, "POST", f"/api/v1/tasks/{tid}", {**task, "priority": 3}, token)
        _call(b, "GET", f"/api/v1/tasks/{tid}", token=token)
        status, scratch = _call(
            b,
            "PUT",
            f"/api/v1/projects/{project_id}/tasks",
            {"title": "Synthetic scratch task"},
            token,
        )
        _call(b, "DELETE", f"/api/v1/tasks/{scratch['id']}", token=token)
        har = tap.export_har()
    return har, token, project_id


# ---------------------------------------------------------------------------
# Recorded real traffic (runs in CI)
# ---------------------------------------------------------------------------


def _fixture_har() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_recorded_trace_gets_roles_from_effects() -> None:
    har = _fixture_har()
    lens = induce_lens_from_har(har)
    assert lens.entity_name == "task"
    assert lens.id_field == "id"
    assert (lens.get_one.method, lens.get_one.path_template) == (
        "GET",
        "/api/v1/tasks/{id}",
    )
    # The update route is a POST on the item path; the create is a PUT on a
    # different, parent-scoped collection.
    assert (lens.write_endpoint.method, lens.write_endpoint.path_template) == (
        "POST",
        "/api/v1/tasks/{id}",
    )
    assert lens.write_mode == "patch"  # not yet probed
    assert lens.create_endpoint is not None
    assert lens.create_endpoint.method == "PUT"
    assert lens.create_endpoint.path_template.startswith("/api/v1/projects/")
    assert lens.create_endpoint.path_template.endswith("/tasks")
    assert lens.delete_endpoint is not None
    assert lens.delete_endpoint.path_template == "/api/v1/tasks/{id}"


def test_recorded_trace_keeps_user_dates_in_the_frame() -> None:
    lens = induce_lens_from_har(_fixture_har())
    for user_date in ("due_date", "start_date", "end_date"):
        assert user_date not in lens.volatile_fields
    # The server bumps ``updated`` on every write; the trace shows it.
    assert "updated" in lens.volatile_fields


def test_recorded_trace_does_not_learn_from_the_parent_record() -> None:
    lens = induce_lens_from_har(_fixture_har())
    # Project-only fields never become task codecs or identity.
    for project_field in ("is_archived", "parent_project_id", "views"):
        assert project_field not in lens.field_codecs
        assert project_field not in lens.identity_fields


def test_fixture_holds_no_bearer_token() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    har = json.loads(raw)
    for entry in har["log"]["entries"]:
        for header in entry["request"]["headers"]:
            if header["name"].lower() == "authorization":
                assert "Bearer" not in header["value"]
                assert "ey" not in header["value"]


# ---------------------------------------------------------------------------
# Live application (lab only)
# ---------------------------------------------------------------------------

live = pytest.mark.skipif(not LIVE_URL, reason="set OAFLOW_TX_LIVE_VIKUNJA_URL")


@pytest.fixture(scope="module")
def live_demo() -> tuple[dict[str, Any], str, int]:
    return _record_demo(LIVE_URL)


def _probed(lens: Any, token: str) -> Any:
    return probe_put_semantics(
        lens,
        sample_record={k: v for k, v in SEED_TASK.items() if k != "due_date"},
        sample_update={"priority": 4},
        auth_headers={"Authorization": "Bearer " + token},
    )


def _engine_write(lens: Any, token: str, project_id: int) -> tuple[Any, dict, dict]:
    from openadapt_flow.tx.lens import TransactionalCommitEngine
    from openadapt_flow.tx.types import TxTask

    status, task = _call(
        LIVE_URL, "PUT", f"/api/v1/projects/{project_id}/tasks", SEED_TASK, token
    )
    assert status in (200, 201)
    tid = task["id"]
    _, before = _call(LIVE_URL, "GET", f"/api/v1/tasks/{tid}", token=token)
    engine = TransactionalCommitEngine(
        lens,
        tempfile.mkdtemp(prefix="lens-live-"),
        headers={"Authorization": "Bearer " + token},
        allow_unqualified_prototype=True,
    )
    result = engine.execute(
        TxTask(
            task_id=f"live-{tid}-{secrets.token_hex(3)}",
            operation="update",
            entity_id=str(tid),
            intended_delta={"priority": 5},
        )
    )
    # Truth is read by the test itself, not by the engine.
    _, after = _call(LIVE_URL, "GET", f"/api/v1/tasks/{tid}", token=token)
    return result, before, after


FRAME = ("title", "description", "percent_done", "hex_color", "due_date")


@live
def test_live_probe_finds_the_replace_route(live_demo) -> None:
    har, token, _ = live_demo
    lens = induce_lens_from_har(har)
    assert lens.write_endpoint.method == "POST"
    probed = _probed(lens, token)
    assert probed.write_mode == "replace"
    assert "due_date" not in probed.volatile_fields


@live
def test_live_probed_lens_changes_only_the_intended_field(live_demo) -> None:
    har, token, project_id = live_demo
    lens = _probed(induce_lens_from_har(har), token)
    result, before, after = _engine_write(lens, token, project_id)
    assert after["priority"] == 5
    for name in FRAME:
        assert after[name] == before[name], name
    # Without a row authority the best HTTP outcome is UNCERTAIN, never
    # CONFIRMED; it must not be REFUTED on a correct write.
    assert result.outcome == "UNCERTAIN"
    assert set(result.reasons) == {"unqualified_observation_only"}


@live
def test_live_unprobed_lens_clobber_is_reported(live_demo) -> None:
    """Negative control: the trace alone says patch, the route replaces."""
    har, token, project_id = live_demo
    lens = induce_lens_from_har(har)
    assert lens.write_mode == "patch"
    result, before, after = _engine_write(lens, token, project_id)
    clobbered = [n for n in FRAME if after[n] != before[n]]
    assert "due_date" in clobbered and "description" in clobbered
    assert result.outcome == "REFUTED"
    for name in clobbered:
        assert f"clobbered:{name}" in result.reasons


if __name__ == "__main__":  # pragma: no cover - fixture regeneration
    base_url, out_path = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    recorded, _, _ = _record_demo(base_url)
    out_path.write_text(json.dumps(recorded, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(recorded['log']['entries'])} entries)")
