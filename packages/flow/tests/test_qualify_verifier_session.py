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


_VERIFIERS = {
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
