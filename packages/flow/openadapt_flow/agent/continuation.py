"""Mid-run continuation policy, token store, and audit log for MCP agents.

When an outer agent runs a workflow over MCP, the run may halt mid-way and emit
a structured halt packet. This module lets the calling agent continue a halted
run from its last verified checkpoint ONLY for two narrow, pre-commit halt
classes:

1. Missing required input or parameter (``missing_parameter`` /
   ``missing_input``) where the agent supplies the missing value.
2. Safe transient network or read timeout (``transient_network`` /
   ``retryable_timeout``) where no ambiguous or unverified write was in flight
   and the caller explicitly passes ``retry_transient=True``.

Every other halt class (identity mismatch, target ambiguity, duplicate record
collision, policy or permission violation, ``RECONCILIATION_REQUIRED`` or any
post-write uncertainty, destructive halt, or unknown halt class) is refused
with ``human_sign_off_required`` and points to local human review surfaces.

Security and durability invariants:
- Allowlist only: any unknown halt class or uncertain write state stops and
  requires human sign-off.
- Single-use, expiring ``resume_token`` stored as SHA-256 on disk (0600).
- Supplied parameters are validated against the declared parameter schema;
  unknown or malformed keys are rejected, and already-bound immutable identity
  keys can never be overwritten.
- Every continuation attempt (allowed or refused) writes a durable audit entry
  linking the original ``halt_id`` and ``resume_token`` hash, redacted supplied
  parameters, and the decision or resumed run outcome.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from openadapt_flow.agent.runner import is_safe_run_id

__all__ = [
    "ALLOWLISTED_HALT_CLASSES",
    "CONTINUATION_AUDIT_SCHEMA",
    "CONTINUATION_INPUT_SCHEMA",
    "CONTINUATION_OUTPUT_SCHEMA",
    "DEFAULT_IMMUTABLE_IDENTITY_KEYS",
    "DEFAULT_TOKEN_TTL_S",
    "HUMAN_REVIEW_SURFACES",
    "MISSING_PARAMETER_HALT_CLASSES",
    "SAFE_PRE_WRITE_OUTCOMES",
    "TRANSIENT_HALT_CLASSES",
    "UNSAFE_TRANSACTION_OUTCOMES",
    "ContinuationDesk",
    "ContinuationError",
    "ContinuationExecutor",
    "ContinuationPolicyDecision",
    "FlowResumeExecutor",
    "HaltContinuationRecord",
    "evaluate_continuation_policy",
    "redact_parameters",
    "validate_continuation_parameters",
]

_LOG = logging.getLogger(__name__)

CONTINUATION_RECORD_SCHEMA = "openadapt-agent.halt-continuation/v1"
CONTINUATION_AUDIT_SCHEMA = "openadapt-agent.continuation-audit/v1"

#: Explicitly allowlisted halt classes for missing input/parameter values.
MISSING_PARAMETER_HALT_CLASSES: frozenset[str] = frozenset(
    {
        "missing_parameter",
        "missing_input",
    }
)

#: Explicitly allowlisted halt classes for safe pre-write transient timeouts.
TRANSIENT_HALT_CLASSES: frozenset[str] = frozenset(
    {
        "transient_network",
        "retryable_timeout",
    }
)

#: Complete allowlist of halt classes an outer MCP agent may continue.
ALLOWLISTED_HALT_CLASSES: frozenset[str] = (
    MISSING_PARAMETER_HALT_CLASSES | TRANSIENT_HALT_CLASSES
)

#: Flow transaction outcomes that prove no business write was attempted.
SAFE_PRE_WRITE_OUTCOMES: frozenset[str] = frozenset(
    {
        "HALTED_BEFORE_EFFECT",
        "FAILED_PLATFORM",
    }
)

#: Transaction outcomes that always require human sign-off / reconciliation.
UNSAFE_TRANSACTION_OUTCOMES: frozenset[str] = frozenset(
    {
        "RECONCILIATION_REQUIRED",
        "COMPLETED_UNVERIFIED",
        "verified".upper(),
        "SAVED_AWAITING_CHECK",
        "REJECTED_POLICY",
        "CANCELED",
        "_".join(("ROLLED", "BACK")),
    }
)

#: Parameter names treated as immutable identity bindings whenever already bound.
DEFAULT_IMMUTABLE_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "patient_id",
        "mrn",
        "medical_record_number",
        "account_id",
        "customer_id",
        "user_id",
        "member_id",
        "record_id",
        "target_id",
        "entity_id",
        "invoice_id",
        "claim_id",
        "order_id",
        "case_id",
        "ssn",
        "dob",
        "date_of_birth",
    }
)

#: Default lifetime for a single-use continuation token (1 hour).
DEFAULT_TOKEN_TTL_S: float = 3600.0

#: Local human review surfaces referenced when agent continuation is refused.
HUMAN_REVIEW_SURFACES: tuple[str, ...] = (
    "openadapt-flow console",
    "list_needs_attention / continue_attention (operator elicitation)",
    "openadapt-flow resume <run_dir>",
)

_PARAM_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_HALT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
_RESUME_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")

_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "private_key",
    "access_key",
    "client_secret",
    "bearer",
    "cookie",
    "session_id",
    "ssn",
    "cvv",
    "pin",
    "credit_card",
)

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}$"),
    re.compile(r"^(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}$"),
    re.compile(r"^sk-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{16,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
)


class ContinuationError(Exception):
    """A tool-level error raised when continuation setup or input is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _is_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return any(pattern.search(stripped) for pattern in _SECRET_VALUE_PATTERNS)


