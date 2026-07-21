"""Redacted, local-only projections for the staff attended halt queue.

The queue deliberately does not expose workflow names, parameters, halt
reasons, observed text, local paths, or raw report/checkpoint payloads.  It
returns opaque run ids, category-derived operator copy, counts, timestamps,
and existing opaque screenshot ids.  Protected screenshots remain behind the
console's authenticated, symlink-safe artifact endpoint.

This module remains projection-only.  Engine-owned attended mutations live in
``runtime.durable.attended`` and appear only when an exact pause capability and
deployment-bound action service are present.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from openadapt_flow.console import data
from openadapt_flow.runtime.durable.attended import attended_capability_summary
from openadapt_flow.runtime.durable.controller import looks_like_human_required

_KNOWN_CATEGORIES = {
    "effect_refuted",
    "effect_indeterminate",
    "effect_escalated",
    "placeholder_effect",
    "effect_unverifiable",
    "unmet_guard",
    "disambiguation",
    "identity",
    "postcondition",
    "resolution",
    "human_required",
    "halt",
}
_ATTENTION_ID_RE = re.compile(r"^[0-9a-f]{24}$")

_COPY: dict[str, tuple[str, str]] = {
    "human_required": (
        "The application needs a person before the workflow can continue.",
        "Complete the challenge, sign-in, or verification in the live "
        "application. OpenAdapt never answers it; Continue only verifies the "
        "resulting live state before deterministic resume.",
    ),
    "identity": (
        "The target identity could not be certified.",
        "Review the live application and the protected local evidence.",
    ),
    "disambiguation": (
        "More than one target looked plausible, so no target was chosen.",
        "Select or prepare the intended target in the live application.",
    ),
    "effect_refuted": (
        "The expected system-of-record result was not confirmed.",
        "Review the destination record before deciding how to proceed.",
    ),
    "effect_indeterminate": (
        "The system-of-record result could not be verified.",
        "Restore the verifier or inspect the destination record.",
    ),
    "effect_escalated": (
        "The result could not be safely reconciled.",
        "Review and correct the destination record before continuing.",
    ),
    "effect_unverifiable": (
        "This write has no usable independent verifier.",
        "Configure the deployment verifier before continuing.",
    ),
    "placeholder_effect": (
        "This workflow still needs a deployment-specific effect binding.",
        "Complete and certify the binding before continuing.",
    ),
    "unmet_guard": (
        "The application was not in the expected state.",
        "Prepare the live application, then review the paused run.",
    ),
    "postcondition": (
        "The application did not reach the expected state.",
        "Inspect the live application before deciding how to proceed.",
    ),
    "resolution": (
        "The compiled target could not be resolved uniquely.",
        "Prepare the correct application view or teach a reviewed repair.",
    ),
    "halt": (
        "The workflow stopped instead of guessing.",
        "Review the protected local evidence before taking action.",
    ),
    "operator_review": (
        "This run needs local operator review.",
        "Open the protected run details on this computer.",
    ),
}


class AttentionItem(BaseModel):
    """Browser-safe queue item; no protected values or filesystem paths."""

    id: str
    created_at: Optional[str] = None
    category: str
    headline: str
    next_action: str
    status: str
    human_required: bool = False
    encrypted_pause: bool = False
    observed_text_count: int = 0
    completed_intent_count: int = 0
    before_artifact_id: Optional[str] = None
    after_artifact_id: Optional[str] = None
    capability: Optional[dict[str, Any]] = None


class AttentionNotification(BaseModel):
    """PHI-free payload an OS shell/tray notifier may render."""

    title: str = "OpenAdapt needs attention"
    body: str
    open_count: int
    route: str = "#/attention"


def _normal_category(raw: Any, *protected_texts: Any) -> str:
    value = raw if isinstance(raw, str) else ""
    # Preserve a runtime-produced typed category.  An observed phrase such as
    # "session expired" must not re-label an effect/identity/postcondition halt.
    # A category never authorizes an action. Mutations require a separately
    # signed engine capability tied to the exact pause.
    if value in _KNOWN_CATEGORIES:
        return value
    marker_present = looks_like_human_required(
        *(text for text in protected_texts if isinstance(text, str))
    )
    if marker_present and value in {"", "challenge", "authentication", "mfa"}:
        return "human_required"
    return "operator_review"


def _last_failed_result(report: Any) -> tuple[Optional[Any], Optional[int]]:
    if report is None:
        return None, None
    for index in range(len(report.results) - 1, -1, -1):
        result = report.results[index]
        # Program mode appends a synthetic terminal failure after the real
        # failing action.  Evidence review should show the action's screenshots
        # and verdicts, not the evidence-free terminal bookkeeping row.
        if result.step_id == "<terminal>":
            continue
        if not result.ok and not result.skipped and not result.exception_handled:
            return result, index + 1
    # A non-action terminal may be the only failure in a program run.
    for index in range(len(report.results) - 1, -1, -1):
        result = report.results[index]
        if not result.ok and not result.skipped and not result.exception_handled:
            return result, index + 1
    return None, None


def _artifact_ids(
    result: Any, one_based_index: Optional[int]
) -> tuple[Optional[str], Optional[str]]:
    if result is None or one_based_index is None:
        return None, None
    before = f"step-{one_based_index:03d}-before" if result.before_png else None
    after = f"step-{one_based_index:03d}-after" if result.after_png else None
    return before, after


def attention_item(root: Path, path: Path) -> Optional[AttentionItem]:
    """Project one run directory into an open attention item, if applicable."""
    summary = data.run_summary(root, path)
    report, _ = data._load_report(path)
    pending = data._read_json_opt(path / "pending_escalation.json", root=path)
    encrypted_pause = (
        data._contained_file(path, path / "pending_escalation.json.enc") is not None
    )

    if (
        report is not None
        and report.success
        and pending is None
        and not encrypted_pause
    ):
        return None
    if report is None and pending is None and not encrypted_pause:
        return None
    if (
        pending is None
        and not encrypted_pause
        and report is not None
        and report.halt is None
    ):
        return None

    failed, failed_index = _last_failed_result(report)
    protected_texts: list[Any] = []
    if pending is not None:
        protected_texts.append(pending.get("reason"))
    if report is not None and report.halt is not None:
        protected_texts.extend(report.halt.observed_texts)
        protected_texts.append(report.halt.reason)
    category = _normal_category(
        pending.get("category") if pending is not None else None,
        *protected_texts,
    )
    headline, next_action = _COPY.get(category, _COPY["operator_review"])
    status = "encrypted" if encrypted_pause and pending is None else "halted"
    if pending is not None:
        raw_status = pending.get("status")
        status = raw_status if raw_status in {"pending", "approved"} else "pending"

    before_id, after_id = _artifact_ids(failed, failed_index)
    observed_count = (
        len(report.halt.observed_texts)
        if report is not None and report.halt is not None
        else 0
    )
    completed_count = (
        len(report.halt.completed_intents)
        if report is not None and report.halt is not None
        else 0
    )
    return AttentionItem(
        id=summary.id,
        created_at=summary.started_at,
        category=category,
        headline=headline,
        next_action=next_action,
        status=status,
        human_required=category == "human_required",
        encrypted_pause=encrypted_pause,
        observed_text_count=observed_count,
        completed_intent_count=completed_count,
        before_artifact_id=before_id,
        after_artifact_id=after_id,
        capability=attended_capability_summary(path),
    )


def list_attention(root: Path) -> list[AttentionItem]:
    items = [
        item
        for path in data._scan(root, data._is_run_dir)
        if (item := attention_item(root, path)) is not None
    ]
    return sorted(items, key=lambda item: item.created_at or "", reverse=True)


def resolve_attention(
    root: Path,
    attention_id: str,
) -> Optional[tuple[Path, AttentionItem]]:
    """Resolve one current opaque queue id without exposing scan internals.

    The returned path is guaranteed to be one of the fresh, bounded,
    symlink-refusing run-directory scan results under ``root``. Closed or
    changed items resolve to ``None`` rather than granting stale authority.
    """
    root = Path(root)
    if (
        root.is_symlink()
        or not root.is_dir()
        or not isinstance(attention_id, str)
        or _ATTENTION_ID_RE.fullmatch(attention_id) is None
    ):
        return None
    path = data.resolve_scanned_dir(root, attention_id, data._is_run_dir)
    if path is None:
        return None
    item = attention_item(root, path)
    if item is None or item.id != attention_id:
        return None
    return path, item


def notification(items: list[AttentionItem]) -> AttentionNotification:
    """Build the only payload an OS notification integration should render."""
    count = len(items)
    noun = "item needs" if count == 1 else "items need"
    return AttentionNotification(
        body=f"{count} {noun} review on this computer.",
        open_count=count,
    )
