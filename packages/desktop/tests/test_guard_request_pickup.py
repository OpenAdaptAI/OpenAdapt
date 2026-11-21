"""Desktop shows a guarded agent's save approval when its state is in the runs folder.

``openadapt_flow.guard.GuardedSave`` says that when its ``state_dir`` is the
runs folder the console watches, or a folder directly inside it, the phone
portal shows its approval requests. This test holds Desktop to that with its
own code: the ``--runs`` folder Desktop starts the Flow console with, and the
portal's relay of ``/api/portal/save-approvals``. The relay is answered by
Flow's own console functions over that folder, so nothing is canned.

Synthetic MockMed fault server on localhost; no browser, no model calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("openadapt_flow")

from openadapt_desktop.portal.flow_client import FlowResponse  # noqa: E402
from openadapt_desktop.portal.ingress import resolve_ingress  # noqa: E402
from openadapt_desktop.portal.pairing import DevicePairingStore  # noqa: E402
from openadapt_desktop.portal.server import PortalApp  # noqa: E402
from openadapt_desktop.portal.service import PortalError, PortalService  # noqa: E402

ORIGIN = "https://openadapt.clinic.example"
NOTE = "Synthetic referral note for the pickup test"
#: The open record's text, as an agent's harness reads it.
SCREEN = "Chart: patient p1 (synthetic)"


class _Config:
    portal_ingress_mode = "customer_ingress"
    portal_public_origin = ORIGIN
    portal_bind_host = ""
    portal_ingress_acknowledged = True
    portal_port = 8443
    portal_console_port = 7863

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _Spawned(Exception):
    pass


def _console_runs_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The ``--runs`` folder Desktop gives the Flow console it starts."""

    import openadapt_desktop.approver_key as approver_key

    monkeypatch.setattr(approver_key, "portal_approver_environment", lambda: {})
    seen: list[list[str]] = []

    def popen(command: list[str], **_kw: Any) -> Any:
        seen.append(list(command))
        raise _Spawned()

    service = PortalService(_Config(tmp_path / "desktop-data"), popen=popen)
    with pytest.raises((_Spawned, PortalError)):
        service._spawn_console(["openadapt-flow"], tmp_path / "flow.yaml")
    [command] = seen
    return Path(command[command.index("--runs") + 1])


class _FlowConsole:
    """Answers the portal's relay with Flow's console functions over ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def request(self, route: str, **kwargs: Any) -> FlowResponse:
        from openadapt_flow.console.save_approvals import (
            list_save_approvals,
            save_approval_detail,
        )

        if route == "save_approvals":
            rows = [r.model_dump(mode="json") for r in list_save_approvals(self.root)]
            return FlowResponse(200, {"save_approvals": rows}, None, "application/json")
        if route == "save_approval_detail":
            detail = save_approval_detail(self.root, kwargs["request_id"])
            return FlowResponse(200, detail, None, "application/json")
        raise AssertionError(route)


def _paired(app: PortalApp, pairings: DevicePairingStore) -> dict[str, str]:
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "Phone")
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    return {
        "authorization": f"Bearer {claim['session_token']}",
        "origin": ORIGIN,
        "content-type": "application/json",
        "x-openadapt-portal-csrf": claim["csrf_token"],
    }


def _verify(base_url: str) -> Any:
    from openadapt_flow.mockmed.fault_server import MOCKMED_RECORDS_READ_CONTRACT
    from openadapt_flow.verify_run import parse_verify_config

    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {"base_url": base_url, **MOCKMED_RECORDS_READ_CONTRACT},
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "patient_id": {"param": "patient_id"},
                        "type": "Referral",
                        "note": {"param": "note"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        }
    )


def _png() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xf0\xf0\xf0"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def mockmed():
    from openadapt_flow.mockmed.fault_server import serve

    url, _db, stop = serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


@pytest.mark.parametrize("state", ["folder_inside", "runs_folder"])
def test_desktop_shows_a_guard_request_from_a_state_dir_in_its_runs_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mockmed: str, state: str
) -> None:
    from openadapt_flow.guard import GuardedSave, GuardTask

    runs = _console_runs_folder(tmp_path, monkeypatch)
    state_dir = runs / "guarded-agent" if state == "folder_inside" else runs
    guarded = GuardedSave(_verify(mockmed), state_dir, approval="each_item")
    task = GuardTask(
        task_id="pickup-task-0001",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
    )
    guard = guarded.begin(task, screen_text=SCREEN, agent_id="cua-pickup")
    waiting = guard.commit(screenshot_png=_png())
    assert waiting.status == "waiting_for_approval"

    pairings = DevicePairingStore(runner_id="runner_test")
    app = PortalApp(resolve_ingress(_Config(tmp_path)), pairings, _FlowConsole(runs))
    headers = _paired(app, pairings)
    status, _, body = app.handle("GET", "/api/portal/save-approvals", headers, b"")
    assert status == 200
    [row] = json.loads(body)["save_approvals"]
    assert row["request_id"] == waiting.approval_request_id
    status, _, body = app.handle(
        "GET", f"/api/portal/save-approvals/{row['request_id']}", headers, b""
    )
    assert status == 200
    presentation = json.loads(body)["presentation"]
    # The card carries the field the save writes, from the guard's request.
    assert presentation["fields"] == [{"label": "note", "before": None, "after": NOTE}]


def test_a_state_dir_two_folders_down_is_not_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mockmed: str
) -> None:
    """The console looks two levels deep: a guard folder under
    ``runs/<task>/guard`` (``openadapt-flow guard --out runs/<task>``) is
    deeper than that, and the phone doesn't see it."""

    from openadapt_flow.guard import GuardedSave, GuardTask

    runs = _console_runs_folder(tmp_path, monkeypatch)
    guarded = GuardedSave(_verify(mockmed), runs / "task-0001" / "guard")
    task = GuardTask(
        task_id="pickup-task-0002",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
    )
    guarded.begin(task, screen_text=SCREEN).commit(screenshot_png=_png())
    assert _FlowConsole(runs).request("save_approvals").json == {"save_approvals": []}