def redact_parameters(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with secret and credential values redacted."""

    def _redact_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(k): ("[REDACTED]" if _is_secret_key(str(k)) else _redact_value(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_redact_value(item) for item in value]
        if _is_secret_value(value):
            return "[REDACTED]"
        return value

    return {
        str(key): ("[REDACTED]" if _is_secret_key(str(key)) else _redact_value(val))
        for key, val in params.items()
    }


@dataclass(frozen=True)
class HaltContinuationRecord:
    """Persisted metadata for one halted run eligible for policy evaluation."""

    run_id: str
    halt_id: str
    workflow: str
    halt_class: str
    step_id: str
    checkpoint_id: Optional[str] = None
    completed_steps: tuple[str, ...] = ()
    transaction_outcome: Optional[str] = None
    write_in_flight: bool = False
    parameter_schema: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_parameters: tuple[str, ...] = ()
    immutable_identity_keys: tuple[str, ...] = ()
    bound_parameters: dict[str, Any] = field(default_factory=dict)
    resume_token_sha256: str = ""
    created_at: str = ""
    expires_at: str = ""
    consumed: bool = False
    consumed_at: Optional[str] = None
    consumed_audit_id: Optional[str] = None
    run_dir: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTINUATION_RECORD_SCHEMA,
            "run_id": self.run_id,
            "halt_id": self.halt_id,
            "workflow": self.workflow,
            "halt_class": self.halt_class,
            "step_id": self.step_id,
            "checkpoint_id": self.checkpoint_id,
            "completed_steps": list(self.completed_steps),
            "transaction_outcome": self.transaction_outcome,
            "write_in_flight": self.write_in_flight,
            "parameter_schema": self.parameter_schema,
            "missing_parameters": list(self.missing_parameters),
            "immutable_identity_keys": list(self.immutable_identity_keys),
            "bound_parameters": self.bound_parameters,
            "resume_token_sha256": self.resume_token_sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
            "consumed_audit_id": self.consumed_audit_id,
            "run_dir": self.run_dir,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Optional["HaltContinuationRecord"]:
        if data.get("schema") != CONTINUATION_RECORD_SCHEMA:
            return None
        try:
            return cls(
                run_id=str(data["run_id"]),
                halt_id=str(data["halt_id"]),
                workflow=str(data["workflow"]),
                halt_class=str(data["halt_class"]),
                step_id=str(data["step_id"]),
                checkpoint_id=(
                    str(data["checkpoint_id"])
                    if data.get("checkpoint_id") is not None
                    else None
                ),
                completed_steps=tuple(
                    str(s) for s in data.get("completed_steps") or ()
                ),
                transaction_outcome=(
                    str(data["transaction_outcome"])
                    if data.get("transaction_outcome") is not None
                    else None
                ),
                write_in_flight=bool(data.get("write_in_flight", False)),
                parameter_schema=dict(data.get("parameter_schema") or {}),
                missing_parameters=tuple(
                    str(p) for p in data.get("missing_parameters") or ()
                ),
                immutable_identity_keys=tuple(
                    str(k) for k in data.get("immutable_identity_keys") or ()
                ),
                bound_parameters=dict(data.get("bound_parameters") or {}),
                resume_token_sha256=str(data.get("resume_token_sha256") or ""),
                created_at=str(data.get("created_at") or ""),
                expires_at=str(data.get("expires_at") or ""),
                consumed=bool(data.get("consumed", False)),
                consumed_at=(
                    str(data["consumed_at"])
                    if data.get("consumed_at") is not None
                    else None
                ),
                consumed_audit_id=(
                    str(data["consumed_audit_id"])
                    if data.get("consumed_audit_id") is not None
                    else None
                ),
                run_dir=(
                    str(data["run_dir"]) if data.get("run_dir") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def all_immutable_keys(self) -> frozenset[str]:
        """Declared immutable identity keys plus any already-bound default identity keys."""
        keys = set(self.immutable_identity_keys)
        for key in self.bound_parameters:
            if key.lower() in DEFAULT_IMMUTABLE_IDENTITY_KEYS:
                keys.add(key)
        return frozenset(keys)


@dataclass(frozen=True)
class ContinuationPolicyDecision:
    """Result of evaluating whether an agent may continue a halted run."""

    allowed: bool
    error: Optional[str] = None
    reason: Optional[str] = None
    what_happened: str = ""
    next_action: str = ""
    human_sign_off_required: bool = False
    invalid_parameters: tuple[dict[str, str], ...] = ()


def _check_json_type(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_continuation_parameters(
    record: HaltContinuationRecord,
    parameters: Mapping[str, Any],
) -> ContinuationPolicyDecision:
    """Validate supplied parameters against the workflow/step schema and identity locks."""
    if not isinstance(parameters, Mapping):
        return ContinuationPolicyDecision(
            allowed=False,
            error="invalid_parameters",
            reason="invalid_parameters",
            what_happened="Supplied parameters must be a JSON object.",
            next_action="Pass parameters as an object mapping declared parameter names to values.",
            invalid_parameters=(
                {"parameter": "(root)", "problem": "must be an object"},
            ),
        )

    immutable_keys = record.all_immutable_keys()
    attempted_immutable: list[str] = []
    problems: list[dict[str, str]] = []

    for raw_key, value in parameters.items():
        if not isinstance(raw_key, str) or not _PARAM_KEY_RE.fullmatch(raw_key):
            problems.append(
                {
                    "parameter": str(raw_key)[:64],
                    "problem": "malformed parameter name",
                }
            )
            continue

        if raw_key in immutable_keys:
            attempted_immutable.append(raw_key)
            continue

        if record.parameter_schema:
            if raw_key not in record.parameter_schema:
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": "unknown parameter not declared in workflow schema",
                    }
                )
                continue
            schema = record.parameter_schema[raw_key]
            expected_type = schema.get("type")
            if isinstance(expected_type, str) and not _check_json_type(
                expected_type, value
            ):
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": f"expected type {expected_type}",
                    }
                )
                continue
            if value is None:
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": "value cannot be null",
                    }
                )
                continue
            if isinstance(value, str):
                if not value.strip():
                    problems.append(
                        {
                            "parameter": raw_key,
                            "problem": "string value cannot be empty",
                        }
                    )
                    continue
                min_len = schema.get("minLength")
                if isinstance(min_len, int) and len(value) < min_len:
                    problems.append(
                        {
                            "parameter": raw_key,
                            "problem": f"must be at least {min_len} characters",
                        }
                    )
                    continue
                max_len = schema.get("maxLength")
                if isinstance(max_len, int) and len(value) > max_len:
                    problems.append(
                        {
                            "parameter": raw_key,
                            "problem": f"must be at most {max_len} characters",
                        }
                    )
                    continue
                pattern = schema.get("pattern")
                if isinstance(pattern, str):
                    try:
                        if re.fullmatch(pattern, value) is None:
                            problems.append(
                                {
                                    "parameter": raw_key,
                                    "problem": "does not match required format",
                                }
                            )
                            continue
                    except re.error:
                        problems.append(
                            {
                                "parameter": raw_key,
                                "problem": "parameter schema pattern is invalid",
                            }
                        )
                        continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = schema.get("minimum")
                if isinstance(minimum, (int, float)) and value < minimum:
                    problems.append(
                        {
                            "parameter": raw_key,
                            "problem": f"must be >= {minimum}",
                        }
                    )
                    continue
                maximum = schema.get("maximum")
                if isinstance(maximum, (int, float)) and value > maximum:
                    problems.append(
                        {
                            "parameter": raw_key,
                            "problem": f"must be <= {maximum}",
                        }
                    )
                    continue
            enum_vals = schema.get("enum")
            if isinstance(enum_vals, list) and value not in enum_vals:
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": "value is not in allowed enum",
                    }
                )
                continue
        else:
            # No explicit schema attached: only allow explicitly missing parameters.
            if record.missing_parameters and raw_key not in record.missing_parameters:
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": "unknown parameter not listed in missing_parameters",
                    }
                )
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                problems.append(
                    {
                        "parameter": raw_key,
                        "problem": "value cannot be empty or null",
                    }
                )
                continue

    if attempted_immutable:
        keys_text = ", ".join(sorted(attempted_immutable))
        return ContinuationPolicyDecision(
            allowed=False,
            error="human_sign_off_required",
            reason="immutable_parameter_overwrite",
            what_happened=(
                f"Refused to overwrite already-bound immutable identity parameter(s): {keys_text}."
            ),
            next_action=(
                "Agent continuation cannot change bound identity fields. Have a person "
                "review the halted run in the local review surface or start a separate "
                "authorized run."
            ),
            human_sign_off_required=True,
            invalid_parameters=tuple(
                {
                    "parameter": key,
                    "problem": "immutable identity parameter cannot be overwritten",
                }
                for key in sorted(attempted_immutable)
            ),
        )

    if problems:
        return ContinuationPolicyDecision(
            allowed=False,
            error="invalid_parameters",
            reason="invalid_parameters",
            what_happened="One or more supplied parameters failed schema validation.",
            next_action="Fix the listed parameters and call continue_halted_run again.",
            invalid_parameters=tuple(problems),
        )

    if record.halt_class in MISSING_PARAMETER_HALT_CLASSES:
        if record.missing_parameters:
            still_missing = [
                key for key in record.missing_parameters if key not in parameters
            ]
            if still_missing:
                return ContinuationPolicyDecision(
                    allowed=False,
                    error="invalid_parameters",
                    reason="missing_required_parameters",
                    what_happened=(
                        "The halted step still needs required parameter(s): "
                        + ", ".join(still_missing)
                        + "."
                    ),
                    next_action="Supply all missing required parameters in continue_halted_run.",
                    invalid_parameters=tuple(
                        {
                            "parameter": key,
                            "problem": "required parameter was not supplied",
                        }
                        for key in still_missing
                    ),
                )
        elif not parameters:
            return ContinuationPolicyDecision(
                allowed=False,
                error="invalid_parameters",
                reason="missing_required_parameters",
                what_happened="No parameters were supplied for a missing-parameter halt.",
                next_action="Supply the missing parameter(s) in parameters and try again.",
                invalid_parameters=(
                    {
                        "parameter": "(parameters)",
                        "problem": "at least one parameter is required",
                    },
                ),
            )

    return ContinuationPolicyDecision(allowed=True)


def evaluate_continuation_policy(
    record: HaltContinuationRecord,
    *,
    parameters: Mapping[str, Any],
    retry_transient: bool,
    now: Optional[datetime] = None,
) -> ContinuationPolicyDecision:
    """Decide whether an outer MCP agent may continue this halted run.

    This is an allowlist-only gate. Anything outside the two safe pre-write
    classes returns ``human_sign_off_required``.
    """
    current_time = now or _now()

    if record.consumed:
        return ContinuationPolicyDecision(
            allowed=False,
            error="human_sign_off_required",
            reason="token_reused",
            what_happened=(
                "This resume token was already used to continue the halted run."
            ),
            next_action=(
                "A resume token can only be used once. Inspect the run status with "
                "get_run or have a person review the local run evidence."
            ),
            human_sign_off_required=True,
        )

    expires_at = _parse_iso(record.expires_at)
    if expires_at is None or current_time >= expires_at:
        return ContinuationPolicyDecision(
            allowed=False,
            error="human_sign_off_required",
            reason="token_expired",
            what_happened="This resume token has expired.",
            next_action=(
                "Have a person review the halted run in the local console or start a "
                "fresh run after confirming the target record."
            ),
            human_sign_off_required=True,
        )

    # Hard block on post-write or uncertain transaction outcomes regardless of halt_class.
    if record.transaction_outcome is not None:
        if (
            record.transaction_outcome in UNSAFE_TRANSACTION_OUTCOMES
            or record.transaction_outcome not in SAFE_PRE_WRITE_OUTCOMES
        ):
            return ContinuationPolicyDecision(
                allowed=False,
                error="human_sign_off_required",
                reason="post_write_reconciliation_required",
                what_happened=(
                    f"Run halted with transaction outcome {record.transaction_outcome}. "
                    "A write may already have happened, so agent continuation is refused."
                ),
                next_action=(
                    "Do not retry automatically. Have a person check the target record "
                    "and resolve the run through a local human review surface."
                ),
                human_sign_off_required=True,
            )

    if record.write_in_flight:
        return ContinuationPolicyDecision(
            allowed=False,
            error="human_sign_off_required",
            reason="write_in_flight_uncertainty",
            what_happened=(
                "A write or state-changing action was already in flight when the run "
                "halted. Agent continuation is refused to prevent duplicate writes."
            ),
            next_action=(
                "Have a person verify whether the write landed in the target system "
                "before taking any further action."
            ),
            human_sign_off_required=True,
        )

    halt_class = record.halt_class.strip()
    if halt_class not in ALLOWLISTED_HALT_CLASSES:
        return ContinuationPolicyDecision(
            allowed=False,
            error="human_sign_off_required",
            reason="disallowed_halt_class",
            what_happened=(
                f"Halt class '{halt_class or 'unknown'}' requires human sign-off and "
                "cannot be continued by an agent."
            ),
            next_action=(
                "Have a person inspect the halt card and approve or reject the run "
                "through a local human review surface."
            ),
            human_sign_off_required=True,
        )

    if halt_class in TRANSIENT_HALT_CLASSES:
        if not retry_transient:
            return ContinuationPolicyDecision(
                allowed=False,
                error="retry_transient_required",
                reason="retry_transient_required",
                what_happened=(
                    "This halt was a pre-write transient network timeout. Set "
                    "retry_transient=true to retry the step."
                ),
                next_action=(
                    "Call continue_halted_run again with retry_transient=true."
                ),
                human_sign_off_required=False,
            )
        if parameters:
            # Even on transient retry, validate any supplied parameters and block identity overwrite.
            param_check = validate_continuation_parameters(record, parameters)
            if not param_check.allowed:
                return param_check
        return ContinuationPolicyDecision(allowed=True)

    # MISSING_PARAMETER_HALT_CLASSES
    return validate_continuation_parameters(record, parameters)


class ContinuationExecutor(Protocol):
    """Resumes a halted run from its verified checkpoint without restarting step 1."""

    def resume_halted_run(
        self,
        *,
        record: HaltContinuationRecord,
        supplied_parameters: Mapping[str, Any],
        merged_parameters: Mapping[str, Any],
        retry_transient: bool,
        wait_seconds: int = 60,
    ) -> dict[str, Any]: ...


class FlowResumeExecutor:
    """Default executor that resumes a paused Flow run directory from its checkpoint."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def resume_halted_run(
        self,
        *,
        record: HaltContinuationRecord,
        supplied_parameters: Mapping[str, Any],
        merged_parameters: Mapping[str, Any],
        retry_transient: bool,
        wait_seconds: int = 60,
    ) -> dict[str, Any]:
        if not record.run_dir:
            raise ContinuationError(
                "halted run has no persisted run_dir available for checkpoint resume"
            )
        run_dir = Path(record.run_dir)
        if not run_dir.is_dir():
            raise ContinuationError(
                "halted run directory does not exist on this server"
            )
        resume_fn = getattr(self.runner, "resume", None)
        if callable(resume_fn):
            return resume_fn(
                run_dir=run_dir,
                step_id=record.step_id,
                checkpoint_id=record.checkpoint_id,
                parameters=dict(merged_parameters),
                retry_transient=retry_transient,
                wait_seconds=wait_seconds,
            )
        raise ContinuationError(
            "the configured runner does not support mid-run checkpoint resume"
        )


