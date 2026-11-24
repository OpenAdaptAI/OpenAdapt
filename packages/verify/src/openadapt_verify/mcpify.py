"""Compile a deterministic workflow pack into a verified, $0-token local MCP server.

``openadapt_verify.mcpify`` turns a compiled workflow specification (``.oapack.json``
or workflow manifest JSON) into a lightweight MCP JSON-RPC 2.0 ``stdio`` server
using Python stdlib + ``cryptography`` (no external MCP SDK required).

Every ``tools/call`` invocation:
1. Requires the MCP ``initialize`` + ``notifications/initialized`` handshake.
2. Validates the tool name and input arguments against the tool's typed JSON Schema
   (fail-closed on unknown tool or missing/invalid arguments).
3. Runs the compiled deterministic step callback with zero LLM token calls.
4. Queries an independent read-back oracle against the system of record.
5. Returns ``VERIFIED`` with a tamper-evident Ed25519 receipt digest only when a
   write was applied and independent read-back confirms the expected state;
   otherwise returns ``HALT`` (``isError: true``) with the exact mismatch/abstain
   reason and human-review handoff details.

No external MCP SDK is required, and no vision, GUI, or ML dependencies are imported.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    TextIO,
    Union,
)

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.2.0"
RECEIPT_SCHEMA_VERSION = "openadapt.mcpify.receipt/v1"
SUPPORTED_CLIENTS = ("claude", "cursor", "generic")

_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Standard JSON-RPC 2.0 error codes.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _canonical_json_bytes(obj: Any) -> bytes:
    """Serialize *obj* to deterministic UTF-8 JSON bytes for hashing and signing."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SafetyPolicy:
    """Fail-closed safety policy for a compiled MCP workflow tool."""

    fail_closed: bool = True
    require_readback: bool = True
    require_signed_receipt: bool = True
    handoff_action: str = (
        "Inspect the target system of record and reconcile before retrying."
    )
    escalation_contact: str = "operator"

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> SafetyPolicy:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("safety_policy must be a JSON object")
        fail_closed = raw.get("fail_closed", True)
        require_readback = raw.get("require_readback", True)
        require_signed_receipt = raw.get("require_signed_receipt", True)
        if not isinstance(fail_closed, bool) or not fail_closed:
            raise ValueError("safety_policy.fail_closed must be true")
        if not isinstance(require_readback, bool) or not require_readback:
            raise ValueError("safety_policy.require_readback must be true")
        if not isinstance(require_signed_receipt, bool) or not require_signed_receipt:
            raise ValueError("safety_policy.require_signed_receipt must be true")
        handoff_action = raw.get("handoff_action", cls.handoff_action)
        escalation_contact = raw.get("escalation_contact", cls.escalation_contact)
        if not isinstance(handoff_action, str) or not handoff_action.strip():
            raise ValueError("safety_policy.handoff_action must be a non-empty string")
        if not isinstance(escalation_contact, str) or not escalation_contact.strip():
            raise ValueError(
                "safety_policy.escalation_contact must be a non-empty string"
            )
        return cls(
            fail_closed=fail_closed,
            require_readback=require_readback,
            require_signed_receipt=require_signed_receipt,
            handoff_action=handoff_action.strip(),
            escalation_contact=escalation_contact.strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fail_closed": self.fail_closed,
            "require_readback": self.require_readback,
            "require_signed_receipt": self.require_signed_receipt,
            "handoff_action": self.handoff_action,
            "escalation_contact": self.escalation_contact,
        }


