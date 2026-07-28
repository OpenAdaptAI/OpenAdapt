"""Security and lifecycle tests for the installed-app pairing protocol."""

from __future__ import annotations

from uuid import uuid4

import pytest

from engine.auth import pairing, store

SECRET = "oap_" + "A" * 43
TOKEN = "oai_ingest_" + "B" * 43
OLD_TOKEN = "oai_ingest_" + "C" * 43
OTHER_TOKEN = "oai_ingest_" + "D" * 43
HOST = "https://app.openadapt.ai"
OTHER_HOST = "https://previous.example"
VALID_URI = f"openadapt://connect?pairing={SECRET}&host=https%3A%2F%2Fapp.openadapt.ai"
EXPIRES_AT = "2026-10-26T12:00:00+00:00"
EXPIRES_TS = 1793016000.0


class _Response:
    def __init__(
        self,
        status_code: int,
        body: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body


def _credential_lifetime() -> dict:
    return {
        "expires_at": EXPIRES_AT,
        "expires_in_days": 90,
        "expiring_soon": False,
        "legacy_non_expiring": False,
        "warning_days": 14,
    }


def _claim_response(pairing_id: str) -> _Response:
    return _Response(
        201,
        {
            "paired": True,
            "pairing_id": pairing_id,
            "ingest_token_id": str(uuid4()),
            "ingest_token": TOKEN,
            "credential": _credential_lifetime(),
        },
        {"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )


def _validation_response(status: int = 200) -> _Response:
    return _Response(
        status,
        {"count": 0, "credential": _credential_lifetime()},
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "90",
        },
    )


def test_parser_accepts_only_the_fixed_connect_action() -> None:
    assert pairing.parse_connect_uri(VALID_URI) == {
        "pairing": SECRET,
        "host": HOST,
    }
    for uri in (
        VALID_URI.replace("://connect?", "://run?"),
        VALID_URI.replace("openadapt:", "https:"),
        VALID_URI.replace("connect?", "connect/record?"),
        VALID_URI + "#fragment",
        f"openadapt://user@connect?pairing={SECRET}&host={HOST}",
    ):
        with pytest.raises(pairing.PairingError, match="Invalid OpenAdapt connect link"):
            pairing.parse_connect_uri(uri)


def test_parser_rejects_malformed_missing_duplicate_and_unknown_fields() -> None:
    bad = (
        "",
        "openadapt://connect?pairing",
        f"openadapt://connect?pairing=short&host={HOST}",
        f"openadapt://connect?pairing={SECRET}",
        f"openadapt://connect?pairing={SECRET}&host={HOST}&pairing={SECRET}",
        f"openadapt://connect?pairing={SECRET}&host={HOST}&command=whoami",
        f"openadapt://connect?pairing={SECRET}&host={HOST}&destination_kind=customer-managed",
    )
    for uri in bad:
        with pytest.raises(pairing.PairingError):
            pairing.parse_connect_uri(uri)


def test_destination_is_exact_managed_origin_or_explicit_loopback() -> None:
    for host in (
        "https://app.openadapt.ai.evil.example",
        "https://user@app.openadapt.ai",
        "https://app.openadapt.ai/path",
        "http://app.openadapt.ai",
        "https://example.com",
    ):
        uri = f"openadapt://connect?pairing={SECRET}&host={host}"
        with pytest.raises(pairing.PairingError):
            pairing.parse_connect_uri(uri)

    local = (
        f"openadapt://connect?pairing={SECRET}&host=http%3A%2F%2Flocalhost%3A3000"
        "&destination_kind=local"
    )
    assert pairing.parse_connect_uri(local)["host"] == "http://localhost:3000"
    remote_local = (
        f"openadapt://connect?pairing={SECRET}&host=https%3A%2F%2Fexample.com"
        "&destination_kind=local"
    )
    with pytest.raises(pairing.PairingError, match="must use this computer"):
        pairing.parse_connect_uri(remote_local)


def test_argument_shaped_values_remain_data_and_cannot_select_an_action() -> None:
    for payload in (
        "--host=https://evil.example",
        "%2D%2Dhost%3Dhttps%3A%2F%2Fevil.example",
        "oap_" + "A" * 42 + ";",
    ):
        uri = f"openadapt://connect?pairing={payload}&host={HOST}"
        with pytest.raises(pairing.PairingError, match="Pairing code is malformed"):
            pairing.parse_connect_uri(uri)


def test_pairing_refuses_before_claim_when_secure_store_is_unavailable(monkeypatch) -> None:
    called = False

    def _post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("claim must not be consumed")

    monkeypatch.setattr(pairing, "secure_store_available", lambda: False)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    with pytest.raises(pairing.PairingError, match="keychain"):
        pairing.connect_uri(VALID_URI)
    assert called is False


def test_connect_claims_stores_verifies_and_confirms_without_returning_token(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    events: list[str] = []
    real_snapshot = pairing.snapshot_pairing_canonical
    real_stage = pairing.stage_pairing_credential
    real_commit = pairing.commit_pairing_stage
    real_clear = pairing.clear_pairing_stage

    def _post(url, *, json, headers=None, **kwargs):
        if url.endswith("/claim"):
            events.append("claim")
            assert json == {"pairing_secret": SECRET, "device_name": "test-device"}
            assert headers is None
            return _claim_response(pairing_id)
        assert not url.endswith("/abort")
        events.append("confirm")
        assert url.endswith("/confirm")
        assert json == {"pairing_id": pairing_id}
        assert headers == {"Authorization": f"Bearer {TOKEN}"}
        return _Response(200, {"connected": True})

    def _snapshot(host):
        events.append("snapshot")
        return real_snapshot(host)

    def _stage(*args):
        events.append("stage")
        return real_stage(*args)

    def _get(url, *, headers, **kwargs):
        events.append("validate")
        assert url == f"{HOST}/api/needs-attention/count"
        assert headers == {"Authorization": f"Bearer {TOKEN}"}
        return _validation_response()

    def _commit(value):
        events.append("commit")
        return real_commit(value)

    def _clear(value):
        events.append("clear")
        return real_clear(value)

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing, "_safe_device_name", lambda: "test-device")
    monkeypatch.setattr(pairing, "snapshot_pairing_canonical", _snapshot)
    monkeypatch.setattr(pairing, "stage_pairing_credential", _stage)
    monkeypatch.setattr(pairing, "commit_pairing_stage", _commit)
    monkeypatch.setattr(pairing, "clear_pairing_stage", _clear)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", _get)

    result = pairing.connect_uri(VALID_URI)

    assert result["authenticated"] is True
    assert result["host"] == HOST
    assert "token" not in result
    assert store.load_credential(HOST) == _credential(HOST, TOKEN, EXPIRES_TS)
    assert store.load_pairing_stage() is None
    assert events == [
        "snapshot",
        "claim",
        "stage",
        "validate",
        "commit",
        "confirm",
        "clear",
    ]


def _credential(host: str, token: str, expires_at: float | None = None) -> dict:
    return {
        "kind": "ingest_token",
        "token": token,
        "refresh_token": None,
        "org_id": None,
        "host": host,
        "expires_at": expires_at,
    }


def test_rejected_verification_aborts_new_claim_without_touching_prior_state(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    store.store_credential(_credential(HOST, OLD_TOKEN))
    store.store_credential(_credential(OTHER_HOST, OTHER_TOKEN))
    posts: list[tuple[str, dict, dict]] = []

    def _post(url, *, json, headers=None, **kwargs):
        posts.append((url, json, headers or {}))
        if url.endswith("/claim"):
            return _claim_response(pairing_id)
        assert url.endswith("/abort")
        return _Response(200, {"revoked": True})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _Response(401, {}))

    with pytest.raises(
        pairing.PairingError,
        match="previous Desktop connection is unchanged",
    ) as error:
        pairing.connect_uri(VALID_URI)
    assert SECRET not in str(error.value)
    assert TOKEN not in str(error.value)
    assert pairing_id not in str(error.value)
    assert store.load_credential(HOST) == _credential(HOST, OLD_TOKEN)
    assert store.load_credential(OTHER_HOST) == _credential(OTHER_HOST, OTHER_TOKEN)
    assert store.active_host() == OTHER_HOST
    assert posts[-1] == (
        f"{HOST}/api/local-bridge/pairings/abort",
        {"pairing_id": pairing_id},
        {"Authorization": f"Bearer {TOKEN}"},
    )
    assert store.load_pairing_stage() is None


def test_confirm_ambiguity_preserves_new_canonical_and_recovers_idempotently(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    endpoints: list[str] = []
    confirm_ambiguous = True

    def _post(url, *, json, headers=None, **kwargs):
        endpoints.append(url.rsplit("/", 1)[-1])
        if url.endswith("/claim"):
            return _claim_response(pairing_id)
        if url.endswith("/confirm"):
            if confirm_ambiguous:
                return _Response(503, {})
            return _Response(200, {"connected": True})
        raise AssertionError("ambiguous confirmation must not be aborted")

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _validation_response())

    with pytest.raises(pairing.PairingError, match="confirmation.*uncertain"):
        pairing.connect_uri(VALID_URI)
    assert store.load_credential(HOST) == _credential(HOST, TOKEN, EXPIRES_TS)
    assert store.active_host() == HOST
    assert store.load_pairing_stage()["state"] == "confirm_ambiguous"
    assert endpoints == ["claim", "confirm", "confirm"]

    confirm_ambiguous = False
    recovered = pairing.recover_pending_pairing()
    assert recovered is not None and recovered["authenticated"] is True
    assert store.load_credential(HOST) == _credential(HOST, TOKEN, EXPIRES_TS)
    assert store.load_pairing_stage() is None
    assert endpoints[-1] == "confirm"


def test_crash_after_canonical_write_recovers_without_a_second_claim(
    monkeypatch,
    fake_keyring,
) -> None:
    pairing_id = str(uuid4())
    previous = store.snapshot_pairing_canonical(HOST)
    assert previous is not None
    assert store.stage_pairing_credential(
        pairing_id,
        _credential(HOST, TOKEN, EXPIRES_TS),
        previous,
        "crash-device",
    )
    staged_keychain_value = fake_keyring.get_password(
        store.SERVICE_NAME,
        store._PAIRING_STAGE_ACCOUNT,
    )
    assert staged_keychain_value is not None and TOKEN in staged_keychain_value
    assert store.commit_pairing_stage(pairing_id)
    assert store.load_pairing_stage()["state"] == "canonical_written"

    posts: list[str] = []

    def _post(url, **kwargs):
        posts.append(url.rsplit("/", 1)[-1])
        return _Response(200, {"connected": True})

    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _validation_response())

    recovered = pairing.recover_pending_pairing()
    assert recovered is not None
    assert recovered["device_name"] == "crash-device"
    assert posts == ["confirm"]
    assert store.load_credential(HOST) == _credential(HOST, TOKEN, EXPIRES_TS)
    assert store.load_pairing_stage() is None


def test_definitive_confirm_failure_aborts_and_restores_exact_prior_state(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    store.store_credential(_credential(HOST, OLD_TOKEN))
    store.store_credential(_credential(OTHER_HOST, OTHER_TOKEN))
    endpoints: list[str] = []

    def _post(url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        endpoints.append(endpoint)
        if endpoint == "claim":
            return _claim_response(pairing_id)
        if endpoint == "confirm":
            return _Response(409, {})
        return _Response(200, {"revoked": True})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _validation_response())

    with pytest.raises(pairing.PairingError, match="previous Desktop connection is unchanged"):
        pairing.connect_uri(VALID_URI)
    assert endpoints == ["claim", "confirm", "abort"]
    assert store.load_credential(HOST) == _credential(HOST, OLD_TOKEN)
    assert store.load_credential(OTHER_HOST) == _credential(OTHER_HOST, OTHER_TOKEN)
    assert store.active_host() == OTHER_HOST
    assert store.load_pairing_stage() is None


def test_abort_409_never_blindly_restores_over_possibly_confirmed_token(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    store.store_credential(_credential(HOST, OLD_TOKEN))

    def _post(url, **kwargs):
        if url.endswith("/claim"):
            return _claim_response(pairing_id)
        return _Response(409, {})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _validation_response())

    with pytest.raises(pairing.PairingError, match="preserved.*safe recovery"):
        pairing.connect_uri(VALID_URI)
    assert store.load_credential(HOST) == _credential(HOST, TOKEN, EXPIRES_TS)
    assert store.active_host() == HOST
    assert store.load_pairing_stage() is not None


def test_keychain_commit_failure_restores_prior_state_and_attempts_abort(
    monkeypatch,
    fake_keyring,
) -> None:
    pairing_id = str(uuid4())
    store.store_credential(_credential(HOST, OLD_TOKEN))
    store.store_credential(_credential(OTHER_HOST, OTHER_TOKEN))
    original_set = fake_keyring.set_password

    def _fail_new_companion(service, account, value):
        if account == HOST + store._CRED_SUFFIX and TOKEN in value:
            raise RuntimeError("keychain locked during commit")
        return original_set(service, account, value)

    fake_keyring.set_password = _fail_new_companion
    endpoints: list[str] = []

    def _post(url, *, json, headers=None, **kwargs):
        endpoints.append(url.rsplit("/", 1)[-1])
        if url.endswith("/claim"):
            return _claim_response(pairing_id)
        assert url.endswith("/abort")
        return _Response(200, {"revoked": True})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *args, **kwargs: _validation_response())

    with pytest.raises(pairing.PairingError, match="atomically write"):
        pairing.connect_uri(VALID_URI)
    assert store.load_credential(HOST) == _credential(HOST, OLD_TOKEN)
    assert store.load_credential(OTHER_HOST) == _credential(OTHER_HOST, OTHER_TOKEN)
    assert store.active_host() == OTHER_HOST
    assert store.load_pairing_stage() is None
    assert endpoints == ["claim", "abort"]


def test_pkce_claim_sends_the_exact_verifier_and_maps_410(monkeypatch) -> None:
    verifier = "V" * 43
    requests: list[dict] = []

    def _post(url, *, json, headers=None, **kwargs):
        requests.append(json)
        assert url == f"{HOST}/api/local-bridge/pairings/claim"
        assert headers is None
        return _Response(410, {})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing, "_safe_device_name", lambda: "test-device")
    monkeypatch.setattr(pairing.httpx, "post", _post)

    with pytest.raises(pairing.PairingError, match="expired.*already used"):
        pairing.claim_pairing(HOST, SECRET, code_verifier=verifier)
    assert requests == [
        {
            "pairing_secret": SECRET,
            "device_name": "test-device",
            "code_verifier": verifier,
        }
    ]


