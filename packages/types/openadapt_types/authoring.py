"""Public MCP wire contracts for hosted and stdio authoring.

These models are the vendor-facing observe, command, and bind shapes. They do
not reuse :class:`~openadapt_types.computer_state.ComputerState` or
:class:`~openadapt_types.computer_state.UINode`. Sharing
:class:`~openadapt_types.computer_state.ElementRole` is the only computer-state
type that may appear here.

The projector that drops field values, titles, and screenshots lives in
Capture. This module only refuses those keys on the wire.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from math import isfinite
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_types.computer_state import ElementRole

AUTHORING_OBSERVE_SCHEMA: Literal["openadapt.authoring.observe/v1"] = (
    "openadapt.authoring.observe/v1"
)
AUTHORING_COMMAND_SCHEMA: Literal["openadapt.authoring.command/v1"] = (
    "openadapt.authoring.command/v1"
)
AUTHORING_BIND_SCHEMA: Literal["openadapt.authoring.bind/v1"] = (
    "openadapt.authoring.bind/v1"
)
OBSERVE_SCHEMA_VERSION = AUTHORING_OBSERVE_SCHEMA
COMMAND_SCHEMA_VERSION = AUTHORING_COMMAND_SCHEMA
BIND_SCHEMA_VERSION = AUTHORING_BIND_SCHEMA

AUTHORING_ORIGIN = "https://openadapt.ai"
AUTHORING_RUNNER_SCHEME = "openadapt"
AUTHORING_RUNNER_ACTION = "runner"
AUTHORING_MAX_URI_BYTES = 2048
AUTHORING_MAX_NODES = 200
AUTHORING_MAX_OBSERVE_BYTES = 32 * 1024
AUTHORING_MAX_COMMAND_BYTES = 8 * 1024
AUTHORING_LEASE_S = 900
AUTHORING_RETRY_AFTER_MS = 1000

BIND_TOKEN_PREFIX = "oab_"
LEASE_SECRET_PREFIX = "oals_"
BIND_TOKEN_PATTERN = r"^oab_[A-Za-z0-9_-]{43}$"
LEASE_SECRET_PATTERN = r"^oals_[a-f0-9]{64}$"
_CLOUD_RUNNER_TOKEN_PATTERN = r"^oar_[a-f0-9]{64}$"
_PAIRING_SECRET_PATTERN = r"^oap_[A-Za-z0-9_-]{43}$"
_BIND_HEX_BODY_PATTERN = r"^oab_[a-f0-9]{64}$"
_LEASE_BASE64URL_BODY_PATTERN = r"^oals_[A-Za-z0-9_-]{43}$"

_BIND_TOKEN_RE = re.compile(BIND_TOKEN_PATTERN)
_LEASE_SECRET_RE = re.compile(LEASE_SECRET_PATTERN)
_CLOUD_RUNNER_TOKEN_RE = re.compile(_CLOUD_RUNNER_TOKEN_PATTERN)
_PAIRING_SECRET_RE = re.compile(_PAIRING_SECRET_PATTERN)
_BIND_HEX_BODY_RE = re.compile(_BIND_HEX_BODY_PATTERN)
_LEASE_BASE64URL_BODY_RE = re.compile(_LEASE_BASE64URL_BODY_PATTERN)

_NODE_ID_PATTERN = r"^n_[0-9a-f]{8}$"
_COMMAND_ID_PATTERN = r"^cmd_[0-9A-HJKMNP-TV-Z]{26}$"
_WORKFLOW_ID_PATTERN = r"^wf_[A-Za-z0-9_-]{8,64}$"
_PACK_ID_PATTERN = r"^(p\.[A-Za-z0-9_-]{12}|v1\.[A-Za-z0-9_-]{38,512})$"
_PROCESS_NAME_PATTERN = r"^[A-Za-z0-9 ._-]{1,64}$"
_PROJECTED_LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$"
_PARAM_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_SIX_DIGITS_RE = re.compile(r"\d{6}")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)"
)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_SHA256_HEX_PATTERN = r"^[a-f0-9]{64}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"
_RUNNER_FIELDS = frozenset({"pack", "bind", "origin"})
_COACH_ONLY_BACKENDS = frozenset({"windows", "rdp", "citrix"})
_DEEP_LINK_PATTERN = r"^openadapt://runner\?[A-Za-z0-9._~=&%:/+-]{1,2000}$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoringBackendV1(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"
    WEB = "web"
    RDP = "rdp"
    CITRIX = "citrix"


class AuthoringProviderV1(str, Enum):
    PLAYWRIGHT_AX = "playwright_ax"
    MACOS_AX = "macos_ax"
    WINDOWS_UIA = "windows_uia"
    LINUX_ATSPI = "linux_atspi"
    NONE = "none"


class AuthoringEmptyProjectionReason(str, Enum):
    EMPTY_PROJECTION = "empty_projection"


class AuthoringClientDisplayV1(str, Enum):
    CHATGPT = "ChatGPT"
    CLAUDE = "Claude"


class AuthoringAllowStateV1(str, Enum):
    NONE = "none"
    PENDING = "pending"
    GRANTED = "granted"


class AuthoringEnqueueToolV1(str, Enum):
    OBSERVE = "observe"
    START_RECORD = "start_record"
    CLICK = "click"
    HALT = "halt"
    STOP_RECORD = "stop_record"
    COMPILE = "compile"
    PAUSE_FOR_INPUT = "pause_for_input"
    SET_COACH = "set_coach"
    GET_COACH = "get_coach"
    BIND_PACK = "bind_pack"


class AuthoringCommandStatusV1(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    EXPIRED = "expired"
    HALTED = "halted"


class AuthoringErrorCodeV1(str, Enum):
    STALE_NODE = "stale_node"
    IN_FLIGHT = "in_flight"
    COACH_ONLY = "COACH_ONLY"
    NOT_BOUND = "not_bound"
    NOT_ALLOWED = "not_allowed"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    MISSING_SECRET_TYPE = "missing_secret_type"
    UNKNOWN_COMMAND = "unknown_command"
    UNKNOWN_PACK = "unknown_pack"
    UNKNOWN_TOOL = "unknown_tool"
    DENIED = "denied"
    HALTED = "halted"


class AuthoringTokenError(ValueError):
    """A bind token, lease secret, or runner URI failed exact parsing."""


def parse_authoring_bind_token(value: object) -> str:
    """Accept only ``oab_`` + 43 unreserved characters; fail closed otherwise."""

    if not isinstance(value, str):
        raise AuthoringTokenError("bind token is malformed")
    if (
        _CLOUD_RUNNER_TOKEN_RE.fullmatch(value)
        or _PAIRING_SECRET_RE.fullmatch(value)
        or _BIND_HEX_BODY_RE.fullmatch(value)
        or not _BIND_TOKEN_RE.fullmatch(value)
    ):
        raise AuthoringTokenError("bind token is malformed")
    return value


def parse_authoring_lease_secret(value: object) -> str:
    """Accept only ``oals_`` + 64 lowercase hex characters."""

    if not isinstance(value, str):
        raise AuthoringTokenError("lease secret is malformed")
    if (
        _CLOUD_RUNNER_TOKEN_RE.fullmatch(value)
        or _PAIRING_SECRET_RE.fullmatch(value)
        or _LEASE_BASE64URL_BODY_RE.fullmatch(value)
        or not _LEASE_SECRET_RE.fullmatch(value)
    ):
        raise AuthoringTokenError("lease secret is malformed")
    return value


def _canonical_origin(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AuthoringTokenError("runner origin is malformed")
    if parsed.hostname.endswith("."):
        raise AuthoringTokenError("runner origin is malformed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AuthoringTokenError("runner origin is malformed") from exc
    if port not in (None, 443):
        raise AuthoringTokenError("runner origin is malformed")
    host = parsed.hostname.lower()
    return f"https://{host}"


class AuthoringRunnerUriV1(_StrictContract):
    pack: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    bind: StrictStr = Field(pattern=BIND_TOKEN_PATTERN)
    origin: Literal["https://openadapt.ai"] = AUTHORING_ORIGIN

    @model_validator(mode="after")
    def _bind_is_authoring(self) -> "AuthoringRunnerUriV1":
        parse_authoring_bind_token(self.bind)
        return self


def parse_authoring_runner_uri(uri: object) -> AuthoringRunnerUriV1:
    """Parse ``openadapt://runner`` and reject any other scheme or field."""

    if (
        not isinstance(uri, str)
        or not uri
        or len(uri.encode("utf-8")) > AUTHORING_MAX_URI_BYTES
    ):
        raise AuthoringTokenError("runner URI is malformed")
    parsed = urlparse(uri)
    if (
        parsed.scheme != AUTHORING_RUNNER_SCHEME
        or parsed.netloc != AUTHORING_RUNNER_ACTION
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise AuthoringTokenError("runner URI is malformed")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise AuthoringTokenError("runner URI is malformed") from exc
    if set(query) != _RUNNER_FIELDS or any(len(values) != 1 for values in query.values()):
        raise AuthoringTokenError("runner URI contains unknown or duplicate fields")
    origin = _canonical_origin(query["origin"][0])
    if origin != AUTHORING_ORIGIN:
        raise AuthoringTokenError("runner origin is not pinned")
    return AuthoringRunnerUriV1(
        pack=query["pack"][0],
        bind=parse_authoring_bind_token(query["bind"][0]),
        origin=AUTHORING_ORIGIN,
    )


def _projected_label(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(_PROJECTED_LABEL_PATTERN, value):
        raise ValueError("projected label is not allowed on the authoring wire")
    if (
        _SIX_DIGITS_RE.search(value)
        or _SSN_RE.search(value)
        or _PHONE_RE.search(value)
        or _EMAIL_RE.search(value)
        or "://" in value
        or "@" in value
    ):
        raise ValueError("projected label is not allowed on the authoring wire")
    return value


def _utf8_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _reject_oversize(data: Any, limit: int, label: str) -> Any:
    if isinstance(data, dict) and _utf8_bytes(data) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return data


def _finite_unit(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("normalized bounds must be finite numbers")
    number = float(value)
    if not isfinite(number):
        raise ValueError("normalized bounds must be finite")
    return number


class AuthoringNormalizedBoundsV1(_StrictContract):
    """Viewport-normalized overlay coordinates. Not backend pixels."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)

    @field_validator("x", "y", "w", "h", mode="before")
    @classmethod
    def _coerce_unit(cls, value: object) -> float:
        return _finite_unit(value)

    @model_validator(mode="after")
    def _inside_viewport(self) -> "AuthoringNormalizedBoundsV1":
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("normalized bounds exceed the viewport")
        return self


class AuthoringWindowV1(_StrictContract):
    process_name: StrictStr = Field(pattern=_PROCESS_NAME_PATTERN)
    role: Literal["window"] = "window"
    bounds: AuthoringNormalizedBoundsV1


class AuthoringNodeV1(_StrictContract):
    node_id: StrictStr = Field(pattern=_NODE_ID_PATTERN)
    role: ElementRole
    control_type: StrictStr | None = Field(default=None, pattern=_PROJECTED_LABEL_PATTERN)
    automation_id: StrictStr | None = Field(
        default=None, pattern=_PROJECTED_LABEL_PATTERN
    )
    class_name: StrictStr | None = Field(default=None, pattern=_PROJECTED_LABEL_PATTERN)
    name: StrictStr | None = Field(default=None, pattern=_PROJECTED_LABEL_PATTERN)
    enabled: StrictBool
    focused: StrictBool
    bounds: AuthoringNormalizedBoundsV1

    @field_validator("control_type", "automation_id", "class_name", "name")
    @classmethod
    def _labels(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _projected_label(value)


class AuthoringObserveV1(_StrictContract):
    """PHI-safe projected tree. No screenshots, titles, or field values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-max-bytes": AUTHORING_MAX_OBSERVE_BYTES,
            "x-openadapt-max-nodes": AUTHORING_MAX_NODES,
        },
    )

    schema_version: Literal["openadapt.authoring.observe/v1"] = AUTHORING_OBSERVE_SCHEMA
    backend: AuthoringBackendV1
    provider: AuthoringProviderV1
    mode: Literal["authoring"] = "authoring"
    agent_drive: StrictBool
    coach_only: StrictBool
    recording: StrictBool
    window: AuthoringWindowV1 | None = None
    tree: tuple[AuthoringNodeV1, ...] = Field(default=(), max_length=AUTHORING_MAX_NODES)
    truncated: StrictBool
    node_count: StrictInt = Field(ge=0, le=AUTHORING_MAX_NODES)
    reason: AuthoringEmptyProjectionReason | None = None

    @model_validator(mode="before")
    @classmethod
    def _cap_wire_bytes(cls, data: Any) -> Any:
        return _reject_oversize(data, AUTHORING_MAX_OBSERVE_BYTES, "observe")

    @model_validator(mode="after")
    def _consistent_projection(self) -> "AuthoringObserveV1":
        if self.node_count != len(self.tree):
            raise ValueError("node_count must equal the projected tree length")
        if self.reason is not None and self.tree:
            raise ValueError("empty_projection cannot carry a tree")
        if self.coach_only and self.agent_drive:
            raise ValueError("coach_only observations cannot advertise agent_drive")
        if self.backend.value in _COACH_ONLY_BACKENDS and not self.coach_only:
            raise ValueError("this backend is coach_only on the authoring wire")
        if self.agent_drive and self.window is None:
            raise ValueError("agent_drive observations require a window")
        return self


class AuthoringEmptyArgsV1(_StrictContract):
    pass


class AuthoringClickArgsV1(_StrictContract):
    node_id: StrictStr = Field(pattern=_NODE_ID_PATTERN)


class AuthoringPauseArgsV1(_StrictContract):
    param: StrictStr | None = Field(default=None, pattern=_PARAM_NAME_PATTERN)
    secret: StrictBool | None = None


class AuthoringSetCoachArgsV1(_StrictContract):
    hint: StrictStr = Field(pattern=_PROJECTED_LABEL_PATTERN)

    @field_validator("hint")
    @classmethod
    def _hint(cls, value: str) -> str:
        return _projected_label(value)


class AuthoringBindPackArgsV1(_StrictContract):
    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)


