"""Actual localhost transfers and synthetic archives; no released tools run."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_checked_tool",
    Path(__file__).resolve().parents[1] / "install_checked_tool.py",
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
PROGRAM = b"#!/bin/sh\nprintf 'synthetic-tool\\n'\n"


def archive(entries=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, body, mode, kind in entries or [
            ("tool", PROGRAM, 0o755, tarfile.REGTYPE)
        ]:
            info = tarfile.TarInfo(name)
            info.mode, info.type = mode, kind
            info.size = len(body) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "elsewhere"
            tar.addfile(info, io.BytesIO(body) if info.size else None)
    return gzip.compress(stream.getvalue())


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    responses = []
    requests = []
    destination = tmp_path / "bin/tool"
    real_sleep = time.sleep
    # Retry-count tests avoid backoff walltime; transfer/deadline tests still
    # use the actual monotonic clock and real curl subprocess timeout.
    monkeypatch.setattr(installer.time, "sleep", lambda _: None)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            index = len(requests)
            requests.append({"path": self.path, "published": destination.exists()})
            status, body, extra = responses[min(index, len(responses) - 1)]
            if extra.get("delay"):
                real_sleep(extra["delay"])
            self.send_response(status)
            self.send_header("Content-Length", str(extra.get("length", len(body))))
            if "location" in extra:
                self.send_header("Location", extra["location"])
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/release",
            responses,
            requests,
            destination,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        assert not thread.is_alive()


def install(endpoint, body, member="tool"):
    url, _, _, destination = endpoint
    installer.install(url, hashlib.sha256(body).hexdigest(), member, destination)
    return destination


def test_verified_installation_executes_only_the_synthetic_member(endpoint):
    body = archive()
    endpoint[1].append((200, body, {}))
    destination = install(endpoint, body)
    assert destination.read_bytes() == PROGRAM
    assert destination.stat().st_mode & 0o777 == 0o755
    assert subprocess.check_output([destination], text=True) == "synthetic-tool\n"
    assert endpoint[2] == [{"path": "/release", "published": False}]
    assert not list(destination.parent.glob(".checked-tool-*"))


@pytest.mark.parametrize("status", [404, 429, 500, 502, 503])
def test_transient_http_failure_retries_before_atomic_install(endpoint, status):
    body = archive()
    endpoint[1].extend([(status, b"temporary HTTP error", {}), (200, body, {})])
    assert install(endpoint, body).read_bytes() == PROGRAM
    assert len(endpoint[2]) == 2
    assert all(not request["published"] for request in endpoint[2])


@pytest.mark.parametrize("status", [204, 206, 400, 401, 403, 404, 429, 500])
def test_non_success_response_cannot_install_even_matching_bytes(endpoint, status):
    body = archive()
    endpoint[1].append((status, body, {}))
    with pytest.raises(installer.InstallError, match="failed after 3 attempts"):
        install(endpoint, body)
    assert len(endpoint[2]) == 3
    assert not endpoint[3].exists()
    assert not list(endpoint[3].parent.glob(".checked-tool-*"))


@pytest.mark.parametrize(
    "problem",
    [
        "html",
        "checksum",
        "short_http",
        "gzip_trailer",
        "gzip_crc",
        "invalid_tar",
        "short_tar",
    ],
)
def test_invalid_or_partial_content_never_publishes_a_tool(endpoint, problem):
    expected = body = archive()
    extra = {}
    if problem == "html":
        expected = body = b"<html>service unavailable</html>"
    elif problem == "checksum":
        body = archive([("tool", PROGRAM + b"# different\n", 0o755, tarfile.REGTYPE)])
    elif problem == "short_http":
        extra["length"] = len(body) + 100
    elif problem == "gzip_trailer":
        expected = body = body[:-4]
    elif problem == "gzip_crc":
        expected = body = body[:-8] + bytes([body[-8] ^ 1]) + body[-7:]
    elif problem == "invalid_tar":
        expected = body = gzip.compress(b"not a tar archive")
    elif problem == "short_tar":
        expected = body = gzip.compress(
            gzip.decompress(archive([("tool", b"x" * 4096, 0o755, tarfile.REGTYPE)]))[
                :700
            ]
        )
    endpoint[1].append((200, body, extra))
    with pytest.raises(installer.InstallError, match="failed after 3 attempts"):
        install(endpoint, expected)
    assert len(endpoint[2]) == 3
    assert not endpoint[3].exists()


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.FIFOTYPE,
        tarfile.DIRTYPE,
    ],
)
def test_declared_tool_must_be_a_regular_executable(endpoint, kind):
    body = archive([("tool", PROGRAM, 0o755, kind)])
    endpoint[1].append((200, body, {}))
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert len(endpoint[2]) == 3
    assert not endpoint[3].exists()


@pytest.mark.parametrize(
    "problem",
    [
        "traversal",
        "absolute",
        "duplicate",
        "missing",
        "empty",
        "mode",
        "unrelated_link",
    ],
)
def test_archive_member_contract_is_closed(endpoint, problem):
    entries = [("tool", PROGRAM, 0o755, tarfile.REGTYPE)]
    if problem == "traversal":
        entries.append(("../escape", PROGRAM, 0o755, tarfile.REGTYPE))
    elif problem == "absolute":
        entries.append(("/escape", PROGRAM, 0o755, tarfile.REGTYPE))
    elif problem == "duplicate":
        entries *= 2
    elif problem == "missing":
        entries[0] = ("other", PROGRAM, 0o755, tarfile.REGTYPE)
    elif problem == "empty":
        entries[0] = ("tool", b"", 0o755, tarfile.REGTYPE)
    elif problem == "mode":
        entries[0] = ("tool", PROGRAM, 0o644, tarfile.REGTYPE)
    else:
        entries.append(("unrelated", b"", 0o755, tarfile.SYMTYPE))
    body = archive(entries)
    endpoint[1].append((200, body, {}))
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert len(endpoint[2]) == 3
    assert not endpoint[3].exists()
    assert not (endpoint[3].parent.parent / "escape").exists()


def test_corrected_content_can_succeed_after_a_bad_response(endpoint):
    body = archive()
    endpoint[1].extend([(200, b"invalid first response", {}), (200, body, {})])
    assert install(endpoint, body).read_bytes() == PROGRAM
    assert len(endpoint[2]) == 2


def test_nested_exact_member_is_supported(endpoint):
    body = archive([("release/tool", PROGRAM, 0o755, tarfile.REGTYPE)])
    endpoint[1].append((200, body, {}))
    assert install(endpoint, body, "release/tool").read_bytes() == PROGRAM


def test_existing_destination_is_not_reused_as_a_success(endpoint):
    endpoint[3].parent.mkdir()
    endpoint[3].write_bytes(b"stale")
    with pytest.raises(installer.InstallError, match="already exists"):
        install(endpoint, archive())
    assert endpoint[3].read_bytes() == b"stale"
    assert not endpoint[2]


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_EXPANDED_BYTES"])
def test_size_budget_refuses_before_publication(endpoint, monkeypatch, limit):
    body = archive()
    monkeypatch.setattr(installer, limit, 10)
    endpoint[1].append((200, body, {}))
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert not endpoint[3].exists()
    assert len(endpoint[2]) == 3


def test_actual_transfer_timeout_obeys_one_overall_budget(endpoint, monkeypatch):
    body = archive()
    endpoint[1].append((200, body, {"delay": 0.3}))
    monkeypatch.setattr(installer, "ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(installer, "BUDGET_SECONDS", 0.12)
    started = time.monotonic()
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert time.monotonic() - started < 2
    assert 1 <= len(endpoint[2]) <= 3
    assert not endpoint[3].exists()


def test_install_budget_and_cli_policy_remain_explicit():
    assert installer.ATTEMPTS == 3
    assert installer.BUDGET_SECONDS == 150
    assert installer.ATTEMPT_SECONDS == 45
    with pytest.raises(SystemExit) as error:
        installer.main(
            [
                "--url",
                "http://example.invalid/tool",
                "--sha256",
                "0" * 64,
                "--member",
                "tool",
                "--destination",
                "/unused/tool",
            ]
        )
    assert error.value.code == 2


def test_redirect_cannot_downgrade_to_an_http_destination(endpoint):
    body = archive()
    endpoint[1].append((302, b"", {"location": endpoint[0] + "/redirected"}))
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert [request["path"] for request in endpoint[2]] == ["/release"] * 3
    assert not endpoint[3].exists()


def test_atomic_creation_cannot_replace_a_destination_created_during_download(
    endpoint, monkeypatch
):
    body = archive()
    endpoint[1].append((200, body, {}))
    unpack = installer._unpack

    def competing_destination(*args):
        verified = unpack(*args)
        endpoint[3].write_bytes(b"other owner")
        return verified

    monkeypatch.setattr(installer, "_unpack", competing_destination)
    with pytest.raises(installer.InstallError):
        install(endpoint, body)
    assert endpoint[3].read_bytes() == b"other owner"
    assert not list(endpoint[3].parent.glob(".checked-tool-*"))