@dataclass(frozen=True)
class WorkflowToolSpec:
    """Compiled workflow tool specification loaded from a ``.oapack.json`` manifest."""

    name: str
    description: str
    input_schema: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    readback_contract: dict[str, Any]
    safety_policy: SafetyPolicy = field(default_factory=SafetyPolicy)
    schema_version: str = "openadapt.oapack/v1"

    def to_mcp_tool(self) -> dict[str, Any]:
        """Return the MCP ``tools/list`` descriptor for this workflow tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkflowToolSpec:
        if not isinstance(data, Mapping):
            raise ValueError("Workflow spec must be a JSON object")

        name = data.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
            raise ValueError(
                "Workflow spec 'name' must match ^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
            )

        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Workflow spec 'description' must be a non-empty string")

        input_schema = data.get("input_schema")
        if not isinstance(input_schema, dict):
            raise ValueError("Workflow spec 'input_schema' must be a JSON object")
        if input_schema.get("type") != "object":
            raise ValueError("Workflow spec 'input_schema.type' must be 'object'")
        properties = input_schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(
                "Workflow spec 'input_schema.properties' must be an object"
            )
        required = input_schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise ValueError(
                "Workflow spec 'input_schema.required' must be a list of strings"
            )

        raw_steps = data.get("steps")
        if (
            not isinstance(raw_steps, list)
            or not raw_steps
            or not all(isinstance(step, dict) for step in raw_steps)
        ):
            raise ValueError(
                "Workflow spec 'steps' must be a non-empty list of step objects"
            )

        readback_contract = data.get("readback_contract")
        if not isinstance(readback_contract, dict) or not readback_contract:
            raise ValueError(
                "Workflow spec 'readback_contract' must be a non-empty JSON object"
            )

        safety_policy = SafetyPolicy.from_dict(data.get("safety_policy"))
        schema_version = str(data.get("schema_version", "openadapt.oapack/v1"))

        return cls(
            name=name,
            description=description.strip(),
            input_schema=dict(input_schema),
            steps=tuple(dict(step) for step in raw_steps),
            readback_contract=dict(readback_contract),
            safety_policy=safety_policy,
            schema_version=schema_version,
        )


def load_workflow_spec(path: Union[str, Path]) -> WorkflowToolSpec:
    """Load and validate a single ``WorkflowToolSpec`` from a JSON manifest file."""
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"Workflow spec not found: {spec_path}")
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in workflow spec {spec_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("Workflow spec file must contain a JSON object")
    if "tools" in raw and "name" not in raw:
        tools = raw["tools"]
        if not isinstance(tools, list) or len(tools) != 1:
            raise ValueError(
                "Multi-tool packs must be loaded with load_workflow_specs()"
            )
        return WorkflowToolSpec.from_dict(tools[0])
    return WorkflowToolSpec.from_dict(raw)


def load_workflow_specs(path: Union[str, Path]) -> tuple[WorkflowToolSpec, ...]:
    """Load one or more ``WorkflowToolSpec`` entries from a pack JSON file."""
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"Workflow spec not found: {spec_path}")
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in workflow spec {spec_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("Workflow spec file must contain a JSON object")
    if "tools" in raw:
        tools = raw["tools"]
        if not isinstance(tools, list) or not tools:
            raise ValueError("Workflow pack 'tools' must be a non-empty list")
        specs = tuple(WorkflowToolSpec.from_dict(item) for item in tools)
    else:
        specs = (WorkflowToolSpec.from_dict(raw),)
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"Duplicate tool name in workflow pack: {spec.name}")
        seen.add(spec.name)
    return specs


@dataclass(frozen=True)
class ReadbackResult:
    """Outcome from the independent system-of-record read-back oracle."""

    verdict: Literal["VERIFIED", "MISMATCH", "ABSTAIN"]
    reason: str
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    discrepancies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "expected": self.expected,
            "observed": self.observed,
            "discrepancies": list(self.discrepancies),
        }


@dataclass(frozen=True)
class SignedReceipt:
    """Tamper-evident Ed25519 receipt proving a verified deterministic execution."""

    schema_version: str
    algorithm: str
    receipt_digest: str
    payload_sha256: str
    signature_b64: str
    public_key_b64: str
    timestamp_utc: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "algorithm": self.algorithm,
            "receipt_digest": self.receipt_digest,
            "payload_sha256": self.payload_sha256,
            "signature_b64": self.signature_b64,
            "public_key_b64": self.public_key_b64,
            "timestamp_utc": self.timestamp_utc,
            "payload": self.payload,
        }


def issue_mcpify_receipt(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    readback: ReadbackResult,
    signing_key: Ed25519PrivateKey,
    llm_calls: int = 0,
) -> SignedReceipt:
    """Issue a tamper-evident Ed25519 receipt for a ``VERIFIED`` execution."""
    if readback.verdict != "VERIFIED":
        raise ValueError(
            f"Cannot issue a VERIFIED receipt for verdict {readback.verdict!r}"
        )
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "verdict": "VERIFIED",
        "tool_name": tool_name,
        "arguments_sha256": _sha256_hex(_canonical_json_bytes(dict(arguments))),
        "steps_sha256": _sha256_hex(_canonical_json_bytes(list(steps))),
        "readback_sha256": _sha256_hex(_canonical_json_bytes(readback.to_dict())),
        "llm_calls": llm_calls,
        "timestamp_utc": timestamp_utc,
    }
    payload_bytes = _canonical_json_bytes(payload)
    payload_sha256 = _sha256_hex(payload_bytes)
    signature = signing_key.sign(payload_bytes)
    public_raw = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature_b64 = base64.b64encode(signature).decode("ascii")
    public_key_b64 = base64.b64encode(public_raw).decode("ascii")
    receipt_digest = _sha256_hex(f"{payload_sha256}:{signature_b64}".encode("ascii"))
    return SignedReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        algorithm="Ed25519",
        receipt_digest=receipt_digest,
        payload_sha256=payload_sha256,
        signature_b64=signature_b64,
        public_key_b64=public_key_b64,
        timestamp_utc=timestamp_utc,
        payload=payload,
    )


def load_signing_key(key_path: Union[str, Path]) -> Ed25519PrivateKey:
    """Load a PEM-encoded Ed25519 private signing key from disk."""
    raw = Path(key_path).read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(
            f"Expected an Ed25519 private key in {key_path}, got {type(key).__name__}"
        )
    return key


def _coerce_trusted_public_key(
    trusted_public_key: Union[str, bytes, Ed25519PublicKey],
) -> tuple[Ed25519PublicKey, str]:
    if isinstance(trusted_public_key, Ed25519PublicKey):
        raw = trusted_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return trusted_public_key, base64.b64encode(raw).decode("ascii")
    if isinstance(trusted_public_key, bytes):
        pub = Ed25519PublicKey.from_public_bytes(trusted_public_key)
        return pub, base64.b64encode(trusted_public_key).decode("ascii")
    if isinstance(trusted_public_key, str):
        raw = base64.b64decode(trusted_public_key, validate=True)
        pub = Ed25519PublicKey.from_public_bytes(raw)
        return pub, base64.b64encode(raw).decode("ascii")
    raise TypeError(
        "trusted_public_key must be a base64 str, 32-byte raw key, or Ed25519PublicKey"
    )


def verify_mcpify_receipt(
    receipt: Union[SignedReceipt, Mapping[str, Any]],
    *,
    trusted_public_key: Union[str, bytes, Ed25519PublicKey],
) -> bool:
    """Verify a ``SignedReceipt`` against an operator-pinned Ed25519 public key.

    Refuses self-vouching receipts: *trusted_public_key* is required, the
    receipt's embedded ``public_key_b64`` must match it in constant time, and
    the signature is verified strictly using the trusted key.
    """
    data = receipt.to_dict() if isinstance(receipt, SignedReceipt) else dict(receipt)
    try:
        trusted_pub, expected_pub_b64 = _coerce_trusted_public_key(trusted_public_key)
        receipt_pub_b64 = str(data["public_key_b64"])
        if not hmac.compare_digest(receipt_pub_b64, expected_pub_b64):
            return False
        payload = data["payload"]
        payload_bytes = _canonical_json_bytes(payload)
        expected_payload_sha = _sha256_hex(payload_bytes)
        if data.get("payload_sha256") != expected_payload_sha:
            return False
        signature_b64 = str(data["signature_b64"])
        expected_digest = _sha256_hex(
            f"{expected_payload_sha}:{signature_b64}".encode("ascii")
        )
        if data.get("receipt_digest") != expected_digest:
            return False
        signature = base64.b64decode(signature_b64, validate=True)
        trusted_pub.verify(signature, payload_bytes)
        return True
    except (KeyError, ValueError, TypeError, InvalidSignature):
        return False


def _matches_json_type(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "null":
        return value is None
    return False


def validate_tool_arguments(schema: Mapping[str, Any], arguments: Any) -> list[str]:
    """Validate *arguments* against a typed JSON Schema object; return error list."""
    if not isinstance(arguments, Mapping):
        return ["arguments must be a JSON object"]

    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for req_key in required:
        if req_key not in arguments or arguments[req_key] is None:
            errors.append(f"missing required argument: {req_key}")

    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                errors.append(f"unexpected argument: {key}")

    for key, value in arguments.items():
        if key not in properties:
            continue
        prop_schema = properties[key]
        if not isinstance(prop_schema, Mapping):
            continue
        expected_type = prop_schema.get("type")
        if isinstance(expected_type, str):
            if not _matches_json_type(value, expected_type):
                errors.append(
                    f"argument '{key}' must be of type '{expected_type}', "
                    f"got {type(value).__name__}"
                )
                continue
        elif isinstance(expected_type, list):
            if not any(
                isinstance(t, str) and _matches_json_type(value, t)
                for t in expected_type
            ):
                errors.append(
                    f"argument '{key}' must match one of types {expected_type}"
                )
                continue
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            errors.append(f"argument '{key}' must be one of {prop_schema['enum']!r}")
        if isinstance(value, str):
            min_len = prop_schema.get("minLength")
            if isinstance(min_len, int) and len(value) < min_len:
                errors.append(f"argument '{key}' must have length >= {min_len}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = prop_schema.get("minimum")
            if isinstance(minimum, (int, float)) and value < minimum:
                errors.append(f"argument '{key}' must be >= {minimum}")
            maximum = prop_schema.get("maximum")
            if isinstance(maximum, (int, float)) and value > maximum:
                errors.append(f"argument '{key}' must be <= {maximum}")

    return errors


def _render_value(template: Any, arguments: Mapping[str, Any]) -> Any:
    """Deterministically resolve ``{param}`` placeholders without eval."""
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}"):
            key = template[1:-1]
            if key in arguments:
                return arguments[key]
        result = template
        for key, value in arguments.items():
            result = result.replace("{" + key + "}", str(value))
        return result
    if isinstance(template, list):
        return [_render_value(item, arguments) for item in template]
    if isinstance(template, dict):
        return {k: _render_value(v, arguments) for k, v in template.items()}
    return template


RunnerCallback = Callable[[WorkflowToolSpec, Mapping[str, Any]], Mapping[str, Any]]
ReadbackOracle = Callable[
    [WorkflowToolSpec, Mapping[str, Any], Mapping[str, Any]], ReadbackResult
]


def default_deterministic_runner(
    spec: WorkflowToolSpec, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve and execute compiled workflow steps deterministically (0 LLM calls).

    Executes supported deterministic write ops (``json_write``, ``sqlite_insert``)
    and tracks ``writes_applied``. Steps with non-executable/UI-only ops do not
    increment ``writes_applied``, causing independent read-back to fail closed
    instead of verifying pre-existing state.
    """
    executed_steps = [_render_value(step, arguments) for step in spec.steps]
    writes_applied = 0
    unexecuted_ops: list[str] = []
    applied_targets: list[dict[str, Any]] = []

    for step in executed_steps:
        if not isinstance(step, Mapping):
            continue
        op = step.get("op")
        if op == "json_write":
            target_raw = step.get("path")
            data = step.get("data")
            if not isinstance(target_raw, str) or not isinstance(data, Mapping):
                raise ValueError(
                    "json_write step requires string 'path' and object 'data'"
                )
            target = Path(target_raw)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(dict(data), sort_keys=True), encoding="utf-8")
            tmp.replace(target)
            writes_applied += 1
            applied_targets.append({"kind": "json_file", "path": str(target.resolve())})
        elif op == "sqlite_insert":
            db_raw = step.get("database")
            table = step.get("table")
            values = step.get("values")
            if (
                not isinstance(db_raw, str)
                or not isinstance(table, str)
                or not _SAFE_SQL_IDENTIFIER_RE.match(table)
                or not isinstance(values, Mapping)
                or not values
            ):
                raise ValueError(
                    "sqlite_insert step requires valid 'database', 'table', and non-empty 'values'"
                )
            cols = list(values.keys())
            for col in cols:
                if not isinstance(col, str) or not _SAFE_SQL_IDENTIFIER_RE.match(col):
                    raise ValueError(
                        f"Unsafe column identifier in sqlite_insert: {col!r}"
                    )
            placeholders = ", ".join("?" for _ in cols)
            col_list = ", ".join(cols)
            with sqlite3.connect(db_raw) as conn:
                cursor = conn.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                    [values[c] for c in cols],
                )
                inserted_rowid = cursor.lastrowid
            writes_applied += 1
            applied_targets.append(
                {
                    "kind": "sqlite",
                    "database": str(Path(db_raw).resolve()),
                    "table": table,
                    "rowid": inserted_rowid,
                    "values": dict(values),
                }
            )
        elif isinstance(op, str):
            unexecuted_ops.append(op)

    return {
        "executed_steps": executed_steps,
        "step_count": len(executed_steps),
        "writes_applied": writes_applied,
        "applied_targets": applied_targets,
        "unexecuted_ops": unexecuted_ops,
        "llm_calls": 0,
    }