class AuthoringHostedPackArgsV1(_StrictContract):
    """Hosted MCP arguments for tools that only take a pack id."""

    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)


class AuthoringHostedClickArgsV1(_StrictContract):
    """Hosted ``click`` arguments. ``node_id`` only; never pixels or a value."""

    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    node_id: StrictStr = Field(pattern=_NODE_ID_PATTERN)


class AuthoringHostedPauseArgsV1(_StrictContract):
    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    param: StrictStr | None = Field(default=None, pattern=_PARAM_NAME_PATTERN)
    secret: StrictBool | None = None


class AuthoringHostedSetCoachArgsV1(_StrictContract):
    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    hint: StrictStr = Field(pattern=_PROJECTED_LABEL_PATTERN)

    @field_validator("hint")
    @classmethod
    def _hint(cls, value: str) -> str:
        return _projected_label(value)


class AuthoringCommandLookupArgsV1(_StrictContract):
    command_id: StrictStr = Field(pattern=_COMMAND_ID_PATTERN)


class AuthoringCompileResultV1(_StrictContract):
    status: Literal["needs_human_admit"] = "needs_human_admit"
    workflow_id: StrictStr = Field(pattern=_WORKFLOW_ID_PATTERN)
    recording_retained: StrictBool


class AuthoringPauseResultV1(_StrictContract):
    recorded: StrictBool
    param: StrictStr = Field(pattern=_PARAM_NAME_PATTERN)


