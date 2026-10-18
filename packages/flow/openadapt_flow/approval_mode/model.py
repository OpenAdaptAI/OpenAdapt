"""PHI-free data model for approval modes.

Everything here is import-light (pydantic and the standard library) because
:mod:`openadapt_flow.ir` embeds :class:`ApprovalModeItemEvidence` in the run
report. No field holds an input value, screen text or free text: reasons are
closed codes, and identifiers are digests or workflow step ids.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openadapt_flow.approval_mode.approver import ApproverIdentityV1

#: A closed, machine-readable reason code. Never free text, never a value.
CODE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
#: Opaque identifiers issued by Cloud or the runner (switch, runner, org).
OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
PREFIXED_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
HEX_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ApprovalMode(str, Enum):
    """Whether a person approves an item before it is saved."""

    #: The default. A person approves every item before its first save.
    EACH_ITEM = "each_item"
    #: "Only ask a person about problems."
    PROBLEMS_ONLY = "problems_only"
    #: Today's behavior with no per-item pause, set explicitly and recorded.
    NOT_GATED = "not_gated"


def resolve_approval_mode(value: object) -> ApprovalMode:
    """Return the mode a run must use for ``value``.

    A missing, malformed or unknown value means ``each_item``: when the
    software cannot tell which mode was meant, a person approves the item.
    ``problems_only`` and ``not_gated`` apply only when named exactly.
    """

    if isinstance(value, ApprovalMode):
        return value
    if isinstance(value, str):
        for mode in ApprovalMode:
            if value == mode.value:
                return mode
    return ApprovalMode.EACH_ITEM


class OffReason(str, Enum):
    """Why the switch turned off (design section 7.6). Closed set."""

    UNCERTAIN_RESULT = "uncertain_result"
    WRONG_ITEM_FOUND = "wrong_item_found"
    TOO_MANY_PROBLEMS = "too_many_problems"
    SCREEN_CHANGED = "screen_changed"
    RECORD_CHECK_LOST = "record_check_lost"
    SPOT_CHECKS_OVERDUE = "spot_checks_overdue"
    VERSION_CHANGED = "version_changed"
    ENVIRONMENT_CHANGED = "environment_changed"
    ADMISSION_LOST = "admission_lost"
    SETUP_LAPSED = "setup_lapsed"
    TURNED_OFF = "turned_off"


#: Item checks (design 7.2). A failure sends the item to per-item approval and
#: never counts toward ``too_many_problems``.
ItemCheckCode = Literal[
    "channel",
    "inputs_strict",
    "inputs_supplied",
    "not_a_retry",
    "record_key_not_recent",
    "deployment_config",
    "switch_lease",
    "not_resumed",
]
#: Pre-save checks (design 7.3), evaluated before every consequential save.
PreCheckCode = Literal[
    "switch_on",
    "signed_in_user",
    "right_record",
    "same_screens",
    "record_check_ready",
]
#: Post-run checks (design 7.4).
PostCheckCode = Literal["verified", "whole_run_clean"]
#: Codes for the gate's own failure handling and for the each-item gate.
GateCode = Literal["each_item", "gate_error", "switch_log"]

CheckCode = Literal[
    "channel",
    "inputs_strict",
    "inputs_supplied",
    "not_a_retry",
    "record_key_not_recent",
    "deployment_config",
    "switch_lease",
    "not_resumed",
    "switch_on",
    "signed_in_user",
    "right_record",
    "same_screens",
    "record_check_ready",
    "verified",
    "whole_run_clean",
    "each_item",
    "gate_error",
    "switch_log",
]


class CheckOutcome(BaseModel):
    """The result of one named check, with a closed-vocabulary detail code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CheckCode
    passed: bool
    #: Why the check failed (or ``ok``). A closed code, never free text.
    detail: str = Field(default="ok", pattern=CODE_PATTERN)
    #: The workflow step the check ran before, for pre-save checks.
    step_id: Optional[str] = Field(default=None, max_length=256)


class ItemDecisionKind(str, Enum):
    """What happened to one item under its approval mode."""

    #: Saved without a person: every pre-check, R1 and R2 passed.
    NO_PERSON = "no_person"
    #: The item pauses for a person to approve it before the save.
    APPROVAL_REQUIRED = "approval_required"
    #: Stopped before saving, as for an expired login.
    STOP_BEFORE_SAVING = "stop_before_saving"
    #: The item reached a result that needs a person (after or without a save).
    PROBLEM = "problem"
    #: ``not_gated``: no per-item pause, recorded as such.
    NOT_GATED = "not_gated"
    #: A person approved every save of the item, and the item reached
    #: ``VERIFIED``. Never counted as saved without a person.
    APPROVED = "approved"
    #: A person declined a save ("Don't save"). The item stopped before that
    #: save; earlier saves of the same item, if any, stay as they are.
    DECLINED = "declined"


#: The message prefix the gate uses when an item waits for a person's
#: approval before a save. The durable pause classifier keys on it.
ITEM_APPROVAL_PAUSE_MARKER = (
    "Approval mode: this item is waiting for a person to approve it before saving"
)
#: The message prefix the gate uses when a person declined a save.
ITEM_DECLINED_MARKER = "Approval mode: a person declined this save"

