"""Follow-up records: a later signed update to a receipt's counterparty lines."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from openadapt_types import receipt as r  # noqa: E402

_GEN = Path(__file__).parent / "vectors" / "record-run-v1" / "generate.py"
_spec = importlib.util.spec_from_file_location("receipt_vector_generate", _GEN)
gen = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(gen)


def _registry():
    return r.parse_registry(gen.REGISTRY)


def _sign(statement):
    return r.sign_statement(statement, [gen.signer("this_computer")])


def _original():
    statement, openings = gen._confirmed("sent")
    return _sign(statement), openings


def _update(original, sequence, state):
    updated, _ = gen._confirmed(state)
    return _sign(
        r.follow_up_statement(original, sequence, updated["predicate"]["confirmations"])
    )


def test_a_chain_verifies_and_labels_each_update():
    original, openings = _original()
    one = _update(original, 1, "acknowledged")
    two = _update(original, 2, "recorded_as_intended")
    chain = r.verify_chain(
        original,
        [one, two],
        _registry(),
        openings=r.private_openings(original, openings),
    )
    assert chain.ok, chain
    assert chain.lines == (
        "Sent to the other party at 2026-11-20T23:41:07Z, not confirmed",
        "Update 1: Acknowledged by the other party",
        "Update 2: Confirmed in the other party's system",
    )


def test_a_receipt_without_follows_is_byte_identical():
    statement, _ = gen._confirmed("sent")
    assert (
        "follows" not in r.RunStatementV1.model_validate(statement).wire()["predicate"]
    )


@pytest.mark.parametrize(
    "build,reason",
    [
        (lambda o: [_update(o, 2, "acknowledged")], "out_of_sequence"),
        (lambda o: [_sign(gen._confirmed("acknowledged")[0])], "not_a_follow_up"),
    ],
)
def test_broken_chains_are_refused(build, reason):
    original, _ = _original()
    chain = r.verify_chain(original, build(original), _registry())
    assert (chain.ok, chain.reason) == (False, reason)


def test_a_follow_up_naming_another_receipt_or_changing_it_is_refused():
    original, _ = _original()
    update = _update(original, 1, "acknowledged")
    stmt = r.follow_up_statement(
        original, 1, gen._confirmed("acknowledged")[0]["predicate"]["confirmations"]
    )
    stmt["predicate"]["follows"]["receipt"]["sha256"] = "0" * 64
    assert (
        r.verify_chain(original, [_sign(stmt)], _registry()).reason == "wrong_original"
    )
    changed = r.follow_up_statement(
        original, 1, gen._confirmed("acknowledged")[0]["predicate"]["confirmations"]
    )
    changed["predicate"]["model_calls"] = 9
    assert (
        r.verify_chain(original, [_sign(changed)], _registry()).reason
        == "changed_original"
    )
    assert r.verify_chain(update, [], _registry()).reason == "original_is_follow_up"


def test_a_follow_up_signed_by_another_registered_key_is_refused():
    original, _ = _original()
    updated, _ = gen._confirmed("acknowledged")
    statement = r.follow_up_statement(
        original, 1, updated["predicate"]["confirmations"]
    )
    by_other = r.sign_statement(statement, [gen.signer("customer_key")])
    assert r.verify_receipt(by_other, _registry()).ok
    chain = r.verify_chain(original, [by_other], _registry())
    assert (chain.ok, chain.reason) == (False, "different_signer")
    both = r.sign_statement(
        statement, [gen.signer("this_computer"), gen.signer("customer_key")]
    )
    assert r.verify_chain(original, [both], _registry()).reason == "different_signer"


def test_an_empty_follow_up_cannot_be_signed():
    original, _ = _original()
    with pytest.raises(ValueError, match="at least one confirmation"):
        _sign(r.follow_up_statement(original, 1, []))
