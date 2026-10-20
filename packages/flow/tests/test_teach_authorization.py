"""Teach authorizations: a person signs the scope a teaching agent gets.

The authorizer is a throwaway in-memory key (``LocalAuthorizer``); GitHub's
``.keys`` answer is a stand-in function. Nothing here contacts the network.
All data is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import ssh_sig
from openadapt_flow import teach_authorization as ta
from openadapt_flow.jit.adapters import Op
from openadapt_flow.mockmed.teach_scope import (
    EncounterWrites,
    encounter_authorization_spec,
    encounter_scope,
)
from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
from openadapt_flow.teach_scope import ScopeViolation, TeachGuard, TeachScope

BASE = "http://127.0.0.1:8765"
NOTE = "Synthetic follow-up in two weeks"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(ta.AUTHORIZERS_ENV, raising=False)


@pytest.fixture
def person() -> LocalAuthorizer:
    return LocalAuthorizer()


def _issue(tmp_path: Path, person: LocalAuthorizer, **kw: Any) -> tuple[Path, Path]:
    spec = encounter_authorization_spec(BASE, patient_id="P1", note=NOTE, **kw)
    statement = person.issue(tmp_path / "auth", spec)
    pinned = person.pin(tmp_path / "operator" / "teach-authorizers")
    return statement, pinned


def test_a_signed_authorization_binds_the_scope(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    result = ta.require(statement, purpose="teach", authorizers=pinned)
    assert result.identity_verified
    scope = ta.bind(result, {"note": NOTE})
    assert scope.authorized
    assert dict(scope.record) == {"patient_id": "P1"}
    assert dict(scope.fields) == {"type": "Triage", "note": NOTE}
    assert scope.origins == (BASE,)
    assert isinstance(scope.write_adapter, EncounterWrites)
    assert "delete_request" in scope.forbidden_actions
    assert scope.authorization["sha256"] == result.sha256
    # The report names the authorization but holds no values.
    report = json.dumps(scope.to_dict())
    assert NOTE not in report and result.sha256 in report


def test_identity_must_be_pinned_or_live(tmp_path, person):
    statement, _ = _issue(tmp_path, person)
    # The snapshot beside the statement is written by whoever writes the
    # files, so it never verifies identity.
    assert ta.verify(statement).ok and not ta.verify(statement).identity_verified
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="teach")
    assert err.value.code == "teach_auth_identity_unverified"
    # Live: github.com lists the key.
    live = lambda user: [ssh_sig.key_line(person.raw)]  # noqa: E731
    assert ta.require(statement, purpose="teach", live=live).identity_verified
    # Live: github.com does not list it.
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="teach", live=lambda user: [])
    assert err.value.code == "teach_auth_not_on_github"


def test_an_agent_cannot_sign_its_own_authorization(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    agent = LocalAuthorizer(handle=person.handle)  # same handle, its own key
    forged = agent.issue(
        tmp_path / "forged",
        encounter_authorization_spec(BASE, patient_id="P2", note=NOTE),
    )
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(forged, purpose="teach", authorizers=pinned)
    assert err.value.code == "teach_auth_unknown_signer"


def test_an_edited_statement_fails(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    data = json.loads(statement.read_text())
    data["writes"]["identity"]["patient_id"]["value"] = "P2"
    statement.write_text(json.dumps(data))
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="teach", authorizers=pinned)
    assert err.value.code == "teach_auth_bad_signature"


def test_purpose_task_and_expiry(tmp_path, person):
    statement, pinned = _issue(tmp_path, person, purposes=("teach",))
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="repair", authorizers=pinned)
    assert err.value.code == "teach_auth_purpose"
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="teach", authorizers=pinned, task_id="other")
    assert err.value.code == "teach_auth_task_mismatch"
    later = datetime.now(timezone.utc) + timedelta(days=2)
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.require(statement, purpose="teach", authorizers=pinned, now=later)
    assert err.value.code == "teach_auth_expired"


def test_digest_bound_inputs(tmp_path, person):
    spec = encounter_authorization_spec(
        BASE, patient_id="P1", note_sha256=ta.value_sha256(NOTE)
    )
    statement = person.issue(tmp_path / "auth", spec)
    assert NOTE not in statement.read_text()
    result = ta.require(
        statement, purpose="teach", authorizers=person.pin(tmp_path / "pin")
    )
    assert dict(ta.bind(result, {"note": NOTE}).fields)["note"] == NOTE
    for params, code in (
        ({}, "teach_auth_input_missing"),
        ({"note": "cleared for discharge"}, "teach_auth_input_mismatch"),
        ({"note": NOTE, "extra": "x"}, "teach_auth_input_not_declared"),
    ):
        with pytest.raises(ta.TeachAuthorizationError) as err:
            ta.bind(result, params)
        assert err.value.code == code


def test_a_scope_from_flags_may_narrow_but_not_widen(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    authorized = ta.bind(ta.require(statement, purpose="teach", authorizers=pinned))
    ta.check_within(encounter_scope(BASE, patient_id="P1", note=NOTE), authorized)
    for widened in (
        encounter_scope(BASE, patient_id="P2", note=NOTE),
        encounter_scope(BASE, patient_id="P1", note="cleared for discharge"),
        encounter_scope(BASE, patient_id="P1", note=NOTE, constants=("x",)),
        TeachScope(
            task_id="mockmed.add_note",
            origins=(BASE, "http://127.0.0.1:9999"),
            record={"patient_id": "P1"},
            fields={"type": "Triage", "note": NOTE},
        ),
    ):
        with pytest.raises(ta.TeachAuthorizationError) as err:
            ta.check_within(widened, authorized)
        assert err.value.code == "teach_auth_scope_widened"


def test_admission_reverifies_the_recorded_digest(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    result = ta.require(statement, purpose="teach", authorizers=pinned)
    recorded = ta.record(result)
    draft = tmp_path / "draft"
    copy = ta.copy_into(statement, draft)
    assert ta.check_admission(recorded, copy, authorizers=pinned).ok
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.check_admission(None, copy, authorizers=pinned)
    assert err.value.code == "teach_auth_not_recorded"
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.check_admission(recorded, copy)
    assert err.value.code == "teach_auth_identity_unverified"
    other, _ = _issue(tmp_path / "second", person)
    with pytest.raises(ta.TeachAuthorizationError) as err:
        ta.check_admission(recorded, other, authorizers=pinned)
    assert err.value.code == "teach_auth_digest_mismatch"


def test_statement_rules():
    base = encounter_authorization_spec(BASE, patient_id="P1", note=NOTE)
    fields = {
        "schema": ta.SCHEMA,
        "authorizer": "github:someone",
        "signer_fingerprint": "SHA256:" + "A" * 43,
        "issued_at": "2026-10-20T00:00:00Z",
        "nonce": "a" * 64,
    }
    ta.TeachAuthorizationV1.model_validate({**base, **fields})
    bad = [
        {**base, "origins": ["http://127.0.0.1:8765/path"]},
        {**base, "inputs": {"note": {"value": NOTE, "sha256": "a" * 64}}},
        {**base, "writes": {**base["writes"], "identity": {}}},
        {**base, "writes": {**base["writes"], "fields": {"note": {"input": "nope"}}}},
        {**base, "forbidden": {"actions": ["teleport"]}},
        {**base, "purposes": ["run"]},
        {**base, "constants": [{"sha256": "a" * 64}]},
        {**base, "surprise": True},
    ]
    for spec in bad:
        with pytest.raises(ValueError):
            ta.TeachAuthorizationV1.model_validate({**spec, **fields})


# -- the guard enforces the forbidden list -----------------------------------------


class _Page:
    """Enough of a Playwright page for the guard's probes."""

    url = BASE + "/"

    def __init__(self, label: str = "", link: Any = None) -> None:
        self.label, self.link = label, link

    def evaluate(self, script: str, args: Any) -> Any:
        return self.label if "aria-label" in script else self.link


def _guard(tmp_path: Path, person: LocalAuthorizer) -> TeachGuard:
    statement, pinned = _issue(tmp_path, person)
    return TeachGuard(ta.bind(ta.require(statement, purpose="teach", authorizers=pinned)))


def test_forbidden_controls_and_actions(tmp_path, person):
    guard = _guard(tmp_path, person)
    with pytest.raises(ScopeViolation) as err:
        guard.check_ops([Op("click", x=1, y=1)], page=_Page("  delete   ENCOUNTER "))
    assert err.value.code == "control_forbidden"
    guard.check_ops([Op("click", x=1, y=1)], page=_Page("Save encounter"))
    with pytest.raises(ScopeViolation) as err:
        guard.check_ops([Op("drag", x=1, y=1, end_x=2, end_y=2)])
    assert err.value.code == "forbidden_action"
    assert (
        guard.scope.request_refusal("DELETE", BASE + "/api/encounter/1", None)
        == "forbidden_action"
    )


def test_render_shows_what_is_signed(tmp_path, person):
    statement, pinned = _issue(tmp_path, person)
    text = ta.render(ta.require(statement, purpose="teach", authorizers=pinned))
    assert "patient_id = 'P1'" in text
    assert "Delete encounter" in text
    assert BASE in text
