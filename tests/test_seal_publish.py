"""`openadapt seal publish`: nothing leaves without a yes, and only the public card.

Every test here runs with real sockets blocked, so a request that slipped past
the mock transport fails the test instead of reaching a server.
"""

from __future__ import annotations

import io
import json
import socket
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip(
    "openadapt_flow.public_seal", reason="openadapt-flow without public Seals"
)

from openadapt import (
    cli,  # noqa: E402
    seal_publish,  # noqa: E402
)
from openadapt.quickstart.seal_link import read_card  # noqa: E402
from openadapt.seal_publish import (  # noqa: E402
    PROMPT,
    PublishError,
    Response,
    check_link,
    consent,
    endpoint,
    parse_link,
    publish,
    unpublish,
)

SEAL_ID = "A" * 22 + "b" * 21
TOKEN = "t" * 43
ORIGIN = "https://seal-endpoint.test"


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    """Any real connection fails the test."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(seal_publish, "urllib_transport", refuse)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal-home"))
    monkeypatch.setenv("BROWSER", "true")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_ENDPOINT", raising=False)


class Recorder:
    """A mock transport: records each request and answers from a list."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, bytes | None, dict]] = []

    def __call__(self, method, url, body, headers):
        self.requests.append((method, url, body, dict(headers)))
        return self.responses.pop(0)


def created() -> Response:
    body = {"id": SEAL_ID, "url": f"{ORIGIN}/seal#{SEAL_ID}", "delete_token": TOKEN}
    return Response(201, {}, json.dumps(body).encode())


def _sealed_run(tmp_path: Path, name: str = "run-1", **report_extra) -> Path:
    from openadapt_flow.ir import RunReport, StepResult
    from openadapt_flow.offline_seal import seal_home, seal_run

    run = tmp_path / "openadapt-quickstart" / name
    run.mkdir(parents=True)
    report = RunReport(
        workflow_name="local-quickstart",
        started_at="2026-11-18T09:41:07+00:00",
        execution_outcome="VERIFIED",
        results=[
            StepResult(
                step_id="s1", intent="type Jane Doe", ok=True, effect_verified=True
            )
        ],
        params={"patient": "Jane Doe"},
        bundle_content_digest="b" * 64,
    )
    data = json.loads(report.model_dump_json())
    data.update(report_extra)
    (run / "report.json").write_text(json.dumps(data), encoding="utf-8")
    seal_run(run, home=seal_home(), enqueue=False)
    return run


def _publish(run: Path, transport: Recorder, **kwargs) -> tuple[str | None, list[str]]:
    said: list[str] = []
    kwargs.setdefault("endpoint_value", ORIGIN)
    url = publish(run, transport=transport, say=said.append, **kwargs)
    return url, said


# -- upload --------------------------------------------------------------------


def test_publish_uploads_the_public_seal_bytes_and_saves_the_token(tmp_path):
    run = _sealed_run(tmp_path)
    transport = Recorder(created())
    url, said = _publish(run, transport, yes=True, interactive=False)
    assert url == f"{ORIGIN}/seal#{SEAL_ID}"
    [(method, target, body, headers)] = transport.requests
    assert (method, target) == ("POST", f"{ORIGIN}/api/public/local-seals")
    assert body == (run / "public-seal.json").read_bytes()
    assert len(body) <= 4096
    assert b"Jane" not in body and b"Doe" not in body
    assert headers["Content-Type"] == "application/json"
    record = json.loads((run / "seal.publish.json").read_text())
    assert record == {"id": SEAL_ID, "url": url, "delete_token": TOKEN}
    assert stat.S_IMODE((run / "seal.publish.json").stat().st_mode) == 0o600
    assert f"Published: {url}" in said
    # The exact card is shown before anything is sent.
    shown = json.loads(said[0])
    assert shown["card"] == json.loads(body)["card"]


def test_no_terminal_and_no_yes_refuses_without_a_request(tmp_path):
    run = _sealed_run(tmp_path)
    transport = Recorder()
    with pytest.raises(PublishError) as exc:
        _publish(run, transport, interactive=False)
    assert exc.value.exit_code == 2
    assert transport.requests == []
    assert not (run / "seal.publish.json").exists()


