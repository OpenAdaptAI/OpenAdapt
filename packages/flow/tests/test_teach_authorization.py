"""Teach authorizations: a person signs the scope a teaching agent gets.

The authorizer is a real approver key on a real signed approver roster
(``LocalAuthorizer`` holds the key in memory); verification is the approver
verifier's own. Nothing here contacts the network. All data is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

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
    monkeypatch.delenv("OPENADAPT_APPROVER_ROOT", raising=False)


@pytest.fixture
def person(tmp_path) -> LocalAuthorizer:
    return LocalAuthorizer(tmp_path / "keys", "front-desk")


def _issue(tmp_path: Path, person: LocalAuthorizer, **kw: Any) -> Path:
    spec = encounter_authorization_spec(BASE, patient_id="P1", note=NOTE, **kw)
    return person.issue(tmp_path / "auth", spec)


def _require(person: LocalAuthorizer, statement: Path, **kw: Any) -> ta.Verification:
    kw.setdefault("purpose", "teach")
    return ta.require(statement, key_dir=person.key_dir, root=person.root, **kw)


def _code(fn, *args, **kw) -> str:
    with pytest.raises(ta.TeachAuthorizationError) as err:
        fn(*args, **kw)
    return err.value.code


def test_a_signed_authorization_binds_the_scope(tmp_path, person):
    statement = _issue(tmp_path, person)
    result = _require(person, statement)
    assert result.signer == "front-desk"
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


def test_the_roster_root_must_be_pinned(tmp_path, person):
    statement = _issue(tmp_path, person)
    assert (
        _code(ta.require, statement, purpose="teach", key_dir=person.key_dir, root=None)
        == "teach_auth_identity_unverified"
    )
    other = LocalAuthorizer(tmp_path / "other-keys", "front-desk")
    assert (
        _code(
            ta.require,
            statement,
            purpose="teach",
            key_dir=person.key_dir,
            root=other.root,
        )
        == "teach_auth_identity_unverified"
    )
    # A key directory with no roster vouches for nobody.
    assert (
        _code(
            ta.require,
            statement,
            purpose="teach",
            key_dir=tmp_path / "empty",
            root=person.root,
        )
        == "teach_auth_identity_unverified"
    )


def test_an_unknown_key_refuses(tmp_path, person):
    stranger = LocalAuthorizer(person.key_dir, "night-desk", register=False)
    forged = stranger.issue(
        tmp_path / "forged",
        encounter_authorization_spec(BASE, patient_id="P2", note=NOTE),
    )
    assert _code(_require, person, forged) == "teach_auth_unknown_signer"


def test_a_key_signing_as_another_approver_refuses(tmp_path, person):
    # The agent makes its own key and signs in the root approver's name.
    agent = LocalAuthorizer(person.key_dir, "agent-key", register=False)
    forged = agent.issue(
        tmp_path / "forged",
        encounter_authorization_spec(BASE, patient_id="P2", note=NOTE),
        authorizer="front-desk",
    )
    assert _code(_require, person, forged) == "teach_auth_bad_signature"


def test_a_revoked_key_refuses(tmp_path, person):
    nurse = LocalAuthorizer(person.key_dir, "night-desk", added_by=person)
    statement = nurse.issue(
        tmp_path / "auth",
        encounter_authorization_spec(BASE, patient_id="P1", note=NOTE),
    )
    recorded = ta.record(_require(person, statement))
    person.remove(nurse)
    assert _code(_require, person, statement) == "teach_auth_unknown_signer"
    # Admission checks the roster as it is now, so revoking after teaching refuses.
    assert (
        _code(
            ta.check_admission,
            recorded,
            statement,
            key_dir=person.key_dir,
            root=person.root,
        )
        == "teach_auth_unknown_signer"
    )


def test_an_old_roster_put_back_refuses(tmp_path, person):
    roster_file = person.key_dir / "approvers.json"
    before = roster_file.read_bytes()
    nurse = LocalAuthorizer(person.key_dir, "night-desk", added_by=person)
    person.remove(nurse)
    statement = _issue(tmp_path, person)
    roster_file.write_bytes(before)  # the head sidecar still names the newer roster
    assert _code(_require, person, statement) == "teach_auth_identity_unverified"


def test_an_attestation_labelled_for_the_portal_is_refused(tmp_path):
    portal = LocalAuthorizer(tmp_path / "keys", "portal", method="portal_key")
    statement = portal.issue(
        tmp_path / "auth",
        encounter_authorization_spec(BASE, patient_id="P1", note=NOTE),
    )
    assert _code(_require, portal, statement) == "teach_auth_method"


def test_an_edited_statement_fails(tmp_path, person):
    statement = _issue(tmp_path, person)
    data = json.loads(statement.read_text())
    data["writes"]["identity"]["patient_id"]["value"] = "P2"
    statement.write_text(json.dumps(data))
    assert _code(_require, person, statement) == "teach_auth_bad_signature"


def test_an_unsigned_statement_fails(tmp_path, person):
    statement = _issue(tmp_path, person)
    statement.with_name(ta.SIGNATURE_FILE).unlink()
    assert _code(_require, person, statement) == "teach_auth_unsigned"


def test_sign_with_a_software_key_file(tmp_path, monkeypatch):
    """No Secure Enclave: an encrypted key file, added to the roster, signs."""

    from openadapt_flow.approval_mode import approver_roster as roster
    from openadapt_flow.approval_mode.human_attestation import load_signer
    from openadapt_flow.jit.authority import (
        KEY_PASSPHRASE_ENV,
        generate_encrypted_key,
        load_encrypted_key,
    )

    env = {KEY_PASSPHRASE_ENV: "synthetic-passphrase"}
    keys = tmp_path / "keys"
    root_path, mine_path = tmp_path / "root.pem", tmp_path / "mine.pem"
    root_public = generate_encrypted_key(root_path, env=env)
    root_key = load_encrypted_key(root_path, env=env)
    roster.init_root(
        keys,
        principal="admin",
        public_key=root_public,
        sign=roster.file_key_signer(root_key),
    )
    mine = generate_encrypted_key(mine_path, env=env)
    roster.add_approver(
        keys,
        root_public,
        principal="clinic-lead",
        public_key=mine,
        signed_by="admin",
        sign=roster.file_key_signer(root_key),
    )
    spec = encounter_authorization_spec(BASE, patient_id="P1", note=NOTE)
    statement = ta.create(
        tmp_path / "auth", spec, authorizer="clinic-lead", authorizer_public_key=mine
    )
    # A key file for another approver can't sign it.
    admin = load_signer(root_path, keys, method="person_key", env=env, root=root_public)
    assert _code(ta.sign, statement, admin) == "teach_auth_signer_mismatch"
    signer = load_signer(
        mine_path, keys, method="person_key", env=env, root=root_public
    )
    ta.sign(statement, signer)
    assert _code(ta.sign, statement, signer) == "teach_auth_already_signed"
    result = ta.require(statement, purpose="teach", key_dir=keys, root=root_public)
    assert (
        result.ok and result.signer == "clinic-lead" and result.method == "person_key"
    )


def test_purpose_task_and_expiry(tmp_path, person):
    statement = _issue(tmp_path, person, purposes=("teach",))
    assert _code(_require, person, statement, purpose="repair") == "teach_auth_purpose"
    assert (
        _code(_require, person, statement, task_id="other")
        == "teach_auth_task_mismatch"
    )
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert _code(_require, person, statement, now=later) == "teach_auth_expired"


def test_digest_bound_inputs(tmp_path, person):
    spec = encounter_authorization_spec(
        BASE, patient_id="P1", note_sha256=ta.value_sha256(NOTE)
    )
    statement = person.issue(tmp_path / "auth", spec)
    assert NOTE not in statement.read_text()
    result = _require(person, statement)
    assert dict(ta.bind(result, {"note": NOTE}).fields)["note"] == NOTE
    for params, code in (
        ({}, "teach_auth_input_missing"),
        ({"note": "cleared for discharge"}, "teach_auth_input_mismatch"),
        ({"note": NOTE, "extra": "x"}, "teach_auth_input_not_declared"),
    ):
        assert _code(ta.bind, result, params) == code


def test_a_scope_from_flags_may_narrow_but_not_widen(tmp_path, person):
    statement = _issue(tmp_path, person)
    authorized = ta.bind(_require(person, statement))
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
        assert _code(ta.check_within, widened, authorized) == "teach_auth_scope_widened"


def test_admission_reverifies_the_recorded_digest(tmp_path, person):
    statement = _issue(tmp_path, person)
    recorded = ta.record(_require(person, statement))
    copy = ta.copy_into(statement, tmp_path / "draft")
    trust = {"key_dir": person.key_dir, "root": person.root}
    assert ta.check_admission(recorded, copy, **trust).ok
    assert _code(ta.check_admission, None, copy, **trust) == "teach_auth_not_recorded"
    assert _code(ta.check_admission, recorded, None, **trust) == "teach_auth_missing"
    assert (
        _code(ta.check_admission, recorded, copy, key_dir=person.key_dir, root=None)
        == "teach_auth_identity_unverified"
    )
    other = _issue(tmp_path / "second", person)
    assert (
        _code(ta.check_admission, recorded, other, **trust)
        == "teach_auth_digest_mismatch"
    )
    assert (
        _code(
            ta.check_admission, recorded, copy, purpose="repair", task_id="x", **trust
        )
        == "teach_auth_task_mismatch"
    )
    assert (
        _code(ta.check_admission, recorded, copy, not_signed_by="front-desk", **trust)
        == "teach_auth_agent_signer"
    )
    # A different statement can't replace the kept copy.
    assert (
        _code(ta.copy_into, other, tmp_path / "draft") == "teach_auth_digest_mismatch"
    )


def test_statement_rules():
    base = encounter_authorization_spec(BASE, patient_id="P1", note=NOTE)
    fields = {
        "schema": ta.SCHEMA,
        "authorizer": "front-desk",
        "authorizer_public_key": "a" * 64,
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
        {**base, "authorizer_public_key": "github:someone"},
    ]
    for spec in bad:
        with pytest.raises(ValueError):
            ta.TeachAuthorizationV1.model_validate({**fields, **spec})


# -- the guard enforces the forbidden list -----------------------------------------


PAGE = """<!doctype html><title>Chart</title><body>
<input id="note" aria-label="Note">
<button id="save">Save encounter</button>
<button id="del"><span>  delete   ENCOUNTER </span></button>
<button id="icon" aria-label="Delete encounter">X</button>
<div id="plain">just text</div>
</body>"""

#: A page that lies to any script asking what a control is called.
LYING = PAGE.replace(
    "</body>",
    "<script>Element.prototype.getAttribute = () => 'Save encounter';"
    "Element.prototype.closest = function () { return this; };"
    "Object.defineProperty(HTMLElement.prototype, 'innerText', "
    "{get() { return 'Save encounter'; }});</script></body>",
)


@pytest.fixture
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser installed
            pytest.skip(f"no Chromium: {exc}")
        page = browser.new_page()
        yield page
        browser.close()


def _guard(tmp_path: Path, person: LocalAuthorizer) -> TeachGuard:
    statement = _issue(tmp_path, person)
    return TeachGuard(ta.bind(_require(person, statement)))


def _at(page, selector: str) -> Op:
    box = page.locator(selector).bounding_box()
    return Op("click", x=int(box["x"] + box["width"] / 2), y=int(box["y"] + 4))


def _violation(fn, *args, **kw) -> str:
    with pytest.raises(ScopeViolation) as err:
        fn(*args, **kw)
    return err.value.code


def test_forbidden_controls_are_read_from_the_browser(tmp_path, person, chromium):
    guard = _guard(tmp_path, person)
    for html in (PAGE, LYING):
        chromium.set_content(html)
        guard.check_ops([_at(chromium, "#save")], page=chromium)
        guard.check_ops([_at(chromium, "#plain")], page=chromium)
        # Its visible text, and an icon button's accessible name.
        for forbidden in ("#del", "#icon"):
            code = _violation(
                guard.check_ops, [_at(chromium, forbidden)], page=chromium
            )
            assert code == "control_forbidden", (html is LYING, forbidden)
    # A click with no position can't be checked.
    assert (
        _violation(guard.check_ops, [Op("click")], page=chromium) == "control_forbidden"
    )


def test_a_key_that_reaches_a_forbidden_control_is_refused(tmp_path, person, chromium):
    guard = _guard(tmp_path, person)
    for html in (PAGE, LYING):
        chromium.set_content(html)
        chromium.focus("#note")
        guard.check_key(Op("type", text=NOTE), chromium)
        chromium.keyboard.press("Tab")  # to Save encounter
        guard.check_key(Op("press", key="Enter"), chromium)
        chromium.keyboard.press("Tab")  # to the forbidden one
        for op in (
            Op("press", key="Enter"),
            Op("press", key="Space"),
            Op("press", key="a"),
            Op("hotkey", key="Enter", modifiers=("Shift",)),
            Op("type", text=NOTE),
        ):
            assert _violation(guard.check_key, op, chromium) == "control_forbidden"
    # Nothing to read the focus from.
    assert _violation(guard.check_key, Op("press", key="Enter"), None) == (
        "control_forbidden"
    )


def test_a_key_in_a_frame_or_a_form_is_checked_where_it_lands(
    tmp_path, person, chromium
):
    guard = _guard(tmp_path, person)
    # Focus inside a frame.
    chromium.set_content(
        '<iframe srcdoc="<button>Delete encounter</button><button>Save</button>">'
        "</iframe>"
    )
    frame = chromium.frame_locator("iframe")
    frame.locator("button", has_text="Delete encounter").focus()
    assert _violation(guard.check_key, Op("press", key="Enter"), chromium) == (
        "control_forbidden"
    )
    frame.locator("button", has_text="Save").focus()
    guard.check_key(Op("press", key="Enter"), chromium)
    # Enter in a field sends the form with its default button.
    chromium.set_content(
        "<form><input aria-label=Note><button>Delete encounter</button>"
        "<button>Save</button></form>"
    )
    chromium.focus("input")
    guard.check_key(Op("type", text=NOTE), chromium)
    for op in (Op("press", key="Enter"), Op("type", text=NOTE + "\n")):
        assert _violation(guard.check_key, op, chromium) == "control_forbidden"
    chromium.set_content(
        "<form><input aria-label=Note><button>Save</button>"
        "<button>Delete encounter</button></form>"
    )
    chromium.focus("input")
    guard.check_key(Op("press", key="Enter"), chromium)  # Save is the default
    # The browser's own default button, not a re-implementation of it.
    for html in (
        # A button in a shadow root isn't the form's.
        "<script>customElements.define('x-b', class extends HTMLElement {"
        "constructor(){super(); this.attachShadow({mode:'open'}).innerHTML="
        "'<button>Save</button>';}});</script>"
        "<form><input aria-label=Note><x-b></x-b><button>Delete encounter</button>"
        "</form>",
        # Nor is one in a frame inside it.
        '<form><input aria-label=Note><iframe srcdoc="<button>Save</button>">'
        "</iframe><button>Delete encounter</button></form>",
        # An unknown type is a submit button.
        "<form><input aria-label=Note><button type=foo>Delete encounter</button>"
        "<button>Save</button></form>",
    ):
        chromium.set_content(html)
        chromium.focus("input")
        assert _violation(guard.check_key, Op("press", key="Enter"), chromium) == (
            "control_forbidden"
        ), html
    # A key chord can activate a control by its access key.
    assert _violation(
        guard.check_key, Op("hotkey", key="d", modifiers=("Alt",)), chromium
    ) == ("control_forbidden")
    # A control with no role, and a label for a forbidden control.
    chromium.set_content(
        '<div id=d onclick="this.dataset.x=1">Delete encounter</div>'
        "<label id=l for=c>Confirm</label>"
        "<input type=checkbox id=c aria-label='Delete encounter'>"
    )
    for selector in ("#d", "#l"):
        assert _violation(
            guard.check_ops, [_at(chromium, selector)], page=chromium
        ) == ("control_forbidden"), selector
    # aria-labelledby: the accessible name, not the visible text.
    chromium.set_content(
        '<button aria-labelledby="l">Open</button><span id="l">Delete encounter</span>'
    )
    assert _violation(guard.check_ops, [_at(chromium, "button")], page=chromium) == (
        "control_forbidden"
    )
    # No page to read from.
    assert _violation(guard.check_ops, [Op("click", x=1, y=1)], page=None) == (
        "control_forbidden"
    )


def test_forbidden_actions(tmp_path, person):
    guard = _guard(tmp_path, person)
    assert _violation(guard.check_ops, [Op("drag", x=1, y=1, end_x=2, end_y=2)]) == (
        "forbidden_action"
    )
    assert (
        guard.scope.request_refusal("DELETE", BASE + "/api/encounter/1", None)
        == "forbidden_action"
    )


def test_render_shows_what_is_signed(tmp_path, person):
    statement = _issue(tmp_path, person)
    text = ta.render(_require(person, statement))
    assert "patient_id = 'P1'" in text
    assert "Delete encounter" in text
    assert BASE in text
