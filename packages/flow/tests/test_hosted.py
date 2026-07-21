"""Unit tests for the hosted-connectivity wrapper (login / push / break-emit).

Network is fully mocked (monkeypatched ``httpx.get`` / ``httpx.post`` returning
``httpx.Response`` objects, mirroring ``tests/test_remote_vlm.py``). No real
credentials, no real host, no real recording — every artifact is a tmp fixture.
The engine internals (compiler / IR / replay) are untouched; only the new
``openadapt_flow.hosted`` module + its CLI wiring are exercised.
"""

from __future__ import annotations

import base64
import json
import stat
import threading
import zipfile
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from openadapt_flow import hosted, privacy
from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.ir import HaltObservation, Resolution, RunReport, StepResult


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Root config.toml under a tmp dir and clear the token env for every test."""
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(hosted.TOKEN_ENV, raising=False)
    monkeypatch.setenv(hosted.DESTINATION_KIND_ENV, "customer-managed")
    monkeypatch.setenv(hosted.TRUSTED_HOSTS_ENV, "https://h.test")
    monkeypatch.setenv(hosted.AUTO_APPROVE_ENV, "true")
    monkeypatch.setenv("OPENADAPT_SANITIZATION_POLICY_KEY_ID", "test-policy-key")
    monkeypatch.setenv(
        "OPENADAPT_SANITIZATION_POLICY_KEY",
        base64.b64encode(b"test-policy-secret-material-32-bytes!!").decode(),
    )
    monkeypatch.setattr(hosted, "_keyring_token", lambda host: None)
    monkeypatch.setattr(hosted, "_store_keyring_token", lambda host, token: False)
    monkeypatch.setattr(hosted, "_snapshot_keyring_token", lambda host: (True, None))
    monkeypatch.setattr(
        hosted,
        "_store_staged_keyring_token",
        lambda host, pairing_id, token: False,
    )
    monkeypatch.setattr(
        hosted,
        "_delete_staged_keyring_token",
        lambda host, pairing_id: True,
    )
    yield
    privacy.reset_scrubbers()


# ---------------------------------------------------------------------------
# host + token resolution + config.toml
# ---------------------------------------------------------------------------


def test_resolve_host_precedence(monkeypatch):
    assert hosted.resolve_host() == hosted.DEFAULT_HOST
    assert hosted.resolve_host("https://example.test/") == "https://example.test"
    hosted._update_hosted_config({"host": "https://stored.test"})
    assert hosted.resolve_host() == "https://stored.test"
    # explicit arg still wins over stored config
    assert hosted.resolve_host("https://arg.test") == "https://arg.test"


def test_resolve_host_canonicalizes_the_policy_and_request_origin():
    assert hosted.resolve_host("HTTPS://B\u00dcCHER.example:443/") == (
        "https://xn--bcher-kva.example"
    )
    assert hosted.resolve_host("http://LOCALHOST:80") == "http://localhost"
    assert hosted.resolve_host("http://[0:0:0:0:0:0:0:1]:8080") == ("http://[::1]:8080")


@pytest.mark.parametrize(
    "host",
    [
        "ftp://localhost",
        "https://user:password@example.test",
        "https://example.test/api",
        "https://example.test?next=evil",
        "https://example.test.",
        "https://example.test:99999",
    ],
)
def test_resolve_host_refuses_ambiguous_or_unsafe_origins(host):
    with pytest.raises(hosted.HostedError):
        hosted.resolve_host(host)


def test_resolve_token_precedence(monkeypatch):
    with pytest.raises(hosted.HostedError):
        hosted.resolve_token()
    hosted._update_hosted_config({"token": "from_config"})
    assert hosted.resolve_token() == "from_config"
    monkeypatch.setenv(hosted.TOKEN_ENV, "from_env")
    assert hosted.resolve_token() == "from_env"
    assert hosted.resolve_token("from_arg") == "from_arg"


def test_resolve_token_prefers_keyring_over_legacy_config(monkeypatch):
    hosted._update_hosted_config({"token": "legacy"})
    monkeypatch.setattr(hosted, "_keyring_token", lambda host: "from_keyring")
    assert hosted.resolve_token(host="https://h.test") == "from_keyring"


def test_config_toml_roundtrip_and_perms():
    path = hosted._update_hosted_config({"host": "https://h.test", "token": "tok"})
    assert path.is_file()
    section = hosted._hosted_config()
    assert section["host"] == "https://h.test"
    assert section["token"] == "tok"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    # a second update merges, not clobbers
    hosted._update_hosted_config({"deployment_lane": "byoc"})
    section = hosted._hosted_config()
    assert section["host"] == "https://h.test"
    assert section["deployment_lane"] == "byoc"


def test_load_toml_minimal_fallback(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[hosted]\nhost = "https://x.test"\nphi = true\npoll = 60\n')
    data = hosted._load_toml_minimal(f)
    assert data["hosted"] == {"host": "https://x.test", "phi": True, "poll": 60}


# ---------------------------------------------------------------------------
# recording discovery + zipping
# ---------------------------------------------------------------------------


def _make_recording(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "events.jsonl").write_text("{}\n")
    return d


def test_find_latest_recording_picks_newest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_recording(tmp_path, "rec_old")
    new = _make_recording(tmp_path, "rec_new")
    import os
    import time

    os.utime(old, (1, 1))
    os.utime(new, (time.time(), time.time()))
    assert hosted.find_latest_recording() == new


def test_find_latest_recording_none(tmp_path):
    with pytest.raises(hosted.HostedError):
        hosted.find_latest_recording(tmp_path)


def test_zip_dir_contents_at_root(tmp_path):
    rec = _make_recording(tmp_path, "rec")
    zip_path = hosted._zip_dir(rec)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert "meta.json" in names
        assert "events.jsonl" in names
    finally:
        import shutil

        shutil.rmtree(zip_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_success_saves_config(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    result = hosted.login(
        token="tok", host="https://h.test", allow_plaintext_token=True
    )
    assert result["valid"] is True
    assert result["host"] == "https://h.test"
    assert hosted._hosted_config()["token"] == "tok"


def test_login_sends_token_only_to_canonical_policy_checked_origin(monkeypatch):
    captured: dict = {}

    def get(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return httpx.Response(200, json={"count": 0})

    monkeypatch.setattr(httpx, "get", get)
    result = hosted.login(
        token="tok",
        host="HTTPS://H.TEST:443/",
        save=False,
        destination_kind="customer-managed",
        trusted_hosts=["https://h.test"],
    )

    assert result["host"] == "https://h.test"
    assert captured["url"] == "https://h.test/api/needs-attention/count"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer tok"
    assert captured["kwargs"]["follow_redirects"] is False


def test_login_no_save(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    result = hosted.login(token="tok", host="https://h.test", save=False)
    assert result["config_path"] is None
    assert "token" not in hosted._hosted_config()


def test_login_requires_explicit_plaintext_fallback(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    with pytest.raises(hosted.HostedError, match="allow-plaintext-token"):
        hosted.login(token="tok", host="https://h.test")
    assert "token" not in hosted._hosted_config()


def test_login_prefers_keyring(monkeypatch):
    stored: dict = {}
    hosted._update_hosted_config({"token": "legacy-plaintext"})
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    monkeypatch.setattr(
        hosted,
        "_store_keyring_token",
        lambda host, token: stored.update(host=host, token=token) or True,
    )
    result = hosted.login(token="tok", host="https://h.test")
    assert result["token_storage"] == "keyring"
    assert stored == {"host": "https://h.test", "token": "tok"}
    assert "token" not in hosted._hosted_config()


def test_login_rejected_token(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(401))
    with pytest.raises(hosted.HostedError, match="401"):
        hosted.login(token="bad", host="https://h.test")


def test_login_network_error(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(hosted.HostedError, match="reach"):
        hosted.login(token="tok", host="https://h.test")


# ---------------------------------------------------------------------------
# one-click browser pairing
# ---------------------------------------------------------------------------


PAIRING = "oap_" + "A" * 43
PAIRED_TOKEN = "oai_ingest_" + "b" * 43
PRIOR_TOKEN = "oai_ingest_" + "c" * 43
PAIRING_ID = "550e8400-e29b-41d4-a716-446655440000"


def test_parse_connect_uri_accepts_only_the_fixed_action():
    request = hosted.parse_connect_uri(
        f"openadapt://connect?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai"
    )
    assert request == {
        "pairing": PAIRING,
        "host": "https://app.openadapt.ai",
    }

    for unsafe in [
        f"openadapt://run?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai",
        f"openadapt://connect/shell?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai",
        f"openadapt://connect?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai&command=rm",
        f"openadapt://connect?pairing={PAIRING}&pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai",
        f"openadapt://connect?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai#fragment",
    ]:
        with pytest.raises(hosted.HostedError):
            hosted.parse_connect_uri(unsafe)


def test_pairing_staging_account_is_pairing_specific_and_not_canonical():
    other_pairing_id = "123e4567-e89b-12d3-a456-426614174000"
    canonical = hosted.resolve_host("https://h.test")
    account = hosted._pairing_staging_account(canonical, PAIRING_ID)

    assert account != canonical
    assert account != hosted._pairing_staging_account(canonical, other_pairing_id)
    assert PAIRED_TOKEN not in account
    assert PAIRING not in account


def _install_connect_keyring(
    monkeypatch,
    *,
    prior=PRIOR_TOKEN,
    canonical_results=None,
    staging_result=True,
    delete_canonical_result=True,
    delete_staging_result=True,
):
    state = {
        "canonical": prior,
        "staging": None,
        "events": [],
    }
    results = list(canonical_results or [])
    monkeypatch.setattr(hosted, "_keyring_available", lambda: True)

    def snapshot(host):
        state["events"].append("snapshot")
        return True, state["canonical"]

    def stage(host, pairing_id, token):
        state["events"].append(("stage", pairing_id))
        if staging_result:
            state["staging"] = token
        return staging_result

    def store(host, token):
        state["events"].append(("canonical", token))
        stored = results.pop(0) if results else True
        if stored:
            state["canonical"] = token
        return stored

    def delete_canonical(host):
        state["events"].append("delete-canonical")
        if delete_canonical_result:
            state["canonical"] = None
        return delete_canonical_result

    def delete_staging(host, pairing_id):
        state["events"].append(("delete-stage", pairing_id))
        if delete_staging_result:
            state["staging"] = None
        return delete_staging_result

    monkeypatch.setattr(hosted, "_snapshot_keyring_token", snapshot)
    monkeypatch.setattr(hosted, "_store_staged_keyring_token", stage)
    monkeypatch.setattr(hosted, "_store_keyring_token", store)
    monkeypatch.setattr(hosted, "_delete_keyring_token", delete_canonical)
    monkeypatch.setattr(hosted, "_delete_staged_keyring_token", delete_staging)
    return state


def _install_connect_http(
    monkeypatch,
    *,
    validation=None,
    confirmations=None,
    abort=None,
):
    calls = {"post": [], "get": []}
    validation_result = (
        httpx.Response(200, json={"count": 0}) if validation is None else validation
    )
    confirmation_results = list(
        confirmations or [httpx.Response(200, json={"connected": True})]
    )
    abort_result = (
        httpx.Response(200, json={"revoked": True}) if abort is None else abort
    )

    def resolve(result):
        if isinstance(result, Exception):
            raise result
        return result

    def post(url, **kwargs):
        calls["post"].append((url, kwargs))
        if url.endswith("/claim"):
            return httpx.Response(
                201,
                json={
                    "paired": True,
                    "pairing_id": PAIRING_ID,
                    "ingest_token": PAIRED_TOKEN,
                },
            )
        if url.endswith("/confirm"):
            return resolve(confirmation_results.pop(0))
        if url.endswith("/abort"):
            return resolve(abort_result)
        raise AssertionError(f"unexpected POST {url}")

    def get(url, **kwargs):
        calls["get"].append((url, kwargs))
        return resolve(validation_result)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return calls


def _connect_test():
    return hosted.connect(
        PAIRING,
        host="https://h.test",
        destination_kind="customer-managed",
        trusted_hosts=["https://h.test"],
    )


def test_connect_claims_once_validates_and_saves_only_to_keyring(monkeypatch):
    calls: dict = {"post": []}
    events: list[str] = []
    monkeypatch.setattr(hosted, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        hosted,
        "_snapshot_keyring_token",
        lambda host: events.append("snapshot") or (True, PRIOR_TOKEN),
    )
    monkeypatch.setattr(
        hosted,
        "_store_staged_keyring_token",
        lambda host, pairing_id, token: events.append("stage") or True,
    )
    monkeypatch.setattr(
        hosted,
        "_store_keyring_token",
        lambda host, token: events.append("canonical") or True,
    )
    monkeypatch.setattr(
        hosted,
        "_delete_staged_keyring_token",
        lambda host, pairing_id: events.append("delete-stage") or True,
    )

    def post(url, **kwargs):
        calls["post"].append((url, kwargs))
        events.append(url.rsplit("/", 1)[-1])
        if url.endswith("/confirm"):
            return httpx.Response(200, json={"connected": True})
        return httpx.Response(
            201,
            json={
                "paired": True,
                "pairing_id": PAIRING_ID,
                "org_id": "org",
                "user_id": "user",
                "ingest_token": PAIRED_TOKEN,
            },
        )

    def get(url, **kwargs):
        calls["get"] = (url, kwargs)
        events.append("validate")
        return httpx.Response(200, json={"count": 0})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    result = hosted.connect(
        PAIRING,
        host="https://h.test",
        device_name="Reception PC",
        destination_kind="customer-managed",
        trusted_hosts=["https://h.test"],
    )

    assert result["paired"] is True
    assert result["token_storage"] == "keyring"
    assert events == [
        "snapshot",
        "claim",
        "stage",
        "validate",
        "canonical",
        "confirm",
        "delete-stage",
    ]
    assert calls["post"][0][0] == "https://h.test/api/local-bridge/pairings/claim"
    assert calls["post"][0][1]["json"] == {
        "pairing_secret": PAIRING,
        "device_name": "Reception PC",
    }
    assert calls["post"][0][1]["follow_redirects"] is False
    assert calls["get"][1]["headers"]["Authorization"] == f"Bearer {PAIRED_TOKEN}"
    assert calls["post"][1][0] == "https://h.test/api/local-bridge/pairings/confirm"
    assert calls["post"][1][1]["json"] == {"pairing_id": PAIRING_ID}
    assert calls["post"][1][1]["headers"]["Authorization"] == f"Bearer {PAIRED_TOKEN}"
    assert "token" not in hosted._hosted_config()
    assert hosted._hosted_config()["host"] == "https://h.test"


def test_connect_refuses_before_claim_without_a_keychain(monkeypatch):
    monkeypatch.setattr(hosted, "_keyring_available", lambda: False)
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: pytest.fail("must not consume pairing")
    )
    with pytest.raises(hosted.HostedError, match="OS keychain"):
        hosted.connect(
            PAIRING,
            host="https://h.test",
            destination_kind="customer-managed",
            trusted_hosts=["https://h.test"],
        )


def test_connect_never_reports_expired_or_unverified_pairing_as_success(monkeypatch):
    monkeypatch.setattr(hosted, "_keyring_available", lambda: True)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(410))
    with pytest.raises(hosted.HostedError, match="expired"):
        hosted.connect(
            PAIRING,
            host="https://h.test",
            destination_kind="customer-managed",
            trusted_hosts=["https://h.test"],
        )


def test_connect_refuses_before_claim_when_canonical_snapshot_fails(monkeypatch):
    monkeypatch.setattr(hosted, "_keyring_available", lambda: True)
    monkeypatch.setattr(hosted, "_snapshot_keyring_token", lambda host: (False, None))
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: pytest.fail("must not consume pairing")
    )
    with pytest.raises(hosted.HostedError, match="could not read"):
        _connect_test()


def test_connect_aborts_and_restores_prior_when_canonical_set_fails(monkeypatch):
    state = _install_connect_keyring(
        monkeypatch,
        canonical_results=[False, True],
    )
    calls = _install_connect_http(monkeypatch)

    with pytest.raises(hosted.HostedError, match="refused to promote"):
        _connect_test()

    assert state["canonical"] == PRIOR_TOKEN
    assert state["staging"] is None
    abort_url, abort_kwargs = calls["post"][-1]
    assert abort_url.endswith("/pairings/abort")
    assert abort_kwargs["json"] == {"pairing_id": PAIRING_ID}
    assert abort_kwargs["headers"]["Authorization"] == f"Bearer {PAIRED_TOKEN}"


@pytest.mark.parametrize(
    "validation",
    [
        httpx.Response(401),
        httpx.Response(503),
        httpx.ConnectError("validation unavailable"),
    ],
    ids=["401", "5xx", "transport"],
)
def test_connect_validation_failure_preserves_prior_and_aborts(monkeypatch, validation):
    state = _install_connect_keyring(monkeypatch)
    calls = _install_connect_http(monkeypatch, validation=validation)

    with pytest.raises(hosted.HostedError) as error:
        _connect_test()

    assert state["canonical"] == PRIOR_TOKEN
    assert state["staging"] is None
    assert calls["post"][-1][0].endswith("/pairings/abort")
    assert PAIRED_TOKEN not in str(error.value)
    assert PAIRING not in str(error.value)


@pytest.mark.parametrize(
    "confirmations",
    [
        [httpx.Response(200, json={"connected": False})],
        [httpx.Response(409, json={"connected": False})],
        [
            httpx.Response(503, json={"connected": False}),
            httpx.Response(503, json={"connected": False}),
        ],
        [
            httpx.ConnectError("confirmation unavailable"),
            httpx.ConnectError("confirmation unavailable"),
        ],
    ],
    ids=["false", "409", "5xx", "transport"],
)
def test_connect_confirmation_failure_aborts_before_restoring_prior(
    monkeypatch, confirmations
):
    state = _install_connect_keyring(monkeypatch)
    calls = _install_connect_http(monkeypatch, confirmations=confirmations)

    with pytest.raises(hosted.HostedError, match="did not definitively confirm"):
        _connect_test()

    assert state["canonical"] == PRIOR_TOKEN
    assert state["staging"] is None
    assert calls["post"][-1][0].endswith("/pairings/abort")


@pytest.mark.parametrize(
    "first_confirmation",
    [
        httpx.Response(503, json={"connected": False}),
        httpx.ConnectError("confirmation unavailable"),
    ],
    ids=["5xx", "transport"],
)
def test_connect_idempotently_retries_ambiguous_confirmation(
    monkeypatch, first_confirmation
):
    state = _install_connect_keyring(monkeypatch)
    calls = _install_connect_http(
        monkeypatch,
        confirmations=[
            first_confirmation,
            httpx.Response(200, json={"connected": True}),
        ],
    )

    result = _connect_test()

    assert result["paired"] is True
    assert state["canonical"] == PAIRED_TOKEN
    assert state["staging"] is None
    confirm_calls = [
        call for call in calls["post"] if call[0].endswith("/pairings/confirm")
    ]
    assert len(confirm_calls) == 2
    assert not any(call[0].endswith("/pairings/abort") for call in calls["post"])


def test_connect_abort_failure_retains_recovery_without_clobbering_prior(monkeypatch):
    state = _install_connect_keyring(monkeypatch)
    _install_connect_http(
        monkeypatch,
        validation=httpx.Response(503),
        abort=httpx.ConnectError("abort unavailable"),
    )

    with pytest.raises(hosted.HostedError, match="recovery copy was retained") as error:
        _connect_test()

    assert state["canonical"] == PRIOR_TOKEN
    assert state["staging"] == PAIRED_TOKEN
    assert PAIRED_TOKEN not in str(error.value)
    assert PAIRING not in str(error.value)


def test_connect_abort_conflict_preserves_new_recoverable_state(monkeypatch):
    state = _install_connect_keyring(monkeypatch)
    _install_connect_http(
        monkeypatch,
        confirmations=[
            httpx.ConnectError("confirmation unavailable"),
            httpx.ConnectError("confirmation unavailable"),
        ],
        abort=httpx.Response(409),
    )

    with pytest.raises(hosted.HostedError, match="Retry confirmation"):
        _connect_test()

    assert state["canonical"] == PAIRED_TOKEN
    assert state["staging"] == PAIRED_TOKEN


def test_connect_restoration_failure_retains_staging_recovery(monkeypatch):
    state = _install_connect_keyring(
        monkeypatch,
        canonical_results=[True, False],
    )
    _install_connect_http(
        monkeypatch,
        confirmations=[httpx.Response(200, json={"connected": False})],
    )

    with pytest.raises(hosted.HostedError, match="could not be restored"):
        _connect_test()

    assert state["canonical"] == PAIRED_TOKEN
    assert state["staging"] == PAIRED_TOKEN


def test_connect_with_no_prior_token_cleans_canonical_only_after_abort(monkeypatch):
    state = _install_connect_keyring(monkeypatch, prior=None)
    calls = _install_connect_http(
        monkeypatch,
        confirmations=[httpx.Response(200, json={"connected": False})],
    )

    with pytest.raises(hosted.HostedError):
        _connect_test()

    assert calls["post"][-1][0].endswith("/pairings/abort")
    assert state["canonical"] is None
    assert state["staging"] is None
    assert "delete-canonical" in state["events"]


def test_connect_staging_failure_aborts_without_touching_prior(monkeypatch):
    state = _install_connect_keyring(monkeypatch, staging_result=False)
    calls = _install_connect_http(monkeypatch)

    with pytest.raises(hosted.HostedError, match="staging copy"):
        _connect_test()

    assert state["canonical"] == PRIOR_TOKEN
    assert state["staging"] is None
    assert calls["post"][-1][0].endswith("/pairings/abort")


def test_connect_cli_failure_never_emits_pairing_or_ingest_secrets(monkeypatch, capsys):
    _install_connect_keyring(monkeypatch)
    _install_connect_http(
        monkeypatch,
        validation=httpx.Response(503),
        abort=httpx.ConnectError("abort unavailable"),
    )

    assert (
        main(
            [
                "connect",
                "--pairing",
                PAIRING,
                "--host",
                "https://h.test",
                "--destination-kind",
                "customer-managed",
                "--trusted-host",
                "https://h.test",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "connect failed:" in output
    assert PAIRING not in output
    assert PAIRED_TOKEN not in output


def test_connect_cli_supports_pairing_and_strict_uri(monkeypatch, capsys):
    captured: dict = {}

    def fake_connect(pairing_secret, **kwargs):
        captured.update(pairing=pairing_secret, **kwargs)
        return {
            "host": kwargs["host"],
            "device_name": "Laptop",
            "settings_url": f"{kwargs['host']}/dashboard/settings/ingest",
        }

    monkeypatch.setattr(hosted, "connect", fake_connect)
    assert (
        main(
            [
                "connect",
                "--pairing",
                PAIRING,
                "--host",
                "https://h.test",
                "--destination-kind",
                "customer-managed",
                "--trusted-host",
                "https://h.test",
            ]
        )
        == 0
    )
    assert captured["pairing"] == PAIRING
    assert "credential saved in the OS keychain" in capsys.readouterr().out

    captured.clear()
    uri = f"openadapt://connect?pairing={PAIRING}&host=https%3A%2F%2Fapp.openadapt.ai"
    assert main(["connect", "--uri", uri]) == 0
    assert captured["host"] == "https://app.openadapt.ai"


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def _capture_post(recorder, status=201, json_body=None):
    def fake(url, **kw):
        recorder["url"] = url
        recorder["kw"] = kw
        return httpx.Response(status, json=json_body or {})

    return fake


class _FakeScrubber:
    """A fast text+image scrubber double (satisfies privacy.Scrubber).

    Text scrubbing replaces the fixture PHI tokens; image scrubbing is identity
    (the redaction geometry is Presidio's job, out of scope for these unit
    tests — here we only assert the scrub PATH is taken)."""

    def __init__(self):
        self.text_calls = 0
        self.image_calls = 0

    def scrub_text(self, text, is_separated=False):
        self.text_calls += 1
        return text.replace("Jane Doe", "<PERSON>").replace("12345", "<NUM>")

    def scrub_image(self, image, fill_color=None):
        self.image_calls += 1
        return image


def test_push_success(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())  # cloud recording => scrub before upload
    recorder: dict = {}
    body = {
        "ingest": {
            "workflow_id": "wf_123",
            "workflow_name": "Pushed recording",
            "kind": "recording",
            "compile": {"status": "compiled", "steps": 4},
        }
    }
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 201, body))
    result = hosted.push(rec, name="My flow", host="https://h.test", token="tok")
    assert result["workflow_id"] == "wf_123"
    assert result["dashboard_url"] == "https://h.test/dashboard/workflows/wf_123"
    assert recorder["url"] == "https://h.test/api/ingest"
    assert recorder["kw"]["data"]["kind"] == "recording"
    assert recorder["kw"]["data"]["name"] == "My flow"
    assert "workflow_id" not in recorder["kw"]["data"]
    manifest = json.loads(recorder["kw"]["data"]["sanitization_manifest"])
    assert manifest["schema"] == "openadapt.sanitization/v1"
    assert manifest["approval"]["status"] == "approved"
    assert recorder["kw"]["files"]["file"][0].startswith("openadapt-sanitized-")
    assert recorder["kw"]["headers"]["Authorization"] == "Bearer tok"
    assert "file" in recorder["kw"]["files"]


def test_push_default_path_uses_latest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())
    recorder: dict = {}
    monkeypatch.setattr(
        httpx,
        "post",
        _capture_post(recorder, 201, {"ingest": {"workflow_id": "wf_9"}}),
    )
    result = hosted.push(host="https://h.test", token="tok")
    assert result["workflow_id"] == "wf_9"


def test_push_bad_kind(tmp_path):
    with pytest.raises(hosted.HostedError, match="kind"):
        hosted.push(tmp_path, kind="nonsense", token="tok", host="https://h.test")


@pytest.mark.parametrize(
    ("kind", "workflow_id", "message"),
    [
        ("recording", "ec726a3e-dcaf-40cf-870a-867d104002dd", "bundle"),
        ("bundle", "not-a-uuid", "valid UUID"),
    ],
)
def test_push_refuses_invalid_existing_workflow_binding(
    tmp_path, monkeypatch, kind, workflow_id, message
):
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: pytest.fail("must not upload")
    )
    with pytest.raises(hosted.HostedError, match=message):
        hosted.push(
            tmp_path,
            kind=kind,
            workflow_id=workflow_id,
            token="tok",
            host="https://h.test",
        )


@pytest.mark.parametrize(
    ("kind", "workflow_id", "run_id", "message"),
    [
        ("recording", None, "d3ecf64d-0d25-4df7-9264-77bf7d266d77", "requires"),
        ("bundle", None, "d3ecf64d-0d25-4df7-9264-77bf7d266d77", "requires"),
        (
            "bundle",
            "ec726a3e-dcaf-40cf-870a-867d104002dd",
            "not-a-uuid",
            "valid UUID",
        ),
    ],
)
def test_push_refuses_invalid_halt_resolution_binding(
    tmp_path, monkeypatch, kind, workflow_id, run_id, message
):
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: pytest.fail("must not upload")
    )
    with pytest.raises(hosted.HostedError, match=message):
        hosted.push(
            tmp_path,
            kind=kind,
            workflow_id=workflow_id,
            resolves_run_id=run_id,
            token="tok",
            host="https://h.test",
        )


def test_push_non_201(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(502, text="store down")
    )
    with pytest.raises(hosted.HostedError, match="502"):
        hosted.push(rec, token="tok", host="https://h.test")


def test_push_401(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(401))
    with pytest.raises(hosted.HostedError, match="401"):
        hosted.push(rec, token="tok", host="https://h.test")


def test_push_bundle_requires_verified_sanitization_not_attestation(
    tmp_path, monkeypatch
):
    """A declaration cannot replace a verified, approved derivative."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text("{}")
    privacy.set_text_scrubber(None)  # no capability
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload by default")
    )
    with pytest.raises(hosted.HostedError, match="no longer bypasses sanitization"):
        hosted.push(
            bundle,
            kind="bundle",
            deployment_kind="cloud",
            host="https://h.test",
            token="tok",
            attest_non_phi=True,
        )


