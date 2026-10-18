"""Only ask a person about problems: the Desktop side of the switch.

The switch lets a clinic stop approving routine items one by one. With it on,
an item saves without a person only when every check before and after the
save passes, and the record read back from the records system shows exactly
the values it was given. Everything else goes to a person in Needs you.

Who decides what:

* A clinic owner or admin turns the switch on in OpenAdapt Cloud, and the
  clinic's privacy contact confirms. Desktop never turns it on. No IPC command
  writes a switch record; :func:`accept_switch` exists for the Cloud status
  client and for tests.
* Anyone at this computer can turn it off, offline too. The switch-off goes
  into Flow's hash-chained switch log, which Flow's pre-save gate reads before
  every save, so it applies before the next save and Cloud can't undo it.
* Flow decides each item: the pre-save checks (P1 to P5), the post-run checks
  (R1 and R2) and the switch-off rules. Desktop reads Flow's PHI-free
  ``approval`` block from the run report and never upgrades it. An item counts
  as saved without a person only when that block says so, both post-run checks
  passed, and the item's transaction outcome is a consistent ``VERIFIED``.
  Desktop also checks the block against its own records before it lets the
  item close without a person (:func:`confirm_no_person`); a block it can't
  confirm needs a person.

What is local, per task version (local bundle id)::

    <data_dir>/approval_mode/<bundle_id>/switch.json      the switch Cloud sent
    <data_dir>/approval_mode/<bundle_id>/switch.log       Flow's switch log
    <data_dir>/approval_mode/<bundle_id>/turned_off.json  a switch-off made here

Nothing here holds an input value, screen content or free text. The names of
the clinic staff who turned the switch on and confirmed it are kept on this
computer only, to show who is accountable.

The switch can't take effect on this computer yet. :func:`blockers` names
every missing piece, and while any is missing Desktop shows the switch as not
used here and never says that only problems come to the person.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ENVELOPE_SCHEMA = "openadapt-desktop.approval-mode-switch/v1"
#: Where a switch log goes when a switch-off couldn't be appended to it. Flow's
#: gate reads a missing ``switch.log`` as off.
UNUSABLE_LOG = "switch.log.unusable"
TURNED_OFF_SCHEMA = "openadapt-desktop.approval-mode-turned-off/v1"

#: Off reasons Flow's switch log can name (``openadapt_flow.approval_mode``).
OFF_REASONS = frozenset(
    {
        "uncertain_result",
        "wrong_item_found",
        "too_many_problems",
        "screen_changed",
        "record_check_lost",
        "spot_checks_overdue",
        "version_changed",
        "environment_changed",
        "admission_lost",
        "setup_lapsed",
        "turned_off",
    }
)

#: Pieces the switch needs that don't exist yet. Each one keeps the switch
#: from taking effect on this computer; remove a code only together with the
#: code that builds it.
#:
#: ``field_change_check``  Flow can't yet check that a save changed only
#:     the fields it was given, and the three drills for that check don't
#:     exist (Flow ``docs/APPROVAL_MODES.md``). Until they do, Desktop asks
#:     Flow for ``each_item`` on every run: a person approves each save.
#: ``cloud_status``  Desktop has no client for Cloud's signed switch status,
#:     which Flow's P1 check needs before every save.
#: ``spot_checks``   Mandatory spot checks (design 7.7) aren't built.
#: ``list_queue``    Lists run from the window, not from an engine-held queue,
#:     so a paused list doesn't survive closing the window.
NOT_BUILT: tuple[str, ...] = (
    "field_change_check",
    "cloud_status",
    "spot_checks",
    "list_queue",
)

_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


class SwitchApprovalError(ValueError):
    """A refused switch action. The message is safe to show."""


class _Person(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME.pattern)
    at: datetime


class _TurnedOnBy(_Person):
    role: Literal["owner", "admin"]


class SwitchEnvelope(BaseModel):
    """What Cloud sends a runner when the switch turns on.

    ``record`` is Flow's switch record, validated with Flow's own model. The
    two people are the accountable admin and the privacy contact. Their names
    are display names from the clinic's own Cloud organization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt-desktop.approval-mode-switch/v1"] = Field(
        ENVELOPE_SCHEMA, alias="schema"
    )
    record: dict[str, Any]
    turned_on: _TurnedOnBy
    confirmed: _Person