class AuthoringRecordingResultV1(_StrictContract):
    recording: StrictBool


class AuthoringClickResultV1(_StrictContract):
    clicked: Literal[True] = True


class AuthoringHaltResultV1(_StrictContract):
    halted: Literal[True] = True


class AuthoringSetCoachResultV1(_StrictContract):
    ok: StrictBool


class AuthoringGetCoachResultV1(_StrictContract):
    hint: StrictStr | None = Field(default=None, pattern=_PROJECTED_LABEL_PATTERN)

    @field_validator("hint")
    @classmethod
    def _hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _projected_label(value)


class AuthoringBindPackResultV1(_StrictContract):
    allowed: StrictBool
    client_display: AuthoringClientDisplayV1 | None = None

    @model_validator(mode="after")
    def _display_after_allow(self) -> "AuthoringBindPackResultV1":
        if self.client_display is not None and not self.allowed:
            raise ValueError("client_display is only present after Allow")
        return self


class AuthoringErrorResultV1(_StrictContract):
    error: AuthoringErrorCodeV1
    command_id: StrictStr | None = Field(default=None, pattern=_COMMAND_ID_PATTERN)


class AuthoringEnqueueAcceptedV1(_StrictContract):
    status: Literal["pending"] = "pending"
    command_id: StrictStr = Field(pattern=_COMMAND_ID_PATTERN)


