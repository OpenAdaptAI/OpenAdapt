"""Per-item approval requests and a person's answers (phase 0).

Under ``each_item`` the engine stops before every consequential save and asks
a person. The request and the answer are two small files in the run
directory, so the waiting run and the app that shows the question (Desktop's
Run it) need no other channel:

- ``item-approval/request-<id>.json`` (:class:`ItemApprovalRequest`), written
  by the run. It binds the run, the save step, the bundle and input digests,
  the screenshot of the filled form the person is shown, and a digest of the
  form's field state at that moment.
- ``item-approval/answer-<id>.json`` (:class:`ItemApprovalAnswer`), written
  once by ``openadapt-flow item-approval approve|decline``. It names the
  request digest the person saw, who decided (``decided_by`` must be
  ``human`` for a person), the route (Desktop Run it, or the decision
  portal on a paired phone) and the approver (a Cloud user id, or
  ``unauthenticated`` when nobody was signed in).
- ``item-approval/consumed-<id>``, created once by the run when it uses an
  approval for exactly one save. An approval is never used twice.

Files are created exclusively and never overwritten. The run re-validates
every answer itself; the CLI's checks are a convenience, not the control.
Nothing here is sent anywhere. The run report keeps only the PHI-free
:class:`~openadapt_flow.approval_mode.model.ItemApprovalRef` codes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from openadapt_flow.approval_mode.approver import (
    DEFAULT_APPROVER_POLICY,
    DELEGATED_ROUTES,
    Approver,
    ApproverIdentityV1,
    ApproverPolicyV1,
    StepClass,
    WebhookRefused,
)
from openadapt_flow.approval_mode.model import (
    DECISION_PORTAL_ROUTE,
    ITEM_APPROVAL_ROUTE,
    OPAQUE_ID_PATTERN,
    PREFIXED_SHA256_PATTERN,
    ApprovalMode,
    ApproverAuthMethod,
    DeclineReason,
    ItemApprovalRef,
)
from openadapt_flow.approval_mode.signed_policy import (
    GrantRefused,
    SignedApproverGrant,
    VerifiedGrant,
    allows_non_person,
    verify_grant,
)
from openadapt_flow.approval_mode.switch import canonical_json

REQUEST_SCHEMA: Literal["openadapt.item-approval-request/v1"] = (
    "openadapt.item-approval-request/v1"
)
ANSWER_SCHEMA: Literal["openadapt.item-approval-answer/v1"] = (
    "openadapt.item-approval-answer/v1"
)
APPROVAL_DIRNAME = "item-approval"
#: The run's approver policy, written once by the run. Absent means person-only.
#: How often a waiting run asks a delegated approver again (seconds).
DELEGATE_POLL_S = 5.0
POLICY_FILENAME = "approver-policy.json"
#: The person-signed grant behind a policy that lets a delegate answer.
GRANT_FILENAME = "approver-grant.json"
#: Who started the run, written once by the run. A delegate never answers a
#: run it started.
INITIATOR_FILENAME = "run-initiator.json"
#: The initiator an agent-started run records when the agent has no id.
UNIDENTIFIED_AGENT = "agent:unidentified"
#: Routes a person answers by: Desktop's Run it, or their phone through the
#: runner's decision portal.
PERSON_ROUTES = frozenset({ITEM_APPROVAL_ROUTE, DECISION_PORTAL_ROUTE})
#: How long a run waits for an answer before it stops before the save.
DEFAULT_APPROVAL_WAIT_S = 900.0
MAX_APPROVAL_WAIT_S = 4 * 3600.0
#: How long an answer to a request stays usable, including by a later resume.
REQUEST_TTL_S = 4 * 3600.0
_MAX_FILE_BYTES = 64 * 1024
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_REQUEST_ID_PATTERN = r"^[0-9a-f]{32}$"
_HEX_SHA256 = r"^[0-9a-f]{64}$"


class ItemApprovalRefused(ValueError):
    """An answer or request is not usable. ``code`` is a closed reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps must carry a time zone")
    return parsed.astimezone(timezone.utc)


