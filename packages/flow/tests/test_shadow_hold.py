"""The shadow hold captures every write of a task and refuses it.

A fake page stands in for the CDP adapter; no browser. Synthetic values.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.agent_guard.commit_guard import BLOCK_STATUS, GUARD_HEADER
from openadapt_flow.agent_guard.requests import HeldRequest, NotASave
from openadapt_flow.agent_guard.shadow_hold import (
    PROPOSED_WRITE_SCHEMA,
    ShadowHoldGuard,
    parse_body_fields,
)
from openadapt_flow.guard import GuardTask

APP = "http://127.0.0.1:9999"


class FakePage:
    def __init__(self, screen: Optional[str] = "Jane Sample — MRN P1") -> None:
        self.screen = screen
        self.continued: list[str] = []
        self.blocked: list[tuple[str, int, dict[str, str], str]] = []
        self.notices: list[str] = []

    def continue_request(self, request_id: str) -> None:
        self.continued.append(request_id)

    def block_request(self, request_id: str, *, status, body, content_type, headers):
        self.blocked.append((request_id, status, headers, body))

    def screen_text(self) -> Optional[str]:
        return self.screen

    def screenshot(self) -> Optional[bytes]:
        return b"\x89PNG fake"

    def notify(self, message: str, *, level: str) -> None:
        self.notices.append(message)

    def clear_notice(self) -> None:
        pass


def _task() -> GuardTask:
    return GuardTask(
        task_id="shadow-task-0001",
        expected_record={"patient_id": "p1", "name": "Jane Sample"},
        intended_fields={"type": None, "note": None},
    )


def _guard(tmp_path: Path, page: FakePage, **kw: Any) -> ShadowHoldGuard:
    return ShadowHoldGuard(
        None, _task(), page, app_origin=APP + "/", run_dir=tmp_path, **kw
    )


def _req(
    rid: str,
    method: str,
    path: str,
    body: Optional[str] = None,
    ctype: str = "application/json",
    origin: str = APP,
) -> HeldRequest:
    return HeldRequest(
        request_id=rid,
        method=method,
        url=origin + path,
        resource_type="Fetch",
        post_data=body,
        content_type=ctype,
    )


def test_every_non_get_to_the_app_is_held_and_refused(tmp_path: Path) -> None:
    page = FakePage()
    guard = _guard(tmp_path, page)
    guard.on_request(_req("r0", "GET", "/api/records"))
    for i, method in enumerate(("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")):
        guard.on_request(_req(f"w{i}", method, "/api/encounter", '{"note": "x"}'))
    assert page.continued == ["r0"]
    assert [b[0] for b in page.blocked] == [f"w{i}" for i in range(6)]
    for _rid, status, headers, body in page.blocked:
        assert status == BLOCK_STATUS
        assert headers[GUARD_HEADER] == "blocked; code=shadow_hold"
        assert "stopped before save (shadow mode)" in body
    assert guard.outcome()["held"] == 6
    assert guard.landed_writes == 0
    assert guard.finished  # the first hold ends the task


def test_graphql_query_post_and_get_mutation_are_held(tmp_path: Path) -> None:
    page = FakePage()
    guard = _guard(tmp_path, page)
    guard.on_request(_req("q", "POST", "/graphql", '{"query": "query { a }"}'))
    guard.on_request(
        HeldRequest("m", "GET", APP + "/graphql?query=mutation%20%7B%20b%20%7D")
    )
    assert page.continued == []
    assert [b[0] for b in page.blocked] == ["q", "m"]


def test_not_saves_go_on_and_other_origins_are_refused(tmp_path: Path) -> None:
    page = FakePage()
    guard = _guard(tmp_path, page, not_saves=[NotASave("POST", "/login")])
    guard.on_request(
        _req("s", "POST", "/login", "user=a", "application/x-www-form-urlencoded")
    )
    guard.on_request(_req("o", "POST", "/beacon", "{}", origin="http://127.0.0.1:1"))
    assert page.continued == ["s"]
    assert [b[0] for b in page.blocked] == ["o"]
    assert guard.landed_writes == 0
    assert guard.other_origin_refused == 1
    assert guard.proposed is None


def test_proposed_write_file(tmp_path: Path) -> None:
    page = FakePage()
    guard = _guard(tmp_path, page, save_target=("POST", "/api/encounter"))
    guard.on_request(_req("lock", "POST", "/api/lock", '{"id": 1}'))
    assert guard.proposed is None  # a lock is held, but it isn't the save
    guard.on_request(
        _req(
            "s",
            "POST",
            "/api/encounter",
            json.dumps(
                {
                    "patient_id": "p1",
                    "type": "Triage",
                    "note": "n",
                    "csrf_token": "abcdefghijkl",
                }
            ),
        )
    )
    path = tmp_path / "proposed_write.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(path.read_text())
    assert doc["schema"] == PROPOSED_WRITE_SCHEMA
    assert doc["request_index"] == 1
    assert doc["method"] == "POST" and doc["url_path"] == "/api/encounter"
    assert doc["origin"] == APP
    assert doc["identity"] == {"ok": True, "code": "verified"}
    assert doc["refused"] is True
    assert doc["withheld_fields"] == 1
    assert {f["name"]: f["value"] for f in doc["fields"]} == {
        "patient_id": "p1",
        "type": "Triage",
        "note": "n",
    }
    assert "abcdefghijkl" not in path.read_text()
    assert (tmp_path / "held" / "1.png").is_file()
    assert (tmp_path / "held" / "0.json").is_file()


def test_wrong_record_is_recorded(tmp_path: Path) -> None:
    page = FakePage(screen="Alex Testcase — MRN P2")
    guard = _guard(tmp_path, page)
    guard.on_request(_req("s", "POST", "/api/encounter", '{"note": "n"}'))
    assert guard.proposed["identity"] == {"ok": False, "code": "identity_mismatch"}


def test_a_forwarded_write_is_counted(tmp_path: Path) -> None:
    # Defence in depth: the counting page sees a write go on whatever decided it.
    page = FakePage()
    guard = _guard(tmp_path, page)
    request = _req("x", "POST", "/api/encounter", "{}")
    guard._seen["x"] = request
    guard.page.continue_request("x")
    assert guard.landed_writes == 1
    assert guard.outcome()["landed"] == [
        {"method": "POST", "url_path": "/api/encounter"}
    ]


def test_double_save_and_before_screen(tmp_path: Path) -> None:
    page = FakePage(screen="Referral Tasks")
    guard = _guard(tmp_path, page)
    guard.observe_screen()
    assert guard.before_screen is None  # the record isn't open yet
    page.screen = "Jane Sample — MRN P1 — DOB 1980-01-01"
    guard.observe_screen()
    assert guard.before_screen == page.screen
    for rid in ("a", "b"):
        guard.on_request(_req(rid, "POST", "/api/encounter", '{"note": "n"}'))
    assert guard.outcome()["double_save"] is True


@pytest.mark.parametrize(
    ("ctype", "body", "expected"),
    [
        (
            "application/x-www-form-urlencoded",
            "a=1&b=&a=2&c=x",
            [("a", None, "uncompared"), ("b", "", "form"), ("c", "x", "form")],
        ),
        (
            "application/json",
            '{"a": {"b": 1}, "c": null, "d": true}',
            [("a.b", "1", "json"), ("c", None, "json"), ("d", "true", "json")],
        ),
        ("text/plain", "free text", [("<body>", None, "uncompared")]),
        ("application/json", "[1, 2]", [("<body>", None, "uncompared")]),
        ("application/json", "{not json", [("<body>", None, "uncompared")]),
    ],
)
def test_parse_body_fields(ctype: str, body: str, expected: list) -> None:
    fields, _ = parse_body_fields(
        method="POST", url=APP + "/x", content_type=ctype, body=body
    )
    assert [(f.name, f.value, f.parse) for f in fields] == expected


def test_a_write_with_no_body_seen_is_uncompared() -> None:
    fields, _ = parse_body_fields(
        method="POST", url=APP + "/x", content_type="", body=None
    )
    assert [(f.name, f.parse) for f in fields] == [("<body>", "uncompared")]


def test_multipart_and_lens() -> None:
    from openadapt_flow.tx.types import EndpointTemplate, EntityLensSpec

    boundary = "XyZ"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\nhello\r\n'
        f"--{boundary}--\r\n"
    )
    fields, _ = parse_body_fields(
        method="POST",
        url=APP + "/x",
        content_type=f"multipart/form-data; boundary={boundary}",
        body=body,
    )
    assert [(f.name, f.value, f.parse) for f in fields] == [("note", "hello", "form")]
    lens = EntityLensSpec(
        entity_name="encounter",
        base_url=APP,
        get_one=EndpointTemplate(method="GET", path_template="/api/encounter/{id}"),
        write_endpoint=EndpointTemplate(
            method="PATCH", path_template="/api/encounter/{id}"
        ),
    )
    fields, _ = parse_body_fields(
        method="PATCH",
        url=APP + "/api/encounter/7",
        content_type="application/json",
        body='{"note": "n"}',
        lens=lens,
    )
    assert fields[0].parse == "lens"
