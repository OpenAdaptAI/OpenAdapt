"""`openadapt seal publish`: nothing leaves without a yes, and only the public card.

Every test here runs with real sockets blocked, so a request that slipped past
the mock transport fails the test instead of reaching a server.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli, seal_publish
from openadapt.quickstart.seal_link import read_card
from openadapt.seal_publish import (
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

#: The engine's golden vectors: a real signed public Seal, used as the upload
#: when this environment's openadapt-flow cannot build one.
VECTORS = (
    Path(__file__).resolve().parents[1]
    / "packages/flow/tests/fixtures/public_seal/golden_vectors.json"
)
ENGINE = seal_publish.available()
needs_engine = pytest.mark.skipif(
    not ENGINE, reason="this openadapt-flow cannot build public Seals"
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

    def __call__(self, method, url, body, headers, *_timeout):
        self.requests.append((method, url, body, dict(headers)))
        return self.responses.pop(0)


def created() -> Response:
    body = {"id": SEAL_ID, "url": f"{ORIGIN}/seal#{SEAL_ID}", "delete_token": TOKEN}
    return Response(201, {}, json.dumps(body).encode())


def _golden_doc() -> dict:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    body = next(c["body"] for c in vectors["cases"] if c["name"] == "valid")
    return json.loads(body)


def _real_run(tmp_path: Path, name: str, report_extra: dict) -> Path:
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


@pytest.fixture()
def make_run(tmp_path, monkeypatch):
    """A sealed run folder. Without an engine that builds public Seals, the
    build step is replaced by the engine's golden public Seal, so the consent,
    upload and token tests still run."""

    def make(name: str = "run-1", **report_extra) -> Path:
        if ENGINE:
            return _real_run(tmp_path, name, report_extra)
        run = tmp_path / "openadapt-quickstart" / name
        run.mkdir(parents=True)
        (run / "seal.json").write_text("{}", encoding="utf-8")

        def build(run_dir, **_kwargs):
            doc = _golden_doc()
            (Path(run_dir) / "public-seal.json").write_bytes(
                seal_publish.public_bytes(doc)
            )
            return doc

        monkeypatch.setattr(seal_publish, "build", build)
        return run

    return make


def _publish(run: Path, transport: Recorder, **kwargs) -> tuple[str | None, list[str]]:
    said: list[str] = []
    kwargs.setdefault("endpoint_value", ORIGIN)
    url = publish(run, transport=transport, say=said.append, **kwargs)
    return url, said


# -- upload --------------------------------------------------------------------


def test_publish_uploads_the_public_seal_bytes_and_saves_the_token(make_run, tmp_path):
    run = make_run()
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


def test_no_terminal_and_no_yes_refuses_without_a_request(make_run, tmp_path):
    run = make_run()
    transport = Recorder()
    with pytest.raises(PublishError) as exc:
        _publish(run, transport, interactive=False)
    assert exc.value.exit_code == 2
    assert transport.requests == []
    assert not (run / "seal.publish.json").exists()


def test_ci_refuses_even_with_yes_unless_allow_ci(make_run, tmp_path, monkeypatch):
    run = make_run()
    monkeypatch.setenv("CI", "true")
    transport = Recorder(created())
    with pytest.raises(PublishError) as exc:
        _publish(run, transport, yes=True, interactive=True, ask=lambda _: "y")
    assert exc.value.exit_code == 2 and transport.requests == []
    url, _ = _publish(run, transport, yes=True, allow_ci=True, interactive=False)
    assert url and len(transport.requests) == 1


@pytest.mark.parametrize("answer", ["", "n", "no", "N", "maybe"])
def test_the_prompt_defaults_to_no(make_run, tmp_path, answer):
    run = make_run()
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


def test_yes_at_the_prompt_publishes(make_run, tmp_path):
    run = make_run()
    transport = Recorder(created())
    url, _ = _publish(run, transport, interactive=True, ask=lambda _: "y")
    assert url and len(transport.requests) == 1


def test_dry_run_writes_the_file_and_sends_nothing(make_run, tmp_path):
    run = make_run()
    transport = Recorder()
    url, said = _publish(run, transport, dry_run=True, yes=True)
    assert url is None and transport.requests == []
    assert (run / "public-seal.json").is_file()
    assert said[-1].startswith("Dry run:")


def test_a_published_run_is_not_uploaded_twice(make_run, tmp_path):
    run = make_run()
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
def test_server_refusals_are_plain_and_save_nothing(
    make_run, tmp_path, status, headers, phrase
):
    run = make_run()
    body = json.dumps({"error": "invalid"}).encode()
    with pytest.raises(PublishError) as exc:
        _publish(
            run, Recorder(Response(status, headers, body)), yes=True, interactive=False
        )
    assert phrase in str(exc.value)
    assert not (run / "seal.publish.json").exists()


@needs_engine
def test_names_that_fail_the_pattern_suggest_leaving_them_out(make_run, tmp_path):
    run = make_run(workflow_name="Jane Doe's chart/notes")
    with pytest.raises(PublishError) as exc:
        _publish(run, Recorder(), dry_run=True, names=True)
    assert "without --names" in str(exc.value)
    _publish(run, Recorder(), dry_run=True)
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["program_name"] is None


@needs_engine
def test_a_real_run_has_no_names_unless_asked(make_run, tmp_path):
    run = make_run()
    _publish(run, Recorder(), dry_run=True)
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["provenance"] == "production"
    assert card["program_name"] is None and "app_name" not in card
    _publish(run, Recorder(), dry_run=True, names=True)
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["program_name"] == "local-quickstart"


@needs_engine
def test_a_tutorial_run_carries_its_name_by_default(make_run, tmp_path):
    run = make_run()
    _publish(run, Recorder(), dry_run=True, provenance="synthetic-tutorial")
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["program_name"] == "local-quickstart"
    _publish(
        run, Recorder(), dry_run=True, provenance="synthetic-tutorial", names=False
    )
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


def test_cli_without_a_terminal_exits_2_and_sends_nothing(
    make_run, tmp_path, monkeypatch
):
    run = make_run()
    result = CliRunner().invoke(cli.main, ["seal", "publish", str(run)])
    assert result.exit_code == 2, result.output
    assert "Pass --yes" in result.output
    assert not (run / "seal.publish.json").exists()


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (("run-1", "run-2"), "run-2"),
        (("run-2", "run-1"), "run-2"),
        (("run-9", "run-10"), "run-9"),
        (("run-10", "run-9"), "run-9"),
    ],
)
def test_newest_sealed_run_equal_nanoseconds_uses_lexical_name(
    tmp_path, monkeypatch, names, expected
):
    root = tmp_path / "openadapt-quickstart"
    root.mkdir()
    timestamp = 1_700_000_000_000_000_000
    for name in (*names, "run-z"):
        run = root / name
        run.mkdir()
        seal = run / "seal.json"
        seal.write_text("{}", encoding="utf-8")
        when = timestamp - 2_000_000_000 if name == "run-z" else timestamp
        os.utime(seal, ns=(when, when))
    assert {(root / name / "seal.json").stat().st_mtime_ns for name in names} == {
        timestamp
    }
    assert (
        root / "run-z" / "seal.json"
    ).stat().st_mtime_ns == timestamp - 2_000_000_000
    monkeypatch.chdir(tmp_path)
    # Equal times use lexical order, not a claim about run sequence numbers.
    assert cli._newest_sealed_run() == root / expected
    assert not list(root.glob("*/public-seal.json"))


def test_cli_dry_run_defaults_to_the_newest_quickstart_run(
    make_run, tmp_path, monkeypatch
):
    older = make_run("run-1")
    newest = make_run("run-2")
    # Give the two Seals distinct times; the helper must pick the newer one.
    os.utime(older / "seal.json", ns=(1_000_000_000, 1_000_000_000))
    os.utime(newest / "seal.json", ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["seal", "publish", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (newest / "public-seal.json").is_file()
    assert '"steps_total":' in result.output


def test_cli_publish_with_yes_uses_the_transport(make_run, tmp_path, monkeypatch):
    run = make_run()
    transport = Recorder(created())
    monkeypatch.setattr(seal_publish, "urllib_transport", transport)
    result = CliRunner().invoke(
        cli.main, ["seal", "publish", str(run), "--yes", "--endpoint", ORIGIN]
    )
    assert result.exit_code == 0, result.output
    assert f"Published: {ORIGIN}/seal#{SEAL_ID}" in result.output
    # --yes skips the question, not the card: it is printed before the upload.
    assert result.output.index('"card"') < result.output.index("Published:")
    assert "Publish? [y/N]" not in result.output


def test_cli_publish_help_runs_nothing(tmp_path):
    result = CliRunner().invoke(cli.main, ["seal", "publish", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output and "--names" in result.output


def test_cli_names_and_no_names_cannot_both_be_given(make_run, tmp_path):
    run = make_run()
    result = CliRunner().invoke(
        cli.main, ["seal", "publish", str(run), "--names", "--no-names", "--dry-run"]
    )
    assert result.exit_code == 2
    assert not (run / "public-seal.json").exists()


# -- unpublish and links ---------------------------------------------------------


def test_unpublish_sends_the_token_and_forgets_it(make_run, tmp_path):
    run = make_run()
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


def test_unpublish_with_a_refused_token_keeps_it(make_run, tmp_path):
    run = make_run()
    _publish(run, Recorder(created()), yes=True, interactive=False)
    with pytest.raises(PublishError):
        unpublish(run, origin=None, transport=Recorder(Response(403, {}, b"")))
    assert (run / "seal.publish.json").exists()


def test_unpublish_on_a_404_keeps_the_only_copy_of_the_token(make_run, tmp_path):
    run = make_run()
    _publish(run, Recorder(created()), yes=True, interactive=False)
    with pytest.raises(PublishError) as caught:
        unpublish(run, origin=None, transport=Recorder(Response(404, {}, b"")))
    assert "stays in" in str(caught.value)
    assert (run / "seal.publish.json").exists()


def test_unpublish_on_a_410_forgets_the_token(make_run, tmp_path):
    run = make_run()
    _publish(run, Recorder(created()), yes=True, interactive=False)
    line = unpublish(run, origin=None, transport=Recorder(Response(410, {}, b"")))
    assert line.startswith("Already gone")
    assert not (run / "seal.publish.json").exists()


@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_cli_unpublish_accepts_yes_like_publish(make_run, tmp_path, monkeypatch, extra):
    run = make_run()
    _publish(run, Recorder(created()), yes=True, interactive=False)
    transport = Recorder(Response(204, {}, b""))
    monkeypatch.setattr(seal_publish, "urllib_transport", transport)
    result = CliRunner().invoke(cli.main, ["seal", "unpublish", str(run), *extra])
    assert result.exit_code == 0, result.output
    assert "Unpublished." in result.output
    assert [request[0] for request in transport.requests] == ["DELETE"]
    assert not (run / "seal.publish.json").exists()


def test_a_tutorial_sibling_marks_practice_only_inside_a_quickstart_folder(tmp_path):
    from openadapt.seal_publish import tutorial_provenance

    receipt = json.dumps({"provenance": "synthetic-tutorial"})
    for parent in ("runs", "openadapt-quickstart-2"):
        (tmp_path / parent / "tutorial").mkdir(parents=True)
        (tmp_path / parent / "tutorial" / "receipt.json").write_text(receipt)
        (tmp_path / parent / "mine").mkdir()
    assert tutorial_provenance(tmp_path / "runs" / "mine") is None
    assert (
        tutorial_provenance(tmp_path / "openadapt-quickstart-2" / "mine")
        == "synthetic-tutorial"
    )


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


@needs_engine
def test_seal_verify_checks_a_hosted_link_here(make_run, tmp_path):
    run = make_run()
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


@needs_engine
def test_seal_verify_refuses_a_changed_card_whatever_the_server_says(
    make_run, tmp_path
):
    run = make_run()
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


@needs_engine
def test_cli_seal_verify_routes_a_link(make_run, tmp_path, monkeypatch):
    run = make_run()
    _publish(run, Recorder(), dry_run=True)
    monkeypatch.setattr(seal_publish, "urllib_transport", Recorder(_hosted(run)))
    result = CliRunner().invoke(
        cli.main, ["seal", "verify", f"https://app.openadapt.ai/seal#{SEAL_ID}"]
    )
    assert result.exit_code == 0, result.output
    assert "Signature: valid." in result.output


# -- quickstart -----------------------------------------------------------------


def _status(enabled) -> Response:
    return Response(200, {}, json.dumps({"enabled": enabled}).encode())


def test_hosted_seals_live_needs_exactly_enabled_true():
    live = seal_publish.hosted_seals_live
    transport = Recorder(_status(True))
    assert live(ORIGIN, transport=transport)
    assert transport.requests == [
        (
            "GET",
            f"{ORIGIN}/api/public/local-seals/status",
            None,
            {"Accept": "application/json"},
        )
    ]
    assert not live(ORIGIN, transport=Recorder(_status(False)))
    assert not live(ORIGIN, transport=Recorder(_status("true")))
    assert not live(ORIGIN, transport=Recorder(Response(404, {}, b"")))
    assert not live(ORIGIN, transport=Recorder(Response(200, {}, b"not json")))
    body = json.dumps({"enabled": True, "extra": 1}).encode()
    assert not live(ORIGIN, transport=Recorder(Response(200, {}, body)))

    def unreachable(*_args):
        raise OSError("no route")

    assert not live(ORIGIN, transport=unreachable)


def test_hosted_seals_live_gives_up_after_its_timeout():
    import threading
    import time

    release = threading.Event()

    def slow(*_args):
        release.wait(5)
        return _status(True)

    started = time.monotonic()
    try:
        assert not seal_publish.hosted_seals_live(ORIGIN, transport=slow, timeout_s=0.2)
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_hosted_seals_live_bounds_the_real_transport_to_its_timeout(monkeypatch):
    seen: list = []

    def real(method, url, body, headers, timeout):
        seen.append(timeout)
        return _status(True)

    monkeypatch.setattr(seal_publish, "urllib_transport", real)
    assert seal_publish.hosted_seals_live(ORIGIN)
    assert seen == [seal_publish.STATUS_TIMEOUT_S] == [1.0]


def _terminal(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_public_seals_available", lambda: True)


def test_the_quickstart_offer_exists_only_in_a_terminal_outside_ci(monkeypatch):
    # The status check runs only when the offer is called, never when it is
    # made, so building (or not building) the offer sends nothing.
    calls: list = []
    monkeypatch.setattr(
        seal_publish, "urllib_transport", lambda *args: calls.append(args)
    )
    _terminal(monkeypatch)
    assert cli._quickstart_publish_offer() is not None
    monkeypatch.setenv("CI", "1")
    assert cli._quickstart_publish_offer() is None
    monkeypatch.delenv("CI")
    monkeypatch.setattr(cli, "_public_seals_available", lambda: False)
    assert cli._quickstart_publish_offer() is None
    monkeypatch.setattr(cli, "_public_seals_available", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._quickstart_publish_offer() is None
    assert calls == []


@pytest.mark.parametrize(
    "status",
    [
        _status(False),
        Response(404, {}, b'{"error":"not_found"}'),
        Response(503, {}, b""),
        OSError("unreachable"),
    ],
)
def test_the_quickstart_offers_nothing_unless_cloud_is_live(
    make_run, tmp_path, monkeypatch, status
):
    run = make_run()
    _terminal(monkeypatch)

    seen: list = []

    def transport(method, url, body, headers, *_timeout):
        seen.append((method, url))
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(seal_publish, "urllib_transport", transport)
    offer = cli._quickstart_publish_offer()
    said: list[str] = []

    def ask(_question):
        raise AssertionError("asked while hosted Seals are not live")

    assert offer(run, said.append, ask) is False
    assert said == []
    assert not (run / "public-seal.json").exists()
    # One status check, to production's status route, and nothing else.
    assert seen == [
        ("GET", f"{seal_publish.DEFAULT_ENDPOINT}/api/public/local-seals/status")
    ]


def test_the_quickstart_offer_shows_the_card_then_asks_once(
    make_run, tmp_path, monkeypatch
):
    run = make_run()
    _terminal(monkeypatch)
    transport = Recorder(_status(True))
    monkeypatch.setattr(seal_publish, "urllib_transport", transport)
    offer = cli._quickstart_publish_offer()
    events: list[tuple[str, str]] = []
    offer(
        run,
        lambda line: events.append(("say", line)),
        lambda q: events.append(("ask", q)) or "",
    )
    asks = [i for i, (kind, _) in enumerate(events) if kind == "ask"]
    assert len(asks) == 1 and events[asks[0]][1].endswith("Publish? [y/N] ")
    # The exact card that would be uploaded is printed before the question.
    card = json.loads((run / "public-seal.json").read_text())["card"]
    shown = [
        json.loads(line) for kind, line in events[: asks[0]] if line.startswith("{")
    ]
    assert shown and shown[0]["card"] == card
    assert card["provenance"] == "synthetic-tutorial"
    assert events[-1] == ("say", "Not published.")
    assert not (run / "seal.publish.json").exists()
    # Only the status check was sent; a No uploads nothing.
    assert [r[0] for r in transport.requests] == ["GET"]


@needs_engine
def test_the_tutorial_card_carries_the_program_name(make_run, tmp_path, monkeypatch):
    run = make_run()
    _terminal(monkeypatch)
    monkeypatch.setattr(seal_publish, "urllib_transport", Recorder(_status(True)))
    cli._quickstart_publish_offer()(run, lambda _line: None, lambda _q: "")
    card = json.loads((run / "public-seal.json").read_text())["card"]
    assert card["program_name"] == "local-quickstart"


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
        return True

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


def test_without_an_offer_the_finale_says_nothing_about_publishing(
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
    text = stream.getvalue()
    assert "seal publish" not in text and "online" not in text.lower()


def _rich_finale(tmp_path, offer) -> str:
    from test_quickstart import FakeEngine, _clock, _Terminal, _visible

    from openadapt.quickstart import next_steps
    from openadapt.quickstart.story import run_story
    from openadapt.quickstart.view import RichView

    stream = _Terminal()
    run_story(
        FakeEngine(tmp_path / "openadapt-quickstart"),
        RichView(stream, environ={}, width=110, animate=False),
        next_steps=next_steps(cli._hint, cli._shell_quote, True, offer_publish=offer),
        clock=_clock(),
    )
    return _visible(stream.getvalue())


def test_the_terminal_finale_offers_once_between_the_seal_and_next(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    calls: list[Path] = []

    def offer(run_dir, say, ask):
        calls.append(run_dir)
        say("offer shown")
        return True

    text = _rich_finale(tmp_path, offer)
    assert calls == [tmp_path / "openadapt-quickstart" / "run-1"]
    assert text.index("Your Seal") < text.index("offer shown") < text.index("Next  ")
    assert "Put it online" not in text


def test_the_terminal_finale_without_an_offer_has_no_publish_line(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    for offer in (None, lambda *_args: False):
        text = _rich_finale(tmp_path, offer)
        assert "Put it online" not in text and "seal publish" not in text


def test_the_upload_bytes_match_the_engines(make_run):
    doc = _golden_doc()
    if ENGINE:
        from openadapt_flow.public_seal import public_seal_bytes

        assert seal_publish.public_bytes(doc) == public_seal_bytes(doc)
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    body = next(c["body"] for c in vectors["cases"] if c["name"] == "valid")
    assert seal_publish.public_bytes(doc) == body.encode()


def test_the_real_transport_refuses_redirects():
    import urllib.request

    handler = seal_publish._NoRedirect()
    request = urllib.request.Request("https://a.test/x", data=b"{}", method="POST")
    assert (
        handler.redirect_request(request, None, 307, "", {}, "https://b.test") is None
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