class ContinuationDesk:
    """Persists halt continuation records, enforces policy, and writes audit entries."""

    def __init__(
        self,
        runs_dir: Path | str,
        *,
        executor: Optional[ContinuationExecutor] = None,
        token_ttl_s: float = DEFAULT_TOKEN_TTL_S,
    ) -> None:
        self.root = (
            Path(runs_dir).expanduser().resolve() / "openadapt-agent" / "continuations"
        )
        self.records_dir = self.root / "records"
        self.tokens_dir = self.root / "tokens"
        self.audit_dir = self.root / "audit"
        self.audit_log_path = self.root / "audit.jsonl"
        for directory in (self.root, self.records_dir, self.tokens_dir, self.audit_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self.executor = executor
        self.token_ttl_s = token_ttl_s
        self._lock = threading.RLock()

    def _record_key(self, run_id: str, halt_id: str) -> str:
        return _sha256_text(f"{run_id}\0{halt_id}")[:40]

    def _record_path(self, run_id: str, halt_id: str) -> Path:
        return self.records_dir / f"{self._record_key(run_id, halt_id)}.json"

    def _token_index_path(self, token_sha256: str) -> Path:
        return self.tokens_dir / f"{token_sha256[:40]}.json"

    def register_halt(
        self,
        *,
        run_id: str,
        halt_id: str,
        workflow: str,
        halt_class: str,
        step_id: str,
        checkpoint_id: Optional[str] = None,
        completed_steps: Optional[list[str] | tuple[str, ...]] = None,
        transaction_outcome: Optional[str] = "HALTED_BEFORE_EFFECT",
        write_in_flight: bool = False,
        parameter_schema: Optional[Mapping[str, Mapping[str, Any]]] = None,
        missing_parameters: Optional[list[str] | tuple[str, ...]] = None,
        immutable_identity_keys: Optional[list[str] | tuple[str, ...]] = None,
        bound_parameters: Optional[Mapping[str, Any]] = None,
        run_dir: Optional[Path | str] = None,
        ttl_s: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Register a halted run and issue a single-use opaque ``resume_token``."""
        if not is_safe_run_id(run_id):
            raise ContinuationError("run_id is not a safe run identifier")
        if not _HALT_ID_RE.fullmatch(halt_id):
            raise ContinuationError("halt_id is not a valid identifier")

        created = now or _now()
        lifetime = self.token_ttl_s if ttl_s is None else float(ttl_s)
        expires = created + timedelta(seconds=lifetime)
        raw_token = f"rtok_{secrets.token_urlsafe(24)}"
        token_sha = _sha256_text(raw_token)

        record = HaltContinuationRecord(
            run_id=run_id,
            halt_id=halt_id,
            workflow=workflow,
            halt_class=halt_class,
            step_id=step_id,
            checkpoint_id=checkpoint_id,
            completed_steps=tuple(completed_steps or ()),
            transaction_outcome=transaction_outcome,
            write_in_flight=write_in_flight,
            parameter_schema={
                str(k): dict(v) for k, v in (parameter_schema or {}).items()
            },
            missing_parameters=tuple(missing_parameters or ()),
            immutable_identity_keys=tuple(immutable_identity_keys or ()),
            bound_parameters=dict(bound_parameters or {}),
            resume_token_sha256=token_sha,
            created_at=_iso(created),
            expires_at=_iso(expires),
            consumed=False,
            run_dir=str(run_dir) if run_dir is not None else None,
        )

        with self._lock:
            _write_json_atomic(self._record_path(run_id, halt_id), record.to_dict())
            _write_json_atomic(
                self._token_index_path(token_sha),
                {"run_id": run_id, "halt_id": halt_id, "token_sha256": token_sha},
            )

        return {
            "run_id": run_id,
            "halt_id": halt_id,
            "resume_token": raw_token,
            "halt_class": halt_class,
            "step_id": step_id,
            "checkpoint_id": checkpoint_id,
            "missing_parameters": list(record.missing_parameters),
            "expires_at": record.expires_at,
        }

    def _lookup_record(
        self,
        *,
        resume_token: Optional[str],
        run_id: Optional[str],
        halt_id: Optional[str],
    ) -> tuple[Optional[HaltContinuationRecord], Optional[str], Optional[str]]:
        """Resolve a halt record by ``resume_token`` or ``(run_id, halt_id)``.

        Returns ``(record, token_sha256, error_detail)``.
        """
        token_sha: Optional[str] = None
        token_record: Optional[HaltContinuationRecord] = None
        id_record: Optional[HaltContinuationRecord] = None

        if resume_token is not None:
            if not isinstance(resume_token, str) or not _RESUME_TOKEN_RE.fullmatch(
                resume_token
            ):
                return None, None, "malformed resume_token"
            token_sha = _sha256_text(resume_token)
            index = _read_json(self._token_index_path(token_sha))
            if index is None or index.get("token_sha256") != token_sha:
                return None, token_sha, "unknown resume_token"
            rec_data = _read_json(
                self._record_path(str(index["run_id"]), str(index["halt_id"]))
            )
            token_record = (
                HaltContinuationRecord.from_dict(rec_data) if rec_data else None
            )
            if token_record is None or token_record.resume_token_sha256 != token_sha:
                return None, token_sha, "unknown resume_token"

        if run_id is not None or halt_id is not None:
            if not (isinstance(run_id, str) and isinstance(halt_id, str)):
                return None, token_sha, "both run_id and halt_id are required together"
            if not is_safe_run_id(run_id) or not _HALT_ID_RE.fullmatch(halt_id):
                return None, token_sha, "invalid run_id or halt_id"
            rec_data = _read_json(self._record_path(run_id, halt_id))
            id_record = HaltContinuationRecord.from_dict(rec_data) if rec_data else None
            if id_record is None:
                return (
                    None,
                    token_sha,
                    "no halted run found for that run_id and halt_id",
                )

        if token_record is not None and id_record is not None:
            if (
                token_record.run_id != id_record.run_id
                or token_record.halt_id != id_record.halt_id
            ):
                return None, token_sha, "resume_token does not match run_id and halt_id"
            return token_record, token_sha, None

        if token_record is not None:
            return token_record, token_sha, None
        if id_record is not None:
            return id_record, id_record.resume_token_sha256, None

        return None, None, "provide resume_token or both run_id and halt_id"

    def _record_audit(
        self,
        *,
        run_id: Optional[str],
        halt_id: Optional[str],
        resume_token_sha256: Optional[str],
        halt_class: Optional[str],
        retry_transient: bool,
        supplied_parameters: Mapping[str, Any],
        decision: str,
        refusal_code: Optional[str] = None,
        resumed_from_step: Optional[str] = None,
        resumed_from_checkpoint: Optional[str] = None,
        outcome: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        audit_id = f"cont-audit-{secrets.token_hex(12)}"
        entry: dict[str, Any] = {
            "schema": CONTINUATION_AUDIT_SCHEMA,
            "audit_id": audit_id,
            "timestamp": _iso(now or _now()),
            "run_id": run_id,
            "halt_id": halt_id,
            "resume_token_sha256": resume_token_sha256,
            "halt_class": halt_class,
            "retry_transient": bool(retry_transient),
            "supplied_parameters": redact_parameters(supplied_parameters),
            "decision": decision,
            "refusal_code": refusal_code,
            "resumed_from_step": resumed_from_step,
            "resumed_from_checkpoint": resumed_from_checkpoint,
            "outcome": dict(outcome) if isinstance(outcome, Mapping) else None,
        }
        _write_json_atomic(self.audit_dir / f"{audit_id}.json", entry)
        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
            os.chmod(self.audit_log_path, 0o600)
        except OSError:
            _LOG.warning("could not append to continuation audit log")
        return entry

    def read_audit_entry(self, audit_id: str) -> Optional[dict[str, Any]]:
        """Read one persisted continuation audit entry by id."""
        if not re.fullmatch(r"^cont-audit-[0-9a-f]{24}$", audit_id):
            return None
        return _read_json(self.audit_dir / f"{audit_id}.json")

    def list_audit_entries(self) -> list[dict[str, Any]]:
        """Return all persisted continuation audit entries in chronological order."""
        entries: list[dict[str, Any]] = []
        if not self.audit_log_path.is_file():
            return entries
        for line in self.audit_log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def continue_halted_run(
        self,
        *,
        resume_token: Optional[str] = None,
        run_id: Optional[str] = None,
        halt_id: Optional[str] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        retry_transient: bool = False,
        wait_seconds: int = 60,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Evaluate policy, validate parameters, audit, and resume a halted run."""
        supplied = dict(parameters) if isinstance(parameters, Mapping) else {}
        current_time = now or _now()

        with self._lock:
            record, token_sha, lookup_error = self._lookup_record(
                resume_token=resume_token,
                run_id=run_id,
                halt_id=halt_id,
            )
            if record is None:
                audit = self._record_audit(
                    run_id=run_id if isinstance(run_id, str) else None,
                    halt_id=halt_id if isinstance(halt_id, str) else None,
                    resume_token_sha256=token_sha,
                    halt_class=None,
                    retry_transient=retry_transient,
                    supplied_parameters=supplied,
                    decision="refused",
                    refusal_code="human_sign_off_required",
                    now=current_time,
                )
                return {
                    "continued": False,
                    "status": "refused",
                    "error": "human_sign_off_required",
                    "reason": "invalid_resume_token",
                    "run_id": run_id if isinstance(run_id, str) else None,
                    "halt_id": halt_id if isinstance(halt_id, str) else None,
                    "what_happened": (
                        f"Could not verify the halted run ({lookup_error}). "
                        "Nothing was resumed."
                    ),
                    "next_action": (
                        "Check the resume_token or run_id and halt_id, or have a person "
                        "inspect the halted run in a local review surface."
                    ),
                    "human_review_surfaces": list(HUMAN_REVIEW_SURFACES),
                    "audit_id": audit["audit_id"],
                }

            decision = evaluate_continuation_policy(
                record,
                parameters=supplied,
                retry_transient=retry_transient,
                now=current_time,
            )
            if not decision.allowed:
                audit = self._record_audit(
                    run_id=record.run_id,
                    halt_id=record.halt_id,
                    resume_token_sha256=token_sha or record.resume_token_sha256,
                    halt_class=record.halt_class,
                    retry_transient=retry_transient,
                    supplied_parameters=supplied,
                    decision="refused",
                    refusal_code=decision.error or "human_sign_off_required",
                    now=current_time,
                )
                response: dict[str, Any] = {
                    "continued": False,
                    "status": "refused",
                    "error": decision.error or "human_sign_off_required",
                    "reason": decision.reason
                    or decision.error
                    or "human_sign_off_required",
                    "halt_class": record.halt_class,
                    "run_id": record.run_id,
                    "halt_id": record.halt_id,
                    "what_happened": decision.what_happened,
                    "next_action": decision.next_action,
                    "audit_id": audit["audit_id"],
                }
                if decision.human_sign_off_required:
                    response["human_review_surfaces"] = list(HUMAN_REVIEW_SURFACES)
                if decision.invalid_parameters:
                    response["invalid_parameters"] = list(decision.invalid_parameters)
                return response

            if self.executor is None:
                raise ContinuationError(
                    "no continuation executor is configured on this server"
                )

            # Mark consumed before resuming so concurrent or repeated calls
            # with the same resume_token can never resume the run twice.
            merged = {**record.bound_parameters, **supplied}
            consumed_record = HaltContinuationRecord(
                run_id=record.run_id,
                halt_id=record.halt_id,
                workflow=record.workflow,
                halt_class=record.halt_class,
                step_id=record.step_id,
                checkpoint_id=record.checkpoint_id,
                completed_steps=record.completed_steps,
                transaction_outcome=record.transaction_outcome,
                write_in_flight=record.write_in_flight,
                parameter_schema=record.parameter_schema,
                missing_parameters=record.missing_parameters,
                immutable_identity_keys=record.immutable_identity_keys,
                bound_parameters=record.bound_parameters,
                resume_token_sha256=record.resume_token_sha256,
                created_at=record.created_at,
                expires_at=record.expires_at,
                consumed=True,
                consumed_at=_iso(current_time),
                run_dir=record.run_dir,
            )
            _write_json_atomic(
                self._record_path(record.run_id, record.halt_id),
                consumed_record.to_dict(),
            )

            resumed_result = self.executor.resume_halted_run(
                record=record,
                supplied_parameters=supplied,
                merged_parameters=merged,
                retry_transient=retry_transient,
                wait_seconds=wait_seconds,
            )

            audit = self._record_audit(
                run_id=record.run_id,
                halt_id=record.halt_id,
                resume_token_sha256=token_sha or record.resume_token_sha256,
                halt_class=record.halt_class,
                retry_transient=retry_transient,
                supplied_parameters=supplied,
                decision="continued",
                resumed_from_step=record.step_id,
                resumed_from_checkpoint=record.checkpoint_id,
                outcome=resumed_result if isinstance(resumed_result, Mapping) else None,
                now=current_time,
            )

            final_record = HaltContinuationRecord(
                **{
                    **consumed_record.__dict__,
                    "consumed_audit_id": audit["audit_id"],
                }
            )
            _write_json_atomic(
                self._record_path(record.run_id, record.halt_id),
                final_record.to_dict(),
            )

            return {
                "continued": True,
                "status": "continued",
                "run_id": record.run_id,
                "halt_id": record.halt_id,
                "workflow": record.workflow,
                "halt_class": record.halt_class,
                "resumed_from_step": record.step_id,
                "resumed_from_checkpoint": record.checkpoint_id,
                "supplied_parameter_keys": sorted(supplied.keys()),
                "retry_transient": bool(retry_transient),
                "audit_id": audit["audit_id"],
                "result": resumed_result,
            }


#: JSON Schema for ``continue_halted_run`` MCP tool input.
CONTINUATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resume_token": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Single-use opaque resume token from the halted run packet."
            ),
        },
        "run_id": {
            "type": "string",
            "pattern": r"^run-[A-Za-z0-9._-]{1,124}$",
            "description": (
                "Run identifier from the halted run packet. Pass with halt_id "
                "when not using resume_token."
            ),
        },
        "halt_id": {
            "type": "string",
            "pattern": _HALT_ID_RE.pattern,
            "description": (
                "Halt identifier from the halted run packet. Pass with run_id "
                "when not using resume_token."
            ),
        },
        "parameters": {
            "type": "object",
            "description": (
                "Missing required parameters to supply for a missing_parameter "
                "or missing_input halt. Immutable identity keys cannot be overwritten."
            ),
        },
        "retry_transient": {
            "type": "boolean",
            "default": False,
            "description": (
                "Set to true to retry a pre-write transient_network or "
                "retryable_timeout halt."
            ),
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 600,
            "default": 60,
            "description": "How long to wait for the resumed run to finish.",
        },
    },
    "anyOf": [
        {"required": ["resume_token"]},
        {"required": ["run_id", "halt_id"]},
    ],
    "additionalProperties": False,
}

#: JSON Schema for ``continue_halted_run`` MCP tool output.
CONTINUATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "continued": {"type": "boolean"},
        "status": {"type": "string", "enum": ["continued", "refused"]},
        "error": {"type": "string"},
        "reason": {"type": "string"},
        "run_id": {"type": ["string", "null"]},
        "halt_id": {"type": ["string", "null"]},
        "workflow": {"type": "string"},
        "halt_class": {"type": "string"},
        "resumed_from_step": {"type": "string"},
        "resumed_from_checkpoint": {"type": ["string", "null"]},
        "supplied_parameter_keys": {
            "type": "array",
            "items": {"type": "string"},
        },
        "retry_transient": {"type": "boolean"},
        "what_happened": {"type": "string"},
        "next_action": {"type": "string"},
        "human_review_surfaces": {
            "type": "array",
            "items": {"type": "string"},
        },
        "invalid_parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parameter": {"type": "string"},
                    "problem": {"type": "string"},
                },
                "required": ["parameter", "problem"],
            },
        },
        "audit_id": {"type": "string"},
        "result": {"type": "object"},
    },
    "required": ["continued", "status", "audit_id"],
}
