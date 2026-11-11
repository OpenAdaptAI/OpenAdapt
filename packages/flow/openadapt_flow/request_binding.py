"""Associate a normal transport reservation with verifier receipt bytes.

These local bindings do not authenticate a caller, issuer or signing key.
A direct caller of a compiled server can send ai.openadapt/request-context
for any request whose run ID it knows; the stored digest prevents post-facto
tampering or substitution.

Execution verification and request/receipt association are separate results.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTEXT_SCHEMA = "openadapt.request-context/1"
BINDING_SCHEMA = "openadapt.receipt-binding/1"
MCP_META_KEY = "ai.openadapt/request-context"
MAX_RECEIPT_BYTES = 8_000_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_JSON_INTEGER_BITS = 4096
MAX_PATH_COMPONENTS = 128
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class BindingError(ValueError):
    """A fixed refusal category; never includes receipt values or paths."""


@dataclass(frozen=True)
class RequestContext:
    run_id: str
    request_ref: str
    workflow: str
    fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": CONTEXT_SCHEMA,
            "run_id": self.run_id,
            "request_ref": self.request_ref,
            "workflow": self.workflow,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def parse(cls, value: object) -> RequestContext:
        if (
            type(value) is not dict
            or any(type(key) is not str for key in value)
            or set(value)
            != {"schema", "run_id", "request_ref", "workflow", "fingerprint"}
        ):
            raise BindingError("request_context_shape")
        if any(type(v) is not str for v in value.values()):
            raise BindingError("request_context_type")
        if (
            value["schema"] != CONTEXT_SCHEMA
            or not _OPAQUE.fullmatch(value["run_id"])
            or ".." in value["run_id"]
            or not value["request_ref"].startswith("sha256:")
            or not _HEX.fullmatch(value["request_ref"][7:])
            or not value["workflow"].startswith("wrap:")
            or not _HEX.fullmatch(value["workflow"][5:])
            or not _HEX.fullmatch(value["fingerprint"])
        ):
            raise BindingError("request_context_value")
        return cls(
            value["run_id"],
            value["request_ref"],
            value["workflow"],
            value["fingerprint"],
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> RequestContext:
        if record.get("mode") != "skill-cache" or record.get("state") not in {
            "running",
            "finished",
        }:
            raise BindingError("request_reservation")
        request_id = record.get("request_id")
        if type(request_id) is not str or not 1 <= len(request_id) <= 128:
            raise BindingError("request_reference")
        return cls.parse(
            {
                "schema": CONTEXT_SCHEMA,
                "run_id": record.get("run_id"),
                "request_ref": "sha256:"
                + hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
                "workflow": record.get("workflow"),
                "fingerprint": record.get("fingerprint"),
            }
        )


_CURRENT: ContextVar[RequestContext | None] = ContextVar(
    "openadapt_request_context", default=None
)


def current_request_context() -> RequestContext | None:
    return _CURRENT.get()


def request_meta() -> dict[str, Any] | None:
    """The exact namespaced transport projection of the active owned scope."""
    context = current_request_context()
    return None if context is None else {MCP_META_KEY: context.to_dict()}


@contextmanager
def request_scope(context: RequestContext | None) -> Iterator[None]:
    if context is not None:
        if type(context) is not RequestContext:
            raise BindingError("request_context_type")
        context = RequestContext.parse(context.to_dict())
    previous = _CURRENT.get()
    if previous is not None and previous != context:
        raise BindingError("request_context_conflict")
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def context_from_mcp(meta: object) -> RequestContext | None:
    if meta is None:
        return None
    # MCP 1 has a Pydantic Meta object; MCP 2 uses a mapping. This is not a
    # generic callback protocol and does not interpret business arguments.
    if type(meta) is not dict:
        from pydantic import BaseModel

        if not isinstance(meta, BaseModel):
            raise BindingError("mcp_context_metadata")
        meta = meta.model_dump(by_alias=True)
    if type(meta) is not dict:
        raise BindingError("mcp_context_metadata")
    document = _json_value(meta)
    if type(document) is not dict:
        raise BindingError("mcp_context_metadata")
    if MCP_META_KEY not in document:
        return None
    return RequestContext.parse(document[MCP_META_KEY])


@contextmanager
def mcp_request_scope(context: Any) -> Iterator[None]:
    """Use native transport metadata with the existing strict context model."""
    try:
        meta = context.request_context.meta if context is not None else None
    except ValueError:
        # Native direct calls have no incoming request context.
        meta = None
    with request_scope(context_from_mcp(meta)):
        yield


def _json_value(value: Any) -> Any:
    """Detach bounded exact JSON values without serialization coercions.

    This is a representation check, not a receipt or caller authority check.
    Keys and values share one aggregate budget. Cycles exceed the depth bound;
    repeated finite subtrees count once per occurrence, as they do in JSON.
    No caller-owned mutable container is retained in the returned snapshot.
    """
    nodes = 0
    string_bytes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes, string_bytes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise BindingError("json_bound")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int:
            if item.bit_length() > MAX_JSON_INTEGER_BITS:
                raise BindingError("json_bound")
        elif kind is float:
            if not math.isfinite(item):
                raise BindingError("json_number")
        elif kind is str:
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                raise BindingError("json_string") from None
            if string_bytes > MAX_RECEIPT_BYTES:
                raise BindingError("json_bound")
        elif kind is list:
            if nodes + len(item) > MAX_JSON_NODES:
                raise BindingError("json_bound")
            return [visit(child, depth + 1) for child in item]
        elif kind is dict:
            if nodes + 2 * len(item) > MAX_JSON_NODES:
                raise BindingError("json_bound")
            detached: dict[str, Any] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise BindingError("json_key")
                visit(key, depth + 1)
                detached[key] = visit(child, depth + 1)
            return detached
        else:
            raise BindingError("json_type")
        return item

    try:
        return visit(value, 0)
    except (RecursionError, RuntimeError):
        raise BindingError("json_changed") from None


def _canonical(value: Any) -> str:
    detached = _json_value(value)
    try:
        result = json.dumps(
            detached, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise BindingError("terminal_result") from None
    if len(result.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise BindingError("json_bound")
    return result


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BindingError("receipt_duplicate_key")
        value[key] = item
    return value


def _read_receipt_impl(path: object, under: Path) -> tuple[bytes, dict[str, Any]]:
    """Read through a retained, no-follow directory-FD chain.

    Every component, including the supplied root's ancestors, must be a real
    directory. Relative names are resolved from retained parent descriptors,
    never from a later pathname resolution. Because the no-follow walk begins
    at '/', any ancestor component that is a symlink (such as macOS '/tmp' or
    '/var', or a symlinked home) will cause the read to refuse; caller-supplied
    receipt roots should be resolved with realpath at configuration time.
    The chain and leaf must still name the opened inodes after reading. This is
    not an atomic filesystem snapshot or a lock against another writer with
    access to those same inodes.
    """
    if type(path) is not str:
        raise BindingError("receipt_path")
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or any(
            not hasattr(os, flag)
            for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
        )
    ):
        raise BindingError("receipt_primitives")
    file, supplied_root = Path(path), Path(under)
    if ".." in file.parts or ".." in supplied_root.parts:
        raise BindingError("receipt_path")
    root = Path(os.path.abspath(supplied_root))
    file = Path(os.path.abspath(file))
    if not file.is_relative_to(root):
        raise BindingError("receipt_owner")
    relative = file.relative_to(root)
    if file.name != "effect-receipt.json" or "receipts" not in relative.parts[:-1]:
        raise BindingError("receipt_path")
    components = file.parts[1:]
    if not 1 < len(components) <= MAX_PATH_COMPONENTS:
        raise BindingError("receipt_path")
    descriptors: list[int] = []
    chain: list[tuple[int, str, os.stat_result]] = []
    primary: BaseException | None = None
    try:
        directory = os.open(file.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for name in components[:-1]:
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise BindingError("receipt_owner")
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            descriptors.append(child)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise BindingError("receipt_changed")
            chain.append((directory, name, opened))
            directory = child
        name = components[-1]
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_RECEIPT_BYTES
        ):
            raise BindingError("receipt_header")
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        descriptors.append(fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ) != (before.st_dev, before.st_ino, before.st_size):
            raise BindingError("receipt_changed")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise BindingError("receipt_changed")
        for parent, component, expected in chain:
            named = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise BindingError("receipt_changed")
    except BaseException as error:
        primary = error
        raise
    finally:
        closing: BaseException | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except BaseException as error:
                if closing is None:
                    closing = error
        if primary is None and closing is not None:
            raise closing
    try:
        document = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeError, RecursionError):
        raise BindingError("receipt_json") from None
    document = _json_value(document)
    if type(document) is not dict:
        raise BindingError("receipt_shape")
    return raw, document


def _read_receipt(path: object, under: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        return _read_receipt_impl(path, under)
    except BindingError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise BindingError("receipt_unavailable") from None


def _check_document(
    document: dict[str, Any], expected: RequestContext, outcome: object
) -> None:
    from openadapt_flow.verify_run import evidence_problem, receipt_problem

    receipt, evidence = document.get("receipt"), document.get("evidence")
    if type(receipt) is not dict or type(evidence) is not dict:
        raise BindingError("receipt_shape")
    if (
        RequestContext.parse(receipt.get("request_context")) != expected
        or RequestContext.parse(evidence.get("request_context")) != expected
    ):
        raise BindingError("receipt_context_mismatch")
    if (
        receipt_problem(receipt) is not None
        or evidence_problem(evidence, receipt) is not None
    ):
        raise BindingError("receipt_evidence")
    if (
        type(outcome) is not str
        or type(receipt.get("outcome")) is not str
        or receipt["outcome"].upper() != outcome
        or receipt.get("executor_kind") != "flow_replay"
    ):
        raise BindingError("receipt_outcome")


def unbound(reason: str) -> dict[str, str]:
    if reason not in {
        "NO_REQUEST_CONTEXT",
        "NO_RECEIPT",
        "NO_OWNED_RECEIPT_ROOT",
        "LEGACY_TERMINAL",
    }:
        raise BindingError("binding_reason")
    return {"schema": BINDING_SCHEMA, "status": "UNBOUND", "reason": reason}


def capture_receipt_binding(
    result: dict[str, Any], expected: RequestContext, under: Path | None
) -> dict[str, Any]:
    """Capture actual producer bytes before the durable terminal is written.

    An absent receipt is explicitly unbound, including first-run admission;
    its canary receipt must never be substituted for this operation.
    """
    result = _json_value(result)
    if result.get("receipt") is None:
        return unbound("NO_RECEIPT")
    if under is None:
        return unbound("NO_OWNED_RECEIPT_ROOT")
    raw, document = _read_receipt(result.get("receipt"), under)
    _check_document(document, expected, result.get("receipt_outcome"))
    return {
        "schema": BINDING_SCHEMA,
        "status": "BOUND",
        "request_context": expected.to_dict(),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "receipt_bytes": len(raw),
    }


def verify_receipt_binding(
    record: dict[str, Any], reply: dict[str, Any], under: Path
) -> dict[str, Any]:
    """Low-level check against a trusted normal RunStore record.

    Supplied arbitrary records are not authority. WrappedTask.verify_reply
    reads the actual record and also binds the caller's request and arguments.
    Legacy, unbound and refused metadata never succeeds here.
    """
    record, reply = _json_value(record), _json_value(reply)
    if type(record) is not dict or type(reply) is not dict:
        raise BindingError("terminal_result")
    if record.get("state") != "finished":
        raise BindingError("terminal_unfinished")
    expected = RequestContext.from_record(record)
    stored = record.get("result")
    if type(stored) is not dict or type(reply) is not dict:
        raise BindingError("terminal_result")
    compared = dict(reply)
    # The normal replay branch changes this one transport field only.
    if compared.get("replayed") is True and stored.get("replayed") is False:
        compared["replayed"] = False
    if _canonical(stored) != _canonical(compared):
        raise BindingError("terminal_result_mismatch")
    if reply.get("run_id") != expected.run_id or reply.get("request_id") != record.get(
        "request_id"
    ):
        raise BindingError("terminal_identity")
    binding = stored.get("receipt_binding")
    if type(binding) is not dict or set(binding) != {
        "schema",
        "status",
        "request_context",
        "receipt_sha256",
        "receipt_bytes",
    }:
        raise BindingError("receipt_unbound")
    if binding["schema"] != BINDING_SCHEMA or binding["status"] != "BOUND":
        raise BindingError("receipt_unbound")
    if RequestContext.parse(binding["request_context"]) != expected:
        raise BindingError("terminal_context_mismatch")
    if type(binding["receipt_sha256"]) is not str or not _HEX.fullmatch(
        binding["receipt_sha256"]
    ):
        raise BindingError("receipt_digest")
    if (
        type(binding["receipt_bytes"]) is not int
        or not 0 < binding["receipt_bytes"] <= MAX_RECEIPT_BYTES
    ):
        raise BindingError("receipt_size")
    raw, document = _read_receipt(reply.get("receipt"), under)
    if (
        len(raw) != binding["receipt_bytes"]
        or hashlib.sha256(raw).hexdigest() != binding["receipt_sha256"]
    ):
        raise BindingError("receipt_digest_mismatch")
    _check_document(document, expected, reply.get("receipt_outcome"))
    return dict(binding)
