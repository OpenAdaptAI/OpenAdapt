"""The exception card and answer contracts: closed sets, sealed ids, expiry shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_types import exception_card as ec

OPENED = "2026-11-21T12:00:00Z"
DEADLINE = "2026-11-21T12:15:00Z"


def _body(**overrides):
    body = {
        "schema": ec.EXCEPTION_CARD_SCHEMA,
        "run_ref": "run-1",
        "task_id": None,
        "kind": "second_factor",
        "opened_at": OPENED,
        "deadline_at": DEADLINE,
        "screenshot_ref": None,
        "summary": "The app is asking for a one-time code.",
        "choices": [
            {"id": "done_by_person", "label": "I entered it", "consequence": "The run goes on."},
            {"id": "abort", "label": "Stop", "consequence": "Nothing more is saved."},
        ],
    }
    body.update(overrides)
    return body


def _card(**overrides):
    body = _body(**overrides)
    return ec.ExceptionCardV1.model_validate({**body, "card_id": ec.card_digest(body)})


def test_the_closed_sets():
    assert ec.EXCEPTION_KINDS == (
        "second_factor",
        "unexpected_dialog",
        "session_expired",
        "timeout_or_stall",
        "possible_duplicate",
        "ambiguous_record",
        "cannot_complete",
    )
    assert ec.EXCEPTION_ANSWERS == ("continue", "skip", "decline", "done_by_person", "abort")
    assert ec.PROCEED_ANSWERS == {"continue", "done_by_person"}


def test_a_card_round_trips_with_its_schema_name():
    card = _card()
    wire = card.wire()
    assert wire["schema"] == "openadapt.exception-card/v1"
    assert ec.ExceptionCardV1.model_validate(wire) == card
    assert card.offers("abort") and not card.offers("continue")


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "captcha"},
        {"summary": "x" * 201},
        {"summary": ""},
        {"run_ref": "../up"},
        {"opened_at": "yesterday"},
        {"screenshot_ref": "/etc/passwd"},
        {"choices": [{"id": "approve", "label": "x", "consequence": "y"}]},
        {"choices": []},
    ],
)
def test_anything_outside_the_contract_is_refused(overrides):
    with pytest.raises(ValidationError):
        _card(**overrides)


def test_a_card_whose_id_isnt_its_digest_is_refused():
    body = _body()
    with pytest.raises(ValidationError):
        ec.ExceptionCardV1.model_validate({**body, "card_id": "sha256:" + "0" * 64})


def test_a_card_offers_each_answer_once():
    twice = [{"id": "abort", "label": "a", "consequence": "b"}] * 2
    with pytest.raises(ValidationError):
        _card(choices=twice)


def test_an_unknown_field_is_refused():
    body = _body()
    body["raw_values"] = {"name": "x"}
    with pytest.raises(ValidationError):
        ec.ExceptionCardV1.model_validate({**body, "card_id": ec.card_digest(body)})


def _answer(**overrides):
    base = {
        "schema": ec.EXCEPTION_ANSWER_SCHEMA,
        "card_id": "sha256:" + "a" * 64,
        "kind": "second_factor",
        "opened_at": OPENED,
        "decided_at": "2026-11-21T12:01:00Z",
        "answer": "done_by_person",
        "approver_kind": "person",
        "route": "cli",
        "reason": None,
        "public_key": "b" * 64,
        "signature": "ed25519:" + "c" * 128,
    }
    base.update(overrides)
    return ec.ExceptionAnswerV1.model_validate(base)


def test_a_persons_answer_is_timed_and_signed():
    answer = _answer()
    assert answer.proceeds
    with pytest.raises(ValidationError):
        _answer(signature=None)
    with pytest.raises(ValidationError):
        _answer(decided_at=None)
    with pytest.raises(ValidationError):
        _answer(reason="expired")


def test_an_expiry_is_an_unsigned_decline_with_no_decision_time():
    expiry = _answer(
        decided_at=None,
        answer="decline",
        approver_kind="expiry",
        route="registry",
        reason="expired",
        public_key=None,
        signature=None,
    )
    assert not expiry.proceeds
    with pytest.raises(ValidationError):  # an expiry can't say continue
        _answer(
            decided_at=None,
            answer="continue",
            approver_kind="expiry",
            route="registry",
            reason="expired",
            public_key=None,
            signature=None,
        )


@pytest.mark.parametrize("value", ["skip", "decline", "abort"])
def test_only_continue_and_done_by_person_proceed(value):
    assert not _answer(answer=value).proceeds


def test_the_signed_bytes_leave_out_the_key_and_signature():
    wire = _answer().wire()
    message = ec.answer_message(wire)
    assert message.startswith(ec.EXCEPTION_ANSWER_DOMAIN)
    assert b"signature" not in message and b"public_key" not in message
    assert b'"answer":"done_by_person"' in message


def test_not_exported_from_the_package_root():
    import openadapt_types

    assert not hasattr(openadapt_types, "ExceptionCardV1")