@pytest.mark.parametrize(
    "wrong_role",
    [
        "supabase_authorization_code",
        "oai_ingest_" + "A" * 43,
        "oapp_" + "A" * 43,
        "oaps_" + "A" * 43,
    ],
)
def test_claim_rejects_every_non_pairing_credential_before_network(wrong_role, monkeypatch) -> None:
    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(
        pairing.httpx,
        "post",
        lambda *a, **k: pytest.fail("wrong credential role reached the claim route"),
    )
    with pytest.raises(pairing.PairingError, match="malformed"):
        pairing.claim_pairing(HOST, wrong_role, code_verifier="V" * 43)


def test_malformed_201_is_aborted_when_the_rollback_identity_is_available(
    monkeypatch,
) -> None:
    pairing_id = str(uuid4())
    endpoints: list[str] = []
    malformed = _claim_response(pairing_id)._body
    malformed.pop("ingest_token_id")

    def _post(url, **kwargs):
        endpoints.append(url.rsplit("/", 1)[-1])
        if url.endswith("/claim"):
            return _Response(201, malformed)
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        return _Response(200, {"revoked": True})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    with pytest.raises(pairing.PairingError, match="rolled back.*previous connection"):
        pairing.claim_pairing(HOST, SECRET)
    assert endpoints == ["claim", "abort"]
    assert store.load_credential(HOST) is None
    assert store.load_pairing_stage() is None


