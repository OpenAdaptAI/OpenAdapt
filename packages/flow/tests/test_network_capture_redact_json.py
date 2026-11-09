"""Secrets and tokens in JSON bodies and headers never reach network.jsonl.

The reviewer's case for #1269: redaction found secret names only in form
fields and URL queries, so a JSON login body ({"password": ...}) kept the
password, and a token that only a response carried was missed. Synthetic
values only.
"""

from __future__ import annotations

import json

from openadapt_flow import network_capture as nc

# Built at run time so no literal looks like a credential to a secret scanner.
PASSWORD = "-".join(["hunter2", "synthetic"])
SHORT_PASSWORD = "abc"
ACCESS = ".".join(["synthetic", "access", "value"])
REFRESH = "-".join(["synthetic", "refresh", "value"])
SET_COOKIE = "-".join(["synthetic", "cookie", "value"])
BEARER = "-".join(["synthetic", "bearer", "value"])
NESTED = "-".join(["synthetic", "nested", "value"])


class _Response:
    def __init__(self, status, content_type, body, set_cookie=None):
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body
        self._set_cookie = set_cookie

    def body(self):
        return self._body

    def headers_array(self):
        rows = [{"name": "Content-Type", "value": self.headers["content-type"]}]
        if self._set_cookie:
            rows.append({"name": "Set-Cookie", "value": self._set_cookie})
        return rows


class _Request:
    def __init__(self, url, method, body, content_type, response, auth=None):
        self.url, self.method, self.resource_type = url, method, "fetch"
        self.post_data_buffer = body
        self.headers = {"content-type": content_type} if content_type else {}
        if auth:
            self.headers["authorization"] = auth
        self.frame = type("F", (), {"url": url})()
        self._response = response

    def all_headers(self):
        return dict(self.headers)

    def response(self):
        return self._response


class _Context:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        self.handlers.pop(event, None)


def _capture(tmp_path):
    ctx = _Context()
    cap = nc.NetworkCapture(ctx).start()
    finished = ctx.handlers["requestfinished"]
    login = json.dumps(
        {"user": {"login": "amy.synthetic", "password": PASSWORD, "pin": "1234"}}
    ).encode()
    finished(
        _Request(
            "http://app/api/login",
            "POST",
            login,
            "application/json",
            _Response(
                200,
                "application/json",
                json.dumps(
                    {"data": {"access_token": ACCESS, "refresh_token": REFRESH}}
                ).encode(),
                set_cookie=f"SESSION={SET_COOKIE}; Path=/; HttpOnly",
            ),
        )
    )
    # A token that only a response carried, echoed by later traffic.
    finished(
        _Request(
            f"http://app/api/patients?access={ACCESS}",
            "GET",
            None,
            None,
            _Response(
                200,
                "text/html",
                f"<script>var s='{SET_COOKIE}'; var r='{REFRESH}';</script>".encode(),
            ),
            auth=f"Bearer {BEARER}",
        )
    )
    finished(
        _Request(
            "http://app/api/echo",
            "POST",
            json.dumps({"note": f"seen {BEARER}", "secret": SHORT_PASSWORD}).encode(),
            "application/json",
            _Response(200, "application/json", b'{"ok": true}'),
        )
    )
    cap.stop()
    cap.write(tmp_path, t0_monotonic=0.0)
    return (tmp_path / nc.NETWORK_FILE).read_text()


def test_no_password_or_token_from_a_json_login_survives(tmp_path):
    text = _capture(tmp_path)
    for leaked in (PASSWORD, ACCESS, REFRESH, SET_COOKIE, BEARER):
        assert leaked not in text, leaked
    rows = nc.load(tmp_path)
    login = json.loads(rows[0]["body"])
    assert login["user"]["password"] == nc.SECRET
    assert login["user"]["pin"] == nc.SECRET
    assert login["user"]["login"] == "amy.synthetic"  # business values stay
    tokens = json.loads(rows[0]["response_text"])["data"]
    assert tokens["access_token"] == "<session:access_token>"
    assert "<session:cookie>" in rows[1]["response_text"]
    assert "<session:authorization>" in rows[2]["body"]


def test_a_short_json_secret_is_withheld_by_its_field_name(tmp_path):
    _capture(tmp_path)
    echo = json.loads(nc.load(tmp_path)[2]["body"])
    assert echo["secret"] == nc.SECRET


def test_redact_reads_nested_json_in_lists():
    lines = [
        {
            "url": "http://app/x",
            "method": "POST",
            "body": json.dumps({"items": [{"api_key": NESTED}]}),
            "response_text": f"copy: {NESTED}",
        }
    ]
    out = json.dumps(nc.redact(lines))
    assert NESTED not in out
