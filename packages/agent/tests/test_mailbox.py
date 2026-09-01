"""Outbound mailbox CLI: claim, wait=0 poll, Allow-per-sub, record_observed."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_agent.mailbox import (
    BOUND_SENTENCE,
    DEFAULT_LEASE_S,
    PAUSE_PROMPT,
    POLL_WAIT_S,
    MailboxClient,
    MailboxError,
    MailboxTransport,
    connect_mailbox,
    open_mailbox_transport,
    require_empty_cookies,
)
from openadapt_agent.runner_bind import (
    AUTHORING_ORIGIN,
    RunnerBindError,
    parse_connect_target,
    parse_runner_uri,
    valid_bind_token,
    valid_lease_secret,
    valid_pack_id,
)
from test_authoring import FakeAuthoringSession

BIND = "oab_" + "A" * 43
PACK = "p.abcdefghijkl"
LEASE = "oals_" + "a" * 64
ORIGIN = "https://openadapt.ai"
URI = f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https%3A%2F%2Fopenadapt.ai"
PACK_URL = f"https://openadapt.ai/j/{PACK}"
SUB = "b" * 64
OTHER_SUB = "d" * 64
CLIENT = "c" * 64


class FakeRecorder:
    def __init__(self) -> None:
        self.observed: list[dict[str, Any]] = []
        self.typed: list[Any] = []

    def type_text(self, *args: Any, **kwargs: Any) -> None:
        self.typed.append((args, kwargs))
        raise AssertionError("Continue must not call type_text")

    def record_observed(self, **kwargs: Any) -> None:
        self.observed.append(kwargs)


class MockMailbox:
    def __init__(
        self,
        *,
        claim_status: int = 201,
        poll_bodies: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.claim_status = claim_status
        self.polls = list(poll_bodies or [])

    def post(
        self, path: str, body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, str], Any]:
        self.requests.append((path, body, headers))
        no_store = {"cache-control": "no-store"}
        if path.endswith("/runner/claim"):
            if self.claim_status == 201:
                return 201, no_store, {"leaseSecret": LEASE, "lease_s": 900}
            return self.claim_status, no_store, {"error": "rejected"}
        if path.endswith("/runner/poll"):
            assert body["wait_seconds"] == 0
            assert body["lease_seconds"] == 900
            assert headers["Authorization"] == f"Bearer {LEASE}"
            if not self.polls:
                return 204, {}, None
            next_body = self.polls.pop(0)
            if next_body is None:
                return 204, {}, None
            return 200, no_store, next_body
        if path.endswith("/runner/callback"):
            assert headers["Authorization"] == f"Bearer {LEASE}"
            return 202, no_store, {"accepted": True}
        if path.endswith("/runner/allow"):
            assert headers["Authorization"] == f"Bearer {LEASE}"
            return 200, no_store, {"allowedAt": "2026-09-01T00:00:00Z"}
        return 404, no_store, {"error": "missing"}


def _envelope(tool: str, *, sub: str = SUB, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.authoring.command/v1",
        "command_id": f"cmd_{tool}",
        "pack_id": PACK,
        "tool": tool,
        "args": args or {},
        "oauth_sub_sha256": sub,
        "client_id_sha256": CLIENT,
        "client_display": "ChatGPT",
    }


def _client(
    mock: MockMailbox,
    *,
    session: Any | None = None,
    prompt: Any = None,
    pause_wait: Any = None,
    unique_window: Any = None,
    recorder: Any = None,
    url: str | None = None,
    platform: str = "darwin",
    stdout: io.StringIO | None = None,
) -> tuple[MailboxClient, io.StringIO]:
    out = stdout or io.StringIO()
    transport = MailboxTransport(origin=ORIGIN, post=mock.post)
    client = MailboxClient(
        transport,
        session=session if session is not None else FakeAuthoringSession(),
        prompt=prompt or (lambda _message: True),
        pause_wait=pause_wait or (lambda: None),
        sleep=lambda _seconds: None,
        stdout=out,
        url=url,
        platform=platform,
        unique_window=unique_window,
        recorder=recorder,
        text_value_at=lambda _pixels: "follow up in two weeks",
    )
    return client, out


def test_parser_accepts_only_the_fixed_runner_action() -> None:
    assert parse_runner_uri(URI) == {
        "pack": PACK,
        "bind": BIND,
        "origin": ORIGIN,
    }
    for uri in (
        URI.replace("://runner?", "://run?"),
        URI.replace("://runner?", "://connect?"),
        URI.replace("openadapt:", "https:"),
        URI.replace("runner?", "runner/claim?"),
        URI + "#fragment",
        f"openadapt://user@runner?pack={PACK}&bind={BIND}&origin={ORIGIN}",
    ):
        with pytest.raises(RunnerBindError, match="Invalid OpenAdapt runner link"):
            parse_runner_uri(uri)


def test_parser_rejects_malformed_missing_duplicate_and_unknown_fields() -> None:
    bad = (
        "",
        "openadapt://runner?pack",
        f"openadapt://runner?pack=short&bind={BIND}&origin={ORIGIN}",
        f"openadapt://runner?pack={PACK}&bind={BIND}",
        f"openadapt://runner?pack={PACK}&bind={BIND}&bind={BIND}&origin={ORIGIN}",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin={ORIGIN}&command=whoami",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://preview.openadapt.ai",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai/",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai:443",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=http://openadapt.ai",
    )
    for uri in bad:
        with pytest.raises(RunnerBindError):
            parse_runner_uri(uri)


def test_prefix_parsers_reject_foreign_and_swapped_encodings() -> None:
    oar = "oar_" + "a" * 64
    oap = "oap_" + "A" * 43
    oab_hex = "oab_" + "a" * 64
    oals_b64 = "oals_" + "A" * 43
    oa_prefix = "oa" + "A" * 43
    oals = "oals_" + "a" * 64

    assert valid_bind_token(BIND) is True
    assert valid_lease_secret(oals) is True
    assert valid_pack_id(PACK) is True
    assert valid_pack_id("v1." + "A" * 48) is True

    for value in (oar, oap, oab_hex, oals_b64, oals, oa_prefix, PACK):
        assert valid_bind_token(value) is False, value
    for value in (oar, oap, BIND, oab_hex, oals_b64, oa_prefix, PACK):
        assert valid_lease_secret(value) is False, value
    for value in (oar, oap, BIND, oals, oa_prefix, "p.short", "v1.short"):
        assert valid_pack_id(value) is False, value

    for bind in (oar, oap, oab_hex, oals_b64, oa_prefix):
        uri = f"openadapt://runner?pack={PACK}&bind={bind}&origin={ORIGIN}"
        with pytest.raises(RunnerBindError, match="Bind token is malformed"):
            parse_runner_uri(uri)


def test_pack_url_parses_without_getting() -> None:
    parsed = parse_connect_target(PACK_URL)
    assert parsed == {"pack": PACK, "origin": ORIGIN}
    with_bind = parse_connect_target(f"{PACK_URL}?bind={BIND}")
    assert with_bind["bind"] == BIND
    assert with_bind["pack"] == PACK


def test_parse_connect_target_does_not_get() -> None:
    source = Path("src/openadapt_agent/runner_bind.py").read_text(encoding="utf-8")
    assert "urlopen" not in source
    assert "Request(" not in source
    assert 'method="GET"' not in source
    assert "method='GET'" not in source


def test_claim_does_not_print_the_lease(capsys) -> None:
    mock = MockMailbox()
    stdout = io.StringIO()
    result = connect_mailbox(
        URI,
        post=mock.post,
        session=FakeAuthoringSession(),
        prompt=lambda _message: True,
        pause_wait=lambda: None,
        sleep=lambda _seconds: None,
        max_polls=1,
        stdout=stdout,
        platform="darwin",
    )
    assert result == 0
    text = stdout.getvalue()
    assert BOUND_SENTENCE in text
    assert LEASE not in text
    assert BIND not in text
    path, body, _headers = mock.requests[0]
    assert path == f"/j/{PACK}/runner/claim"
    assert body == {"bind": BIND}
    captured = capsys.readouterr()
    assert LEASE not in captured.out
    assert LEASE not in captured.err


@pytest.mark.parametrize("status", [409, 410, 404, 401])
def test_claim_maps_mailbox_failures(status: int) -> None:
    mock = MockMailbox(claim_status=status)
    client, _out = _client(mock)
    with pytest.raises(MailboxError):
        client.claim(PACK, BIND)


def test_poll_wait_is_zero_not_twenty_five() -> None:
    assert POLL_WAIT_S == 0
    assert DEFAULT_LEASE_S == 900
    source = Path("src/openadapt_agent/mailbox.py").read_text(encoding="utf-8")
    assert "DEFAULT_WAIT_S" not in source
    assert '"wait_seconds": POLL_WAIT_S' in source
    assert "from openadapt_flow.backends.win_agent" not in source
    assert "launch_agent(" not in source
    assert "parallels_vm" not in source
    mock = MockMailbox()
    client, _out = _client(mock)
    client.claim(PACK, BIND)
    client.poll_once()
    poll = next(item for item in mock.requests if item[0].endswith("/poll"))
    assert poll[1]["wait_seconds"] == 0


def test_bind_pack_allow_is_per_sub_and_required_for_halt() -> None:
    mock = MockMailbox()
    client, out = _client(mock)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    assert "Allow ChatGPT to drive this job?" in out.getvalue()
    allow = next(item for item in mock.requests if item[0].endswith("/allow"))
    assert allow[1] == {"command_id": "cmd_bind_pack"}
    client.handle_envelope(_envelope("observe", sub=OTHER_SUB))
    client.handle_envelope(_envelope("halt", sub=OTHER_SUB))
    denied = [item[1] for item in mock.requests if item[0].endswith("/callback")]
    assert {item["result"]["error"] for item in denied if "error" in item.get("result", {})} >= {
        "not_allowed"
    }
    observed = client.handle_envelope(_envelope("observe"))
    assert observed["schema_version"] == "openadapt.authoring.observe/v1"
    blob = json.dumps(observed)
    assert "title" not in blob
    assert "value" not in blob
    halted = client.handle_envelope(_envelope("halt"))
    assert halted["status"] == "halted"


def test_continue_records_observed_never_type_text() -> None:
    mock = MockMailbox()
    recorder = FakeRecorder()
    session = FakeAuthoringSession()
    client, out = _client(mock, session=session, recorder=recorder)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    client.handle_envelope(_envelope("start_record"))
    client.handle_envelope(_envelope("observe"))
    client.handle_envelope(
        _envelope("pause_for_input", args={"node_id": "n_9f2c001a", "param": "note"})
    )
    assert PAUSE_PROMPT in out.getvalue()
    assert "password" not in out.getvalue().lower()
    assert recorder.typed == []
    assert recorder.observed
    assert recorder.observed[0]["event"] == {"kind": "type"}
    assert "text" in recorder.observed[0]
    callbacks = [item[1] for item in mock.requests if item[0].endswith("/callback")]
    last = callbacks[-1]
    assert last["result"]["recorded"] is True
    assert "text" not in last["result"]
    assert "value" not in last["result"]


def test_secret_continue_has_no_text() -> None:
    mock = MockMailbox()
    recorder = FakeRecorder()
    client, _out = _client(mock, recorder=recorder)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    client.handle_envelope(
        _envelope(
            "pause_for_input",
            args={"node_id": "n_9f2c001a", "param": "ssn", "secret": True},
        )
    )
    assert recorder.typed == []
    assert recorder.observed[0]["secret"] is True
    assert "text" not in recorder.observed[0]


def test_hosted_type_tool_is_refused() -> None:
    mock = MockMailbox()
    session = FakeAuthoringSession()
    client, _out = _client(mock, session=session)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    client.handle_envelope(_envelope("type_text", args={"text": "secret"}))
    assert session.typed_via_backend == []
    callback = [item[1] for item in mock.requests if item[0].endswith("/callback")][-1]
    assert callback["result"]["error"] == "type_refused"


def test_windows_native_is_coach_only() -> None:
    mock = MockMailbox()
    client, _out = _client(mock, platform="win32", url=None)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    observed = client.handle_envelope(_envelope("observe"))
    assert observed["coach_only"] is True
    assert observed["agent_drive"] is False
    assert observed["tree"] == []
    client.handle_envelope(_envelope("start_record"))
    callback = [item[1] for item in mock.requests if item[0].endswith("/callback")][-1]
    assert callback["result"]["error"] == "COACH_ONLY"


def test_linux_without_unique_title_is_coach_only() -> None:
    mock = MockMailbox()
    client, _out = _client(
        mock,
        platform="linux",
        unique_window=lambda: {"backend": "linux", "window_title_unique": False},
        session=FakeAuthoringSession(backend="linux"),
    )
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    observed = client.handle_envelope(_envelope("observe"))
    assert observed["coach_only"] is True
    assert "title" not in json.dumps(observed)


def test_uncertain_delivery_does_not_blind_retry() -> None:
    mock = MockMailbox()
    client, _out = _client(mock)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    client._uncertain = True
    client.handle_envelope(_envelope("click", args={"node_id": "n_9f2c001a"}))
    callback = [item[1] for item in mock.requests if item[0].endswith("/callback")][-1]
    assert callback["result"]["error"] == "RECONCILIATION_REQUIRED"
    client.handle_envelope(_envelope("click", args={"node_id": "n_9f2c001a"}))
    clicks = [
        item
        for item in mock.requests
        if item[0].endswith("/callback") and item[1].get("command_id") == "cmd_click"
    ]
    assert len(clicks) == 1


def test_titles_do_not_go_to_callback() -> None:
    mock = MockMailbox()
    client, _out = _client(mock)
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    client.handle_envelope(_envelope("observe"))
    callback = [item[1] for item in mock.requests if item[0].endswith("/callback")][-1]
    blob = json.dumps(callback)
    assert "title" not in blob
    assert "Jane Roe" not in blob
    assert "4111111111111111" not in blob


def test_pack_url_without_bind_fails_closed() -> None:
    with pytest.raises(MailboxError, match="not a runner link"):
        connect_mailbox(
            PACK_URL,
            post=MockMailbox().post,
            session=FakeAuthoringSession(),
            max_polls=1,
            stdout=io.StringIO(),
        )


def test_open_mailbox_transport_falls_back_when_desktop_missing(monkeypatch) -> None:
    import sys
    import types

    monkeypatch.setitem(sys.modules, "engine", types.ModuleType("engine"))
    monkeypatch.delitem(sys.modules, "engine.authoring_runner", raising=False)

    def missing(*_args, **_kwargs):
        raise ImportError("engine.authoring_runner")

    monkeypatch.setattr(
        "openadapt_agent.mailbox._try_desktop_transport",
        lambda **_kwargs: None,
    )
    transport = open_mailbox_transport()
    assert isinstance(transport, MailboxTransport)


def test_open_mailbox_transport_prefers_desktop_when_importable(monkeypatch) -> None:
    class DesktopTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "openadapt_agent.mailbox._try_desktop_transport",
        lambda **kwargs: DesktopTransport(**kwargs),
    )
    transport = open_mailbox_transport()
    assert isinstance(transport, DesktopTransport)


def test_mailbox_sources_have_no_listener_or_win_agent() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "openadapt_agent"
    for name in ("mailbox.py", "runner_bind.py", "cli.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "HTTPServer" not in text
        assert "uvicorn" not in text
        assert "streamable_http" not in text.lower()
        assert "FastAPI" not in text
        assert "win_agent" not in text
        assert "parallels_vm" not in text
        assert "127.0.0.1" not in text
        assert "ngrok" not in text
    bind_src = (root / "runner_bind.py").read_text(encoding="utf-8")
    assert "wait_seconds" not in bind_src
    assert "def claim" not in bind_src
    mailbox_src = (root / "mailbox.py").read_text(encoding="utf-8")
    assert 'method="POST"' in mailbox_src
    assert 'method="GET"' not in mailbox_src


def test_url_pin_refuses_nonempty_cookies() -> None:
    class Browser:
        def cookies(self):
            return [{"name": "sid", "value": "1"}]

    with pytest.raises(MailboxError, match="empty cookies"):
        require_empty_cookies(Browser())

    class Empty:
        def cookies(self):
            return []

    require_empty_cookies(Empty())


def test_try_desktop_transport_is_none_without_engine() -> None:
    from openadapt_agent.mailbox import _try_desktop_transport

    assert _try_desktop_transport(origin=AUTHORING_ORIGIN) is None


def test_replace_allow_required_for_a_second_sub() -> None:
    mock = MockMailbox()
    answers = iter([True, False, True])
    client, _out = _client(mock, prompt=lambda _message: next(answers))
    client.claim(PACK, BIND)
    client.handle_envelope(_envelope("bind_pack"))
    assert client._allowed_sub == SUB
    second = _envelope("bind_pack", sub=OTHER_SUB)
    second["command_id"] = "cmd_bind_pack_other"
    denied = client.handle_envelope(second)
    assert denied["error"] == "denied"
    client._pending_allow = {
        "command_id": "cmd_bind_pack_2",
        "oauth_sub_sha256": OTHER_SUB,
        "client_display": "ChatGPT",
        "replace": True,
    }
    client.allow(replace=True)
    assert client._allowed_sub == OTHER_SUB


def test_connect_help_does_not_name_mockmed(capsys) -> None:
    from openadapt_agent.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["authoring", "connect", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "openadapt://runner" in out
    assert "Allow" in out
    assert "MockMed" not in out
    assert "password" not in out.lower()
