"""Actor-native ``openadapt.commit`` protocol and embeddable inbox card artifact.

Provides stateless, fail-closed commit request/receipt contracts for external
agent runtimes, an append-only filesystem event log (``receipts/<id>.json`` and
``memory_log.md``), and a renderer for structured JSON, Slack Block Kit, and
XSS-escaped HTML ``<OpenAdaptInboxCard />`` artifacts.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

COMMIT_PROTOCOL_SCHEMA = "openadapt.commit/v1"
INBOX_CARD_SCHEMA = "openadapt.inbox-card/v1"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_safe_id(value: str, field_name: str) -> str:
    if not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"{field_name} must match {_SAFE_ID_RE.pattern!r} (got {value!r})"
        )
    return value


def _sanitize_inline_markdown(text: str) -> str:
    """Collapse newlines/control chars so appended log lines stay single-line."""
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


class CommitStatus(str, Enum):
    """Terminal or human-gated state of an ``openadapt.commit`` execution."""

    VERIFIED = "VERIFIED"
    STOPPED = "STOPPED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class InboxActionType(str, Enum):
    """Explicit human decision actions exposed on an inbox card."""

    APPROVE = "approve"
    OVERRIDE = "override"
    REJECT = "reject"
    INSPECT = "inspect"


class FieldDiff(BaseModel):
    """Single expected-vs-observed field comparison from a read-back check."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    expected: str | None = None
    observed: str | None = None
    matched: bool
    note: str = ""


class CommitRequest(BaseModel):
    """Stateless actor request to execute and verify a compiled workflow commit."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = COMMIT_PROTOCOL_SCHEMA
    commit_id: str
    actor_id: str = Field(min_length=1)
    target_computer_id: str = Field(min_length=1)
    workflow_ref: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    inputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    requested_at: str = Field(default_factory=_utc_now_iso)
    require_readback: bool = True

    @field_validator("commit_id")
    @classmethod
    def _check_commit_id(cls, value: str) -> str:
        return _validate_safe_id(value, "commit_id")


class CommitReceipt(BaseModel):
    """Fail-closed commit outcome returned to the calling actor and inbox."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = COMMIT_PROTOCOL_SCHEMA
    commit_id: str
    status: CommitStatus
    actor_id: str = Field(min_length=1)
    target_computer_id: str = Field(min_length=1)
    workflow_ref: str = Field(min_length=1)
    readback_checked: bool = False
    readback_diffs: list[FieldDiff] = Field(default_factory=list)
    reason: str = ""
    dsse_receipt_ref: str | None = None
    dsse_signature_verified: bool = False
    recorded_at: str = Field(default_factory=_utc_now_iso)

    @field_validator("commit_id")
    @classmethod
    def _check_commit_id(cls, value: str) -> str:
        return _validate_safe_id(value, "commit_id")

    @model_validator(mode="after")
    def _enforce_fail_closed_invariants(self) -> CommitReceipt:
        if self.status is CommitStatus.VERIFIED:
            if not self.readback_checked:
                raise ValueError(
                    "VERIFIED receipt requires readback_checked=True (fail-closed)."
                )
            mismatched = [d.field for d in self.readback_diffs if not d.matched]
            if mismatched:
                raise ValueError(
                    "VERIFIED receipt cannot contain mismatched read-back diffs: "
                    + ", ".join(mismatched)
                )
            if not self.dsse_receipt_ref or not self.dsse_receipt_ref.strip():
                raise ValueError(
                    "VERIFIED receipt requires a non-empty dsse_receipt_ref."
                )
            if not self.dsse_signature_verified:
                raise ValueError(
                    "VERIFIED receipt requires dsse_signature_verified=True."
                )
        elif not self.reason.strip():
            raise ValueError(
                f"{self.status.value} receipt requires a non-empty reason."
            )
        return self


