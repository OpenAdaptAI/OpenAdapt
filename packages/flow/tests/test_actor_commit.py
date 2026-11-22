"""Unit tests for the actor-native ``openadapt.commit`` protocol and inbox card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_flow.actor_commit import (
    COMMIT_PROTOCOL_SCHEMA,
    INBOX_CARD_SCHEMA,
    CommitReceipt,
    CommitRequest,
    CommitStatus,
    FieldDiff,
    InboxActionType,
    evaluate_commit,
    render_inbox_card,
    write_commit_log,
)


def _sample_request(commit_id: str = "commit-2026-001") -> CommitRequest:
    return CommitRequest(
        commit_id=commit_id,
        actor_id="actor.planner.v1",
        target_computer_id="host-west-04",
        workflow_ref="wf-record-update@sha256:abcdef0123456789",
        idempotency_key="idem-001",
        inputs={"record_id": "rec-42", "status": "approved"},
    )


def test_evaluate_commit_verified_happy_path() -> None:
    req = _sample_request("commit-verified-1")
    diffs = [
        FieldDiff(field="record_id", expected="rec-42", observed="rec-42", matched=True),
        FieldDiff(field="status", expected="approved", observed="approved", matched=True),
    ]
    receipt = evaluate_commit(
        req,
        readback_diffs=diffs,
        readback_checked=True,
        dsse_receipt_ref="dsse://sha256:1122334455667788",
        dsse_signature_verified=True,
    )

    assert receipt.schema_version == COMMIT_PROTOCOL_SCHEMA
    assert receipt.status is CommitStatus.VERIFIED
    assert receipt.readback_checked is True
    assert receipt.dsse_signature_verified is True
    assert len(receipt.readback_diffs) == 2


def test_evaluate_commit_stopped_when_unverified_or_unsigned() -> None:
    req = _sample_request("commit-stopped-1")
    matched = [FieldDiff(field="status", expected="ok", observed="ok", matched=True)]

    no_readback = evaluate_commit(
        req,
        readback_diffs=matched,
        readback_checked=False,
        dsse_receipt_ref="dsse://sha256:abc",
        dsse_signature_verified=True,
    )
    assert no_readback.status is CommitStatus.STOPPED
    assert "Read-back" in no_readback.reason

    no_dsse = evaluate_commit(
        req,
        readback_diffs=matched,
        readback_checked=True,
        dsse_receipt_ref=None,
        dsse_signature_verified=False,
    )
    assert no_dsse.status is CommitStatus.STOPPED
    assert "DSSE" in no_dsse.reason

    explicit_stop = evaluate_commit(
        req,
        readback_diffs=[],
        readback_checked=False,
        stop_reason="Pre-write guard detected duplicate target record.",
    )
    assert explicit_stop.status is CommitStatus.STOPPED
    assert "duplicate target record" in explicit_stop.reason


def test_evaluate_commit_needs_human_on_readback_mismatch() -> None:
    req = _sample_request("commit-human-1")
    diffs = [
        FieldDiff(field="record_id", expected="rec-42", observed="rec-42", matched=True),
        FieldDiff(field="amount", expected="120.00", observed="1200.00", matched=False),
    ]
    receipt = evaluate_commit(
        req,
        readback_diffs=diffs,
        readback_checked=True,
        dsse_receipt_ref="dsse://sha256:deadbeef",
        dsse_signature_verified=True,
    )

    assert receipt.status is CommitStatus.NEEDS_HUMAN
    assert "amount" in receipt.reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "readback_checked": False,
            "readback_diffs": [],
            "dsse_receipt_ref": "dsse://sha256:abc",
            "dsse_signature_verified": True,
        },
        {
            "readback_checked": True,
            "readback_diffs": [
                FieldDiff(field="code", expected="A", observed="B", matched=False)
            ],
            "dsse_receipt_ref": "dsse://sha256:abc",
            "dsse_signature_verified": True,
        },
        {
            "readback_checked": True,
            "readback_diffs": [],
            "dsse_receipt_ref": None,
            "dsse_signature_verified": True,
        },
        {
            "readback_checked": True,
            "readback_diffs": [],
            "dsse_receipt_ref": "dsse://sha256:abc",
            "dsse_signature_verified": False,
        },
    ],
)
def test_commit_receipt_fail_closed_refuses_unverified_verified_status(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CommitReceipt(
            commit_id="commit-bad-verified",
            status=CommitStatus.VERIFIED,
            actor_id="actor.v1",
            target_computer_id="host-01",
            workflow_ref="wf-1",
            **kwargs,  # type: ignore[arg-type]
        )


def test_commit_receipt_requires_reason_for_non_verified_and_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        CommitReceipt(
            commit_id="commit-no-reason",
            status=CommitStatus.STOPPED,
            actor_id="actor.v1",
            target_computer_id="host-01",
            workflow_ref="wf-1",
            reason="   ",
        )

    for bad_id in ("../escape", "sub/dir", "", ".hidden"):
        with pytest.raises(ValidationError):
            _sample_request(bad_id)


def test_write_commit_log_persists_json_and_appends_memory_log(tmp_path: Path) -> None:
    req1 = _sample_request("commit-log-1")
    r1 = evaluate_commit(
        req1,
        readback_diffs=[FieldDiff(field="k", expected="1", observed="1", matched=True)],
        readback_checked=True,
        dsse_receipt_ref="dsse://sha256:aaa",
        dsse_signature_verified=True,
    )
    req2 = _sample_request("commit-log-2")
    r2 = evaluate_commit(
        req2,
        readback_diffs=[FieldDiff(field="k", expected="1", observed="2", matched=False)],
        readback_checked=True,
        stop_reason="Value drifted\ninjected line",
        needs_human=True,
    )

    p1, log1 = write_commit_log(r1, tmp_path)
    p2, log2 = write_commit_log(r2, tmp_path)

    assert p1 == (tmp_path / "receipts" / "commit-log-1.json").resolve()
    assert p2 == (tmp_path / "receipts" / "commit-log-2.json").resolve()
    assert log1 == log2 == (tmp_path / "memory_log.md").resolve()

    loaded = json.loads(p1.read_text(encoding="utf-8"))
    assert loaded["status"] == "VERIFIED"
    assert loaded["commit_id"] == "commit-log-1"

    lines = log1.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# OpenAdapt Commit Memory Log"
    bullet_lines = [line for line in lines if line.startswith("- ")]
    assert len(bullet_lines) == 2
    assert "commit-log-1" in bullet_lines[0] and "**VERIFIED**" in bullet_lines[0]
    assert "commit-log-2" in bullet_lines[1] and "**NEEDS_HUMAN**" in bullet_lines[1]
    assert "Value drifted injected line" in bullet_lines[1]


def test_render_inbox_card_json_slack_blocks_and_xss_safe_html() -> None:
    xss_payload = '<script>alert("xss")</script>'
    attr_break = '"><img src=x onerror=alert(1)>'
    receipt = CommitReceipt(
        commit_id="commit-xss-check",
        status=CommitStatus.NEEDS_HUMAN,
        actor_id="actor.v1",
        target_computer_id=f"host-{attr_break}",
        workflow_ref=f"wf-{xss_payload}",
        readback_checked=True,
        readback_diffs=[
            FieldDiff(
                field=f"field-{xss_payload}",
                expected=f"exp-{attr_break}",
                observed=f"obs-{xss_payload}",
                matched=False,
            )
        ],
        reason=f"Mismatch detected: {xss_payload}",
        dsse_receipt_ref=f"dsse://{attr_break}",
        dsse_signature_verified=False,
    )

    card = render_inbox_card(receipt)
    assert card.schema_version == INBOX_CARD_SCHEMA
    assert card.status is CommitStatus.NEEDS_HUMAN
    assert [a.action for a in card.actions] == [
        InboxActionType.APPROVE,
        InboxActionType.OVERRIDE,
        InboxActionType.REJECT,
    ]

    # Slack Block Kit schema checks
    block_types = [b["type"] for b in card.slack_blocks]
    assert block_types[0] == "header"
    assert "section" in block_types
    assert block_types[-1] == "actions"
    actions_block = card.slack_blocks[-1]
    assert len(actions_block["elements"]) == 3
    action_ids = [el["action_id"] for el in actions_block["elements"]]
    assert action_ids == [
        "openadapt_commit_approve",
        "openadapt_commit_override",
        "openadapt_commit_reject",
    ]
    parsed_val = json.loads(actions_block["elements"][0]["value"])
    assert parsed_val["commit_id"] == "commit-xss-check"
    assert parsed_val["action"] == "approve"

    # Strict XSS escaping checks on HTML artifact
    assert "<script>" not in card.html
    assert "<img" not in card.html
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in card.html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in card.html
    assert 'data-oa-action="approve"' in card.html
    assert 'data-oa-action="override"' in card.html
    assert 'data-oa-action="reject"' in card.html

    # Parse HTML to confirm zero injected tags or inline event handler attributes
    from html.parser import HTMLParser

    seen_tags: list[str] = []
    seen_attrs: list[str] = []

    class _DomCheck(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            seen_tags.append(tag)
            seen_attrs.extend(name.lower() for name, _ in attrs)

    _DomCheck().feed(card.html)
    assert "script" not in seen_tags
    assert "img" not in seen_tags
    assert not any(attr.startswith("on") for attr in seen_attrs)

    # Verified card exposes only INSPECT action
    verified = evaluate_commit(
        _sample_request("commit-verified-card"),
        readback_diffs=[FieldDiff(field="k", expected="v", observed="v", matched=True)],
        readback_checked=True,
        dsse_receipt_ref="dsse://sha256:9999",
        dsse_signature_verified=True,
    )
    verified_card = render_inbox_card(verified)
    assert [a.action for a in verified_card.actions] == [InboxActionType.INSPECT]
    assert 'data-oa-action="inspect"' in verified_card.html