# ---------------------------------------------------------------- Flow access


def flow_switch_api() -> dict[str, Any] | None:
    """Flow's switch-log and record API, or None when this Flow lacks it."""

    try:
        from openadapt_flow.approval_mode.model import OffReason
        from openadapt_flow.approval_mode.switch import (
            SwitchLog,
            SwitchLogError,
            SwitchRecord,
            turn_off,
        )
    except ImportError:
        return None
    return {
        "OffReason": OffReason,
        "SwitchLog": SwitchLog,
        "SwitchLogError": SwitchLogError,
        "SwitchRecord": SwitchRecord,
        "turn_off": turn_off,
    }


def blockers(*, flow_has_switch: bool) -> list[str]:
    """Why the switch can't take effect on this computer, first reason first.

    An empty list is the only case in which Desktop may show the switch as on
    and say that only problems come to the person.
    """

    found: list[str] = [] if flow_has_switch else ["engine_without_switch"]
    found.extend(NOT_BUILT)
    return found


# --------------------------------------------------------------- local store


def store_dir(data_dir: Path, bundle_id: str) -> Path:
    if not _BUNDLE_ID.fullmatch(str(bundle_id or "")):
        raise SwitchApprovalError("That task version has an unexpected id.")
    return Path(data_dir) / "approval_mode" / str(bundle_id)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write owner-only JSON and move it into place in one step."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    temporary.replace(path)