def test_ci_refuses_even_with_yes_unless_allow_ci(tmp_path, monkeypatch):
    run = _sealed_run(tmp_path)
    monkeypatch.setenv("CI", "true")
    transport = Recorder(created())
    with pytest.raises(PublishError) as exc:
        _publish(run, transport, yes=True, interactive=True, ask=lambda _: "y")
    assert exc.value.exit_code == 2 and transport.requests == []
    url, _ = _publish(run, transport, yes=True, allow_ci=True, interactive=False)
    assert url and len(transport.requests) == 1


@pytest.mark.parametrize("answer", ["", "n", "no", "N", "maybe"])
def test_the_prompt_defaults_to_no(tmp_path, answer):
    run = _sealed_run(tmp_path)
    transport = Recorder()
    asked: list[str] = []
    url, said = _publish(
        run, transport, interactive=True, ask=lambda q: asked.append(q) or answer
    )
    assert url is None and transport.requests == []
    assert asked == [PROMPT.format(host="seal-endpoint.test")]
    assert said[-1] == "Not published."


def test_the_prompt_text_is_the_agreed_one():
    assert PROMPT.format(host="app.openadapt.ai") == (
        "This uploads the summary above to app.openadapt.ai and gives you a link. "
        "Anyone with the link can see it. It does not upload your inputs, "
        "screenshots or records. Publish? [y/N] "
    )


def test_yes_at_the_prompt_publishes(tmp_path):
    run = _sealed_run(tmp_path)
    transport = Recorder(created())
    url, _ = _publish(run, transport, interactive=True, ask=lambda _: "y")
    assert url and len(transport.requests) == 1


def test_dry_run_writes_the_file_and_sends_nothing(tmp_path):
    run = _sealed_run(tmp_path)
    transport = Recorder()
    url, said = _publish(run, transport, dry_run=True, yes=True)
    assert url is None and transport.requests == []
    assert (run / "public-seal.json").is_file()
    assert said[-1].startswith("Dry run:")


def test_a_published_run_is_not_uploaded_twice(tmp_path):
    run = _sealed_run(tmp_path)
    transport = Recorder(created())
    first, _ = _publish(run, transport, yes=True, interactive=False)
    again, said = _publish(run, transport, yes=True, interactive=False)
    assert again == first and len(transport.requests) == 1
    assert said[0] == f"Already published: {first}"


@pytest.mark.parametrize(
    ("status", "headers", "phrase"),
    [
        (409, {}, "already online"),
        (429, {"Retry-After": "120"}, "Try again in 120 seconds"),
        (413, {}, "too large"),
        (503, {}, "busy"),
        (400, {}, "invalid"),
    ],
)
def test_server_refusals_are_plain_and_save_nothing(tmp_path, status, headers, phrase):
    run = _sealed_run(tmp_path)
    body = json.dumps({"error": "invalid"}).encode()
    with pytest.raises(PublishError) as exc:
        _publish(
            run, Recorder(Response(status, headers, body)), yes=True, interactive=False
        )
    assert phrase in str(exc.value)
    assert not (run / "seal.publish.json").exists()


def test_names_that_fail_the_pattern_suggest_no_names(tmp_path):
    run = _sealed_run(tmp_path, workflow_name="Jane Doe's chart/notes")
    with pytest.raises(PublishError) as exc:
        _publish(run, Recorder(), dry_run=True)
    assert "--no-names" in str(exc.value)
    _publish(run, Recorder(), dry_run=True, no_names=True)
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["program_name"] is None


def test_the_endpoint_comes_from_the_environment(monkeypatch):
    assert endpoint(env={}) == "https://app.openadapt.ai"
    assert (
        endpoint(env={"OPENADAPT_SEAL_ENDPOINT": "https://x.test/"}) == "https://x.test"
    )
    assert endpoint(env={"OPENADAPT_SEAL_ENDPOINT": "http://localhost:3000"})
    with pytest.raises(PublishError):
        endpoint("http://plain.test")
    with pytest.raises(PublishError):
        endpoint("https://x.test/path")


def test_consent_never_asks_off_a_terminal():
    def ask(_prompt):
        raise AssertionError("asked without a terminal")

    with pytest.raises(PublishError):
        consent(yes=False, allow_ci=False, interactive=False, ask=ask, host="h", env={})


# -- the command ----------------------------------------------------------------