def test_claim_without_no_store_is_aborted(monkeypatch) -> None:
    pairing_id = str(uuid4())
    endpoints: list[str] = []
    claim = _claim_response(pairing_id)
    claim.headers = {}

    def _post(url, **kwargs):
        endpoints.append(url.rsplit("/", 1)[-1])
        if url.endswith("/claim"):
            return claim
        return _Response(200, {"revoked": True})

    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    with pytest.raises(pairing.PairingError, match="rolled back"):
        pairing.claim_pairing(HOST, SECRET)
    assert endpoints == ["claim", "abort"]
    assert store.load_credential(HOST) is None


def test_validation_requires_matching_14_day_headers_and_rolls_back(monkeypatch) -> None:
    pairing_id = str(uuid4())
    endpoints: list[str] = []

    def _post(url, **kwargs):
        endpoints.append(url.rsplit("/", 1)[-1])
        if url.endswith("/claim"):
            return _claim_response(pairing_id)
        if url.endswith("/abort"):
            return _Response(200, {"revoked": True})
        raise AssertionError("a malformed validation must not confirm")

    invalid_headers = _validation_response()
    invalid_headers.headers["x-openadapt-credential-warning-days"] = "30"
    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)
    monkeypatch.setattr(pairing.httpx, "post", _post)
    monkeypatch.setattr(pairing.httpx, "get", lambda *a, **k: invalid_headers)

    with pytest.raises(pairing.PairingError, match="lifetime contract"):
        pairing.claim_pairing(HOST, SECRET)
    assert endpoints == ["claim", "abort"]
    assert store.load_credential(HOST) is None
