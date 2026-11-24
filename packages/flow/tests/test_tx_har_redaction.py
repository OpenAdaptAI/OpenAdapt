"""Encoded privacy across detection, HAR export and encrypted persistence."""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import crypto
from openadapt_flow.secret_redaction import MASK, carries_secret, mask_secrets
from openadapt_flow.tx.wire_tap import (
    SessionWireTap,
    WirePolicy,
    _build_har_entry,
    _encode_har_body,
    dump_har,
    load_har,
)
from openadapt_flow.wire_check import carries_secret as wire_carries_secret

SECRETS = ("s3cr3t-TOKEN-value", "abcdefSECRET +/", "clé-🔒/+", 'q"u\\ote\n+/SECRET')


def _forms(secret: str) -> list[bytes]:
    raw = secret.encode("utf-8")
    utf16 = secret.encode("utf-16-be")
    unicode_escaped = "".join(
        f"\\u{int.from_bytes(utf16[i : i + 2], 'big'):04x}"
        for i in range(0, len(utf16), 2)
    )
    partial = secret.replace("-", r"\u002d").replace("/", r"\/")
    percent = "".join(f"%{value:02X}" for value in raw)
    return [
        raw,
        quote(secret, safe="").encode(),
        quote_plus(secret).encode(),
        secret.replace("-", "%2D").encode(),
        percent.encode(),
        percent.lower().encode(),
        quote(percent, safe="").encode(),
        partial.encode(),
        unicode_escaped.encode(),
        quote(unicode_escaped, safe="").encode(),
        "".join(f"&#x{ord(c):x};" for c in secret).encode(),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw).rstrip(b"="),
    ]


def _entry(secret: str, body: bytes) -> dict[str, Any]:
    return _build_har_entry(
        started_utc="2026-01-01T00:00:00Z",
        duration_ms=1,
        method="POST",
        url="http://fixture.test/records?token=" + body.decode("utf-8", "replace"),
        request_headers=[("Content-Type", "application/octet-stream")],
        request_body=body,
        response_status=200,
        response_headers=[("Content-Type", "application/octet-stream")],
        response_body=body,
        secrets=(secret,),
        tx_meta={"encoded": body.decode("utf-8", "replace")},
    )


@pytest.mark.parametrize("secret", SECRETS)
def test_every_supported_encoding_is_detected_and_absent_from_all_har_fields(secret):
    for encoded in _forms(secret):
        assert carries_secret(encoded, (secret,))
        text = encoded.decode("utf-8")
        assert wire_carries_secret(text, (secret,))
        assert not carries_secret(mask_secrets(text, (secret,)), (secret,))
        har = {"log": {"entries": [_entry(secret, encoded)]}}
        exported = dump_har(har, secrets=(secret,))
        assert not carries_secret(exported, (secret,)), repr(encoded)
        assert not carries_secret(json.dumps(load_har(exported)), (secret,))


@settings(max_examples=45, deadline=None)
@given(st.sampled_from(SECRETS), st.binary(max_size=8), st.binary(max_size=8))
def test_binary_secret_at_every_base64_alignment_is_withheld(secret, prefix, suffix):
    body = b"\xff" + prefix + secret.encode() + suffix + b"\xfe"
    for encoded in (
        base64.b64encode(body),
        base64.urlsafe_b64encode(body).rstrip(b"="),
    ):
        assert carries_secret(encoded, (secret,))
        assert not carries_secret(mask_secrets(encoded.decode(), (secret,)), (secret,))
    wrapped = b"\r\n".join(
        base64.b64encode(body)[i : i + 4]
        for i in range(0, len(base64.b64encode(body)), 4)
    )
    assert carries_secret(wrapped, (secret,))
    assert not carries_secret(mask_secrets(wrapped.decode(), (secret,)), (secret,))
    for raw in (body, gzip.compress(body)):
        headers = {"Content-Encoding": "gzip"} if raw != body else {}
        text, encoding = _encode_har_body(raw, headers, (secret,))
        assert encoding == "base64"
        assert base64.b64decode(text, validate=True).decode() == MASK


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "Proxy-Authorization",
        "cOoKiE",
        "SET-cookie",
        "X-Api-Key",
        "X-Auth-Token",
        "X-CSRF-Token",
        "X-Amz-Security-Token",
    ],
)
def test_undeclared_credentials_in_arbitrary_har_are_always_redacted(header):
    value = "undeclared-session-credential-913"
    entry = {
        "request": {"headers": [{"name": header, "value": value}]},
        "response": {
            "headers": {header: value},
            "cookies": [{"name": "sid", "value": value}],
        },
    }
    out = load_har(dump_har({"log": {"entries": [entry]}}))
    assert value not in json.dumps(out)
    assert out["log"]["entries"][0]["request"]["headers"][0]["value"] == MASK


def test_nonsecret_text_and_binary_remain_usable():
    text = "http://fixture.test/records?q=ordinary%20value&note=slash\\/escaped"
    assert not carries_secret(text, SECRETS)
    assert mask_secrets(text, SECRETS) == text
    body = b"\xff\xfe\x00ordinary\x80binary"
    encoded, kind = _encode_har_body(body, {}, SECRETS)
    assert kind == "base64"
    assert base64.b64decode(encoded, validate=True) == body
    entry = _entry(SECRETS[0], b'{"id":"17","status":"saved"}')
    # Explicit octet-stream is opaque once minimized; labelled JSON keeps id.
    opaque = load_har(dump_har({"log": {"entries": [entry]}}))["log"]["entries"][0]
    assert opaque["response"]["status"] == 200
    assert opaque["response"]["content"]["text"] == ""
    entry["response"]["content"]["mimeType"] = "application/json"
    out = load_har(dump_har({"log": {"entries": [entry]}}))
    record = out["log"]["entries"][0]
    assert record["response"]["status"] == 200
    assert json.loads(record["response"]["content"]["text"])["id"] == "17"


def test_live_proxy_export_and_encrypted_saved_har_share_the_privacy_boundary(
    tmp_path: Path,
):
    secret = SECRETS[0]
    session_cookie = "undeclared-cookie-value-912"
    payload = b"\xff\xfe" + secret.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Set-Cookie", "sid=" + session_cookie)
            self.send_header("X-Result", "kept")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        upstream = f"http://127.0.0.1:{server.server_port}"
        with SessionWireTap(upstream, policy=WirePolicy(secrets=(secret,))) as tap:
            from urllib.parse import urlsplit

            endpoint = urlsplit(tap.base_url)
            conn = http.client.HTTPConnection(
                endpoint.hostname, endpoint.port, timeout=5
            )
            conn.request(
                "POST",
                "/records",
                body=secret.replace("-", "%2D").encode(),
                headers={"Cookie": "sid=" + session_cookie},
            )
            response = conn.getresponse()
            assert response.status == 200
            assert response.read() == payload
            conn.close()
            exported = tap.export_har()
            path = tap.save_har(tmp_path / "session.har", raw_bodies=True)
            assert crypto.is_encrypted(path.read_bytes())
            restored = load_har(path)
            assert restored == exported
            minimized = load_har(tap.save_har(tmp_path / "minimized.har"))
            assert not carries_secret(json.dumps(minimized), (secret,))
            assert path.stat().st_mode & 0o077 == 0
        serialized = json.dumps(restored)
        assert not carries_secret(serialized, (secret,))
        assert session_cookie not in serialized
        response_har = restored["log"]["entries"][0]["response"]
        assert {item["name"]: item["value"] for item in response_har["headers"]}[
            "X-Result"
        ] == "kept"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