#: Check detail when the record check is the separate-session UI read-back
#: (tier 2). That read-back is a pilot: it counts as record evidence while a
#: person approves each entry (each_item), and it never lets an entry save
#: without a person under problems_only.
UI_READBACK_PILOT = "ui_readback_pilot_each_item_only"

#: Closed decline reasons a person picks in Desktop (design 9.1).
DeclineReason = Literal["wrong_values", "wrong_record", "screen_problem"]
DECLINE_REASONS: tuple[str, ...] = ("wrong_values", "wrong_record", "screen_problem")
#: The only route that may approve or decline an item in phase 0.
ITEM_APPROVAL_ROUTE: Literal["desktop_run_it"] = "desktop_run_it"
#: Routes a person answers by: Desktop's Run it, or their phone through Cloud.
PERSON_APPROVAL_ROUTES = frozenset({"desktop_run_it", "cloud_phone"})
#: How the approver was authenticated. ``none`` only for ``unauthenticated``.
ApproverAuthMethod = Literal["cloud_aal2", "cloud_aal1", "none"]


class ItemApprovalRef(BaseModel):
    """One person's answer to one item-approval request, as PHI-free codes.

    The approver is a Cloud user id (never a name), or ``unauthenticated``
    when the person approved in Desktop while signed out. Digests bind the
    exact request the person saw and the exact answer file.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=256)
    request_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    answer_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    decision: Literal["approved", "declined"]
    decline_reason: Optional[DeclineReason] = None
    decided_by: Literal["human", "automation"] = "human"
    #: ``desktop_run_it`` or ``cloud_phone`` (a Cloud-signed decision from
    #: the person's phone) for a person; ``local_cli``, ``webhook`` or ``mcp``
    #: for a delegated approver.
    route: Literal["desktop_run_it", "cloud_phone", "local_cli", "webhook", "mcp"] = (
        ITEM_APPROVAL_ROUTE
    )
    approver_kind: Literal["cloud_user", "unauthenticated"]
    approver_id: Optional[str] = Field(default=None, pattern=OPAQUE_ID_PATTERN)
    auth_method: ApproverAuthMethod
    #: The agent, rule or system that decided, when it wasn't a person. Its
    #: id is opaque. Left out of the report when absent, so person rows keep
    #: their bytes. Such a row is never a training label.
    approver: Optional[ApproverIdentityV1] = None

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.approver is None:
            data.pop("approver", None)
        return data

    @model_validator(mode="after")
    def _consistent(self) -> "ItemApprovalRef":
        if (self.decision == "declined") != (self.decline_reason is not None):
            raise ValueError("a declined save needs exactly one closed reason")
        if (self.decided_by == "automation") != (self.approver is not None):
            raise ValueError("an automated decision, and only one, names its approver")
        if self.approver is not None and self.approver.is_person:
            raise ValueError("a person's decision uses the person fields")
        if (self.route in PERSON_APPROVAL_ROUTES) != (self.decided_by == "human"):
            raise ValueError(
                "a person answers in Desktop or on their phone; a delegate by its own route"
            )
        if self.route == "cloud_phone" and self.auth_method != "cloud_aal2":
            raise ValueError("a phone approval needs a Cloud sign-in with AAL2")
        if self.approver_kind == "cloud_user":
            if self.approver_id is None or self.auth_method == "none":
                raise ValueError("a Cloud approver needs an id and a sign-in method")
        elif self.approver_id is not None or self.auth_method != "none":
            raise ValueError("an unauthenticated approval names no approver")
        return self


class SwitchThresholds(BaseModel):
    """Conservative public defaults (design 7.6). Deployments may only tighten.

    Tuned values are private configuration and stay out of this repository;
    the validator refuses any value looser than these defaults.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: A signed Cloud status is fresh for this long on the monotonic clock.
    status_max_age_s: int = Field(default=900, gt=0, le=900)
    #: Problems in a row that turn the switch off.
    problems_in_a_row: int = Field(default=3, ge=1, le=3)
    #: Problems within the last ``problems_window`` items that turn it off.
    problems_in_window: int = Field(default=5, ge=1, le=5)
    problems_window: int = Field(default=20, ge=20, le=1000)

    @model_validator(mode="after")
    def _window_holds_its_count(self) -> "SwitchThresholds":
        if self.problems_in_window > self.problems_window:
            raise ValueError("problems_in_window cannot exceed problems_window")
        return self


#: The only list channel the switch covers in phase 1.
DESKTOP_LIST_WITH_HEADER: Literal["desktop_run_it_list_with_header"] = (
    "desktop_run_it_list_with_header"
)


