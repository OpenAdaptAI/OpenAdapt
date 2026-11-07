"""``verifier.session`` renews read-back credentials for FHIR and GraphQL too.

A static token can expire during a long qualification campaign; a session hook
is called again for every trial's read-back, so the next read uses a fresh
token instead of replaying the expired one.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.qualify.config import SystemOfRecordConfig
from openadapt_flow.qualify.session import verifier_factory


class _Response:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _Authority:
    """Issues one bearer token at a time; expiring it refuses every read."""

    def __init__(self) -> None:
        self.issued = 0
        self.current: Optional[str] = None

    def mint(self) -> str:
        self.issued += 1
        self.current = f"token-{self.issued}"
        return self.current

    def expire(self) -> None:
        self.current = None


class _Session:
    """A ``requests``-style client bound to the token it was opened with."""

    def __init__(self, authority: _Authority, token: str) -> None:
        self.authority = authority
        self.token = token

    def _authorized(self) -> bool:
        return self.token == self.authority.current

    def get(self, url: str, **_kwargs: Any) -> _Response:
        if not self._authorized():
            return _Response(401)
        return _Response(
            200,
            {
                "resourceType": "Bundle",
                "entry": [{"resource": {"id": "p1", "name": "Ada"}}],
            },
        )

    def post(self, url: str, **_kwargs: Any) -> _Response:
        if not self._authorized():
            return _Response(401)
        return _Response(200, {"data": {"patients": [{"id": "p1", "name": "Ada"}]}})


@pytest.fixture
def authority(monkeypatch) -> _Authority:
    authority = _Authority()
    module = types.ModuleType("fake_sor_auth")

    def open_session(_config: Any) -> _Session:
        return _Session(authority, authority.mint())

    module.open_session = open_session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_sor_auth", module)
    return authority


_VERIFIERS: dict[str, dict[str, Any]] = {
    "fhir": {
        "kind": "fhir",
        "base_url": "https://sor.invalid/fhir",
        "resource_type": "Patient",
        "field_paths": {"name": "name"},
    },
    "graphql": {
        "kind": "graphql",
        "base_url": "https://sor.invalid/graphql",
        "graphql_query": "{ patients { id name } }",
        "graphql_records_path": "data.patients",
    },
}


@pytest.mark.parametrize("kind", sorted(_VERIFIERS))
def test_session_hook_renews_an_expired_token_for_each_read(authority, kind):
    sor = SystemOfRecordConfig(
        session="fake_sor_auth:open_session",
        verifier=EffectsConfig(**_VERIFIERS[kind]),
    )
    make_verifier = verifier_factory(SimpleNamespace(system_of_record=sor))

    first = make_verifier({})
    before = first.capture_pre_state()
    assert before.reachable is True
    assert [record["id"] for record in before.records] == ["p1"]

    # The token expires mid-run: the client it was issued to can't read any
    # more, which is what a static token would leave every later trial with.
    authority.expire()
    assert first.capture_pre_state().reachable is False

    # The next trial's read-back gets a freshly authenticated client.
    second = make_verifier({})
    after = second.capture_pre_state()
    assert after.reachable is True
    assert [record["id"] for record in after.records] == ["p1"]
    assert authority.issued == 2


def test_session_hook_is_still_refused_for_a_kind_that_cannot_use_it():
    with pytest.raises(ValueError, match="verifier.session is supported for kind"):
        SystemOfRecordConfig(
            session="fake_sor_auth:open_session",
            verifier=EffectsConfig(kind="sql", sqlite_database="/nonexistent.db"),
        )


def test_build_effect_verifier_refuses_a_session_it_would_ignore():
    with pytest.raises(ValueError, match="injected read-back session"):
        build_effect_verifier(
            EffectsConfig(kind="sql", sqlite_database="/nonexistent.db"),
            session=object(),
        )


def test_rest_session_hook_uses_shared_paged_parameterized_builder(monkeypatch):
    module = types.ModuleType("fake_paged_sor_auth")

    class PagedSession:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, **_kwargs: Any) -> _Response:
            self.urls.append(url)
            if url.endswith("/records/a%20b"):
                return _Response(
                    200,
                    {
                        "items": [{"id": "a", "secret": "x"}],
                        "meta": {"total": 2, "next": "?page=2"},
                    },
                )
            return _Response(
                200,
                {"items": [{"id": "b", "secret": "y"}], "meta": {"total": 2}},
            )

    session = PagedSession()
    module.open_session = lambda _config: session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_paged_sor_auth", module)
    sor = SystemOfRecordConfig(
        session="fake_paged_sor_auth:open_session",
        verifier=EffectsConfig(
            kind="rest",
            base_url="https://sor.invalid/api",
            records_path="/records/{record}",
            path_params={"record": {"param": "record_id"}},
            records_key="items",
            records_next_path="meta.next",
            records_total_path="meta.total",
            records_complete=True,
            records_drop_fields=["secret"],
        ),
    )

    verifier = verifier_factory(SimpleNamespace(system_of_record=sor))(
        {"record_id": "a b"}
    )
    before = verifier.capture_pre_state()

    assert before.reachable is True
    assert [record["id"] for record in before.records] == ["a", "b"]
    assert all("secret" not in record for record in before.records)
    assert session.urls == [
        "https://sor.invalid/api/records/a%20b",
        "https://sor.invalid/api/records/a%20b?page=2",
    ]
    assert verifier.timeout_s == 10.0
    assert verifier.next_path == "meta.next"
    assert verifier.total_path == "meta.total"
    assert verifier.records_complete is True


_REST: dict[str, Any] = {
    "kind": "rest",
    "base_url": "https://sor.invalid/api",
    "records_path": "/patients",
}


@pytest.mark.parametrize("kind", [*sorted(_VERIFIERS), "rest"])
def test_a_session_hook_that_opens_no_client_is_refused_before_any_read(
    monkeypatch, kind
):
    import httpx

    from openadapt_flow.qualify.config import QualifyConfigError

    module = types.ModuleType("fake_sor_no_client")
    module.open_session = lambda _config: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_sor_no_client", module)

    def no_anonymous_client(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an anonymous client was opened")

    monkeypatch.setattr(httpx, "Client", no_anonymous_client)
    spec = _REST if kind == "rest" else _VERIFIERS[kind]
    sor = SystemOfRecordConfig(
        session="fake_sor_no_client:open_session",
        verifier=EffectsConfig(**spec),
    )
    make_verifier = verifier_factory(SimpleNamespace(system_of_record=sor))

    with pytest.raises(QualifyConfigError, match="returned no client"):
        make_verifier({})