def test_cli_without_a_terminal_exits_2_and_sends_nothing(tmp_path, monkeypatch):
    run = _sealed_run(tmp_path)
    result = CliRunner().invoke(cli.main, ["seal", "publish", str(run)])
    assert result.exit_code == 2, result.output
    assert "Pass --yes" in result.output
    assert not (run / "seal.publish.json").exists()


def test_cli_dry_run_defaults_to_the_newest_quickstart_run(tmp_path, monkeypatch):
    _sealed_run(tmp_path, "run-1")
    newest = _sealed_run(tmp_path, "run-2")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["seal", "publish", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (newest / "public-seal.json").is_file()
    assert '"steps_total": 1' in result.output


def test_cli_publish_with_yes_uses_the_transport(tmp_path, monkeypatch):
    run = _sealed_run(tmp_path)
    transport = Recorder(created())
    monkeypatch.setattr(seal_publish, "urllib_transport", transport)
    result = CliRunner().invoke(
        cli.main, ["seal", "publish", str(run), "--yes", "--endpoint", ORIGIN]
    )
    assert result.exit_code == 0, result.output
    assert f"Published: {ORIGIN}/seal#{SEAL_ID}" in result.output


def test_cli_publish_help_runs_nothing(tmp_path):
    result = CliRunner().invoke(cli.main, ["seal", "publish", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output and "--no-names" in result.output


# -- unpublish and links ---------------------------------------------------------


def test_unpublish_sends_the_token_and_forgets_it(tmp_path):
    run = _sealed_run(tmp_path)
    _publish(run, Recorder(created()), yes=True, interactive=False)
    transport = Recorder(Response(204, {}, b""))
    line = unpublish(run, origin=None, transport=transport)
    [(method, target, body, headers)] = transport.requests
    assert (method, target, body) == (
        "DELETE",
        f"{ORIGIN}/api/public/local-seals/{SEAL_ID}",
        None,
    )
    assert headers["Authorization"] == f"Delete {TOKEN}"
    assert line.startswith("Unpublished.")
    assert not (run / "seal.publish.json").exists()


def test_unpublish_with_a_refused_token_keeps_it(tmp_path):
    run = _sealed_run(tmp_path)
    _publish(run, Recorder(created()), yes=True, interactive=False)
    with pytest.raises(PublishError):
        unpublish(run, origin=None, transport=Recorder(Response(403, {}, b"")))
    assert (run / "seal.publish.json").exists()


def _hosted(run: Path, **identity) -> Response:
    from openadapt_flow.offline_seal import seal_sha256

    doc = json.loads((run / "public-seal.json").read_text())
    body = {
        **doc,
        "seal_sha256": seal_sha256(doc["seal"]),
        "created_at": "2026-11-18T10:00:00Z",
        "identity": identity or {"status": "unlinked"},
        "verified": True,
    }
    return Response(200, {}, json.dumps(body).encode())


def test_seal_verify_checks_a_hosted_link_here(tmp_path):
    run = _sealed_run(tmp_path)
    _publish(run, Recorder(), dry_run=True)
    said: list[str] = []
    transport = Recorder(_hosted(run))
    url = f"https://app.openadapt.ai/seal#{SEAL_ID}"
    assert check_link(url, transport=transport, say=said.append) == 0
    assert transport.requests[0][:2] == (
        "GET",
        f"https://app.openadapt.ai/api/public/local-seals/{SEAL_ID}",
    )
    assert said[0].startswith(
        "Signature: valid. This Seal was signed by key seal-ed25519-"
    )
    assert (
        said[1]
        == "Key holder: not linked to a person or company. Anyone can make a key."
    )
    assert "OpenAdapt did not watch this run." in said[2]


def test_seal_verify_refuses_a_changed_card_whatever_the_server_says(tmp_path):
    run = _sealed_run(tmp_path)
    _publish(run, Recorder(), dry_run=True)
    reply = _hosted(run)
    data = json.loads(reply.body)
    data["card"]["steps_ok"] = 0
    data["card"]["steps"] = []
    said: list[str] = []
    tampered = Response(200, {}, json.dumps(data).encode())
    url = f"https://app.openadapt.ai/seal#{SEAL_ID}"
    assert check_link(url, transport=Recorder(tampered), say=said.append) == 1
    assert said == ["not valid: card_mismatch"]


def test_seal_verify_reports_a_deleted_seal(tmp_path):
    said: list[str] = []
    url = f"https://app.openadapt.ai/seal#{SEAL_ID}"
    assert (
        check_link(url, transport=Recorder(Response(410, {}, b"")), say=said.append)
        == 1
    )
    assert "deleted" in said[0]


def test_only_hosted_seal_links_are_links():
    assert parse_link(f"https://app.openadapt.ai/seal#{SEAL_ID}") == (
        "https://app.openadapt.ai",
        SEAL_ID,
    )
    assert parse_link(f"https://evil.test/seal#{SEAL_ID}") is None
    assert parse_link("https://app.openadapt.ai/seal#short") is None
    assert parse_link(f"https://app.openadapt.ai/receipts#{SEAL_ID}") is None
    assert parse_link("report.json") is None


def test_cli_seal_verify_routes_a_link(tmp_path, monkeypatch):
    run = _sealed_run(tmp_path)
    _publish(run, Recorder(), dry_run=True)
    monkeypatch.setattr(seal_publish, "urllib_transport", Recorder(_hosted(run)))
    result = CliRunner().invoke(
        cli.main, ["seal", "verify", f"https://app.openadapt.ai/seal#{SEAL_ID}"]
    )
    assert result.exit_code == 0, result.output
    assert "Signature: valid." in result.output


# -- quickstart -----------------------------------------------------------------


def test_the_quickstart_offer_exists_only_in_a_terminal_outside_ci(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert cli._quickstart_publish_offer() is not None
    monkeypatch.setenv("CI", "1")
    assert cli._quickstart_publish_offer() is None
    monkeypatch.delenv("CI")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._quickstart_publish_offer() is None


def test_the_quickstart_offer_asks_once_and_no_sends_nothing(tmp_path, monkeypatch):
    run = _sealed_run(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    offer = cli._quickstart_publish_offer()
    asked: list[str] = []
    said: list[str] = []
    offer(run, said.append, lambda q: asked.append(q) or "")
    assert len(asked) == 1 and asked[0].endswith("Publish? [y/N] ")
    assert said[-1] == "Not published."
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["provenance"] == "synthetic-tutorial"
    assert not (run / "seal.publish.json").exists()


def test_the_finale_offers_once_after_the_seal_card(tmp_path, monkeypatch):
    from test_quickstart import FakeEngine, _clock

    from openadapt.quickstart import next_steps
    from openadapt.quickstart.story import run_story
    from openadapt.quickstart.view import PlainView

    monkeypatch.chdir(tmp_path)
    calls: list[Path] = []

    def offer(run_dir, say, ask):
        calls.append(run_dir)
        say("offer shown")

    root = tmp_path / "openadapt-quickstart"
    stream = io.StringIO()
    run_story(
        FakeEngine(root),
        PlainView(stream),
        next_steps=next_steps(cli._hint, cli._shell_quote, True, offer_publish=offer),
        clock=_clock(),
    )
    assert calls == [root / "run-1"]
    text = stream.getvalue()
    assert text.index("Your Seal:") < text.index("offer shown") < text.index("Next:")
    assert "seal publish" not in text


def test_without_a_terminal_the_finale_prints_the_publish_command(
    tmp_path, monkeypatch
):
    from test_quickstart import FakeEngine, _clock

    from openadapt.quickstart import next_steps
    from openadapt.quickstart.story import run_story
    from openadapt.quickstart.view import PlainView

    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    run_story(
        FakeEngine(tmp_path / "openadapt-quickstart"),
        PlainView(stream),
        next_steps=next_steps(cli._hint, cli._shell_quote, True),
        clock=_clock(),
    )
    assert (
        "  Put it online: openadapt seal publish openadapt-quickstart/run-1\n"
        in stream.getvalue()
    )


def test_read_card_needs_both_files(tmp_path):
    from test_quickstart import RECEIPT, SEAL

    assert read_card(tmp_path) is None
    (tmp_path / "receipt.json").write_text(json.dumps(RECEIPT))
    assert read_card(tmp_path) is None
    (tmp_path / "seal.json").write_text(json.dumps(SEAL))
    card = read_card(tmp_path)
    assert card is not None
    assert (card.steps_ok, card.effects_confirmed, card.model_calls) == (6, 2, 0)
    assert card.short_digest == "6dd4a7f2d1ea"