def evaluate_commit(
    request: CommitRequest,
    *,
    readback_diffs: Sequence[FieldDiff],
    readback_checked: bool,
    dsse_receipt_ref: str | None = None,
    dsse_signature_verified: bool = False,
    stop_reason: str = "",
    needs_human: bool = False,
) -> CommitReceipt:
    """Evaluate execution evidence into a fail-closed ``CommitReceipt``."""
    diffs = list(readback_diffs)
    mismatched = [d.field for d in diffs if not d.matched]

    if needs_human:
        reason = stop_reason.strip() or (
            "Human review required for mismatched fields: " + ", ".join(mismatched)
            if mismatched
            else "Human review required before committing changes."
        )
        return CommitReceipt(
            commit_id=request.commit_id,
            status=CommitStatus.NEEDS_HUMAN,
            actor_id=request.actor_id,
            target_computer_id=request.target_computer_id,
            workflow_ref=request.workflow_ref,
            readback_checked=readback_checked,
            readback_diffs=diffs,
            reason=reason,
            dsse_receipt_ref=dsse_receipt_ref,
            dsse_signature_verified=dsse_signature_verified,
        )

    if stop_reason.strip():
        return CommitReceipt(
            commit_id=request.commit_id,
            status=CommitStatus.STOPPED,
            actor_id=request.actor_id,
            target_computer_id=request.target_computer_id,
            workflow_ref=request.workflow_ref,
            readback_checked=readback_checked,
            readback_diffs=diffs,
            reason=stop_reason.strip(),
            dsse_receipt_ref=dsse_receipt_ref,
            dsse_signature_verified=dsse_signature_verified,
        )

    if request.require_readback and not readback_checked:
        return CommitReceipt(
            commit_id=request.commit_id,
            status=CommitStatus.STOPPED,
            actor_id=request.actor_id,
            target_computer_id=request.target_computer_id,
            workflow_ref=request.workflow_ref,
            readback_checked=False,
            readback_diffs=diffs,
            reason="Read-back check did not complete; commit refused (fail-closed).",
            dsse_receipt_ref=dsse_receipt_ref,
            dsse_signature_verified=dsse_signature_verified,
        )

    if mismatched:
        return CommitReceipt(
            commit_id=request.commit_id,
            status=CommitStatus.NEEDS_HUMAN,
            actor_id=request.actor_id,
            target_computer_id=request.target_computer_id,
            workflow_ref=request.workflow_ref,
            readback_checked=readback_checked,
            readback_diffs=diffs,
            reason="Read-back mismatch on fields: " + ", ".join(mismatched),
            dsse_receipt_ref=dsse_receipt_ref,
            dsse_signature_verified=dsse_signature_verified,
        )

    if not dsse_receipt_ref or not dsse_receipt_ref.strip() or not dsse_signature_verified:
        return CommitReceipt(
            commit_id=request.commit_id,
            status=CommitStatus.STOPPED,
            actor_id=request.actor_id,
            target_computer_id=request.target_computer_id,
            workflow_ref=request.workflow_ref,
            readback_checked=readback_checked,
            readback_diffs=diffs,
            reason=(
                "Ed25519 DSSE receipt is missing or unverified; "
                "commit cannot be marked VERIFIED."
            ),
            dsse_receipt_ref=dsse_receipt_ref,
            dsse_signature_verified=dsse_signature_verified,
        )

    matched_count = len(diffs)
    return CommitReceipt(
        commit_id=request.commit_id,
        status=CommitStatus.VERIFIED,
        actor_id=request.actor_id,
        target_computer_id=request.target_computer_id,
        workflow_ref=request.workflow_ref,
        readback_checked=True,
        readback_diffs=diffs,
        reason=(
            f"Verified by independent read-back ({matched_count} field(s) matched) "
            "and Ed25519 DSSE signature."
        ),
        dsse_receipt_ref=dsse_receipt_ref,
        dsse_signature_verified=True,
    )


