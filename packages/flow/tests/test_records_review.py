"""The review queue: only problems enter; decisions are signed, written once,
made by a person who did not create the item."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.records.read import channel_read, combine_reads
from openadapt_flow.records.review import ReviewError, ReviewQueue, apply_decision
from openadapt_flow.records.schema import FieldSpec, RecordRead, RecordSchema

SCHEMA = RecordSchema(
    name="customer_account",
    fields=(
        FieldSpec(name="account_number", kind="code"),
        FieldSpec(name="balance", kind="currency"),
    ),
    identity_fields=("account_number",),
)
RUNNER = "runner:test-host"
CLERK = "person:clerk-1"


def _record() -> RecordRead:
    acct = SCHEMA.field("account_number")
    bal = SCHEMA.field("balance")
    good = combine_reads(
        acct,
        [
            channel_read(acct, "clipboard", "AC-1042"),
            channel_read(acct, "ocr", "AC-1042"),
        ],
    )
    bad = combine_reads(
        bal,
        [
            channel_read(bal, "clipboard", "$1,234.50"),
            channel_read(bal, "ocr", "$1,284.50"),
        ],
    )
    return RecordRead(
        schema_name=SCHEMA.name,
        schema_version=1,
        record_key="ac1042",
        run_id="run-7",
        read_at="2026-10-17T21:00:00-04:00",
        fields=(good, bad),
    )


def test_only_problems_are_queued(tmp_path) -> None:
    queue = ReviewQueue(tmp_path)
    items = queue.add_record(_record(), created_by=RUNNER)
    assert [i.field for i in items] == ["balance"]
    assert items[0].candidates == {"clipboard": "$1,234.50", "ocr": "$1,284.50"}
    assert queue.open_items() == items
    # Re-adding the same item is idempotent.
    assert queue.add_record(_record(), created_by=RUNNER) == items


def test_person_accepts_a_read_and_it_is_signed(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=RUNNER)[0]
    decision = queue.decide(
        item.item_id,
        action="accept_read",
        channel="clipboard",
        reviewer=CLERK,
        signing_key=key,
        schema=SCHEMA,
    )
    assert decision.value == "1234.5"
    assert queue.check(item.item_id, key.public_key())
    assert queue.open_items() == []
    settled = apply_decision(_record().fields[1], decision)
    assert settled is not None
    assert settled.value == "1234.5"
    assert settled.grade == "unconfirmed"  # a person's choice is not a read
    assert settled.review_decision == item.item_id
    assert not settled.needs_review


def test_decision_is_written_once(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=RUNNER)[0]
    queue.decide(
        item.item_id, action="reject", reviewer=CLERK, signing_key=key, schema=SCHEMA
    )
    with pytest.raises(ReviewError, match="already decided"):
        queue.decide(
            item.item_id,
            action="reject",
            reviewer="person:other",
            signing_key=key,
            schema=SCHEMA,
        )


@pytest.mark.parametrize(
    "reviewer", ["model:local-vlm", "agent:cua", RUNNER, "person:"]
)
def test_only_a_person_other_than_the_creator_decides(tmp_path, reviewer) -> None:
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=RUNNER)[0]
    with pytest.raises(ReviewError):
        queue.decide(
            item.item_id,
            action="reject",
            reviewer=reviewer,
            signing_key=Ed25519PrivateKey.generate(),
            schema=SCHEMA,
        )


def test_no_self_approval(tmp_path) -> None:
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=CLERK)[0]
    with pytest.raises(ReviewError, match="cannot decide"):
        queue.decide(
            item.item_id,
            action="reject",
            reviewer=CLERK,
            signing_key=Ed25519PrivateKey.generate(),
            schema=SCHEMA,
        )


def test_correction_must_be_valid(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=RUNNER)[0]
    with pytest.raises(ReviewError, match="not a valid currency"):
        queue.decide(
            item.item_id,
            action="correct",
            value="lots",
            reviewer=CLERK,
            signing_key=key,
            schema=SCHEMA,
        )
    decision = queue.decide(
        item.item_id,
        action="correct",
        value="1,234.50",
        reviewer=CLERK,
        signing_key=key,
        schema=SCHEMA,
    )
    assert decision.value == "1234.5"


def test_tampered_item_or_decision_fails_check(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    queue = ReviewQueue(tmp_path)
    item = queue.add_record(_record(), created_by=RUNNER)[0]
    queue.decide(
        item.item_id,
        action="accept_read",
        channel="ocr",
        reviewer=CLERK,
        signing_key=key,
        schema=SCHEMA,
    )
    assert not queue.check(item.item_id, Ed25519PrivateKey.generate().public_key())
    path = tmp_path / "decisions" / f"{item.item_id}.json"
    data = json.loads(path.read_text())
    data["value"] = "9999"
    path.write_text(json.dumps(data))
    assert not queue.check(item.item_id, key.public_key())


def test_halt_item_and_reject_drop(tmp_path) -> None:
    queue = ReviewQueue(tmp_path)
    halt = queue.add_halt(
        schema=SCHEMA,
        record_key="ac2000",
        run_id="run-8",
        reason="screen did not match the record layout",
        created_by=RUNNER,
    )
    assert halt.kind == "halt"
    with pytest.raises(ReviewError, match="field items only"):
        queue.decide(
            halt.item_id,
            action="correct",
            value="1",
            reviewer=CLERK,
            signing_key=Ed25519PrivateKey.generate(),
            schema=SCHEMA,
        )
    decision = queue.decide(
        halt.item_id,
        action="reread",
        reviewer=CLERK,
        signing_key=Ed25519PrivateKey.generate(),
        schema=SCHEMA,
    )
    assert apply_decision(_record().fields[1], decision) is None
