"""Credential lifetime warnings and no-outage rotation contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from engine.auth import rotation, store

HOST = "https://app.openadapt.ai"
OLD_TOKEN = "oai_ingest_" + "A" * 43
NEW_TOKEN = "oai_ingest_" + "B" * 43
THIRD_TOKEN = "oai_ingest_" + "C" * 43
FOURTH_TOKEN = "oai_ingest_" + "D" * 43
PREVIOUS_ID = str(uuid4())
REPLACEMENT_ID = str(uuid4())


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


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _lifetime(days: int = 90) -> dict:
    return {
        "expires_at": _iso(days),
        "expires_in_days": days,
        "expiring_soon": days <= 14,
        "legacy_non_expiring": False,
        "warning_days": 14,
    }


def _rotation_response(
    *,
    token: str = NEW_TOKEN,
    previous_id: str = PREVIOUS_ID,
    replacement_id: str = REPLACEMENT_ID,
) -> _Response:
    lifetime = _lifetime(90)
    return _Response(
        201,
        {
            "token": token,
            "record": {
                "id": replacement_id,
                "org_id": "org_1",
                "name": "Desktop",
                "token_prefix": token[:20],
                "created_at": _iso(0),
                "last_used_at": None,
                "expires_at": lifetime["expires_at"],
                "revoked_at": None,
                "rotated_to_id": None,
            },
            "previous_id": previous_id,
            "previous_expires_at": _iso(7),
            "credential": lifetime,
        },
        {"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )


def _replacement_validation_response(
    credential: dict,
    *,
    days: int | None = None,
) -> _Response:
    current = dict(credential)
    if days is not None:
        current["expires_in_days"] = days
        current["expiring_soon"] = days < 14
    return _Response(
        200,
        {"count": 0, "credential": current},
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": str(current["expires_in_days"]),
        },
    )


def _credential(token: str, expires_at: float | None = None) -> dict:
    return {
        "kind": "ingest_token",
        "token": token,
        "refresh_token": None,
        "org_id": "org_1",
        "host": HOST,
        "expires_at": expires_at,
    }


def test_14_day_warning_requires_matching_header_and_body(monkeypatch) -> None:
    status_body = {"count": 0, "credential": _lifetime(14)}
    response = _Response(
        200,
        status_body,
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "14",
        },
    )
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: response)

    status = rotation.credential_status(HOST, OLD_TOKEN)

    assert status is not None
    assert status["expires_in_days"] == 14
    assert status["expiring_soon"] is True
    assert "expires in 14 days" in rotation.expiry_warning(status)


def test_status_rejects_a_wrong_credential_role_before_network(monkeypatch) -> None:
    monkeypatch.setattr(
        rotation.httpx,
        "get",
        lambda *a, **k: pytest.fail("wrong credential role reached Cloud status"),
    )
    assert rotation.credential_status(HOST, "oap_" + "A" * 43) is None


def test_day_14_accepts_server_authoritative_false_before_exact_boundary(monkeypatch) -> None:
    lifetime = _lifetime(14)
    lifetime["expiring_soon"] = False
    response = _Response(
        200,
        {"count": 0, "credential": lifetime},
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "14",
        },
    )
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: response)

    status = rotation.credential_status(HOST, OLD_TOKEN)

    assert status is not None
    assert status["expires_in_days"] == 14
    assert status["expiring_soon"] is False
    assert rotation.expiry_warning(status) is None


@pytest.mark.parametrize(
    ("days", "expiring"),
    [(13, False), (15, True)],
)
def test_warning_rejects_impossible_whole_day_state(days, expiring, monkeypatch) -> None:
    lifetime = _lifetime(days)
    lifetime["expiring_soon"] = expiring
    response = _Response(
        200,
        {"count": 0, "credential": lifetime},
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": str(days),
        },
    )
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: response)
    assert rotation.credential_status(HOST, OLD_TOKEN) is None


def test_warning_rejects_a_header_body_disagreement(monkeypatch) -> None:
    response = _Response(
        200,
        {"count": 0, "credential": _lifetime(14)},
        {
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "13",
        },
    )
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: response)
    assert rotation.credential_status(HOST, OLD_TOKEN) is None


def test_legacy_non_expiring_status_has_no_expiry_header(monkeypatch) -> None:
    response = _Response(
        200,
        {
            "count": 0,
            "credential": {
                "expires_at": None,
                "expires_in_days": None,
                "expiring_soon": False,
                "legacy_non_expiring": True,
                "warning_days": 14,
            },
        },
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
        },
    )
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: response)
    status = rotation.credential_status(HOST, OLD_TOKEN)
    assert status is not None
    assert "no renewal date" in rotation.expiry_warning(status)


def test_rotation_stages_then_atomically_promotes_the_one_time_replacement(
    monkeypatch,
) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    requests: list[tuple[str, dict, dict]] = []
    rotated = _rotation_response()

    def _post(url, *, json, headers, **kwargs):
        requests.append((url, json, headers))
        return rotated

    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", _post)
    monkeypatch.setattr(
        rotation.httpx,
        "get",
        lambda *a, **k: _replacement_validation_response(rotated.json()["credential"]),
    )

    replacement = rotation.rotate_credential(HOST)

    assert replacement["token"] == NEW_TOKEN
    assert isinstance(replacement["expires_at"], float)
    assert store.load_credential(HOST) == replacement
    assert store.load_rotation_stage() is None
    assert requests == [
        (
            f"{HOST}/api/ingest-tokens/rotate",
            {},
            {"Authorization": f"Bearer {OLD_TOKEN}"},
        )
    ]


def test_immediate_replacement_verification_still_requires_a_fresh_lifetime(
    monkeypatch,
) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    rotated = _rotation_response()
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", lambda *a, **k: rotated)
    monkeypatch.setattr(
        rotation.httpx,
        "get",
        lambda *a, **k: _replacement_validation_response(
            rotated.json()["credential"],
            days=1,
        ),
    )

    with pytest.raises(rotation.RotationError, match="incomplete verification contract"):
        rotation.rotate_credential(HOST)

    assert store.load_credential(HOST)["token"] == OLD_TOKEN
    assert store.load_rotation_stage() is not None


@pytest.mark.parametrize("field", ["previous_id", "record_id"])
def test_rotation_response_requires_production_uuid_ids(field) -> None:
    response = _rotation_response()
    body = response.json()
    if field == "previous_id":
        body["previous_id"] = "ingest_previous_1"
    else:
        body["record"]["id"] = "ingest_replacement_1"

    with pytest.raises(rotation.RotationError, match="credential id|credential record"):
        rotation._validated_rotation(body, response.headers)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_prefix", "oai_ingest_wrong"),
        ("last_used_at", "2026-07-28T00:00:00+00:00"),
        ("revoked_at", "2026-07-28T00:00:00+00:00"),
        ("rotated_to_id", str(uuid4())),
    ],
)
def test_rotation_response_rejects_a_noncurrent_record(field, value) -> None:
    response = _rotation_response()
    response.json()["record"][field] = value

    with pytest.raises(rotation.RotationError, match="credential record"):
        rotation._validated_rotation(response.json(), response.headers)


def test_rotation_response_rejects_extra_record_fields() -> None:
    response = _rotation_response()
    response.json()["record"]["token_hash"] = "must-not-cross-the-wire"

    with pytest.raises(rotation.RotationError, match="credential record"):
        rotation._validated_rotation(response.json(), response.headers)


def test_rotation_refuses_before_network_when_keychain_is_unavailable(monkeypatch) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    monkeypatch.setattr(rotation, "secure_store_available", lambda: False)
    monkeypatch.setattr(
        rotation.httpx,
        "post",
        lambda *a, **k: pytest.fail("rotation must not consume a response"),
    )
    with pytest.raises(rotation.RotationError, match="keychain"):
        rotation.rotate_credential(HOST)


def test_lost_rotation_response_preserves_old_token_and_requires_reconnect(
    monkeypatch,
) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(
        rotation.httpx,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ReadError("lost")),
    )

    with pytest.raises(rotation.RotationError, match="did not receive.*do not retry"):
        rotation.rotate_credential(HOST)
    assert store.load_credential(HOST)["token"] == OLD_TOKEN
    assert store.load_rotation_stage() is None


def test_stage_failure_preserves_old_token_and_does_not_recommend_replay(monkeypatch) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", lambda *a, **k: _rotation_response())
    monkeypatch.setattr(rotation, "stage_rotation_credential", lambda *a, **k: False)

    with pytest.raises(rotation.RotationError, match="Sign in again; do not retry"):
        rotation.rotate_credential(HOST)
    assert store.load_credential(HOST)["token"] == OLD_TOKEN


def test_rotation_rejects_a_one_time_response_without_no_store(monkeypatch) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    response = _rotation_response()
    response.headers = {}
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", lambda *a, **k: response)

    with pytest.raises(rotation.RotationError, match="Sign in again; do not retry"):
        rotation.rotate_credential(HOST)
    assert store.load_credential(HOST)["token"] == OLD_TOKEN
    assert store.load_rotation_stage() is None


def test_delayed_rotation_recovery_accepts_decreased_days_without_a_second_request(
    monkeypatch,
) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    response = _rotation_response().json()
    token, previous_id, expiry = rotation._validated_rotation(
        response,
        {"cache-control": "no-store"},
    )
    replacement = _credential(token, expiry)
    previous = store.snapshot_pairing_canonical(HOST)
    assert previous is not None
    assert store.stage_rotation_credential(previous_id, replacement, previous)
    monkeypatch.setattr(
        rotation.httpx,
        "post",
        lambda *a, **k: pytest.fail("recovery must not issue another rotation"),
    )
    monkeypatch.setattr(
        rotation.httpx,
        "get",
        lambda *a, **k: _replacement_validation_response(response["credential"], days=80),
    )

    recovered = rotation.rotate_credential(HOST)

    assert recovered == replacement
    assert store.load_credential(HOST) == replacement
    assert store.load_rotation_stage() is None


def test_bad_replacement_stays_staged_and_recovery_validates_without_reissuing(
    monkeypatch,
) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    rotated = _rotation_response()
    post_calls = 0

    def _post(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        return rotated

    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", _post)
    monkeypatch.setattr(rotation.httpx, "get", lambda *a, **k: _Response(401, {}))

    with pytest.raises(rotation.RotationError, match="did not accept.*old credential"):
        rotation.rotate_credential(HOST)

    assert post_calls == 1
    assert store.load_credential(HOST)["token"] == OLD_TOKEN
    assert store.load_rotation_stage() is not None

    monkeypatch.setattr(
        rotation.httpx,
        "get",
        lambda *a, **k: _replacement_validation_response(rotated.json()["credential"]),
    )
    recovered = rotation.rotate_credential(HOST)

    assert post_calls == 1
    assert recovered["token"] == NEW_TOKEN
    assert store.load_credential(HOST) == recovered
    assert store.load_rotation_stage() is None


def test_later_login_supersedes_rejected_stage_before_future_rotation(monkeypatch) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    rejected = _rotation_response()
    replacement = _rotation_response(
        token=FOURTH_TOKEN,
        previous_id=str(uuid4()),
        replacement_id=str(uuid4()),
    )
    posted_bearers: list[str] = []
    validated_bearers: list[str] = []

    def _post(url, *, headers, **kwargs):
        posted_bearers.append(headers["Authorization"])
        return rejected if len(posted_bearers) == 1 else replacement

    def _get(url, *, headers, **kwargs):
        bearer = headers["Authorization"]
        validated_bearers.append(bearer)
        if bearer == f"Bearer {NEW_TOKEN}":
            return _Response(401, {})
        assert bearer == f"Bearer {FOURTH_TOKEN}"
        return _replacement_validation_response(replacement.json()["credential"])

    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", _post)
    monkeypatch.setattr(rotation.httpx, "get", _get)

    with pytest.raises(rotation.RotationError, match="did not accept"):
        rotation.rotate_credential(HOST)
    assert store.load_rotation_stage() is not None
    assert store.load_credential(HOST)["token"] == OLD_TOKEN

    # A later browser/deep-link login writes a coherent third credential.
    store.store_credential(_credential(THIRD_TOKEN))

    rotated = rotation.rotate_credential(HOST)

    assert posted_bearers == [
        f"Bearer {OLD_TOKEN}",
        f"Bearer {THIRD_TOKEN}",
    ]
    assert validated_bearers == [
        f"Bearer {NEW_TOKEN}",
        f"Bearer {FOURTH_TOKEN}",
    ]
    assert rotated["token"] == FOURTH_TOKEN
    assert store.load_credential(HOST) == rotated
    assert store.load_rotation_stage() is None


def test_second_rotation_409_requires_a_new_login(monkeypatch) -> None:
    store.store_credential(_credential(OLD_TOKEN))
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(rotation.httpx, "post", lambda *a, **k: _Response(409, {}))
    with pytest.raises(rotation.RotationError, match="already renewed.*login"):
        rotation.rotate_credential(HOST)


@pytest.mark.parametrize(
    "wrong_role",
    [
        "supabase_authorization_code",
        "oap_" + "A" * 43,
        "oapp_" + "A" * 43,
        "oaps_" + "A" * 43,
    ],
)
def test_rotation_rejects_every_non_ingest_credential_before_network(
    wrong_role, monkeypatch
) -> None:
    store.store_credential(_credential(wrong_role))
    monkeypatch.setattr(rotation, "secure_store_available", lambda: True)
    monkeypatch.setattr(
        rotation.httpx,
        "post",
        lambda *a, **k: pytest.fail("wrong credential role reached Cloud rotation"),
    )
    with pytest.raises(rotation.RotationError, match="no stored connection"):
        rotation.rotate_credential(HOST)