class ItemContext(BaseModel):
    """What the caller knows about one item before it runs (design 7.2).

    Desktop fills this from its own list parsing, work-item history and lease.
    Every field defaults to the value that fails its check, so an omitted
    field sends the item to per-item approval rather than past a person.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Optional[str] = Field(default=None, pattern=CODE_PATTERN)
    #: The header row maps every declared input.
    header_maps_every_input: bool = False
    #: Every row has exactly the header's number of cells, and a single-input
    #: list was not split on commas.
    rows_well_formed: bool = False
    #: Each value passed its input type (date, number, choice list).
    values_pass_types: bool = False
    is_retry: bool = True
    allow_repeat: bool = True
    #: Whether the item's record key was entered through this workflow within
    #: the repeat window. ``None`` (unknown) fails the check.
    record_key_seen_recently: Optional[bool] = None
    #: The run uses a caller-supplied configuration or target.
    caller_supplied_configuration: bool = True
    #: The runner holds the switch's lease, and no other list of this
    #: workflow is running on this runner.
    holds_switch_lease: bool = False


ITEM_EVIDENCE_SCHEMA: Literal["openadapt.approval-mode-item/v1"] = (
    "openadapt.approval-mode-item/v1"
)


class ApprovalModeItemEvidence(BaseModel):
    """The per-item approval block stored in the run report (design 10).

    PHI-free: codes, counts, digests and step ids only. The model enforces the
    safety rule at the evidence boundary: an item can be recorded as saved
    without a person only in ``problems_only`` mode, only after at least one
    save passed every pre-check, and only when both post-checks passed.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.approval-mode-item/v1"] = ITEM_EVIDENCE_SCHEMA
    mode: ApprovalMode
    channel: Optional[str] = Field(default=None, pattern=CODE_PATTERN)
    switch_id: Optional[str] = Field(default=None, pattern=OPAQUE_ID_PATTERN)
    switch_record_sha256: Optional[str] = Field(
        default=None, pattern=PREFIXED_SHA256_PATTERN
    )
    item_checks: list[CheckOutcome] = Field(default_factory=list)
    pre_checks: list[CheckOutcome] = Field(default_factory=list)
    post_checks: list[CheckOutcome] = Field(default_factory=list)
    #: Saves that passed every pre-check and were delivered without a person.
    saves_without_person: int = Field(default=0, ge=0)
    #: The step whose pre-save gate sent the item to a person, if any.
    gated_step_id: Optional[str] = Field(default=None, max_length=256)
    decision: ItemDecisionKind
    decision_reason: Optional[str] = Field(default=None, pattern=CODE_PATTERN)
    needs_person: bool
    counts_toward_problems: bool = False
    switch_off_reason: Optional[OffReason] = None
    #: True when the runner's switch log recorded this item's result.
    switch_log_recorded: bool = False
    #: Every person's answer to an approval request in this item, in order.
    approvals: list[ItemApprovalRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_person_requires_every_check(self) -> "ApprovalModeItemEvidence":
        decision = self.decision
        if decision is ItemDecisionKind.NO_PERSON:
            if self.mode is not ApprovalMode.PROBLEMS_ONLY:
                raise ValueError("only problems_only can save without a person")
            if self.needs_person:
                raise ValueError("an item saved without a person cannot need one")
            if self.saves_without_person < 1:
                raise ValueError("no_person requires at least one gated save")
            if not self.pre_checks or not all(c.passed for c in self.pre_checks):
                raise ValueError("no_person requires every pre-save check to pass")
            codes = {c.code for c in self.post_checks if c.passed}
            if codes != {"verified", "whole_run_clean"} or not all(
                c.passed for c in self.post_checks
            ):
                raise ValueError("no_person requires both post-run checks to pass")
            if self.item_checks and not all(c.passed for c in self.item_checks):
                raise ValueError("no_person requires every item check to pass")
            if self.switch_off_reason is not None:
                raise ValueError("an item that switched the mode off needs a person")
            if not self.switch_log_recorded:
                raise ValueError("no_person requires a recorded switch-log entry")
        elif decision is ItemDecisionKind.NOT_GATED:
            if self.mode is not ApprovalMode.NOT_GATED:
                raise ValueError("not_gated decision requires not_gated mode")
        elif decision is ItemDecisionKind.APPROVED:
            if self.mode is ApprovalMode.NOT_GATED:
                raise ValueError("not_gated items are never approved per item")
            if not self.approvals or any(
                ref.decision != "approved" for ref in self.approvals
            ):
                raise ValueError("an approved item needs a person's approval")
            if self.saves_without_person:
                raise ValueError("an approved item has no save without a person")
            if self.needs_person:
                raise ValueError("an approved, checked item needs no person")
            if self.switch_off_reason is not None:
                raise ValueError("an item that switched the mode off needs a person")
        elif decision is ItemDecisionKind.DECLINED:
            if self.mode is ApprovalMode.NOT_GATED:
                raise ValueError("not_gated items are never declined per item")
            if not self.approvals or self.approvals[-1].decision != "declined":
                raise ValueError("a declined item needs a person's decline")
        else:
            if not self.needs_person:
                raise ValueError(f"a {decision.value} item needs a person")
        if self.mode is not ApprovalMode.PROBLEMS_ONLY and (
            self.switch_off_reason is not None or self.saves_without_person
        ):
            raise ValueError("only problems_only records saves without a person")
        return self