class AuthoringNotBoundV1(_StrictContract):
    status: Literal["not_bound"] = "not_bound"


class AuthoringInFlightV1(_StrictContract):
    error: Literal["in_flight"] = "in_flight"
    command_id: StrictStr = Field(pattern=_COMMAND_ID_PATTERN)


_ARGS_BY_TOOL: dict[AuthoringEnqueueToolV1, type[_StrictContract]] = {
    AuthoringEnqueueToolV1.CLICK: AuthoringClickArgsV1,
    AuthoringEnqueueToolV1.PAUSE_FOR_INPUT: AuthoringPauseArgsV1,
    AuthoringEnqueueToolV1.SET_COACH: AuthoringSetCoachArgsV1,
    AuthoringEnqueueToolV1.BIND_PACK: AuthoringBindPackArgsV1,
}

_RESULT_BY_TOOL: dict[AuthoringEnqueueToolV1, type[_StrictContract]] = {
    AuthoringEnqueueToolV1.OBSERVE: AuthoringObserveV1,
    AuthoringEnqueueToolV1.COMPILE: AuthoringCompileResultV1,
    AuthoringEnqueueToolV1.PAUSE_FOR_INPUT: AuthoringPauseResultV1,
    AuthoringEnqueueToolV1.GET_COACH: AuthoringGetCoachResultV1,
    AuthoringEnqueueToolV1.BIND_PACK: AuthoringBindPackResultV1,
    AuthoringEnqueueToolV1.START_RECORD: AuthoringRecordingResultV1,
    AuthoringEnqueueToolV1.STOP_RECORD: AuthoringRecordingResultV1,
    AuthoringEnqueueToolV1.CLICK: AuthoringClickResultV1,
    AuthoringEnqueueToolV1.HALT: AuthoringHaltResultV1,
    AuthoringEnqueueToolV1.SET_COACH: AuthoringSetCoachResultV1,
}