def _digest(model: BaseModel) -> str:
    payload = canonical_json(model.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(payload).hexdigest()


#: The most field rows one request may carry. Each row is short, so the
#: request file stays well under its 64 KiB read limit.
MAX_SAVE_FIELDS = 24
_MAX_FIELD_VALUE = 512


class SaveField(BaseModel):
    """One value a save writes, as the person sees it on the phone.

    Local only, like the screenshot: it is inside the request file and its
    digest, served to the paired phone no-store, and never copied into the
    report, a notification, or anything a delegated approver or Cloud sees.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=128)
    #: The value before the run changed it, when known. ``None`` when the
    #: field was empty or the run can't tell.
    before: Optional[str] = Field(default=None, max_length=_MAX_FIELD_VALUE)
    after: Optional[str] = Field(default=None, max_length=_MAX_FIELD_VALUE)


class ItemApprovalRequest(BaseModel):
    """What the person is asked to approve: one save of one item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.item-approval-request/v1"] = REQUEST_SCHEMA
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    run_id: str = Field(min_length=1, max_length=256)
    workflow_name: str = Field(min_length=1, max_length=512)
    step_id: str = Field(min_length=1, max_length=256)
    step_index: int = Field(ge=0)
    #: The compiled step's own intent ("Click 'Save'"). Local only.
    step_intent: str = Field(default="", max_length=1024)
    mode: ApprovalMode
    bundle_content_digest: Optional[str] = Field(default=None, pattern=_HEX_SHA256)
    runtime_inputs_digest: Optional[str] = Field(default=None, pattern=_HEX_SHA256)
    #: Run-directory-relative path of the screenshot the person is shown.
    screenshot: str = Field(min_length=1, max_length=256)
    screenshot_sha256: str = Field(pattern=_HEX_SHA256)
    #: How the field state was read: the page's own form controls, or the
    #: text on the screen where the application exposes no form controls.
    form_state_kind: Literal["dom", "screen_text"]
    form_state_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    created_at: str
    expires_at: str
    #: Set when the run is a qualification case. A drill's own fault step
    #: never asks; the drill must stop there by itself.
    qualification_case_kind: Optional[str] = Field(default=None, max_length=64)
    #: What kind of save this is, for the approver policy. Left out of the
    #: file when it is the default, so earlier requests keep their digest.
    step_class: StepClass = StepClass.CONSEQUENTIAL
    #: The values this save writes, for the phone. Part of the digest, so the
    #: signed task, the answer and the run all bind exactly these values.
    #: Left out of the file when absent, so earlier requests keep their digest.
    fields: Optional[tuple[SaveField, ...]] = Field(
        default=None, min_length=1, max_length=MAX_SAVE_FIELDS
    )

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.step_class is StepClass.CONSEQUENTIAL:
            data.pop("step_class", None)
        if self.fields is None:
            data.pop("fields", None)
        return data

    @model_validator(mode="after")
    def _times(self) -> "ItemApprovalRequest":
        created = _parse_time(self.created_at)
        expires = _parse_time(self.expires_at)
        if expires <= created:
            raise ValueError("an approval request must expire after it is made")
        if (expires - created).total_seconds() > REQUEST_TTL_S + 1:
            raise ValueError("an approval request may not outlive its window")
        if self.screenshot != f"{APPROVAL_DIRNAME}/{self.request_id}.png":
            raise ValueError("the request screenshot must be its own file")
        return self

    def digest(self) -> str:
        return _digest(self)

    def expired(self, now: Optional[datetime] = None) -> bool:
        return (now or _utc_now()) > _parse_time(self.expires_at)


class ItemApprovalAnswer(BaseModel):
    """A person's answer to one request. Written once, never changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.item-approval-answer/v1"] = ANSWER_SCHEMA
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    decision: Literal["approve", "decline"]
    decline_reason: Optional[DeclineReason] = None
    #: Parsed as given so a wrong value is refused with a reason, not
    #: silently coerced. Only ``human`` is ever accepted.
    decided_by: Literal["human", "automation", "unknown"]
    route: str = Field(min_length=1, max_length=64)
    approver_kind: Literal["cloud_user", "unauthenticated"]
    approver_id: Optional[str] = Field(default=None, pattern=OPAQUE_ID_PATTERN)
    auth_method: ApproverAuthMethod
    answered_at: str
    #: Who or what decided. Required for an agent, rule or system; optional
    #: for a person, whose older fields above already name them. Left out of
    #: the file when absent, so earlier answers keep their digest.
    approver: Optional[ApproverIdentityV1] = None

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.approver is None:
            data.pop("approver", None)
        return data

    def digest(self) -> str:
        return _digest(self)

    def approver_identity(self) -> ApproverIdentityV1:
        """Who decided, for the audit trail."""

        if self.approver is not None:
            return self.approver
        from openadapt_flow.approval_mode.approver import person_identity

        return person_identity(self.approver_id, self.auth_method)

    def reference(self, request: ItemApprovalRequest) -> ItemApprovalRef:
        """The PHI-free evidence row for the run report."""

        return ItemApprovalRef(
            step_id=request.step_id,
            request_sha256=request.digest(),
            answer_sha256=self.digest(),
            decision="approved" if self.decision == "approve" else "declined",
            decline_reason=self.decline_reason,
            approver_kind=self.approver_kind,
            approver_id=self.approver_id,
            auth_method=self.auth_method,
            decided_by="human" if self.decided_by == "human" else "automation",
            route=self.route,  # type: ignore[arg-type]
            approver=(
                self.approver
                if self.approver is not None and not self.approver.is_person
                else None
            ),
        )


@dataclass(frozen=True)
class ApproverContext:
    """Everything an answer is checked against, read from the run's store.

    ``policy`` is person-only unless ``grant`` is a person-signed grant that
    verified just now. ``initiator`` is who started the run (``None`` for a
    person or operator); ``initiator_known`` is False when the run never
    recorded it, and then no delegate may answer.
    """

    policy: ApproverPolicyV1 = DEFAULT_APPROVER_POLICY
    grant: Optional[VerifiedGrant] = None
    grant_refusal: Optional[str] = None
    initiator: Optional[str] = None
    initiator_known: bool = False


def _as_context(
    policy: "ApproverPolicyV1 | ApproverContext | None",
) -> ApproverContext:
    if isinstance(policy, ApproverContext):
        return policy
    # A bare policy carries no signed grant, so it can never admit a delegate.
    return ApproverContext(policy=policy or DEFAULT_APPROVER_POLICY)


def _delegate_refusal(
    request: ItemApprovalRequest,
    approver: ApproverIdentityV1,
    context: ApproverContext,
) -> Optional[str]:
    """Why this delegate can't answer this request under the run's grant."""

    grant = context.grant
    if grant is None and context.grant_refusal is not None:
        # The run asked for delegates, but the grant didn't verify: say why.
        return context.grant_refusal
    refusal = context.policy.refusal(request.step_class, approver)
    if refusal is not None:
        return refusal.value
    if grant is None:
        return "approver_policy_unsigned"
    if not grant.names(approver.id):
        return "delegate_not_in_grant"
    if not grant.covers(request.workflow_name):
        return "workflow_not_in_grant"
    if approver.policy_ref is not None and approver.policy_ref != grant.policy_ref:
        return "policy_ref_mismatch"
    if not context.initiator_known:
        return "run_initiator_unknown"
    if context.initiator == UNIDENTIFIED_AGENT:
        # An agent we can't name started the run: any delegate might be it.
        return "self_approval_unprovable"
    if context.initiator is not None and (
        context.initiator.lower() == (approver.id or "").lower()
        or context.initiator.lower() == (approver.on_behalf_of or "").lower()
    ):
        return "self_approval"
    return None


def answer_refusal(
    request: ItemApprovalRequest,
    answer: ItemApprovalAnswer,
    *,
    now: Optional[datetime] = None,
    policy: "ApproverPolicyV1 | ApproverContext | None" = None,
) -> Optional[str]:
    """Return a closed reason this answer can't be used, or ``None``.

    The run calls this itself; it never trusts that the CLI checked.
    ``policy`` is the run's :class:`ApproverContext` (``store.context()``); a
    bare policy or ``None`` means no delegate may answer. A person answers
    through Desktop's Run it, or on their phone through the runner's
    decision portal. An agent or system answers only under a verified,
    person-signed grant that names it, allows its kind for the step class,
    and wasn't started by it.
    """

    context = _as_context(policy)
    if answer.request_id != request.request_id:
        return "request_mismatch"
    if answer.request_sha256 != request.digest():
        return "request_digest_mismatch"
    approver = answer.approver
    if approver is None or approver.is_person:
        if answer.decided_by != "human":
            return "not_decided_by_a_person"
        if answer.route not in PERSON_ROUTES:
            return "route_not_allowed"
        if approver is not None and approver.auth.value != answer.approver_kind:
            return "answer_fields_inconsistent"
    else:
        if answer.decided_by != "automation":
            return "answer_fields_inconsistent"
        if answer.route not in DELEGATED_ROUTES:
            return "route_not_allowed"
        if answer.approver_kind != "unauthenticated" or answer.approver_id is not None:
            # The older person fields stay empty; ``approver`` names who decided.
            return "answer_fields_inconsistent"
        refusal_code = _delegate_refusal(request, approver, context)
        if refusal_code is not None:
            return refusal_code
    try:
        answer.reference(request)
    except ValidationError:
        return "answer_fields_inconsistent"
    try:
        answered = _parse_time(answer.answered_at)
    except ValueError:
        return "answer_time_invalid"
    created = _parse_time(request.created_at)
    expires = _parse_time(request.expires_at)
    if answered < created:
        return "answer_predates_request"
    if answered > expires:
        return "answer_after_expiry"
    if (now or _utc_now()) > expires:
        return "request_expired"
    return None


# -- files -------------------------------------------------------------------------


def _open_flags(base: int) -> int:
    flags = base
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _read_regular(path: Path, *, limit: int = _MAX_FILE_BYTES) -> bytes:
    descriptor = os.open(path, _open_flags(os.O_RDONLY))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > limit:
            raise OSError("file too large")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise OSError("file too large")
        return data
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes) -> None:
    """Create ``path`` once (0600), never replacing anything already there."""

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(tmp, _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # link() fails when the target exists, so an existing answer is never
        # replaced, and readers never see a half-written file.
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ItemApprovalStore:
    """The ``item-approval`` folder of one run directory."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        key_dir: Optional[Path | str] = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir / APPROVAL_DIRNAME
        #: Where the signer registry lives (``$OPENADAPT_JIT_KEY_DIR`` or
        #: ``~/.openadapt/jit`` when ``None``). Keep it away from the agents.
        self._key_dir = Path(key_dir) if key_dir is not None else None
        # The policy this store wrote at run start. Once set, it is the only
        # policy this store applies, whatever the file says later.
        self._pinned_policy: Optional[ApproverPolicyV1] = None

    # -- layout --------------------------------------------------------------
    def _checked_dir(self, *, create: bool) -> Path:
        if create:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.dir, 0o700)
            except FileExistsError:
                pass
        info = os.lstat(self.dir)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ItemApprovalRefused(
                "store_unsafe", "the item-approval folder is not a plain folder"
            )
        return self.dir

    def _path(self, name: str) -> Path:
        return self._checked_dir(create=False) / name

    @staticmethod
    def _check_id(request_id: str) -> str:
        if (
            not isinstance(request_id, str)
            or len(request_id) != 32
            or any(ch not in "0123456789abcdef" for ch in request_id)
        ):
            raise ItemApprovalRefused("request_unknown", "unknown approval request")
        return request_id

    # -- approver policy -----------------------------------------------------
    def write_policy(self, policy: ApproverPolicyV1) -> None:
        """Record the run's approver policy once, and apply only it from now
        on. A second, different policy is refused; the same policy again is
        a no-op."""

        self._checked_dir(create=True)
        payload = canonical_json(policy.model_dump(mode="json"))
        try:
            _write_exclusive(self.dir / POLICY_FILENAME, payload)
        except FileExistsError:
            if self._policy_on_disk() != policy:
                raise ItemApprovalRefused(
                    "policy_changed", "this run already has a different approver policy"
                ) from None
        self._pinned_policy = policy

    def write_grant(self, signed: SignedApproverGrant) -> None:
        """Record the person-signed grant once, and the policy it carries."""

        self._checked_dir(create=True)
        payload = canonical_json(signed.model_dump(mode="json", by_alias=True))
        try:
            _write_exclusive(self.dir / GRANT_FILENAME, payload)
        except FileExistsError:
            existing = self._read_grant()
            if existing != signed:
                raise ItemApprovalRefused(
                    "policy_changed", "this run already has a different approver grant"
                ) from None
        self.write_policy(signed.grant.approver_policy)

    def _read_grant(self) -> Optional[SignedApproverGrant]:
        try:
            raw = _read_regular(self._path(GRANT_FILENAME))
        except (OSError, ItemApprovalRefused):
            return None
        try:
            return SignedApproverGrant.model_validate_json(raw)
        except ValueError:
            return None

    def write_initiator(self, initiator: Optional[str]) -> None:
        """Record who started the run, once. ``None``: a person or operator.

        The run writes this before it asks anything. A file that is already
        there with another value (planted before the run) refuses the run.
        """

        self._checked_dir(create=True)
        payload = canonical_json({"started_by": initiator})
        try:
            _write_exclusive(self.dir / INITIATOR_FILENAME, payload)
        except FileExistsError:
            known, existing = self._read_initiator()
            if not known or existing != initiator:
                raise ItemApprovalRefused(
                    "initiator_changed", "this run already names another initiator"
                ) from None

    def _read_initiator(self) -> tuple[bool, Optional[str]]:
        try:
            raw = _read_regular(self._path(INITIATOR_FILENAME))
            data = json.loads(raw)
        except (OSError, ItemApprovalRefused, ValueError):
            return False, None
        if not isinstance(data, dict) or set(data) != {"started_by"}:
            return False, None
        value = data["started_by"]
        if value is not None and not isinstance(value, str):
            return False, None
        return True, value

    def _key_directory(self) -> Path:
        if self._key_dir is not None:
            return self._key_dir
        from openadapt_flow.jit.lifecycle import default_key_dir

        return default_key_dir()

    def context(self, *, now: Optional[datetime] = None) -> ApproverContext:
        """What every answer in this run is checked against, verified now.

        The grant's signature, signer and dates are re-checked on every call,
        so a grant that expired, or a file swapped mid-run, stops working.
        """

        known, initiator = self._read_initiator()

        def make(**fields: Any) -> ApproverContext:
            return ApproverContext(
                initiator=initiator,
                initiator_known=known,
                **fields,
            )

        recorded = self._recorded_policy()
        if recorded is None or not allows_non_person(recorded):
            return make(policy=recorded or DEFAULT_APPROVER_POLICY)
        signed = self._read_grant()
        if signed is None:
            return make(grant_refusal="approver_policy_unsigned")
        try:
            verified = verify_grant(signed, self._key_directory(), now=now)
        except GrantRefused as exc:
            return make(grant_refusal=exc.code)
        if verified.policy != recorded:
            return make(grant_refusal="approver_grant_policy_mismatch")
        return make(policy=verified.policy, grant=verified)

    def _recorded_policy(self) -> Optional[ApproverPolicyV1]:
        """The policy this run recorded. ``None`` when none was written.

        After :meth:`write_policy` (the gate calls it at run start), this
        store applies the policy it wrote: a file replaced later changes
        nothing. A reader that wrote no policy reads the file.
        """

        if self._pinned_policy is not None:
            return self._pinned_policy
        return self._policy_on_disk()

    def _policy_on_disk(self) -> Optional[ApproverPolicyV1]:
        try:
            raw = _read_regular(self._path(POLICY_FILENAME))
        except (OSError, ItemApprovalRefused):
            return None
        try:
            return ApproverPolicyV1.model_validate_json(raw)
        except ValueError:
            return DEFAULT_APPROVER_POLICY

    def policy(self) -> ApproverPolicyV1:
        """The run's effective approver policy. Person-only when none was
        written, when it is unreadable, or when it would let a delegate answer
        without a person-signed grant that verifies now."""

        return self.context().policy

    # -- requests ------------------------------------------------------------
    def open_request(
        self,
        *,
        run_id: str,
        workflow_name: str,
        step_id: str,
        step_index: int,
        step_intent: str,
        mode: ApprovalMode,
        bundle_content_digest: Optional[str],
        runtime_inputs_digest: Optional[str],
        frame_png: bytes,
        form_state: tuple[str, str],
        qualification_case_kind: Optional[str] = None,
        now: Optional[datetime] = None,
        ttl_s: float = REQUEST_TTL_S,
        step_class: StepClass = StepClass.CONSEQUENTIAL,
        fields: Optional[Sequence[SaveField]] = None,
    ) -> ItemApprovalRequest:
        """Write the screenshot and the request. Returns the request.

        ``fields`` are the values the save writes, shown on the phone. An
        empty list is stored as none.
        """

        self._checked_dir(create=True)
        request_id = uuid.uuid4().hex
        created = now or _utc_now()
        kind, form_digest = form_state
        request = ItemApprovalRequest(
            request_id=request_id,
            run_id=run_id,
            workflow_name=workflow_name,
            step_id=step_id,
            step_index=step_index,
            step_intent=step_intent[:1024],
            mode=mode,
            bundle_content_digest=bundle_content_digest,
            runtime_inputs_digest=runtime_inputs_digest,
            screenshot=f"{APPROVAL_DIRNAME}/{request_id}.png",
            screenshot_sha256=hashlib.sha256(frame_png).hexdigest(),
            form_state_kind=kind,  # type: ignore[arg-type]
            form_state_sha256=form_digest,
            created_at=_iso(created),
            expires_at=_iso(created + timedelta(seconds=min(ttl_s, REQUEST_TTL_S))),
            qualification_case_kind=qualification_case_kind,
            step_class=step_class,
            fields=tuple(fields) if fields else None,
        )
        _write_exclusive(self.dir / f"{request_id}.png", frame_png)
        _write_exclusive(
            self.dir / f"request-{request_id}.json",
            canonical_json(request.model_dump(mode="json")),
        )
        return request

    def read_request(self, request_id: str) -> ItemApprovalRequest:
        self._check_id(request_id)
        try:
            raw = _read_regular(self._path(f"request-{request_id}.json"))
            request = ItemApprovalRequest.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise ItemApprovalRefused(
                "request_unknown", "the approval request is missing or unreadable"
            ) from exc
        if request.request_id != request_id:
            raise ItemApprovalRefused("request_unknown", "request id mismatch")
        return request

    def read_frame(self, request: ItemApprovalRequest) -> bytes:
        """The screenshot the person is shown, checked against its digest.

        Raises :class:`ItemApprovalRefused` when it is missing, too large, or
        isn't the exact image the request was opened with.
        """

        try:
            frame = _read_regular(
                self.run_dir / request.screenshot, limit=_MAX_FRAME_BYTES
            )
        except OSError as exc:
            raise ItemApprovalRefused(
                "frame_unreadable", "the screenshot is missing or unreadable"
            ) from exc
        if hashlib.sha256(frame).hexdigest() != request.screenshot_sha256:
            raise ItemApprovalRefused(
                "frame_digest_mismatch", "the screenshot isn't the one requested"
            )
        return frame

    def requests(self) -> list[ItemApprovalRequest]:
        """Every readable request in this run, oldest first."""

        try:
            folder = self._checked_dir(create=False)
        except (OSError, ItemApprovalRefused):
            return []
        found: list[ItemApprovalRequest] = []
        for entry in sorted(folder.glob("request-*.json")):
            request_id = entry.name[len("request-") : -len(".json")]
            try:
                found.append(self.read_request(request_id))
            except ItemApprovalRefused:
                continue
        found.sort(key=lambda item: (item.created_at, item.request_id))
        return found

    # -- answers -------------------------------------------------------------
    def read_answer(self, request_id: str) -> Optional[ItemApprovalAnswer]:
        """The answer, ``None`` when there is none yet.

        Raises :class:`ItemApprovalRefused` for an answer file that exists
        but can't be parsed: a malformed answer is never "no answer".
        """

        self._check_id(request_id)
        try:
            path = self._path(f"answer-{request_id}.json")
        except FileNotFoundError:
            return None
        try:
            raw = _read_regular(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ItemApprovalRefused(
                "answer_unreadable", "the approval answer is unreadable"
            ) from exc
        try:
            return ItemApprovalAnswer.model_validate_json(raw)
        except ValueError as exc:
            raise ItemApprovalRefused(
                "answer_malformed", "the approval answer is malformed"
            ) from exc

    def write_answer(self, answer: ItemApprovalAnswer) -> None:
        self._check_id(answer.request_id)
        path = self._path(f"answer-{answer.request_id}.json")
        try:
            _write_exclusive(path, canonical_json(answer.model_dump(mode="json")))
        except FileExistsError as exc:
            raise ItemApprovalRefused(
                "already_answered", "this approval request was already answered"
            ) from exc

    # -- consumption -----------------------------------------------------------
    def consume(self, request_id: str) -> bool:
        """Mark an approval used. ``False`` when it was already used."""

        self._check_id(request_id)
        try:
            _write_exclusive(self._path(f"consumed-{request_id}"), b"consumed\n")
        except FileExistsError:
            return False
        return True

    def is_consumed(self, request_id: str) -> bool:
        self._check_id(request_id)
        try:
            os.lstat(self._path(f"consumed-{request_id}"))
        except FileNotFoundError:
            return False
        return True

    def status(
        self, request: ItemApprovalRequest, *, now: Optional[datetime] = None
    ) -> str:
        """``waiting``, ``approved``, ``declined``, ``used``, ``expired`` or
        ``invalid_answer``."""

        if self.is_consumed(request.request_id):
            return "used"
        try:
            answer = self.read_answer(request.request_id)
        except ItemApprovalRefused:
            return "invalid_answer"
        if answer is None:
            return "expired" if request.expired(now) else "waiting"
        refusal = answer_refusal(request, answer, now=now, policy=self.context(now=now))
        if refusal == "request_expired":
            return "expired"
        if refusal is not None:
            return "invalid_answer"
        return "approved" if answer.decision == "approve" else "declined"

    def consumed_approvals(self, run_id: str) -> list[ItemApprovalRef]:
        """Evidence rows for every approval this run used, oldest first."""

        rows: list[ItemApprovalRef] = []
        for request in self.requests():
            if request.run_id != run_id or not self.is_consumed(request.request_id):
                continue
            try:
                answer = self.read_answer(request.request_id)
            except ItemApprovalRefused:
                continue
            if answer is None or answer.decision != "approve":
                continue
            rows.append(answer.reference(request))
        return rows

    def find_unused_approval(
        self,
        *,
        run_id: str,
        step_id: str,
        step_index: int,
        now: Optional[datetime] = None,
    ) -> Optional[tuple[ItemApprovalRequest, ItemApprovalAnswer]]:
        """An answered, unused, unexpired approval for this exact save.

        A resumed run uses it instead of asking again. Only the newest
        request for the save counts, so an older answer can't stand in for
        the request the person saw last.
        """

        candidates = [
            request
            for request in self.requests()
            if request.run_id == run_id
            and request.step_id == step_id
            and request.step_index == step_index
        ]
        if not candidates:
            return None
        request = candidates[-1]
        if self.is_consumed(request.request_id):
            return None
        try:
            answer = self.read_answer(request.request_id)
        except ItemApprovalRefused:
            return None
        if answer is None or answer.decision != "approve":
            return None
        if (
            answer_refusal(request, answer, now=now, policy=self.context(now=now))
            is not None
        ):
            return None
        return request, answer


def wait_for_answer(
    store: ItemApprovalStore,
    request: ItemApprovalRequest,
    *,
    timeout_s: float,
    poll_s: float = 0.5,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    approver: Optional[Approver] = None,
) -> tuple[Optional[ItemApprovalAnswer], Optional[str]]:
    """Wait for a usable answer. Returns ``(answer, None)`` or ``(None, code)``.

    ``code`` is ``approval_wait_timed_out`` when nobody answered in time, or
    the closed reason an answer was refused. A refused answer is final for
    this request: the run stops before the save rather than waiting on.

    The answer can come from Desktop's Run it, from the person's phone
    through the decision portal (:mod:`openadapt_flow.console.save_approvals`
    writes it), or from a delegate the run's signed grant allows. Whichever
    answer is written first wins; the file is created exclusively.
    """

    deadline = monotonic() + max(0.0, min(timeout_s, MAX_APPROVAL_WAIT_S))
    context = store.context()
    delegate = approver
    if delegate is not None and _delegate_refusal(request, delegate.identity, context):
        # The grant doesn't let this approver decide this step: ask a person.
        delegate = None
    next_ask = 0.0
    while True:
        try:
            answer = store.read_answer(request.request_id)
        except ItemApprovalRefused as exc:
            return None, exc.code
        if answer is None and delegate is not None and monotonic() >= next_ask:
            next_ask = monotonic() + DELEGATE_POLL_S
            answer = _ask_delegate(store, request, delegate)
            if answer is None and getattr(delegate, "_failed", False):
                delegate = None
        if answer is not None:
            refusal = answer_refusal(request, answer, policy=store.context())
            if refusal is not None:
                return None, refusal
            return answer, None
        if request.expired():
            return None, "request_expired"
        if monotonic() >= deadline:
            return None, "approval_wait_timed_out"
        sleep(poll_s)


def request_facts(request: ItemApprovalRequest) -> dict[str, str]:
    """The PHI-free facts a delegated approver sees. No intent, no image."""

    return {
        "schema_version": "openadapt.delegated-approval-request/v1",
        "request_id": request.request_id,
        "request_sha256": request.digest(),
        "run_id": request.run_id,
        "step_id": request.step_id,
        "step_index": str(request.step_index),
        "step_class": request.step_class.value,
        "mode": request.mode.value,
        "form_state_sha256": request.form_state_sha256,
        "screenshot_sha256": request.screenshot_sha256,
        "expires_at": request.expires_at,
    }


def _ask_delegate(
    store: ItemApprovalStore, request: ItemApprovalRequest, delegate: Approver
) -> Optional[ItemApprovalAnswer]:
    """Ask a delegated approver once and record its answer, if it gave one.

    Any failure (network, signature, malformed reply) leaves the request to a
    person and stops asking this approver for this request.
    """

    try:
        decided = delegate.decide(request_facts(request))
    except (OSError, ValueError, WebhookRefused):
        setattr(delegate, "_failed", True)
        return None
    if decided is None:
        return None
    try:
        answer = make_answer(
            request,
            decision=decided.decision,
            decided_by="automation",
            route=delegate.route,
            approver_id=None,
            auth_method="none",
            decline_reason=decided.decline_reason,
            approver=delegate.identity,
            policy=store.context(),
        )
        store.write_answer(answer)
    except ItemApprovalRefused:
        # Already answered (a person was faster) or refused: read what's there.
        return None
    return answer


def make_answer(
    request: ItemApprovalRequest,
    *,
    decision: Literal["approve", "decline"],
    decided_by: str,
    route: str,
    approver_id: Optional[str],
    auth_method: str,
    decline_reason: Optional[str] = None,
    now: Optional[datetime] = None,
    approver: Optional[ApproverIdentityV1] = None,
    policy: "ApproverPolicyV1 | ApproverContext | None" = None,
) -> ItemApprovalAnswer:
    """Build and check one answer. Raises :class:`ItemApprovalRefused`.

    ``approver`` names an agent or system; it is refused unless ``policy``
    (``store.context()``) holds a verified, person-signed grant that names it
    and allows its kind here. The answer records the grant's digest as the
    approver's ``policy_ref``.
    """

    context = _as_context(policy)
    if (
        approver is not None
        and not approver.is_person
        and approver.policy_ref is None
        and context.grant is not None
    ):
        approver = approver.model_copy(update={"policy_ref": context.grant.policy_ref})

    approver_kind: Literal["cloud_user", "unauthenticated"] = (
        "cloud_user" if approver_id is not None else "unauthenticated"
    )
    try:
        answer = ItemApprovalAnswer(
            request_id=request.request_id,
            request_sha256=request.digest(),
            decision=decision,
            decline_reason=decline_reason,  # type: ignore[arg-type]
            decided_by=decided_by,  # type: ignore[arg-type]
            route=route,
            approver_kind=approver_kind,
            approver_id=approver_id,
            auth_method=auth_method,  # type: ignore[arg-type]
            answered_at=_iso(now or _utc_now()),
            approver=approver,
        )
    except ValidationError as exc:
        raise ItemApprovalRefused(
            "answer_fields_invalid", "the answer's fields are not valid"
        ) from exc
    refusal = answer_refusal(request, answer, now=now, policy=context)
    if refusal is not None:
        raise ItemApprovalRefused(refusal, f"the answer was refused ({refusal})")
    return answer


def request_summary(
    store: ItemApprovalStore,
    request: ItemApprovalRequest,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The JSON row ``item-approval show`` prints for one request."""

    return {
        "request_id": request.request_id,
        "request_sha256": request.digest(),
        "status": store.status(request, now=now),
        "run_id": request.run_id,
        "step_id": request.step_id,
        "step_index": request.step_index,
        "step_intent": request.step_intent,
        "mode": request.mode.value,
        "screenshot": str((store.run_dir / request.screenshot).resolve()),
        "screenshot_sha256": request.screenshot_sha256,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "qualification_case_kind": request.qualification_case_kind,
        "step_class": request.step_class.value,
        "decided_by": decided_by_summary(store, request),
    }


def decided_by_summary(
    store: ItemApprovalStore, request: ItemApprovalRequest
) -> Optional[dict[str, Any]]:
    """Who answered this request, for Desktop and Cloud to show.

    A person: their Cloud user id (or none when Desktop was signed out) and
    where they answered (``desktop_run_it`` or ``decision_portal``). An agent or
    system: its id and the digest of the person-signed grant it acted under.
    ``None`` when nobody answered yet or the answer isn't readable.
    """

    try:
        answer = store.read_answer(request.request_id)
    except ItemApprovalRefused:
        return None
    if answer is None:
        return None
    identity = answer.approver_identity()
    return {
        "decision": answer.decision,
        "kind": identity.kind.value,
        "id": identity.id,
        "auth_method": answer.auth_method,
        "route": answer.route,
        "policy_ref": identity.policy_ref,
        "answered_at": answer.answered_at,
    }


__all__ = [
    "ANSWER_SCHEMA",
    "ApproverContext",
    "GRANT_FILENAME",
    "INITIATOR_FILENAME",
    "PERSON_ROUTES",
    "UNIDENTIFIED_AGENT",
    "APPROVAL_DIRNAME",
    "DEFAULT_APPROVAL_WAIT_S",
    "MAX_APPROVAL_WAIT_S",
    "REQUEST_SCHEMA",
    "REQUEST_TTL_S",
    "ItemApprovalAnswer",
    "ItemApprovalRefused",
    "ItemApprovalRequest",
    "ItemApprovalStore",
    "POLICY_FILENAME",
    "answer_refusal",
    "make_answer",
    "request_facts",
    "request_summary",
    "wait_for_answer",
]
