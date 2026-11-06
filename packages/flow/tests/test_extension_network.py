"""Optional Chrome extension: network-save capture with fail-closed redaction.

Part of OpenAdapt#1872. Covers the engine redactor
(``openadapt_flow.extension_network``), its wiring into extension evidence
ingest and the native host, and the extension's ``webRequest`` capture in
``chrome_extension/background.js`` + ``network_redact.js`` (run in Node with a
stubbed ``chrome`` API). These checks fail on a tree without the redactor or
without the wiring.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.extension_evidence import (
    EVIDENCE_FILE,
    SCHEMA,
    ExtensionEvidenceBuffer,
    load_extension_evidence,
    validate_native_message,
)
from openadapt_flow.extension_network import (
    PLACEHOLDER,
    network_saves,
    redact_network_save,
)

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome_extension"
HARNESS = ROOT / "tests" / "extension_support" / "network_harness.js"
HOST = EXT / "native_host" / "openadapt_extension_host.py"

BEARER = "Bearer live-bearer-0f9e8d7c6b5a"
COOKIE = "sid=live-cookie-a1b2c3d4e5f6; theme=dark"
SET_COOKIE = "session=live-set-cookie-778899aabb; HttpOnly"
API_KEY = "live-api-key-5566778899"
CSRF = "live-csrf-1122334455"
QUERY_TOKEN = "live-query-token-abcdef"
PASSWORD = "hunter2-live-password"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsaXZlIn0.c2lnbmF0dXJlLWxpdmU"
GITHUB = "ghp_liveGithubToken0123456789abcd"
USERINFO = "liveuser:livepass99"
FRAGMENT = "access_token=live-fragment-token-42"
ALL_SECRETS = [
    "live-bearer-0f9e8d7c6b5a",
    "live-cookie-a1b2c3d4e5f6",
    "live-set-cookie-778899aabb",
    API_KEY,
    CSRF,
    QUERY_TOKEN,
    PASSWORD,
    JWT,
    GITHUB,
    "livepass99",
    "live-fragment-token-42",
]


def _save(**overrides):
    line = {
        "kind": "network_save",
        "method": "POST",
        "url": f"https://{USERINFO}@claims.example/api/claims/{JWT}"
        f"?access_token={QUERY_TOKEN}&page=2#{FRAGMENT}",
        "resource_type": "xmlhttprequest",
        "tab_id": 4,
        "frame_id": 0,
        "status": 201,
        "request_headers": [
            {"name": "Authorization", "value": BEARER},
            {"name": "Cookie", "value": COOKIE},
            {"name": "X-API-Key", "value": API_KEY},
            {"name": "X-CSRF-Token", "value": CSRF},
            {"name": "Content-Type", "value": "application/json"},
            {"name": "X-Custom-Trace", "value": "trace-123456"},
        ],
        "response_headers": [
            {"name": "Set-Cookie", "value": SET_COOKIE},
            {"name": "Content-Type", "value": "application/json"},
        ],
        "request_body_status": "json",
        "request_body": {
            "claim": {"id": 7, "amount": 120.5, "note": "follow up"},
            "auth": {"password": PASSWORD, "remember": True},
            "user": {"password": PASSWORD, "name": "Dana"},
            "integration": {"label": GITHUB},
            "items": [{"code": "991122", "qty": 2}],
        },
        "redacted": True,
        "t": 1000,
    }
    line.update(overrides)
    return line


def _no_secrets(blob: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in blob, secret


def _headers(record, which="request_headers"):
    return {h["name"]: h["value"] for h in record[which]}


# --- engine redactor ---------------------------------------------------------


def test_credential_headers_are_withheld():
    record = redact_network_save(_save())
    assert record is not None
    req = _headers(record)
    for name in ("authorization", "cookie", "x-api-key", "x-csrf-token"):
        assert req[name] == PLACEHOLDER
    assert req["content-type"] == "application/json"
    resp = _headers(record, "response_headers")
    assert resp["set-cookie"] == PLACEHOLDER
    _no_secrets(json.dumps(record))


def test_unknown_header_values_fail_closed():
    record = redact_network_save(_save())
    assert _headers(record)["x-custom-trace"] == PLACEHOLDER


def test_widening_the_allowlist_never_unlocks_credentials():
    record = redact_network_save(
        _save(),
        extra_allowed_headers=[
            "authorization",
            "cookie",
            "x-api-key",
            "x-custom-trace",
        ],
    )
    req = _headers(record)
    assert req["authorization"] == PLACEHOLDER
    assert req["cookie"] == PLACEHOLDER
    assert req["x-api-key"] == PLACEHOLDER
    assert req["x-custom-trace"] == "trace-123456"


def test_allowed_header_with_credential_value_is_withheld():
    line = _save(request_headers=[{"name": "Accept", "value": BEARER}])
    assert _headers(redact_network_save(line))["accept"] == PLACEHOLDER


def test_url_userinfo_fragment_token_query_and_path_are_redacted():
    record = redact_network_save(_save())
    assert record["url"] == (
        f"https://claims.example/api/claims/{PLACEHOLDER}"
        f"?access_token={PLACEHOLDER}&page=2"
    )
    assert record["url_origin"] == "https://claims.example"


def test_ipv6_origin_keeps_brackets():
    record = redact_network_save(_save(url="http://[::1]:8080/save?x=1"))
    assert record["url"] == "http://[::1]:8080/save?x=1"


def test_body_secrets_are_withheld_and_ordinary_fields_kept():
    body = redact_network_save(_save())["request_body"]
    assert body["claim"] == {"id": 7, "amount": 120.5, "note": "follow up"}
    assert body["auth"] == PLACEHOLDER  # whole subtree under a secret key
    assert body["user"] == {"password": PLACEHOLDER, "name": "Dana"}
    assert body["integration"] == {"label": PLACEHOLDER}  # token-shaped value
    assert body["items"] == [{"code": PLACEHOLDER, "qty": 2}]


def test_form_body_is_redacted():
    line = _save(
        request_body_status="form",
        request_body={"username": ["dana"], "password": [PASSWORD]},
    )
    body = redact_network_save(line)["request_body"]
    assert body == {"username": ["dana"], "password": PLACEHOLDER}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        ("raw", f"password={PASSWORD}", "omitted:unsupported_type"),
        ("json", f"password={PASSWORD}", "omitted:unsupported_type"),
        ("json", {"blob": "x" * (70 * 1024)}, "omitted:too_large"),
        ("omitted:file_upload", None, "omitted:file_upload"),
    ],
)
def test_unsupported_or_oversized_bodies_are_dropped(status, body, expected):
    record = redact_network_save(_save(request_body_status=status, request_body=body))
    assert record["request_body_status"] == expected
    assert record["request_body"] is None
    _no_secrets(json.dumps(record))


def test_too_deep_body_is_dropped():
    deep: dict = {"v": "x"}
    for _ in range(40):
        deep = {"n": deep}
    record = redact_network_save(_save(request_body=deep))
    assert record["request_body_status"] == "omitted:too_complex"
    assert record["request_body"] is None


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", ""])
def test_non_save_methods_are_dropped(method):
    assert redact_network_save(_save(method=method)) is None


@pytest.mark.parametrize(
    "url", ["data:text/plain,hi", "chrome-extension://abc/x", "file:///etc/x", "nope"]
)
def test_non_http_urls_are_dropped(url):
    assert redact_network_save(_save(url=url)) is None


def test_unknown_top_level_keys_are_dropped():
    record = redact_network_save(_save(cookies=COOKIE, raw_body=PASSWORD))
    assert "cookies" not in record and "raw_body" not in record
    _no_secrets(json.dumps(record))


def test_redaction_is_idempotent():
    once = redact_network_save(_save())
    assert redact_network_save(once) == once


def test_record_with_a_withheld_value_elsewhere_is_dropped():
    # The bearer value also appears in an ordinary field: fail closed.
    line = _save(
        request_body={"note": "live-bearer-0f9e8d7c6b5a"},
        request_body_status="json",
    )
    assert redact_network_save(line) is None


# --- ingest wiring -----------------------------------------------------------


def test_native_message_accepts_and_redacts_network_save():
    evidence = validate_native_message(
        {"type": "EXTENSION_EVIDENCE", "schema": SCHEMA, "evidence": _save()}
    )
    assert evidence["kind"] == "network_save"
    _no_secrets(json.dumps(evidence))


def test_native_message_refuses_unredactable_network_save():
    with pytest.raises(ValueError, match="network_save"):
        validate_native_message(
            {"type": "EXTENSION_EVIDENCE", "evidence": _save(method="GET")}
        )


def test_buffer_round_trip_stores_no_secret(tmp_path):
    buf = ExtensionEvidenceBuffer()
    buf.append(_save())
    buf.append({"kind": "target", "event": "click", "path": "button#save"})
    path = buf.write(tmp_path)
    _no_secrets(path.read_text(encoding="utf-8"))
    lines = load_extension_evidence(tmp_path)
    saves = network_saves(lines)
    assert len(saves) == 1 and saves[0]["status"] == 201
    assert any(line.get("kind") == "target" for line in lines)


def test_buffer_refuses_unredactable_network_save():
    buf = ExtensionEvidenceBuffer()
    with pytest.raises(ValueError):
        buf.append(_save(url="javascript:alert(1)"))
    assert buf.count == 0


def test_load_re_redacts_a_hand_written_line(tmp_path):
    header = {"schema": SCHEMA, "events": 2, "source": "chrome_extension"}
    bad = _save()  # never went through the redactor
    dropped = _save(method="GET")
    (tmp_path / EVIDENCE_FILE).write_text(
        "\n".join(json.dumps(x) for x in (header, bad, dropped)) + "\n",
        encoding="utf-8",
    )
    lines = load_extension_evidence(tmp_path)
    assert len(lines) == 1
    _no_secrets(json.dumps(lines))


def _frame(message: dict) -> bytes:
    data = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def test_native_host_writes_redacted_network_save(tmp_path):
    payload = _frame(
        {"type": "EXTENSION_EVIDENCE", "schema": SCHEMA, "evidence": _save()}
    ) + _frame({"type": "EXTENSION_EVIDENCE", "evidence": _save(method="GET")})
    completed = subprocess.run(
        [sys.executable, str(HOST), "--recording", str(tmp_path)],
        input=payload,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    replies = []
    out = completed.stdout
    while out:
        (n,) = struct.unpack("<I", out[:4])
        replies.append(json.loads(out[4 : 4 + n]))
        out = out[4 + n :]
    assert replies[0] == {"ok": True, "count": 1}
    assert replies[1]["ok"] is False
    stored = (tmp_path / EVIDENCE_FILE).read_text(encoding="utf-8")
    assert "network_save" in stored
    _no_secrets(stored)
    _no_secrets(completed.stdout.decode("utf-8", "replace"))


# --- extension (Node) --------------------------------------------------------


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required to test the Chrome extension scripts")
        pytest.skip("Node.js is not installed")
    return node


def _run(steps: list[dict], *flags: str) -> list[dict]:
    completed = subprocess.run(
        [_node(), str(HARNESS), str(EXT), json.dumps(steps), *flags],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def _request(**overrides):
    request = {
        "method": "POST",
        "url": f"https://claims.example/api/claims?access_token={QUERY_TOKEN}&page=2",
        "body_text": json.dumps(
            {"claim": {"id": 7}, "password": PASSWORD, "integration": GITHUB}
        ),
        "requestHeaders": [
            {"name": "Authorization", "value": BEARER},
            {"name": "Cookie", "value": COOKIE},
            {"name": "X-API-Key", "value": API_KEY},
            {"name": "Content-Type", "value": "application/json"},
        ],
        "responseHeaders": [{"name": "Set-Cookie", "value": SET_COOKIE}],
        "statusCode": 201,
    }
    request.update(overrides)
    return request


def test_manifest_requests_webrequest_and_ships_redactor():
    data = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert "webRequest" in data["permissions"]
    # Observe only: no blocking or header-rewriting permissions.
    assert "webRequestBlocking" not in data["permissions"]
    assert "declarativeNetRequest" not in data["permissions"]
    assert (EXT / "network_redact.js").is_file()


def test_saves_are_captured_redacted_only_while_recording():
    results = _run(
        [
            {"request": _request()},  # idle: ignored
            {"mode": "record"},
            {"request": _request()},
            {"request": _request(method="GET")},  # not a save
            {"request": _request(tabId=-1)},  # extension / browser traffic
            {"state": True},
        ]
    )
    state = results[-1]
    assert state["redactorLoaded"] is True
    assert state["networkListening"] is True
    assert len(state["queued"]) == 1
    record = state["queued"][0]
    assert record["kind"] == "network_save" and record["status"] == 201
    assert record["url"].endswith(f"access_token={PLACEHOLDER}&page=2")
    headers = {h["name"]: h["value"] for h in record["request_headers"]}
    assert headers["authorization"] == PLACEHOLDER
    assert headers["cookie"] == PLACEHOLDER
    assert headers["x-api-key"] == PLACEHOLDER
    assert headers["content-type"] == "application/json"
    assert record["request_body"] == {
        "claim": {"id": 7},
        "password": PLACEHOLDER,
        "integration": PLACEHOLDER,
    }
    _no_secrets(json.dumps(state))


def test_listeners_never_ask_for_cookies_or_blocking():
    state = _run([{"state": True}])[0]
    for listeners in state["listeners"].values():
        for listener in listeners:
            assert "extraHeaders" not in listener["extra"]
            assert "blocking" not in listener["extra"]
    assert state["listeners"]["onBeforeRequest"][0]["extra"] == ["requestBody"]


def test_stop_recording_discards_pending_and_queue():
    results = _run(
        [{"mode": "record"}, {"request": _request()}, {"mode": "idle"}, {"state": True}]
    )
    assert results[-1]["queued"] == []
    assert results[-1]["pendingSaves"] == 0


def test_failed_request_is_not_recorded():
    state = _run(
        [
            {"mode": "record"},
            {"request": _request(error="net::ERR_FAILED")},
            {"state": True},
        ]
    )[-1]
    assert state["queued"] == [] and state["pendingSaves"] == 0


def test_without_the_redactor_nothing_is_captured():
    state = _run(
        [{"mode": "record"}, {"request": _request()}, {"state": True}], "--no-redactor"
    )[-1]
    assert state["redactorLoaded"] is False
    assert state["queued"] == []


def test_non_json_raw_body_is_dropped_in_the_extension():
    request = _request(
        body_text=f"password={PASSWORD}",
        requestHeaders=[{"name": "Content-Type", "value": "text/plain"}],
    )
    record = _run([{"mode": "record"}, {"request": request}, {"state": True}])[-1][
        "queued"
    ][0]
    assert record["request_body"] is None
    assert record["request_body_status"] == "omitted:unsupported_type"
    _no_secrets(json.dumps(record))


def test_extension_and_engine_redaction_agree():
    record = _run([{"mode": "record"}, {"request": _request()}, {"state": True}])[-1][
        "queued"
    ][0]
    assert redact_network_save(record) == record


def test_extension_drops_a_record_that_repeats_a_withheld_token():
    request = _request(body_text=json.dumps({"note": "live-bearer-0f9e8d7c6b5a"}))
    state = _run([{"mode": "record"}, {"request": request}, {"state": True}])[-1]
    assert state["queued"] == []