def _snapshot_sqlite_before_count(
    spec: WorkflowToolSpec,
    arguments: Mapping[str, Any],
) -> Optional[int]:
    """Count rows matching a SQLite read-back contract BEFORE the runner runs."""
    contract = spec.readback_contract
    if contract.get("kind") != "sqlite":
        return None
    db_path_raw = contract.get("database")
    table = contract.get("table")
    match_spec = contract.get("match")
    if (
        not isinstance(db_path_raw, str)
        or not isinstance(table, str)
        or not _SAFE_SQL_IDENTIFIER_RE.match(table)
        or not isinstance(match_spec, Mapping)
        or not match_spec
    ):
        return None
    db_path = Path(db_path_raw)
    if not db_path.is_file():
        return 0
    columns: list[str] = []
    params: list[Any] = []
    for col, tmpl in match_spec.items():
        if not isinstance(col, str) or not _SAFE_SQL_IDENTIFIER_RE.match(col):
            return None
        columns.append(col)
        params.append(_render_value(tmpl, arguments))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            where_clause = " AND ".join(f"{col} = ?" for col in columns)
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where_clause}",
                params,
            ).fetchone()
            return int(row[0]) if row is not None else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def default_readback_oracle(
    spec: WorkflowToolSpec,
    arguments: Mapping[str, Any],
    execution_output: Mapping[str, Any],
) -> ReadbackResult:
    """Built-in read-back oracle supporting SQLite and JSON file contracts.

    Fails closed (returns ``ABSTAIN``) whenever the runner left unexecuted ops,
    did not attest ``writes_applied > 0``, wrote to a different target than the
    read-back contract, or the contract cannot be read independently.
    """
    unexecuted = execution_output.get("unexecuted_ops")
    if isinstance(unexecuted, (list, tuple)) and len(unexecuted) > 0:
        return ReadbackResult(
            verdict="ABSTAIN",
            reason=(
                f"Runner left unexecuted ops {list(unexecuted)!r}; refusing to "
                "verify a partially executed workflow"
            ),
            observed={"unexecuted_ops": list(unexecuted)},
        )

    writes_applied = execution_output.get("writes_applied")
    if (
        isinstance(writes_applied, bool)
        or not isinstance(writes_applied, int)
        or writes_applied <= 0
    ):
        return ReadbackResult(
            verdict="ABSTAIN",
            reason=(
                "Runner did not attest writes_applied > 0; refusing to verify "
                "pre-existing state without an executed write"
            ),
            observed={"writes_applied": writes_applied},
        )

    applied_targets = execution_output.get("applied_targets")
    before_count = execution_output.get("_readback_before_count")
    has_before_count = isinstance(before_count, int) and not isinstance(
        before_count, bool
    )
    contract = spec.readback_contract
    kind = contract.get("kind")

    if kind == "sqlite":
        db_path_raw = contract.get("database")
        table = contract.get("table")
        match_spec = contract.get("match")
        if (
            not isinstance(db_path_raw, str)
            or not isinstance(table, str)
            or not _SAFE_SQL_IDENTIFIER_RE.match(table)
            or not isinstance(match_spec, Mapping)
            or not match_spec
        ):
            return ReadbackResult(
                verdict="ABSTAIN",
                reason="Incomplete or invalid sqlite readback_contract",
            )
        db_path = Path(db_path_raw)
        if not db_path.is_file():
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=f"Read-back SQLite database does not exist: {db_path}",
            )
        resolved_db = str(db_path.resolve())
        written_rowids: list[int] = []
        if isinstance(applied_targets, (list, tuple)):
            matching_targets = [
                t
                for t in applied_targets
                if isinstance(t, Mapping)
                and t.get("kind") == "sqlite"
                and t.get("database") == resolved_db
                and t.get("table") == table
            ]
            if not matching_targets:
                return ReadbackResult(
                    verdict="ABSTAIN",
                    reason=(
                        f"Applied write(s) did not target read-back SQLite "
                        f"table {table!r} in {db_path}"
                    ),
                    observed={
                        "applied_targets": list(applied_targets)
                        if isinstance(applied_targets, (list, tuple))
                        else []
                    },
                )
            written_rowids = [
                t["rowid"]
                for t in matching_targets
                if isinstance(t.get("rowid"), int)
                and not isinstance(t.get("rowid"), bool)
            ]
        elif not has_before_count:
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=(
                    "Runner did not report applied_targets and no before-call "
                    "snapshot is available; refusing to match pre-existing rows"
                ),
                observed={"applied_targets": applied_targets},
            )
        expected: dict[str, Any] = {}
        columns: list[str] = []
        for col, tmpl in match_spec.items():
            if not isinstance(col, str) or not _SAFE_SQL_IDENTIFIER_RE.match(col):
                return ReadbackResult(
                    verdict="ABSTAIN",
                    reason=f"Unsafe column identifier in readback_contract: {col!r}",
                )
            columns.append(col)
            expected[col] = _render_value(tmpl, arguments)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                base_where = " AND ".join(f"{col} = ?" for col in columns)
                base_params: list[Any] = [expected[col] for col in columns]
                where_clause = base_where
                params = list(base_params)
                if written_rowids:
                    rowid_placeholders = ", ".join("?" for _ in written_rowids)
                    where_clause = f"rowid IN ({rowid_placeholders}) AND {base_where}"
                    params = [*written_rowids, *base_params]
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {where_clause}",
                    params,
                ).fetchall()
                total_after: int | None = None
                if has_before_count and before_count is not None:
                    total_after = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE {base_where}",
                            base_params,
                        ).fetchone()[0]
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=f"Independent SQLite read-back failed: {exc}",
                expected=expected,
            )

        expected_count = int(contract.get("expected_count", 1))
        if has_before_count and before_count is not None and total_after is not None:
            new_total = total_after - int(before_count)
            effective_count = min(len(rows), new_total) if written_rowids else new_total
        else:
            effective_count = len(rows)
        if effective_count == expected_count and (
            not written_rowids or len(rows) == expected_count
        ):
            observed = dict(rows[-1]) if rows else {}
            return ReadbackResult(
                verdict="VERIFIED",
                reason="Independent SQLite read-back matched expected record",
                expected=expected,
                observed=observed,
            )
        return ReadbackResult(
            verdict="MISMATCH",
            reason=(
                f"Expected {expected_count} new matching row(s) in {table}, "
                f"observed {effective_count}"
            ),
            expected=expected,
            observed={"matched_rows": len(rows), "new_matched_rows": effective_count},
            discrepancies=(
                f"row_count: expected={expected_count}, observed={effective_count}",
            ),
        )

    if kind == "json_file":
        file_path_raw = contract.get("path")
        expected_spec = contract.get("expected")
        if not isinstance(file_path_raw, str) or not isinstance(expected_spec, Mapping):
            return ReadbackResult(
                verdict="ABSTAIN",
                reason="Incomplete json_file readback_contract",
            )
        file_path = Path(file_path_raw)
        if not file_path.is_file():
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=f"Read-back JSON file not found: {file_path}",
            )
        resolved_file = str(file_path.resolve())
        if not isinstance(applied_targets, (list, tuple)) or not any(
            isinstance(t, Mapping)
            and t.get("kind") == "json_file"
            and t.get("path") == resolved_file
            for t in applied_targets
        ):
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=(
                    f"Applied write(s) did not target read-back JSON file {file_path}"
                ),
                observed={
                    "applied_targets": list(applied_targets)
                    if isinstance(applied_targets, (list, tuple))
                    else []
                },
            )
        try:
            observed_data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return ReadbackResult(
                verdict="ABSTAIN",
                reason=f"Could not read JSON read-back target: {exc}",
            )
        if not isinstance(observed_data, Mapping):
            return ReadbackResult(
                verdict="MISMATCH",
                reason="Read-back JSON root is not an object",
                expected=_render_value(dict(expected_spec), arguments),
                observed={"root_type": type(observed_data).__name__},
                discrepancies=("root_type: expected object",),
            )
        expected = _render_value(dict(expected_spec), arguments)
        discrepancies = [
            f"{k}: expected={v!r}, observed={observed_data.get(k)!r}"
            for k, v in expected.items()
            if observed_data.get(k) != v
        ]
        if not discrepancies:
            return ReadbackResult(
                verdict="VERIFIED",
                reason="Independent JSON read-back matched all expected fields",
                expected=expected,
                observed={k: observed_data.get(k) for k in expected},
            )
        return ReadbackResult(
            verdict="MISMATCH",
            reason="Independent JSON read-back disagreed with expected fields",
            expected=expected,
            observed={k: observed_data.get(k) for k in expected},
            discrepancies=tuple(discrepancies),
        )

    return ReadbackResult(
        verdict="ABSTAIN",
        reason=(
            f"No independent read-back oracle configured for contract kind {kind!r}; "
            "failing closed"
        ),
        expected={},
        observed={"execution_output_keys": sorted(execution_output.keys())},
    )