_OPTIONAL_RESULT_TOOLS = frozenset(
    {
        AuthoringEnqueueToolV1.START_RECORD,
        AuthoringEnqueueToolV1.STOP_RECORD,
        AuthoringEnqueueToolV1.CLICK,
        AuthoringEnqueueToolV1.HALT,
        AuthoringEnqueueToolV1.SET_COACH,
    }
)

AuthoringCommandArgsV1 = (
    AuthoringClickArgsV1
    | AuthoringPauseArgsV1
    | AuthoringSetCoachArgsV1
    | AuthoringBindPackArgsV1
    | AuthoringEmptyArgsV1
)
AuthoringCommandResultV1 = (
    AuthoringObserveV1
    | AuthoringCompileResultV1
    | AuthoringPauseResultV1
    | AuthoringGetCoachResultV1
    | AuthoringBindPackResultV1
    | AuthoringRecordingResultV1
    | AuthoringClickResultV1
    | AuthoringHaltResultV1
    | AuthoringSetCoachResultV1
    | AuthoringErrorResultV1
)


def _parse_args(tool: AuthoringEnqueueToolV1, args: object) -> AuthoringCommandArgsV1:
    model = _ARGS_BY_TOOL.get(tool, AuthoringEmptyArgsV1)
    if args is None:
        args = {}
    parsed = model.model_validate(args)
    return parsed