def _stored_switch_id(folder: Path) -> str | None:
    """The switch id a stored (or archived) switch names, if readable."""

    try:
        raw = json.loads((folder / "switch.json").read_text(encoding="utf-8"))
        switch_id = raw["record"]["switch_id"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return switch_id if isinstance(switch_id, str) else None


def _closed_switch_ids(root: Path) -> set[str]:
    """Every switch id this computer moved aside. None of them may return."""

    found: set[str] = set()
    try:
        archived = list((root / "closed").iterdir())
    except OSError:
        return found
    for folder in archived:
        switch_id = _stored_switch_id(folder)
        if switch_id:
            found.add(switch_id)
    return found


def accept_switch(data_dir: Path, bundle_id: str, envelope: Mapping[str, Any]) -> str:
    """Store a switch Cloud turned on, and open its switch log.

    For the Cloud status client (not built yet) and for tests. No IPC command
    calls this, so nothing in Desktop can turn the switch on. Returns the
    switch id.

    A closed switch stays closed (design 7.6). Desktop refuses a switch id it
    turned off or moved aside, so a switch-off made here that hasn't reached
    Cloud can't be undone by Cloud sending the same record again. The switch
    already in use, sent again unchanged, keeps its log: a fresh log would
    forget an interrupted item and the problems counted toward
    ``too_many_problems``. A new switch that replaces a log whose last item
    never finished first closes that log as ``uncertain_result``.
    """

    api = flow_switch_api()
    if api is None:
        raise SwitchApprovalError("This computer's OpenAdapt engine can't keep a switch log.")
    try:
        parsed = SwitchEnvelope.model_validate(dict(envelope))
        record = api["SwitchRecord"].model_validate(parsed.record)
    except (ValidationError, ValueError) as exc:
        raise SwitchApprovalError("The switch from OpenAdapt Cloud isn't valid.") from exc
    root = store_dir(data_dir, bundle_id)
    closed_message = (
        "That switch was turned off on this computer, and a switch that turned "
        "off stays off. Turning it on again needs a new switch from OpenAdapt Cloud."
    )
    if record.switch_id in _closed_switch_ids(root):
        raise SwitchApprovalError(closed_message)
    current_id = _stored_switch_id(root)
    log_state = None
    log_readable = False
    if (root / "switch.log").exists():
        try:
            log_state = api["SwitchLog"](root / "switch.log").state()
            log_readable = True
        except api["SwitchLogError"]:
            log_readable = False
    if current_id is None and log_state is not None:
        current_id = log_state.switch_id
    if current_id is not None and current_id == record.switch_id:
        current_closed = (
            not log_readable
            or (log_state is not None and log_state.off_reason is not None)
            or _read_turned_off(root) is not None
        )
        if current_closed:
            raise SwitchApprovalError(closed_message)
        if log_state is None or log_state.switch_record_sha256 != record.digest():
            raise SwitchApprovalError(
                "OpenAdapt Cloud sent a different record under a switch id this "
                "computer already uses."
            )
        # The same switch, sent again: keep its record and its log.
        return record.switch_id
    if log_state is not None and log_state.open_items and log_state.off_reason is None:
        # An item under the current switch started and never finished, so
        # its save may have gone through. Close that log the way Flow's gate
        # would at the next item, so the archive says why it ended. The item
        # itself waits in Needs you as a record to check (its work item was
        # queued before the run) and blocks the task's lists.
        try:
            api["turn_off"](
                api["SwitchLog"](root / "switch.log"),
                api["OffReason"].UNCERTAIN_RESULT,
                at=datetime.now(timezone.utc).isoformat(),
            )
        except api["SwitchLogError"]:
            pass
    if (root / "switch.json").exists() or (root / "switch.log").exists():
        archive = root / "closed" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive.mkdir(parents=True, exist_ok=True)
        for name in (
            "switch.json",
            "switch.log",
            "switch.log.lock",
            UNUSABLE_LOG,
            "turned_off.json",
        ):
            if (root / name).exists():
                (root / name).replace(archive / name)
    payload = parsed.model_dump(mode="json", by_alias=True)
    payload["record"] = record.model_dump(mode="json")
    _write_private_json(root / "switch.json", payload)
    api["SwitchLog"].open_new(root / "switch.log", record)
    return record.switch_id


def _read_turned_off(root: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((root / "turned_off.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != TURNED_OFF_SCHEMA:
        return None
    return raw


def _manifest_digest(bundle_dir: Path | None) -> str | None:
    """The bundle's content digest from its PHI-free manifest sidecar."""

    if bundle_dir is None:
        return None
    try:
        manifest = json.loads((Path(bundle_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = manifest.get("content_digest") if isinstance(manifest, dict) else None
    return digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None


def read_switch(
    data_dir: Path,
    bundle_id: str,
    *,
    bundle_dir: Path | None,
    runner_id: str | None,
    item_running: bool = False,
) -> dict[str, Any]:
    """What this computer knows about one task version's switch.

    ``state`` is one of:

    * ``none``: no switch was ever turned on for this version here.
    * ``on``: the record is valid for this version and runner, and the log
      is intact with no switch-off.
    * ``waiting_for_cloud``: on, but the last Cloud status was stale. Flow
      sends items to a person until a fresh one arrives.
    * ``off``: the switch is closed for good. ``off_reason`` says why.

    Any record, log or binding Desktop can't read or confirm reads as ``off``,
    never as ``on``.
    """

    root = store_dir(data_dir, bundle_id)
    view: dict[str, Any] = {
        "state": "none",
        "switch_id": None,
        "off_reason": None,
        "off_at": None,
        "off_by_this_computer": False,
        "turned_on": None,
        "confirmed": None,
        "evidence": None,
    }
    if not (root / "switch.json").exists() and not (root / "switch.log").exists():
        return view

    def closed(reason: str, at: Any = None, *, here: bool = False) -> dict[str, Any]:
        view.update(
            state="off",
            off_reason=reason if reason in OFF_REASONS else "uncertain_result",
            off_at=at,
            off_by_this_computer=here,
        )
        return view

    api = flow_switch_api()
    try:
        envelope = SwitchEnvelope.model_validate(
            json.loads((root / "switch.json").read_text(encoding="utf-8"))
        )
        if api is None:
            raise SwitchApprovalError("no switch API")
        record = api["SwitchRecord"].model_validate(envelope.record)
    except (OSError, ValueError, ValidationError, SwitchApprovalError):
        # An unreadable switch never counts as on.
        return closed("uncertain_result")
    view["switch_id"] = record.switch_id
    view["turned_on"] = envelope.turned_on.model_dump(mode="json")
    view["confirmed"] = envelope.confirmed.model_dump(mode="json")

    note = _read_turned_off(root)
    try:
        events = api["SwitchLog"](root / "switch.log").read()
        from openadapt_flow.approval_mode.switch import summarize_events

        state = summarize_events(events)
    except api["SwitchLogError"]:
        if note is not None:
            return closed("turned_off", note.get("at"), here=True)
        return closed("uncertain_result")
    if state.switch_record_sha256 != record.digest() or state.switch_id != record.switch_id:
        return closed("uncertain_result")

    no_person = sum(
        1 for event in state.finished if event.decision is not None
        and event.decision.value == "no_person"
    )
    view["evidence"] = {
        "switch_id": record.switch_id,
        "record_sha256": record.digest(),
        "bundle_digest": record.bundle_content_digest,
        "runner_id": record.runner_id,
        "verifiers": len(record.verifier_identities),
        "profile": record.profile,
        "log_intact": True,
        "log_events": len(events),
        "items_finished": len(state.finished),
        "items_without_person": no_person,
    }
    if state.off_reason is not None:
        off_event = next(
            (event for event in events if event.kind == "switched_off"), None
        )
        here = note is not None and state.off_reason.value == "turned_off"
        return closed(
            state.off_reason.value,
            off_event.at if off_event is not None else None,
            here=here,
        )
    if note is not None:
        # Desktop wrote its own note but the log append didn't land. Off.
        return closed("turned_off", note.get("at"), here=True)
    if state.open_items and not item_running:
        # An item started and never finished: its save may have gone through.
        return closed("uncertain_result")
    if _manifest_digest(bundle_dir) != record.bundle_content_digest:
        return closed("version_changed")
    if not runner_id or runner_id != record.runner_id:
        return closed("environment_changed")
    view["state"] = "waiting_for_cloud" if state.status_unconfirmed else "on"
    return view


def turn_off(data_dir: Path, bundle_id: str) -> dict[str, Any]:
    """Turn the switch off from this computer. Works offline.

    Writes Flow's ``switched_off`` event first, so Flow's gate refuses the
    next save without a person, then a local note of when. A broken or missing
    log already reads as off; the note still records the person's choice.

    If the append itself fails while the log still reads as on (for example,
    on a full disk), the log is moved aside. Flow's gate reads a missing log
    as off, so the switch-off still applies before the next save. Only if that
    also fails does turning off report an error.
    """

    root = store_dir(data_dir, bundle_id)
    if not (root / "switch.json").exists() and not (root / "switch.log").exists():
        raise SwitchApprovalError("This task has no switch to turn off.")
    now = datetime.now(timezone.utc).isoformat()
    already_off = False
    logged = False
    moved_aside = False
    api = flow_switch_api()
    if api is not None:
        log = api["SwitchLog"](root / "switch.log")
        try:
            state = log.state()
            if state.off_reason is not None:
                already_off = True
            else:
                api["turn_off"](log, api["OffReason"].TURNED_OFF, at=now)
                logged = True
        except api["SwitchLogError"]:
            logged = False
        except Exception:
            try:
                (root / "switch.log").replace(root / UNUSABLE_LOG)
                moved_aside = True
            except OSError as exc:
                raise SwitchApprovalError(
                    "OpenAdapt couldn't record the switch-off on this computer."
                ) from exc
    if not already_off and _read_turned_off(root) is None:
        try:
            _write_private_json(
                root / "turned_off.json",
                {"schema": TURNED_OFF_SCHEMA, "at": now, "where": "this_computer"},
            )
        except OSError:
            if not (logged or moved_aside):
                raise
    return {
        "ok": True,
        "already_off": already_off,
        "logged": logged,
        "moved_aside": moved_aside,
        "at": now,
    }


def stored_switch_ids(data_dir: Path) -> list[str]:
    """Bundle ids that have a switch store on this computer."""

    root = Path(data_dir) / "approval_mode"
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    return [
        child.name
        for child in children
        if child.is_dir()
        and _BUNDLE_ID.fullmatch(child.name)
        and ((child / "switch.json").exists() or (child / "switch.log").exists())
    ]


# ------------------------------------------------------------- requirements


def local_requirements(workflow: Any, *, certified_and_sealed: bool) -> dict[str, Any]:
    """Requirements E1 to E8 from what this computer knows.

    Flow's evaluator is the one list (design 6). Desktop knows whether the
    version is checked and sealed, and Flow reads the workflow itself for E2
    and E3. Everything Cloud holds (the admission, attestations, readiness
    sign-off, approvals) defaults to "not recorded", which can't pass, so an
    unmet requirement here means "not confirmed on this computer".
    """

    try:
        from openadapt_flow.approval_mode.requirements import (
            DeploymentEvidence,
            RequirementsEvidence,
            evaluate_requirements,
        )
    except ImportError:
        return {"available": False, "reason": "engine_without_switch", "items": []}
    evidence = RequirementsEvidence(
        deployment=DeploymentEvidence(certified_and_sealed=certified_and_sealed)
    )
    try:
        report = evaluate_requirements(workflow, evidence)
    except Exception:
        return {"available": False, "reason": "requirements_error", "items": []}
    return {
        "available": True,
        "source": "this_computer",
        "items": [item.model_dump(mode="json") for item in report.requirements],
    }


# ------------------------------------------------------- per-item evidence

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _check(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    code = value.get("code")
    detail = value.get("detail", "ok")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return None
    if not isinstance(detail, str) or not _CODE.fullmatch(detail):
        detail = "unknown"
    return {"code": code, "passed": value.get("passed") is True, "detail": detail}


#: Every item check Flow's gate runs in ``problems_only`` (design 7.2).
REQUIRED_ITEM_CHECKS = frozenset(
    {
        "inputs_supplied",
        "channel",
        "inputs_strict",
        "not_a_retry",
        "record_key_not_recent",
        "deployment_config",
        "switch_lease",
        "not_resumed",
    }
)
#: Every pre-save check (design 7.3) and post-run check (design 7.4).
REQUIRED_PRE_CHECKS = frozenset(
    {"switch_on", "signed_in_user", "right_record", "same_screens", "record_check_ready"}
)
REQUIRED_POST_CHECKS = frozenset({"verified", "whole_run_clean"})

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Why Desktop didn't accept a block's ``no_person`` (closed codes).
UNCONFIRMED_REASONS = frozenset(
    {
        "engine_without_switch",
        "flow_rejected",
        "checks_incomplete",
        "not_verified",
        "retry_or_repeat",
        "caller_configuration",
        "switch_not_on_here",
        "switch_mismatch",
    }
)


#: How an answer reached the run: a person in Desktop's Run it or on their
#: phone (the runner's decision portal), or a delegate by its own route.
PERSON_ROUTES = frozenset({"desktop_run_it", "decision_portal"})
DELEGATE_ROUTES = frozenset({"local_cli", "webhook", "mcp"})
DELEGATE_KINDS = frozenset({"rule", "agent", "system"})


def approvers(raw: Any) -> list[dict[str, Any]]:
    """Who decided each save of an item, from Flow's ``approvals`` rows.

    Closed codes and opaque ids only. A person row names the route and, when
    signed in, the Cloud user id. A delegate row names the agent, rule or
    system and the ``sha256:`` of the person-signed grant it acted under; a
    delegate row without one reads as ``unverified``. A row Desktop can't
    read is left out, never shown as a person.
    """

    out: list[dict[str, Any]] = []
    rows = raw if isinstance(raw, list) else []
    for row in rows[:64]:
        if not isinstance(row, dict):
            continue
        decision = row.get("decision")
        if decision not in ("approved", "declined"):
            continue
        route = row.get("route", "desktop_run_it")
        decided_by = row.get("decided_by", "human")
        entry: dict[str, Any] = {
            "decision": decision,
            "route": route if route in PERSON_ROUTES | DELEGATE_ROUTES else "unknown",
            "kind": "person",
            "id": None,
            "policy_ref": None,
            "signed_in": False,
            "verified": True,
        }
        if decided_by == "automation":
            approver = row.get("approver")
            approver = approver if isinstance(approver, dict) else {}
            kind = approver.get("kind")
            approver_id = approver.get("id")
            policy_ref = approver.get("policy_ref")
            entry.update(
                kind=kind if kind in DELEGATE_KINDS else "unknown",
                id=approver_id
                if isinstance(approver_id, str) and _OPAQUE_ID.fullmatch(approver_id)
                else None,
                policy_ref=policy_ref
                if isinstance(policy_ref, str) and _PREFIXED_SHA256.fullmatch(policy_ref)
                else None,
            )
            entry["verified"] = (
                entry["kind"] != "unknown"
                and entry["id"] is not None
                and entry["policy_ref"] is not None
                and route in DELEGATE_ROUTES
            )
        elif decided_by == "human":
            approver_id = row.get("approver_id")
            entry["signed_in"] = row.get("approver_kind") == "cloud_user"
            entry["id"] = (
                approver_id
                if isinstance(approver_id, str) and _OPAQUE_ID.fullmatch(approver_id)
                else None
            )
            entry["verified"] = route in PERSON_ROUTES
        else:
            continue
        out.append(entry)
    return out


def _unconfirmed(approval: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """The same block, now needing a person and never counting as without one."""

    out = dict(approval)
    out.update(
        validated=False,
        decision="unverified",
        needs_person=True,
        unconfirmed=reason if reason in UNCONFIRMED_REASONS else "checks_incomplete",
    )
    return out


def item_approval(raw: Any) -> dict[str, Any] | None:
    """Project Flow's per-item ``approval`` block to closed codes.

    Returns None when the report has no block (the run had no approval gate).
    A block Flow's own model rejects, or one Desktop can't validate because
    this Flow lacks the model, is kept as ``unverified``: it needs a person
    and never counts as saved without one.

    Desktop is stricter than Flow's model about ``no_person``. Flow's model
    accepts a ``no_person`` block with no item checks and any non-empty set
    of passed pre-save checks. Desktop requires every item check, every
    pre-save check and both post-run checks that Flow's gate runs, each
    passed, and the switch id and record digest the block was decided under.
    """

    if raw is None:
        return None
    validated = False
    unconfirmed: str | None = None
    try:
        from openadapt_flow.approval_mode.model import ApprovalModeItemEvidence

        ApprovalModeItemEvidence.model_validate(raw)
        validated = True
    except ImportError:
        validated = False
        unconfirmed = "engine_without_switch"
    except (ValidationError, ValueError, TypeError):
        validated = False
        unconfirmed = "flow_rejected"
    block = raw if isinstance(raw, dict) else {}

    def code(name: str) -> str | None:
        value = block.get(name)
        return value if isinstance(value, str) and _CODE.fullmatch(value) else None

    def matching(name: str, pattern: re.Pattern[str]) -> str | None:
        value = block.get(name)
        return value if isinstance(value, str) and pattern.fullmatch(value) else None

    checks = {
        group: [c for c in (_check(v) for v in block.get(group) or []) if c is not None]
        for group in ("item_checks", "pre_checks", "post_checks")
    }
    failed = next(
        (
            check
            for group in ("item_checks", "pre_checks", "post_checks")
            for check in checks[group]
            if not check["passed"]
        ),
        None,
    )

    def every_check_passed(group: str, required: frozenset[str]) -> bool:
        listed = checks[group]
        return (
            len(listed) == len(block.get(group) or [])
            and required <= {check["code"] for check in listed}
            and all(check["passed"] for check in listed)
        )

    switch_id = matching("switch_id", _OPAQUE_ID)
    record_sha256 = matching("switch_record_sha256", _PREFIXED_SHA256)
    if validated and code("decision") == "no_person" and not (
        every_check_passed("item_checks", REQUIRED_ITEM_CHECKS)
        and every_check_passed("pre_checks", REQUIRED_PRE_CHECKS)
        and every_check_passed("post_checks", REQUIRED_POST_CHECKS)
        and switch_id is not None
        and record_sha256 is not None
        and block.get("switch_log_recorded") is True
    ):
        validated = False
        unconfirmed = "checks_incomplete"
    decision = code("decision") if validated else "unverified"
    needs_person = (block.get("needs_person") is not False) or not validated
    post = {check["code"]: check["passed"] for check in checks["post_checks"]}
    off = code("switch_off_reason")
    return {
        "validated": validated,
        "unconfirmed": unconfirmed,
        "mode": code("mode"),
        "decision": decision,
        "reason": code("decision_reason"),
        "needs_person": needs_person,
        "counts_toward_problems": block.get("counts_toward_problems") is True,
        "switch_off_reason": off if off in OFF_REASONS else None,
        "switch_id": switch_id,
        "switch_record_sha256": record_sha256,
        "failed_check": failed,
        "post_checks": {
            "verified": post.get("verified") is True,
            "whole_run_clean": post.get("whole_run_clean") is True,
        },
        "saves_without_person": (
            block.get("saves_without_person")
            if isinstance(block.get("saves_without_person"), int)
            else 0
        ),
        "approvers": approvers(block.get("approvals")),
    }


def confirm_no_person(
    approval: Mapping[str, Any] | None,
    *,
    consistent_tx: str | None,
    local_switch: Mapping[str, Any] | None,
    retry_or_repeat: bool,
    caller_configuration: bool,
) -> dict[str, Any] | None:
    """Desktop's own checks before an item may close without a person.

    Flow decides ``no_person``; Desktop accepts it only when what this
    computer knows agrees (design 5.2, step 5: the runner uses the switch only
    if its own records agree). Otherwise the item goes to a person:

    * the transaction outcome is a consistent ``VERIFIED`` (design R1; a
      ``VERIFIED`` execution outcome with no transaction outcome fails);
    * the item isn't a retry or a repeat, and the run used the task's own
      configuration (design 7.2; Desktop knows this, Flow doesn't);
    * this computer has the switch on for this task version, with the same
      switch id and record digest the block names.

    Any other block is returned unchanged.
    """

    if approval is None:
        return None
    if approval.get("decision") != "no_person":
        return dict(approval)
    if consistent_tx != "VERIFIED":
        return _unconfirmed(approval, "not_verified")
    if retry_or_repeat:
        return _unconfirmed(approval, "retry_or_repeat")
    if caller_configuration:
        return _unconfirmed(approval, "caller_configuration")
    if local_switch is None or local_switch.get("state") != "on":
        return _unconfirmed(approval, "switch_not_on_here")
    evidence = local_switch.get("evidence") or {}
    if (
        local_switch.get("switch_id") != approval.get("switch_id")
        or evidence.get("record_sha256") != approval.get("switch_record_sha256")
    ):
        return _unconfirmed(approval, "switch_mismatch")
    return dict(approval)


def saved_without_person(approval: Mapping[str, Any] | None, consistent_tx: str | None) -> bool:
    """True only when Flow decided ``no_person`` and every condition holds.

    The conditions are the outcome safety rule (design 8): Flow's validated
    block says ``no_person`` and doesn't need a person, R1 and R2 both passed,
    and the item's transaction outcome is a consistent ``VERIFIED``. A
    ``VERIFIED`` execution outcome with no transaction outcome never counts.
    """

    if not approval or consistent_tx != "VERIFIED":
        return False
    post = approval.get("post_checks") or {}
    return bool(
        approval.get("validated") is True
        and approval.get("mode") == "problems_only"
        and approval.get("decision") == "no_person"
        and approval.get("needs_person") is False
        and post.get("verified") is True
        and post.get("whole_run_clean") is True
    )


def parse_stored(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "NOT_BUILT",
    "OFF_REASONS",
    "SwitchApprovalError",
    "SwitchEnvelope",
    "accept_switch",
    "approvers",
    "blockers",
    "confirm_no_person",
    "flow_switch_api",
    "item_approval",
    "local_requirements",
    "parse_stored",
    "read_switch",
    "saved_without_person",
    "store_dir",
    "stored_switch_ids",
    "turn_off",
]