def write_commit_log(receipt: CommitReceipt, base_dir: Path) -> tuple[Path, Path]:
    """Persist ``receipts/<commit_id>.json`` atomically and append ``memory_log.md``."""
    root = Path(base_dir).resolve()
    receipts_dir = root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    receipt_path = (receipts_dir / f"{receipt.commit_id}.json").resolve()
    if receipt_path.parent != receipts_dir.resolve():
        raise ValueError(f"Refusing path outside receipts directory: {receipt_path}")

    payload = json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=receipts_dir, prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, receipt_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    log_path = root / "memory_log.md"
    needs_header = not log_path.exists() or log_path.stat().st_size == 0
    matched = sum(1 for d in receipt.readback_diffs if d.matched)
    total = len(receipt.readback_diffs)
    dsse_label = _sanitize_inline_markdown(receipt.dsse_receipt_ref or "none")
    reason_label = _sanitize_inline_markdown(receipt.reason or "n/a")
    entry = (
        f"- `{receipt.recorded_at}` | **{receipt.status.value}** | "
        f"commit=`{receipt.commit_id}` | actor=`{_sanitize_inline_markdown(receipt.actor_id)}` | "
        f"host=`{_sanitize_inline_markdown(receipt.target_computer_id)}` | "
        f"workflow=`{_sanitize_inline_markdown(receipt.workflow_ref)}` | "
        f"readback={matched}/{total} | dsse=`{dsse_label}` | "
        f"receipt=`receipts/{receipt.commit_id}.json` | {reason_label}\n"
    )
    with log_path.open("a", encoding="utf-8") as handle:
        if needs_header:
            handle.write("# OpenAdapt Commit Memory Log\n\n")
        handle.write(entry)

    return receipt_path, log_path


class InboxAction(BaseModel):
    """Explicit action descriptor rendered on an ``<OpenAdaptInboxCard />``."""

    model_config = ConfigDict(extra="forbid")

    action: InboxActionType
    label: str = Field(min_length=1)
    commit_id: str
    style: Literal["primary", "secondary", "danger"] = "secondary"
    requires_reason: bool = False