class AuthoringCommandV1(_StrictContract):
    """Mailbox envelope. Result is PHI-free; args never carry typed values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-max-enqueue-bytes": AUTHORING_MAX_COMMAND_BYTES,
        },
    )

    schema_version: Literal["openadapt.authoring.command/v1"] = AUTHORING_COMMAND_SCHEMA
    command_id: StrictStr = Field(pattern=_COMMAND_ID_PATTERN)
    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    tool: AuthoringEnqueueToolV1
    args: AuthoringCommandArgsV1
    enqueued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    expires_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    status: AuthoringCommandStatusV1
    result: AuthoringCommandResultV1 | None = None
    oauth_sub_sha256: StrictStr = Field(pattern=_SHA256_HEX_PATTERN)
    client_id_sha256: StrictStr = Field(pattern=_SHA256_HEX_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def _typed_args(cls, data: Any) -> Any:
        if isinstance(data, dict):
            enqueue = dict(data)
            enqueue.pop("result", None)
            _reject_oversize(enqueue, AUTHORING_MAX_COMMAND_BYTES, "command envelope")
        if not isinstance(data, dict):
            return data
        tool = data.get("tool")
        try:
            parsed_tool = AuthoringEnqueueToolV1(tool)
        except ValueError:
            return data
        payload = dict(data)
        payload["args"] = _parse_args(parsed_tool, data.get("args", {}))
        return payload

    @model_validator(mode="after")
    def _status_and_result(self) -> "AuthoringCommandV1":
        expected_args = type(_parse_args(self.tool, self.args.model_dump(mode="json")))
        if type(self.args) is not expected_args:
            raise ValueError("command args do not match tool")
        if (
            isinstance(self.args, AuthoringBindPackArgsV1)
            and self.args.pack_id != self.pack_id
        ):
            raise ValueError("bind_pack args pack_id must match the envelope")
        if self.expires_at <= self.enqueued_at:
            raise ValueError("expires_at must be after enqueued_at")
        if self.status is AuthoringCommandStatusV1.ERROR:
            if not isinstance(self.result, AuthoringErrorResultV1):
                raise ValueError("error status requires an error result")
            return self
        if self.status is not AuthoringCommandStatusV1.DONE:
            if self.result is not None:
                raise ValueError("only done or error commands may carry a result")
            return self
        expected_result = _RESULT_BY_TOOL.get(self.tool)
        if expected_result is None:
            if self.result is not None:
                raise ValueError("this tool has no result payload")
            return self
        if self.result is None:
            if self.tool in _OPTIONAL_RESULT_TOOLS:
                return self
            raise ValueError("this tool requires a PHI-free result")
        if not isinstance(self.result, expected_result):
            raise ValueError("command result does not match tool")
        return self


class AuthoringCommandLookupV1(_StrictContract):
    """Non-blocking ``get_command_result`` body. No tree unless observe is done."""

    command_id: StrictStr = Field(pattern=_COMMAND_ID_PATTERN)
    status: AuthoringCommandStatusV1
    retry_after_ms: Literal[1000] | None = None
    result: AuthoringCommandResultV1 | None = None

    @model_validator(mode="after")
    def _retry_matches_status(self) -> "AuthoringCommandLookupV1":
        waiting = self.status in {
            AuthoringCommandStatusV1.PENDING,
            AuthoringCommandStatusV1.RUNNING,
        }
        if waiting:
            if self.retry_after_ms != AUTHORING_RETRY_AFTER_MS or self.result is not None:
                raise ValueError("pending lookup result is null and retry_after_ms is 1000")
            return self
        if self.retry_after_ms is not None:
            raise ValueError("terminal lookup has no retry_after_ms")
        if self.status is AuthoringCommandStatusV1.ERROR:
            if not isinstance(self.result, AuthoringErrorResultV1):
                raise ValueError("error lookup requires an error result")
        return self


class AuthoringBindMintV1(_StrictContract):
    schema_version: Literal["openadapt.authoring.bind/v1"] = AUTHORING_BIND_SCHEMA
    bind: StrictStr = Field(pattern=BIND_TOKEN_PATTERN)
    deep_link: StrictStr = Field(
        min_length=1,
        max_length=AUTHORING_MAX_URI_BYTES,
        pattern=_DEEP_LINK_PATTERN,
    )

    @field_validator("bind")
    @classmethod
    def _bind_token(cls, value: str) -> str:
        return parse_authoring_bind_token(value)

    @field_validator("deep_link")
    @classmethod
    def _runner_uri(cls, value: str) -> str:
        parse_authoring_runner_uri(value)
        return value

    @model_validator(mode="after")
    def _bind_matches_link(self) -> "AuthoringBindMintV1":
        parsed = parse_authoring_runner_uri(self.deep_link)
        if parsed.bind != self.bind:
            raise ValueError("deep_link bind does not match bind")
        return self


class AuthoringBindClaimV1(_StrictContract):
    schema_version: Literal["openadapt.authoring.bind/v1"] = AUTHORING_BIND_SCHEMA
    leaseSecret: StrictStr = Field(pattern=LEASE_SECRET_PATTERN)
    lease_s: Literal[900] = AUTHORING_LEASE_S

    @field_validator("leaseSecret")
    @classmethod
    def _lease(cls, value: str) -> str:
        return parse_authoring_lease_secret(value)


class AuthoringBindV1(_StrictContract):
    """``bind_status`` body. No token, lease secret, tree, hint, or command args."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-bind-token-pattern": BIND_TOKEN_PATTERN,
            "x-openadapt-lease-secret-pattern": LEASE_SECRET_PATTERN,
            "x-openadapt-bind-token-rejects-hex-body": True,
            "x-openadapt-lease-secret-rejects-base64url-body": True,
            "x-openadapt-rejected-token-patterns": [
                _CLOUD_RUNNER_TOKEN_PATTERN,
                _PAIRING_SECRET_PATTERN,
            ],
            "x-openadapt-deep-link-scheme": "openadapt://runner",
            "x-openadapt-origin": AUTHORING_ORIGIN,
            "x-openadapt-lease-s": AUTHORING_LEASE_S,
        },
    )

    schema_version: Literal["openadapt.authoring.bind/v1"] = AUTHORING_BIND_SCHEMA
    pack_id: StrictStr = Field(pattern=_PACK_ID_PATTERN)
    bound: StrictBool
    allow: AuthoringAllowStateV1
    client_display: AuthoringClientDisplayV1 | None = None
    backend: AuthoringBackendV1 | None = None
    coach_only: StrictBool
    halted: StrictBool = False

    @model_validator(mode="after")
    def _status_shape(self) -> "AuthoringBindV1":
        granted = self.allow is AuthoringAllowStateV1.GRANTED
        if not self.bound and self.allow is not AuthoringAllowStateV1.NONE:
            raise ValueError("an unbound laptop cannot be allowed")
        if self.allow is AuthoringAllowStateV1.PENDING and not self.bound:
            raise ValueError("pending allow requires a bound laptop")
        if self.client_display is not None and not granted:
            raise ValueError("client_display is only present after Allow")
        if self.backend is not None and not self.bound:
            raise ValueError("backend is only present on a bound laptop")
        if not self.bound and not self.coach_only:
            raise ValueError("an unbound laptop is coach_only")
        return self


class AuthoringPollRequestV1(_StrictContract):
    wait_seconds: Literal[0] = 0
    lease_seconds: Literal[900] = AUTHORING_LEASE_S


class AuthoringCallbackV1(_StrictContract):
    """Desktop mailbox callback. PHI-free result only."""

    command_id: StrictStr | None = Field(default=None, pattern=_COMMAND_ID_PATTERN)
    status: AuthoringCommandStatusV1 | None = None
    result: AuthoringCommandResultV1 | None = None
    halted: StrictBool | None = None

    @model_validator(mode="after")
    def _callback_shape(self) -> "AuthoringCallbackV1":
        if self.halted is True and self.command_id is None:
            return self
        if self.command_id is None:
            raise ValueError("callback requires command_id unless it is unsigned halt")
        return self