class McpifyServer:
    """MCP JSON-RPC 2.0 ``stdio`` server (stdlib + ``cryptography``, no MCP SDK)."""

    def __init__(
        self,
        specs: Union[WorkflowToolSpec, Sequence[WorkflowToolSpec]],
        *,
        server_name: str = "openadapt-mcpify",
        runner: Optional[RunnerCallback] = None,
        readback_oracle: Optional[ReadbackOracle] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
    ) -> None:
        if isinstance(specs, WorkflowToolSpec):
            spec_list = [specs]
        else:
            spec_list = list(specs)
        if not spec_list:
            raise ValueError("McpifyServer requires at least one WorkflowToolSpec")
        self._specs: dict[str, WorkflowToolSpec] = {}
        for spec in spec_list:
            if spec.name in self._specs:
                raise ValueError(f"Duplicate tool name: {spec.name}")
            self._specs[spec.name] = spec

        self.server_name = server_name
        self._runner: RunnerCallback = runner or default_deterministic_runner
        self._readback_oracle: ReadbackOracle = (
            readback_oracle or default_readback_oracle
        )
        self._signer = signing_key or Ed25519PrivateKey.generate()
        self._initialize_received: bool = False
        self.initialized: bool = False

    @property
    def public_key_b64(self) -> str:
        raw = self._signer.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: Any, code: int, message: str, data: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}

    def handle_message(self, message: Any) -> Optional[dict[str, Any]]:
        """Process one decoded JSON-RPC 2.0 message; return response or ``None``."""
        if not isinstance(message, Mapping):
            return self._error(
                None, JSONRPC_INVALID_REQUEST, "Request must be an object"
            )
        if message.get("jsonrpc") != "2.0":
            return self._error(
                message.get("id"),
                JSONRPC_INVALID_REQUEST,
                "jsonrpc field must be '2.0'",
            )

        method = message.get("method")
        if not isinstance(method, str) or not method:
            return self._error(
                message.get("id"),
                JSONRPC_INVALID_REQUEST,
                "method must be a non-empty string",
            )

        is_notification = "id" not in message
        request_id = message.get("id")
        params = message.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            if is_notification:
                return None
            return self._error(
                request_id, JSONRPC_INVALID_PARAMS, "params must be an object"
            )

        if method == "notifications/initialized":
            if self._initialize_received:
                self.initialized = True
            return None

        if is_notification:
            return None

        if method == "initialize":
            return self._handle_initialize(request_id, params)
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._handle_tools_list(request_id)
        if method == "tools/call":
            return self._handle_tools_call(request_id, params)

        return self._error(
            request_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}"
        )

    def _handle_initialize(
        self, request_id: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        del params
        self._initialize_received = True
        return self._ok(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": self.server_name,
                    "version": SERVER_VERSION,
                },
            },
        )

    def _handle_tools_list(self, request_id: Any) -> dict[str, Any]:
        return self._ok(
            request_id,
            {"tools": [spec.to_mcp_tool() for spec in self._specs.values()]},
        )

    def _build_halt_result(
        self,
        spec: WorkflowToolSpec,
        *,
        verdict: str,
        reason: str,
        expected: Mapping[str, Any],
        observed: Mapping[str, Any],
        discrepancies: Sequence[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "HALT",
            "verdict": verdict,
            "tool": spec.name,
            "reason": reason,
            "discrepancies": list(discrepancies),
            "expected": dict(expected),
            "observed": dict(observed),
            "human_review": {
                "required": True,
                "handoff_action": spec.safety_policy.handoff_action,
                "escalation_contact": spec.safety_policy.escalation_contact,
            },
            "receipt": None,
        }
        return {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "structuredContent": payload,
        }

    def _handle_tools_call(
        self, request_id: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not self.initialized:
            return self._error(
                request_id,
                JSONRPC_INVALID_REQUEST,
                "Server not initialized; send 'initialize' and 'notifications/initialized' before 'tools/call'",
            )
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name not in self._specs:
            return self._error(
                request_id,
                JSONRPC_INVALID_PARAMS,
                f"Unknown tool: {tool_name!r}",
            )

        spec = self._specs[tool_name]
        arguments = params.get("arguments", {})
        validation_errors = validate_tool_arguments(spec.input_schema, arguments)
        if validation_errors:
            return self._error(
                request_id,
                JSONRPC_INVALID_PARAMS,
                "; ".join(validation_errors),
                data={"validation_errors": validation_errors},
            )

        before_count = _snapshot_sqlite_before_count(spec, arguments)
        try:
            execution_output = dict(self._runner(spec, arguments))
        except Exception as exc:
            return self._ok(
                request_id,
                self._build_halt_result(
                    spec,
                    verdict="EXECUTION_ERROR",
                    reason=f"Deterministic runner failed: {type(exc).__name__}: {exc}",
                    expected={},
                    observed={},
                    discrepancies=(f"runner_exception: {type(exc).__name__}",),
                ),
            )
        if before_count is not None:
            execution_output["_readback_before_count"] = before_count

        llm_calls = int(execution_output.get("llm_calls", 0))
        if llm_calls != 0:
            return self._ok(
                request_id,
                self._build_halt_result(
                    spec,
                    verdict="ABSTAIN",
                    reason=(
                        f"Compiled workflow violated $0-token invariant "
                        f"(llm_calls={llm_calls})"
                    ),
                    expected={"llm_calls": 0},
                    observed={"llm_calls": llm_calls},
                    discrepancies=(f"llm_calls: expected=0, observed={llm_calls}",),
                ),
            )

        try:
            readback = self._readback_oracle(spec, arguments, execution_output)
        except Exception as exc:
            return self._ok(
                request_id,
                self._build_halt_result(
                    spec,
                    verdict="ABSTAIN",
                    reason=(
                        f"Independent read-back oracle errored: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    expected={},
                    observed={},
                    discrepancies=(f"readback_exception: {type(exc).__name__}",),
                ),
            )

        if not isinstance(readback, ReadbackResult) or readback.verdict != "VERIFIED":
            verdict = (
                readback.verdict if isinstance(readback, ReadbackResult) else "ABSTAIN"
            )
            reason = (
                readback.reason
                if isinstance(readback, ReadbackResult)
                else "Read-back oracle returned an invalid result object"
            )
            expected = readback.expected if isinstance(readback, ReadbackResult) else {}
            observed = readback.observed if isinstance(readback, ReadbackResult) else {}
            discrepancies = (
                readback.discrepancies
                if isinstance(readback, ReadbackResult)
                else ("invalid_readback_result",)
            )
            return self._ok(
                request_id,
                self._build_halt_result(
                    spec,
                    verdict=verdict,
                    reason=reason,
                    expected=expected,
                    observed=observed,
                    discrepancies=discrepancies,
                ),
            )

        raw_executed = execution_output.get("executed_steps")
        executed_steps: Sequence[Mapping[str, Any]] = (
            raw_executed
            if isinstance(raw_executed, list)
            else [_render_value(step, arguments) for step in spec.steps]
        )
        receipt = issue_mcpify_receipt(
            tool_name=spec.name,
            arguments=arguments,
            steps=executed_steps,
            readback=readback,
            signing_key=self._signer,
            llm_calls=0,
        )
        payload: dict[str, Any] = {
            "status": "VERIFIED",
            "tool": spec.name,
            "llm_calls": 0,
            "readback": readback.to_dict(),
            "receipt": receipt.to_dict(),
        }
        return self._ok(
            request_id,
            {
                "isError": False,
                "content": [
                    {"type": "text", "text": json.dumps(payload, sort_keys=True)}
                ],
                "structuredContent": payload,
            },
        )

    def serve_stdio(
        self,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
    ) -> None:
        """Run the line-delimited JSON-RPC 2.0 loop over *stdin* and *stdout*."""
        in_stream = stdin if stdin is not None else sys.stdin
        out_stream = stdout if stdout is not None else sys.stdout

        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: Optional[dict[str, Any]] = self._error(
                    None, JSONRPC_PARSE_ERROR, f"Parse error: {exc}"
                )
            else:
                response = self.handle_message(message)

            if response is not None:
                out_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
                out_stream.flush()


def generate_mcp_client_config(
    spec_path: Union[str, Path],
    server_name: Optional[str] = None,
    *,
    client: str = "generic",
    python_executable: Optional[str] = None,
    signing_key_path: Optional[Union[str, Path]] = None,
) -> dict[str, Any]:
    """Generate a copy-paste ``mcpServers`` JSON snippet for an MCP client."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(
            f"Unsupported client {client!r}; expected one of {SUPPORTED_CLIENTS}"
        )

    resolved_spec = Path(spec_path).expanduser().resolve()
    specs = load_workflow_specs(resolved_spec)
    effective_name = server_name or specs[0].name
    command = python_executable or sys.executable

    args = [
        "-m",
        "openadapt_verify.mcpify",
        "serve",
        "--spec",
        str(resolved_spec),
        "--server-name",
        effective_name,
    ]
    if signing_key_path is not None:
        args.extend(
            ["--signing-key", str(Path(signing_key_path).expanduser().resolve())]
        )

    entry: dict[str, Any] = {
        "command": command,
        "args": args,
    }
    if client == "cursor":
        entry["type"] = "stdio"

    return {"mcpServers": {effective_name: entry}}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m openadapt_verify.mcpify",
        description=(
            "Serve a compiled workflow pack as a verified, $0-token local MCP "
            "server or generate client configuration."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser("serve", help="Run the live stdio MCP server.")
    serve_cmd.add_argument(
        "--spec",
        required=True,
        help="Path to the compiled workflow pack JSON (.oapack.json).",
    )
    serve_cmd.add_argument(
        "--server-name",
        default=None,
        help="MCP server name reported in serverInfo (defaults to first tool name).",
    )
    serve_cmd.add_argument(
        "--signing-key",
        default=None,
        help="Path to a PEM-encoded Ed25519 private key so receipts use a stable operator key across restarts.",
    )

    config_cmd = sub.add_parser(
        "config",
        help="Print a copy-paste mcpServers JSON snippet for an MCP client.",
    )
    config_cmd.add_argument(
        "--spec",
        required=True,
        help="Path to the compiled workflow pack JSON (.oapack.json).",
    )
    config_cmd.add_argument(
        "--client",
        choices=SUPPORTED_CLIENTS,
        default="generic",
        help="Target MCP client format (claude, cursor, or generic).",
    )
    config_cmd.add_argument(
        "--server-name",
        default=None,
        help="Key name under mcpServers (defaults to first tool name).",
    )
    config_cmd.add_argument(
        "--signing-key",
        default=None,
        help="Optional PEM-encoded Ed25519 private key path to include in the serve args.",
    )

    pubkey_cmd = sub.add_parser(
        "pubkey",
        help="Print the base64 Ed25519 public key for a PEM signing key so operators can pin it.",
    )
    pubkey_cmd.add_argument(
        "--signing-key",
        required=True,
        help="Path to a PEM-encoded Ed25519 private key.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        specs = load_workflow_specs(args.spec)
        server_name = args.server_name or specs[0].name
        signing_key = load_signing_key(args.signing_key) if args.signing_key else None
        server = McpifyServer(specs, server_name=server_name, signing_key=signing_key)
        sys.stderr.write(f"[openadapt-mcpify] public_key_b64={server.public_key_b64}\n")
        sys.stderr.flush()
        server.serve_stdio()
        return 0

    if args.command == "pubkey":
        key = load_signing_key(args.signing_key)
        raw = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        print(base64.b64encode(raw).decode("ascii"))
        return 0

    if args.command == "config":
        snippet = generate_mcp_client_config(
            args.spec,
            server_name=args.server_name,
            client=args.client,
            signing_key_path=args.signing_key,
        )
        print(json.dumps(snippet, indent=2))
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