class InboxCardArtifact(BaseModel):
    """Structured JSON, Slack Block Kit, and XSS-safe HTML inbox card artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = INBOX_CARD_SCHEMA
    commit_id: str
    status: CommitStatus
    title: str
    summary: str
    target_computer_id: str
    workflow_ref: str
    dsse_receipt_ref: str | None = None
    readback_checked: bool
    diffs: list[FieldDiff]
    actions: list[InboxAction]
    slack_blocks: list[dict[str, Any]]
    html: str


def _esc(value: str | None) -> str:
    return html.escape("" if value is None else value, quote=True)


def _build_actions(receipt: CommitReceipt) -> list[InboxAction]:
    if receipt.status is CommitStatus.VERIFIED:
        return [
            InboxAction(
                action=InboxActionType.INSPECT,
                label="Inspect Receipt",
                commit_id=receipt.commit_id,
                style="secondary",
                requires_reason=False,
            )
        ]
    return [
        InboxAction(
            action=InboxActionType.APPROVE,
            label="Approve",
            commit_id=receipt.commit_id,
            style="primary",
            requires_reason=False,
        ),
        InboxAction(
            action=InboxActionType.OVERRIDE,
            label="Override",
            commit_id=receipt.commit_id,
            style="secondary",
            requires_reason=True,
        ),
        InboxAction(
            action=InboxActionType.REJECT,
            label="Reject",
            commit_id=receipt.commit_id,
            style="danger",
            requires_reason=True,
        ),
    ]


def _build_slack_blocks(
    receipt: CommitReceipt,
    title: str,
    actions: Sequence[InboxAction],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title[:150], "emoji": False},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{receipt.status.value}* — {receipt.reason}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Commit:*\n`{receipt.commit_id}`"},
                {"type": "mrkdwn", "text": f"*Target Computer:*\n`{receipt.target_computer_id}`"},
                {"type": "mrkdwn", "text": f"*Workflow:*\n`{receipt.workflow_ref}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*DSSE Receipt:*\n`{receipt.dsse_receipt_ref or 'none'}`",
                },
            ],
        },
    ]

    if receipt.readback_diffs:
        diff_lines = []
        for diff in receipt.readback_diffs:
            marker = "MATCH" if diff.matched else "MISMATCH"
            diff_lines.append(
                f"• `{diff.field}` [{marker}]: expected=`{diff.expected}` observed=`{diff.observed}`"
            )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Read-Back Diff:*\n" + "\n".join(diff_lines),
                },
            }
        )

    slack_elements: list[dict[str, Any]] = []
    for act in actions:
        element: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": act.label, "emoji": False},
            "action_id": f"openadapt_commit_{act.action.value}",
            "value": json.dumps(
                {
                    "commit_id": act.commit_id,
                    "action": act.action.value,
                    "requires_reason": act.requires_reason,
                },
                sort_keys=True,
            ),
        }
        if act.style in ("primary", "danger"):
            element["style"] = act.style
        slack_elements.append(element)

    if slack_elements:
        blocks.append({"type": "actions", "elements": slack_elements})

    return blocks


def _build_html_card(
    receipt: CommitReceipt,
    title: str,
    actions: Sequence[InboxAction],
) -> str:
    diff_rows: list[str] = []
    for diff in receipt.readback_diffs:
        state_text = "matched" if diff.matched else "mismatch"
        diff_rows.append(
            "<tr data-matched=\""
            + ("true" if diff.matched else "false")
            + "\">"
            + "<td><code>" + _esc(diff.field) + "</code></td>"
            + "<td>" + _esc(diff.expected) + "</td>"
            + "<td>" + _esc(diff.observed) + "</td>"
            + "<td>" + _esc(state_text) + "</td>"
            + "</tr>"
        )

    diff_section = ""
    if diff_rows:
        diff_section = (
            "<table class=\"oa-inbox-diff\">"
            "<thead><tr><th>Field</th><th>Expected</th><th>Observed</th><th>Check</th></tr></thead>"
            "<tbody>"
            + "".join(diff_rows)
            + "</tbody></table>"
        )

    action_buttons = "".join(
        "<button type=\"button\" class=\"oa-action oa-action-"
        + _esc(act.style)
        + "\" data-oa-action=\""
        + _esc(act.action.value)
        + "\" data-commit-id=\""
        + _esc(act.commit_id)
        + "\" data-requires-reason=\""
        + ("true" if act.requires_reason else "false")
        + "\">"
        + _esc(act.label)
        + "</button>"
        for act in actions
    )

    return (
        "<article class=\"oa-inbox-card\" data-schema=\""
        + _esc(INBOX_CARD_SCHEMA)
        + "\" data-status=\""
        + _esc(receipt.status.value)
        + "\" data-commit-id=\""
        + _esc(receipt.commit_id)
        + "\">"
        + "<header class=\"oa-inbox-header\">"
        + "<span class=\"oa-badge\">" + _esc(receipt.status.value) + "</span>"
        + "<h3>" + _esc(title) + "</h3>"
        + "</header>"
        + "<p class=\"oa-inbox-reason\">" + _esc(receipt.reason) + "</p>"
        + "<dl class=\"oa-inbox-meta\">"
        + "<dt>Target Computer</dt><dd><code>" + _esc(receipt.target_computer_id) + "</code></dd>"
        + "<dt>Workflow</dt><dd><code>" + _esc(receipt.workflow_ref) + "</code></dd>"
        + "<dt>DSSE Receipt</dt><dd><code>" + _esc(receipt.dsse_receipt_ref or "none") + "</code></dd>"
        + "</dl>"
        + diff_section
        + "<footer class=\"oa-inbox-actions\">"
        + action_buttons
        + "</footer>"
        + "</article>"
    )


def render_inbox_card(receipt: CommitReceipt) -> InboxCardArtifact:
    """Render a ``CommitReceipt`` into JSON, Slack Block Kit, and XSS-safe HTML."""
    titles = {
        CommitStatus.VERIFIED: f"Commit verified: {receipt.commit_id}",
        CommitStatus.STOPPED: f"Commit stopped: {receipt.commit_id}",
        CommitStatus.NEEDS_HUMAN: f"Human decision needed: {receipt.commit_id}",
    }
    title = titles[receipt.status]
    actions = _build_actions(receipt)
    return InboxCardArtifact(
        commit_id=receipt.commit_id,
        status=receipt.status,
        title=title,
        summary=receipt.reason,
        target_computer_id=receipt.target_computer_id,
        workflow_ref=receipt.workflow_ref,
        dsse_receipt_ref=receipt.dsse_receipt_ref,
        readback_checked=receipt.readback_checked,
        diffs=list(receipt.readback_diffs),
        actions=actions,
        slack_blocks=_build_slack_blocks(receipt, title, actions),
        html=_build_html_card(receipt, title, actions),
    )


__all__ = [
    "COMMIT_PROTOCOL_SCHEMA",
    "INBOX_CARD_SCHEMA",
    "CommitRequest",
    "CommitReceipt",
    "CommitStatus",
    "FieldDiff",
    "InboxAction",
    "InboxActionType",
    "InboxCardArtifact",
    "evaluate_commit",
    "render_inbox_card",
    "write_commit_log",
]