def test_push_attested_non_phi_bundle_on_cloud_is_refused(tmp_path, monkeypatch):
    """Synthetic assertions still pass through the sanitizer contract."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text("{}")
    privacy.set_text_scrubber(None)
    recorder: dict = {}
    monkeypatch.setattr(
        httpx, "post", _capture_post(recorder, 201, {"ingest": {"workflow_id": "wf_b"}})
    )
    with pytest.raises(hosted.HostedError, match="no longer bypasses sanitization"):
        hosted.push(
            bundle,
            kind="bundle",
            deployment_kind="cloud",
            host="https://h.test",
            token="tok",
            attest_non_phi=True,
        )


def test_push_attestation_cannot_bypass_regulated_boundary(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text("{}")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload on regulated")
    )
    with pytest.raises(hosted.HostedError, match="no longer bypasses sanitization"):
        hosted.push(
            bundle,
            kind="bundle",
            deployment_kind="regulated",
            host="https://h.test",
            token="tok",
            attest_non_phi=True,
        )


def test_push_attestation_cannot_bypass_phi_mode(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text("{}")
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload in PHI mode")
    )
    with pytest.raises(hosted.HostedError, match="no longer bypasses sanitization"):
        hosted.push(
            bundle,
            kind="bundle",
            deployment_kind="cloud",
            host="https://h.test",
            token="tok",
            attest_non_phi=True,
        )


def test_push_refuses_unknown_lane_from_configuration(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))
    with pytest.raises(hosted.HostedError, match="Unknown deployment lane"):
        hosted.push(
            rec,
            deployment_kind="production",
            host="https://h.test",
            token="tok",
        )


def test_push_rejects_non_phi_attestation_for_recording(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))
    with pytest.raises(hosted.HostedError, match="no longer bypasses sanitization"):
        hosted.push(
            rec,
            deployment_kind="cloud",
            host="https://h.test",
            token="tok",
            attest_non_phi=True,
        )


def test_push_recording_mislabeled_as_bundle_refused_on_regulated(
    tmp_path, monkeypatch
):
    """Fail closed: a raw recording dir mislabeled ``--kind bundle`` (no
    workflow.json[.enc]) must NOT skip the PHI gate. On a regulated lane it is
    treated as a recording and REFUSED, never egressed."""
    rec = _make_recording(tmp_path, "rec")  # meta.json + events.jsonl, NOT a bundle
    privacy.set_text_scrubber(_FakeScrubber())  # even WITH a scrubber, refuse
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload on regulated")
    )
    with pytest.raises(hosted.HostedError, match="not a compiled bundle"):
        hosted.push(
            rec,
            kind="bundle",
            deployment_kind="regulated",
            host="https://h.test",
            token="tok",
        )


def test_push_recording_mislabeled_as_bundle_is_refused(tmp_path, monkeypatch):
    """Artifact kind must match the reviewed source; it is never inferred on wire."""
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())
    recorder: dict = {}
    monkeypatch.setattr(
        httpx, "post", _capture_post(recorder, 201, {"ingest": {"workflow_id": "wf_r"}})
    )
    with pytest.raises(hosted.HostedError, match="not a compiled bundle"):
        hosted.push(
            rec,
            kind="bundle",
            deployment_kind="cloud",
            host="https://h.test",
            token="tok",
        )


def test_push_recording_on_byoc_is_sanitized_before_upload(tmp_path, monkeypatch):
    """BYOC describes execution; destination trust + sanitization govern upload."""
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(_FakeScrubber())  # even WITH a scrubber, refuse
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(201, json={"ingest": {"workflow_id": "wf"}}),
    )
    result = hosted.push(
        rec, deployment_kind="byoc", host="https://h.test", token="tok"
    )
    assert result["uploaded"] is True


def test_push_recording_under_phi_mode_is_sanitized_before_upload(
    tmp_path, monkeypatch
):
    """PHI mode never permits raw egress but does permit an approved derivative."""
    rec = _make_recording(tmp_path, "rec")
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    privacy.set_text_scrubber(_FakeScrubber())
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(201, json={"ingest": {"workflow_id": "wf"}}),
    )
    result = hosted.push(
        rec, deployment_kind="cloud", host="https://h.test", token="tok"
    )
    assert result["uploaded"] is True


def test_push_recording_refused_when_scrubber_unavailable(tmp_path, monkeypatch):
    """Cloud lane, but no scrubber to de-identify frames/values => refuse
    (never ship raw PHI)."""
    rec = _make_recording(tmp_path, "rec")
    privacy.set_text_scrubber(None)  # capability absent
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload unscrubbed")
    )
    with pytest.raises(hosted.HostedError, match="No PHI scrubber"):
        hosted.push(rec, deployment_kind="cloud", host="https://h.test", token="tok")


def test_push_recording_scrubs_before_upload(tmp_path, monkeypatch):
    """Cloud lane with a scrubber: the recording's text artifacts + frames are
    de-identified on a temp copy BEFORE the upload (the original is untouched)."""
    rec = _make_recording(tmp_path, "rec")
    # PHI in an artifact + a frame that must be image-scrubbed.
    (rec / "meta.json").write_text('{"params": {"patient": "Jane Doe"}}')
    frames = rec / "frames"
    frames.mkdir()
    from PIL import Image

    buf = __import__("io").BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
    (frames / "0000_before.png").write_bytes(buf.getvalue())

    fake = _FakeScrubber()
    privacy.set_text_scrubber(fake)

    captured: dict = {}

    def fake_post(url, **kw):
        # Read the uploaded zip bytes so we can assert what actually shipped.
        captured["bytes"] = kw["files"]["file"][1].read()
        return httpx.Response(201, json={"ingest": {"workflow_id": "wf_s"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = hosted.push(
        rec, deployment_kind="cloud", host="https://h.test", token="tok"
    )
    assert result["workflow_id"] == "wf_s"
    # scrub path ran: text + image scrubbers were both invoked.
    assert fake.text_calls >= 1
    assert fake.image_calls == 3  # transform, stable second pass, approval rescan
    # the uploaded zip carries the SCRUBBED artifact, not raw PHI.
    import io as _io

    with zipfile.ZipFile(_io.BytesIO(captured["bytes"])) as zf:
        meta = zf.read("meta.json").decode()
    assert "Jane Doe" not in meta
    assert "<PERSON>" in meta
    # original on disk is untouched.
    assert "Jane Doe" in (rec / "meta.json").read_text()


def test_push_recording_refuses_unsupported_binary_artifact(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    (rec / "recording.db").write_bytes(b"SQLite format 3\x00Jane Doe")
    privacy.set_text_scrubber(_FakeScrubber())
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("must not upload raw database")
    )
    with pytest.raises(hosted.HostedError, match="recording.db"):
        hosted.push(rec, host="https://h.test", token="tok")


def test_push_refuses_symlinked_artifact(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    outside = tmp_path / "outside.txt"
    outside.write_text("Jane Doe")
    try:
        (rec / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    privacy.set_text_scrubber(_FakeScrubber())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not upload"))
    with pytest.raises(hosted.HostedError, match="symlink"):
        hosted.push(rec, host="https://h.test", token="tok")


# ---------------------------------------------------------------------------
# report_break
# ---------------------------------------------------------------------------

_WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
_RUN_UUID = "22222222-2222-4222-8222-222222222222"
_HALT_UUID = "33333333-3333-4333-8333-333333333333"


def _run_response(**overrides) -> dict:
    body = {
        "run_id": _RUN_UUID,
        "status": "success",
        "provenance": "locally_reported",
    }
    body.update(overrides)
    return body


def _break_response(status: str = "halt", **overrides) -> dict:
    body = {
        "run_id": _RUN_UUID,
        "halt_id": _HALT_UUID,
        "status": status,
        "provenance": "locally_reported",
        "teach_url": f"/dashboard/runs/{_RUN_UUID}/teach",
    }
    body.update(overrides)
    return body


def _halted_run(run_dir: Path) -> Path:
    report = RunReport(
        workflow_name="triage",
        started_at="2026-01-01T00:00:00Z",
        bundle_content_digest="ab" * 32,
        success=False,
        total_ms=2500.0,
        results=[
            StepResult(
                step_id="s1",
                intent="click Save for Jane Doe",
                ok=False,
                error="element not found for MRN 12345",
                resolution=Resolution(
                    rung="ocr", point=(0, 0), confidence=0.5, elapsed_ms=1.0
                ),
            )
        ],
        halt=HaltObservation(
            state_id="st1",
            intent="click Save for Jane Doe",
            reason="unexpected dialog blocking MRN 12345",
            observed_texts=["Jane Doe"],
        ),
    )
    report.save(run_dir)
    return run_dir / "report.json"


def test_report_break_success(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    recorder: dict = {}
    body = _break_response(deployment_kind="byoc")
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, body))
    result = hosted.report_break(
        run_dir,
        workflow_id=_WORKFLOW_UUID,
        deployment_kind="byoc",
        org_id="org_1",
        host="https://h.test",
        token="tok",
    )
    assert result["emitted"] is True
    assert result["teach_url"] == (f"https://h.test/dashboard/runs/{_RUN_UUID}/teach")
    posted = recorder["kw"]["json"]
    assert posted["kind"] == "break_summary"
    assert posted["schema"] == hosted.BREAK_SUMMARY_SCHEMA
    assert posted["workflow_id"] == _WORKFLOW_UUID
    assert posted["bundle_content_digest"] == "ab" * 32
    assert "deployment_kind" not in posted
    assert "org_id" not in posted
    assert posted["status"] == "halt"
    assert posted["resolver_rung"] == "ocr"
    assert posted["metrics"] == {"steps": 1, "duration_s": 2.5}
    assert "report_path" not in posted
    assert "drift_signature" not in posted
    assert str(UUID(posted["client_run_id"])) == posted["client_run_id"]
    # no screenshots / dom / field values leak
    assert "screenshots" not in posted


def test_report_break_omits_optional_resolver_rung_when_unavailable(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    RunReport(
        workflow_name="triage",
        started_at="2026-01-01T00:00:00Z",
        bundle_content_digest="ab" * 32,
        success=False,
        total_ms=125.0,
        results=[StepResult(step_id="s1", intent="wait", ok=False)],
        halt=HaltObservation(state_id="st1"),
    ).save(run_dir)
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _break_response()))
    hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert "resolver_rung" not in recorder["kw"]["json"]


def test_report_break_scrubs_phi(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)

    class FakeScrubber:
        def scrub_text(self, text, is_separated=False):
            return text.replace("Jane Doe", "<PERSON>").replace("12345", "<NUM>")

    privacy.set_text_scrubber(FakeScrubber())
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _break_response()))
    hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    posted = recorder["kw"]["json"]
    assert "Jane Doe" not in json.dumps(posted)
    assert "12345" not in json.dumps(posted)
    assert posted["phi_minimal"] is True
    assert "step_intent" not in posted
    assert "reason" not in posted
    assert "error" not in posted


def test_report_break_422_falls_back_local(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(422, json={"error": "phi"})
    )
    result = hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is False
    assert result["local_only"] is True


def test_report_break_422_no_fallback_raises(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(422))
    with pytest.raises(hosted.HostedError, match="422"):
        hosted.report_break(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
            allow_local_fallback=False,
        )


def test_report_break_success_run_emits_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    RunReport(
        workflow_name="triage",
        started_at="2026-01-01T00:00:00Z",
        success=True,
    ).save(run_dir)
    # No httpx call should happen; make post explode if it does.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: pytest.fail("should not POST for a successful run"),
    )
    result = hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is False


def test_report_break_missing_report(tmp_path):
    with pytest.raises(hosted.HostedError, match="report.json"):
        hosted.report_break(
            tmp_path, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


def test_report_break_scrubber_unavailable_still_emits_minimal(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    privacy.reset_scrubbers()
    privacy.set_text_scrubber(None)

    def boom(*a, **k):
        raise privacy.PrivacyNotAvailable("missing")

    monkeypatch.setattr(privacy, "scrub_text", boom)
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _break_response()))
    result = hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is True
    assert recorder["kw"]["json"]["phi_minimal"] is True


def test_report_break_omits_free_text_when_scrub_unavailable(tmp_path, monkeypatch):
    """Default `auto` posture + NO scrubber: the break is still emitted, but as a
    PHI-free MINIMAL descriptor — raw free-text PHI must NOT be sent (Violation
    A: previously step_intent/reason/error went out unscrubbed under auto)."""
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    # auto mode (default), capability explicitly absent.
    privacy.set_text_scrubber(None)
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _break_response()))
    result = hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is True
    posted = recorder["kw"]["json"]
    blob = json.dumps(posted)
    # NO raw PHI leaks.
    assert "Jane Doe" not in blob
    assert "12345" not in blob
    # free-text fields are omitted; the minimal descriptor is still useful.
    assert "step_intent" not in posted
    assert "reason" not in posted
    assert "error" not in posted
    assert posted["phi_minimal"] is True
    assert posted["status"] == "halt"
    assert posted["resolver_rung"] == "ocr"
    assert "drift_signature" not in posted
    assert posted["metrics"] == {"steps": 1, "duration_s": 2.5}


# ---------------------------------------------------------------------------
# report_run (SUCCESS rail)
# ---------------------------------------------------------------------------


def _successful_run(run_dir: Path, **overrides) -> Path:
    """A rich SUCCESSFUL run report whose free-text fields all carry PHI —
    the payload builder must never read any of them."""
    from openadapt_flow.ir import IdentityCheck

    fields: dict = dict(
        workflow_name="eligibility for Jane's Dental",
        started_at="2026-07-19T00:00:00Z",
        execution_origin="https://emr.example-clinic.test",
        bundle_content_digest="ab" * 32,
        success=True,
        total_ms=61500.0,
        params={"patient_name": "Jane Doe", "mrn": "12345"},
        rung_counts={"structural": 3, "ocr": 1},
        heal_count=1,
        model_calls=2,
        est_model_cost_usd=0.0123,
        identity_applicable_steps=2,
        identity_armed_steps=2,
        results=[
            StepResult(
                step_id="s1",
                intent="click Save for Jane Doe",
                ok=True,
                resolution=Resolution(
                    rung="structural", point=(0, 0), confidence=1.0, elapsed_ms=1.0
                ),
                identity=IdentityCheck(
                    status="verified",
                    expected="Jane Doe 1980-01-01",
                    observed="Jane Doe 1980-01-01",
                ),
                effect_verified=True,
                effect_results=["CONFIRMED eligibility write for MRN 12345"],
                effect_contract_hashes=["sha256:" + "cd" * 32],
            ),
            StepResult(
                step_id="s2",
                intent="type MRN 12345",
                ok=True,
                resolution=Resolution(
                    rung="ocr", point=(1, 1), confidence=0.9, elapsed_ms=2.0
                ),
                identity=IdentityCheck(
                    status="abstain", expected="Jane Doe", observed="Jane D0e"
                ),
                effect_approved_unverified=True,
            ),
            StepResult(
                step_id="s3", intent="skip Jane's recall", ok=True, skipped=True
            ),
        ],
    )
    fields.update(overrides)
    report = RunReport(**fields)
    report.save(run_dir)
    return run_dir / "report.json"


def test_report_run_success(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    recorder: dict = {}
    body = _run_response()
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, body))
    result = hosted.report_run(
        run_dir,
        workflow_id=_WORKFLOW_UUID,
        deployment_kind="byoc",
        org_id="legacy-org-id",
        backend="web",
        host="https://h.test",
        token="tok",
    )
    assert result["emitted"] is True
    assert result["run_id"] == _RUN_UUID
    assert recorder["url"] == "https://h.test/api/runs/ingest-report"
    posted = recorder["kw"]["json"]
    assert posted["kind"] == "run_summary"
    assert posted["schema"] == hosted.RUN_SUMMARY_SCHEMA
    assert posted["status"] == "success"
    assert posted["workflow_id"] == _WORKFLOW_UUID
    assert posted["bundle_content_digest"] == "ab" * 32
    assert "org_id" not in posted
    assert "deployment_kind" not in posted
    assert posted["backend"] == "web"
    assert posted["flow_version"]
    assert posted["phi_minimal"] is True
    # A persisted random UUID is the idempotency key. The raw report hash stays
    # local because the report can contain low-entropy identifiers.
    from uuid import UUID as _UUID

    assert str(_UUID(posted["client_run_id"])) == posted["client_run_id"]
    assert "run_content_hash" not in posted
    assert posted["metrics"] == {
        "steps": 3,
        "steps_ok": 3,
        "steps_skipped": 1,
        "duration_s": 61.5,
        "heal_count": 1,
        "model_calls": 2,
        "est_model_cost_usd": 0.0123,
        "rung_counts": {"structural": 3, "ocr": 1},
        "identity": {
            "verified": 1,
            "mismatch": 0,
            "abstain": 1,
            "unreadable": 0,
            "applicable": 2,
            "armed": 2,
        },
        "effects": {
            "verified": 1,
            "approved_unverified": 1,
            "contract_count": 1,
        },
    }
    assert "effect_contract_hashes" not in posted


def test_report_run_binds_by_digest_without_workflow_id(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _run_response()))
    hosted.report_run(run_dir, host="https://h.test", token="tok")
    posted = recorder["kw"]["json"]
    assert "workflow_id" not in posted
    assert "backend" not in posted
    assert posted["bundle_content_digest"] == "ab" * 32
    assert "workflow_name_digest" not in posted
    assert "eligibility" not in json.dumps(posted)


def test_report_run_payload_carries_no_phi(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _run_response()))
    hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    posted = recorder["kw"]["json"]
    blob = json.dumps(posted)
    # No PHI values: names, identifiers, param values, raw origins/URLs.
    assert "Jane" not in blob
    assert "12345" not in blob
    assert "example-clinic" not in blob
    assert "https://emr" not in blob
    # Excluded field SHAPES are absent entirely (fail-closed contract keys).
    for excluded in (
        "params",
        "observed_texts",
        "error",
        "expected",
        "observed",
        "intent",
        "screenshots",
        "url",
        "report_path",
        "workflow_name",
        "effect_results",
        "workflow_name_digest",
        "origin_domain_hash",
        "effect_contract_hashes",
        "run_content_hash",
    ):
        assert excluded not in posted, excluded


def test_report_run_never_egresses_actual_resolved_effect_contract_hash(
    tmp_path, monkeypatch
):
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"mrn": ValueExpr(param="mrn")},
        field="eligibility_status",
        value=ValueExpr(param="status"),
        idempotency_key=ValueExpr(param="mrn"),
    ).resolve({"mrn": "12345", "status": "active"})
    resolved_hash = effect.contract_hash()
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(
        run_dir,
        results=[
            StepResult(
                step_id="s1",
                intent="write eligibility for MRN 12345",
                ok=True,
                effect_verified=True,
                effect_contract_hashes=[resolved_hash],
            )
        ],
    )
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _run_response()))
    hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    blob = json.dumps(recorder["kw"]["json"])
    assert resolved_hash not in blob
    assert "12345" not in blob
    assert recorder["kw"]["json"]["metrics"]["effects"]["contract_count"] == 1


def test_report_run_refuses_non_success(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: pytest.fail("should not POST for a non-successful run"),
    )
    result = hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is False
    assert "report-break" in result["reason"]


def test_report_run_missing_binding_raises(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir, bundle_content_digest=None)
    with pytest.raises(hosted.HostedError, match="binding"):
        hosted.report_run(run_dir, host="https://h.test", token="tok")


def test_report_run_missing_report_raises(tmp_path):
    with pytest.raises(hosted.HostedError, match="report.json"):
        hosted.report_run(
            tmp_path, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


def test_report_run_422_falls_back_local(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(422, json={"error": "phi"})
    )
    result = hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert result["emitted"] is False
    assert result["local_only"] is True


def test_report_run_422_no_fallback_raises(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(422))
    with pytest.raises(hosted.HostedError, match="422"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
            allow_local_fallback=False,
        )


def test_report_run_401_raises(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(401))
    with pytest.raises(hosted.HostedError, match="401"):
        hosted.report_run(
            run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202, content=b"{not-json"),
        httpx.Response(202, json=["unexpected", "list"]),
    ],
)
def test_report_run_refuses_malformed_success_response(tmp_path, monkeypatch, response):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: response)
    with pytest.raises(hosted.HostedError, match="malformed JSON|unexpected response"):
        hosted.report_run(
            run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "run_id"),
        (_run_response(run_id="run_9"), "run_id"),
        (_run_response(status="halt"), "expected 'success'"),
        (_run_response(duplicate="true"), "duplicate"),
        (_run_response(prior_halt_run_id="run_1"), "prior_halt_run_id"),
        (_run_response(provenance="hosted_execution"), "provenance"),
    ],
)
def test_report_run_refuses_adversarial_success_shapes(
    tmp_path, monkeypatch, body, message
):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(httpx, "post", _capture_post({}, 202, body))
    with pytest.raises(hosted.HostedError, match=message):
        hosted.report_run(
            run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202, content=b"{not-json"),
        httpx.Response(202, json=["unexpected", "list"]),
    ],
)
def test_report_break_wraps_malformed_success_response(tmp_path, monkeypatch, response):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: response)
    with pytest.raises(hosted.HostedError, match="malformed JSON|unexpected response"):
        hosted.report_break(
            run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "run_id"),
        (_break_response(run_id="run_1"), "run_id"),
        (_break_response(status="success"), "expected 'halt'"),
        (_break_response(duplicate=1), "duplicate"),
        (
            {
                key: value
                for key, value in _break_response().items()
                if key != "halt_id"
            },
            "halt_id",
        ),
        (_break_response(halt_id="halt_1"), "halt_id"),
        (
            {
                key: value
                for key, value in _break_response().items()
                if key != "teach_url"
            },
            "teach_url",
        ),
        (_break_response(teach_url="/dashboard/runs/wrong/teach"), "teach_url"),
        (_break_response(provenance="hosted_execution"), "provenance"),
    ],
)
def test_report_break_refuses_adversarial_success_shapes(
    tmp_path, monkeypatch, body, message
):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(httpx, "post", _capture_post({}, 202, body))
    with pytest.raises(hosted.HostedError, match=message):
        hosted.report_break(
            run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )


def test_report_run_idempotency_key_stable_per_run_distinct_across_runs(
    tmp_path, monkeypatch
):
    """A retry reuses its UUID; a distinct run gets a distinct UUID."""
    run_a = tmp_path / "runs" / "r1"
    run_b = tmp_path / "runs" / "r2"
    _successful_run(run_a)
    _successful_run(run_b)  # byte-identical report.json
    ids = []
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: (
            ids.append(kw["json"]["client_run_id"]),
            httpx.Response(202, json=_run_response()),
        )[1],
    )
    for target in (run_a, run_a, run_b):
        hosted.report_run(
            target, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
        )
    assert ids[0] == ids[1]
    assert ids[2] != ids[0]
    assert (run_a / ".report_run_id").is_file()


def test_break_and_resumed_success_share_attempt_id(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    payloads: list[dict] = []

    def capture(url, **kwargs):
        payloads.append(kwargs["json"])
        if kwargs["json"]["status"] == "success":
            return httpx.Response(202, json=_run_response())
        return httpx.Response(
            202,
            json=_break_response(status=kwargs["json"]["status"]),
        )

    monkeypatch.setattr(httpx, "post", capture)
    hosted.report_break(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    _successful_run(run_dir)
    hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    assert payloads[0]["schema"] == hosted.BREAK_SUMMARY_SCHEMA
    assert payloads[1]["schema"] == hosted.RUN_SUMMARY_SCHEMA
    assert payloads[0]["client_run_id"] == payloads[1]["client_run_id"]
    assert payloads[0]["bundle_content_digest"] == payloads[1]["bundle_content_digest"]


def test_report_run_backend_is_a_closed_enum_before_egress(tmp_path, monkeypatch):
    """Red-team PHI-1: `backend` may only carry a fixed substrate token —
    free text (e.g. a recorded rdp window title / backend_hints) is dropped
    client-side, never sent."""
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid backend must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="backend"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            backend="rdp:Jane Doe - Dentrix",
            host="https://h.test",
            token="tok",
        )


def test_report_run_keeps_resolved_effect_hashes_local(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    many = [f"sha256:{i:064x}" for i in range(300)]
    _successful_run(
        run_dir,
        results=[
            StepResult(
                step_id="s1",
                intent="bulk write",
                ok=True,
                effect_verified=True,
                effect_contract_hashes=many,
            )
        ],
    )
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, _run_response()))
    hosted.report_run(
        run_dir, workflow_id=_WORKFLOW_UUID, host="https://h.test", token="tok"
    )
    posted = recorder["kw"]["json"]
    assert posted["metrics"]["effects"]["contract_count"] == 300
    assert "effect_contract_hashes" not in posted
    assert all(value not in json.dumps(posted) for value in many)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"started_at": "Jane Doe MRN 12345"}, "started_at"),
        ({"rung_counts": {"Jane Doe MRN 12345": 1}}, "resolution rung"),
        ({"total_ms": float("inf")}, "report.json"),
    ],
)
def test_report_run_refuses_malformed_report_fields_before_egress(
    tmp_path, monkeypatch, overrides, message
):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir, **overrides)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid summary must not egress"),
    )
    with pytest.raises(hosted.HostedError, match=message):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


def test_report_run_refuses_malformed_bundle_digest_before_egress(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    report_path = _successful_run(run_dir)
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    raw["bundle_content_digest"] = "Jane Doe MRN 12345"
    report_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid report must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="report.json"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


def test_report_run_refuses_non_uuid_workflow_before_egress(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid workflow id must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="workflow_id"):
        hosted.report_run(
            run_dir,
            workflow_id="Jane Doe MRN 12345",
            host="https://h.test",
            token="tok",
        )


def test_report_run_refuses_noncanonical_uuid_before_egress(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid workflow id must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="canonical UUID"):
        hosted.report_run(
            run_dir,
            workflow_id="00000000-0000-0000-0000-000000000000",
            host="https://h.test",
            token="tok",
        )


def test_report_run_refuses_non_semver_flow_version_before_egress(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.setattr(hosted, "_flow_version", lambda: "local-dev")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid version must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="flow_version"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_ms": (hosted._MAX_DURATION_S + 0.001) * 1000},
        {"est_model_cost_usd": hosted._MAX_MODEL_COST_USD + 0.001},
        {"rung_counts": {"structural": hosted._MAX_COUNTER + 1}},
    ],
)
def test_report_run_enforces_exact_cloud_numeric_bounds_before_egress(
    tmp_path, monkeypatch, overrides
):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir, **overrides)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("out-of-range summary must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="accepted range"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


def test_report_run_refuses_symlink_id_without_overwriting_target(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    target = tmp_path / "do-not-touch.txt"
    target.write_text("preserve me", encoding="utf-8")
    try:
        (run_dir / ".report_run_id").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("unsafe sidecar must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="regular file"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )
    assert target.read_text(encoding="utf-8") == "preserve me"


def test_report_run_refuses_when_id_cannot_be_persisted(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    real_open = hosted.os.open

    def fail_sidecar(path, flags, mode=0o777):
        if str(path).endswith(".report_run_id"):
            raise PermissionError("read only")
        return real_open(path, flags, mode)

    monkeypatch.setattr(hosted.os, "open", fail_sidecar)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("unstable id must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="persist"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


def test_client_run_id_reads_from_descriptor_with_nofollow_when_available(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
    real_open = hosted.os.open
    opened_flags: list[int] = []

    def recording_open(path, flags, mode=0o777):
        if Path(path) == id_path:
            opened_flags.append(flags)
        return real_open(path, flags, mode)

    def refuse_path_read(*args, **kwargs):
        raise AssertionError("client run id must be read from its bound descriptor")

    monkeypatch.setattr(hosted.os, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", refuse_path_read)
    assert hosted._client_run_id(run_dir) == _RUN_UUID
    assert opened_flags
    if hasattr(hosted.os, "O_NOFOLLOW"):
        assert opened_flags[0] & hosted.os.O_NOFOLLOW


def test_client_run_id_fallback_refuses_entry_swap_before_read(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
    substitute = tmp_path / "substitute-id"
    substitute.write_text(_HALT_UUID + "\n", encoding="utf-8")
    real_open = hosted.os.open

    monkeypatch.delattr(hosted.os, "O_NOFOLLOW", raising=False)

    def swapped_open(path, flags, mode=0o777):
        if Path(path) == id_path:
            return real_open(substitute, flags, mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(hosted.os, "open", swapped_open)
    with pytest.raises(hosted.HostedError, match="changed while"):
        hosted._client_run_id(run_dir)


@pytest.mark.skipif(hosted.os.name == "nt", reason="directory fsync is POSIX-only")
def test_client_run_id_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    real_fsync = hosted.os.fsync
    fsynced_modes: list[int] = []

    def record_fsync(fd):
        fsynced_modes.append(hosted.os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(hosted.os, "fsync", record_fsync)
    hosted._client_run_id(run_dir)
    assert any(hosted.stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(hosted.stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_client_run_id_is_race_safe(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: hosted._client_run_id(run_dir), range(200)))
    assert len(set(ids)) == 1
    if hosted.os.name != "nt":
        assert (run_dir / ".report_run_id").stat().st_mode & 0o777 == 0o600


def test_client_run_id_waits_for_exact_exclusive_creator(tmp_path, monkeypatch):
    """A loser may observe O_EXCL's empty entry before the winner writes it."""
    from concurrent.futures import ThreadPoolExecutor

    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    writer_created = threading.Event()
    allow_writer = threading.Event()
    reader_saw_empty = threading.Event()
    real_fdopen = hosted.os.fdopen
    real_read = hosted.os.read
    paused = False

    def paused_fdopen(fd, *args, **kwargs):
        nonlocal paused
        if not paused and stat.S_ISREG(hosted.os.fstat(fd).st_mode):
            paused = True
            writer_created.set()
            assert allow_writer.wait(timeout=2), "test did not release id writer"
        return real_fdopen(fd, *args, **kwargs)

    def recording_read(fd, size):
        raw = real_read(fd, size)
        if raw == b"" and id_path.exists():
            reader_saw_empty.set()
        return raw

    monkeypatch.setattr(hosted.os, "fdopen", paused_fdopen)
    monkeypatch.setattr(hosted.os, "read", recording_read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(hosted._client_run_id, run_dir)
        assert writer_created.wait(timeout=2), "winner did not create id entry"
        loser = pool.submit(hosted._client_run_id, run_dir)
        assert reader_saw_empty.wait(timeout=2), "loser did not observe empty entry"
        allow_writer.set()
        assert winner.result(timeout=2) == loser.result(timeout=2)


@pytest.mark.skipif(
    hosted.os.name == "nt",
    reason="Windows denies replacing an entry while its descriptor is open",
)
def test_client_run_id_refuses_inode_swap_while_waiting(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.touch()
    real_sleep = hosted.time.sleep
    swapped = False

    def swap_entry(_delay):
        nonlocal swapped
        if not swapped:
            swapped = True
            id_path.unlink()
            id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
        real_sleep(0)

    monkeypatch.setattr(hosted.time, "sleep", swap_entry)
    with pytest.raises(hosted.HostedError, match="changed while waiting"):
        hosted._client_run_id(run_dir)


@pytest.mark.skipif(
    hosted.os.name == "nt",
    reason="Windows denies replacing an entry while its descriptor is open",
)
def test_client_run_id_refuses_inode_swap_during_read(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
    real_read = hosted.os.read
    swapped = False

    def swap_then_read(fd, size):
        nonlocal swapped
        if not swapped:
            swapped = True
            id_path.unlink()
            id_path.write_text(_HALT_UUID + "\n", encoding="utf-8")
        return real_read(fd, size)

    monkeypatch.setattr(hosted.os, "read", swap_then_read)
    with pytest.raises(hosted.HostedError, match="changed while"):
        hosted._client_run_id(run_dir)
    assert id_path.read_text(encoding="utf-8").strip() == _HALT_UUID


@pytest.mark.skipif(
    hosted.os.name == "nt" or not hasattr(hosted.os, "mkfifo"),
    reason="FIFO substitution is POSIX-only",
)
def test_client_run_id_nonblocking_open_refuses_fifo_swap(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
    real_open = hosted.os.open
    swapped = False

    def swap_to_fifo(path, flags, mode=0o777):
        nonlocal swapped
        if Path(path) == id_path and not (flags & hosted.os.O_CREAT) and not swapped:
            assert flags & hosted.os.O_NONBLOCK
            swapped = True
            id_path.unlink()
            hosted.os.mkfifo(id_path)
        return real_open(path, flags, mode)

    monkeypatch.setattr(hosted.os, "open", swap_to_fifo)
    with pytest.raises(hosted.HostedError, match="regular file"):
        hosted._client_run_id(run_dir)


@pytest.mark.skipif(
    hosted.os.name == "nt",
    reason="Windows denies replacing an entry while its descriptor is open",
)
def test_client_run_id_write_failure_never_unlinks_replacement(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    real_fsync = hosted.os.fsync
    replaced = False

    def replace_then_fail(fd):
        nonlocal replaced
        if not replaced:
            replaced = True
            id_path.unlink()
            id_path.write_text(_HALT_UUID + "\n", encoding="utf-8")
            raise OSError("forced persistence failure")
        return real_fsync(fd)

    monkeypatch.setattr(hosted.os, "fsync", replace_then_fail)
    with pytest.raises(hosted.HostedError, match="persist"):
        hosted._client_run_id(run_dir)
    assert id_path.read_text(encoding="utf-8").strip() == _HALT_UUID


def test_client_run_id_closes_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    real_close = hosted.os.close
    created_fd = None
    closed_fds = []

    def fail_fdopen(fd, *args, **kwargs):
        nonlocal created_fd
        created_fd = fd
        raise OSError("forced fdopen failure")

    def record_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(hosted.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(hosted.os, "close", record_close)
    with pytest.raises(hosted.HostedError, match="persist"):
        hosted._client_run_id(run_dir)
    assert created_fd in closed_fds
    assert (run_dir / ".report_run_id").exists()


@pytest.mark.skipif(
    hosted.os.name != "nt",
    reason="Windows-specific open-descriptor replacement denial",
)
def test_client_run_id_windows_denies_replacement_during_read(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_text(_RUN_UUID + "\n", encoding="utf-8")
    real_read = hosted.os.read
    replacement_denied = False

    def attempt_replacement_then_read(fd, size):
        nonlocal replacement_denied
        with pytest.raises(OSError):
            id_path.unlink()
        replacement_denied = True
        return real_read(fd, size)

    monkeypatch.setattr(hosted.os, "read", attempt_replacement_then_read)
    assert hosted._client_run_id(run_dir) == _RUN_UUID
    assert replacement_denied


def test_client_run_id_retries_only_canonical_partial_write(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    id_path = run_dir / ".report_run_id"
    id_path.write_bytes(_RUN_UUID[:12].encode("ascii"))
    completed = False

    def complete_same_inode(_delay):
        nonlocal completed
        assert not completed
        completed = True
        with id_path.open("r+b") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write((_RUN_UUID + "\n").encode("ascii"))
            handle.flush()

    monkeypatch.setattr(hosted.time, "sleep", complete_same_inode)
    assert hosted._client_run_id(run_dir) == _RUN_UUID
    assert completed


def test_client_run_id_refuses_malformed_short_value_without_retry(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / ".report_run_id").write_bytes(b"not-a-prefix")
    monkeypatch.setattr(
        hosted.time,
        "sleep",
        lambda _delay: pytest.fail("malformed value must not be retried"),
    )
    with pytest.raises(hosted.HostedError, match="unreadable or invalid"):
        hosted._client_run_id(run_dir)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_client_run_id_accepts_canonical_platform_line_endings(tmp_path, ending):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / ".report_run_id").write_bytes(_RUN_UUID.encode("ascii") + ending)
    assert hosted._client_run_id(run_dir) == _RUN_UUID


def test_report_run_refuses_oversized_report_before_egress(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    report_path = _successful_run(run_dir)
    with report_path.open("ab") as handle:
        handle.write(b" " * (hosted._MAX_RUN_REPORT_BYTES + 1))
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("oversized report must not egress"),
    )
    with pytest.raises(hosted.HostedError, match="exceeds"):
        hosted.report_run(
            run_dir,
            workflow_id=_WORKFLOW_UUID,
            host="https://h.test",
            token="tok",
        )


# --- opt-in post-run hook ---------------------------------------------------


def test_maybe_report_run_requires_explicit_opt_in(tmp_path, monkeypatch):
    from openadapt_flow.__main__ import _maybe_report_run

    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    monkeypatch.delenv("OPENADAPT_FLOW_REPORT_RUN", raising=False)
    monkeypatch.setattr(
        hosted,
        "report_run",
        lambda *a, **k: pytest.fail("must never upload without opt-in"),
    )
    import argparse as _argparse

    report = RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )
    _maybe_report_run(run_dir, report, _argparse.Namespace())  # no --report
    _maybe_report_run(run_dir, report, None)  # no args at all


def test_maybe_report_run_fires_on_opt_in_success_only(tmp_path, monkeypatch, capsys):
    from openadapt_flow.__main__ import _maybe_report_run

    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)
    calls: list = []

    def fake_report_run(run_dir, **kw):
        calls.append(kw)
        return {"emitted": True, "run_id": "run_9"}

    monkeypatch.setattr(hosted, "report_run", fake_report_run)
    monkeypatch.setenv("OPENADAPT_FLOW_HOSTED_WORKFLOW_ID", "wf_1")
    import argparse as _argparse

    report = RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )
    args = _argparse.Namespace(report=True, backend="web")
    _maybe_report_run(run_dir, report, args)
    assert calls and calls[0]["workflow_id"] == "wf_1"
    assert calls[0]["backend"] == "web"
    assert "deployment_kind" not in calls[0]
    assert "Run summary reported" in capsys.readouterr().out

    # A halted run never fires the SUCCESS hook, even when opted in.
    halted_dir = tmp_path / "runs" / "r2"
    _halted_run(halted_dir)
    halted = RunReport.model_validate_json(
        (halted_dir / "report.json").read_text(encoding="utf-8")
    )
    _maybe_report_run(halted_dir, halted, args)
    assert len(calls) == 1


def test_maybe_report_run_env_opt_in_and_swallows_errors(tmp_path, monkeypatch, capsys):
    from openadapt_flow.__main__ import _maybe_report_run

    run_dir = tmp_path / "runs" / "r1"
    _successful_run(run_dir)

    def exploding_report_run(run_dir, **kw):
        raise hosted.HostedError("network down")

    monkeypatch.setattr(hosted, "report_run", exploding_report_run)
    monkeypatch.setenv("OPENADAPT_FLOW_REPORT_RUN", "1")
    report = RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )
    # Must never raise: the hook cannot change the run's outcome.
    _maybe_report_run(run_dir, report, None)
    assert "run summary report skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_parser_has_new_commands():
    parser = build_parser()
    # smoke: each subcommand parses to its handler
    for cmd in (
        "login",
        "sanitize",
        "review-sanitized",
        "approve-sanitized",
        "validate-hosted",
        "push",
        "report-break",
        "report-run",
    ):
        assert cmd in parser._subparsers._group_actions[0].choices


def test_cli_login_dispatch(monkeypatch, capsys):
    called: dict = {}

    def fake_login(
        token=None,
        host=None,
        save=True,
        allow_plaintext_token=False,
        destination_kind=None,
        trusted_hosts=None,
    ):
        called.update(
            token=token,
            host=host,
            save=save,
            allow_plaintext_token=allow_plaintext_token,
            destination_kind=destination_kind,
            trusted_hosts=trusted_hosts,
        )
        return {
            "host": host or "https://h.test",
            "valid": True,
            "settings_url": "https://h.test/dashboard/settings/ingest",
            "config_path": "/tmp/config.toml",
        }

    monkeypatch.setattr(hosted, "login", fake_login)
    rc = main(
        [
            "login",
            "--token",
            "tok",
            "--host",
            "https://h.test",
            "--destination-kind",
            "customer-managed",
            "--trusted-host",
            "https://h.test",
            "--trusted-host",
            "https://backup.test",
            "--no-save",
            "--allow-plaintext-token",
        ]
    )
    assert rc == 0
    assert called == {
        "token": "tok",
        "host": "https://h.test",
        "save": False,
        "allow_plaintext_token": True,
        "destination_kind": "customer-managed",
        "trusted_hosts": ["https://h.test", "https://backup.test"],
    }
    assert "Logged in" in capsys.readouterr().out


def test_cli_push_dispatch(monkeypatch, capsys):
    captured: dict = {}

    def fake_push(
        path,
        kind="recording",
        name=None,
        host=None,
        token=None,
        deployment_kind=None,
        attest_non_phi=False,
        **kwargs,
    ):
        captured.update(
            path=path,
            name=name,
            host=host,
            token=token,
            kind=kind,
            deployment_kind=deployment_kind,
            attest_non_phi=attest_non_phi,
            **kwargs,
        )
        return {
            "workflow_id": "wf_7",
            "workflow_name": "n",
            "kind": kind,
            "compile": {"status": "compiled"},
            "dashboard_url": "https://h.test/dashboard/workflows/wf_7",
        }

    monkeypatch.setattr(hosted, "push", fake_push)
    rc = main(
        [
            "push",
            "some/rec",
            "--kind",
            "bundle",
            "--deployment-kind",
            "cloud",
            "--attest-non-phi",
            "--name",
            "Demo",
            "--workflow-id",
            "ec726a3e-dcaf-40cf-870a-867d104002dd",
            "--resolves-run-id",
            "d3ecf64d-0d25-4df7-9264-77bf7d266d77",
            "--host",
            "https://h.test",
            "--token",
            "tok",
            "--destination-kind",
            "customer-managed",
            "--trusted-host",
            "https://h.test",
            "--sanitized-out",
            "derived",
            "--auto-approve",
            "--validation-attestation",
            "validation.json",
        ]
    )
    assert rc == 0
    assert captured == {
        "path": "some/rec",
        "name": "Demo",
        "workflow_id": "ec726a3e-dcaf-40cf-870a-867d104002dd",
        "resolves_run_id": "d3ecf64d-0d25-4df7-9264-77bf7d266d77",
        "host": "https://h.test",
        "token": "tok",
        "kind": "bundle",
        "deployment_kind": "cloud",
        "attest_non_phi": True,
        "destination_kind": "customer-managed",
        "trusted_hosts": ["https://h.test"],
        "sanitized_out": "derived",
        "auto_approve": True,
        "validation_attestation": "validation.json",
    }
    out = capsys.readouterr().out
    assert "wf_7" in out
    assert "Dashboard" in out


def test_cli_report_break_dispatch(monkeypatch, capsys):
    captured: dict = {}

    def fake_report_break(run_dir, **kw):
        captured.update(run_dir=run_dir, **kw)
        return {
            "emitted": True,
            "run_id": "r",
            "halt_id": "h",
            "status": "halt",
            "teach_url": "https://h.test/dashboard/runs/r/teach",
        }

    monkeypatch.setattr(hosted, "report_break", fake_report_break)
    rc = main(
        [
            "report-break",
            "runs/r1",
            "--workflow-id",
            "wf_1",
            "--deployment-kind",
            "byoc",
            "--org-id",
            "org_1",
            "--host",
            "https://h.test",
            "--destination-kind",
            "customer-managed",
            "--trusted-host",
            "https://h.test",
            "--token",
            "tok",
        ]
    )
    assert rc == 0
    assert captured == {
        "run_dir": "runs/r1",
        "workflow_id": "wf_1",
        "deployment_kind": "byoc",
        "org_id": "org_1",
        "host": "https://h.test",
        "destination_kind": "customer-managed",
        "trusted_hosts": ["https://h.test"],
        "token": "tok",
    }
    assert "Break reported" in capsys.readouterr().out


def test_cli_report_run_dispatch(monkeypatch, capsys):
    captured: dict = {}

    def fake_report_run(run_dir, **kw):
        captured.update(run_dir=run_dir, **kw)
        return {"emitted": True, "run_id": "run_9", "status": "success"}

    monkeypatch.setattr(hosted, "report_run", fake_report_run)
    rc = main(
        [
            "report-run",
            "runs/r1",
            "--workflow-id",
            _WORKFLOW_UUID,
            "--deployment-kind",
            "byoc",
            "--org-id",
            "legacy-org-id",
            "--backend",
            "windows",
            "--host",
            "https://h.test",
            "--destination-kind",
            "customer-managed",
            "--trusted-host",
            "https://h.test",
            "--token",
            "tok",
        ]
    )
    assert rc == 0
    assert captured == {
        "run_dir": "runs/r1",
        "workflow_id": _WORKFLOW_UUID,
        "deployment_kind": "byoc",
        "org_id": "legacy-org-id",
        "backend": "windows",
        "host": "https://h.test",
        "destination_kind": "customer-managed",
        "trusted_hosts": ["https://h.test"],
        "token": "tok",
    }
    assert "Run summary reported" in capsys.readouterr().out


def test_cli_validate_hosted_dispatches_every_contract_flag(
    tmp_path, monkeypatch, capsys
):
    from openadapt_flow import runtime_validation

    compiler_config = tmp_path / "compiler.json"
    compiler_config.write_text('{"mode":"strict"}')
    captured: dict = {}
    expected = {"schema": runtime_validation.SCHEMA, "signature": "a" * 64}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return expected

    def fake_save(attestation, path):
        assert attestation is expected
        assert path == tmp_path / "validation.json"
        return path

    monkeypatch.setattr(
        runtime_validation, "create_runtime_validation_attestation", fake_create
    )
    monkeypatch.setattr(
        runtime_validation, "save_runtime_validation_attestation", fake_save
    )

    rc = main(
        [
            "validate-hosted",
            "--recording",
            "recording-reviewed",
            "--bundle",
            "bundle-reviewed",
            "--run-dir",
            "run-1",
            "--policy",
            "permissive",
            "--risk-class",
            "low",
            "--environment",
            "clean-room-v1",
            "--target-url",
            "https://app.example/login",
            "--allowed-host",
            "cdn.example",
            "--allowed-host",
            "api.example",
            "--compiler-config",
            str(compiler_config),
            "--out",
            str(tmp_path / "validation.json"),
            "--host",
            "https://h.test",
            "--destination-kind",
            "customer-managed",
            "--trusted-host",
            "https://h.test",
            "--token",
            "tok",
        ]
    )

    assert rc == 0
    assert captured == {
        "recording_derivative": Path("recording-reviewed"),
        "bundle_derivative": Path("bundle-reviewed"),
        "run_dir": Path("run-1"),
        "policy_source": "permissive",
        "risk_class": "low",
        "environment": "clean-room-v1",
        "target_url": "https://app.example/login",
        "allowed_hosts": ["cdn.example", "api.example"],
        "compiler_config": {"mode": "strict"},
        "host": "https://h.test",
        "token": "tok",
        "destination_kind": "customer-managed",
        "trusted_hosts": ["https://h.test"],
    }
    assert "attestation written" in capsys.readouterr().out


def test_cli_push_error_returns_1(monkeypatch, capsys):
    def fake_push(*a, **k):
        raise hosted.HostedError("no token")

    monkeypatch.setattr(hosted, "push", fake_push)
    rc = main(["push"])
    assert rc == 1
    assert "push failed" in capsys.readouterr().out
